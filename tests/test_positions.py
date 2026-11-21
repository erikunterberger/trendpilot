import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.analysis import positions as pos
from src.fetchers.fmp_client import FMPQuotaExhausted
from tests.helpers import bars, open_position, strategy


def bench_flat(start="2026-01-01", n=400, price=500.0):
    return bars([price] * n, start=start)


class ExitRules(unittest.TestCase):
    def setUp(self):
        self.s = strategy()
        self.bench = bench_flat(start="2025-12-01", n=500)

    def run_bars(self, p, closes, **kw):
        b = bars([100.0] + closes, start=p["entry"]["date"], **kw)
        pos._evaluate(p, b, self.bench, b[-1]["date"], self.s)
        return p

    def test_stop_loss_fills_at_stop(self):
        # Each bar opens at the prior close, so 89 is reached intraday, not on a gap.
        p = self.run_bars(open_position(), [98, 95, 89], opens=[100, 100, 98, 95])
        self.assertEqual(p["status"], "closed")
        self.assertEqual(p["close_reason"], "stop-loss")
        self.assertAlmostEqual(p["exits"][0]["price"], 90.0)
        ret, bench = pos.position_return(p)
        self.assertAlmostEqual(ret, -10.0)
        self.assertAlmostEqual(bench, 0.0)

    def test_gap_down_fills_at_open_not_stop(self):
        closes = [98, 84]
        opens = [100, 98, 85]
        p = self.run_bars(open_position(), closes, opens=opens)
        self.assertAlmostEqual(p["exits"][0]["price"], 85.0)

    def test_winner_rides_past_any_target_and_exits_on_trailing_stop(self):
        # 12% trail once +1R (+10%). Rally to 150 (stop trails to 132), then drop to 125.
        p = self.run_bars(open_position(), [110, 120, 130, 140, 150, 125], spread=0.0,
                          opens=[100, 100, 110, 120, 130, 140, 150])
        self.assertEqual(p["close_reason"], "trailing stop")
        self.assertEqual(len(p["exits"]), 1)                 # no partial sells
        self.assertAlmostEqual(p["exits"][0]["price"], 132.0)
        ret, _ = pos.position_return(p)
        self.assertAlmostEqual(ret, 32.0)                    # the old rules sold half at +25%

    def test_stop_never_moves_down(self):
        p = self.run_bars(open_position(), [110, 150, 140], spread=0.0, opens=[100, 100, 110, 150])
        self.assertEqual(p["status"], "open")
        self.assertAlmostEqual(p["stop"], 132.0)             # 150 x 0.88, kept after the dip

    def test_breakeven_after_plus_one_r(self):
        # +11% puts it past 1R; the 12% trail (97.7) is below entry, so the stop sits at breakeven.
        p = self.run_bars(open_position(), [105, 111, 104, 99], spread=0.0,
                          opens=[100, 100, 105, 111, 104])
        self.assertEqual(p["close_reason"], "breakeven stop")
        self.assertAlmostEqual(p["exits"][0]["price"], 100.0)

    def test_trailing_start_is_signalled(self):
        p = open_position()
        b = bars([100.0, 105.0, 111.0], start="2026-01-01", spread=0.0)
        events = pos._evaluate(p, b, self.bench, b[-1]["date"], self.s)
        sig = pos._signal_from_events(p, events)
        self.assertEqual(sig["action"], "TRAILING")
        self.assertIn("12.0% trailing", sig["detail"])

    def test_stop_checked_before_the_bars_high(self):
        p = self.run_bars(open_position(), [100], lows=[100, 85], highs=[100, 130])
        self.assertEqual(p["close_reason"], "stop-loss")

    def test_flat_trade_is_sold_two_weeks_in(self):
        p = open_position()
        b = bars([100.0] * 30, start="2026-01-01")
        pos._evaluate(p, b, self.bench, b[-1]["date"], self.s)
        self.assertEqual(p["close_reason"], "no progress — freed the cash")
        self.assertEqual(p["closed_on"], "2026-01-15")

    def test_working_trade_is_never_cut_on_time(self):
        p = open_position()
        b = bars([100.0 + 0.5 * i for i in range(90)], start="2026-01-01", spread=0.0)
        pos._evaluate(p, b, self.bench, b[-1]["date"], self.s)
        self.assertEqual(p["status"], "open")                # 89 days in, still riding
        self.assertTrue(p["trailing"])

    def test_split_rescales_levels_instead_of_faking_a_loss(self):
        # Entry recorded at $100 pre-split; history is 4:1 split-adjusted.
        p = open_position()
        b = bars([25.0, 25.5, 26.0], start="2026-01-01", spread=0.0)
        events = pos._evaluate(p, b, self.bench, b[-1]["date"], self.s)
        self.assertEqual(p["status"], "open")
        self.assertAlmostEqual(p["entry"]["price"], 25.0)
        self.assertAlmostEqual(p["stop"], 22.5)
        self.assertEqual(events[0]["type"], "split")
        self.assertEqual(pos._signal_from_events(p, events)["action"], "UPDATE ORDERS")
        ret, _ = pos.position_return(p)
        self.assertAlmostEqual(ret, 4.0)

    def test_ordinary_move_is_not_mistaken_for_split(self):
        p = open_position()
        b = bars([97.0, 98.0], start="2026-01-01", spread=0.0)
        pos._evaluate(p, b, self.bench, b[-1]["date"], self.s)
        self.assertNotIn("split_adjustments", p)
        self.assertAlmostEqual(p["entry"]["price"], 100.0)

    def test_no_new_bars_changes_nothing(self):
        p = open_position(entry_date="2026-01-05")
        b = bars([100.0] * 5, start="2026-01-01")
        pos._evaluate(p, b, self.bench, "2026-01-05", self.s)
        self.assertEqual(p["status"], "open")
        self.assertEqual(p["exits"], [])


