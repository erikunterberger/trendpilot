import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.analysis import analyzer, performance, technicals
from src.fetchers import fmp_client, market_data
from src.report import advice
from tests.helpers import bars, open_position

KEY = "SECRETKEY1234567890"


def closed(ticker, ret, bench, setup="same_day_mover", conviction=3, source="legacy"):
    p = open_position(ticker=ticker, setup=setup, conviction=conviction, source=source)
    entry = 100.0
    p["exits"] = [{"date": "2026-02-01", "price": entry * (1 + ret / 100), "fraction": 1.0,
                   "reason": "stop-loss", "bench": 500.0 * (1 + bench / 100)}]
    p.update(status="closed", remaining=0.0, closed_on="2026-02-01", close_reason="stop-loss")
    return p


class Learning(unittest.TestCase):
    def test_losing_setup_gets_blocked_with_enough_trades(self):
        book = {"positions": [closed(f"T{i}", -12.0, 2.0) for i in range(10)]}
        stats = performance.compute(book)
        self.assertAlmostEqual(stats["closed"]["avg_excess"], -14.0)
        policy = performance.learn_policy(stats)
        self.assertIn("same_day_mover", policy["blocked_setups"])

    def test_nine_trades_is_not_enough_to_learn(self):
        book = {"positions": [closed(f"T{i}", -12.0, 2.0) for i in range(9)]}
        policy = performance.learn_policy(performance.compute(book))
        self.assertEqual(policy["blocked_setups"], {})
        self.assertIn("no learned rules yet", policy["rules"][0])

    def test_slightly_negative_setup_sized_down_not_blocked(self):
        book = {"positions": [closed(f"T{i}", 1.0 if i % 2 else 0.0, 1.5, setup="momentum_trend")
                              for i in range(12)]}
        policy = performance.learn_policy(performance.compute(book))
        self.assertEqual(policy["setup_size_factor"].get("momentum_trend"), 0.75)
        self.assertNotIn("momentum_trend", policy["blocked_setups"])

    def test_winning_setup_is_favored(self):
        book = {"positions": [closed(f"T{i}", 15.0, 3.0, setup="breakout") for i in range(10)]}
        policy = performance.learn_policy(performance.compute(book))
        self.assertIn("breakout", policy["favored_setups"])

    def test_conviction_ignored_when_it_does_not_predict(self):
        hi = [closed(f"H{i}", 0.0, 2.0, setup="breakout", conviction=5, source="agent") for i in range(10)]
        lo = [closed(f"L{i}", 5.0, 2.0, setup="breakout", conviction=2, source="agent") for i in range(10)]
        policy = performance.learn_policy(performance.compute({"positions": hi + lo}))
        self.assertEqual(set(policy["conviction_factors"].values()), {0.8})

    def test_track_record_and_action_plan_render(self):
        book = {"positions": [closed(f"T{i}", -5.0, 1.0) for i in range(3)] + [open_position(ticker="OPN")]}
        stats = performance.compute(book)
        policy = performance.learn_policy(stats)
        md = advice.track_record(stats, policy)
        self.assertIn("## Track Record vs S&P 500", md)
        sig = {"ticker": "NVDA", "action": "BUY", "detail": "Buy. Notes: x.", "category": "growth_momentum",
               "setup": "momentum_trend", "conviction": 4, "horizon": "medium", "status": "open",
               "price": 100.0, "stop": 91.0, "target": 122.5, "size_pct": 9.5}
        plan = advice.action_plan([sig], [], {"NVDA"})
        self.assertIn("**NVDA** (you hold)", plan)
        self.assertIn("$91.00", plan)


class Technicals(unittest.TestCase):
    def test_indicators_on_steady_uptrend(self):
        t = technicals.compute(bars([100 * 1.002 ** i for i in range(260)], start="2025-01-01"))
        self.assertEqual(t["trend"], "up")
        self.assertGreater(t["rsi14"], 70)
        self.assertGreater(t["ret_3m"], 0)
        self.assertIsNotNone(t["sma200"])
        self.assertGreater(t["atr_pct"], 0)

    def test_short_history_degrades_gracefully(self):
        t = technicals.compute(bars([10, 11, 12]))
        self.assertIsNone(t["sma50"])
        self.assertIsNone(t["rsi14"])
        self.assertEqual(t["trend"], "unknown")

    def test_close_on_or_before_skips_weekends(self):
        b = bars([1, 2, 3], start="2026-01-01")
        self.assertEqual(technicals.close_on_or_before(b, "2026-01-10"), 3)
        self.assertIsNone(technicals.close_on_or_before(b, "2025-12-31"))


