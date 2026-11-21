"""Quota-aware, caching client for Financial Modeling Prep (FMP).

Docs: https://site.financialmodelingprep.com/developer/docs

Why this exists instead of bare `requests.get` calls:

- FMP's free tier allows 250 requests per UTC day, and a full run used to
  spend well over 100 of them. Running the agent twice in one day re-bought
  identical data. Responses are now cached on disk with a per-endpoint
  lifetime, so a re-run costs a handful of calls instead of another 100+.
- A 429 used to be swallowed into an empty result, which the old ledger then
  recorded as a missing entry price. Quota exhaustion now raises
  `FMPQuotaExhausted`, so callers can't mistake "out of quota" for "no data".
- `requests`' own error messages include the full URL, API key included, and
  that text ended up in reports/run_log.txt. Every error raised here names
  the endpoint only.
"""
import hashlib
import json
import time
from datetime import datetime, timezone

import requests

from src import config

BASE_URL = "https://financialmodelingprep.com/stable"
CACHE_DIR = config.CACHE_DIR / "fmp"
USAGE_PATH = config.CACHE_DIR / "fmp_usage.json"

# Cache lifetimes, in seconds.
TTL_LIVE = 15 * 60            # quotes, movers lists: intraday data
TTL_DAILY = 12 * 3600         # end-of-day price history
TTL_WEEK = 7 * 24 * 3600      # profiles, ratios, analyst targets: slow-moving

CACHE_PRUNE_AGE = 14 * 24 * 3600

# Phrases FMP puts in an "Error Message" body.
_LIMIT_PHRASES = ("limit reach",)
_UNAVAILABLE_PHRASES = ("premium", "subscription", "upgrade", "not available", "exclusive")


class FMPError(RuntimeError):
    """FMP failed for a reason other than quota or plan gating."""


class FMPQuotaExhausted(FMPError):
    """The daily request allowance is spent (FMP said so, or the local
    budget would be overrun). Retry after the reset at 00:00 UTC."""


class FMPUnavailable(FMPError):
    """The endpoint or symbol isn't available on this plan, or returned
    nothing usable. symbol_restricted=True means the endpoint works but this
    particular symbol isn't covered (the free plan covers a limited set,
    mostly large US companies)."""

    def __init__(self, msg: str, symbol_restricted: bool = False):
        super().__init__(msg)
        self.symbol_restricted = symbol_restricted


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_usage() -> dict:
    today = _utc_day()
    try:
        usage = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        usage = {}
    if usage.get("day") != today:
        # New UTC day: the allowance has reset. Plan capabilities (which
        # history endpoint works) carry over.
        usage = {"day": today, "calls": 0, "exhausted": False,
                 "capabilities": usage.get("capabilities", {})}
    usage.setdefault("capabilities", {})
    return usage


def _save_usage(usage: dict) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(usage, indent=2), encoding="utf-8")


def usage_summary() -> dict:
    usage = _load_usage()
    return {
        "day_utc": usage["day"],
        "calls": usage["calls"],
        "limit": config.FMP_DAILY_LIMIT,
        "remaining": max(0, config.FMP_DAILY_LIMIT - usage["calls"]),
        "exhausted": usage["exhausted"],
    }


def get_capability(name: str):
    return _load_usage()["capabilities"].get(name)


def set_capability(name: str, value) -> None:
    usage = _load_usage()
    usage["capabilities"][name] = value
    _save_usage(usage)


def _cache_path(path: str, params: dict):
    key_src = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    digest = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:20]
    safe = path.replace("/", "_")
    return CACHE_DIR / f"{safe}-{digest}.json"


def _read_cache(path: str, params: dict, ttl: float | None):
    """Returns cached data, or None. ttl=None accepts any age (stale reads)."""
    cp = _cache_path(path, params)
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if ttl is not None and time.time() - entry.get("fetched_at", 0) > ttl:
        return None
    return entry.get("data")


def _write_cache(path: str, params: dict, data) -> None:
    cp = _cache_path(path, params)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps({"fetched_at": time.time(), "data": data}), encoding="utf-8")


def _is_known_unavailable(path: str, params: dict) -> bool:
    """A symbol FMP refused for this plan within the last week. Asking again
    would only spend another call to get the same refusal."""
    try:
        entry = json.loads(_cache_path(path, params).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(entry.get("unavailable")) and time.time() - entry.get("fetched_at", 0) < TTL_WEEK


def _remember_unavailable(path: str, params: dict) -> None:
    cp = _cache_path(path, params)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps({"fetched_at": time.time(), "unavailable": "symbol"}), encoding="utf-8")


