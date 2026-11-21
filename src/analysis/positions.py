"""The model portfolio: every recommendation becomes a tracked position.

Each new idea gets a mechanical plan computed from real price data — buy now
or wait for a pullback, an initial stop-loss, a trailing-stop width, and a
position size — and every later run replays the daily price bars since the
last check against it. Winners are held as long as their trend lasts; there
are no profit targets. Exits are rules, not opinions:

  stop-loss       low trades through the stop          -> sell everything
  breakeven       high reaches +1R (one stop-width)    -> stop moves to entry
  trailing stop   from +1R on                          -> stop follows the high, never down
  flat trade      two weeks in, still below +0.5R      -> sell to free the cash
  thesis broken   the model's daily review says so     -> sell (labelled as judgment)

Because each run replays every bar since the last one, checking less often
doesn't miss a stop: a stop hit on Tuesday is found on Thursday's run and
recorded at Tuesday's price.

Stored at reports/positions.json. On first run, the old picks_ledger.json is
migrated in as "legacy" positions: tracked for the track record, never
turned into buy/sell signals.
"""
import json
from datetime import date, timedelta

from src import config
from src.analysis import account as acct
from src.analysis import strategy as strat
from src.analysis import technicals
from src.fetchers.fmp_client import FMPQuotaExhausted

BOOK_PATH = config.REPORTS_DIR / "positions.json"
LEGACY_LEDGER_PATH = config.REPORTS_DIR / "picks_ledger.json"

