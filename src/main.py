"""Entry point: fetch -> update positions -> learn -> analyze -> advise -> save.

Usage:
    python -m src.main            # report (.md + .pdf + Obsidian) + dashboard
    python -m src.main --email    # also email the PDF

Each run:
 1. Pulls market data, news and a year of S&P 500 (SPY) history.
 2. Replays every open position against the daily prices since the last run
    (initial stops, trailing stops, two-week flat-trade exits) -> sell/hold signals.
 3. Backfills pre-rebuild picks from price history, a batch per run.
 4. Recomputes the track record vs the S&P 500 and the rules learned from it.
 5. Runs the Gemini pipeline (screen -> draft -> verify -> extract).
 6. Applies the model's thesis reviews, sizes and opens new ideas.
 7. Writes the report: Action Plan + Track Record (code) + narrative (model).
"""
import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from src import config
from src.analysis import account as account_mod
from src.analysis import analyzer, performance, positions, technicals
from src.analysis import strategy as strategy_mod
from src.execution import paper_trader
from src.fetchers import broker_alpaca, fmp_client, market_data, news_fetcher
from src.fetchers.fmp_client import FMPQuotaExhausted
from src.report import advice, dashboard, emailer, obsidian, report_builder

EXIT_QUOTA = 2


def _positions_for_model(book: dict) -> list[dict]:
    out = []
    for p in positions.agent_positions(book):
        ret, bench = positions.position_return(p)
        reviews = p.get("reviews") or []
        out.append({
            "ticker": p["ticker"], "status": p["status"], "category": p["category"],
            "setup": p["setup"], "horizon": p["horizon"], "conviction": p.get("conviction"),
            "recommended_on": p["recommended_on"],
            "entry_date": (p.get("entry") or {}).get("date"),
            "days_held": ((date.today() - date.fromisoformat(p["entry"]["date"])).days
                          if p.get("entry") else None),
            "return_pct": ret, "sp500_return_same_period_pct": bench,
            "thesis": p.get("thesis"), "invalidation": p.get("invalidation"),
            "key_risk": p.get("key_risk"), "catalyst": p.get("catalyst"),
            "last_review": reviews[-1] if reviews else None,
            "technicals_now": p.get("tech"),
        })
    return out


def _quota_reset_local() -> str:
    now = datetime.now(timezone.utc)
    reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return reset.astimezone().strftime("%H:%M %Z on %a %b %d")


FALLBACK_REPORT = """## Executive Summary
- The AI analysis didn't run today (see the note at the top of the Action Plan). No new ideas were
  made, but every open position was still checked against its stop and trailing-stop rules and its
  orders were updated.

## New Ideas

No new ideas today — the AI analysis failed.

## Open Position Review

Not reviewed today — the AI analysis failed. Price-based exits still applied.
"""


def analysis_or_fallback(run_analysis, notes: list[str]) -> dict:
    """Runs the Gemini pipeline; if it fails for any reason (an outage, a
    rate limit that outlasts the retries, a missing key), returns an empty
    result instead of ending the run. Everything that doesn't need the AI —
    stop raises, trailing-stop switches, exits, the account checks and the
    report — still happens, so a bad day at Gemini never leaves positions
    unmanaged."""
    try:
        return run_analysis()
    except Exception as e:  # noqa: BLE001 - any failure here must not stop position management
        kind = type(e).__name__
        print(f"Warning: the AI analysis failed ({kind}: {str(e)[:200]}). "
              "No new ideas today; existing positions are still managed.")
        notes.append(f"The AI analysis failed today ({kind}) — no new ideas, but open positions were still "
                     "checked and their orders updated.")
        return {"report": FALLBACK_REPORT, "ideas": [], "reviews": [], "eligible": [], "rejected": [],
                "dropped": [], "failed": True}


def _merge_signals(base: list[dict], overrides: list[dict]) -> list[dict]:
    by_t = {s["ticker"]: s for s in overrides}
    merged = [by_t.pop(s["ticker"], s) for s in base]
    return merged + list(by_t.values())


