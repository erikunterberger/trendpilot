"""Market-wide data via Financial Modeling Prep (FMP).

Docs: https://site.financialmodelingprep.com/developer/docs

Uses the /stable/ API — FMP retired the old /api/v3/ paths for non-legacy
accounts (cutoff Aug 31, 2025). All requests go through fmp_client, which
caches responses and enforces the daily request budget.
"""
import re
import time
from datetime import date, timedelta

from src.fetchers import fmp_client
from src.fetchers.fmp_client import TTL_DAILY, TTL_LIVE, TTL_WEEK, FMPQuotaExhausted

# Movers below these thresholds are micro-cap noise or data errors, not
# tradeable signal.
MIN_PRICE = 1.0
MAX_ABS_CHANGE_PCT = 300.0
MIN_MARKET_CAP = 250_000_000

# New recommendations face a slightly stricter floor: sub-$3 stocks carry
# wide spreads that eat a small account's returns.
MIN_PICK_PRICE = 3.0

BENCHMARK_SYMBOLS = ("SPY", "^GSPC")
HISTORY_CALENDAR_DAYS = 400   # covers a 200-day average and the 52-week range
HISTORY_RETRY_SECONDS = 7 * 24 * 3600

MACRO_SYMBOLS = {
    "^GSPC": "S&P 500",
    "^DJI": "Dow Jones Industrial Average",
    "^IXIC": "Nasdaq Composite",
    "^RUT": "Russell 2000",
    "^VIX": "CBOE Volatility Index",
    "^TNX": "10-Year Treasury Yield",
}

# Leveraged and inverse funds. The investor profile excludes leveraged ETFs,
# and inverse funds carry the same daily-reset decay. Matched on fund names
# like "Direxion Daily Semiconductor Bull 3X" or "ProShares Short S&P500".
_LEVERAGED_RE = re.compile(
    r"(?<![\w.])-?[1-9](?:\.\d)?x\b"
    r"|\bultra(?:pro|short)?\b|\bleverag|\binverse\b|\bdaily target\b"
    r"|\bbull\b|\bbear\b"
    r"|\bshort\b(?!\s*-?\s*(?:term|duration|maturity|dated))",
    re.IGNORECASE,
)


def is_leveraged_or_inverse(name: str | None) -> bool:
    return bool(name and _LEVERAGED_RE.search(name))


def get_quote(symbol: str, critical: bool = False) -> dict:
    """Latest quote, or {} if FMP has none for this symbol.
    Raises FMPQuotaExhausted rather than returning {} when out of quota."""
    data = fmp_client.get_soft("quote", {"symbol": symbol}, ttl=TTL_LIVE, critical=critical)
    return data[0] if isinstance(data, list) and data else {}


def get_profile(symbol: str) -> dict:
    data = fmp_client.get_soft("profile", {"symbol": symbol}, ttl=TTL_WEEK, allow_stale=True)
    return data[0] if isinstance(data, list) and data else {}


def check_universe(profile: dict, quote_price: float | None = None) -> tuple[bool, str]:
    """Whether a ticker is eligible to be recommended at all.

    Applied to every candidate regardless of where it came from. It used to
    run only on the movers lists, and exempted every fund, so a $1M-market-
    cap ETF and nine leveraged ETFs reached the picks list.
    """
    if not profile:
        return False, "no company data (likely not covered by the FMP plan)"
    name = profile.get("companyName") or ""
    if is_leveraged_or_inverse(name):
        return False, f"leveraged/inverse fund ({name}) — excluded by your investor profile"
    price = quote_price or profile.get("price") or 0
    if price < MIN_PICK_PRICE:
        return False, f"price ${price:.2f} below the ${MIN_PICK_PRICE:.0f} floor"
    mcap = profile.get("marketCap") or 0
    if mcap < MIN_MARKET_CAP:
        kind = "fund size" if (profile.get("isEtf") or profile.get("isFund")) else "market cap"
        return False, f"{kind} ${mcap / 1e6:,.0f}M below the ${MIN_MARKET_CAP / 1e6:,.0f}M floor"
    if profile.get("isActivelyTrading") is False:
        return False, "not actively trading"
    return True, ""


def _filter_movers(rows: list[dict], enrich_limit: int = 12) -> list[dict]:
    """Drops data errors and untradeable micro-caps from a movers list.

    First pass: cheap heuristics (price floor, change-% sanity cap, rights/
    warrants/units by name). Second pass: profiles for the survivors (cached
    for a week, so mostly free after the first run) and the universe check.
    """
    cheap = []
    for r in rows:
        price = r.get("price") or 0
        change = abs(r.get("changesPercentage") or 0)
        name = (r.get("name") or "").lower()
        if price < MIN_PRICE or change > MAX_ABS_CHANGE_PCT:
            continue
        if any(w in name for w in ("warrant", " right", " rights", " unit")):
            continue
        cheap.append(r)

    kept = []
    for r in cheap[:enrich_limit]:
        try:
            profile = get_profile(r.get("symbol", ""))
        except FMPQuotaExhausted:
            break  # keep what we have; screening is not worth the reserve
        ok, _ = check_universe(profile, r.get("price"))
        if ok:
            r = dict(r)
            r["marketCap"] = profile.get("marketCap")
            r["sector"] = profile.get("sector")
            kept.append(r)
    return kept


