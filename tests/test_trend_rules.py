"""The "ride the trend" rules: expected-hold labels, migration of old
records, and how they flow into account checks and the paper trader."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.analysis import account as acct
from src.analysis import analyzer
from src.analysis import positions as pos
from src.analysis import strategy as strat
from tests.helpers import open_position


class Labels(unittest.TestCase):
    def test_horizon_words(self):
        self.assertEqual(strat.normalize_horizon("1-2 weeks"), "swing")
        self.assertEqual(strat.normalize_horizon("swing"), "swing")
        self.assertEqual(strat.normalize_horizon("Short Term"), "short")
        self.assertEqual(strat.normalize_horizon("Long Term"), "long")
        self.assertEqual(strat.normalize_horizon("long trend"), "long")

    def test_legacy_labels_from_the_model_get_normal_room(self):
        eligible = [{"ticker": "F", "proposed_category": "growth_momentum", "proposed_setup": "momentum_trend",
                     "fundamentals": {}, "technicals": {}}]
        ideas, _ = analyzer._validate_ideas([{"ticker": "F", "horizon": "medium"}], eligible)
        self.assertEqual(ideas[0]["horizon"], "short")


class Migration(unittest.TestCase):
    def test_v1_book_rescored_under_trailing_rules(self):
        legacy = open_position(source="legacy", ticker="OLD")
        legacy.update(status="closed", close_reason="target hit — sold half", legacy_entry_price=50.0,
                      exits=[{"date": "2026-02-01", "price": 60.0, "fraction": 1.0, "reason": "x", "bench": 1}])
        agent = open_position(ticker="NEW")
        agent["plan"] = {"stop": 90.0, "stop_pct": 10.0, "target": 125.0, "rr": 2.5, "take_fraction": 0.5,
                         "atr_pct": 0.03, "size_pct": 8.0}
        unscorable = {**open_position(source="legacy", ticker="GONE"), "status": "unscorable"}
        book = {"version": 1, "positions": [legacy, agent, unscorable]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "positions.json"
            path.write_text(json.dumps(book))
            with mock.patch.object(pos, "BOOK_PATH", path):
                out = pos.load_book()
        old, new, gone = out["positions"]
        self.assertEqual(out["version"], pos.BOOK_VERSION)
        self.assertEqual((old["status"], old["exits"], old["remaining"]), ("legacy_unpriced", [], 1.0))
        self.assertNotIn("plan", old)
        self.assertEqual(old["legacy_entry_price"], 50.0)    # kept for reference
        self.assertNotIn("target", new["plan"])
        self.assertEqual(new["plan"]["exit_mode"], "trail")
        self.assertIn("trail_pct", new["plan"])
        self.assertEqual(gone["status"], "unscorable")

    def test_current_book_is_left_alone(self):
        book = {"version": pos.BOOK_VERSION, "positions": [open_position()]}
        self.assertIs(pos._upgrade(book), book)


class AccountWithTrailing(unittest.TestCase):
    def snap(self, orders, price=130.0):
        return {"env": "paper", "equity": 1000.0, "orders": orders,
                "positions": [{"symbol": "TEST", "qty": 5.0, "side": "long", "market_value": price * 5,
                               "current_price": price}]}

    def trailing_position(self, stop=114.4):
        p = open_position()
        p.update(trailing=True, stop=stop)
        return p

    def test_instructions_are_one_stop_for_all_shares(self):
        self.assertEqual(acct.order_instructions(5, 94.0), "place a GTC stop order for all 5 shares at $94.00.")

    def test_switch_only_when_trailing_from_here_is_at_least_as_safe(self):
        self.assertTrue(acct.can_switch_to_trailing(130.0, 12.0, 114.4))     # 130 x .88 = 114.4
        self.assertFalse(acct.can_switch_to_trailing(120.0, 12.0, 114.4))    # would start at 105.6

    def test_fixed_stop_on_a_trailing_position_is_flagged(self):
        fixed = {"symbol": "TEST", "side": "sell", "type": "stop", "qty": 5.0, "stop_price": 114.4,
                 "time_in_force": "gtc"}
        r = acct.check_account(self.snap([fixed]), {"positions": [self.trailing_position()]})
        self.assertEqual(r[0]["action"], "SWITCH TO TRAILING")

    def test_matching_trailing_order_is_protected(self):
        trail = {"symbol": "TEST", "side": "sell", "type": "trailing_stop", "qty": 5.0, "stop_price": 118.0,
                 "trail_percent": 12.0, "time_in_force": "gtc"}
        r = acct.check_account(self.snap([trail]), {"positions": [self.trailing_position()]})
        self.assertEqual(r[0]["action"], "PROTECTED")
        self.assertIn("12% trailing stop", r[0]["detail"])

    def test_too_wide_trailing_order_is_flagged(self):
        trail = {"symbol": "TEST", "side": "sell", "type": "trailing_stop", "qty": 5.0, "stop_price": 100.0,
                 "trail_percent": 20.0, "time_in_force": "gtc"}
        r = acct.check_account(self.snap([trail]), {"positions": [self.trailing_position()]})
        self.assertEqual(r[0]["action"], "RAISE STOP")


if __name__ == "__main__":
    unittest.main()
