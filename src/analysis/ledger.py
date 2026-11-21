"""Picks ledger: records every recommendation and scores it over time.

Stored at reports/picks_ledger.json as a flat list of pick records:
    {date, ticker, category, horizon, thesis, price_at_pick}
"""
import json

from src import config
from src.fetchers.market_data import get_quote

LEDGER_PATH = config.REPORTS_DIR / "picks_ledger.json"

MAX_SCORECARD_TICKERS = 20


def load_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def record_picks(picks: list[dict], run_date: str) -> None:
    """Appends today's picks (with entry price) to the ledger. A ticker
    already recorded with a usable entry price is not re-recorded — the
    original entry price stays the benchmark.

    If a ticker was recorded earlier but its price lookup failed (the quote
    endpoint returns nothing for some ETFs and micro-caps), the record is
    repaired on a later run instead of staying permanently unscoreable. The
    entry date moves to the day the price was actually captured — we can't
    claim a return measured from a date we never had a price for — and the
    original recommendation date is kept in `first_recommended`.
    """
    ledger = load_ledger()
    by_ticker = {p["ticker"]: p for p in ledger}
    for pick in picks:
        ticker = (pick.get("ticker") or "").strip().upper()
        if not ticker:
            continue

        record = by_ticker.get(ticker)
        if record is not None and record.get("price_at_pick") is not None:
            continue  # already tracked with a usable entry price

        price = get_quote(ticker).get("price")

        if record is not None:
            if price is None:
                continue  # still no quote data; leave the record as it stands
            record["first_recommended"] = record.get("first_recommended") or record.get("date")
            record["date"] = run_date
            record["price_at_pick"] = price
            continue

        new_record = {
            "date": run_date,
            "ticker": ticker,
            "category": pick.get("category"),
            "horizon": pick.get("horizon"),
            "thesis": pick.get("thesis"),
            "price_at_pick": price,
        }
        ledger.append(new_record)
        by_ticker[ticker] = new_record

    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def get_scorecard(run_date: str) -> list[dict]:
    """Current performance of past picks (excluding ones made today),
    most recent first, capped to bound API usage.

    Picks with no entry price are skipped: they can't be scored, and letting
    them occupy scorecard slots meant half the table rendered as empty rows.
    """
    ledger = [
        p for p in load_ledger()
        if p.get("date") != run_date and p.get("price_at_pick") is not None
    ]
    ledger.sort(key=lambda p: p.get("date", ""), reverse=True)

    rows = []
    for pick in ledger[:MAX_SCORECARD_TICKERS]:
        entry = pick.get("price_at_pick")
        current = get_quote(pick["ticker"]).get("price")
        change_pct = None
        if entry and current:
            change_pct = round((current - entry) / entry * 100, 2)
        rows.append(
            {
                "ticker": pick["ticker"],
                "picked_on": pick.get("date"),
                "category": pick.get("category"),
                "horizon": pick.get("horizon"),
                "thesis": pick.get("thesis"),
                "price_at_pick": entry,
                "price_now": current,
                "change_pct_since_pick": change_pct,
            }
        )
    return rows