def prune_cache() -> int:
    """Deletes cache files older than two weeks. Returns how many."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - CACHE_PRUNE_AGE
    removed = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def get(path: str, params: dict | None = None, ttl: float = TTL_LIVE,
        critical: bool = False, allow_stale: bool = False):
    """Fetches an FMP /stable/ endpoint through the cache and the quota guard.

    critical:    may spend the reserved part of the daily budget. Use for
                 pricing open positions and the benchmark, never screening.
    allow_stale: if the quota is gone, fall back to a cached copy of any age.
                 Fine for profiles and ratios; wrong for prices.
    """
    params = dict(params or {})
    if params.get("symbol") and _is_known_unavailable(path, params):
        raise FMPUnavailable(f"'{params['symbol']}' isn't covered by your FMP plan.", symbol_restricted=True)
    cached = _read_cache(path, params, ttl)
    if cached is not None:
        return cached

    usage = _load_usage()
    budget = config.FMP_DAILY_LIMIT - (0 if critical else config.FMP_CRITICAL_RESERVE)
    if usage["exhausted"] or usage["calls"] >= budget:
        if allow_stale:
            stale = _read_cache(path, params, None)
            if stale is not None:
                return stale
        if usage["exhausted"]:
            raise FMPQuotaExhausted("FMP daily request limit reached (FMP returned 429 earlier today).")
        raise FMPQuotaExhausted(
            f"Local FMP budget spent: {usage['calls']} of {config.FMP_DAILY_LIMIT} calls used today"
            + ("" if critical else f", last {config.FMP_CRITICAL_RESERVE} reserved for pricing positions")
            + "."
        )

    api_key = config.require(config.FMP_API_KEY, "FMP_API_KEY")
    usage["calls"] += 1
    _save_usage(usage)
    try:
        resp = requests.get(f"{BASE_URL}/{path}", params={**params, "apikey": api_key}, timeout=20)
    except requests.RequestException as e:
        # str(e) would include the URL and therefore the key.
        raise FMPError(f"FMP request to '{path}' failed: {type(e).__name__}") from None

    if resp.status_code == 429:
        usage["exhausted"] = True
        _save_usage(usage)
        raise FMPQuotaExhausted("FMP daily request limit reached (HTTP 429).")
    if resp.status_code in (401, 402, 403):
        body = (resp.text or "")[:400].lower() if hasattr(resp, "text") else ""
        sym = params.get("symbol")
        if sym and "symbol" in body:
            # "This value set for 'symbol' is not available under your current
            # subscription" — the endpoint works, this symbol doesn't.
            _remember_unavailable(path, params)
            raise FMPUnavailable(f"'{sym}' isn't covered by your FMP plan.", symbol_restricted=True)
        raise FMPUnavailable(f"FMP '{path}' not available on this plan (HTTP {resp.status_code}).")
    if resp.status_code >= 400:
        raise FMPError(f"FMP '{path}' returned HTTP {resp.status_code}.")

    try:
        data = resp.json()
    except ValueError:
        raise FMPError(f"FMP '{path}' returned a non-JSON body.") from None

    if isinstance(data, dict) and "Error Message" in data:
        msg = str(data["Error Message"])
        low = msg.lower()
        if any(p in low for p in _LIMIT_PHRASES):
            usage["exhausted"] = True
            _save_usage(usage)
            raise FMPQuotaExhausted("FMP daily request limit reached.")
        if any(p in low for p in _UNAVAILABLE_PHRASES):
            raise FMPUnavailable(f"FMP '{path}' not available on this plan.")
        raise FMPError(f"FMP error on '{path}': {msg[:200]}")

    _write_cache(path, params, data)
    return data


def get_soft(path: str, params: dict | None = None, ttl: float = TTL_LIVE,
             critical: bool = False, allow_stale: bool = False):
    """Like get(), but returns [] when the data is unavailable or the call
    fails. Quota exhaustion still raises: running out of quota is not the
    same thing as "this symbol has no data", and treating it that way is
    what produced the ledger's blank entry prices."""
    try:
        return get(path, params, ttl=ttl, critical=critical, allow_stale=allow_stale)
    except FMPQuotaExhausted:
        raise
    except FMPError:
        return []