def run(send_email: bool) -> None:
    run_date = date.today().isoformat()
    s = strategy_mod.load()
    fmp_client.prune_cache()

    u = fmp_client.usage_summary()
    print(f"FMP budget: {u['calls']}/{u['limit']} calls used today (UTC day {u['day_utc']}).")
    if u["exhausted"]:
        raise FMPQuotaExhausted("FMP daily request limit already reached today.")
    notes = []
    if not s["profile_loaded"]:
        notes.append("Investor profile not found — using default limits (10% max per position).")
        print("Warning: profile/investor-one-pager.md not found; using default limits.")

    # Brokerage account (read-only). Used locally for sizing, allocation and
    # order checks; never included in anything sent to the model.
    account, account_error, max_share_price = None, None, None
    if broker_alpaca.is_configured():
        print(f"Reading Alpaca account ({broker_alpaca.env()}, read-only)...")
        try:
            account = broker_alpaca.get_snapshot()
            sm = account_mod.summary(account)
            print(f"  Equity ${sm['equity']:,.2f}, cash ${sm['cash']:,.2f}, {sm['positions']} positions.")
            if sm["env"] == "paper":
                notes.append("Sizes use your Alpaca PAPER account — test data, not real money.")
            margin = account_mod.margin_warning(account)
            if margin:
                notes.append(f"Account settings: {margin}. Your profile excludes margin.")
                print(f"  Warning: {margin}.")
            if sm["equity"]:
                max_share_price = round(sm["equity"] * s["max_position_pct"] / 100, 2)
                print(f"  Affordability filter: skipping shares above ${max_share_price:,.2f} "
                      f"(one share would exceed {s['max_position_pct']:.0f}% of the account).")
        except broker_alpaca.AlpacaError as e:
            account_error = str(e)
            notes.append(f"Alpaca account unavailable this run ({e}) — sizes are % of portfolio.")
            print(f"Warning: {e} Continuing without the account.")

    print("Fetching market data (movers, macro, earnings calendar)...")
    market_snapshot = market_data.get_market_snapshot()

    print("Fetching news...")
    news_bundle = news_fetcher.get_daily_news_bundle()

    print("Loading S&P 500 benchmark history...")
    bench_sym, bench = market_data.get_benchmark_history()
    bench_tech = technicals.compute(bench) if bench else {}
    bench_price = None
    if bench_sym:
        bench_price = market_data.get_quote(bench_sym, critical=True).get("price") or bench[-1]["close"]
    else:
        notes.append("No S&P 500 history from FMP this run — returns aren't benchmarked today.")
        print("Warning: no benchmark history; returns won't be compared to the S&P 500 this run.")

    book = positions.load_book()
    if book.get("migrated_legacy") and not positions.BOOK_PATH.exists():
        print(f"Migrated {book['migrated_legacy']} pre-rebuild picks from picks_ledger.json.")

    print("Updating positions against prices since the last run...")
    position_signals = positions.update_positions(book, run_date, market_data.get_history, bench, s)
    positions.save_book(book)  # price-rule exits are kept even if Gemini fails below

    stats = performance.compute(book)
    policy = performance.learn_policy(stats)
    for r in policy["rules"]:
        print(f"  Rule: {r}")

    print("Loading recent report history from Obsidian...")
    history = obsidian.load_recent_reports(exclude_date=run_date)

    print("Running multi-stage analysis (4 Gemini calls, may take a few minutes)...")
    result = analysis_or_fallback(lambda: analyzer.build_analysis(market_snapshot, news_bundle, history, {
        "today": run_date,
        "constraints": strategy_mod.prompt_constraints(s),
        "track_record": performance.model_feedback(stats, policy),
        "open_positions": _positions_for_model(book),
        "benchmark_technicals": bench_tech,
        "blocked_setups": policy["blocked_setups"],
        "max_share_price": max_share_price,
    }), notes)
    unaffordable = [r["ticker"] for r in result["rejected"] if r["reason"] == analyzer.UNAFFORDABLE]
    if unaffordable:
        notes.append(f"Screened out {len(unaffordable)} candidate(s) priced above ${max_share_price:,.2f} a share "
                     f"(one share would exceed your {s['max_position_pct']:.0f}% position limit): "
                     f"{', '.join(unaffordable)}.")

    review_signals = positions.apply_reviews(book, result["reviews"], run_date, bench)
    earnings = {e["symbol"]: e["date"] for e in market_snapshot.get("earnings_calendar", []) if e.get("symbol")}
    new_signals = positions.open_new_ideas(
        book, result["ideas"], run_date, s, policy, earnings,
        lambda t: market_data.get_quote(t, critical=True), bench_price, account=account,
    )
    positions.save_book(book)
    position_signals = _merge_signals(position_signals, review_signals)

    # Backfilling old picks matters least, so it runs last and only spends
    # whatever FMP budget today's ideas left over.
    backfill = None
    if any(p["status"] == "legacy_unpriced" for p in book["positions"]):
        backfill = positions.backfill_legacy(book, market_data.get_history, bench, s)
        positions.save_book(book)
        print(f"Backfilled {backfill['backfilled']} pre-rebuild picks "
              f"({backfill['unscorable']} unscorable, {backfill['remaining']} left).")
    stats = performance.compute(book)
    # Today's decisions used the rules learned before this run; the report
    # shows the rules as they stand now, which is what the next run applies.
    policy = performance.learn_policy(stats)
    account_signals = account_mod.check_account(account, book) if account else []

    # Paper auto-trading (opt-in, paper account only).
    trades, paper_perf = [], None
    off_reason = paper_trader.disabled_reason()
    if off_reason:
        notes.append(f"Auto-trading is off: {off_reason}.")
        print(f"Warning: auto-trading is off: {off_reason}.")
    if account and paper_trader.enabled() and account.get("env") == "paper":
        actions = paper_trader.plan_actions(account, book, new_signals, position_signals, account_signals, s)
        print(f"Paper auto-trading: {len(actions)} action(s).")
        if actions:
            trades = paper_trader.execute(actions)
            for t in trades:
                print(f"  {'OK ' if t['ok'] else 'FAIL'} {t['kind']:<12} {t['symbol']:<6} {t['detail']}")
            try:  # re-read so the report shows the orders just placed
                account = broker_alpaca.get_snapshot()
                account_signals = account_mod.check_account(account, book)
            except broker_alpaca.AlpacaError as e:
                print(f"  Warning: couldn't re-read the account after trading ({e}).")
        paper_perf = paper_trader.record_performance(account, bench_price, run_date)

    print("Building report...")
    held = dashboard.held_tickers(account)
    body = "\n".join([
        advice.action_plan(new_signals, position_signals, held, notes,
                           account=account, account_signals=account_signals,
                           trades=trades, paper_perf=paper_perf,
                           autotrade=bool(account and paper_trader.enabled())),
        advice.track_record(stats, policy, backfill),
        result["report"],
    ])
    markdown_text = report_builder.build_markdown(market_snapshot, news_bundle, body)
    md_path, pdf_path = report_builder.save_report(markdown_text)
    print(f"Saved: {md_path}")
    print(f"Saved: {pdf_path}")

    dash_path = dashboard.update_dashboard(market_snapshot, news_bundle, body, {
        "new_signals": new_signals,
        "position_signals": position_signals,
        "account_signals": account_signals,
        "account": account_mod.summary(account) if account else None,
        "autotrade": bool(account and paper_trader.enabled()),
        "trades": trades,
        "paper_performance": paper_perf,
        "notes": notes,
        "stats": stats,
        "rules": policy["rules"],
        "candidates": [{"ticker": e["ticker"], "setup": e["proposed_setup"],
                        "fundamentals": e["fundamentals"], "technicals": e["technicals"]}
                       for e in result["eligible"]],
        "rejected": result["rejected"],
    }, run_date, account=account, account_error=account_error)
    print(f"Dashboard: data updated -> {dash_path}")

    vault_path = obsidian.save_to_vault(markdown_text, run_date)
    if vault_path:
        print(f"Obsidian: saved to {vault_path}")
    else:
        print("Obsidian: vault not reachable, skipped (report still saved locally).")

    if send_email:
        print(f"Emailing report to {config.EMAIL_TO}...")
        emailer.send_report_email(
            subject=f"Daily Market Report — {run_date}",
            body_text="Attached: today's automated market and investment report.",
            pdf_path=pdf_path,
        )
        print("Email sent.")

    print("\n--- Today's actions ---")
    if paper_perf and paper_perf.get("return_pct") is not None:
        print(f"\nPaper account since {paper_perf['since']}: {paper_perf['return_pct']:+.2f}% "
              f"(S&P 500 {paper_perf['spy_return_pct']:+.2f}%)" if paper_perf.get("spy_return_pct") is not None
              else f"\nPaper account since {paper_perf['since']}: {paper_perf['return_pct']:+.2f}%")
    quiet = ("HOLD", "PASS", "PROTECTED", "NOT BOUGHT", "UNTRACKED")
    actionable = [x for x in new_signals + position_signals + account_signals if x["action"] not in quiet]
    for x in actionable or [{"ticker": "-", "action": "NONE", "detail": "No buys or sells today."}]:
        print(f"  {x['action']:<11} {x['ticker']:<6} {x['detail']}")
    u = fmp_client.usage_summary()
    print(f"\nFMP budget: {u['calls']}/{u['limit']} calls used today.")


def main() -> None:
    # Windows consoles and the redirected run_log.txt use a legacy encoding;
    # one unprintable character (e.g. from a news headline or model text)
    # used to be enough to crash the whole run. Substitute instead.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Daily stock trading adviser")
    parser.add_argument("--email", action="store_true", help="Email the report after building it")
    args = parser.parse_args()

    try:
        run(send_email=args.email)
    except FMPQuotaExhausted as e:
        print(f"\nError: {e} The allowance resets at 00:00 UTC ({_quota_reset_local()}). "
              "No positions were changed by the failed step.", file=sys.stderr)
        sys.exit(EXIT_QUOTA)
    except RuntimeError as e:
        # Config/validation problems already carry an actionable message.
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level guard for the scheduled run
        print(f"\nError: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
