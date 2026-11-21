import json
import re
import unittest
from unittest import mock

from src.analysis import analyzer

CAP = 137.53  # an unusual number, easy to spot if it leaks into a prompt


def profile(sym, price, mcap=5e10, name=None):
    return {"symbol": sym, "companyName": name or f"{sym} Inc", "price": price,
            "marketCap": mcap, "isActivelyTrading": True}


PROFILES = {"CHEAP": profile("CHEAP", 40.0), "PRICEY": profile("PRICEY", 450.0),
            "MID": profile("MID", 120.0), "LEV": profile("LEV", 30.0, name="Direxion Daily Bull 3X")}


class Screening(unittest.TestCase):
    def run_screen(self, tickers, cap):
        deep = []

        def fundamentals(t):
            deep.append(t)
            return {"symbol": t, "price": PROFILES.get(t, profile(t, 50.0))["price"]}

        with mock.patch.object(analyzer.market_data, "get_profile",
                               side_effect=lambda t: PROFILES.get(t, profile(t, 50.0))), \
             mock.patch.object(analyzer, "get_fundamentals", side_effect=fundamentals), \
             mock.patch.object(analyzer, "_candidate_technicals", return_value={"atr_pct": 0.03}):
            eligible, rejected = analyzer.screen_candidates(
                [{"ticker": t, "setup": "momentum_trend"} for t in tickers], {}, {}, cap)
        return eligible, rejected, deep

    def test_unaffordable_dropped_before_any_deep_fetch(self):
        eligible, rejected, deep = self.run_screen(["CHEAP", "PRICEY", "MID"], CAP)
        self.assertEqual([e["ticker"] for e in eligible], ["CHEAP", "MID"])
        self.assertEqual(deep, ["CHEAP", "MID"])            # no ratios/targets/history spent on PRICEY
        self.assertEqual(rejected, [{"ticker": "PRICEY", "reason": analyzer.UNAFFORDABLE}])

    def test_reason_reveals_no_figures(self):
        self.assertIsNone(re.search(r"\d", analyzer.UNAFFORDABLE))

    def test_no_cap_without_an_account(self):
        eligible, _, _ = self.run_screen(["CHEAP", "PRICEY"], None)
        self.assertEqual([e["ticker"] for e in eligible], ["CHEAP", "PRICEY"])

    def test_universe_rules_still_apply(self):
        _, rejected, _ = self.run_screen(["LEV"], CAP)
        self.assertIn("leveraged", rejected[0]["reason"])

    def test_deep_fetch_capped(self):
        many = [f"T{i}" for i in range(30)]
        eligible, _, deep = self.run_screen(many, CAP)
        self.assertEqual(len(deep), analyzer.MAX_DEEP)
        self.assertEqual(len(eligible), analyzer.MAX_DEEP)


class PromptPrivacy(unittest.TestCase):
    def capture(self, cap):
        prompts = []

        def gen(client, contents, system=None, **kw):
            prompts.append((system or "") + contents)
            if "extract the new ideas" in contents:
                return json.dumps({"new_ideas": [], "position_reviews": []})
            if "screening for a daily trading report" in contents:
                return "[]"
            return "## Executive Summary\n- x"

        ctx = {"today": "2026-11-20", "constraints": {"max_single_position_pct": 10.0},
               "track_record": {}, "open_positions": [], "benchmark_technicals": {},
               "blocked_setups": {}, "max_share_price": cap}
        with mock.patch.object(analyzer, "_client", return_value=None), \
             mock.patch.object(analyzer, "_generate", side_effect=gen), \
             mock.patch.object(analyzer, "screen_candidates",
                               return_value=([], [{"ticker": "PRICEY", "reason": analyzer.UNAFFORDABLE}])):
            analyzer.build_analysis({}, {}, [], ctx)
        return prompts

    def test_cap_never_reaches_the_model(self):
        prompts = self.capture(CAP)
        self.assertEqual(len(prompts), 4)
        for p in prompts:
            self.assertNotIn("137.5", p)
            self.assertNotIn("max_share_price", p)

    def test_screen_asks_for_more_and_cheaper_names_when_constrained(self):
        self.assertIn("lower-priced shares", self.capture(CAP)[0])
        self.assertNotIn("lower-priced shares", self.capture(None)[0])


if __name__ == "__main__":
    unittest.main()
