"""Multi-stage analysis pipeline.

Stage 1 — screen: Gemini proposes candidates from the day's data, tagging
each with the *setup* (why it's interesting) from a fixed list, informed by
the agent's measured track record per setup.
Between 1 and 2 — the code drops candidates that fail the universe rules
(leveraged/inverse funds, micro-caps, sub-$3 stocks), then pulls real
fundamentals and a year of daily prices for the survivors and computes
trend, momentum, RSI, volatility and relative strength vs the S&P 500.
Stage 2 — draft: Gemini writes the report: new ideas (eligible candidates
only, each with setup, conviction and an invalidation condition) and a
thesis review of every open position.
Stage 3 — verify: a second pass audits the draft against the payload.
Stage 4 — extract: ideas and reviews come back as JSON (JSON mode).

The model never sets prices. Entries, stops, trailing stops and sizes are computed
by src/analysis/positions.py from the price data, so they can't be invented.
"""
import json
import re
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from src import config
from src.analysis import strategy as strat
from src.analysis import technicals
from src.fetchers import market_data
from src.fetchers.fmp_client import FMPQuotaExhausted
from src.fetchers.fundamentals import get_fundamentals

# Transient server-side / quota conditions worth a retry rather than a dead run.
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
MAX_LLM_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 20

# Screening: up to MAX_SCREENED candidates get the cheap checks (a profile,
# cached for a week); at most MAX_DEEP survivors get ratios, analyst targets
# and a year of prices.
MAX_SCREENED = 25
MAX_DEEP = 15

# Reason shown for candidates the account can't buy a whole share of. It goes
# to the model in `rejected_candidates`, so it deliberately has no figures:
# a price cap would reveal the account's size.
UNAFFORDABLE = "share price too high to buy one whole share within the per-position limit"

CONSTRAINED_SCREEN_NOTE = """
Note: many candidates will be screened out because their share price is \
too high for this account to buy in whole shares. So list 20-25 tickers \
instead of 10-15, and make sure plenty of them are lower-priced shares — \
still real companies or funds, never penny stocks or micro-caps.
"""

_SETUP_LINES = "\n".join(f"- {k}: {v}" for k, v in strat.SETUPS.items() if k in strat.SELECTABLE_SETUPS)
_CATEGORY_LINES = "\n".join(f"- {k}: {v}" for k, v in strat.CATEGORIES.items())
_HORIZON_LINES = "\n".join(f"- {k}: {strat.HORIZON_DESCRIPTIONS[k]}" for k in strat.SELECTABLE_HORIZONS)

