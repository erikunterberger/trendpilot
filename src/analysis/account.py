"""Uses a live brokerage snapshot (see fetchers/broker_alpaca.py) for:

- allocation limits from what you actually hold, not what the model
  portfolio assumes you bought;
- whole-share sizing from your real equity;
- order instructions that Alpaca will accept;
- checks that every position the plan manages has a stop order in place at
  the plan's level, and that nothing the plan exited is still held.

Whole shares, not fractional: at Alpaca, orders on fractional shares can
only be DAY orders, so a stop-loss on a fractional position lapses every
evening and OCO/bracket protection isn't available. A plan built around
standing stops needs whole shares.

The model portfolio in positions.json still records every recommendation
whether or not you bought it — that's what the track record and learning
measure. This module is about *your* account.
"""
import math

from src.analysis import strategy as strat

PRICE_TOLERANCE = 0.005   # a stop within 0.5% of the plan's level counts as matching
STOP_TYPES = ("stop", "stop_limit", "trailing_stop")

SEVERITY = ["SELL", "NO STOP", "PARTIAL STOP", "RAISE STOP", "SWITCH TO TRAILING", "STOP EXPIRES",
            "PROTECTED", "NOT BOUGHT", "UNTRACKED"]


def held(snapshot: dict | None) -> dict:
    """symbol -> position, long positions with a non-zero quantity only."""
    if not snapshot:
        return {}
    return {p["symbol"]: p for p in snapshot.get("positions", [])
            if (p.get("qty") or 0) > 0 and p.get("side", "long") == "long"}


def latest_agent_position(book: dict, ticker: str) -> dict | None:
    mine = [p for p in book["positions"] if p["source"] == "agent" and p["ticker"] == ticker]
    return max(mine, key=lambda p: p["recommended_on"]) if mine else None


def live_exposure(snapshot: dict, book: dict) -> tuple[dict, float]:
    """Exposure as % of account equity: what you hold, priced now, plus
    limit buys the plan still has working. Holdings the agent didn't pick
    count toward the total but not toward any category."""
    equity = snapshot.get("equity") or 0
    if equity <= 0:
        return {}, 0.0
    by_cat: dict[str, float] = {}
    total = 0.0
    for sym, pos in held(snapshot).items():
        pct = 100 * (pos.get("market_value") or 0) / equity
        total += pct
        p = latest_agent_position(book, sym)
        if p:
            by_cat[p["category"]] = by_cat.get(p["category"], 0.0) + pct
    for p in book["positions"]:
        if p["source"] == "agent" and p["status"] == "pending":
            size = (p.get("plan") or {}).get("size_pct") or 0.0
            by_cat[p["category"]] = by_cat.get(p["category"], 0.0) + size
            total += size
    return by_cat, total


def share_plan(size_pct: float, price: float, equity: float, max_pct: float) -> tuple[int, float, str | None]:
    """Whole shares for a target % of equity. Rounds down, except that a
    single share is allowed when it stays inside the per-position cap.
    Returns (shares, dollars, note); shares == 0 means it can't be sized."""
    if not price or not equity:
        return 0, 0.0, "no account equity or price to size from"
    shares = math.floor(equity * size_pct / 100 / price)
    if shares >= 1:
        return shares, round(shares * price, 2), None
    if price <= equity * max_pct / 100:
        return 1, round(price, 2), (f"1 share (${price:,.2f}, {100 * price / equity:.1f}%) is above the "
                                    f"risk-based size but inside the per-position limit")
    return 0, 0.0, (f"one share (${price:,.2f}) is more than the {max_pct:.1f}% of the account "
                    f"(${equity * max_pct / 100:,.2f}) this position has room for")


def order_instructions(shares: int, stop: float) -> str:
    """One stop covers the whole position; there's no take-profit order,
    because winners are held until their trailing stop is hit."""
    return f"place a GTC stop order for all {shares} share{'s' if shares != 1 else ''} at ${stop:,.2f}."


def _issue(sym, action, detail, pos=None, p=None, stop_now=None):
    return {
        "ticker": sym, "action": action, "detail": detail,
        "qty": (pos or {}).get("qty"), "market_value": (pos or {}).get("market_value"),
        "unrealized_plpc": (pos or {}).get("unrealized_plpc"),
        "stop_order": stop_now, "plan_stop": (p or {}).get("stop") or ((p or {}).get("plan") or {}).get("stop"),
        "trail_pct": ((p or {}).get("plan") or {}).get("trail_pct"),
    }


def can_switch_to_trailing(price: float | None, trail_pct: float | None, plan_stop: float | None) -> bool:
    """An Alpaca trailing stop starts tracking from the price when it's
    placed, not from the stock's earlier high. Switching is only safe when
    that starting level is at least as protective as the plan's stop;
    otherwise a fixed stop at the plan's level is better for now."""
    return bool(price and trail_pct and plan_stop) and price * (1 - trail_pct / 100) >= plan_stop - 1e-9