ACTIVE = ("pending", "open")
EPS = 1e-6


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _add_days(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _migrate_legacy() -> list[dict]:
    """Converts picks_ledger.json records into legacy positions awaiting a
    price-history backfill. No network calls."""
    try:
        records = json.loads(LEGACY_LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for r in records:
        ticker = (r.get("ticker") or "").strip().upper()
        day = r.get("date")
        if not ticker or not day:
            continue
        category = strat.normalize_category(r.get("category"))
        horizon = strat.normalize_horizon(r.get("horizon"))
        out.append({
            "id": f"legacy-{day}-{ticker}",
            "ticker": ticker,
            "source": "legacy",
            "status": "legacy_unpriced",
            "recommended_on": r.get("first_recommended") or day,
            "category": category,
            "setup": strat.classify_legacy_setup(r.get("thesis"), category),
            "horizon": horizon,
            "horizon_days": strat.HORIZONS[horizon],
            "conviction": None,
            "thesis": r.get("thesis"),
            "legacy_entry_price": r.get("price_at_pick"),
            "exits": [],
            "remaining": 1.0,
            "signals": [],
        })
    return out


BOOK_VERSION = 2

# Fields a replay writes; cleared when an old pick is reset for re-scoring.
_REPLAY_FIELDS = ("plan", "entry", "stop", "high_water", "trailing", "progress_checked", "closed_on",
                  "close_reason", "last_checked", "last_evaluated", "last_price", "last_price_date",
                  "bench_last", "tech", "split_adjustments")


def _upgrade(book: dict) -> dict:
    """Version 2 replaced profit targets and holding-period exits with
    trailing-stop exits. Old picks already scored under the old rules are
    reset so the backfill replays them under the current ones — the track
    record should measure the rules the agent actually uses."""
    if book.get("version", 1) >= BOOK_VERSION:
        return book
    reset = 0
    for p in book["positions"]:
        if p["source"] == "legacy" and p["status"] in ("open", "closed"):
            for k in _REPLAY_FIELDS:
                p.pop(k, None)
            p.update(status="legacy_unpriced", exits=[], remaining=1.0)
            reset += 1
        elif p["source"] == "agent" and p.get("plan") and "trail_pct" not in p["plan"]:
            plan = p["plan"]
            for k in ("target", "target_pct", "rr", "take_fraction", "rsi_exit"):
                plan.pop(k, None)
            plan.update(exit_mode="trail", trail_pct=_trail_pct(plan.get("atr_pct") or 0.03, p["horizon"]),
                        trail_start_pct=round(strat.EXIT_RULES["trail_start_r"] * plan["stop_pct"], 2))
    book["version"] = BOOK_VERSION
    book["rescored_for_v2"] = reset
    return book


def load_book() -> dict:
    if BOOK_PATH.exists():
        try:
            return _upgrade(json.loads(BOOK_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(
                f"{BOOK_PATH} is unreadable. Fix or move it aside — it holds every "
                "tracked position, so it isn't silently replaced."
            )
    legacy = _migrate_legacy()
    return {"version": BOOK_VERSION, "created": date.today().isoformat(),
            "migrated_legacy": len(legacy), "positions": legacy}


def save_book(book: dict) -> None:
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BOOK_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(book, indent=2, default=str), encoding="utf-8")
    tmp.replace(BOOK_PATH)


def agent_positions(book: dict, statuses=ACTIVE) -> list[dict]:
    return [p for p in book["positions"] if p["source"] == "agent" and p["status"] in statuses]


def active_for(book: dict, ticker: str) -> dict | None:
    for p in agent_positions(book):
        if p["ticker"] == ticker:
            return p
    return None


# --------------------------------------------------------------------------
# plan: stop, target, timing, size
# --------------------------------------------------------------------------

def _stop_pct(atr_pct: float, category: str, horizon: str, s: dict) -> float:
    """Initial stop distance: a multiple of ATR, bounded per category."""
    lo, hi = s["stop_bounds_pct"][category]
    raw = s["stop_atr_mult"].get(horizon, 2.0) * atr_pct * 100
    return round(min(max(raw, lo), hi), 2)


def _trail_pct(atr_pct: float, horizon: str) -> float:
    """Trailing-stop distance once the trade is working: a wider multiple of
    ATR (wider still for long trends), bounded."""
    lo, hi = strat.EXIT_RULES["trail_bounds_pct"]
    raw = strat.EXIT_RULES["trail_atr"].get(horizon, 3.0) * atr_pct * 100
    return round(min(max(raw, lo), hi), 2)


def build_plan(price: float, tech: dict, fundamentals: dict, category: str, horizon: str,
               conviction: int, setup: str, s: dict, policy: dict,
               earnings_date: str | None, today: str) -> dict:
    """Mechanical buy plan for a new idea. Sizes here ignore what's already
    held; open_new_ideas() fits them into the remaining room."""
    notes = []

    if setup in policy.get("blocked_setups", {}):
        return {"action": "PASS", "notes": [policy["blocked_setups"][setup]]}

    atr = tech.get("atr_pct")
    if not price or not atr:
        return {"action": "PASS", "notes": ["no price history to set a stop from"]}

    stop_pct = _stop_pct(atr, category, horizon, s)

    rsi, sma20 = tech.get("rsi14"), tech.get("sma20")
    day_change = tech.get("day_change")
    reasons = []
    if rsi is not None and rsi > s["extended_rsi"]:
        reasons.append(f"RSI {rsi:.0f} (overbought)")
    if sma20 and price > sma20 * (1 + s["extended_atr_mult"] * atr):
        reasons.append(f"{(price / sma20 - 1) * 100:.1f}% above its 20-day average")
    if day_change is not None and day_change > s["spike_atr_mult"] * atr:
        reasons.append(f"up {day_change * 100:.1f}% today")

    if reasons:
        action = "WAIT"
        floor = max(sma20 or 0, price * (1 - 1.5 * atr))
        ref = round(min(price * (1 - 0.5 * atr), floor), 2)
        notes.append("extended: " + ", ".join(reasons) + f" — limit buy at ${ref:,.2f}")
    else:
        action = "BUY"
        ref = round(price, 2)

    stop = round(ref * (1 - stop_pct / 100), 2)
    trail_pct = _trail_pct(atr, horizon)

    size = s["risk_per_trade_pct"] / (stop_pct / 100)
    size *= policy.get("conviction_factors", {}).get(str(conviction), 0.8)
    size *= policy.get("setup_size_factor", {}).get(setup, 1.0)

    if tech.get("trend") == "down" and setup not in strat.COUNTER_TREND_SETUPS:
        size *= 0.5
        notes.append("below falling 50/200-day averages — half size")
    if earnings_date and setup != "earnings_catalyst":
        days = _days_between(today, earnings_date)
        if 0 <= days <= s["earnings_buffer_days"]:
            size *= 0.5
            notes.append(f"earnings {earnings_date} — half size")
    if tech.get("source") == "quote_only":
        size *= 0.5
        notes.append("no full price history — levels approximate, half size")

    size = min(size, s["max_position_pct"])
    size = round(size * 2) / 2  # nearest 0.5%
    if size < s["min_position_pct"]:
        return {"action": "PASS", "notes": notes + [f"risk-based size {size:.1f}% below the {s['min_position_pct']:.0f}% minimum"]}

    plan = {
        "action": action,
        "reference_price": ref,
        "price_at_recommendation": round(price, 2),
        "stop": stop, "stop_pct": stop_pct,
        # No profit target: the position rides its trailing stop.
        "exit_mode": "trail",
        "trail_pct": trail_pct,
        "trail_start_pct": round(strat.EXIT_RULES["trail_start_r"] * stop_pct, 2),
        "size_pct": size,
        "atr_pct": atr,
        "notes": notes,
    }
    if action == "WAIT":
        plan["buy_limit"] = ref
        plan["pending_expires"] = _add_days(today, s["pending_days"])
    return plan


# --------------------------------------------------------------------------
# exits
# --------------------------------------------------------------------------

def _bench_close(bench: list[dict], day: str) -> float | None:
    return technicals.close_on_or_before(bench, day) if bench else None


def _exit(p: dict, day: str, price: float, fraction: float, reason: str,
          bench: list[dict], events: list) -> None:
    fraction = min(fraction, p["remaining"])
    p["exits"].append({"date": day, "price": round(price, 4), "fraction": round(fraction, 4),
                       "reason": reason, "bench": _bench_close(bench, day)})
    p["remaining"] = round(p["remaining"] - fraction, 6)
    events.append({"type": "exit", "date": day, "price": round(price, 2),
                   "fraction": fraction, "reason": reason})
    if p["remaining"] <= EPS:
        p["remaining"] = 0.0
        p["status"] = "closed"
        p["closed_on"] = day
        p["close_reason"] = reason


def _apply_exit_rules(p: dict, bars: list[dict], bench: list[dict], s: dict, events: list) -> None:
    """Rides the position until its stop is hit. No targets, no time limit
    for a trade that's working — see strategy.EXIT_RULES."""
    rules = strat.EXIT_RULES
    entry = p["entry"]["price"]
    entry_day = p["entry"]["date"]
    plan = p["plan"]
    risk = plan["stop_pct"] / 100
    trail = (plan.get("trail_pct") or _trail_pct(plan.get("atr_pct") or 0.03, p["horizon"])) / 100
    for b in bars:
        if p["remaining"] <= EPS:
            break
        o = b.get("open")
        hi = b.get("high") if b.get("high") is not None else b["close"]
        lo = b.get("low") if b.get("low") is not None else b["close"]

        # The stop in force coming into the bar is checked first; a gap down
        # fills at the open.
        if lo <= p["stop"]:
            px = min(o, p["stop"]) if o else p["stop"]
            if p["stop"] < entry - EPS:
                reason = "stop-loss"
            elif p.get("trailing") and p["stop"] > entry + EPS:
                reason = "trailing stop"
            else:
                reason = "breakeven stop"
            _exit(p, b["date"], px, p["remaining"], reason, bench, events)
            break

        p["high_water"] = max(p.get("high_water") or entry, hi)
        old = p["stop"]
        gain_r = (p["high_water"] / entry - 1) / risk if risk else 0.0
        if gain_r >= rules["breakeven_r"]:
            p["stop"] = max(p["stop"], entry)
        if gain_r >= rules["trail_start_r"]:
            if not p.get("trailing"):
                p["trailing"] = True
                events.append({"type": "trailing_started", "date": b["date"],
                               "trail_pct": round(trail * 100, 2)})
            p["stop"] = max(p["stop"], round(p["high_water"] * (1 - trail), 4))
        if p["stop"] > old + EPS:
            events.append({"type": "stop_raised", "date": b["date"], "from": round(old, 2),
                           "to": round(p["stop"], 2)})

        # One check, two weeks in: a trade that hasn't made meaningful
        # progress is sold to free the cash. Trades that are working are
        # never cut on time.
        if (not p.get("progress_checked")
                and _days_between(entry_day, b["date"]) >= rules["no_progress_days"]):
            p["progress_checked"] = True
            if b["close"] < entry * (1 + rules["no_progress_min_r"] * risk):
                _exit(p, b["date"], b["close"], p["remaining"], "no progress — freed the cash", bench, events)
                break


def _fill_pending(p: dict, bars: list[dict], bench: list[dict]) -> list[dict]:
    """Fills a WAIT order on the first bar that trades at or below the
    limit. Returns the bars after the fill day (to run exits over)."""
    limit = p["plan"]["buy_limit"]
    for i, b in enumerate(bars):
        lo = b.get("low") if b.get("low") is not None else b["close"]
        if lo <= limit:
            o = b.get("open")
            px = min(o, limit) if o else limit
            p["status"] = "open"
            p["entry"] = {"date": b["date"], "price": round(px, 4), "bench": _bench_close(bench, b["date"])}
            p["stop"] = p["plan"]["stop"]
            p["high_water"] = px
            return bars[i + 1:]
    return []


SPLIT_LOW, SPLIT_HIGH = 0.67, 1.5


def _adjust_for_split(p: dict, bars: list[dict], events: list) -> None:
    """FMP's price history is split-adjusted; a recorded entry price isn't.
    If the history's close on the entry (or recommendation) day disagrees
    with the recorded price by more than a split-sized factor, rescale every
    stored price level. A real entry-day move of 33%+ intraday is rare
    enough that the threshold doesn't misfire on ordinary volatility."""
    if p["status"] == "open" and p.get("entry"):
        day, recorded = p["entry"]["date"], p["entry"]["price"]
    elif p["status"] == "pending":
        day, recorded = p["recommended_on"], (p.get("plan") or {}).get("price_at_recommendation")
    else:
        return
    ref = technicals.close_on_or_before(bars, day)
    if not ref or not recorded:
        return
    f = ref / recorded
    if SPLIT_LOW <= f <= SPLIT_HIGH:
        return
    plan = p.get("plan") or {}
    for k in ("reference_price", "price_at_recommendation", "stop", "target", "buy_limit"):
        if plan.get(k) is not None:
            plan[k] = round(plan[k] * f, 4)
    for k in ("stop", "high_water", "last_price"):
        if p.get(k) is not None:
            p[k] = round(p[k] * f, 4)
    if p.get("entry"):
        p["entry"]["price"] = round(p["entry"]["price"] * f, 4)
    for x in p["exits"]:
        x["price"] = round(x["price"] * f, 4)
    p.setdefault("split_adjustments", []).append({"factor": round(f, 4), "detected": bars[-1]["date"]})
    events.append({"type": "split", "factor": round(f, 4)})


def _evaluate(p: dict, bars: list[dict], bench: list[dict], today: str, s: dict) -> list[dict]:
    """Runs one position forward over its new bars. Returns events."""
    events = []
    _adjust_for_split(p, bars, events)
    new_bars = technicals.bars_after(bars, p.get("last_checked"))

    if p["status"] == "pending":
        rest = _fill_pending(p, new_bars, bench)
        if p["status"] == "open":
            events.append({"type": "filled", "date": p["entry"]["date"], "price": round(p["entry"]["price"], 2)})
            new_bars = rest
        elif today > p["plan"]["pending_expires"]:
            p["status"] = "cancelled"
            p["closed_on"] = today
            p["close_reason"] = "limit never reached"
            events.append({"type": "cancelled", "date": today})
            new_bars = []
        else:
            new_bars = []

    if p["status"] == "open" and new_bars:
        _apply_exit_rules(p, new_bars, bench, s, events)

    p["last_checked"] = bars[-1]["date"]
    p["last_price"] = bars[-1]["close"]
    p["last_price_date"] = bars[-1]["date"]
    p["bench_last"] = _bench_close(bench, bars[-1]["date"])
    tech = technicals.compute(bars)
    p["tech"] = {k: tech.get(k) for k in ("trend", "rsi14", "ret_1m", "pct_from_52w_high", "sma50", "sma200")}
    return events


# --------------------------------------------------------------------------
# per-run operations
# --------------------------------------------------------------------------

def backfill_legacy(book: dict, history_fn, bench: list[dict], s: dict) -> dict:
    """Prices pre-rebuild picks from history and replays them through the
    exit rules — a backtest of what the old picks would have done under
    these rules. Spends at most `legacy_backfill_per_run` history calls."""
    pending = [p for p in book["positions"] if p["status"] == "legacy_unpriced"]
    pending.sort(key=lambda p: p["recommended_on"])
    done = unscorable = 0
    for p in pending[: s["legacy_backfill_per_run"]]:
        try:
            bars = history_fn(p["ticker"], critical=False)
        except FMPQuotaExhausted:
            break
        day = p["recommended_on"]
        upto = [b for b in bars if b["date"] <= day]
        # Entry comes from the same (split-adjusted) series as every later
        # price. The old ledger's raw quote is kept for reference only: a
        # split since then would otherwise show up as a huge gain or loss.
        entry = upto[-1]["close"] if upto else None
        if not bars or not upto or not entry:
            p["status"] = "unscorable"
            p["close_reason"] = "no price history for the recommendation date"
            unscorable += 1
            continue
        tech = technicals.compute(upto)
        atr = tech.get("atr_pct") or 0.03
        stop_pct = _stop_pct(atr, p["category"], p["horizon"], s)
        p["plan"] = {
            "reference_price": round(entry, 4), "stop_pct": stop_pct,
            "stop": round(entry * (1 - stop_pct / 100), 4),
            "exit_mode": "trail", "trail_pct": _trail_pct(atr, p["horizon"]),
            "atr_pct": atr, "backfilled": True,
        }
        p["status"] = "open"
        p["entry"] = {"date": day, "price": round(entry, 4), "bench": _bench_close(bench, day)}
        p["stop"] = p["plan"]["stop"]
        p["high_water"] = entry
        p["last_checked"] = day
        _evaluate(p, bars, bench, date.today().isoformat(), s)
        p["last_evaluated"] = date.today().isoformat()
        done += 1
    remaining = sum(1 for p in book["positions"] if p["status"] == "legacy_unpriced")
    return {"backfilled": done, "unscorable": unscorable, "remaining": remaining}


def update_positions(book: dict, today: str, history_fn, bench: list[dict], s: dict) -> list[dict]:
    """Advances every active position through the bars since its last
    check. Returns one signal per agent position; legacy ones are updated
    silently (weekly) since they were never meant to be traded."""
    signals = []
    for p in book["positions"]:
        if p["status"] not in ACTIVE:
            continue
        if p["source"] == "legacy":
            last = p.get("last_evaluated")
            if last and _days_between(last, today) < s["legacy_check_days"]:
                continue
        try:
            bars = history_fn(p["ticker"], critical=(p["source"] == "agent"))
        except FMPQuotaExhausted:
            if p["source"] == "agent":
                signals.append(_signal(p, "NOT CHECKED", "FMP quota ran out before this position was priced — check it manually."))
            continue
        if not bars:
            if p["source"] == "agent":
                signals.append(_signal(p, "NO DATA", "No price history from FMP today — check the price, stop and target manually."))
            continue
        events = _evaluate(p, bars, bench, today, s)
        p["last_evaluated"] = today
        if p["source"] == "agent":
            signals.append(_signal_from_events(p, events))
    return signals


def apply_reviews(book: dict, reviews: list[dict], today: str, bench: list[dict]) -> list[dict]:
    """Applies the model's daily thesis reviews to agent positions. 'broken'
    sells an open position or cancels a pending one; 'watch' is recorded."""
    signals = []
    by_ticker = {(r.get("ticker") or "").upper(): r for r in reviews}
    for p in agent_positions(book):
        r = by_ticker.get(p["ticker"])
        if not r:
            continue
        verdict = (r.get("verdict") or "").strip().lower()
        note = (r.get("note") or "").strip()
        p.setdefault("reviews", []).append({"date": today, "verdict": verdict, "note": note})
        p["reviews"] = p["reviews"][-10:]
        if verdict != "broken":
            continue
        if p["status"] == "pending":
            p["status"] = "cancelled"
            p["closed_on"] = today
            p["close_reason"] = "thesis broken before entry (model review)"
            signals.append(_signal(p, "CANCEL", f"Cancel the limit buy — thesis broken: {note}"))
        elif p["status"] == "open" and p.get("last_price"):
            events = []
            _exit(p, p.get("last_price_date") or today, p["last_price"], p["remaining"],
                  "thesis broken (model review)", bench, events)
            signals.append(_signal(p, "SELL", f"Thesis broken (model judgment, not a price rule): {note}"))
    return signals


def open_new_ideas(book: dict, ideas: list[dict], today: str, s: dict, policy: dict,
                   earnings: dict, quote_fn, bench_price: float | None,
                   account: dict | None = None) -> list[dict]:
    """Turns the model's new ideas into BUY / WAIT / PASS signals and opens
    the positions. Ideas are taken highest conviction first and must fit
    the category and total-exposure limits from the investor profile.

    With a live brokerage snapshot (`account`), limits are measured against
    what you actually hold and sizes become whole-share counts from your
    real equity; without one, against the model portfolio, in percent."""
    signals = []
    equity = (account or {}).get("equity") or 0
    if account and equity > 0:
        exposure, total = acct.live_exposure(account, book)
    else:
        account = None
        exposure, total = _exposure(book)
    ideas = sorted(ideas, key=lambda i: -(i.get("conviction") or 0))[: s["max_new_ideas"]]

    for idea in ideas:
        t = idea["ticker"]
        if active_for(book, t):
            signals.append({"ticker": t, "action": "ALREADY OPEN",
                            "detail": "Re-recommended today; existing plan unchanged."})
            continue
        recent_stop = _recent_stop_out(book, t, today, s)
        if recent_stop:
            signals.append(_idea_signal(idea, "PASS", f"stopped out {recent_stop} — {s['reentry_cooldown_days']}-day cooldown"))
            continue
        try:
            quote = quote_fn(t)
        except FMPQuotaExhausted:
            signals.append(_idea_signal(idea, "PASS", "FMP quota ran out before a live price could be fetched"))
            continue
        price = quote.get("price")
        if not price:
            signals.append(_idea_signal(idea, "PASS", "no live price available"))
            continue

        plan = build_plan(price, idea.get("technicals") or {}, idea.get("fundamentals") or {},
                          idea["category"], idea["horizon"], idea.get("conviction") or 3,
                          idea["setup"], s, policy, earnings.get(t), today)
        if plan["action"] == "PASS":
            signals.append(_idea_signal(idea, "PASS", "; ".join(plan["notes"])))
            continue

        bucket_room = s["bucket_max_pct"][idea["category"]] - exposure.get(idea["category"], 0.0)
        total_room = s["max_total_exposure_pct"] - total
        room = min(bucket_room, total_room)
        if room < s["min_position_pct"]:
            which = (f"{strat.CATEGORIES[idea['category']]} category is at its "
                     f"{s['bucket_max_pct'][idea['category']]:.0f}% limit") if bucket_room <= total_room \
                else f"portfolio is at its {s['max_total_exposure_pct']:.0f}% invested limit"
            signals.append(_idea_signal(idea, "PASS", which))
            continue
        if plan["size_pct"] > room:
            plan["notes"].append(f"size cut from {plan['size_pct']}% to fit allocation limits")
            plan["size_pct"] = round(room * 2) / 2

        if account:
            # The per-position cap covers shares you already own, not just this buy.
            have = acct.held(account).get(t)
            have_pct = 100 * (have.get("market_value") or 0) / equity if have else 0.0
            pos_room = s["max_position_pct"] - have_pct
            if pos_room < s["min_position_pct"]:
                signals.append(_idea_signal(idea, "PASS", f"you already hold {have['qty']:g} shares "
                                                          f"({have_pct:.1f}% of the account; limit "
                                                          f"{s['max_position_pct']:.0f}%)"))
                continue
            if have:
                plan["notes"].append(f"adds to the {have['qty']:g} shares you already hold "
                                     f"({have_pct:.1f}% of the account)")
            buy_at = plan.get("buy_limit") or price
            shares, dollars, note = acct.share_plan(min(plan["size_pct"], pos_room), buy_at, equity, pos_room)
            if not shares:
                signals.append(_idea_signal(idea, "PASS", note))
                continue
            actual_pct = round(100 * dollars / equity, 1)
            if actual_pct > room + 1e-9:
                signals.append(_idea_signal(idea, "PASS", f"one share ({actual_pct}% of the account) "
                                                          f"doesn't fit the remaining allocation room"))
                continue
            if note:
                plan["notes"].append(note)
            plan.update(shares=shares, dollars=dollars, size_pct=actual_pct)

        p = {
            "id": f"{today}-{t}",
            "ticker": t,
            "source": "agent",
            "status": "open" if plan["action"] == "BUY" else "pending",
            "recommended_on": today,
            "category": idea["category"],
            "setup": idea["setup"],
            "horizon": idea["horizon"],
            "horizon_days": strat.HORIZONS[idea["horizon"]],
            "conviction": idea.get("conviction"),
            "thesis": idea.get("thesis"),
            "key_risk": idea.get("key_risk"),
            "invalidation": idea.get("invalidation"),
            "catalyst": idea.get("catalyst"),
            "plan": plan,
            "exits": [],
            "remaining": 1.0,
            "last_checked": today,
            "last_price": price,
            "last_price_date": today,
            "signals": [],
        }
        if p["status"] == "open":
            p["entry"] = {"date": today, "price": round(price, 4), "bench": bench_price}
            p["stop"] = plan["stop"]
            p["high_water"] = price
            p["bench_last"] = bench_price
        book["positions"].append(p)
        exposure[idea["category"]] = exposure.get(idea["category"], 0.0) + plan["size_pct"]
        total += plan["size_pct"]

        ride = (f"No profit target: once it's up {plan['trail_start_pct']}%, the stop becomes a "
                f"{plan['trail_pct']}% trailing stop and the trade rides until that's hit.")
        if account:
            n = plan["shares"]
            orders = acct.order_instructions(n, plan["stop"])
            if plan["action"] == "BUY":
                detail = (f"Buy {n} share{'s' if n != 1 else ''} (~${plan['dollars']:,.2f}, "
                          f"{plan['size_pct']}% of the account) near ${price:,.2f}. Then {orders} {ride}")
            else:
                detail = (f"GTC limit buy {n} share{'s' if n != 1 else ''} at ${plan['buy_limit']:,.2f} or lower "
                          f"(now ${price:,.2f}), valid until {plan['pending_expires']}. Once it fills, {orders} {ride}")
        elif plan["action"] == "BUY":
            detail = (f"Buy ~{plan['size_pct']}% of portfolio near ${price:,.2f}. "
                      f"Stop-loss ${plan['stop']:,.2f} (-{plan['stop_pct']}%). {ride}")
        else:
            detail = (f"Limit buy ~{plan['size_pct']}% at ${plan['buy_limit']:,.2f} or lower "
                      f"(now ${price:,.2f}), valid until {plan['pending_expires']}. "
                      f"Then stop ${plan['stop']:,.2f}. {ride}")
        if plan["notes"]:
            detail += " Notes: " + "; ".join(plan["notes"]) + "."
        sig = _signal(p, plan["action"], detail)
        p["signals"].append({"date": today, "action": sig["action"], "detail": detail})
        signals.append(sig)
    return signals


def _exposure(book: dict) -> tuple[dict, float]:
    by_cat: dict[str, float] = {}
    for p in agent_positions(book):
        size = (p.get("plan") or {}).get("size_pct") or 0.0
        frac = p["remaining"] if p["status"] == "open" else 1.0
        by_cat[p["category"]] = by_cat.get(p["category"], 0.0) + size * frac
    return by_cat, sum(by_cat.values())


def _recent_stop_out(book: dict, ticker: str, today: str, s: dict) -> str | None:
    for p in book["positions"]:
        if (p["ticker"] == ticker and p["source"] == "agent" and p["status"] == "closed"
                and "stop" in (p.get("close_reason") or "") and p.get("closed_on")
                and _days_between(p["closed_on"], today) < s["reentry_cooldown_days"]):
            return p["closed_on"]
    return None


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

def position_return(p: dict) -> tuple[float | None, float | None]:
    """(return %, benchmark return %) over the position's life so far,
    blending realized exits with the open remainder at the last price."""
    entry = (p.get("entry") or {}).get("price")
    bench0 = (p.get("entry") or {}).get("bench")
    if not entry:
        return None, None
    ret = bench_ret = 0.0
    bench_ok = bool(bench0)
    for x in p["exits"]:
        ret += x["fraction"] * (x["price"] / entry - 1)
        if bench_ok and x.get("bench"):
            bench_ret += x["fraction"] * (x["bench"] / bench0 - 1)
        else:
            bench_ok = False
    if p["remaining"] > EPS:
        last = p.get("last_price") or entry
        ret += p["remaining"] * (last / entry - 1)
        if bench_ok and p.get("bench_last"):
            bench_ret += p["remaining"] * (p["bench_last"] / bench0 - 1)
        else:
            bench_ok = False
    return round(ret * 100, 2), (round(bench_ret * 100, 2) if bench_ok else None)


def _signal(p: dict, action: str, detail: str) -> dict:
    ret, bench = position_return(p)
    return {
        "ticker": p["ticker"], "action": action, "detail": detail,
        "position_id": p["id"], "category": p["category"], "setup": p["setup"],
        "conviction": p.get("conviction"), "horizon": p["horizon"],
        "status": p["status"],
        "entry": (p.get("entry") or {}).get("price"),
        "price": p.get("last_price"),
        "stop": p.get("stop") or (p.get("plan") or {}).get("stop"),
        "stop_phase": "trailing" if p.get("trailing") else (
            "breakeven" if p.get("entry") and (p.get("stop") or 0) >= p["entry"]["price"] - EPS else "initial"),
        "trail_pct": (p.get("plan") or {}).get("trail_pct"),
        "trail_start_pct": (p.get("plan") or {}).get("trail_start_pct"),
        "buy_limit": (p.get("plan") or {}).get("buy_limit"),
        "size_pct": (p.get("plan") or {}).get("size_pct"),
        "shares": (p.get("plan") or {}).get("shares"),
        "dollars": (p.get("plan") or {}).get("dollars"),
        "remaining": p["remaining"],
        "return_pct": ret, "bench_return_pct": bench,
    }


def _idea_signal(idea: dict, action: str, detail: str) -> dict:
    return {"ticker": idea["ticker"], "action": action, "detail": detail,
            "category": idea["category"], "setup": idea["setup"],
            "conviction": idea.get("conviction"), "horizon": idea["horizon"]}


def _signal_from_events(p: dict, events: list[dict]) -> dict:
    exits = [e for e in events if e["type"] == "exit"]
    raised = [e for e in events if e["type"] == "stop_raised"]
    filled = [e for e in events if e["type"] == "filled"]
    cancelled = [e for e in events if e["type"] == "cancelled"]

    if cancelled:
        return _signal(p, "CANCEL", f"Limit buy at ${p['plan']['buy_limit']:,.2f} never filled; cancel the order.")
    if p["status"] == "closed":
        last = exits[-1]
        if last["reason"].startswith("no progress"):
            detail = (f"No real progress two weeks in (close ${last['price']:,.2f} on {last['date']}) — sell it "
                      "and cancel its stop to free the cash for a better idea.")
        elif last["reason"] == "trailing stop":
            detail = (f"Trailing stop hit {last['date']} at ${last['price']:,.2f} — the uptrend turned, trade "
                      "done. Your trailing stop order should have sold it; if not, sell now.")
        else:
            detail = (f"{last['reason'].capitalize()} hit {last['date']} at ${last['price']:,.2f}. "
                      "If your stop order wasn't in place, sell now.")
        sig = _signal(p, "SELL", detail)
        p["signals"].append({"date": date.today().isoformat(), "action": "SELL", "detail": detail})
        return sig
    parts = []
    split = [e for e in events if e["type"] == "split"]
    if split:
        parts.append(f"Stock split detected (x{1 / split[0]['factor']:.4g}); all levels rescaled — "
                     f"update your stop and limit orders to the prices shown.")
    if filled:
        parts.append(f"Limit buy filled {filled[0]['date']} at ${filled[0]['price']:,.2f}.")
    started = [e for e in events if e["type"] == "trailing_started"]
    trail = (p.get("plan") or {}).get("trail_pct")
    if started:
        parts.append(f"Up enough to trail ({started[0]['date']}): switch your stop to a {trail}% trailing "
                     f"stop — at least ${p['stop']:,.2f} — and let it ride.")
    elif raised:
        parts.append(f"Raise stop to ${p['stop']:,.2f} (was ${raised[0]['from']:,.2f}).")
    if p["status"] == "pending":
        return _signal(p, "WAIT", f"Limit buy at ${p['plan']['buy_limit']:,.2f} still open until {p['plan']['pending_expires']} (now ${p['last_price']:,.2f}).")
    action = ("UPDATE ORDERS" if split else "TRAILING" if started else "RAISE STOP" if raised
              else "BOUGHT" if filled else "HOLD")
    if not parts:
        if p.get("trailing"):
            parts.append(f"Riding the trend: {trail}% trailing stop, now ${p['stop']:,.2f}.")
        else:
            parts.append(f"Stop ${p['stop']:,.2f}; becomes a {trail}% trailing stop once up "
                         f"{(p.get('plan') or {}).get('trail_start_pct', '?')}%.")
    sig = _signal(p, action, " ".join(parts))
    p["signals"].append({"date": date.today().isoformat(), "action": action, "detail": sig["detail"]})
    p["signals"] = p["signals"][-30:]
    return sig