SYSTEM_PROMPT = f"""\
You are the research engine of a disciplined trading adviser for one \
aggressive individual investor with a roughly two-year horizon. Your job is \
to find the few trades with the best odds of beating the S&P 500 after \
risk, and to keep the existing positions honest. The benchmark is SPY. \
A pick that rises less than SPY over the same period was a bad pick.

How decisions are made in this system — understand this before writing:
- You choose WHAT to buy and judge whether open theses still hold.
- Code decides WHEN and HOW MUCH: it sets the entry (buy now vs. wait for \
a pullback), the initial stop-loss, the trailing stop, the position size \
and the exits, from real price data. There are NO profit targets: a \
position is held as long as its uptrend lasts and is sold when its \
trailing stop is hit (or after two weeks if it has gone nowhere). Never \
state entry, stop, target or price levels for a recommendation, and never \
suggest position sizes or selling at a price. They will be printed \
separately and anything you write would conflict.
- Every recommendation is tracked against SPY and the results come back to \
you as `agent_track_record`. It is real measured performance of past picks. \
Use it: lean toward setups with positive measured excess return, and away \
from setups that have lost to SPY. Groups with fewer than 10 trades are \
anecdotes, not evidence. Respect every rule in `learned_rules`; setups in \
`blocked_setups` must not be recommended.

Selection standards:
- Recommend ONLY tickers listed in `eligible_candidates` — they're the only \
ones with verified price history and fundamentals. Tickers in \
`rejected_candidates` failed the investor's rules; never recommend them.
- Never recommend anything in `investor_constraints.excluded_vehicles` \
(no leveraged or inverse ETFs, options or margin).
- A strong idea needs BOTH a fundamental or catalyst reason AND supportive \
price evidence from its technicals: trend, relative strength vs SPY \
(`rel_strength_3m`), distance from 52-week high, RSI. Say which evidence \
you're using. If price action contradicts the story, say so or skip it.
- A big move today is not a thesis. Don't pick something mainly because it \
spiked unless the track record shows `same_day_mover` picks working.
- Quality over quantity: 0 to 5 new ideas. Zero is a valid answer on a \
day with nothing good. Don't re-recommend a ticker that's already an open \
position — review it instead.
- Trading approach: the investor wants SHORT-TERM ENTRIES — stocks \
positioned to move soon (a dip-buy in an uptrend, an oversold bounce, a \
breakout, a catalyst) — held for as long as the move keeps going. Pick \
ideas where the technicals show a clear near-term setup (RSI, distance \
from the 20-day average, recent pullback or breakout, relative strength), \
not ideas that need months before anything happens: a trade that's still \
flat after two weeks gets sold. Label each idea's expected hold: `swing` \
for a quick move (tight stop and trail), `short` for a move that builds \
over weeks, `long` only when the reason to own is durable enough that the \
uptrend could run for months (wide stop and trail so dips don't shake it \
out). The label sets how much room the trade gets, not when it's sold.
- Conviction is 1-5 and gets calibrated against outcomes: 5 is rare (clear \
catalyst, fundamentals and price all agree), 3 is a reasonable bet, 1-2 \
is a speculative stab.
- Use Google Search to verify facts and check that each story is current. \
Never state something as fact from memory alone; label inferences.

Allowed categories:
{_CATEGORY_LINES}

Allowed setups:
{_SETUP_LINES}

Allowed expected holds (the `horizon` field):
{_HORIZON_LINES}

Output valid Markdown with exactly these sections, in this order:

## Executive Summary
2-3 bullets: the most important takeaways today, including anything urgent \
about open positions.

## Market Situation
3-5 bullets on what's driving the tape: cite the actual index, VIX and \
yield numbers, sector rotation and the key news.

## Structural Analysis
The 2-3 most important threads: the force at play, who benefits or loses, \
durable or noise. One bullet each.

## New Ideas
Each idea, highest conviction first, as:
### TICKER — Category | setup | horizon | Conviction N/5
- **Thesis:** why it should move up soon and keep beating SPY while the \
trend lasts, citing specific \
fundamentals and technicals from the payload.
- **Catalyst:** the dated or concrete trigger, if any (use the earnings \
calendar).
- **Key risk:** the single biggest risk.
- **Invalidation:** an observable condition that would prove the thesis \
wrong (e.g. "guidance cut at the Feb 12 report", "loses data-center \
share to X") — not a price level.
If there are no ideas worth taking today, write one line saying so and why.

## Open Position Review
For each position in `open_positions` (skip the section with one line if \
there are none): **TICKER** — Intact / Watch / Broken — one line of \
evidence. Judge the THESIS and its invalidation condition, not the price: \
price exits are handled by rules. Mark Broken only for concrete evidence \
that the reason for owning it is gone, and cite it. A falling price alone \
is not Broken.

## Risks & What to Watch
4-6 bullets: what could invalidate the above, with dated events from the \
earnings calendar.

## Disclaimer
One line: automated analysis for informational purposes only, not \
personalized financial advice; do your own diligence or consult a licensed \
advisor before acting.
"""

CANDIDATE_PROMPT = f"""\
You are screening for a daily trading report. From the data below (and \
Google Search to verify names or surface strong candidates the day's data \
missed), list the 10-15 tickers most worth deep analysis today. Spread \
across the categories, but favor setups with a positive measured record in \
`agent_track_record` and never propose setups listed in `blocked_setups`. \
Never propose leveraged or inverse ETFs, or anything under ~$250M market \
cap. Include promising candidates beyond today's movers; a ticker that \
merely spiked today is not a candidate by itself.

Allowed categories:
{_CATEGORY_LINES}

Allowed setups:
{_SETUP_LINES}

Respond with ONLY a JSON array, no prose, in this exact shape:
[{{"ticker": "AMD", "category": "growth_momentum", "setup": "momentum_trend", "reason": "one short phrase"}}]
"""

VERIFY_PROMPT = """\
You are a skeptical research editor. Audit the draft report below against \
the data payload it was written from. Use Google Search to spot-check \
factual claims.

Rules:
- Delete or correct any claim not supported by the payload or a search \
result. If a number disagrees with the payload, the payload wins.
- Delete any entry price, stop, target or position size the draft states \
for a recommendation; those are set elsewhere.
- Remove any New Ideas entry for a ticker that isn't in \
`eligible_candidates`, or that's a leveraged/inverse fund.
- A position marked Broken must cite concrete evidence; if it only cites \
price weakness, change it to Watch.
- Don't soften opinions or restructure; keep every heading exactly as-is.

Do your fact-checking silently. The very first characters of your response \
must be "## Executive Summary". Return ONLY the corrected report.
"""

