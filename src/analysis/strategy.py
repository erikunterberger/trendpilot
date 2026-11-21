"""Trading rules, plus the investor's own constraints.

The constraints come from profile/investor-one-pager.md (YAML frontmatter),
read locally. Only the generic rules (excluded vehicles, position cap,
horizon, risk tolerance) are ever sent to the model; nothing identifying
from the profile leaves this machine.
"""
import re

import yaml

from src import config

PROFILE_PATH = config.PROFILE_DIR / "investor-one-pager.md"

CATEGORIES = {
    "core_high_conviction": "Core / High Conviction",
    "growth_momentum": "Growth & Momentum",
    "defensive_income": "Defensive & Income",
    "speculative": "Speculative",
    "diversified_etf": "Diversified / ETF",
}

# Expected hold. Trades are NOT sold on a schedule or at a profit target:
# every position is held as long as its trend lasts and exits on its
# trailing stop. The expected hold only sets how much room a trade gets —
# a "long" idea gets a wider stop and trail so a lasting uptrend isn't
# shaken out. "short_medium" and "medium" exist only for pre-rebuild picks.
HORIZONS = {"swing": 14, "short": 30, "short_medium": 90, "medium": 180, "long": 365}
SELECTABLE_HORIZONS = ["swing", "short", "long"]
HORIZON_LABELS = {
    "swing": "Swing (tight trail)", "short": "Short-term (normal trail)",
    "short_medium": "Short-term (legacy)", "medium": "Medium (legacy)", "long": "Long trend (wide trail)",
}
HORIZON_DESCRIPTIONS = {
    "swing": "a quick move (dip-buy, oversold bounce, breakout, catalyst pop): tight stop and trail",
    "short": "a move expected to build over weeks: normal stop and trail",
    "long": "a durable uptrend you'd ride for months: wide stop and trail so dips don't shake it out",
}

# Exit rules, the same for every position ("let winners run, cut losers fast"):
#   initial stop   entry - init_atr x ATR, bounded per category
#   breakeven      once the high reaches entry + 1R, the stop moves to entry
#   trailing       from +1R, stop = highest high x (1 - trail%), trail% =
#                  trail_atr x ATR%, bounded; it only ever moves up
#   flat trade     on the first day >= 14 calendar days after entry, a trade
#                  that's below entry + 0.5R is sold to free the cash
# No profit targets, no partial sells, no holding-period exits.
EXIT_RULES = {
    "init_atr": {"swing": 1.5, "short": 2.0, "short_medium": 2.0, "medium": 2.5, "long": 2.5},
    "trail_atr": {"swing": 2.5, "short": 3.0, "short_medium": 3.0, "medium": 3.5, "long": 3.5},
    "trail_bounds_pct": (6, 25),
    "breakeven_r": 1.0,
    "trail_start_r": 1.0,
    "no_progress_days": 14,
    "no_progress_min_r": 0.5,
}

# Why a stock was picked. The model must choose one, and the track record is
# broken down by it — that's how the agent learns which kinds of picks work.
SETUPS = {
    "momentum_trend": "established uptrend with strong relative strength vs the S&P 500",
    "breakout": "breaking out to new highs on above-average volume",
    "pullback_in_uptrend": "dip within a stock that remains in a long-term uptrend",
    "oversold_bounce": "sharp short-term drop (RSI under ~35) in a stock still above its 200-day average — buy the dip, sell the bounce",
    "earnings_catalyst": "dated earnings or event catalyst expected to re-rate the stock",
    "value_rerating": "undervalued vs fundamentals or analyst targets, with a reason to re-rate",
    "quality_compounder": "durable high-margin grower held for compounding",
    "turnaround_rebound": "beaten-down name, betting on recovery",
    "same_day_mover": "picked mainly because of a large move today",
    "defensive_income": "dividend / low-beta ballast",
    "diversified_etf": "broad or sector ETF exposure",
    # Legacy-only labels, used when classifying pre-rebuild picks.
    "leveraged_etf": "leveraged or inverse fund (excluded going forward)",
    "unclassified": "pre-rebuild pick that couldn't be classified",
}
SELECTABLE_SETUPS = [s for s in SETUPS if s not in ("leveraged_etf", "unclassified")]

# Setups where buying below falling averages is the point, not a red flag.
COUNTER_TREND_SETUPS = {"turnaround_rebound", "value_rerating", "defensive_income", "diversified_etf"}