class PendingOrders(unittest.TestCase):
    def setUp(self):
        self.s = strategy()
        self.bench = bench_flat(start="2025-12-01", n=500)

    def pending(self, limit=95.0):
        p = open_position(entry=limit)
        p.update(status="pending", last_checked="2026-01-01")
        p.pop("entry")
        p["plan"]["buy_limit"] = limit
        p["plan"]["pending_expires"] = "2026-01-15"
        return p

    def test_fills_when_low_reaches_limit(self):
        p = self.pending()
        b = bars([100, 98, 97, 96], start="2026-01-01", lows=[99, 97, 96, 94.5], opens=[100, 99, 97, 96])
        events = pos._evaluate(p, b, self.bench, "2026-01-04", self.s)
        self.assertEqual(p["status"], "open")
        self.assertEqual(p["entry"]["date"], "2026-01-04")
        self.assertAlmostEqual(p["entry"]["price"], 95.0)
        self.assertEqual(events[0]["type"], "filled")

    def test_expires_unfilled(self):
        p = self.pending()
        b = bars([100] * 20, start="2026-01-01", spread=0.0)
        pos._evaluate(p, b, self.bench, "2026-01-20", self.s)
        self.assertEqual(p["status"], "cancelled")


class Plans(unittest.TestCase):
    def setUp(self):
        self.s = strategy()
        self.policy = {"blocked_setups": {}, "setup_size_factor": {}, "conviction_factors": {"4": 0.85}}

    def tech(self, **kw):
        t = {"atr_pct": 0.03, "rsi14": 55, "sma20": 98.0, "trend": "up", "day_change": 0.01, "source": "history"}
        t.update(kw)
        return t

    def plan(self, tech, price=100.0, **kw):
        args = dict(fundamentals={}, category="growth_momentum", horizon="medium", conviction=4,
                    setup="momentum_trend", earnings_date=None, today="2026-01-10")
        args.update(kw)
        return pos.build_plan(price, tech, args.pop("fundamentals"), args.pop("category"), args.pop("horizon"),
                              args.pop("conviction"), args.pop("setup"), self.s, self.policy,
                              args.pop("earnings_date"), args.pop("today"))

    def test_buy_now_levels(self):
        p = self.plan(self.tech(), horizon="short")
        self.assertEqual(p["action"], "BUY")
        self.assertAlmostEqual(p["stop_pct"], 6.0)          # 2.0 ATR x 3%
        self.assertAlmostEqual(p["stop"], 94.0)
        self.assertAlmostEqual(p["trail_pct"], 9.0)         # 3.0 ATR x 3%
        self.assertEqual(p["exit_mode"], "trail")
        self.assertNotIn("target", p)                        # no profit target
        self.assertLessEqual(p["size_pct"], 10.0)

    def test_long_trend_gets_more_room_than_a_swing(self):
        swing, long_ = self.plan(self.tech(), horizon="swing"), self.plan(self.tech(), horizon="long")
        self.assertLess(swing["trail_pct"], long_["trail_pct"])
        self.assertLessEqual(swing["stop_pct"], long_["stop_pct"])

    def test_overbought_waits_for_pullback(self):
        p = self.plan(self.tech(rsi14=82, sma20=90.0))
        self.assertEqual(p["action"], "WAIT")
        self.assertLess(p["buy_limit"], 100.0)
        self.assertIn("pending_expires", p)

    def test_spike_waits(self):
        self.assertEqual(self.plan(self.tech(day_change=0.15))["action"], "WAIT")

    def test_blocked_setup_passes(self):
        self.policy["blocked_setups"] = {"same_day_mover": "learned rule: nope"}
        p = self.plan(self.tech(), setup="same_day_mover")
        self.assertEqual(p["action"], "PASS")

    def test_no_volatility_data_passes(self):
        self.assertEqual(self.plan(self.tech(atr_pct=None))["action"], "PASS")

    def test_size_never_exceeds_profile_cap(self):
        p = self.plan(self.tech(atr_pct=0.005), category="defensive_income", conviction=5)
        self.assertLessEqual(p["size_pct"], self.s["max_position_pct"])

    def test_earnings_halves_size(self):
        normal = self.plan(self.tech())["size_pct"]
        near = self.plan(self.tech(), earnings_date="2026-01-12")["size_pct"]
        self.assertLess(near, normal)


