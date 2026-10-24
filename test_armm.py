#!/usr/bin/env python3
"""ARMM unit tests — pure functions only. NO NETWORK, NO ORDERS.

Run: make check   (or: python3 -m unittest -v test_armm)
"""
import logging
import os
import tempfile
import unittest

# Redirect ARMM_HOME BEFORE the modules bind their paths, so `make check` can
# never touch the real ~/armm — no guard state, no specs cache, no live db.
_TMP_HOME = tempfile.mkdtemp(prefix="armm-test-")
os.environ["ARMM_HOME"] = _TMP_HOME

import config
import guards
import ladder
import manager
import specs
import store as store_mod
import tg

# These tests exercise manager/store paths that log. Detach the handlers the
# imports installed so a test run stays readable and never writes to ~/armm/bot.log.
_armm_log = logging.getLogger("armm")
_armm_log.handlers.clear()
_armm_log.addHandler(logging.NullHandler())
_armm_log.setLevel(logging.CRITICAL)

# Belt and braces: rebind the guard state file explicitly and assert the
# isolation held, so a future refactor cannot quietly reintroduce the leak.
guards.STATE_PATH = os.path.join(_TMP_HOME, "guard_state.json")
# When both suites run in one process the first import wins, so assert the
# property that actually matters: we are NOT pointed at the operator's ~/armm.
_REAL_HOME = os.path.expanduser("~/armm")
assert config.ARMM_HOME != _REAL_HOME, "tests must not run against the real ~/armm"
assert config.ARMM_HOME.startswith(tempfile.gettempdir()), \
    f"ARMM_HOME must be a temp dir, got {config.ARMM_HOME}"


class TestSuiteIsIsolated(unittest.TestCase):
    """The suite mutates guard state; it must never touch the operator's."""

    def test_guard_state_path_is_a_temp_file(self):
        self.assertTrue(guards.STATE_PATH.startswith(tempfile.gettempdir()),
                        guards.STATE_PATH)
        self.assertNotEqual(os.path.dirname(guards.STATE_PATH), _REAL_HOME)

    def test_recording_a_close_does_not_touch_the_real_home(self):
        real = os.path.expanduser("~/armm/guard_state.json")
        before = os.path.getmtime(real) if os.path.exists(real) else None
        guards.record_close("TESTONLY")
        after = os.path.getmtime(real) if os.path.exists(real) else None
        self.assertEqual(before, after, "a test wrote to the real guard state")

BTC = {"step_size": 0.0001, "tick_size": 0.1, "min_qty": 0.0001, "max_qty": 1200.0,
       "price_precision": 1, "qty_precision": 4}
BOOK = {"bid": 78160.8, "ask": 78160.9}


class TestSpecsMath(unittest.TestCase):
    def test_floor_to_step_never_oversizes(self):
        self.assertEqual(specs.floor_to_step(0.006399, 0.0001), 0.0063)
        self.assertEqual(specs.floor_to_step(0.00639999, 0.0001), 0.0063)
        self.assertEqual(specs.floor_to_step(1.0, 0.0001), 1.0)

    def test_floor_to_step_rejects_bad_step(self):
        with self.assertRaises(specs.SpecError):
            specs.floor_to_step(1.0, 0)

    def test_round_to_tick_modes(self):
        self.assertEqual(specs.round_to_tick(78160.87, 0.1), 78160.9)
        self.assertEqual(specs.round_to_tick(78160.87, 0.1, "down"), 78160.8)
        self.assertEqual(specs.round_to_tick(78160.81, 0.1, "up"), 78160.9)

    def test_shift_ticks_is_exact(self):
        # 78346.4 - 0.1 in float is 78346.29999999999, which floors a tick too far.
        self.assertEqual(specs.shift_ticks(78346.4, 0.1, -1), 78346.3)
        self.assertEqual(specs.round_to_tick(
            specs.shift_ticks(78346.4, 0.1, -1), 0.1, "down"), 78346.3)

    def test_fmt_uses_venue_precision(self):
        self.assertEqual(specs.fmt(0.0063, 0.0001), "0.0063")
        self.assertEqual(specs.fmt(78400, 0.1), "78400.0")
        self.assertEqual(specs.decimals_for(0.0001), 4)

    def test_market_symbol_strips_sim_quote(self):
        self.assertEqual(specs.market_symbol("BTCSUSDT"), "BTCUSDT")
        self.assertEqual(specs.market_symbol("BTCUSDT"), "BTCUSDT")


