import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src.execution import paper_trader as pt
from tests.helpers import open_position, strategy

NOW = datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)


def snap(positions=(), orders=(), equity=1000.0):
    return {"env": "paper", "equity": equity, "cash": equity, "positions": list(positions), "orders": list(orders)}


def held(sym, qty, value):
    return {"symbol": sym, "qty": qty, "side": "long", "market_value": value}


def order(oid, sym, side, kind, qty, stop=None, cls="", parent=None, cid="", created=None, status="new"):
    return {"id": oid, "symbol": sym, "side": side, "type": kind, "qty": qty, "stop_price": stop,
            "order_class": cls, "parent_id": parent, "client_order_id": cid, "created_at": created, "status": status}


def buy_signal(sym="F", shares=5, price=12.0, action="BUY"):
    return {"ticker": sym, "action": action, "shares": shares, "price": price, "buy_limit": 11.5,
            "stop": 11.0, "target": 15.0, "position_id": f"2026-11-23-{sym}"}


class Guardrails(unittest.TestCase):
    def test_only_paper_with_explicit_opt_in(self):
        cases = [("paper", "paper", True), ("paper", "", False), ("live", "paper", False), ("paper", "live", False)]
        for env, auto, expected in cases:
            with mock.patch.object(pt.config, "ALPACA_ENV", env), \
                 mock.patch.object(pt.config, "ALPACA_AUTOTRADE", auto), \
                 mock.patch.object(pt.broker_alpaca, "is_configured", return_value=True):
                self.assertEqual(pt.enabled(), expected, (env, auto))

    def test_live_refusal_is_explained(self):
        with mock.patch.object(pt.config, "ALPACA_ENV", "live"), \
             mock.patch.object(pt.config, "ALPACA_AUTOTRADE", "paper"):
            self.assertIn("only runs on the paper account", pt.disabled_reason())

    def test_module_cannot_reach_live_server(self):
        src = inspect.getsource(pt)
        self.assertNotIn("://api.alpaca.markets", src)
        self.assertEqual(pt.PAPER_BASE, "https://paper-api.alpaca.markets")

    def test_oversized_entry_is_not_placed(self):
        acts = pt.plan_actions(snap(equity=1000.0), {"positions": []}, [buy_signal(shares=50)], [], [], strategy(), NOW)
        self.assertEqual([a["kind"] for a in acts], ["skip"])


