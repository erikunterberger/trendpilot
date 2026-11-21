"""Code-generated report sections: the Action Plan and the Track Record.

Every number here comes from price data and the position book, never from
model text, so these two sections can be trusted the way the narrative
can't.
"""
from src.analysis import account as acct
from src.analysis import strategy as strat

UPDATE_ORDER = ["SELL", "CANCEL", "UPDATE ORDERS", "TRAILING", "RAISE STOP", "BOUGHT", "NO DATA", "NOT CHECKED"]
QUIET_ACCOUNT = ("PROTECTED", "NOT BOUGHT", "UNTRACKED")


def _money(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _pct(v, signed=True) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _pts(v) -> str:
    return "—" if v is None else f"{v:+.1f} pts"


def _held(t: str, held: set) -> str:
    return f"**{t}** (you hold)" if t in held else f"**{t}**"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]
    return "\n".join(out)


def _notes(s: dict) -> str:
    return s["detail"].split("Notes: ", 1)[1] if "Notes: " in s["detail"] else ""


def _size(s: dict) -> str:
    if s.get("shares"):
        return f"{s['shares']} sh (~{_money(s.get('dollars'))}, {s['size_pct']:.1f}%)"
    return _pct(s.get("size_pct"), False)


def _trail(s: dict) -> str:
    """When and how the stop starts following the price up."""
    if not s.get("trail_pct"):
        return "—"
    start = s.get("trail_start_pct")
    return f"{s['trail_pct']:g}% trail" + (f" once +{start:g}%" if start else "")


def _phase(s: dict) -> str:
    return {"trailing": "trailing", "breakeven": "at breakeven", "initial": "initial"}.get(s.get("stop_phase"), "—")


def action_plan(new_signals: list[dict], position_signals: list[dict], held: set,
                notes: list[str] | None = None, account: dict | None = None,
                account_signals: list[dict] | None = None, trades: list[dict] | None = None,
                paper_perf: dict | None = None, autotrade: bool = False) -> str:
    rides = ("There are **no profit targets**: once a trade is up by one stop-width, its stop "
             "becomes a trailing stop that follows the price up and only sells when the trend "
             "turns. A trade still going nowhere after two weeks is sold to free the cash.")
    if autotrade:
        how = ("_**Auto-trading is ON for your paper account.** The agent placed the orders below "
               "itself — entries with their stop attached, stop raises, switches to trailing stops "
               "and exits. Fake money only; nothing here touches a live account._ " + rides)
    elif account:
        how = ("_Levels are computed from real price data by rules, not written by the model. "
               "Sizes are whole shares from your Alpaca equity (fractional shares can only hold "
               "day orders at Alpaca, so they can't keep a standing stop). To follow a buy: place "
               "it, then a **GTC stop order for all the shares** at the Initial stop. When the "
               "report says TRAILING, replace it with a GTC **trailing stop** at the percent "
               "shown._ " + rides)
    else:
        how = ("_Levels are computed from real price data by rules, not written by the model. "
               "To follow a trade: buy, then immediately place a **stop-loss order at the Initial "
               "stop**. When the report says TRAILING, switch to a **trailing stop** at the percent "
               "shown. Sizes are % of your total portfolio, capped by your profile's 10% "
               "per-position limit._ " + rides)
    parts = ["## Action Plan", "", how, ""]
    for n in notes or []:
        parts += [f"> {n}", ""]

    buys = [s for s in new_signals if s["action"] == "BUY"]
    waits = [s for s in new_signals if s["action"] == "WAIT"]
    passes = [s for s in new_signals if s["action"] in ("PASS", "ALREADY OPEN")]

    parts.append("### Buy now")
    if buys:
        parts.append(_table(
            ["Ticker", "Size", "Buy near", "Initial stop", "Then", "Room", "Setup", "Conv.", "Notes"],
            [[_held(s["ticker"], held), _size(s), _money(s["price"]), _money(s["stop"]), _trail(s),
              strat.HORIZON_LABELS[s["horizon"]], s["setup"].replace("_", " "), f"{s['conviction']}/5", _notes(s)]
             for s in buys]))
    else:
        parts.append("_No new buys today._")
    parts.append("")

    if waits:
        parts.append("### Limit buys — wait for a pullback")
        parts.append(_table(
            ["Ticker", "Size", "Buy at or below", "Now", "Initial stop", "Then", "Room", "Conv.", "Why wait"],
            [[_held(s["ticker"], held), _size(s), _money(s["buy_limit"]), _money(s["price"]),
              _money(s["stop"]), _trail(s), strat.HORIZON_LABELS[s["horizon"]],
              f"{s['conviction']}/5", _notes(s)]
             for s in waits]))
        parts.append("")

    if trades:
        parts.append("### Orders the agent placed (paper)")
        parts.append(_table(["Ticker", "Action", "Result", "Detail", "Why"],
                            [[f"**{t['symbol']}**", t["kind"].replace("_", " "), "done" if t["ok"] else "**FAILED**",
                              t["detail"], t.get("why", "")] for t in trades]))
        parts.append("")

    if account:
        sm = acct.summary(account)
        label = "PAPER account — test data, not real money" if sm["env"] == "paper" else "live account"
        parts.append(f"### Your Alpaca account ({label})")
        parts.append(f"Equity {_money(sm['equity'])} · cash {_money(sm['cash'])} · invested "
                     f"{_money(sm['invested'])} ({_pct(sm['invested_pct'], False)}) across "
                     f"{sm['positions']} positions · as of {sm['as_of']}")
        if paper_perf and paper_perf.get("return_pct") is not None:
            spy = paper_perf.get("spy_return_pct")
            parts.append(f"**Since auto-trading began ({paper_perf['since']}, {paper_perf['days']} days):** "
                         f"paper account {_pct(paper_perf['return_pct'])}"
                         + (f" vs S&P 500 {_pct(spy)}" if spy is not None else ""))
        parts.append("")
        rows = account_signals or []
        if rows:
            parts.append(_table(
                ["Ticker", "Status", "Shares", "Value", "Your stop", "Plan stop", "What to do"],
                [[f"**{r['ticker']}**", r["action"] if r["action"] in QUIET_ACCOUNT else f"**{r['action']}**",
                  "—" if r.get("qty") is None else f"{r['qty']:g}", _money(r.get("market_value")),
                  _money(r.get("stop_order")), _money(r.get("plan_stop")), r["detail"]] for r in rows]))
            parts.append("")

    updates = sorted([s for s in position_signals if s["action"] in UPDATE_ORDER],
                     key=lambda s: UPDATE_ORDER.index(s["action"]))
    if updates:
        parts.append("### Sell or adjust")
        parts.append(_table(
            ["Ticker", "Action", "What to do", "Return", "S&P same period"],
            [[_held(s["ticker"], held), f"**{s['action']}**", s["detail"],
              _pct(s.get("return_pct")), _pct(s.get("bench_return_pct"))] for s in updates]))
        parts.append("")

    holds = [s for s in position_signals if s["action"] in ("HOLD", "WAIT")]
    if holds:
        parts.append("### Holding / waiting")
        parts.append(_table(
            ["Ticker", "Status", "Entry", "Now", "Return", "S&P same period", "Stop", "Stop is"],
            [[_held(s["ticker"], held), "pending limit" if s["status"] == "pending" else "holding",
              _money(s.get("entry") or s.get("buy_limit")), _money(s.get("price")),
              _pct(s.get("return_pct")), _pct(s.get("bench_return_pct")),
              _money(s.get("stop")), _phase(s)] for s in holds]))
        parts.append("")

    if passes:
        parts.append("### Considered and passed")
        parts += [f"- **{s['ticker']}** — {s['detail']}" for s in passes]
        parts.append("")
    return "\n".join(parts)