EXTRACT_PROMPT = f"""\
From the report below, extract the new ideas and the open position \
reviews. Return a JSON object in exactly this shape:
{{"new_ideas": [{{"ticker": "AMD", "category": "growth_momentum", \
"setup": "momentum_trend", "horizon": "medium", "conviction": 4, \
"thesis": "one sentence", "catalyst": "short or empty", \
"key_risk": "one sentence", "invalidation": "one sentence"}}],
"position_reviews": [{{"ticker": "NVDA", "verdict": "intact", "note": "one line"}}]}}

category must be one of: {", ".join(strat.CATEGORIES)}.
setup must be one of: {", ".join(strat.SELECTABLE_SETUPS)}.
horizon must be one of: {", ".join(strat.SELECTABLE_HORIZONS)}.
verdict must be one of: intact, watch, broken.
Use empty lists when a section has nothing.
"""


def _client() -> genai.Client:
    api_key = config.require(config.GEMINI_API_KEY, "GEMINI_API_KEY")
    return genai.Client(api_key=api_key)


def _generate(client, contents: str, system: str | None = None,
              use_search: bool = True, max_tokens: int = 32768,
              json_mode: bool = False) -> str:
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        tools=[types.Tool(google_search=types.GoogleSearch())] if use_search else None,
        # JSON mode can't be combined with the search tool.
        response_mime_type="application/json" if json_mode and not use_search else None,
    )

    # Gemini's free tier returns 503 under load often enough that a single
    # transient failure used to abort the whole run — after every market and
    # news call had already been spent. Retry the retryable codes first.
    response = None
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=config.ANALYSIS_MODEL, contents=contents, config=cfg
            )
            break
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            if code not in RETRYABLE_STATUS_CODES or attempt == MAX_LLM_ATTEMPTS:
                raise
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"  Gemini returned {code}; retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_LLM_ATTEMPTS})...")
            time.sleep(wait)

    finish_reason = None
    if response.candidates:
        finish_reason = response.candidates[0].finish_reason
    if finish_reason is not None and str(finish_reason) not in ("STOP", "FinishReason.STOP"):
        print(f"Warning: response finished with '{finish_reason}' — output may be truncated.")
    return response.text or ""


def _strip_preamble(text: str) -> str | None:
    """Models occasionally narrate their fact-checking before the report.
    Slice from the first real heading so scratch text never gets saved."""
    match = re.search(r"^##\s+Executive Summary\s*$", text, re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return text[match.start():].strip()


def _balanced_spans(text: str, open_ch: str, close_ch: str) -> list[str]:
    spans = []
    start = text.find(open_ch)
    if start == -1:
        return spans
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                spans.append(text[start:i + 1])
                break
    return spans


def _parse_json(text: str, kind: type):
    """Pull a JSON array or object out of a model response: as-is, then a
    fenced block, then the first balanced span."""
    stripped = text.strip()
    candidates = [stripped]
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates += _balanced_spans(stripped, *(("[", "]") if kind is list else ("{", "}")))
    for c in candidates:
        try:
            data = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, kind):
            return data
    return kind()


def _parse_json_array(text: str) -> list:
    """Like _parse_json(text, list), but if the array was cut off (the
    response hit its token limit), keep every complete object before the
    cut rather than losing the whole list."""
    data = _parse_json(text, list)
    if data or "[" not in text:
        return data
    salvaged = []
    for m in re.finditer(r"\{[^{}]*\}", text[text.find("["):]):
        try:
            obj = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            salvaged.append(obj)
    if salvaged:
        print(f"  Note: response was cut off; kept {len(salvaged)} complete entries.")
    return salvaged


def _condense_news(articles: list[dict], limit: int = 30) -> list[dict]:
    return [
        {
            "title": a.get("title"),
            "source": (a.get("source") or {}).get("name"),
            "description": a.get("description"),
            "publishedAt": a.get("publishedAt"),
        }
        for a in articles[:limit]
    ]


def _condense_movers(rows: list[dict], limit: int = 15) -> list[dict]:
    return [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "price": r.get("price"),
            "changesPercentage": r.get("changesPercentage"),
            "marketCap": r.get("marketCap"),
            "sector": r.get("sector"),
        }
        for r in rows[:limit]
    ]