class TestEntryPricing(unittest.TestCase):
    def test_offset_long_and_short(self):
        # LONG buys 40 pts below last, and 78160.0 is inside the 78160.8/78160.9
        # book, so no reprice. SHORT sells 40 pts above last against a book that
        # sits below it, likewise untouched.
        px, why = ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1, offset=40)
        self.assertEqual((px, why), (78160.0, None))
        low = {"bid": 78000.0, "ask": 78000.1}
        px, why = ladder.resolve_entry("SHORT", 78100.0, low, 0.1, offset=40)
        self.assertEqual((px, why), (78140.0, None))

    def test_offset_pct(self):
        high = {"bid": 79500.0, "ask": 79500.1}
        px, _ = ladder.resolve_entry("LONG", 80000.0, high, 0.1, offset_pct=1.0)
        self.assertEqual(px, 79200.0)
        px, _ = ladder.resolve_entry("SHORT", 80000.0, high, 0.1, offset_pct=1.0)
        self.assertEqual(px, 80800.0)

    def test_offset_below_the_book_stays_maker_without_repricing(self):
        # An offset that lands away from the touch must be left exactly where
        # the operator asked — repricing is only for orders that would cross.
        px, why = ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1, offset=200)
        self.assertEqual((px, why), (78000.0, None))

    def test_absolute_entry_passes_through(self):
        px, why = ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1, entry=78000.0)
        self.assertEqual((px, why), (78000.0, None))

    def test_crossing_long_is_repriced_one_tick_inside_the_ask(self):
        px, why = ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1, entry=99000.0)
        self.assertEqual(px, 78160.8)
        self.assertLess(px, BOOK["ask"])
        self.assertIn("repriced", why)

    def test_crossing_short_is_repriced_one_tick_inside_the_bid(self):
        px, why = ladder.resolve_entry("SHORT", 78200.0, BOOK, 0.1, entry=10.0)
        self.assertEqual(px, 78160.9)
        self.assertGreater(px, BOOK["bid"])
        self.assertIn("repriced", why)

    def test_taker_entry_is_not_repriced(self):
        px, why = ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1, entry=99000.0, maker=False)
        self.assertEqual((px, why), (99000.0, None))

    def test_entry_modes_are_mutually_exclusive(self):
        with self.assertRaises(ladder.LadderError):
            ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1, entry=1, offset=2)
        with self.assertRaises(ladder.LadderError):
            ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1)

    def test_repriced_entries_never_cross_the_book(self):
        # Whatever the operator asked for, a POST_ONLY entry must land on our
        # own side of the touch — that is the whole point of maker-first.
        for ask_side in (99000.0, 78160.9, 78160.85):
            px, _ = ladder.resolve_entry("LONG", 78200.0, BOOK, 0.1, entry=ask_side)
            self.assertLess(px, BOOK["ask"], f"LONG entry {ask_side} crossed")
        for bid_side in (10.0, 78160.8, 78160.75):
            px, _ = ladder.resolve_entry("SHORT", 78200.0, BOOK, 0.1, entry=bid_side)
            self.assertGreater(px, BOOK["bid"], f"SHORT entry {bid_side} crossed")


