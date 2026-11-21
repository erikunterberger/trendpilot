"""Track record vs the S&P 500, and the rules the agent learns from it.

"Getting smarter" here means three concrete, inspectable mechanisms, all
driven by closed trades measured against SPY over the same holding period:

1. Setup gating. Each pick records *why* it was made (momentum trend,
   same-day mover, value re-rating, ...). A setup that has lost to the S&P
   500 badly over enough closed trades is blocked; one that's slightly
   negative gets smaller positions.
2. Conviction calibration. If high-conviction picks don't actually beat
   low-conviction ones, sizing stops trusting the model's conviction.
3. Feedback to the model. The numbers and the learned rules are put in
   front of the model every run, with sample sizes, so it can lean toward
   what has worked.

Nothing is learned from fewer than MIN_SAMPLE closed trades — below that,
a few lucky or unlucky picks would decide the rules.
"""
from collections import Counter
from datetime import date

from src.analysis import strategy as strat
from src.analysis.positions import position_return

MIN_SAMPLE = 10
DEFAULT_CONVICTION_FACTORS = {"5": 1.0, "4": 0.85, "3": 0.7, "2": 0.5, "1": 0.5}


def _rows(book: dict) -> list[dict]:
    rows = []
    for p in book["positions"]:
        if p["status"] not in ("open", "closed") or not p.get("entry"):
            continue
        ret, bench = position_return(p)
        if ret is None:
            continue
        stop_pct = (p.get("plan") or {}).get("stop_pct")
        end = p.get("closed_on") or p.get("last_price_date") or p["entry"]["date"]
        rows.append({
            "ticker": p["ticker"], "source": p["source"], "setup": p["setup"],
            "category": p["category"], "conviction": p.get("conviction"), "horizon": p.get("horizon"),
            "closed": p["status"] == "closed",
            "ret": ret, "bench": bench,
            "excess": round(ret - bench, 2) if bench is not None else None,
            "r_mult": round(ret / stop_pct, 2) if stop_pct else None,
            "reason": p.get("close_reason"),
            "entry_date": p["entry"]["date"], "end_date": end,
            "days": (date.fromisoformat(end) - date.fromisoformat(p["entry"]["date"])).days,
        })
    return rows


