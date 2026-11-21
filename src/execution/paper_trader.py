"""Places the agent's trades in an Alpaca PAPER account — fake money only.

Opt-in: both ALPACA_ENV=paper and ALPACA_AUTOTRADE=paper in .env. The server
address below is Alpaca's paper server and nothing else; this module has no
way to reach a live account. The read-only account reader
(fetchers/broker_alpaca.py) is separate and unchanged.

What it does each run, in this order:
  1. exits   — the plan exited a position you still hold: cancel its open
               orders, then sell the shares at market.
  2. cancels — limit buys the plan cancelled, and buy-now entries that still
               haven't filled after STALE_ENTRY_DAYS.
  3. stops   — every held position the plan manages gets stop orders covering
               all its whole shares, at the plan's current stop (raised when
               the plan raises it). Once the plan is trailing, the fixed stop
               is swapped for an Alpaca trailing_stop order, which follows the
               price up on Alpaca's side even when this computer is off.
  4. entries — new BUY / WAIT ideas, as one OTO order (limit buy + stop) for
               all the shares. No take-profit: winners ride the trailing stop.

Guardrails: never sells more than is held, never shorts, never buys a
position bigger than the per-position cap (plus 5% for price drift).
Decisions (plan_actions) are separate from sending orders (execute), so the
decisions can be tested without touching Alpaca.
"""
import json
import math
import time
from datetime import date, datetime, timedelta, timezone

import requests

from src import config
from src.analysis import account as acct
from src.fetchers import broker_alpaca

PAPER_BASE = "https://paper-api.alpaca.markets"
ENTRY_SLIPPAGE = 0.01      # buy-now entries: limit 1% above the plan's price
STALE_ENTRY_DAYS = 3       # unfilled buy-now entries are cancelled after this
PREFIX = "ag-"             # client_order_id prefix marking the agent's orders
PERF_PATH = config.REPORTS_DIR / "paper_performance.json"


class PaperTradeError(RuntimeError):
    """Alpaca refused or couldn't be reached. Message never includes keys."""


def enabled() -> bool:
    return (broker_alpaca.is_configured() and config.ALPACA_ENV == "paper"
            and config.ALPACA_AUTOTRADE == "paper")


def disabled_reason() -> str | None:
    """Why auto-trading is off when someone tried to turn it on."""
    if not config.ALPACA_AUTOTRADE:
        return None
    if config.ALPACA_AUTOTRADE != "paper":
        return f"ALPACA_AUTOTRADE='{config.ALPACA_AUTOTRADE}' — only 'paper' is supported"
    if config.ALPACA_ENV != "paper":
        return "auto-trading only runs on the paper account (ALPACA_ENV is not 'paper')"
    if not broker_alpaca.is_configured():
        return "Alpaca keys are missing"
    return None


# --------------------------------------------------------------------------
# HTTP (paper server only)
# --------------------------------------------------------------------------

def _req(method: str, path: str, body: dict | None = None):
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_API_SECRET_KEY,
    }
    try:
        resp = requests.request(method, PAPER_BASE + path, headers=headers, json=body, timeout=15)
    except requests.RequestException as e:
        raise PaperTradeError(f"{method} {path} failed: {type(e).__name__}") from None
    if resp.status_code >= 400:
        msg = ""
        try:
            msg = str((resp.json() or {}).get("message", ""))[:200]
        except ValueError:
            pass
        if resp.status_code == 403:
            msg = msg or "forbidden — if your paper API key is read-only, generate a trading key"
        raise PaperTradeError(f"Alpaca refused {method} {path} (HTTP {resp.status_code})" + (f": {msg}" if msg else ""))
    if resp.status_code == 204 or not getattr(resp, "content", b"x"):
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _wait_cancelled(order_id: str, tries: int = 10, delay: float = 0.5) -> None:
    """Cancels are asynchronous; shares stay reserved until it completes."""
    for _ in range(tries):
        o = _req("GET", f"/v2/orders/{order_id}") or {}
        if o.get("status") in ("canceled", "filled", "expired", "rejected", "replaced"):
            return
        time.sleep(delay)
    raise PaperTradeError(f"order {order_id} didn't finish cancelling in time")