class TestStopsAndTargets(unittest.TestCase):
    def test_stop_pct_rounds_away_from_entry(self):
        self.assertEqual(ladder.derive_stop("LONG", 78000.0, 0.1, stop_pct=1), 77220.0)
        self.assertEqual(ladder.derive_stop("SHORT", 78000.0, 0.1, stop_pct=1), 78780.0)

    def test_stop_is_mandatory(self):
        with self.assertRaises(ladder.LadderError):
            ladder.derive_stop("LONG", 78000.0, 0.1)

    def test_stop_forms_are_exclusive(self):
        with self.assertRaises(ladder.LadderError):
            ladder.derive_stop("LONG", 78000.0, 0.1, stop=77000, stop_pct=1)

    def test_targets_pct(self):
        t = ladder.derive_targets("LONG", 78000.0, 0.1, targets_pct=[0.5, 1, 1.5])
        self.assertEqual(t, [78390.0, 78780.0, 79170.0])
        t = ladder.derive_targets("SHORT", 78000.0, 0.1, targets_pct=[0.5, 1, 1.5])
        self.assertEqual(t, [77610.0, 77220.0, 76830.0])

    def test_targets_on_the_wrong_side_are_rejected(self):
        with self.assertRaises(ladder.LadderError):
            ladder.derive_targets("LONG", 78000.0, 0.1, targets=[77000])
        with self.assertRaises(ladder.LadderError):
            ladder.derive_targets("SHORT", 78000.0, 0.1, targets=[79000])

    def test_splits_default_and_validate(self):
        self.assertAlmostEqual(sum(ladder.normalize_splits(None, 3)), 100.0, places=6)
        self.assertEqual(ladder.normalize_splits(["33", "33", "34"], 3), [33.0, 33.0, 34.0])
        with self.assertRaises(ladder.LadderError):
            ladder.normalize_splits(["50", "50"], 3)
        with self.assertRaises(ladder.LadderError):
            ladder.normalize_splits(["50", "20", "10"], 3)


class TestSizing(unittest.TestCase):
    def test_size_floors_to_step(self):
        self.assertEqual(ladder.size_position(500, 78400, 0.0001, 0.0001), 0.0063)

    def test_size_below_venue_minimum_is_refused(self):
        with self.assertRaises(ladder.LadderError):
            ladder.size_position(1, 78400, 0.0001, 0.0001)

    def test_tranches_sum_to_the_filled_quantity(self):
        for qty in (0.0063, 0.01, 0.1234, 1.0):
            tr = ladder.tranche_plan(qty, [1, 2, 3], [33, 33, 34], 0.0001)
            self.assertAlmostEqual(sum(t["qty"] for t in tr), qty, places=8,
                                   msg=f"tranches lost quantity for {qty}")

    def test_tranches_drop_zero_sized_levels(self):
        tr = ladder.tranche_plan(0.0002, [1, 2, 3], [33, 33, 34], 0.0001)
        self.assertTrue(all(t["qty"] > 0 for t in tr))
        self.assertAlmostEqual(sum(t["qty"] for t in tr), 0.0002, places=8)

    def test_manager_shares_the_ladder_tranche_engine(self):
        a = manager.compute_tranches(0.0063, [1, 2, 3], [33, 33, 34], 0.0001)
        b = ladder.tranche_plan(0.0063, [1, 2, 3], [33, 33, 34], 0.0001)
        self.assertEqual(a, b)


class TestVetoRetryPreservesIntent(unittest.TestCase):
    """A retry must re-derive the operator's entry spec, never chase the touch."""

    def test_offset_entry_stays_an_offset_entry_on_the_retry(self):
        # Market fell 44 points between the veto and the retry.
        before = ladder.resolve_entry("LONG", 78214.5, {"bid": 78214.5, "ask": 78214.6},
                                      0.1, offset=40)[0]
        after = ladder.resolve_entry("LONG", 78170.5, {"bid": 78170.5, "ask": 78170.6},
                                     0.1, offset=40)[0]
        self.assertEqual(before, 78174.5)
        self.assertEqual(after, 78130.5)
        # The retry tracks the market down; it does NOT jump to the bid.
        self.assertLess(after, 78170.5, "retry must not chase up to the touch")
        self.assertAlmostEqual(78170.5 - after, 40.0, places=4)

    def test_build_plan_records_the_inputs_a_retry_needs(self):
        import inspect
        src = inspect.getsource(ladder.build_plan)
        self.assertIn("inputs = dict(", src)
        for field in ("offset", "offset_pct", "stop_pct", "targets_pct", "splits"):
            self.assertIn(field, src)

    def test_retry_path_rebuilds_rather_than_repricing_to_the_touch(self):
        import inspect
        src = inspect.getsource(ladder.place_plan)
        self.assertIn("plan = build_plan(client, **plan.inputs)", src)
        self.assertFalse(hasattr(ladder, "maker_price"),
                         "the touch-jumping helper must be gone")