def _agg(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    with_bench = [r for r in rows if r["excess"] is not None]
    rs = [r["r_mult"] for r in rows if r["r_mult"] is not None]
    out = {
        "n": n,
        "avg_return": round(sum(r["ret"] for r in rows) / n, 2),
        "win_rate": round(100 * sum(r["ret"] > 0 for r in rows) / n, 1),
        "best": max(rows, key=lambda r: r["ret"])["ticker"],
        "worst": min(rows, key=lambda r: r["ret"])["ticker"],
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_days": round(sum(r["days"] for r in rows) / n),
    }
    if with_bench:
        m = len(with_bench)
        out.update({
            "n_benchmarked": m,
            "avg_bench": round(sum(r["bench"] for r in with_bench) / m, 2),
            "avg_excess": round(sum(r["excess"] for r in with_bench) / m, 2),
            "beat_rate": round(100 * sum(r["excess"] > 0 for r in with_bench) / m, 1),
        })
    return out


def _group(rows: list[dict], key) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return {k: _agg(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


def compute(book: dict) -> dict:
    rows = _rows(book)
    closed = [r for r in rows if r["closed"]]
    agent = [r for r in rows if r["source"] == "agent"]
    status = Counter(p["status"] for p in book["positions"])
    recent = sorted(closed, key=lambda r: r["end_date"], reverse=True)[:12]
    return {
        "as_of": date.today().isoformat(),
        "min_sample": MIN_SAMPLE,
        "counts": {
            "recommendations": len(book["positions"]),
            "scored": len(rows), "open": status.get("open", 0),
            "closed": status.get("closed", 0), "pending": status.get("pending", 0),
            "cancelled": status.get("cancelled", 0),
            "unscorable": status.get("unscorable", 0),
            "awaiting_backfill": status.get("legacy_unpriced", 0),
        },
        "closed": _agg(closed),
        # Positions opened today have no return yet; counting them as
        # "didn't beat the S&P" would just be noise.
        "open": _agg([r for r in rows if not r["closed"] and r["days"] > 0]),
        "agent_closed": _agg([r for r in agent if r["closed"]]),
        "legacy_closed": _agg([r for r in closed if r["source"] == "legacy"]),
        "by_setup_closed": _group(closed, lambda r: r["setup"]),
        "by_setup_all": _group(rows, lambda r: r["setup"]),
        "by_category_closed": _group(closed, lambda r: r["category"]),
        "by_horizon_closed": _group(closed, lambda r: r["horizon"]),
        "by_conviction_closed": _group([r for r in closed if r["conviction"]],
                                       lambda r: "high (4-5)" if r["conviction"] >= 4 else "low (1-3)"),
        "exit_reasons": dict(Counter(r["reason"] for r in closed)),
        "recent_closed": [
            {k: r[k] for k in ("ticker", "setup", "horizon", "ret", "bench", "excess", "reason", "days", "source")}
            for r in recent
        ],
    }


def learn_policy(stats: dict) -> dict:
    """Turns the track record into rules the buy logic enforces."""
    policy = {
        "blocked_setups": {},
        "setup_size_factor": {},
        "favored_setups": [],
        "conviction_factors": dict(DEFAULT_CONVICTION_FACTORS),
        "rules": [],
    }
    for setup, a in stats["by_setup_closed"].items():
        if setup in ("unclassified", "leveraged_etf") or a.get("n_benchmarked", 0) < MIN_SAMPLE:
            continue
        label = setup.replace("_", " ")
        n, beat, excess = a["n_benchmarked"], a["beat_rate"], a["avg_excess"]
        won = round(n * beat / 100)
        if excess < -3 and beat < 40:
            msg = (f"'{label}' picks beat the S&P 500 in only {won} of {n} closed trades "
                   f"(avg {excess:+.1f} pts vs SPY) — new ones blocked")
            policy["blocked_setups"][setup] = "learned rule: " + msg
            policy["rules"].append(msg)
        elif excess < 0:
            policy["setup_size_factor"][setup] = 0.75
            policy["rules"].append(
                f"'{label}' picks trail the S&P 500 (avg {excess:+.1f} pts over {n} closed trades) — sized 25% smaller")
        elif excess > 3 and beat > 55:
            policy["favored_setups"].append(setup)
            policy["rules"].append(
                f"'{label}' picks beat the S&P 500 in {won} of {n} closed trades (avg {excess:+.1f} pts) — favored")

    conv = stats["by_conviction_closed"]
    hi, lo = conv.get("high (4-5)", {}), conv.get("low (1-3)", {})
    if hi.get("n_benchmarked", 0) >= MIN_SAMPLE and lo.get("n_benchmarked", 0) >= MIN_SAMPLE:
        gap = hi["avg_excess"] - lo["avg_excess"]
        if gap <= 0:
            policy["conviction_factors"] = {k: 0.8 for k in DEFAULT_CONVICTION_FACTORS}
            policy["rules"].append(
                f"high-conviction picks haven't beaten low-conviction ones ({gap:+.1f} pts) — sizing ignores conviction")
        else:
            policy["rules"].append(
                f"high-conviction picks beat low-conviction ones by {gap:+.1f} pts — conviction sizing kept")

    if not policy["rules"]:
        n = stats["closed"].get("n", 0)
        policy["rules"].append(
            f"no learned rules yet: {n} closed trades so far; rules need {MIN_SAMPLE}+ closed trades of the same kind")
    return policy


def model_feedback(stats: dict, policy: dict) -> dict:
    """The compact track-record view the model sees each run."""
    def slim(a):
        return {k: a.get(k) for k in ("n", "n_benchmarked", "avg_return", "avg_excess", "beat_rate", "win_rate", "avg_days")}
    return {
        "note": (f"Measured outcomes of past recommendations vs the S&P 500 over the same holding "
                 f"periods. Treat groups with fewer than {MIN_SAMPLE} trades as anecdotes."),
        "closed_overall": slim(stats["closed"]),
        "by_setup_closed": {k: slim(v) for k, v in stats["by_setup_closed"].items()},
        "by_setup_including_open": {k: slim(v) for k, v in stats["by_setup_all"].items()},
        "by_expected_hold_closed": {k: slim(v) for k, v in stats["by_horizon_closed"].items()},
        "exit_reasons": stats["exit_reasons"],
        "recent_closed_trades": stats["recent_closed"],
        "learned_rules": policy["rules"],
        "blocked_setups": list(policy["blocked_setups"]),
        "favored_setups": policy["favored_setups"],
    }