DEFAULTS = {
    # Sizing. Risk per trade is the % of the portfolio lost if the stop hits.
    # At 1.0%, typical 7-10% stops always exceeded the 10% cap, so every
    # position came out the same size and conviction/volatility did nothing.
    "risk_per_trade_pct": 0.75,
    "max_position_pct": 10.0,
    "min_position_pct": 2.0,
    # Profile: ~30% of the portfolio stays in cash for living costs.
    "max_total_exposure_pct": 70.0,
    "bucket_max_pct": {
        "core_high_conviction": 15, "growth_momentum": 55, "defensive_income": 15,
        "speculative": 20, "diversified_etf": 20,
    },
    # Initial stops: a multiple of the average true range (EXIT_RULES),
    # bounded per category.
    "stop_atr_mult": dict(EXIT_RULES["init_atr"]),
    "stop_bounds_pct": {
        "core_high_conviction": (5, 12), "growth_momentum": (6, 18),
        "defensive_income": (4, 10), "speculative": (8, 25), "diversified_etf": (4, 10),
    },
    # Entry timing.
    "extended_rsi": 75,            # RSI above this = overbought, wait
    "extended_atr_mult": 2.0,      # this many ATRs above the 20-day avg = extended
    "spike_atr_mult": 3.0,         # a one-day move this big = chasing a spike
    "pending_days": 14,            # how long a WAIT limit order stays live
    "reentry_cooldown_days": 10,   # no re-buying a stock right after its stop hit
    "earnings_buffer_days": 5,     # half size when earnings are this close
    "max_new_ideas": 5,
    # Pre-rebuild picks: how many to backfill per run, and how often to re-check.
    "legacy_backfill_per_run": 25,
    "legacy_check_days": 7,
    "excluded_vehicles": ["margin", "options", "leveraged_etfs"],
    "risk_tolerance": "unspecified",
    "tactical_months": None,
}


def _frontmatter(path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def load() -> dict:
    """Default rules overlaid with the investor profile's own constraints."""
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    fm = _frontmatter(PROFILE_PATH)
    if fm.get("max_single_position_pct"):
        s["max_position_pct"] = float(fm["max_single_position_pct"])
    guard = fm.get("allocation_guardrails_pct") or {}
    for bucket, rng in guard.items():
        if bucket in s["bucket_max_pct"] and isinstance(rng, (list, tuple)) and rng:
            s["bucket_max_pct"][bucket] = float(max(rng))
    if fm.get("excluded_vehicles"):
        s["excluded_vehicles"] = list(fm["excluded_vehicles"])
    if fm.get("risk_tolerance"):
        s["risk_tolerance"] = str(fm["risk_tolerance"])
    th = fm.get("time_horizon") or {}
    if isinstance(th, dict) and th.get("tactical_months"):
        s["tactical_months"] = th["tactical_months"]
    s["profile_loaded"] = bool(fm)
    return s


def prompt_constraints(s: dict) -> dict:
    """The generic subset of the rules that the model is told about."""
    return {
        "risk_tolerance": s["risk_tolerance"],
        "tactical_horizon_months": s["tactical_months"],
        "trading_approach": ("short-term entries held as long as the move lasts: no profit targets, "
                             "exits only on a trailing stop, a broken thesis, or a trade that's gone "
                             "nowhere after two weeks. A stock that keeps rising is simply held longer."),
        "excluded_vehicles": s["excluded_vehicles"],
        "max_single_position_pct": s["max_position_pct"],
        "category_max_pct": s["bucket_max_pct"],
        "benchmark": "S&P 500 (SPY)",
    }


def normalize_category(text: str | None) -> str:
    t = (text or "").lower()
    if "core" in t or "conviction" in t:
        return "core_high_conviction"
    if "growth" in t or "momentum" in t:
        return "growth_momentum"
    if "defen" in t or "income" in t or "dividend" in t:
        return "defensive_income"
    if "spec" in t or "risk-reward" in t:
        return "speculative"
    if "etf" in t or "divers" in t or "fund" in t:
        return "diversified_etf"
    return "speculative"  # unknown risk is treated as the riskiest bucket


def normalize_horizon(text: str | None) -> str:
    """Maps free text to a horizon key. Pre-rebuild labels keep their old
    keys (short_medium, medium) so their records stay as they were."""
    t = (text or "").lower().replace("-", " ").replace("_", " ")
    if "swing" in t or "week" in t or "day" in t:
        return "swing"
    if "short" in t and ("medium" in t or "mid" in t):
        return "short_medium"
    if "trend" in t or "core" in t:
        return "long"
    if "short" in t:
        return "short"
    if "long" in t:
        return "long"
    if "medium" in t or "mid" in t:
        return "medium"
    return "medium"


def normalize_setup(text: str | None) -> str:
    t = (text or "").strip().lower().replace("-", "_").replace(" ", "_")
    return t if t in SETUPS else "unclassified"


_SAME_DAY_RE = re.compile(
    r"\d+(\.\d+)?%.{0,25}today|today.{0,40}\d+(\.\d+)?%|surg|gain(ed|ing)? over|up (over|nearly) \d",
    re.IGNORECASE,
)
_LEVERAGED_TEXT_RE = re.compile(r"\b[1-9]x\b|leveraged (daily|inverse|exposure)|daily target|inverse", re.IGNORECASE)
_REBOUND_RE = re.compile(r"rebound|despite .{0,30}(drop|decline|fall)|beaten|turnaround|recovery", re.IGNORECASE)


def classify_legacy_setup(thesis: str | None, category: str) -> str:
    """Best-effort setup label for a pick made before setups were recorded.
    Only confident patterns get a label; everything else is 'unclassified'
    rather than a guess."""
    t = thesis or ""
    if _LEVERAGED_TEXT_RE.search(t):
        return "leveraged_etf"
    if _SAME_DAY_RE.search(t):
        return "same_day_mover"
    if _REBOUND_RE.search(t):
        return "turnaround_rebound"
    if category == "defensive_income":
        return "defensive_income"
    if category == "diversified_etf":
        return "diversified_etf"
    return "unclassified"