class TestGuards(unittest.TestCase):
    def v(self, **kw):
        base = dict(symbol="BTCUSDT", notional=500, leverage=5, stop_price=77000,
                    entry_price=78000, side="LONG")
        base.update(kw)
        return guards.entry_violations(**base)

    def test_clean_entry_passes(self):
        self.assertEqual(self.v(), [])

    def test_leverage_cap(self):
        self.assertTrue(any("leverage" in e for e in self.v(leverage=20)))
        self.assertEqual(self.v(leverage=guards.MAX_LEVERAGE), [])

    def test_stop_is_mandatory(self):
        self.assertTrue(any("stops are mandatory" in e for e in self.v(stop_price=None)))

    def test_stop_must_be_on_the_right_side(self):
        self.assertTrue(any("LONG stop must be below" in e for e in self.v(stop_price=79000)))
        self.assertTrue(any("SHORT stop must be above" in e
                            for e in self.v(side="SHORT", stop_price=77000)))

    def test_notional_cap(self):
        self.assertTrue(any("notional" in e for e in self.v(notional=99999)))

    def test_max_concurrent(self):
        self.assertTrue(any("positions" in e
                            for e in self.v(open_position_count=guards.MAX_CONCURRENT)))

    def test_chase_guard(self):
        self.assertTrue(any("chase guard" in e
                            for e in self.v(entry_price=80000, reference_price=78000)))
        self.assertEqual(self.v(entry_price=78100, reference_price=78000), [])

    def test_chase_guard_short_side(self):
        self.assertTrue(any("chase guard" in e for e in self.v(
            side="SHORT", entry_price=76000, stop_price=77000, reference_price=78000)))

    def test_cooldown(self):
        errs = self.v(last_close_ts=1000.0, now=1000.0 + 60)
        self.assertTrue(any("cooldown" in e for e in errs))
        self.assertEqual(self.v(last_close_ts=1000.0,
                                now=1000.0 + guards.COOLDOWN_MIN * 60 + 1), [])

    def test_halt_blocks_everything(self):
        errs = self.v(halted=True, halt_reason="kill switch")
        self.assertTrue(any("TRADING HALTED" in e for e in errs))

    def test_daily_loss_halt(self):
        self.assertTrue(guards.daily_loss_breached(-70, 1000))
        self.assertFalse(guards.daily_loss_breached(-50, 1000))
        self.assertFalse(guards.daily_loss_breached(-70, 0))
        errs = self.v(day_pnl=-70, day_start_equity=1000)
        self.assertTrue(any("daily loss" in e for e in errs))

    def test_blank_state_is_deep_copied(self):
        """record_close() must not mutate the module-level blank template.

        A shallow dict(_BLANK) leaves the caller sharing the nested last_close
        dict, so the first cooldown ever recorded would poison every later
        'fresh' state in the process — which is exactly how a cooldown from one
        test suite silently refused entries in the next.
        """
        import copy
        import tempfile as _tf
        original = copy.deepcopy(guards._BLANK)
        saved_path = guards.STATE_PATH
        try:
            guards.STATE_PATH = os.path.join(_tf.mkdtemp(), "g.json")
            guards.record_close("BTCUSDT")
            self.assertEqual(guards._BLANK, original,
                             "record_close mutated the blank template")
            guards.STATE_PATH = os.path.join(_tf.mkdtemp(), "g2.json")
            self.assertEqual(guards.state()["last_close"], {},
                             "a fresh state file must be genuinely blank")
        finally:
            guards.STATE_PATH = saved_path

    def test_violations_accumulate(self):
        errs = self.v(leverage=20, stop_price=None, notional=99999)
        self.assertGreaterEqual(len(errs), 3)


