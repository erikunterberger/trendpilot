"""Price-based indicators computed from real end-of-day history.

Everything here is a pure function of a list of daily bars, so it's
deterministic and testable. Bars are dicts sorted oldest-first:
    {"date": "YYYY-MM-DD", "close": float, "high": float|None,
     "low": float|None, "open": float|None, "volume": float|None}
"""
import math

TRADING_DAYS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _atr_pct(bars: list[dict], n: int = 14) -> float | None:
    """Average true range over the last n bars, as a fraction of the last
    close. Falls back to mean absolute close-to-close change when the
    history has no highs/lows."""
    if len(bars) < n + 1:
        return None
    window = bars[-(n + 1):]
    ranges = []
    for prev, cur in zip(window[:-1], window[1:]):
        hi, lo, pc = cur.get("high"), cur.get("low"), prev["close"]
        if hi is not None and lo is not None:
            ranges.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        else:
            ranges.append(abs(cur["close"] - pc))
    last = bars[-1]["close"]
    if not last:
        return None
    return (sum(ranges) / len(ranges)) / last


def _ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or not closes[-n - 1]:
        return None
    return closes[-1] / closes[-n - 1] - 1


def _round(v, digits=4):
    return None if v is None else round(v, digits)


def compute(bars: list[dict]) -> dict:
    """Indicator snapshot as of the last bar. Returns {} with no data."""
    bars = [b for b in bars if b.get("close")]
    if not bars:
        return {}
    closes = [b["close"] for b in bars]
    last = closes[-1]

    sma20, sma50, sma200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)
    year = closes[-TRADING_DAYS["12m"]:]
    high_52w, low_52w = max(year), min(year)

    daily = [b / a - 1 for a, b in zip(closes[:-1], closes[1:]) if a]
    vol20 = None
    if len(daily) >= 20:
        w = daily[-20:]
        mean = sum(w) / 20
        vol20 = math.sqrt(sum((x - mean) ** 2 for x in w) / 19)

    vols = [b.get("volume") for b in bars if b.get("volume")]
    vol_ratio = None
    if len(vols) >= 21 and sum(vols[-21:-1]):
        vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20)

    if sma50 and sma200:
        if last > sma50 > sma200:
            trend = "up"
        elif last < sma50 < sma200:
            trend = "down"
        else:
            trend = "mixed"
    else:
        trend = "unknown"

    day_change = daily[-1] if daily else None

    return {
        "as_of": bars[-1]["date"],
        "bars": len(bars),
        "price": _round(last, 4),
        "sma20": _round(sma20), "sma50": _round(sma50), "sma200": _round(sma200),
        "rsi14": _round(_rsi(closes), 1),
        "atr_pct": _round(_atr_pct(bars)),
        "vol20_daily": _round(vol20),
        "ret_1m": _round(_ret(closes, TRADING_DAYS["1m"])),
        "ret_3m": _round(_ret(closes, TRADING_DAYS["3m"])),
        "ret_6m": _round(_ret(closes, TRADING_DAYS["6m"])),
        "ret_12m": _round(_ret(closes, TRADING_DAYS["12m"])),
        "high_52w": _round(high_52w), "low_52w": _round(low_52w),
        "pct_from_52w_high": _round(last / high_52w - 1) if high_52w else None,
        "volume_ratio": _round(vol_ratio, 2),
        "day_change": _round(day_change),
        "trend": trend,
        "source": "history",
    }


def from_quote(quote: dict) -> dict:
    """Degraded indicator set from a single quote, for when price history
    isn't available. FMP quotes carry 50/200-day averages and the 52-week
    range; ATR is approximated from today's range and flagged as such."""
    price = quote.get("price")
    if not price:
        return {}
    sma50, sma200 = quote.get("priceAvg50"), quote.get("priceAvg200")
    hi, lo = quote.get("dayHigh"), quote.get("dayLow")
    atr_pct = None
    if hi and lo and hi > lo:
        atr_pct = min(max((hi - lo) / price, 0.01), 0.08)
    trend = "unknown"
    if sma50 and sma200:
        trend = "up" if price > sma50 > sma200 else "down" if price < sma50 < sma200 else "mixed"
    year_hi = quote.get("yearHigh")
    chg = quote.get("changePercentage")
    return {
        "price": price,
        "sma50": sma50, "sma200": sma200,
        "atr_pct": _round(atr_pct),
        "high_52w": year_hi, "low_52w": quote.get("yearLow"),
        "pct_from_52w_high": _round(price / year_hi - 1) if year_hi else None,
        "day_change": _round(chg / 100) if chg is not None else None,
        "trend": trend,
        "source": "quote_only",
    }


def relative_strength(tech: dict, bench: dict, key: str = "ret_3m") -> float | None:
    a, b = tech.get(key), bench.get(key)
    if a is None or b is None:
        return None
    return round(a - b, 4)


def close_on_or_before(bars: list[dict], day: str) -> float | None:
    """Close on `day`, or the most recent close before it (weekends,
    holidays). None if the history starts after `day`."""
    best = None
    for b in bars:
        if b["date"] <= day:
            best = b["close"]
        else:
            break
    return best


def bars_after(bars: list[dict], day: str | None) -> list[dict]:
    if not day:
        return list(bars)
    return [b for b in bars if b["date"] > day]
