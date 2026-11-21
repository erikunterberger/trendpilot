"""Exports each run's full output as structured data for the local dashboard.

Reads profile/portfolio.md (YAML frontmatter) for actual holdings, values
them with live FMP quotes, maintains a net-worth history, and writes
dashboard/data.js — a single self-contained payload the static dashboard
page loads with no server needed.
"""
import json
from datetime import datetime
from pathlib import Path

import yaml

from src import config
from src.fetchers.fmp_client import FMPQuotaExhausted
from src.fetchers.market_data import get_quote

DASHBOARD_DIR = config.ROOT_DIR / "dashboard"
DATA_JS_PATH = DASHBOARD_DIR / "data.js"
PORTFOLIO_PATH = config.ROOT_DIR / "profile" / "portfolio.md"
HISTORY_PATH = config.REPORTS_DIR / "networth_history.json"

BUCKET_LABELS = {
    "core_high_conviction": "Core / High Conviction",
    "growth_momentum": "Growth & Momentum",
    "defensive_income": "Defensive & Income",
    "speculative": "Speculative",
    "diversified_etf": "Diversified / ETF",
}


def _load_portfolio_frontmatter() -> dict:
    if not PORTFOLIO_PATH.exists():
        return {}
    text = PORTFOLIO_PATH.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def _is_alpaca(name) -> bool:
    return "alpaca" in str(name or "").lower()


def _alpaca_holdings(account: dict, buckets: dict) -> list[dict]:
    rows = []
    for p in account.get("positions", []):
        if not p.get("qty"):
            continue
        cost = p.get("avg_entry_price")
        price = p.get("current_price")
        rows.append({
            "ticker": p["symbol"],
            "shares": p["qty"],
            "cost_basis_per_share": round(cost, 2) if cost else None,
            "account": f"Alpaca ({account.get('env')})",
            "bucket": buckets.get(p["symbol"]),
            "price_now": price,
            "value_now": round(p.get("market_value") or 0, 2),
            "gain_pct": round(100 * p["unrealized_plpc"], 2) if p.get("unrealized_plpc") is not None else None,
        })
    return rows


def _compute_portfolio_state(account: dict | None = None, buckets: dict | None = None) -> dict:
    """Net worth from profile/portfolio.md plus, when connected, the live
    Alpaca account. A live account replaces any manual entry named "Alpaca".
    A paper account is reported separately and never counted: its balance
    is play money."""
    pf = _load_portfolio_frontmatter()
    live = bool(account) and account.get("env") == "live"
    accounts = [a for a in (pf.get("accounts") or []) if not (live and _is_alpaca(a.get("name")))]
    holdings_raw = [h for h in (pf.get("holdings") or []) if not (live and _is_alpaca(h.get("account")))]
    other_assets = pf.get("other_assets") or []

    holdings = []
    holdings_value = 0.0
    bucket_values: dict[str, float] = {}
    pricing_error = None
    for h in holdings_raw:
        ticker = (h.get("ticker") or "").upper()
        shares = float(h.get("shares") or 0)
        cost = h.get("cost_basis_per_share")
        price = None
        if ticker and not pricing_error:
            try:
                price = get_quote(ticker, critical=True).get("price")
            except FMPQuotaExhausted as e:
                pricing_error = str(e)
        value = round(shares * price, 2) if price else None
        gain_pct = None
        if price and cost:
            gain_pct = round((price - float(cost)) / float(cost) * 100, 2)
        if value:
            holdings_value += value
            bucket = h.get("bucket") or "unbucketed"
            bucket_values[bucket] = bucket_values.get(bucket, 0.0) + value
        holdings.append(
            {
                "ticker": ticker,
                "shares": shares,
                "cost_basis_per_share": cost,
                "account": h.get("account"),
                "bucket": h.get("bucket"),
                "price_now": price,
                "value_now": value,
                "gain_pct": gain_pct,
            }
        )

    paper = None
    if account and live:
        for row in _alpaca_holdings(account, buckets or {}):
            holdings.append(row)
            holdings_value += row["value_now"]
            b = row["bucket"] or "unbucketed"
            bucket_values[b] = bucket_values.get(b, 0.0) + row["value_now"]
        accounts = accounts + [{"name": "Alpaca", "cash": round(account.get("cash") or 0, 2)}]
    elif account:
        paper = {"equity": account.get("equity"), "cash": account.get("cash"),
                 "as_of": account.get("as_of"), "holdings": _alpaca_holdings(account, buckets or {})}

    cash_total = sum(float(a.get("cash") or 0) for a in accounts)
    other_total = sum(float(a.get("estimated_value") or 0) for a in other_assets)
    net_worth = round(cash_total + holdings_value + other_total, 2)

    allocation = [
        {"label": BUCKET_LABELS.get(bucket, bucket), "value": round(value, 2)}
        for bucket, value in sorted(bucket_values.items(), key=lambda kv: -kv[1])
    ]
    if cash_total:
        allocation.append({"label": "Cash", "value": round(cash_total, 2)})
    if other_total:
        allocation.append({"label": "Other Assets", "value": round(other_total, 2)})

    return {
        "net_worth": net_worth,
        "cash_total": round(cash_total, 2),
        "holdings_value": round(holdings_value, 2),
        "accounts": accounts,
        "holdings": holdings,
        "other_assets": other_assets,
        "allocation": allocation,
        "profile_last_updated": str(pf.get("last_updated", "")),
        "pricing_error": pricing_error,
        "account_source": f"alpaca-{account.get('env')}" if account else "manual",
        "account_as_of": (account or {}).get("as_of"),
        "paper_account": paper,
    }