class TestTelegramParsing(unittest.TestCase):
    def test_brief_example(self):
        kw = tg.parse_ladder_args(
            "BTCUSDT LONG offset=40 notional=500 stop=1% targets=0.5,1,1.5".split())
        self.assertEqual(kw["symbol"], "BTCUSDT")
        self.assertEqual(kw["side"], "LONG")
        self.assertEqual(kw["offset"], 40.0)
        self.assertEqual(kw["notional"], 500.0)
        self.assertEqual(kw["stop_pct"], 1.0)
        self.assertEqual(kw["targets_pct"], ["0.5", "1", "1.5"])
        self.assertFalse(kw["live"])

    def test_absolute_forms(self):
        kw = tg.parse_ladder_args(
            "BTCUSDT SHORT entry=78000 notional=500 stopprice=79000 "
            "targetprices=77000,76000 lev=3".split())
        self.assertEqual(kw["entry"], 78000.0)
        self.assertEqual(kw["stop"], 79000.0)
        self.assertEqual(kw["leverage"], 3)

    def test_live_token(self):
        self.assertTrue(tg.parse_ladder_args(
            "BTCUSDT LONG offset=40 notional=500 stop=1% targets=1 live".split())["live"])

    def test_bad_input_is_rejected(self):
        for bad in ("BTCUSDT SIDEWAYS notional=1",
                    "BTCUSDT LONG offset=40",
                    "BTCUSDT LONG offset=40 notional=500 bogus=1",
                    "BTCUSDT LONG offset=40 notional=500 justwrong",
                    "BTCUSDT"):
            with self.assertRaises(ladder.LadderError, msg=bad):
                tg.parse_ladder_args(bad.split())


class TestStoreIdempotency(unittest.TestCase):
    def setUp(self):
        self.s = store_mod.Store(":memory:")
        self.pid = self.s.create_position(
            "BTCUSDT", "LONG", "DEMO", 5, 500, 78000, 0.0064, 77220,
            [78390, 78780, 79170], [33, 33, 34], "armm-e-test")

    def test_new_position_starts_pending(self):
        self.assertEqual(self.s.get_position(self.pid)["state"], store_mod.PENDING_ENTRY)

    def test_client_id_uniqueness_blocks_duplicate_orders(self):
        cid = manager.tranche_client_id(self.pid, 2, "cli")
        self.assertTrue(self.s.add_order(self.pid, "TP", cid, "BTCUSDT", "SELL",
                                         "LONG", 78780, 0.0021))
        # A restart re-derives the SAME id; the second insert must not win.
        self.assertFalse(self.s.add_order(self.pid, "TP", cid, "BTCUSDT", "SELL",
                                          "LONG", 78780, 0.0021))
        self.assertEqual(len(self.s.orders_for(self.pid, kind="TP")), 1)

    def test_tranche_client_id_is_deterministic(self):
        self.assertEqual(manager.tranche_client_id(7, 2, "auto"),
                         manager.tranche_client_id(7, 2, "auto"))
        self.assertNotEqual(manager.tranche_client_id(7, 2, "auto"),
                            manager.tranche_client_id(7, 3, "auto"))
        self.assertNotEqual(manager.tranche_client_id(7, 2, "auto"),
                            manager.tranche_client_id(7, 2, "cli"))
        for who in ("cli", "tg", "auto", "test"):
            self.assertLessEqual(len(manager.tranche_client_id(999999, 9, who)), 36)

    def test_state_machine_transitions(self):
        for st in (store_mod.OPEN, store_mod.EXITING, store_mod.CLOSED):
            self.s.update_position(self.pid, state=st)
            self.assertEqual(self.s.get_position(self.pid)["state"], st)
        self.assertEqual(self.s.active_positions(), [])

    def test_active_positions_filters_by_mode(self):
        self.assertEqual(len(self.s.active_positions(mode="DEMO")), 1)
        self.assertEqual(len(self.s.active_positions(mode="LIVE")), 0)


class TestCapabilities(unittest.TestCase):
    def test_demo_cannot_cancel_or_rest_tranches(self):
        c = manager.Capabilities(demo=True)
        self.assertFalse(c.cancel)
        self.assertFalse(c.real_tranches)
        self.assertFalse(c.conditional_orders)
        self.assertIn("DEMO", c.describe())

    def test_live_can(self):
        c = manager.Capabilities(demo=False)
        self.assertTrue(c.cancel)
        self.assertTrue(c.real_tranches)


