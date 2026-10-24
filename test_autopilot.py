#!/usr/bin/env python3
"""ARMM v0.4 tests — the demo lock, attribution, new vetoes, and the scoreboard.

NO NETWORK, NO ORDERS. Acceptance test 1 lives here: the proof that autopilot is
structurally incapable of reaching the live order path.
"""
import ast
import logging
import os
import tempfile
import unittest

_TMP_HOME = tempfile.mkdtemp(prefix="armm-autotest-")
os.environ.setdefault("ARMM_HOME", _TMP_HOME)

import advisor
import autopilot
import config
import guards
import manager
import origin as origin_mod
import scoreboard
import signals
import starvation
import store as store_mod
import weex_client

import ladder as _ladder
_ladder._pause = lambda seconds: None          # no real waiting in tests

_armm_log = logging.getLogger("armm")
_armm_log.handlers.clear()
_armm_log.addHandler(logging.NullHandler())
_armm_log.setLevel(logging.CRITICAL)

_REAL_HOME = os.path.expanduser("~/armm")
assert config.ARMM_HOME != _REAL_HOME, "tests must not run against the real ~/armm"
assert config.ARMM_HOME.startswith(tempfile.gettempdir())

HERE = os.path.dirname(os.path.abspath(__file__))


# =====================================================================
# ACCEPTANCE TEST 1 — the demo lock
# =====================================================================

class FakeDemoClient:
    demo = True
    mode = "DEMO"
    order_path = weex_client.DEMO_ORDER_PATH

    def __init__(self):
        self.placed = []

    def _sym(self, s):
        return s.replace("USDT", "SUSDT")

    def routing_line(self, s):
        return f"routing: {self.order_path}  (DEMO) | exchange symbol: {self._sym(s)}"

    def place_order(self, symbol, side, position_side, qty, price=None, tif=None,
                    client_id=None, reduce_only=None, **kw):
        self.placed.append({"client_id": client_id, "symbol": symbol, "qty": qty,
                            "price": price})
        return {"orderId": f"oid-{len(self.placed)}"}

    def find_order(self, client_id, symbol=None, limit=100):
        return None                      # resting: sim history lists terminal only

    def open_positions(self):
        return []

    def last_price(self, symbol):
        return 78000.0

    def book_ticker(self, symbol):
        return {"bid": 77999.9, "ask": 78000.1, "bid_qty": 1.0, "ask_qty": 1.0}

    def equity(self):
        return 42000.0

    def exchange_info(self, symbol=None):
        return {"symbols": [{"symbol": "BTCUSDT", "quantityPrecision": 4,
                             "pricePrecision": 1, "minOrderSize": 0.0001,
                             "maxOrderSize": 1200, "maxLeverage": 400,
                             "makerFeeRate": 0.0002, "takerFeeRate": 0.0008,
                             "buyLimitPriceRatio": 0.01, "sellLimitPriceRatio": 0.01,
                             "contractVal": 0.0001}]}

    def set_leverage(self, *a, **k):
        raise weex_client.WeexError("demo mode exposes no leverage endpoint")


class FakeLiveClient(FakeDemoClient):
    demo = False
    mode = "LIVE"
    order_path = weex_client.LIVE_ORDER_PATH