def _row(label: str, a: dict) -> list:
    if not a.get("n"):
        return [label, 0, "—", "—", "—", "—", "—", "—"]
    return [label, a["n"], _pct(a.get("avg_return")), _pct(a.get("avg_bench")),
            _pts(a.get("avg_excess")), _pct(a.get("beat_rate"), False), _pct(a.get("win_rate"), False),
            a.get("avg_days", "—")]


def track_record(stats: dict, policy: dict, backfill: dict | None = None) -> str:
    c = stats["counts"]
    head = ["Group", "Trades", "Avg return", "Avg S&P", "vs S&P", "Beat S&P", "Profitable", "Avg days"]
    parts = [
        "## Track Record vs S&P 500",
        "",
        f"{c['recommendations']} recommendations tracked: {c['open']} open, {c['closed']} closed, "
        f"{c['pending']} waiting on limit buys, {c['unscorable']} unscorable (no price history)"
        + (f", {c['awaiting_backfill']} older picks still being backfilled" if c["awaiting_backfill"] else "")
        + ". Each one is scored as if it had been followed under the current exit rules (initial stop, "
          "trailing stop, two-week flat-trade exit) "
          "and compared with the S&P 500 (SPY) over the exact same dates. **These are the agent's past "
          "recommendations, not trades in your account.**",
        "",
        _table(head, [
            _row("All closed trades", stats["closed"]),
            _row("— this system's picks", stats["agent_closed"]),
            _row("— pre-rebuild picks, replayed under these rules", stats["legacy_closed"]),
            _row("Open positions (unrealized)", stats["open"]),
        ]),
        "",
    ]
    setups = {k: v for k, v in stats["by_setup_closed"].items() if v.get("n")}
    if setups:
        parts += ["**Closed trades by setup** — this is what the agent learns from:", "",
                  _table(head, [_row(k.replace("_", " "), v) for k, v in
                                sorted(setups.items(), key=lambda kv: -(kv[1].get("avg_excess") or -999))]),
                  ""]
    horizons = {k: v for k, v in stats.get("by_horizon_closed", {}).items() if v.get("n")}
    if horizons:
        parts += ["**Closed trades by how much room they got** (avg days held shows how long winners ran):", "",
                  _table(head, [_row(strat.HORIZON_LABELS.get(k, str(k)), v) for k, v in horizons.items()]),
                  ""]
    parts.append("**Learned rules in effect:**")
    parts += [f"- {r}" for r in policy["rules"]]
    if backfill and (backfill.get("backfilled") or backfill.get("unscorable")):
        parts += ["", f"_Backfilled {backfill['backfilled']} pre-rebuild picks from price history this run"
                      + (f" ({backfill['unscorable']} had no usable history)" if backfill.get("unscorable") else "")
                      + "._"]
    parts.append("")
    return "\n".join(parts)