class TestRoutingIsDemoByDefault(unittest.TestCase):
    """Invariant 1: demo unless --live. Checked without constructing a client."""

    def test_paths_are_distinct_and_sim_is_the_default_branch(self):
        import weex_client
        self.assertEqual(weex_client.DEMO_ORDER_PATH, "/capi/v3/sim/order")
        self.assertEqual(weex_client.LIVE_ORDER_PATH, "/capi/v3/order")
        import inspect
        sig = inspect.signature(weex_client.WeexClient.__init__)
        self.assertIs(sig.parameters["demo"].default, True)
        sig = inspect.signature(manager.Manager.__init__)
        self.assertIs(sig.parameters["demo"].default, True)


class FakeClient:
    """Minimal live-capable client stand-in. No network, no venue."""

    demo = False
    mode = "LIVE"

    def __init__(self, crash_after=None):
        self.placed = []
        self.crash_after = crash_after

    def _sym(self, s):
        return s

    def routing_line(self, s):
        return "routing: /capi/v3/order  (LIVE — REAL MONEY) | exchange symbol: " + s

    def place_order(self, symbol, side, position_side, qty, price=None, tif=None,
                    client_id=None, reduce_only=None, **kw):
        if self.crash_after is not None and len(self.placed) >= self.crash_after:
            raise RuntimeError("simulated crash mid-placement")
        self.placed.append(client_id)
        return {"orderId": f"oid-{len(self.placed)}"}

    def find_order(self, client_id, symbol=None, limit=100):
        return {"clientOrderId": client_id, "status": "NEW"} if client_id in self.placed else None

    def open_positions(self):
        return [{"symbol": "BTCUSDT", "side": "LONG", "size": 0.0063, "entry": 78000.0,
                 "leverage": "5", "unrealized_pnl": 0.0, "liq_price": None}]

    def last_price(self, symbol):
        return 78000.0

    def equity(self):
        return 1000.0


class SilentNotifier:
    def alert(self, text):
        pass

    def send(self, text):
        pass


