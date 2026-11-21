"""Shared fixtures: synthetic daily bars and a default strategy."""
from datetime import date, timedelta

from src.analysis import strategy as strat


def bars(closes, start="2026-01-01", spread=0.01, lows=None, highs=None, opens=None):
    """One bar per calendar day. Highs/lows default to close +/- spread."""
    d0 = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        out.append({
            "date": (d0 + timedelta(days=i)).isoformat(),
            "open": opens[i] if opens else c,
            "high": highs[i] if highs else round(c * (1 + spread), 4),
            "low": lows[i] if lows else round(c * (1 - spread), 4),
            "close": c,
            "volume": 1_000_000,
        })
    return out


def strategy():
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in strat.DEFAULTS.items()}
    s["profile_loaded"] = False
    return s


def open_position(entry=100.0, stop_pct=10.0, trail_pct=12.0, horizon="short",
                  entry_date="2026-01-01", source="agent", ticker="TEST",
                  category="growth_momentum", setup="momentum_trend", conviction=4):
    stop = round(entry * (1 - stop_pct / 100), 4)
    return {
        "id": f"{entry_date}-{ticker}", "ticker": ticker, "source": source, "status": "open",
        "recommended_on": entry_date, "category": category, "setup": setup,
        "horizon": horizon, "horizon_days": strat.HORIZONS[horizon], "conviction": conviction,
        "plan": {"stop": stop, "stop_pct": stop_pct, "exit_mode": "trail", "trail_pct": trail_pct,
                 "trail_start_pct": stop_pct, "size_pct": 8.0},
        "entry": {"date": entry_date, "price": entry, "bench": 500.0},
        "stop": stop, "high_water": entry, "exits": [], "remaining": 1.0,
        "last_checked": entry_date, "signals": [],
    }