class UniverseRules(unittest.TestCase):
    def test_leveraged_and_inverse_names(self):
        for name in ("Direxion Daily Semiconductor Bull 3X Shares", "GraniteShares 2x Long NVDA Daily ETF",
                     "ProShares Short S&P500", "ProShares UltraPro QQQ", "Defiance Daily Target 2X Long IREN ETF",
                     "Direxion Daily TSLA Bull 2X Shares", "GraniteShares 2x Short NVDA Daily ETF"):
            self.assertTrue(market_data.is_leveraged_or_inverse(name), name)
        for name in ("Vanguard Short-Term Bond ETF", "iShares Core S&P 500 ETF", "NVIDIA Corporation",
                     "Bullfrog AI Holdings", "Health Care Select Sector SPDR Fund", "Netflix, Inc.",
                     "iShares 0-3 Month Treasury Bond ETF"):
            self.assertFalse(market_data.is_leveraged_or_inverse(name), name)

    def test_micro_cap_fund_rejected(self):
        ok, why = market_data.check_universe({"companyName": "CoreValues America First Technology ETF",
                                              "price": 25.0, "marketCap": 1_050_000, "isEtf": True})
        self.assertFalse(ok)
        self.assertIn("fund size", why)

    def test_normal_large_cap_accepted(self):
        ok, _ = market_data.check_universe({"companyName": "NVIDIA Corporation", "price": 180.0,
                                            "marketCap": 4e12, "isActivelyTrading": True})
        self.assertTrue(ok)

    def test_bar_normalization_handles_both_formats(self):
        full = [{"date": "2026-01-02", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9},
                {"date": "2026-01-01", "open": 1, "high": 2, "low": 0.5, "close": 1.0, "volume": 9}]
        light = {"historical": [{"date": "2026-01-01", "price": 3.0, "volume": 1}]}
        self.assertEqual([b["close"] for b in market_data._normalize_bars(full)], [1.0, 1.5])
        self.assertEqual(market_data._normalize_bars(light)[0]["close"], 3.0)


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FMPClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(fmp_client, "CACHE_DIR", d / "fmp"),
            mock.patch.object(fmp_client, "USAGE_PATH", d / "usage.json"),
            mock.patch.object(fmp_client.config, "FMP_API_KEY", KEY),
            mock.patch.object(fmp_client.config, "FMP_DAILY_LIMIT", 250),
            mock.patch.object(fmp_client.config, "FMP_CRITICAL_RESERVE", 40),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_429_raises_quota_and_never_leaks_key(self):
        with mock.patch.object(fmp_client.requests, "get", return_value=FakeResp(429, {})):
            with self.assertRaises(fmp_client.FMPQuotaExhausted) as cm:
                fmp_client.get("quote", {"symbol": "AAPL"})
        self.assertNotIn(KEY, str(cm.exception))
        self.assertTrue(fmp_client.usage_summary()["exhausted"])
        with mock.patch.object(fmp_client.requests, "get") as net:
            with self.assertRaises(fmp_client.FMPQuotaExhausted):
                fmp_client.get("quote", {"symbol": "MSFT"})
            net.assert_not_called()

    def test_limit_message_body_counts_as_quota(self):
        body = {"Error Message": "Limit Reach . Please upgrade your plan"}
        with mock.patch.object(fmp_client.requests, "get", return_value=FakeResp(200, body)):
            with self.assertRaises(fmp_client.FMPQuotaExhausted):
                fmp_client.get("quote", {"symbol": "AAPL"})

    def test_get_soft_empties_unavailable_but_raises_on_quota(self):
        with mock.patch.object(fmp_client.requests, "get", return_value=FakeResp(402, {})):
            self.assertEqual(fmp_client.get_soft("ratios-ttm", {"symbol": "X"}), [])
        with mock.patch.object(fmp_client.requests, "get", return_value=FakeResp(429, {})):
            with self.assertRaises(fmp_client.FMPQuotaExhausted):
                fmp_client.get_soft("ratios-ttm", {"symbol": "Y"})

    def test_network_error_message_has_no_key(self):
        err = fmp_client.requests.ConnectionError(f"https://x/?apikey={KEY}")
        with mock.patch.object(fmp_client.requests, "get", side_effect=err):
            with self.assertRaises(fmp_client.FMPError) as cm:
                fmp_client.get("quote", {"symbol": "AAPL"})
        self.assertNotIn(KEY, str(cm.exception))

    def test_cache_hit_spends_no_call(self):
        with mock.patch.object(fmp_client.requests, "get", return_value=FakeResp(200, [{"price": 1}])) as net:
            fmp_client.get("quote", {"symbol": "AAPL"})
            fmp_client.get("quote", {"symbol": "AAPL"})
            self.assertEqual(net.call_count, 1)
        self.assertEqual(fmp_client.usage_summary()["calls"], 1)

    def test_reserve_kept_for_critical_calls(self):
        fmp_client._save_usage({"day": fmp_client._utc_day(), "calls": 210, "exhausted": False, "capabilities": {}})
        with mock.patch.object(fmp_client.requests, "get", return_value=FakeResp(200, [{"price": 1}])):
            with self.assertRaises(fmp_client.FMPQuotaExhausted):
                fmp_client.get("quote", {"symbol": "AAPL"})
            self.assertEqual(fmp_client.get("quote", {"symbol": "AAPL"}, critical=True), [{"price": 1}])

    def test_stale_cache_served_when_allowed(self):
        with mock.patch.object(fmp_client.requests, "get", return_value=FakeResp(200, [{"mktCap": 5}])):
            fmp_client.get("profile", {"symbol": "AAPL"}, ttl=0)
        fmp_client._save_usage({"day": fmp_client._utc_day(), "calls": 250, "exhausted": True, "capabilities": {}})
        self.assertEqual(fmp_client.get("profile", {"symbol": "AAPL"}, ttl=0, allow_stale=True), [{"mktCap": 5}])

    def test_uncovered_symbol_is_remembered_and_not_retried(self):
        body = "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available"
        resp = FakeResp(402, {})
        resp.text = body
        with mock.patch.object(fmp_client.requests, "get", return_value=resp) as net:
            self.assertEqual(market_data.get_history("XLV"), [])
            self.assertEqual(net.call_count, 1)          # 'full' refused for the symbol -> no 'light' retry
            self.assertEqual(market_data.get_history("XLV"), [])
            self.assertEqual(net.call_count, 1)          # remembered: no second call at all
        self.assertIsNone(fmp_client.get_capability("history_endpoint"))  # plan capability untouched

    def test_history_falls_back_to_light_endpoint(self):
        def fake(url, params, timeout):
            if url.endswith("/full"):
                return FakeResp(402, {})
            return FakeResp(200, [{"date": "2026-01-01", "price": 10.0, "volume": 1}])
        with mock.patch.object(fmp_client.requests, "get", side_effect=fake):
            b = market_data.get_history("AAPL")
        self.assertEqual(b[0]["close"], 10.0)
        self.assertEqual(fmp_client.get_capability("history_endpoint")["value"], "light")


