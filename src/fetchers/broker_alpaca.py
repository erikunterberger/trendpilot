"""Read-only view of an Alpaca brokerage account.

Docs: https://docs.alpaca.markets/us/docs

This module can only *read*: the account, open positions and open orders.
It contains no code that places, changes or cancels orders, and every
request is an HTTP GET. Create a read-only API key in the Alpaca dashboard
if your account offers that option, so the key itself can't trade either.

Nothing fetched here is sent to the language model; it's used locally for
sizing, allocation limits, order checks and the dashboard.
"""
from datetime import datetime

import requests

from src import config

BASE_URLS = {
    "paper": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}


class AlpacaError(RuntimeError):
    """Alpaca couldn't be reached or refused the request."""


def is_configured() -> bool:
    return bool(config.ALPACA_API_KEY_ID and config.ALPACA_API_SECRET_KEY)


def env() -> str:
    return config.ALPACA_ENV if config.ALPACA_ENV in BASE_URLS else "paper"


def _get(path: str, params: dict | None = None):
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_API_SECRET_KEY,
    }
    try:
        resp = requests.get(f"{BASE_URLS[env()]}{path}", headers=headers, params=params or {}, timeout=15)
    except requests.RequestException as e:
        raise AlpacaError(f"Alpaca request to '{path}' failed: {type(e).__name__}") from None
    if resp.status_code in (401, 403):
        raise AlpacaError(
            f"Alpaca rejected the API key (HTTP {resp.status_code}). Paper and live accounts have "
            f"separate keys — check that ALPACA_ENV ('{env()}') matches the keys in .env."
        )
    if resp.status_code == 429:
        raise AlpacaError("Alpaca rate limit hit (HTTP 429). Try again in a minute.")
    if resp.status_code >= 400:
        raise AlpacaError(f"Alpaca '{path}' returned HTTP {resp.status_code}.")
    try:
        return resp.json()
    except ValueError:
        raise AlpacaError(f"Alpaca '{path}' returned a non-JSON body.") from None


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _flatten_orders(orders: list[dict]) -> list[dict]:
    """Bracket and OCO groups come back nested (nested=true); each leg is a
    real order that matters for protection, so flatten them."""
    out = []
    for o in orders or []:
        out.append(o)
        for leg in o.get("legs") or []:
            out.append({**leg, "parent_id": o.get("id")})
    return out


def _order(o: dict) -> dict:
    trail_pct, trail_price, hwm = _f(o.get("trail_percent")), _f(o.get("trail_price")), _f(o.get("hwm"))
    effective_stop = _f(o.get("stop_price"))
    if o.get("type") == "trailing_stop" and hwm:
        if trail_pct is not None:
            effective_stop = hwm * (1 - trail_pct / 100)
        elif trail_price is not None:
            effective_stop = hwm - trail_price
    return {
        "id": o.get("id"),
        "client_order_id": o.get("client_order_id") or "",
        "parent_id": o.get("parent_id"),
        "created_at": o.get("created_at"),
        "symbol": (o.get("symbol") or "").upper(),
        "side": o.get("side"),
        "type": o.get("type") or o.get("order_type"),
        "order_class": o.get("order_class") or "",
        "time_in_force": o.get("time_in_force"),
        "qty": _f(o.get("qty")),
        "notional": _f(o.get("notional")),
        "limit_price": _f(o.get("limit_price")),
        "stop_price": effective_stop,
        "trail_percent": trail_pct,
        "status": o.get("status"),
    }


def get_snapshot() -> dict:
    """Account, positions and open orders in one call set. Raises
    AlpacaError if anything fails, so callers never act on a partial view."""
    acct = _get("/v2/account")
    positions = _get("/v2/positions")
    orders = _get("/v2/orders", {"status": "open", "nested": "true", "limit": 500})
    return {
        "env": env(),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": acct.get("status"),
        "equity": _f(acct.get("equity")),
        "last_equity": _f(acct.get("last_equity")),
        "cash": _f(acct.get("cash")),
        "buying_power": _f(acct.get("buying_power")),
        "long_market_value": _f(acct.get("long_market_value")),
        # 1 = can only spend deposited cash; 2 or 4 = borrowing (margin) enabled.
        "multiplier": _f(acct.get("multiplier")),
        "shorting_enabled": acct.get("shorting_enabled"),
        "positions": [
            {
                "symbol": (p.get("symbol") or "").upper(),
                "qty": _f(p.get("qty")),
                "side": p.get("side"),
                "asset_class": p.get("asset_class"),
                "avg_entry_price": _f(p.get("avg_entry_price")),
                "current_price": _f(p.get("current_price")),
                "market_value": _f(p.get("market_value")),
                "cost_basis": _f(p.get("cost_basis")),
                "unrealized_plpc": _f(p.get("unrealized_plpc")),
            }
            for p in positions or []
        ],
        "orders": [_order(o) for o in _flatten_orders(orders)],
    }


if __name__ == "__main__":
    # Connection check: python -m src.fetchers.broker_alpaca
    # Prints balances and counts only — never the keys.
    if not is_configured():
        raise SystemExit("Alpaca not configured: set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env.")
    try:
        snap = get_snapshot()
    except AlpacaError as e:
        raise SystemExit(f"Alpaca check failed: {e}")
    print(f"Connected to Alpaca ({snap['env']}, read-only). Account status: {snap['status']}")
    from src.analysis.account import margin_warning
    warning = margin_warning(snap)
    print(f"Margin: {'OFF — locked to 1x, cash only' if not warning else 'WARNING — ' + warning}")
    print(f"Equity ${snap['equity'] or 0:,.2f} | cash ${snap['cash'] or 0:,.2f} | "
          f"{len(snap['positions'])} positions | {len(snap['orders'])} open orders")
    for p in snap["positions"]:
        print(f"  {p['symbol']:<6} {p['qty']:g} sh @ ${p['avg_entry_price'] or 0:,.2f}, now ${p['current_price'] or 0:,.2f}")