class Planning(unittest.TestCase):
    def setUp(self):
        self.s = strategy()

    def plan(self, snapshot, book=None, new=(), pos_sigs=(), acct_sigs=()):
        return pt.plan_actions(snapshot, book or {"positions": []}, list(new), list(pos_sigs), list(acct_sigs), self.s, NOW)

    def test_buy_now_entry_limit_is_one_percent_above(self):
        a = self.plan(snap(), new=[buy_signal()])[0]
        self.assertEqual(a["kind"], "enter")
        self.assertAlmostEqual(a["limit"], 12.12)

    def test_wait_entry_uses_the_plans_limit(self):
        a = self.plan(snap(), new=[buy_signal(action="WAIT")])[0]
        self.assertAlmostEqual(a["limit"], 11.5)

    def test_no_duplicate_entries(self):
        self.assertEqual(self.plan(snap([held("F", 5, 60)]), new=[buy_signal()]), [])
        self.assertEqual(self.plan(snap(orders=[order("1", "F", "buy", "limit", 2)]), new=[buy_signal()]), [])

    def test_exit_cancels_parents_then_sells_held_shares(self):
        orders = [order("p", "F", "sell", "limit", 2, cls="bracket"),
                  order("c", "F", "sell", "stop", 2, stop=11, parent="p"),
                  order("s", "F", "sell", "stop", 3, stop=11)]
        a = self.plan(snap([held("F", 5, 60)], orders), pos_sigs=[{"ticker": "F", "action": "SELL"}])[0]
        self.assertEqual(a["kind"], "exit")
        self.assertEqual(a["cancel_ids"], ["p", "s"])     # legs go with their parent
        self.assertEqual(a["qty"], 5)

    def test_exit_before_fill_cancels_the_entry(self):
        a = self.plan(snap(orders=[order("b", "F", "buy", "limit", 5)]), pos_sigs=[{"ticker": "F", "action": "SELL"}])[0]
        self.assertEqual((a["kind"], a["ids"]), ("cancel", ["b"]))

    def test_stale_buy_now_entry_cancelled(self):
        p = open_position(ticker="F")
        old = (NOW - timedelta(days=4)).isoformat()
        a = self.plan(snap(orders=[order("b", "F", "buy", "limit", 5, cid="ag-x-b", created=old)]),
                      book={"positions": [p]})
        self.assertEqual(a[0]["kind"], "cancel")

    def test_pending_limit_buy_is_left_alone(self):
        p = open_position(ticker="F")
        p["status"] = "pending"
        old = (NOW - timedelta(days=6)).isoformat()
        self.assertEqual(self.plan(snap(orders=[order("b", "F", "buy", "limit", 5, cid="ag-x-b", created=old)]),
                                   book={"positions": [p]}), [])

    def test_stops_raised_patched_or_replaced_and_gaps_filled(self):
        p = open_position(ticker="F")
        p["stop"] = 100.0      # plan moved to breakeven
        orders = [order("c", "F", "sell", "stop", 4, stop=90.0, cls="bracket", parent="p"),
                  order("o", "F", "sell", "stop", 4, stop=90.0, cls="oto")]
        acts = self.plan(snap([held("F", 10, 1050)], orders), book={"positions": [p]})
        kinds = {(a["kind"], a.get("id")) for a in acts}
        self.assertIn(("raise_stop", "c"), kinds)            # bracket legs can be edited
        self.assertIn(("replace_stop", "o"), kinds)          # OTO stops can't: cancel + new
        add = [a for a in acts if a["kind"] == "add_stop"][0]
        self.assertEqual(add["qty"], 2)                       # 10 held, 8 covered

    def test_working_trade_switches_to_native_trailing_stop(self):
        p = open_position(ticker="F")
        p.update(trailing=True, stop=114.4)
        pos_ = {**held("F", 5, 650), "current_price": 130.0}
        acts = self.plan(snap([pos_], [order("s", "F", "sell", "stop", 5, stop=114.4, cls="oto")]),
                         book={"positions": [p]})
        self.assertEqual([(a["kind"], a["ids"], a["trail_pct"]) for a in acts], [("to_trailing", ["s"], 12.0)])

    def test_no_switch_after_a_pullback_keeps_fixed_stop_at_plan_level(self):
        p = open_position(ticker="F")
        p.update(trailing=True, stop=114.4)
        pos_ = {**held("F", 5, 600), "current_price": 120.0}      # trailing from 120 would start at 105.6
        acts = self.plan(snap([pos_], [order("s", "F", "sell", "stop", 5, stop=100.0, cls="oto")]),
                         book={"positions": [p]})
        self.assertEqual([a["kind"] for a in acts], ["replace_stop"])
        self.assertAlmostEqual(acts[0]["stop"], 114.4)

    def test_correct_trailing_order_needs_nothing(self):
        p = open_position(ticker="F")
        p.update(trailing=True, stop=114.4)
        trail = {**order("t", "F", "sell", "trailing_stop", 5, stop=118.0), "trail_percent": 12.0}
        self.assertEqual(self.plan(snap([{**held("F", 5, 650), "current_price": 130.0}], [trail]),
                                   book={"positions": [p]}), [])

    def test_untracked_holdings_are_not_touched(self):
        self.assertEqual(self.plan(snap([held("AAPL", 1, 200)])), [])


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = b"x"

    def json(self):
        return self._body


