"""Per-ticker fundamentals from FMP for the second-pass analysis.

Keeps only decision-relevant fields so the model payload stays small.
Profiles, ratios and analyst targets change slowly, so they're cached for a
week and may be served stale when the daily quota is spent.
"""
from src.fetchers import fmp_client
from src.fetchers.fmp_client import TTL_WEEK, FMPQuotaExhausted


def _first(path: str, symbol: str) -> dict:
    rows = fmp_client.get_soft(path, {"symbol": symbol}, ttl=TTL_WEEK, allow_stale=True)
    return rows[0] if isinstance(rows, list) and rows else {}


def get_fundamentals(symbol: str) -> dict:
    profile = _first("profile", symbol)
    ratios = _first("ratios-ttm", symbol)
    target = _first("price-target-consensus", symbol)

    out = {
        "symbol": symbol,
        "companyName": profile.get("companyName"),
        "price": profile.get("price"),
        "marketCap": profile.get("marketCap"),
        "beta": profile.get("beta"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "lastDividend": profile.get("lastDividend"),
        "range52w": profile.get("range"),
        "isEtf": profile.get("isEtf"),
        "isFund": profile.get("isFund"),
        "isActivelyTrading": profile.get("isActivelyTrading"),
        "peTTM": ratios.get("priceToEarningsRatioTTM"),
        "priceToSalesTTM": ratios.get("priceToSalesRatioTTM"),
        "grossMarginTTM": ratios.get("grossProfitMarginTTM"),
        "operatingMarginTTM": ratios.get("operatingProfitMarginTTM"),
        "netMarginTTM": ratios.get("netProfitMarginTTM"),
        "debtToEquityTTM": ratios.get("debtToEquityRatioTTM"),
        "dividendYieldTTM": ratios.get("dividendYieldTTM"),
        "analystTargetConsensus": target.get("targetConsensus"),
        "analystTargetHigh": target.get("targetHigh"),
        "analystTargetLow": target.get("targetLow"),
    }
    # Drop empty fields to keep the payload tight, but keep falsy-but-real
    # values: `isEtf: False` and a genuine 0% yield are information.
    return {k: v for k, v in out.items() if k == "symbol" or (v is not None and v != "")}


def get_fundamentals_bulk(symbols: list[str], limit: int = 15) -> list[dict]:
    seen = set()
    out = []
    for s in symbols:
        s = (s or "").strip().upper()
        if not s or s in seen or s.startswith("^"):
            continue
        seen.add(s)
        try:
            out.append(get_fundamentals(s))
        except FMPQuotaExhausted:
            print(f"  Warning: FMP budget ran out after {len(out)} of the candidates' fundamentals.")
            break
        if len(out) >= limit:
            break
    return out