def check_account(snapshot: dict, book: dict) -> list[dict]:
    """One line per held position (and per plan position you don't hold),
    worst problem first."""
    out = []
    positions = held(snapshot)
    orders = snapshot.get("orders", [])

    for sym, pos in positions.items():
        qty = pos["qty"]
        p = latest_agent_position(book, sym)
        if p is None:
            out.append(_issue(sym, "UNTRACKED", "Not one of the agent's picks — these rules don't manage it.", pos))
            continue
        if p["status"] in ("closed", "cancelled"):
            why = p.get("close_reason") or "plan exit"
            out.append(_issue(sym, "SELL", f"The plan exited on {p.get('closed_on')} ({why}) but you still hold "
                                            f"{qty:g} shares — sell them and cancel any open orders.", pos, p))
            continue

        plan_stop = p.get("stop") or (p.get("plan") or {}).get("stop")
        trail_pct = (p.get("plan") or {}).get("trail_pct")
        stops = [o for o in orders if o["symbol"] == sym and o["side"] == "sell"
                 and o["type"] in STOP_TYPES and (o.get("stop_price") or o.get("trail_percent"))]
        trailing = [o for o in stops if o["type"] == "trailing_stop"]
        fixed = [o for o in stops if o["type"] != "trailing_stop"]
        covered = sum(o.get("qty") or 0 for o in stops)
        low = min((o["stop_price"] for o in stops if o.get("stop_price")), default=None)
        problems = []
        if not stops:
            problems.append(("NO STOP", f"No stop-loss order on your {qty:g} shares — place a GTC stop at ${plan_stop:,.2f}."))
        else:
            if covered + 1e-9 < qty:
                problems.append(("PARTIAL STOP", f"Stops cover {covered:g} of {qty:g} shares — add a GTC stop "
                                                  f"for the other {qty - covered:g} at ${plan_stop:,.2f}."))
            # Fixed stops are judged by level. Trailing stops by their trail
            # width: Alpaca tracks its own high-water mark, which differs
            # from the daily-bar one the plan uses.
            low_fixed = min((o["stop_price"] for o in fixed if o.get("stop_price")), default=None)
            if plan_stop and low_fixed is not None and low_fixed < plan_stop * (1 - PRICE_TOLERANCE):
                problems.append(("RAISE STOP", f"Your stop is ${low_fixed:,.2f}; the plan's stop is now ${plan_stop:,.2f} "
                                                f"— replace the order at the new level."))
            wide = [o for o in trailing if trail_pct and (o.get("trail_percent") or 0) > trail_pct + 0.5]
            if wide:
                problems.append(("RAISE STOP", f"Your trailing stop is {wide[0]['trail_percent']:g}%; the plan's is "
                                                f"{trail_pct:g}% — tighten it."))
            if (p.get("trailing") and fixed and not trailing
                    and can_switch_to_trailing(pos.get("current_price"), trail_pct, plan_stop)):
                problems.append(("SWITCH TO TRAILING", f"The trade is working — replace your fixed stop with a "
                                                        f"GTC {trail_pct:g}% trailing stop so it follows the price up."))
            if any(o.get("time_in_force") == "day" for o in stops):
                frac = qty != int(qty)
                problems.append(("STOP EXPIRES", "A stop is a DAY order and lapses at today's close — "
                                 + ("fractional shares only allow day orders at Alpaca, so it must be re-placed "
                                    "daily (or sell the fraction)." if frac else "re-place it as GTC.")))
        if problems:
            problems.sort(key=lambda x: SEVERITY.index(x[0]))
            out.append(_issue(sym, problems[0][0], " ".join(d for _, d in problems), pos, p, low))
        elif trailing:
            pct = trailing[0].get("trail_percent")
            out.append(_issue(sym, "PROTECTED", f"{qty:g} shares riding a "
                                                + (f"{pct:g}% " if pct else "") + "trailing stop"
                                                + (f" (now ${low:,.2f})." if low else "."), pos, p, low))
        else:
            out.append(_issue(sym, "PROTECTED", f"{qty:g} shares, stop ${low:,.2f} in place.", pos, p, low))

    for p in book["positions"]:
        if p["source"] == "agent" and p["status"] == "open" and p["ticker"] not in positions:
            out.append(_issue(p["ticker"], "NOT BOUGHT",
                              "You don't hold this — it's tracked as a model position for the track record only.",
                              None, p))

    out.sort(key=lambda x: (SEVERITY.index(x["action"]), x["ticker"]))
    return out


def margin_warning(snapshot: dict) -> str | None:
    """None if the account can only spend deposited cash; otherwise what's
    wrong. Alpaca opens every account as a margin account; below $2,000 of
    equity it's held to 1x anyway, but above that an order bigger than your
    cash would quietly borrow unless the margin multiplier is set to 1."""
    m = snapshot.get("multiplier")
    problems = []
    if m is not None and m > 1:
        problems.append(f"borrowing is enabled ({m:g}x buying power) — an order bigger than your cash "
                        f"would be a margin loan. Set the account's max margin multiplier to 1")
    # Alpaca blocks short selling below $2,000 of equity whatever the flag
    # says, so the flag only matters at or above that.
    if snapshot.get("shorting_enabled") and (snapshot.get("equity") or 0) >= 2000:
        problems.append("short selling is enabled — turn it off in the account configuration")
    return "; ".join(problems) or None


def summary(snapshot: dict) -> dict:
    eq = snapshot.get("equity") or 0
    invested = sum(p.get("market_value") or 0 for p in held(snapshot).values())
    return {
        "env": snapshot.get("env"), "as_of": snapshot.get("as_of"),
        "equity": eq, "cash": snapshot.get("cash"), "invested": round(invested, 2),
        "invested_pct": round(100 * invested / eq, 1) if eq else None,
        "positions": len(held(snapshot)),
        "day_change_pct": (round(100 * (eq / snapshot["last_equity"] - 1), 2)
                           if eq and snapshot.get("last_equity") else None),
    }


def category_label(cat: str | None) -> str:
    return strat.CATEGORIES.get(cat or "", "—")