class TestDemoLock(unittest.TestCase):
    """AT1: autopilot cannot reach the live order path — structurally."""

    def test_autopilot_source_never_names_the_live_order_path(self):
        with open(os.path.join(HERE, "autopilot.py")) as f:
            src = f.read()
        self.assertNotIn("LIVE_ORDER_PATH", src)
        self.assertNotIn("/capi/v3/order", src)

    def test_autopilot_never_imports_the_live_path_constant(self):
        with open(os.path.join(HERE, "autopilot.py")) as f:
            tree = ast.parse(f.read(), "autopilot.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "LIVE_ORDER_PATH")

    def test_autopilot_constructs_its_client_with_a_literal_true(self):
        """The demo flag must be written at the call site, not read from config —
        otherwise 'demo' becomes something a flag could flip."""
        with open(os.path.join(HERE, "autopilot.py")) as f:
            tree = ast.parse(f.read(), "autopilot.py")
        literals = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "WeexClient"):
                for kw in node.keywords:
                    if kw.arg == "demo":
                        literals.append(kw.value)
        self.assertTrue(literals, "autopilot must construct a WeexClient itself")
        for lit in literals:
            self.assertIsInstance(lit, ast.Constant, "demo= must be a literal")
            self.assertIs(lit.value, True, "demo= must be the literal True")

    def test_constructing_autopilot_with_a_live_client_raises(self):
        with self.assertRaises(autopilot.AutopilotSafetyError):
            autopilot.Autopilot(store=store_mod.Store(":memory:"),
                                client=FakeLiveClient())

    def test_demo_client_helper_rejects_a_live_client(self):
        with self.assertRaises(autopilot.AutopilotSafetyError):
            autopilot.demo_client(FakeLiveClient())

    def test_assert_demo_rejects_every_way_of_being_live(self):
        class NotDemo(FakeDemoClient):
            demo = False

        class WrongPath(FakeDemoClient):
            order_path = "/capi/v3/order"

        class WrongMode(FakeDemoClient):
            mode = "LIVE"

        for cls in (NotDemo, WrongPath, WrongMode):
            with self.assertRaises(autopilot.AutopilotSafetyError, msg=cls.__name__):
                autopilot.assert_demo(cls())
        with self.assertRaises(autopilot.AutopilotSafetyError):
            autopilot.assert_demo(None)

    def test_a_client_that_turns_live_after_construction_is_caught(self):
        """The pre-order re-check exists because a client object is mutable."""
        c = FakeDemoClient()
        auto = autopilot.Autopilot(store=store_mod.Store(":memory:"), client=c)
        c.demo = False                                     # sabotage
        with self.assertRaises(autopilot.AutopilotSafetyError):
            autopilot.assert_demo(auto.client, "post-construction")

    def test_autopilot_has_no_live_or_force_parameter(self):
        with open(os.path.join(HERE, "autopilot.py")) as f:
            tree = ast.parse(f.read(), "autopilot.py")
        banned = {"live", "force", "allow_live", "real", "production"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = {a.arg for a in node.args.args + node.args.kwonlyargs}
                self.assertEqual(names & banned, set(),
                                 f"{node.name} exposes a live/force parameter")

    def test_manager_does_not_build_autopilot_in_live_mode(self):
        m = manager.Manager(demo=False, client=FakeLiveClient(),
                            store=store_mod.Store(":memory:"), notifier=SilentNotifier())
        self.assertIsNone(m.autopilot, "live manager must have no autopilot")

    def test_manager_builds_autopilot_in_demo_mode(self):
        m = manager.Manager(demo=True, client=FakeDemoClient(),
                            store=store_mod.Store(":memory:"), notifier=SilentNotifier())
        self.assertIsNotNone(m.autopilot)
        self.assertTrue(m.autopilot.client.demo)


class SilentNotifier:
    def __init__(self):
        self.sent = []

    def alert(self, text):
        self.sent.append(text)

    def send(self, text):
        self.sent.append(text)


# =====================================================================
# AT2 / AT3 / AT4 — autopilot behaviour
# =====================================================================

def confirming_snapshot(setup="B", **over):
    s = signals.Snapshot(symbol="BTCUSDT", price=78000.0, vwap=77500.0,
                         vwap_lower=77000.0, vwap_upper=78500.0, vwap_sigma=500.0,
                         rsi=55.0, rsi_rising=True, macd_hist=1.0,
                         macd_hist_expanding=True, trend_up=True,
                         regime=signals.REGIME_UP, regime_detail="test",
                         confirm_30m=True, rsi_30m=45.0)
    s.setup_b = (setup == "B")
    s.setup_a = (setup == "A")
    s.setup_b_core = s.setup_b
    s.setup_a_core = s.setup_a
    for k, v in over.items():
        setattr(s, k, v)
    return s


class AutopilotHarness(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.Store(":memory:")
        self.notify = SilentNotifier()
        self.client = FakeDemoClient()
        self.auto = autopilot.Autopilot(store=self.store, notify=self.notify,
                                        client=self.client)
        self.auto.enable("BTCUSDT")
        # Explicitly wipe guard state: test_armm's reconcile tests record a
        # BTCUSDT close, and the resulting 30m cooldown would silently refuse
        # every autopilot entry here when the suites run together.
        guards.reset_state(os.path.join(_TMP_HOME, "guard_state.json"))


class TestAutopilotEntry(AutopilotHarness):
    """AT2: a confirmed setup places exactly one demo ladder, correctly labelled."""

    def test_places_one_ladder_with_an_auto_client_id(self):
        r = self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(r["action"], "entered", r)
        self.assertEqual(len(self.client.placed), 1)
        cid = self.client.placed[0]["client_id"]
        self.assertEqual(origin_mod.parse(cid), origin_mod.AUTO, cid)
        self.assertTrue(cid.startswith("armm-auto-"), cid)

    def test_routes_to_the_simulated_venue(self):
        self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(self.client.order_path, weex_client.DEMO_ORDER_PATH)

    def test_announces_the_setup_and_its_justification(self):
        self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        blob = "\n".join(self.notify.sent)
        self.assertIn("AUTOPILOT entering", blob)
        self.assertIn("setup B confirmed", blob)
        self.assertIn("Justification", blob)
        self.assertIn("AUTOPILOT", blob)

    def test_does_not_re_fire_on_the_same_activation(self):
        first = self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(first["action"], "entered")
        # The position is still open, and the setup is still true.
        second = self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(second["action"], "skipped")
        self.assertEqual(len(self.client.placed), 1, "must not double-enter")

    def test_rate_limited_to_one_entry_per_symbol_per_4h(self):
        self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        # Close the position and clear the edge so only the 4h gap can block it.
        for p in self.store.active_positions():
            self.store.update_position(p["id"], state=store_mod.CLOSED)
        self.store.save_setup_state("BTCUSDT", "B", False, 0)
        r = self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(r["reason"], "rate_limited", r)
        self.assertEqual(len(self.client.placed), 1)

    def test_re_arms_after_the_gap_and_a_fresh_activation(self):
        import time as _t
        now = _t.time()
        self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"), now=now)
        for p in self.store.active_positions():
            self.store.update_position(p["id"], state=store_mod.CLOSED)
        self.store.save_setup_state("BTCUSDT", "B", False, 0)
        later = now + autopilot.PER_SYMBOL_MIN_GAP_SECONDS + 60
        r = self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"), now=later)
        self.assertEqual(r["action"], "entered", r)
        self.assertEqual(len(self.client.placed), 2)

    def test_no_setup_means_no_order(self):
        s = confirming_snapshot("B")
        s.setup_a = s.setup_b = s.setup_a_core = s.setup_b_core = False
        r = self.auto.run_symbol("BTCUSDT", snapshot=s)
        self.assertEqual(r["reason"], "no_setup")
        self.assertEqual(self.client.placed, [])

    def test_will_not_open_a_second_position_in_the_same_symbol(self):
        self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.store.save_setup_state("BTCUSDT", "B", False, 0)
        self.store.record_auto_entry("BTCUSDT", 0)          # clear the 4h gap
        r = self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(r["reason"], "position_open", r)


class TestAutopilotHasNoOverride(AutopilotHarness):
    """AT3: any advisor objection ends the attempt. There is no override path.

    Two realistic objections are used:
      * leverage so high the stop sits behind liquidation (the demo account
        really does run at ~141x — found in v0.2), and
      * a broken market regime.
    """

    def _leveraged_autopilot(self, venue_leverage=141):
        """The guards cap what we ASK for at 5x, so a dangerous stop can only
        arise from the ACCOUNT's own leverage — which is exactly the 141x the
        demo account was found running at in v0.2."""
        self.store.set_meta("venue_leverage:BTCUSDT", venue_leverage)
        return autopilot.Autopilot(store=self.store, notify=self.notify,
                                   client=self.client)

    def test_a_stop_behind_liquidation_refuses_the_trade(self):
        auto = self._leveraged_autopilot()
        r = auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(r["action"], "skipped", r)
        self.assertEqual(r["reason"], "advisor_refused", r)
        self.assertIn("BEHIND LIQUIDATION", r["detail"])
        self.assertEqual(self.client.placed, [], "no order may be placed")

    def test_a_sane_venue_leverage_does_not_block(self):
        self.store.set_meta("venue_leverage:BTCUSDT", 5)
        auto = autopilot.Autopilot(store=self.store, notify=self.notify,
                                   client=self.client)
        r = auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(r["action"], "entered", r)

    def test_the_venue_leverage_overrides_the_requested_one(self):
        snap = signals.Snapshot(symbol="BTCUSDT", price=78000.0, rsi=50.0,
                                trend_up=True, macd_hist=1.0, vwap=78000.0,
                                vwap_upper=79000.0, vwap_lower=77000.0)
        safe = advisor.review("BTCUSDT", "LONG", entry_price=78000.0, snapshot=snap,
                              stop_price=78000 * 0.99, leverage=5)
        self.assertFalse(any("BEHIND LIQUIDATION" in c for c in safe.contradictions))
        risky = advisor.review("BTCUSDT", "LONG", entry_price=78000.0, snapshot=snap,
                               stop_price=78000 * 0.99, leverage=5, venue_leverage=141)
        self.assertTrue(any("BEHIND LIQUIDATION" in c for c in risky.contradictions))
        self.assertTrue(any("141x on the venue" in c for c in risky.contradictions))

    def test_an_unverified_venue_leverage_is_stated_as_such(self):
        snap = signals.Snapshot(symbol="BTCUSDT", price=78000.0, rsi=50.0,
                                trend_up=True, macd_hist=1.0, vwap=78000.0,
                                vwap_upper=79000.0, vwap_lower=77000.0)
        adv = advisor.review("BTCUSDT", "LONG", entry_price=78000.0, snapshot=snap,
                             stop_price=78000 * 0.99, leverage=5)
        self.assertTrue(any("unverified" in n for n in adv.notes), adv.notes)

    def test_a_broken_regime_refuses_the_trade(self):
        s = confirming_snapshot("A", regime=signals.REGIME_DOWN,
                                regime_detail="EMA20 < EMA50 — structure broken")
        r = self.auto.run_symbol("BTCUSDT", snapshot=s)
        self.assertEqual(r["reason"], "advisor_refused", r)
        self.assertEqual(self.client.placed, [])

    def test_the_refusal_is_reported_not_swallowed(self):
        self._leveraged_autopilot().run_symbol(
            "BTCUSDT", snapshot=confirming_snapshot("B"))
        blob = "\n".join(self.notify.sent)
        self.assertIn("stood down", blob)
        self.assertIn("no override", blob)

    def test_the_refusal_is_recorded_in_the_database(self):
        self._leveraged_autopilot().run_symbol(
            "BTCUSDT", snapshot=confirming_snapshot("B"))
        kinds = [e["kind"] for e in self.store.recent_events(10)]
        self.assertIn("auto_refused", kinds)

    def test_place_plan_is_never_reached_when_the_advisor_objects(self):
        self._leveraged_autopilot().run_symbol(
            "BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(self.store.active_positions(), [],
                         "a refused attempt must not create a position row")

    def test_autopilot_exposes_no_way_to_proceed_past_an_objection(self):
        """There is deliberately no 'override' parameter anywhere in the module."""
        with open(os.path.join(HERE, "autopilot.py")) as f:
            src = f.read()
        for word in ("override=", "ignore_advisor", "skip_advisor", "confirm_anyway"):
            self.assertNotIn(word, src)


class TestStarvationIntegration(TestAutopilotHasNoOverride):
    """v0.5 regression: replay the September 2026 month-long silent block.

    The real incident: 335 consecutive Setup B refusals between 2026-09-05 and
    2026-10-03, every one "stop behind liquidation" because the demo account sat
    at 141x. Zero entries, four weekly digests reporting "no trades", and no
    alarm anywhere. These tests fail on any build that could repeat that.
    """

    def _starve(self, n, snapshot=None, start=1_000.0):
        auto = self._leveraged_autopilot()
        snapshot = snapshot if snapshot is not None else confirming_snapshot("B")
        for i in range(n):
            auto.run_symbol("BTCUSDT", snapshot=snapshot, now=start + i * 300.0)
        return auto

    def test_a_long_run_of_refusals_raises_a_starvation_alarm(self):
        self._starve(starvation.STREAK_THRESHOLD)
        blob = "\n".join(self.notify.sent)
        self.assertIn("STARVED", blob,
                      "a month of identical refusals must escalate, not stay quiet")

    def test_the_alarm_names_the_real_cause(self):
        self._starve(starvation.STREAK_THRESHOLD)
        alarm = [m for m in self.notify.sent if "STARVED" in m][0]
        self.assertIn("141x", alarm)

    def test_one_or_two_refusals_do_not_alarm(self):
        """A guard declining a marginal entry is the system working."""
        self._starve(2)
        self.assertNotIn("STARVED", "\n".join(self.notify.sent))

    def test_a_quiet_market_never_alarms(self):
        """The false-positive that would render the alarm useless."""
        quiet = confirming_snapshot("B")
        quiet.setup_a = quiet.setup_b = quiet.setup_a_core = quiet.setup_b_core = False
        auto = self._leveraged_autopilot()
        for i in range(starvation.STREAK_THRESHOLD * 10):
            r = auto.run_symbol("BTCUSDT", snapshot=quiet, now=1_000.0 + i * 300.0)
            self.assertEqual(r["reason"], "no_setup", r)
        self.assertNotIn("STARVED", "\n".join(self.notify.sent))

    def test_the_alarm_does_not_repeat_every_cycle(self):
        self._starve(starvation.STREAK_THRESHOLD * 5)
        fired = [m for m in self.notify.sent if "STARVED" in m]
        self.assertEqual(len(fired), 1, "one alarm per day, not one per poll")

    def test_auto_status_explains_the_block_instead_of_just_saying_ON(self):
        """'/auto status' said ON for a month while nothing could trade."""
        auto = self._starve(starvation.STREAK_THRESHOLD)
        lines = "\n".join(auto.status_lines())
        self.assertIn("blocked", lines.lower())
        self.assertIn("141x", lines)

    def test_auto_status_is_unchanged_when_healthy(self):
        lines = "\n".join(self.auto.status_lines())
        self.assertNotIn("blocked", lines.lower())

    def test_a_successful_entry_clears_the_starvation_state(self):
        self._starve(starvation.STREAK_THRESHOLD)
        self.store.set_meta("venue_leverage:BTCUSDT", 5)
        healthy = autopilot.Autopilot(store=self.store, notify=self.notify,
                                      client=self.client)
        r = healthy.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"),
                               now=100_000.0)
        self.assertEqual(r["action"], "entered", r)
        self.assertEqual(
            starvation.Tracker(store=self.store).state("BTCUSDT")["streak"], 0)


class TestAutopilotHalt(AutopilotHarness):
    """AT4: /halt stops autopilot; restart does not resurrect it."""

    def test_halt_stops_it_mid_cycle(self):
        guards.halt("test halt")
        try:
            r = self.auto.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
            self.assertEqual(r["reason"], "halted")
            self.assertEqual(self.client.placed, [])
        finally:
            guards.resume()

    def test_manager_run_autopilot_respects_a_halt_raised_mid_pass(self):
        m = manager.Manager(demo=True, client=self.client, store=self.store,
                            notifier=self.notify)
        m.autopilot = self.auto
        self.store.set_auto("BTCUSDT", True)
        guards.halt("mid-pass halt")
        try:
            results = m.run_autopilot()
            self.assertTrue(all(r["action"] == "skipped" for r in results), results)
            self.assertEqual(self.client.placed, [])
        finally:
            guards.resume()

    def test_auto_state_is_persisted_and_survives_a_restart(self):
        self.assertEqual(self.store.auto_symbols(), ["BTCUSDT"])
        fresh = autopilot.Autopilot(store=self.store, client=FakeDemoClient())
        self.assertTrue(fresh.is_enabled("BTCUSDT"))

    def test_disabled_state_is_persisted_too(self):
        self.auto.disable("BTCUSDT")
        fresh = autopilot.Autopilot(store=self.store, client=FakeDemoClient())
        self.assertFalse(fresh.is_enabled("BTCUSDT"))
        self.assertEqual(fresh.enabled_symbols(), [])

    def test_a_restart_with_no_persisted_auto_on_stays_off(self):
        empty = store_mod.Store(":memory:")
        fresh = autopilot.Autopilot(store=empty, client=FakeDemoClient())
        self.assertEqual(fresh.enabled_symbols(), [])
        r = fresh.run_symbol("BTCUSDT", snapshot=confirming_snapshot("B"))
        self.assertEqual(r["reason"], "disabled")

    def test_safety_trip_disables_everything_and_halts(self):
        self.auto._emergency_halt("simulated routing anomaly")
        self.assertEqual(self.store.auto_symbols(), [])
        halted, reason = guards.is_halted()
        self.assertTrue(halted)
        self.assertIn("safety trip", reason)
        guards.resume()


# =====================================================================
# AT5 — attribution
# =====================================================================

class TestAttribution(unittest.TestCase):
    def test_every_origin_round_trips_through_a_client_id(self):
        for who in origin_mod.ORIGINS:
            cid = origin_mod.entry_id(who)
            self.assertEqual(origin_mod.parse(cid), who)
            self.assertLessEqual(len(cid), 36)

    def test_ids_satisfy_the_venue_pattern(self):
        import re
        pat = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")
        for who in origin_mod.ORIGINS:
            self.assertRegex(origin_mod.entry_id(who), pat)
            self.assertRegex(origin_mod.tranche_id(who, 12345, 3), pat)
            self.assertRegex(origin_mod.adopt_id(who, "BTCUSDT", "LONG"), pat)

    def test_an_unknown_origin_is_rejected_not_defaulted(self):
        """Silently defaulting is how a test order becomes a real one."""
        for bad in ("", "banana", None, "LIVE"):
            with self.assertRaises(origin_mod.OriginError):
                origin_mod.normalize(bad)

    def test_legacy_ids_are_identified_as_legacy(self):
        self.assertEqual(origin_mod.parse("armm-e-1788272129200"), origin_mod.LEGACY)
        self.assertEqual(origin_mod.parse("armm-tp1-p6"), origin_mod.LEGACY)
        self.assertEqual(origin_mod.parse("something-else"), origin_mod.UNKNOWN)
        self.assertEqual(origin_mod.parse(None), origin_mod.UNKNOWN)

    def test_test_orders_are_announced_as_tests(self):
        self.assertIn("TEST", origin_mod.label(origin_mod.TEST))
        self.assertIn("🧪", origin_mod.label(origin_mod.TEST))
        self.assertTrue(origin_mod.is_test(origin_mod.entry_id("test")))
        self.assertFalse(origin_mod.is_test(origin_mod.entry_id("auto")))

    def test_manual_versus_automated(self):
        self.assertTrue(origin_mod.is_manual("cli"))
        self.assertTrue(origin_mod.is_manual("tg"))
        self.assertFalse(origin_mod.is_manual("auto"))
        self.assertFalse(origin_mod.is_manual("test"))


# =====================================================================
# AT6 — scoreboard
# =====================================================================

class TestScoreboard(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.Store(":memory:")

    def _closed(self, who, r, overridden=0):
        pid = self.store.create_position(
            "BTCUSDT", "LONG", "DEMO", 5, 500, 78000, 0.0064, 77220,
            [78390.0], [100], origin_mod.entry_id(who), source=who)
        self.store.update_position(pid, state=store_mod.CLOSED, avg_entry=78000.0,
                                   r_multiple=r, exit_price=78000 + r * 780,
                                   advisor_overridden=overridden)
        return pid

    def test_splits_auto_from_manual(self):
        for r in (1.0, -1.0, 2.0):
            self._closed("auto", r)
        for r in (-1.0, -1.0):
            self._closed("tg", r, overridden=1)
        buckets, _ = scoreboard.collect(self.store)
        self.assertEqual(len(buckets["AUTO"]), 3)
        self.assertEqual(len(buckets["MANUAL"]), 2)

    def test_statistics_are_correct(self):
        for r in (1.0, -1.0, 3.0):
            self._closed("auto", r)
        buckets, _ = scoreboard.collect(self.store)
        s = scoreboard.summarize(buckets["AUTO"])
        self.assertEqual(s["trades"], 3)
        self.assertAlmostEqual(s["win_rate"], 200 / 3, places=4)
        self.assertAlmostEqual(s["avg_r"], 1.0, places=9)
        self.assertAlmostEqual(s["total_r"], 3.0, places=9)
        self.assertAlmostEqual(s["max_dd"], 1.0, places=9)

    def test_test_orders_are_excluded(self):
        self._closed("auto", 1.0)
        self._closed("test", 99.0)
        buckets, excluded = scoreboard.collect(self.store)
        self.assertEqual(excluded, 1)
        self.assertEqual(len(buckets["AUTO"]), 1)
        self.assertNotIn("99", scoreboard.render(self.store))

    def test_unresolved_positions_are_counted_not_dropped(self):
        pid = self.store.create_position(
            "BTCUSDT", "LONG", "DEMO", 5, 500, 78000, 0.0064, 77220, [78390.0],
            [100], origin_mod.entry_id("auto"), source="auto")
        self.store.update_position(pid, state=store_mod.CLOSED)   # no r_multiple
        s = scoreboard.summarize(scoreboard.collect(self.store)[0]["AUTO"])
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["unresolved"], 1)
        self.assertIn("unresolved", scoreboard.render(self.store))

    def test_manual_overrides_are_reported(self):
        self._closed("tg", -1.0, overridden=1)
        self.assertIn("advisor override", scoreboard.render(self.store))

    def test_a_thin_sample_is_flagged_as_such(self):
        self._closed("auto", 2.0)
        self._closed("tg", -1.0)
        text = scoreboard.render(self.store)
        self.assertIn("Too few trades", text)

    def test_the_demo_exit_caveat_is_always_stated(self):
        self.assertIn("not fills", scoreboard.render(self.store))

    def test_r_multiple_maths(self):
        pos = {"avg_entry": 78000.0, "stop_price": 77220.0, "side": "LONG"}
        self.assertAlmostEqual(manager.r_multiple(pos, 78780.0), 1.0, places=9)
        self.assertAlmostEqual(manager.r_multiple(pos, 77220.0), -1.0, places=9)
        self.assertIsNone(manager.r_multiple({"avg_entry": 1, "stop_price": 1,
                                              "side": "LONG"}, 2))
        self.assertIsNone(manager.r_multiple(pos, None))


# =====================================================================
# the v0.4 vetoes
# =====================================================================

class TestLiquidationCheck(unittest.TestCase):
    def test_a_1pct_stop_is_fine_at_5x(self):
        bad, d = guards.stop_is_behind_liquidation(78000, 78000 * 0.99, 5)
        self.assertFalse(bad)
        self.assertGreater(d["liq_pct"], 15)

    def test_a_1pct_stop_is_behind_liquidation_at_141x(self):
        """The demo account's actual leverage, found in v0.2."""
        bad, d = guards.stop_is_behind_liquidation(78000, 78000 * 0.99, 141)
        self.assertTrue(bad)
        self.assertLess(d["liq_pct"], 1.0)

    def test_the_estimate_is_conservative(self):
        # Maintenance margin pulls liquidation CLOSER than the naive 1/L.
        liq = guards.est_liquidation_price(100.0, 10)
        self.assertGreater(liq, 90.0)
        self.assertLess(liq, 100.0)

    def test_shorts_liquidate_upward(self):
        self.assertGreater(guards.est_liquidation_price(100.0, 10, "SHORT"), 100.0)

    def test_zero_leverage_is_rejected(self):
        with self.assertRaises(ValueError):
            guards.est_liquidation_price(100.0, 0)

    def test_the_advisor_surfaces_it_as_a_contradiction(self):
        snap = signals.Snapshot(symbol="BTCUSDT", price=78000.0, rsi=50.0,
                                trend_up=True, macd_hist=1.0, vwap=78000.0,
                                vwap_upper=79000.0, vwap_lower=77000.0)
        adv = advisor.review("BTCUSDT", "LONG", entry_price=78000.0, snapshot=snap,
                             stop_price=78000 * 0.99, leverage=141)
        self.assertTrue(adv.contradicted)
        self.assertTrue(any("BEHIND LIQUIDATION" in c for c in adv.contradictions))

    def test_the_advisor_runs_it_even_when_the_snapshot_is_broken(self):
        broken = signals.Snapshot(symbol="X")
        broken.error = "no candles"
        adv = advisor.review("X", "LONG", entry_price=78000.0, snapshot=broken,
                             stop_price=78000 * 0.99, leverage=141)
        self.assertTrue(adv.contradicted, "a fatal stop must be flagged regardless")


class TestRegimeVeto(unittest.TestCase):
    def _c4h(self, closes):
        import test_signals
        return test_signals.mk(closes)

    def test_a_falling_series_is_regime_down(self):
        regime, detail = signals.classify_regime(
            self._c4h([100.0 - i * 0.3 for i in range(120)]))
        self.assertEqual(regime, signals.REGIME_DOWN)
        self.assertIn("structure broken", detail)

    def test_a_rising_series_is_regime_up(self):
        regime, _ = signals.classify_regime(
            self._c4h([100.0 + i * 0.3 for i in range(120)]))
        self.assertEqual(regime, signals.REGIME_UP)

    def test_insufficient_history_is_unknown_not_up(self):
        regime, detail = signals.classify_regime(self._c4h([100.0] * 10))
        self.assertIsNone(regime)
        self.assertIn("insufficient", detail)

    def test_setup_b_is_blocked_outright_in_regime_down(self):
        snap = confirming_snapshot("B")
        snap.regime = signals.REGIME_DOWN
        snap.regime_detail = "broken"
        signals._apply_v04_vetoes(snap, signals.SetupBParams(), [78000.0],
                                  [77000.0], [55.0], None)
        self.assertFalse(snap.setup_b)
        self.assertTrue(snap.setup_b_core, "the core rule still passed")
        self.assertTrue(any("REGIME VETO" in t for _, t in snap.reasons_b))

    def test_setup_a_needs_stabilisation_in_regime_down(self):
        snap = confirming_snapshot("A")
        snap.regime = signals.REGIME_DOWN
        snap.regime_detail = "broken"
        # RSI falling, no reclaim -> not stabilised
        signals._apply_v04_vetoes(snap, signals.SetupBParams(), [78000.0, 77000.0],
                                  [77500.0, 76500.0], [50.0, 45.0, 40.0], None)
        self.assertFalse(snap.setup_a)
        self.assertFalse(snap.stabilized)

    def test_setup_a_survives_regime_down_when_rsi_turns_up(self):
        snap = confirming_snapshot("A")
        snap.regime = signals.REGIME_DOWN
        snap.regime_detail = "broken"
        c30m = _thirty_min_agreeing_with_setup_a()
        signals._apply_v04_vetoes(snap, signals.SetupBParams(), [77000.0, 76900.0],
                                  [77500.0, 77400.0], [30.0, 32.0, 35.0], c30m)
        self.assertTrue(snap.stabilized, snap.stabilized_detail)

    def test_stabilisation_detects_a_reclaim(self):
        ok, detail = signals.assess_stabilization(
            [76900.0, 77600.0], [77500.0, 77400.0], [30.0, 29.0, 28.0])
        self.assertTrue(ok)
        self.assertIn("reclaim", detail)


def _thirty_min_agreeing_with_setup_a():
    """A 30m series oversold enough to confirm Setup A."""
    import test_signals
    return test_signals.mk(test_signals.series_with_rsi_near(30.0),
                           step=1_800_000)


class TestThirtyMinuteFrame(unittest.TestCase):
    def test_setup_a_requires_the_30m_to_be_oversold_too(self):
        snap = confirming_snapshot("A")
        c30m = _thirty_min_agreeing_with_setup_a()
        ok, detail, rsi30 = signals.assess_30m(snap, c30m, signals.SetupBParams())
        self.assertTrue(ok, detail)
        self.assertLess(rsi30, 40.0)

    def test_setup_a_is_vetoed_when_the_30m_is_not_oversold(self):
        import test_signals
        snap = confirming_snapshot("A")
        c30m = test_signals.mk(test_signals.series_with_rsi_near(60.0), step=1_800_000)
        ok, detail, _ = signals.assess_30m(snap, c30m, signals.SetupBParams())
        self.assertFalse(ok)
        self.assertIn("NOT below", detail)

    def test_a_missing_30m_frame_is_unknown_not_permission(self):
        snap = confirming_snapshot("B")
        ok, detail, _ = signals.assess_30m(snap, None, signals.SetupBParams())
        self.assertIsNone(ok)
        signals._apply_v04_vetoes(snap, signals.SetupBParams(), [78000.0],
                                  [77000.0], [55.0], None)
        self.assertFalse(snap.setup_b, "unknown must not permit an entry")

    def test_setup_b_is_vetoed_by_an_extended_30m(self):
        import test_signals
        snap = confirming_snapshot("B")
        c30m = test_signals.mk(test_signals.series_with_rsi_near(85.0), step=1_800_000)
        ok, detail, _ = signals.assess_30m(snap, c30m, signals.SetupBParams())
        self.assertFalse(ok)
        self.assertIn("extended", detail)


class TestAutopilotPlans(unittest.TestCase):
    def test_setup_b_plan_uses_the_research_targets(self):
        kw = autopilot.setup_b_plan_kwargs(confirming_snapshot("B"))
        self.assertEqual(kw["targets_pct"], ["3", "4", "5"])
        self.assertEqual(kw["stop_pct"], 1.0)

    def test_setup_a_plan_buys_the_lower_band(self):
        snap = confirming_snapshot("A")
        kw = autopilot.setup_a_plan_kwargs(snap)
        self.assertEqual(kw["entry"], snap.vwap_lower)
        self.assertGreaterEqual(kw["stop_pct"], autopilot.SETUP_A["stop_pct_floor"])
        self.assertLessEqual(kw["stop_pct"], autopilot.SETUP_A["stop_pct_cap"])

    def test_setup_a_stop_is_clamped_for_a_huge_sigma(self):
        snap = confirming_snapshot("A", vwap_sigma=50000.0)
        kw = autopilot.setup_a_plan_kwargs(snap)
        self.assertLessEqual(kw["stop_pct"], autopilot.SETUP_A["stop_pct_cap"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
