"""Central config loaded from environment / .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

FMP_API_KEY = os.getenv("FMP_API_KEY", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# FMP's free tier allows 250 requests per UTC day. The client counts its own
# calls and refuses non-essential ones once only the reserve is left, so the
# calls that price open positions are never starved by market screening.
FMP_DAILY_LIMIT = int(os.getenv("FMP_DAILY_LIMIT", "250"))
FMP_CRITICAL_RESERVE = int(os.getenv("FMP_CRITICAL_RESERVE", "40"))

# Alpaca brokerage, read-only. Optional: without keys the agent falls back to
# profile/portfolio.md. ALPACA_ENV picks the account: "paper" (default) or
# "live" — each has its own keys.
ALPACA_API_KEY_ID = os.getenv("ALPACA_API_KEY_ID", "")
ALPACA_API_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY", "")
ALPACA_ENV = os.getenv("ALPACA_ENV", "paper").strip().lower()
# Opt-in for automatic order placement. Only the value "paper" does anything,
# and only together with ALPACA_ENV=paper: there is no live auto-trading.
ALPACA_AUTOTRADE = os.getenv("ALPACA_AUTOTRADE", "").strip().lower()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "gemini-2.5-flash")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")

REPORTS_DIR = ROOT_DIR / "reports"
CACHE_DIR = REPORTS_DIR / "cache"
PROFILE_DIR = ROOT_DIR / "profile"

# Optional: an Obsidian vault to save reports into (and read recent reports
# back from, for continuity). Leave unset to skip.
OBSIDIAN_VAULT_DIR = os.getenv("OBSIDIAN_VAULT_DIR", "").strip()


def require(value: str, name: str) -> str:
    if not value:
        raise RuntimeError(
            f"Missing required config '{name}'. Set it in {ROOT_DIR / '.env'} "
            f"(see .env.example)."
        )
    return value