class TestRestartIdempotency(unittest.TestCase):
    """Acceptance test 5, for the LIVE path where a duplicate costs real money."""

    def setUp(self):
        self.store = store_mod.Store(":memory:")
        self.pid = self.store.create_position(
            "BTCUSDT", "LONG", "LIVE", 5, 500, 78000, 0.0063, 77220,
            [78390.0, 78780.0, 79170.0], [33, 33, 34], "armm-e-live-test")
        self.store.update_position(self.pid, state=store_mod.OPEN, filled_qty=0.0063)

    def _manager(self, client):
        return manager.Manager(demo=False, client=client, store=self.store,
                               notifier=SilentNotifier())

    def _tid(self, index):
        pos = self.store.get_position(self.pid)
        return manager.tranche_client_id(self.pid, index, pos.get("source"))

    def test_clean_run_places_only_the_non_atomic_tranches(self):
        c = FakeClient()
        self._manager(c).place_tranches(self.store.get_position(self.pid))
        # T1 rides on the entry's atomic TP; only T2 and T3 go on the wire.
        self.assertEqual(c.placed, [self._tid(2), self._tid(3)])

    def test_tranche_ids_carry_the_position_origin(self):
        import origin as origin_mod
        self.assertTrue(self._tid(2).startswith("armm-cli-"), self._tid(2))
        self.assertEqual(origin_mod.parse(self._tid(2)), "cli")

    def test_a_pre_v04_tranche_row_blocks_a_duplicate_under_the_new_id(self):
        """Upgrading v0.3 -> v0.4 changed tranche ids. A position that already
        has rows under the OLD id must not have its ladder placed again."""
        import origin as origin_mod
        for i in (2, 3):
            self.store.add_order(self.pid, "TP",
                                 origin_mod.legacy_tranche_id(self.pid, i),
                                 "BTCUSDT", "SELL", "LONG", 78780.0, 0.002,
                                 status="NEW", reduce_only=True)
        c = FakeClient()
        self._manager(c).place_tranches(self.store.get_position(self.pid))
        self.assertEqual(c.placed, [],
                         "legacy-tracked tranches must not be re-placed on upgrade")

    def test_restart_after_completion_places_nothing_again(self):
        c = FakeClient()
        self._manager(c).place_tranches(self.store.get_position(self.pid))
        before = list(c.placed)
        # A fresh Manager over the SAME store — exactly what systemd Restart does.
        self._manager(c).place_tranches(self.store.get_position(self.pid))
        self.assertEqual(c.placed, before, "restart must not re-place settled tranches")

    def test_crash_mid_placement_resumes_without_duplicating(self):
        t2, t3 = self._tid(2), self._tid(3)
        crashed = FakeClient(crash_after=1)      # T2 lands, then we die on T3
        with self.assertRaises(RuntimeError):
            self._manager(crashed).place_tranches(self.store.get_position(self.pid))
        self.assertEqual(crashed.placed, [t2])

        resumed = FakeClient()                   # restart; venue still holds T2
        resumed.placed = list(crashed.placed)
        self._manager(resumed).place_tranches(self.store.get_position(self.pid))
        self.assertEqual(sorted(resumed.placed), sorted([t2, t3]))
        self.assertEqual(resumed.placed.count(t2), 1,
                         "T2 was already on the venue and must not be sent twice")

    def test_crash_before_the_wire_call_is_re_sent_not_lost(self):
        # Row written as PENDING_SEND, then the process died before the venue
        # ever saw it: the same client id must be re-sent, not skipped.
        cid = self._tid(2)
        self.store.add_order(self.pid, "TP", cid, "BTCUSDT", "SELL", "LONG",
                             78780.0, 0.002, status="PENDING_SEND", reduce_only=True)
        c = FakeClient()
        self._manager(c).place_tranches(self.store.get_position(self.pid))
        self.assertIn(cid, c.placed)
        self.assertEqual(c.placed.count(cid), 1)

    def test_reconcile_adopts_an_untracked_exchange_position(self):
        empty = store_mod.Store(":memory:")
        m = manager.Manager(demo=False, client=FakeClient(), store=empty,
                            notifier=SilentNotifier())
        drift = m.reconcile(startup=True)
        self.assertTrue(any("UNTRACKED" in d for d in drift))
        self.assertEqual(len(empty.active_positions()), 1)

    def test_demo_resting_entry_is_not_reported_as_drift(self):
        """Sim history lists terminal orders only, so an acked-but-absent demo
        entry is a RESTING order, not missing state. Flagging it would cry wolf
        on every single poll."""
        class DemoClient(FakeClient):
            demo = True
            mode = "DEMO"

            def find_order(self, client_id, symbol=None, limit=100):
                return None                      # resting: invisible in history

            def open_positions(self):
                return []

        s = store_mod.Store(":memory:")
        pid = s.create_position("BTCUSDT", "LONG", "DEMO", 5, 500, 78000, 0.0063,
                                77220, [78390.0], [100], "armm-e-resting")
        s.update_position(pid, entry_order_id="789672728289346417")
        m = manager.Manager(demo=True, client=DemoClient(), store=s,
                            notifier=SilentNotifier())
        self.assertEqual(m.reconcile(), [], "a resting demo entry must not be drift")

    def test_demo_entry_with_no_ack_is_still_drift(self):
        """No order id means the venue never confirmed it — that IS drift."""
        class DemoClient(FakeClient):
            demo = True
            mode = "DEMO"

            def find_order(self, client_id, symbol=None, limit=100):
                return None

            def open_positions(self):
                return []

        s = store_mod.Store(":memory:")
        s.create_position("BTCUSDT", "LONG", "DEMO", 5, 500, 78000, 0.0063, 77220,
                          [78390.0], [100], "armm-e-never-acked")
        m = manager.Manager(demo=True, client=DemoClient(), store=s,
                            notifier=SilentNotifier())
        self.assertTrue(any("PENDING_ENTRY" in d for d in m.reconcile()))

    def test_reconcile_closes_a_position_the_exchange_no_longer_has(self):
        class Flat(FakeClient):
            def open_positions(self):
                return []
        m = self._manager(Flat())
        drift = m.reconcile()
        self.assertTrue(any("flat on the exchange" in d for d in drift))
        self.assertEqual(self.store.get_position(self.pid)["state"], store_mod.CLOSED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
