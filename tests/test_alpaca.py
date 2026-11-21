import inspect
import unittest
from unittest import mock

from src.analysis import account as acct
from src.analysis import positions as pos
from src.fetchers import broker_alpaca
from src.report import advice, dashboard
from tests.helpers import open_position, strategy

KEY_ID, SECRET = "PKTESTKEYID123", "SECRETVALUE456"


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


ACCOUNT = {"status": "ACTIVE", "equity": "10000", "last_equity": "9900", "cash": "4000",
           "buying_power": "8000", "long_market_value": "6000", "multiplier": "2", "shorting_enabled": True}
POSITIONS = [{"symbol": "NVDA", "qty": "10", "side": "long", "asset_class": "us_equity",
              "avg_entry_price": "100", "current_price": "120", "market_value": "1200",
              "cost_basis": "1000", "unrealized_plpc": "0.2"}]
ORDERS = [{"id": "p1", "symbol": "NVDA", "side": "sell", "type": "limit", "order_class": "oco",
           "time_in_force": "gtc", "qty": "5", "limit_price": "130", "stop_price": None,
           "legs": [{"id": "c1", "symbol": "NVDA", "side": "sell", "type": "stop", "time_in_force": "gtc",
                     "qty": "5", "stop_price": "90"}]}]


def fake_get(url, headers, params, timeout):
    if url.endswith("/v2/account"):
        return FakeResp(200, ACCOUNT)
    if url.endswith("/v2/positions"):
        return FakeResp(200, POSITIONS)
    if url.endswith("/v2/orders"):
        assert params.get("status") == "open" and params.get("nested") == "true"
        return FakeResp(200, ORDERS)
    raise AssertionError(url)