class Parsing(unittest.TestCase):
    def test_parse_object_from_fenced_text(self):
        text = 'Sure:\n```json\n{"new_ideas": [], "position_reviews": [{"ticker": "A", "verdict": "watch"}]}\n```'
        self.assertEqual(analyzer._parse_json(text, dict)["position_reviews"][0]["ticker"], "A")

    def test_truncated_candidate_list_keeps_complete_entries(self):
        text = ('[{"ticker": "F", "category": "growth", "setup": "momentum_trend", "reason": "x"}, '
                '{"ticker": "T", "category": "defensive", "setup": "defensive_income", "reason": "y"}, '
                '{"ticker": "PF')
        self.assertEqual([c["ticker"] for c in analyzer._parse_json_array(text)], ["F", "T"])

    def test_ideas_without_verified_data_are_dropped(self):
        eligible = [{"ticker": "NVDA", "proposed_category": "growth_momentum", "proposed_setup": "momentum_trend",
                     "fundamentals": {}, "technicals": {"atr_pct": 0.03}}]
        raw = [{"ticker": "nvda", "category": "Growth & Momentum", "setup": "made_up", "horizon": "Medium Term",
                "conviction": "9"},
               {"ticker": "SOXL", "category": "speculative", "setup": "momentum_trend", "horizon": "short"}]
        ideas, dropped = analyzer._validate_ideas(raw, eligible)
        self.assertEqual([i["ticker"] for i in ideas], ["NVDA"])
        self.assertEqual(dropped, ["SOXL"])
        self.assertEqual(ideas[0]["setup"], "momentum_trend")   # unknown setup -> screen's label
        self.assertEqual(ideas[0]["conviction"], 5)             # clamped
        self.assertEqual(ideas[0]["horizon"], "short")   # legacy "medium" gets normal room


if __name__ == "__main__":
    unittest.main()