class NewIdeas(unittest.TestCase):
    def setUp(self):
        self.s = strategy()
        self.policy = {"blocked_setups": {}, "setup_size_factor": {}, "conviction_factors": {}}

    def idea(self, t, category="growth_momentum"):
        return {"ticker": t, "category": category, "setup": "momentum_trend", "horizon": "medium",
                "conviction": 4, "thesis": "x", "fundamentals": {},
                "technicals": {"atr_pct": 0.03, "rsi14": 55, "sma20": 98.0, "trend": "up",
                               "day_change": 0.0, "source": "history"}}

    def test_opens_buy_and_respects_category_limit(self):
        book = {"positions": []}
        self.s["bucket_max_pct"]["speculative"] = 10
        ideas = [self.idea(t, "speculative") for t in ("AAA", "BBB", "CCC")]
        sigs = pos.open_new_ideas(book, ideas, "2026-01-10", self.s, self.policy, {},
                                  lambda t: {"price": 100.0}, 500.0)
        exposure, _ = pos._exposure(book)
        self.assertLessEqual(exposure.get("speculative", 0), 10.0 + 1e-9)
        self.assertTrue(any(s["action"] == "PASS" for s in sigs))

    def test_already_open_is_not_duplicated(self):
        book = {"positions": [open_position(ticker="AAA")]}
        sigs = pos.open_new_ideas(book, [self.idea("AAA")], "2026-01-10", self.s, self.policy, {},
                                  lambda t: {"price": 100.0}, 500.0)
        self.assertEqual(sigs[0]["action"], "ALREADY OPEN")
        self.assertEqual(len(book["positions"]), 1)

    def test_cooldown_after_stop_out(self):
        closed = open_position(ticker="AAA")
        closed.update(status="closed", close_reason="stop-loss", closed_on="2026-01-08")
        book = {"positions": [closed]}
        sigs = pos.open_new_ideas(book, [self.idea("AAA")], "2026-01-10", self.s, self.policy, {},
                                  lambda t: {"price": 100.0}, 500.0)
        self.assertEqual(sigs[0]["action"], "PASS")

    def test_quota_during_quote_passes_instead_of_crashing(self):
        def boom(t):
            raise FMPQuotaExhausted("out")
        sigs = pos.open_new_ideas({"positions": []}, [self.idea("AAA")], "2026-01-10", self.s,
                                  self.policy, {}, boom, 500.0)
        self.assertEqual(sigs[0]["action"], "PASS")