def _build_payload(market_snapshot: dict, news_bundle: dict, history: list[dict], ctx: dict) -> dict:
    return {
        "date": ctx["today"],
        "investor_constraints": ctx["constraints"],
        "agent_track_record": ctx["track_record"],
        "open_positions": ctx["open_positions"],
        "benchmark_technicals": ctx.get("benchmark_technicals"),
        "macro_levels": market_snapshot.get("macro", []),
        "gainers": _condense_movers(market_snapshot.get("gainers", [])),
        "losers": _condense_movers(market_snapshot.get("losers", [])),
        "most_active": _condense_movers(market_snapshot.get("most_active", [])),
        "sector_performance": market_snapshot.get("sector_performance", []),
        "earnings_calendar_next_14d": market_snapshot.get("earnings_calendar", []),
        "recent_report_history": history,
        "top_headlines": _condense_news(news_bundle.get("top_headlines", [])),
        "market_news": _condense_news(news_bundle.get("market_news", [])),
        "sector_news": _condense_news(news_bundle.get("sector_news", [])),
    }


def _candidate_technicals(symbol: str, bench_tech: dict) -> dict:
    """A year of daily bars -> indicators. Falls back to quote-only
    indicators when history isn't available. {} if neither works."""
    bars = market_data.get_history(symbol)
    tech = technicals.compute(bars) if bars else {}
    if not tech or tech.get("bars", 0) < 30:
        tech = technicals.from_quote(market_data.get_quote(symbol))
    if tech and bench_tech:
        tech["rel_strength_3m"] = technicals.relative_strength(tech, bench_tech, "ret_3m")
        tech["rel_strength_6m"] = technicals.relative_strength(tech, bench_tech, "ret_6m")
    return tech


def screen_candidates(candidates: list[dict], bench_tech: dict, blocked: dict,
                      max_share_price: float | None = None) -> tuple[list[dict], list[dict]]:
    """Universe rules, learned blocks and affordability first (one cached
    profile call each), then fundamentals + technicals for the survivors.
    Returns (eligible, rejected). Each eligible entry carries the data the
    plan needs, so nothing is fetched twice.

    max_share_price: with a connected account, the most one whole share can
    cost while staying inside the per-position cap. Pricier candidates are
    dropped here, before any further FMP calls are spent on them."""
    by_symbol = {}
    for c in candidates:
        if not isinstance(c, dict):
            continue
        t = (c.get("ticker") or "").strip().upper()
        if t and not t.startswith("^") and t not in by_symbol:
            by_symbol[t] = c
        if len(by_symbol) >= MAX_SCREENED:
            break

    eligible, rejected, survivors = [], [], []
    for t, c in by_symbol.items():
        setup = strat.normalize_setup(c.get("setup"))
        try:
            profile = market_data.get_profile(t)
        except FMPQuotaExhausted:
            rejected.append({"ticker": t, "reason": "FMP quota ran out before it was screened"})
            continue
        ok, why = market_data.check_universe(profile)
        if ok and setup in blocked:
            ok, why = False, blocked[setup]
        if ok and max_share_price and (profile.get("price") or 0) > max_share_price:
            ok, why = False, UNAFFORDABLE
        if not ok:
            rejected.append({"ticker": t, "reason": why})
            continue
        if len(survivors) < MAX_DEEP:
            survivors.append((t, c, setup))

    for t, c, setup in survivors:
        try:
            f = get_fundamentals(t)
            tech = _candidate_technicals(t, bench_tech)
        except FMPQuotaExhausted:
            rejected.append({"ticker": t, "reason": "FMP quota ran out before its data was fetched"})
            continue
        if not tech:
            rejected.append({"ticker": t, "reason": "no price data"})
            continue
        eligible.append({
            "ticker": t,
            "proposed_category": strat.normalize_category(c.get("category")),
            "proposed_setup": setup,
            "screen_reason": c.get("reason"),
            "fundamentals": f,
            "technicals": tech,
        })
    return eligible, rejected