def _cancel(order_id: str) -> None:
    _req("DELETE", f"/v2/orders/{order_id}")
    _wait_cancelled(order_id)


def _px(v: float) -> str:
    return f"{v:.2f}"


def _qty(q: float) -> str:
    return f"{q:g}"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


# --------------------------------------------------------------------------
# planning (pure)
# --------------------------------------------------------------------------

def _age_days(created_at: str | None, now: datetime) -> float:
    if not created_at:
        return 0.0
    try:
        t = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (now - t).total_seconds() / 86400


def plan_actions(snapshot: dict, book: dict, new_signals: list[dict], position_signals: list[dict],
                 account_signals: list[dict], s: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    held = acct.held(snapshot)
    orders = [o for o in snapshot.get("orders", []) if o.get("status") not in ("canceled", "filled", "expired")]
    equity = snapshot.get("equity") or 0
    actions: list[dict] = []
    touched: set[str] = set()

    def open_orders(sym, side=None):
        return [o for o in orders if o["symbol"] == sym and (side is None or o["side"] == side)]

    def parents(os_):
        return [o["id"] for o in os_ if not o.get("parent_id")]

    # 1. exits
    sells = ({x["ticker"] for x in position_signals if x["action"] == "SELL"}
             | {x["ticker"] for x in account_signals if x["action"] == "SELL"})
    for sym in sorted(sells):
        touched.add(sym)
        pos = held.get(sym)
        if pos:
            actions.append({"kind": "exit", "symbol": sym, "qty": pos["qty"],
                            "cancel_ids": parents(open_orders(sym)), "why": "plan exited the position"})
        elif open_orders(sym, "buy"):
            actions.append({"kind": "cancel", "symbol": sym, "ids": parents(open_orders(sym, "buy")),
                            "why": "plan exited before the entry filled"})

    # 2. cancels
    for x in position_signals:
        sym = x["ticker"]
        if x["action"] == "CANCEL" and sym not in touched and open_orders(sym, "buy"):
            touched.add(sym)
            actions.append({"kind": "cancel", "symbol": sym, "ids": parents(open_orders(sym, "buy")),
                            "why": "plan cancelled the limit buy"})
    for o in orders:
        sym = o["symbol"]
        if (o["side"] == "buy" and not o.get("parent_id") and o["client_order_id"].startswith(PREFIX)
                and sym not in touched and sym not in held):
            p = acct.latest_agent_position(book, sym)
            # Buy-now entries are expected to fill at the next open; WAIT
            # entries are allowed to sit until the plan cancels them.
            if p and p["status"] == "open" and _age_days(o.get("created_at"), now) >= STALE_ENTRY_DAYS:
                touched.add(sym)
                actions.append({"kind": "cancel", "symbol": sym, "ids": parents(open_orders(sym, "buy")),
                                "why": f"entry unfilled after {STALE_ENTRY_DAYS} days"})

    # 3. stops on held positions the plan manages
    for sym, pos in held.items():
        if sym in touched:
            continue
        p = acct.latest_agent_position(book, sym)
        if not p or p["status"] not in ("open", "pending"):
            continue
        plan_stop = p.get("stop") or (p.get("plan") or {}).get("stop")
        trail_pct = (p.get("plan") or {}).get("trail_pct")
        if not plan_stop:
            continue
        stops = [o for o in open_orders(sym, "sell") if o["type"] in acct.STOP_TYPES
                 and (o.get("stop_price") or o.get("trail_percent"))]
        trailing = [o for o in stops if o["type"] == "trailing_stop"]
        fixed = [o for o in stops if o["type"] != "trailing_stop"]

        if (p.get("trailing") and fixed and not trailing
                and acct.can_switch_to_trailing(pos.get("current_price"), trail_pct, plan_stop)):
            actions.append({"kind": "to_trailing", "symbol": sym, "ids": [o["id"] for o in fixed],
                            "qty": math.floor(pos["qty"] + 1e-9), "trail_pct": trail_pct,
                            "why": f"trade is working — {trail_pct:g}% trailing stop from here on"})
            continue
        for o in fixed:
            if o.get("stop_price") and o["stop_price"] < plan_stop * (1 - acct.PRICE_TOLERANCE):
                kind = "raise_stop" if o.get("order_class") == "bracket" else "replace_stop"
                actions.append({"kind": kind, "symbol": sym, "id": o["id"], "qty": o.get("qty"),
                                "stop": plan_stop, "why": f"plan stop is now ${plan_stop:,.2f} (was ${o['stop_price']:,.2f})"})
        for o in trailing:
            if trail_pct and (o.get("trail_percent") or 0) > trail_pct + 0.5:
                actions.append({"kind": "tighten_trail", "symbol": sym, "id": o["id"], "trail_pct": trail_pct,
                                "why": f"trail {o['trail_percent']:g}% -> {trail_pct:g}%"})
        covered = sum(o.get("qty") or 0 for o in stops)
        missing = math.floor(pos["qty"] - covered + 1e-9)
        if missing >= 1:
            actions.append({"kind": "add_stop", "symbol": sym, "qty": missing, "stop": plan_stop,
                            "why": f"{missing:g} share(s) had no stop"})

    # 4. entries
    cap = equity * s["max_position_pct"] / 100 * 1.05
    for x in new_signals:
        if x["action"] not in ("BUY", "WAIT") or not x.get("shares"):
            continue
        sym = x["ticker"]
        if sym in held or open_orders(sym, "buy"):
            continue
        limit = x["buy_limit"] if x["action"] == "WAIT" else round(x["price"] * (1 + ENTRY_SLIPPAGE), 2)
        if x["shares"] * limit > cap:
            actions.append({"kind": "skip", "symbol": sym,
                            "why": f"{x['shares']} sh at ${limit:,.2f} exceeds the position cap — not placed"})
            continue
        actions.append({"kind": "enter", "symbol": sym, "shares": x["shares"], "limit": limit,
                        "stop": x["stop"], "pid": x["position_id"],
                        "why": "new idea" if x["action"] == "BUY" else "limit buy (wait for pullback)"})
    return actions


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _enter(a: dict) -> str:
    """One OTO order: the limit buy, with its stop attached for every share.
    No take-profit — winners are held until the trailing stop takes them out."""
    _req("POST", "/v2/orders", {"symbol": a["symbol"], "side": "buy", "type": "limit",
                                "limit_price": _px(a["limit"]), "qty": _qty(a["shares"]),
                                "time_in_force": "gtc", "order_class": "oto",
                                "stop_loss": {"stop_price": _px(a["stop"])},
                                "client_order_id": f"{PREFIX}{a['pid']}-o"})
    return f"buy {a['shares']:g} at up to ${a['limit']:,.2f} with stop ${a['stop']:,.2f} attached"


def _stop_order(sym: str, qty: float, stop: float) -> dict:
    return {"symbol": sym, "side": "sell", "type": "stop", "qty": _qty(qty), "stop_price": _px(stop),
            "time_in_force": "gtc", "client_order_id": f"{PREFIX}stop-{sym}-{_stamp()}"}


def execute(actions: list[dict]) -> list[dict]:
    """Sends the planned orders. One failure doesn't stop the rest; every
    action comes back with ok/failed and what happened."""
    results = []
    for a in actions:
        r = {"symbol": a["symbol"], "kind": a["kind"], "why": a.get("why", "")}
        try:
            if a["kind"] == "skip":
                r.update(ok=False, detail=a["why"])
            elif a["kind"] == "enter":
                r.update(ok=True, detail=_enter(a))
            elif a["kind"] == "exit":
                for oid in a["cancel_ids"]:
                    _cancel(oid)
                _req("POST", "/v2/orders", {"symbol": a["symbol"], "side": "sell", "type": "market",
                                            "qty": _qty(a["qty"]), "time_in_force": "day",
                                            "client_order_id": f"{PREFIX}exit-{a['symbol']}-{_stamp()}"})
                r.update(ok=True, detail=f"cancelled {len(a['cancel_ids'])} order(s), market sell {a['qty']:g}")
            elif a["kind"] == "cancel":
                for oid in a["ids"]:
                    _cancel(oid)
                r.update(ok=True, detail=f"cancelled {len(a['ids'])} order(s)")
            elif a["kind"] == "raise_stop":
                _req("PATCH", f"/v2/orders/{a['id']}", {"stop_price": _px(a["stop"])})
                r.update(ok=True, detail=f"stop raised to ${a['stop']:,.2f}")
            elif a["kind"] == "replace_stop":
                _cancel(a["id"])
                _req("POST", "/v2/orders", _stop_order(a["symbol"], a["qty"], a["stop"]))
                r.update(ok=True, detail=f"stop replaced at ${a['stop']:,.2f} for {a['qty']:g} sh")
            elif a["kind"] == "to_trailing":
                for oid in a["ids"]:
                    _cancel(oid)
                _req("POST", "/v2/orders", {"symbol": a["symbol"], "side": "sell", "type": "trailing_stop",
                                            "qty": _qty(a["qty"]), "trail_percent": f"{a['trail_pct']:g}",
                                            "time_in_force": "gtc",
                                            "client_order_id": f"{PREFIX}trail-{a['symbol']}-{_stamp()}"})
                r.update(ok=True, detail=f"switched to a {a['trail_pct']:g}% trailing stop for {a['qty']:g} sh")
            elif a["kind"] == "tighten_trail":
                _req("PATCH", f"/v2/orders/{a['id']}", {"trail": f"{a['trail_pct']:g}"})
                r.update(ok=True, detail=f"trailing stop tightened to {a['trail_pct']:g}%")
            elif a["kind"] == "add_stop":
                _req("POST", "/v2/orders", _stop_order(a["symbol"], a["qty"], a["stop"]))
                r.update(ok=True, detail=f"GTC stop ${a['stop']:,.2f} for {a['qty']:g} sh")
        except PaperTradeError as e:
            r.update(ok=False, detail=str(e))
        results.append(r)
    return results


# --------------------------------------------------------------------------
# how is it doing?
# --------------------------------------------------------------------------

def record_performance(snapshot: dict, spy_price: float | None, today: str) -> dict:
    """Paper equity vs the S&P 500 since auto-trading started. Deposits
    aren't possible on paper, so equity change is performance."""
    try:
        perf = json.loads(PERF_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        perf = {}
    eq = snapshot.get("equity")
    if not perf.get("start") and eq:
        perf["start"] = {"date": today, "equity": eq, "spy": spy_price}
    hist = [h for h in perf.get("history", []) if h["date"] != today]
    hist.append({"date": today, "equity": eq, "spy": spy_price})
    perf["history"] = sorted(hist, key=lambda h: h["date"])
    PERF_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERF_PATH.write_text(json.dumps(perf, indent=2), encoding="utf-8")

    st = perf["start"]
    out = {"since": st["date"], "start_equity": st["equity"], "equity": eq,
           "days": (date.fromisoformat(today) - date.fromisoformat(st["date"])).days}
    out["return_pct"] = round(100 * (eq / st["equity"] - 1), 2) if eq and st.get("equity") else None
    out["spy_return_pct"] = (round(100 * (spy_price / st["spy"] - 1), 2)
                             if spy_price and st.get("spy") else None)
    return out