class Execution(unittest.TestCase):
    def run_actions(self, actions, status="canceled", fail_on=None):
        calls = []

        def fake(method, url, headers, json, timeout):
            calls.append((method, url, json))
            if fail_on and fail_on in url and method == "POST":
                return FakeResp(422, {"message": "insufficient qty"})
            if method == "GET":
                return FakeResp(200, {"status": status})
            return FakeResp(200, {"id": "new"})

        with mock.patch.object(pt.requests, "request", side_effect=fake), \
             mock.patch.object(pt.time, "sleep"):
            results = pt.execute(actions)
        return results, calls

    def test_entry_is_one_oto_order_for_all_shares_on_paper_server(self):
        a = {"kind": "enter", "symbol": "F", "shares": 5, "limit": 12.12, "stop": 11.0, "pid": "2026-11-23-F"}
        results, calls = self.run_actions([a])
        self.assertTrue(results[0]["ok"])
        self.assertEqual(len(calls), 1)
        method, url, body = calls[0]
        self.assertTrue(url.startswith("https://paper-api.alpaca.markets/"))
        self.assertEqual((body["order_class"], body["qty"], body["time_in_force"]), ("oto", "5", "gtc"))
        self.assertEqual(body["stop_loss"], {"stop_price": "11.00"})
        self.assertNotIn("take_profit", body)                 # no profit target
        self.assertEqual(body["client_order_id"], "ag-2026-11-23-F-o")

    def test_switch_to_trailing_cancels_then_places_trailing_stop(self):
        a = {"kind": "to_trailing", "symbol": "F", "ids": ["s1"], "qty": 5, "trail_pct": 9.0}
        results, calls = self.run_actions([a])
        self.assertEqual([m for m, _, _ in calls], ["DELETE", "GET", "POST"])
        body = calls[-1][2]
        self.assertEqual((body["type"], body["trail_percent"], body["time_in_force"], body["qty"]),
                         ("trailing_stop", "9", "gtc", "5"))
        self.assertTrue(results[0]["ok"])

    def test_tighten_trail_patches_the_order(self):
        _, calls = self.run_actions([{"kind": "tighten_trail", "symbol": "F", "id": "t1", "trail_pct": 9.0}])
        self.assertEqual(calls[0][0], "PATCH")
        self.assertEqual(calls[0][2], {"trail": "9"})

    def test_single_share_entry(self):
        a = {"kind": "enter", "symbol": "F", "shares": 1, "limit": 12.0, "stop": 11.0, "pid": "x"}
        _, calls = self.run_actions([a])
        self.assertEqual(len(calls), 1)

    def test_exit_waits_for_cancels_before_selling(self):
        a = {"kind": "exit", "symbol": "F", "qty": 5, "cancel_ids": ["p"], "why": ""}
        results, calls = self.run_actions([a])
        methods = [m for m, _, _ in calls]
        self.assertEqual(methods, ["DELETE", "GET", "POST"])
        self.assertEqual(calls[-1][2]["type"], "market")
        self.assertTrue(results[0]["ok"])

    def test_failure_is_reported_not_raised(self):
        a = {"kind": "add_stop", "symbol": "F", "qty": 2, "stop": 11.0}
        results, _ = self.run_actions([a], fail_on="/v2/orders")
        self.assertFalse(results[0]["ok"])
        self.assertIn("insufficient qty", results[0]["detail"])


class Performance(unittest.TestCase):
    def test_tracks_return_vs_spy_from_first_run(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(pt, "PERF_PATH", Path(d) / "perf.json"):
            pt.record_performance({"equity": 1000.0}, 500.0, "2026-11-23")
            out = pt.record_performance({"equity": 1030.0}, 505.0, "2026-12-01")
        self.assertEqual((out["return_pct"], out["spy_return_pct"], out["days"]), (3.0, 1.0, 8))


if __name__ == "__main__":
    unittest.main()
