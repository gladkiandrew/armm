#!/usr/bin/env python3
"""ARMM v0.5 tests — guard-starvation detection.

NO NETWORK, NO ORDERS.

The bug these tests exist to prevent: between 2026-09-05 and 2026-10-03 the
autopilot refused 335 consecutive Setup B entries for one single reason (the
venue account sat at 141x, so every stop was behind liquidation), four weekly
digests went out saying "no trades", and nothing anywhere said "I am blocked,
not idle". A starved autopilot and a correctly quiet one looked identical.

The distinction these tests pin down: "the market is not offering a setup" is
HEALTH. "A setup confirmed and I then refused it" is a BLOCK. Only the second
kind accumulates, because only the second kind can indicate a config fault.
"""
import logging
import os
import tempfile
import unittest

_TMP_HOME = tempfile.mkdtemp(prefix="armm-starvtest-")
os.environ.setdefault("ARMM_HOME", _TMP_HOME)

import config
import starvation
import store as store_mod

_armm_log = logging.getLogger("armm")
_armm_log.handlers.clear()
_armm_log.addHandler(logging.NullHandler())
_armm_log.setLevel(logging.CRITICAL)

assert config.ARMM_HOME != os.path.expanduser("~/armm"), \
    "tests must not run against the real ~/armm"


def blocked(reason="advisor_refused", detail="STOP BEHIND LIQUIDATION: at 141x ...",
            symbol="BTCUSDT"):
    return {"symbol": symbol, "action": "skipped", "reason": reason, "detail": detail}


def idle(reason="no_setup", symbol="BTCUSDT"):
    return {"symbol": symbol, "action": "skipped", "reason": reason,
            "detail": "setup B 2/3"}


def entered(symbol="BTCUSDT"):
    return {"symbol": symbol, "action": "entered", "setup": "B",
            "status": "NEW", "client_id": "armm-auto-e-1", "position_id": 1}


class TestClassify(unittest.TestCase):
    """A refusal after a confirmed setup is not the same event as a quiet market."""

    def test_advisor_refusal_is_blocked(self):
        self.assertEqual(starvation.classify(blocked("advisor_refused")),
                         starvation.BLOCKED)

    def test_guard_refusal_is_blocked(self):
        self.assertEqual(starvation.classify(blocked("guard_refused")),
                         starvation.BLOCKED)

    def test_no_setup_is_idle_not_blocked(self):
        """The whole alarm is worthless if a quiet market counts as starvation."""
        self.assertEqual(starvation.classify(idle("no_setup")), starvation.IDLE)

    def test_rate_limited_is_idle(self):
        """Autopilot declining to over-trade is the design working, not a fault."""
        self.assertEqual(starvation.classify(idle("rate_limited")), starvation.IDLE)

    def test_already_acted_is_idle(self):
        self.assertEqual(starvation.classify(idle("already_acted")), starvation.IDLE)

    def test_position_open_is_idle(self):
        self.assertEqual(starvation.classify(idle("position_open")), starvation.IDLE)

    def test_disabled_is_idle(self):
        self.assertEqual(starvation.classify(idle("disabled")), starvation.IDLE)

    def test_halted_is_idle(self):
        """A halt is a human decision, loudly visible already — not starvation."""
        self.assertEqual(starvation.classify(idle("halted")), starvation.IDLE)

    def test_entry_is_entered(self):
        self.assertEqual(starvation.classify(entered()), starvation.ENTERED)

    def test_unknown_reason_is_idle_not_blocked(self):
        """An unrecognised code must not manufacture a false alarm."""
        self.assertEqual(starvation.classify(idle("some_future_code")),
                         starvation.IDLE)