class Reviews(unittest.TestCase):
    def test_broken_thesis_sells_at_last_price(self):
        p = open_position(ticker="AAA")
        p.update(last_price=104.0, last_price_date="2026-01-09")
        book = {"positions": [p]}
        sigs = pos.apply_reviews(book, [{"ticker": "AAA", "verdict": "broken", "note": "guidance cut"}],
                                 "2026-01-10", bench_flat(start="2025-12-01"))
        self.assertEqual(sigs[0]["action"], "SELL")
        self.assertEqual(p["status"], "closed")
        self.assertAlmostEqual(p["exits"][0]["price"], 104.0)

    def test_watch_is_recorded_only(self):
        p = open_position(ticker="AAA")
        book = {"positions": [p]}
        sigs = pos.apply_reviews(book, [{"ticker": "AAA", "verdict": "watch", "note": "hmm"}], "2026-01-10", [])
        self.assertEqual(sigs, [])
        self.assertEqual(p["reviews"][-1]["verdict"], "watch")


class LegacyMigration(unittest.TestCase):
    def test_migrate_and_backfill_replays_history(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "picks_ledger.json"
            ledger.write_text(json.dumps([
                {"date": "2026-01-20", "ticker": "AAA", "category": "Growth & Momentum",
                 "horizon": "Short Term", "thesis": "surged 40% today", "price_at_pick": None},
                {"date": "2026-01-20", "ticker": "GONE", "category": "Speculative",
                 "horizon": "Short Term", "thesis": "x", "price_at_pick": 5.0},
            ]))
            with mock.patch.object(pos, "LEGACY_LEDGER_PATH", ledger), \
                 mock.patch.object(pos, "BOOK_PATH", Path(d) / "positions.json"):
                book = pos.load_book()
                self.assertEqual(book["migrated_legacy"], 2)
                aaa = next(p for p in book["positions"] if p["ticker"] == "AAA")
                self.assertEqual(aaa["setup"], "same_day_mover")
                self.assertEqual(aaa["horizon"], "short")

                hist = bars([100.0] * 20 + [80.0] * 30, start="2026-01-01", spread=0.0)

                def history(t, critical=False):
                    return hist if t == "AAA" else []

                res = pos.backfill_legacy(book, history, bench_flat(start="2025-12-01"), strategy())
                self.assertEqual(res, {"backfilled": 1, "unscorable": 1, "remaining": 0})
                self.assertAlmostEqual(aaa["entry"]["price"], 100.0)   # close on the pick date
                self.assertEqual(aaa["status"], "closed")
                self.assertEqual(aaa["close_reason"], "stop-loss")

    def test_corrupt_book_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "positions.json"
            bad.write_text("{not json")
            with mock.patch.object(pos, "BOOK_PATH", bad):
                with self.assertRaises(RuntimeError):
                    pos.load_book()


if __name__ == "__main__":
    unittest.main()