class Client(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(broker_alpaca.config, "ALPACA_API_KEY_ID", KEY_ID),
                  mock.patch.object(broker_alpaca.config, "ALPACA_API_SECRET_KEY", SECRET),
                  mock.patch.object(broker_alpaca.config, "ALPACA_ENV", "paper")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_snapshot_parses_and_flattens_oco_legs(self):
        with mock.patch.object(broker_alpaca.requests, "get", side_effect=fake_get) as g:
            snap = broker_alpaca.get_snapshot()
        self.assertTrue(g.call_args.args[0].startswith("https://paper-api.alpaca.markets"))
        self.assertEqual(g.call_args.kwargs["headers"]["APCA-API-KEY-ID"], KEY_ID)
        self.assertEqual(snap["equity"], 10000.0)
        self.assertEqual(snap["positions"][0]["qty"], 10.0)
        self.assertEqual(len(snap["orders"]), 2)          # parent + stop leg
        self.assertEqual(snap["orders"][1]["stop_price"], 90.0)

    def test_live_env_uses_live_host(self):
        with mock.patch.object(broker_alpaca.config, "ALPACA_ENV", "live"), \
             mock.patch.object(broker_alpaca.requests, "get", side_effect=fake_get) as g:
            broker_alpaca.get_snapshot()
        self.assertTrue(g.call_args.args[0].startswith("https://api.alpaca.markets"))

    def test_rejected_key_explains_paper_vs_live_and_hides_secrets(self):
        with mock.patch.object(broker_alpaca.requests, "get", return_value=FakeResp(401, {})):
            with self.assertRaises(broker_alpaca.AlpacaError) as cm:
                broker_alpaca.get_snapshot()
        msg = str(cm.exception)
        self.assertIn("ALPACA_ENV", msg)
        self.assertNotIn(SECRET, msg)
        self.assertNotIn(KEY_ID, msg)

    def test_network_error_hides_secrets(self):
        err = broker_alpaca.requests.ConnectionError(f"boom {SECRET}")
        with mock.patch.object(broker_alpaca.requests, "get", side_effect=err):
            with self.assertRaises(broker_alpaca.AlpacaError) as cm:
                broker_alpaca.get_snapshot()
        self.assertNotIn(SECRET, str(cm.exception))

    def test_client_is_read_only(self):
        src = inspect.getsource(broker_alpaca)
        for verb in (".post(", ".put(", ".patch(", ".delete(", ".request("):
            self.assertNotIn(verb, src, f"broker client must not contain {verb}")
        with mock.patch.object(broker_alpaca.requests, "get", side_effect=fake_get), \
             mock.patch.object(broker_alpaca.requests, "post") as post, \
             mock.patch.object(broker_alpaca.requests, "delete") as delete:
            broker_alpaca.get_snapshot()
        post.assert_not_called()
        delete.assert_not_called()

    def test_margin_settings_are_read_and_flagged(self):
        with mock.patch.object(broker_alpaca.requests, "get", side_effect=fake_get):
            snap = broker_alpaca.get_snapshot()
        self.assertEqual(snap["multiplier"], 2.0)
        warning = acct.margin_warning(snap)
        self.assertIn("multiplier to 1", warning)
        self.assertIn("short selling", warning)
        self.assertIsNone(acct.margin_warning({"multiplier": 1.0, "shorting_enabled": False}))
        # Under $2,000 Alpaca blocks shorting regardless of the flag.
        self.assertIsNone(acct.margin_warning({"multiplier": 1.0, "shorting_enabled": True, "equity": 1500.0}))

    def test_trailing_stop_effective_level(self):
        o = broker_alpaca._order({"symbol": "x", "side": "sell", "type": "trailing_stop",
                                  "trail_percent": "10", "hwm": "200", "stop_price": None})
        self.assertAlmostEqual(o["stop_price"], 180.0)


def snapshot(positions=(), orders=(), equity=10000.0, env="live"):
    return {"env": env, "as_of": "now", "equity": equity, "last_equity": equity, "cash": 4000.0,
            "positions": list(positions), "orders": list(orders)}


def held(sym, qty, value):
    return {"symbol": sym, "qty": qty, "side": "long", "market_value": value,
            "current_price": value / qty, "avg_entry_price": value / qty, "unrealized_plpc": 0.0}


def stop(sym, qty, price, tif="gtc", kind="stop"):
    return {"symbol": sym, "side": "sell", "type": kind, "qty": qty, "stop_price": price, "time_in_force": tif}


class Sizing(unittest.TestCase):
    def test_rounds_down_to_whole_shares(self):
        self.assertEqual(acct.share_plan(7.0, 120.0, 10000, 10)[:2], (5, 600.0))

    def test_one_share_allowed_inside_cap(self):
        shares, dollars, note = acct.share_plan(2.0, 900.0, 10000, 10)
        self.assertEqual(shares, 1)
        self.assertIn("inside the per-position limit", note)

    def test_one_share_refused_above_cap(self):
        shares, _, note = acct.share_plan(7.0, 1500.0, 10000, 10)
        self.assertEqual(shares, 0)
        self.assertIn("more than the 10.0%", note)



class Checks(unittest.TestCase):
    def setUp(self):
        self.p = open_position(ticker="NVDA")         # stop 90, target 125
        self.book = {"positions": [self.p]}

    def check(self, positions, orders):
        return acct.check_account(snapshot(positions, orders), self.book)

    def test_fully_protected(self):
        r = self.check([held("NVDA", 10, 1200)], [stop("NVDA", 5, 90.0), stop("NVDA", 5, 90.0)])
        self.assertEqual(r[0]["action"], "PROTECTED")

    def test_missing_stop(self):
        r = self.check([held("NVDA", 10, 1200)], [])
        self.assertEqual(r[0]["action"], "NO STOP")
        self.assertIn("$90.00", r[0]["detail"])

    def test_partial_stop(self):
        r = self.check([held("NVDA", 10, 1200)], [stop("NVDA", 5, 90.0)])
        self.assertEqual(r[0]["action"], "PARTIAL STOP")

    def test_stop_below_raised_plan_stop(self):
        self.p["stop"] = 100.0   # breakeven move after +1R
        r = self.check([held("NVDA", 10, 1200)], [stop("NVDA", 10, 90.0)])
        self.assertEqual(r[0]["action"], "RAISE STOP")
        self.assertIn("$100.00", r[0]["detail"])

    def test_day_stop_on_fraction_explains_alpaca_rule(self):
        r = self.check([held("NVDA", 2.5, 300)], [stop("NVDA", 2.5, 90.0, tif="day")])
        self.assertEqual(r[0]["action"], "STOP EXPIRES")
        self.assertIn("fractional", r[0]["detail"])

    def test_plan_exited_but_still_held(self):
        self.p.update(status="closed", closed_on="2026-01-05", close_reason="stop-loss")
        r = self.check([held("NVDA", 10, 900)], [])
        self.assertEqual(r[0]["action"], "SELL")

    def test_untracked_and_not_bought(self):
        r = self.check([held("AAPL", 3, 600)], [])
        actions = {x["ticker"]: x["action"] for x in r}
        self.assertEqual(actions, {"AAPL": "UNTRACKED", "NVDA": "NOT BOUGHT"})

    def test_worst_problem_first(self):
        other = open_position(ticker="MSFT")
        self.book["positions"].append(other)
        r = self.check([held("NVDA", 10, 1200), held("MSFT", 2, 800)], [stop("NVDA", 10, 90.0)])
        self.assertEqual([x["action"] for x in r], ["NO STOP", "PROTECTED"])


class LiveSizingInOpenNewIdeas(unittest.TestCase):
    def idea(self, t):
        return {"ticker": t, "category": "growth_momentum", "setup": "momentum_trend", "horizon": "medium",
                "conviction": 4, "thesis": "x", "fundamentals": {},
                "technicals": {"atr_pct": 0.03, "rsi14": 55, "sma20": 98.0, "trend": "up",
                               "day_change": 0.0, "source": "history"}}

    def run_ideas(self, account, price=100.0, ideas=("AAA",)):
        book = {"positions": []}
        policy = {"blocked_setups": {}, "setup_size_factor": {}, "conviction_factors": {}}
        sigs = pos.open_new_ideas(book, [self.idea(t) for t in ideas], "2026-01-10", strategy(), policy, {},
                                  lambda t: {"price": price}, 500.0, account=account)
        return book, sigs

    def test_buy_gets_whole_shares_and_one_stop_for_all(self):
        _, sigs = self.run_ideas(snapshot(equity=10000.0))
        s = sigs[0]
        self.assertEqual(s["action"], "BUY")
        self.assertEqual(s["shares"], 8)                 # 0.75% / 7.5% stop x 0.8 -> 8% of $10k -> 8 sh
        self.assertIn("GTC stop order for all 8 shares", s["detail"])
        self.assertIn("No profit target", s["detail"])

    def test_live_holdings_count_against_category_limit(self):
        s = strategy()
        account = snapshot([held("OLD", 10, 5400)], equity=10000.0)   # 54% held
        book = {"positions": [dict(open_position(ticker="OLD"), category="growth_momentum")]}
        policy = {"blocked_setups": {}, "setup_size_factor": {}, "conviction_factors": {}}
        sigs = pos.open_new_ideas(book, [self.idea("NEW")], "2026-01-10", s, policy, {},
                                  lambda t: {"price": 100.0}, 500.0, account=account)
        self.assertEqual(sigs[0]["action"], "PASS")           # growth cap is 55%
        self.assertIn("limit", sigs[0]["detail"])

    def test_existing_holding_counts_toward_position_cap(self):
        book = {"positions": []}
        policy = {"blocked_setups": {}, "setup_size_factor": {}, "conviction_factors": {}}
        full = snapshot([held("AAA", 5, 500)], equity=1000.0)            # already 50%
        sigs = pos.open_new_ideas(book, [self.idea("AAA")], "2026-01-10", strategy(), policy, {},
                                  lambda t: {"price": 100.0}, 500.0, account=full)
        self.assertEqual(sigs[0]["action"], "PASS")
        self.assertIn("already hold", sigs[0]["detail"])

        partial = snapshot([held("AAA", 3, 300)], equity=10000.0)        # 3%, room for 7% more
        sigs = pos.open_new_ideas({"positions": []}, [self.idea("AAA")], "2026-01-10", strategy(), policy, {},
                                  lambda t: {"price": 100.0}, 500.0, account=partial)
        self.assertEqual(sigs[0]["action"], "BUY")
        self.assertLessEqual(sigs[0]["shares"] * 100.0 + 300, 1000.0)      # total stays within 10%
        self.assertIn("adds to the 3 shares", sigs[0]["detail"])

    def test_unaffordable_share_passes(self):
        _, sigs = self.run_ideas(snapshot(equity=2000.0), price=900.0)
        self.assertEqual(sigs[0]["action"], "PASS")

    def test_without_account_sizes_stay_percent(self):
        _, sigs = self.run_ideas(None)
        self.assertIsNone(sigs[0]["shares"])


class DashboardAccounts(unittest.TestCase):
    def test_paper_account_never_counts_toward_net_worth(self):
        pf = {"accounts": [{"name": "Bank", "cash": 1000}], "holdings": []}
        with mock.patch.object(dashboard, "_load_portfolio_frontmatter", return_value=pf):
            st = dashboard._compute_portfolio_state(snapshot([held("NVDA", 10, 1200)], env="paper", equity=100000.0))
        self.assertEqual(st["net_worth"], 1000.0)
        self.assertEqual(st["paper_account"]["holdings"][0]["ticker"], "NVDA")

    def test_live_account_replaces_manual_alpaca_entry(self):
        pf = {"accounts": [{"name": "Bank", "cash": 1000}, {"name": "Alpaca", "cash": 999}],
              "holdings": [{"ticker": "OLD", "shares": 1, "account": "Alpaca"}]}
        with mock.patch.object(dashboard, "_load_portfolio_frontmatter", return_value=pf):
            st = dashboard._compute_portfolio_state(snapshot([held("NVDA", 10, 1200)], env="live"))
        # Bank 1000 + Alpaca cash 4000 + NVDA 1200; the stale manual Alpaca rows are dropped.
        self.assertEqual(st["net_worth"], 6200.0)
        self.assertEqual([h["ticker"] for h in st["holdings"]], ["NVDA"])

    def test_account_section_renders(self):
        md = advice.action_plan([], [], set(), account=snapshot([held("NVDA", 10, 1200)], env="paper"),
                                account_signals=[{"ticker": "NVDA", "action": "NO STOP", "detail": "place one",
                                                  "qty": 10, "market_value": 1200, "stop_order": None,
                                                  "plan_stop": 90.0}])
        self.assertIn("PAPER account", md)
        self.assertIn("**NO STOP**", md)


if __name__ == "__main__":
    unittest.main()