def held_tickers(account: dict | None = None) -> set[str]:
    """Tickers you hold — portfolio.md plus the brokerage account — so
    advice can say "you hold this". Read locally; never sent anywhere."""
    pf = _load_portfolio_frontmatter()
    out = {(h.get("ticker") or "").upper() for h in (pf.get("holdings") or []) if h.get("ticker")}
    for p in (account or {}).get("positions", []):
        if p.get("qty"):
            out.add(p["symbol"])
    return out


def _live_account() -> tuple[dict | None, str | None]:
    """Fresh Alpaca snapshot for the Refresh button, or (None, reason)."""
    from src.fetchers import broker_alpaca
    if not broker_alpaca.is_configured():
        return None, None
    try:
        return broker_alpaca.get_snapshot(), None
    except broker_alpaca.AlpacaError as e:
        return None, str(e)


def _bucket_map() -> dict:
    """ticker -> category from the position book, for the allocation chart."""
    try:
        book = json.loads((config.REPORTS_DIR / "positions.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for p in sorted(book.get("positions", []), key=lambda p: p.get("recommended_on", "")):
        if p.get("source") == "agent":
            out[p["ticker"]] = p["category"]
    return out


def _load_history() -> list[dict]:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _update_history(net_worth: float, run_date: str) -> list[dict]:
    history = [h for h in _load_history() if h.get("date") != run_date]
    history.append({"date": run_date, "net_worth": net_worth})
    history.sort(key=lambda h: h["date"])
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return history


def _condense_articles(articles: list[dict], limit: int = 20) -> list[dict]:
    out = []
    for a in articles[:limit]:
        out.append(
            {
                "title": a.get("title"),
                "source": (a.get("source") or {}).get("name"),
                "description": a.get("description"),
                "url": a.get("url"),
                "publishedAt": a.get("publishedAt"),
            }
        )
    return out


def refresh_prices_only() -> dict:
    """Re-prices current holdings and patches just the portfolio section of
    data.js, without touching news/suggested/overall or calling any LLM.
    Used by the dashboard's manual "Refresh" button. Returns the fresh
    portfolio dict (with history) for the caller to send back to the browser.
    """
    from datetime import date

    account, account_error = _live_account()
    if account_error:
        # Without the account, net worth would silently drop its balance.
        raise RuntimeError(account_error)
    portfolio = _compute_portfolio_state(account, _bucket_map())
    if portfolio.get("pricing_error"):
        # Unpriced holdings would record a falsely low net worth.
        raise RuntimeError(portfolio["pricing_error"])
    history = _update_history(portfolio["net_worth"], date.today().isoformat())
    portfolio = {**portfolio, "history": history}

    if DATA_JS_PATH.exists():
        text = DATA_JS_PATH.read_text(encoding="utf-8")
        prefix = "window.DASHBOARD_DATA = "
        if text.startswith(prefix):
            try:
                data = json.loads(text[len(prefix):].rstrip().rstrip(";"))
                data["portfolio"] = portfolio
                DATA_JS_PATH.write_text(
                    prefix + json.dumps(data, indent=2, default=str) + ";\n",
                    encoding="utf-8",
                )
            except json.JSONDecodeError:
                pass  # leave data.js untouched if it's ever malformed

    return portfolio


def update_dashboard(
    market_snapshot: dict,
    news_bundle: dict,
    report_body: str,
    advice: dict,
    run_date: str,
    account: dict | None = None,
    account_error: str | None = None,
) -> Path:
    """advice: {new_signals, position_signals, account_signals, stats, rules,
    candidates} from the run. account: the run's Alpaca snapshot, if any."""
    portfolio = _compute_portfolio_state(account, _bucket_map())
    portfolio["account_error"] = account_error
    if portfolio.get("pricing_error") or account_error:
        history = _load_history()  # don't record a net worth with unpriced holdings
    else:
        history = _update_history(portfolio["net_worth"], run_date)

    data = {
        "run_date": run_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "portfolio": {**portfolio, "history": history},
        "news": {
            "top_headlines": _condense_articles(news_bundle.get("top_headlines", [])),
            "market_news": _condense_articles(news_bundle.get("market_news", [])),
            "sector_news": _condense_articles(news_bundle.get("sector_news", [])),
        },
        "advice": advice,
        "overall": {
            "macro": market_snapshot.get("macro", []),
            "gainers": market_snapshot.get("gainers", []),
            "losers": market_snapshot.get("losers", []),
            "most_active": market_snapshot.get("most_active", []),
            "sector_performance": market_snapshot.get("sector_performance", []),
            "earnings_calendar": market_snapshot.get("earnings_calendar", []),
        },
        "report_markdown": report_body,
    }

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_JS_PATH.write_text(
        "window.DASHBOARD_DATA = " + json.dumps(data, indent=2, default=str) + ";\n",
        encoding="utf-8",
    )
    return DATA_JS_PATH