def _movers(path: str, limit: int) -> list[dict]:
    data = fmp_client.get(path, ttl=TTL_LIVE)
    return _filter_movers((data or [])[:limit])


def get_gainers(limit: int = 25) -> list[dict]:
    return _movers("biggest-gainers", limit)


def get_losers(limit: int = 25) -> list[dict]:
    return _movers("biggest-losers", limit)


def get_most_active(limit: int = 25) -> list[dict]:
    return _movers("most-actives", limit)


def get_sector_performance(max_days_back: int = 5) -> list[dict]:
    """Sector performance needs an explicit date and is empty on non-trading
    days (weekends/holidays), so walk backward to the last trading day."""
    for days_back in range(max_days_back):
        day = (date.today() - timedelta(days=days_back)).isoformat()
        data = fmp_client.get_soft("sector-performance-snapshot", {"date": day}, ttl=TTL_LIVE)
        if data:
            return data
    return []


def get_macro_snapshot() -> list[dict]:
    """Hard index/vol/yield levels so the model never has to guess the tape."""
    rows = []
    for symbol, label in MACRO_SYMBOLS.items():
        q = get_quote(symbol)
        if q:
            rows.append(
                {
                    "symbol": symbol,
                    "name": label,
                    "price": q.get("price"),
                    "changePercentage": q.get("changePercentage"),
                    "yearHigh": q.get("yearHigh"),
                    "yearLow": q.get("yearLow"),
                }
            )
    return rows


def get_earnings_calendar(days_ahead: int = 14, min_revenue: float = 1e9) -> list[dict]:
    """Upcoming earnings for real companies (revenue >= min_revenue) so the
    analysis can cite concrete, dated catalysts, and the buy rules can see
    when a new position would run straight into a report."""
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days_ahead)).isoformat()
    rows = fmp_client.get_soft("earnings-calendar", {"from": start, "to": end}, ttl=TTL_DAILY)
    major = [
        {
            "symbol": r.get("symbol"),
            "date": r.get("date"),
            "epsEstimated": r.get("epsEstimated"),
            "revenueEstimated": r.get("revenueEstimated"),
        }
        for r in rows
        if (r.get("revenueEstimated") or 0) >= min_revenue
    ]
    major.sort(key=lambda r: (r["date"], -(r["revenueEstimated"] or 0)))
    return major[:60]


def _normalize_bars(data) -> list[dict]:
    """Accepts FMP's 'full' (OHLC) or 'light' (close-only) EOD formats, plus
    the legacy {"historical": [...]} wrapper. Returns oldest-first."""
    if isinstance(data, dict):
        data = data.get("historical") or []
    bars = []
    for r in data or []:
        close = r.get("close", r.get("price"))
        if not r.get("date") or close is None:
            continue
        bars.append({
            "date": str(r["date"])[:10],
            "open": r.get("open"),
            "high": r.get("high"),
            "low": r.get("low"),
            "close": float(close),
            "volume": r.get("volume"),
        })
    bars.sort(key=lambda b: b["date"])
    return bars


def get_history(symbol: str, critical: bool = False,
                calendar_days: int = HISTORY_CALENDAR_DAYS) -> list[dict]:
    """Daily bars for roughly the last 13 months, oldest first. [] if FMP has
    no history for the symbol or the plan doesn't include it.

    Prefers the OHLC endpoint (true ranges, intraday stop checks) and falls
    back to the close-only one. If OHLC is refused but close-only works, the
    plan probably lacks OHLC, so close-only is used directly for a week
    before OHLC is probed again. A symbol with no data on either endpoint
    changes nothing: that's about the symbol, not the plan."""
    params = {
        "symbol": symbol,
        "from": (date.today() - timedelta(days=calendar_days)).isoformat(),
        "to": date.today().isoformat(),
    }
    cap = fmp_client.get_capability("history_endpoint") or {}
    light_only = (cap.get("value") == "light"
                  and time.time() - cap.get("checked", 0) < HISTORY_RETRY_SECONDS)
    order = ["light"] if light_only else ["full", "light"]

    for kind in order:
        try:
            data = fmp_client.get(f"historical-price-eod/{kind}", params,
                                  ttl=TTL_DAILY, critical=critical)
        except FMPQuotaExhausted:
            raise
        except fmp_client.FMPUnavailable as e:
            if e.symbol_restricted:
                return []  # the symbol isn't covered; the other endpoint won't help
            continue
        except fmp_client.FMPError:
            continue
        bars = _normalize_bars(data)
        if not bars:
            return []  # endpoint works, symbol has no history
        if kind == "light" and "full" in order:
            fmp_client.set_capability("history_endpoint", {"value": "light", "checked": time.time()})
        elif kind == "full" and cap.get("value") != "full":
            fmp_client.set_capability("history_endpoint", {"value": "full", "checked": time.time()})
        return bars
    return []


def get_benchmark_history() -> tuple[str | None, list[dict]]:
    """S&P 500 benchmark bars: SPY if available, else the index itself."""
    for sym in BENCHMARK_SYMBOLS:
        bars = get_history(sym, critical=True)
        if bars:
            return sym, bars
    return None, []


def get_market_snapshot() -> dict:
    """One-shot pull of the day's market picture."""
    return {
        "macro": get_macro_snapshot(),
        "gainers": get_gainers(),
        "losers": get_losers(),
        "most_active": get_most_active(),
        "sector_performance": get_sector_performance(),
        "earnings_calendar": get_earnings_calendar(),
    }