def _validate_ideas(raw: list, eligible: list[dict]) -> tuple[list[dict], list[str]]:
    by_t = {e["ticker"]: e for e in eligible}
    ideas, dropped = [], []
    seen = set()
    for r in raw:
        if not isinstance(r, dict):
            continue
        t = (r.get("ticker") or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        if t not in by_t:
            dropped.append(t)
            continue
        try:
            conv = int(r.get("conviction") or 3)
        except (TypeError, ValueError):
            conv = 3
        setup = strat.normalize_setup(r.get("setup"))
        if setup == "unclassified":
            setup = by_t[t]["proposed_setup"]
        horizon = strat.normalize_horizon(r.get("horizon"))
        if horizon not in strat.SELECTABLE_HORIZONS:
            horizon = "short"  # legacy labels ("medium", "short_medium") -> normal room
        ideas.append({
            "ticker": t,
            "category": strat.normalize_category(r.get("category") or by_t[t]["proposed_category"]),
            "setup": setup,
            "horizon": horizon,
            "conviction": min(max(conv, 1), 5),
            "thesis": r.get("thesis"),
            "catalyst": r.get("catalyst"),
            "key_risk": r.get("key_risk"),
            "invalidation": r.get("invalidation"),
            "fundamentals": by_t[t]["fundamentals"],
            "technicals": by_t[t]["technicals"],
        })
    return ideas, dropped


def build_analysis(market_snapshot: dict, news_bundle: dict, history: list[dict], ctx: dict) -> dict:
    """Runs the full pipeline. ctx: today, constraints, track_record,
    open_positions, benchmark_technicals, blocked_setups, and optionally
    max_share_price (used locally for screening; never put in the payload).

    Returns {report, ideas, reviews, eligible, rejected, dropped}."""
    client = _client()
    payload = _build_payload(market_snapshot, news_bundle, history, ctx)
    payload_json = json.dumps(payload, indent=2, default=str)

    print("  Stage 1/4: screening candidates...")
    screen_prompt = CANDIDATE_PROMPT + (CONSTRAINED_SCREEN_NOTE if ctx.get("max_share_price") else "")
    try:
        # Gemini 2.5 counts its internal "thinking" toward the output limit,
        # so a small limit truncated the list; 4096 produced no candidates.
        candidates_text = _generate(client, screen_prompt + "\n\nDATA:\n" + payload_json, max_tokens=16384)
    except genai_errors.APIError as e:
        print(f"  Warning: screening stage failed ({e!r}). Continuing with no new ideas.")
        candidates_text = ""
    candidates = _parse_json_array(candidates_text)
    print(f"  Candidates: {', '.join(str(c.get('ticker')) for c in candidates if isinstance(c, dict)) or '(none parsed)'}")

    print("  Fetching fundamentals and price history for candidates...")
    eligible, rejected = screen_candidates(candidates, ctx.get("benchmark_technicals") or {},
                                           ctx.get("blocked_setups") or {}, ctx.get("max_share_price"))
    print(f"  Eligible: {', '.join(e['ticker'] for e in eligible) or '(none)'}")
    for r in rejected:
        print(f"  Rejected {r['ticker']}: {r['reason']}")

    payload["eligible_candidates"] = [
        {k: e[k] for k in ("ticker", "proposed_category", "proposed_setup", "screen_reason", "fundamentals", "technicals")}
        for e in eligible
    ]
    payload["rejected_candidates"] = rejected
    payload_json = json.dumps(payload, indent=2, default=str)

    print("  Stage 2/4: drafting report...")
    draft = _generate(
        client,
        "Here is today's data payload, as JSON. Produce the report per your instructions.\n\n" + payload_json,
        system=SYSTEM_PROMPT,
    )

    print("  Stage 3/4: verification pass...")
    verified = ""
    try:
        verified = _generate(client, VERIFY_PROMPT + "\n\nDATA PAYLOAD:\n" + payload_json + "\n\nDRAFT REPORT:\n" + draft)
    except genai_errors.APIError as e:
        print(f"  Warning: verification stage failed ({e!r}). Falling back to the unverified draft.")

    report = _strip_preamble(verified) or _strip_preamble(draft) or draft
    if not verified:
        report = (
            "> **Note:** the automated verification pass did not run for this report — the "
            "figures below have not been audited against the source data.\n\n"
        ) + report

    print("  Stage 4/4: extracting ideas and position reviews...")
    extracted = {}
    try:
        extracted = _parse_json(
            _generate(client, EXTRACT_PROMPT + "\n\nREPORT:\n" + report,
                      use_search=False, max_tokens=16384, json_mode=True),
            dict,
        )
    except genai_errors.APIError as e:
        print(f"  Warning: extraction failed ({e!r}). The report is saved; no positions change today.")

    ideas, dropped = _validate_ideas(extracted.get("new_ideas") or [], eligible)
    if dropped:
        print(f"  Dropped ideas with no verified data: {', '.join(dropped)}")
    open_tickers = {p["ticker"] for p in ctx["open_positions"]}
    reviews = [r for r in (extracted.get("position_reviews") or [])
               if isinstance(r, dict) and (r.get("ticker") or "").upper() in open_tickers]

    return {"report": report, "ideas": ideas, "reviews": reviews,
            "eligible": eligible, "rejected": rejected, "dropped": dropped}