class TrackerHarness(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.Store(":memory:")
        self.tracker = starvation.Tracker(store=self.store)


class TestStreakAccounting(TrackerHarness):

    def test_a_block_increments_the_streak(self):
        self.tracker.record(blocked(), now=1000.0)
        self.assertEqual(self.tracker.state("BTCUSDT")["streak"], 1)

    def test_blocks_accumulate(self):
        for i in range(4):
            self.tracker.record(blocked(), now=1000.0 + i)
        self.assertEqual(self.tracker.state("BTCUSDT")["streak"], 4)

    def test_an_entry_resets_the_streak(self):
        for i in range(5):
            self.tracker.record(blocked(), now=1000.0 + i)
        self.tracker.record(entered(), now=2000.0)
        self.assertEqual(self.tracker.state("BTCUSDT")["streak"], 0)

    def test_an_idle_cycle_leaves_the_streak_untouched(self):
        """A quiet market must neither raise the alarm nor silently clear it.

        This is the subtle half: if `no_setup` reset the streak, a block that
        only reappears when the setup reappears could never accumulate — which
        is exactly the 335-refusal pattern we failed to catch.
        """
        for i in range(3):
            self.tracker.record(blocked(), now=1000.0 + i)
        for i in range(50):
            self.tracker.record(idle(), now=1100.0 + i)
        self.assertEqual(self.tracker.state("BTCUSDT")["streak"], 3)

    def test_streak_records_its_time_span(self):
        self.tracker.record(blocked(), now=1000.0)
        self.tracker.record(blocked(), now=5000.0)
        st = self.tracker.state("BTCUSDT")
        self.assertEqual(st["first_ts"], 1000.0)
        self.assertEqual(st["last_ts"], 5000.0)

    def test_streak_counts_each_reason(self):
        self.tracker.record(blocked("advisor_refused"), now=1000.0)
        self.tracker.record(blocked("advisor_refused"), now=1001.0)
        self.tracker.record(blocked("guard_refused"), now=1002.0)
        self.assertEqual(self.tracker.state("BTCUSDT")["reasons"],
                         {"advisor_refused": 2, "guard_refused": 1})

    def test_symbols_are_tracked_independently(self):
        self.tracker.record(blocked(symbol="BTCUSDT"), now=1000.0)
        self.tracker.record(blocked(symbol="SOLUSDT"), now=1000.0)
        self.tracker.record(blocked(symbol="SOLUSDT"), now=1001.0)
        self.assertEqual(self.tracker.state("BTCUSDT")["streak"], 1)
        self.assertEqual(self.tracker.state("SOLUSDT")["streak"], 2)

    def test_state_survives_a_new_tracker_on_the_same_store(self):
        """Restart must not amnesia the streak — the service restarts often."""
        for i in range(6):
            self.tracker.record(blocked(), now=1000.0 + i)
        fresh = starvation.Tracker(store=self.store)
        self.assertEqual(fresh.state("BTCUSDT")["streak"], 6)

    def test_unknown_symbol_reads_as_a_clean_slate(self):
        st = self.tracker.state("ETHUSDT")
        self.assertEqual(st["streak"], 0)
        self.assertEqual(st["reasons"], {})


class TestAlarm(TrackerHarness):
    """The alarm that would have caught the 335-refusal month on day one."""

    def _run(self, n, reason="advisor_refused", start=1000.0, step=300.0):
        """n blocked cycles at the real 5-minute poll cadence. Returns alarms."""
        fired = []
        for i in range(n):
            a = self.tracker.record(blocked(reason), now=start + i * step)
            if a:
                fired.append(a)
        return fired

    def test_a_short_streak_does_not_alarm(self):
        """Three refusals is a Tuesday, not an incident."""
        self.assertEqual(self._run(starvation.STREAK_THRESHOLD - 1), [])

    def test_reaching_the_threshold_alarms_exactly_once(self):
        fired = self._run(starvation.STREAK_THRESHOLD)
        self.assertEqual(len(fired), 1)

    def test_the_alarm_names_the_dominant_reason_and_count(self):
        fired = self._run(starvation.STREAK_THRESHOLD)
        text = fired[0]
        self.assertIn("BTCUSDT", text)
        self.assertIn(str(starvation.STREAK_THRESHOLD), text)
        self.assertIn("advisor_refused", text)

    def test_the_alarm_carries_the_guards_own_explanation(self):
        """The remedy is already in the refusal detail — don't make them dig."""
        self.tracker = starvation.Tracker(store=self.store)
        for i in range(starvation.STREAK_THRESHOLD):
            a = self.tracker.record(
                blocked("advisor_refused",
                        "STOP BEHIND LIQUIDATION: at 141x on the venue, "
                        "liquidation is ~0.21% away"),
                now=1000.0 + i * 300.0)
        self.assertIn("141x", a)

    def test_the_alarm_says_this_is_a_config_fault_not_a_quiet_market(self):
        text = self._run(starvation.STREAK_THRESHOLD)[0]
        low = text.lower()
        self.assertTrue("not" in low and ("quiet" in low or "idle" in low),
                        f"alarm must distinguish blocked from quiet: {text!r}")

    def test_it_does_not_realarm_inside_the_cooldown(self):
        fired = self._run(starvation.STREAK_THRESHOLD * 4)
        self.assertEqual(len(fired), 1)

    def test_it_realarms_after_the_cooldown(self):
        self._run(starvation.STREAK_THRESHOLD)
        later = starvation.REALERT_SECONDS + 10_000.0
        a = self.tracker.record(blocked(), now=later)
        self.assertTrue(a, "a block persisting past the cooldown must re-alarm")

    def test_mixed_reasons_are_reported_as_mixed_not_as_one_cause(self):
        """Ten blocks for five different reasons is not one config fault."""
        reasons = ["advisor_refused", "guard_refused", "plan_failed",
                   "order_failed", "advisor_refused"]
        fired = []
        for i in range(starvation.STREAK_THRESHOLD):
            a = self.tracker.record(blocked(reasons[i % len(reasons)]),
                                    now=1000.0 + i * 300.0)
            if a:
                fired.append(a)
        self.assertEqual(len(fired), 1)
        self.assertIn("mixed", fired[0].lower())

    def test_an_entry_clears_the_alarm_so_a_later_block_can_fire_again(self):
        self._run(starvation.STREAK_THRESHOLD)
        self.tracker.record(entered(), now=50_000.0)
        fired = self._run(starvation.STREAK_THRESHOLD, start=60_000.0)
        self.assertEqual(len(fired), 1, "a fresh fault after a good trade must alarm")

    def test_the_alarm_is_written_to_the_event_log(self):
        self._run(starvation.STREAK_THRESHOLD)
        kinds = [e["kind"] for e in self.store.recent_events(50)]
        self.assertIn("starvation_alarm", kinds)

    def test_idle_cycles_do_not_dilute_an_accumulating_streak(self):
        """The real pattern: blocks only when a setup is live, quiet between."""
        fired = []
        for i in range(starvation.STREAK_THRESHOLD):
            for j in range(20):                       # a quiet stretch
                self.tracker.record(idle(), now=1000.0 + i * 10_000 + j)
            a = self.tracker.record(blocked(), now=1000.0 + i * 10_000 + 500)
            if a:
                fired.append(a)
        self.assertEqual(len(fired), 1)


class TestWeeklyDigest(TrackerHarness):
    """Four digests said "no trades" while autopilot was structurally blocked."""

    def _starve(self, n=starvation.STREAK_THRESHOLD, symbol="BTCUSDT"):
        for i in range(n):
            self.tracker.record(
                blocked("advisor_refused",
                        "STOP BEHIND LIQUIDATION: at 141x on the venue",
                        symbol=symbol),
                now=1000.0 + i * 300.0)

    def test_the_digest_leads_with_the_block_when_starved(self):
        import scoreboard
        self._starve()
        text = scoreboard.weekly_digest(self.store, now=200_000.0)
        self.assertIn("BLOCKED", text.upper())
        self.assertIn("141x", text)

    def test_the_block_appears_before_the_trade_stats(self):
        """'no trades' below an unexplained header is how a month was lost."""
        import scoreboard
        self._starve()
        text = scoreboard.weekly_digest(self.store, now=200_000.0)
        # "strategy as coded" is the stats label; plain "AUTO" would match
        # inside the "AUTOPILOT BLOCKED" header itself.
        self.assertLess(text.upper().index("BLOCKED"),
                        text.index("strategy as coded"))

    def test_a_healthy_week_adds_no_block_section(self):
        import scoreboard
        text = scoreboard.weekly_digest(self.store, now=200_000.0)
        self.assertNotIn("BLOCKED", text.upper())

    def test_every_starved_symbol_is_named(self):
        import scoreboard
        self._starve(symbol="BTCUSDT")
        self._starve(symbol="SOLUSDT")
        text = scoreboard.weekly_digest(self.store, now=200_000.0)
        self.assertIn("BTCUSDT", text)
        self.assertIn("SOLUSDT", text)
