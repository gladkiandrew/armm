#!/usr/bin/env python3
"""Claude's /check read — explanation only. NO NETWORK, NO KEYS, NO ORDERS.

Every test injects a fake client: nothing here can reach the Anthropic API, and
the real ~/armm is never touched.
"""
import ast
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("ARMM_HOME", tempfile.mkdtemp(prefix="armm-test-"))

from armm import config, explain  # noqa: E402

SNAPSHOT = """🔭 BTCUSDT signal snapshot
close 78,212.5 · last closed 1H bar 10-25 14:00 UTC
aVWAP 77,950.0 (price above) band 77,400.0 … 78,500.0 σ 550.0
RSI(14) 1H 54.2 ↑ (prev 51.8)
MACD hist +12.40 expanding (macd +40.10 / signal +27.70)
4H trend UP (EMA20/50)

Setup A: no (RSI 54.2 not < 40) · Setup B: yes
  A ✗ RSI 54.2 not < 40
  B ✓ price above aVWAP"""


def fake_client(text="", stop_reason="end_turn", raises=None, seen=None):
    def create(**kw):
        if seen is not None:
            seen.update(kw)
        if raises:
            raise raises
        return SimpleNamespace(stop_reason=stop_reason,
                               content=[SimpleNamespace(type="text", text=text)])
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))


class TestExplain(unittest.TestCase):
    def test_no_key_means_no_call_and_no_change(self):
        with mock.patch.object(config, "env_get", return_value=None):
            self.assertIsNone(explain.explain(SNAPSHOT))
            self.assertEqual(explain.with_explanation(SNAPSHOT), SNAPSHOT)

    def test_a_grounded_read_is_returned_and_labelled(self):
        read = ("Setup B passes: price is above the aVWAP and RSI 54.2 is rising past 50. "
                "Setup A fails only because RSI 54.2 is not below 40.")
        seen = {}
        out = explain.with_explanation(SNAPSHOT, client=fake_client(read, seen=seen))
        self.assertTrue(out.startswith(SNAPSHOT))
        self.assertIn(explain.LABEL, out)
        self.assertIn(read, out)
        self.assertIn(SNAPSHOT, seen["messages"][0]["content"])      # the readout is all it sees
        self.assertIn("Never state a number that is not already in the snapshot", seen["system"])

    def test_a_read_that_invents_a_number_is_dropped(self):
        read = "Setup B passes; a stop near 77,000 would be sensible."     # 77000 is not in the snapshot
        self.assertIsNone(explain.explain(SNAPSHOT, client=fake_client(read)))

    def test_numbers_are_compared_normalized(self):
        self.assertEqual(explain.invented_numbers("close 78212.5, RSI 54.20", SNAPSHOT), [])
        self.assertEqual(explain.invented_numbers("RSI could reach 60", SNAPSHOT), ["60"])

    def test_refusal_error_and_empty_reply_all_fail_quiet(self):
        self.assertIsNone(explain.explain(SNAPSHOT, client=fake_client("x", stop_reason="refusal")))
        self.assertIsNone(explain.explain(SNAPSHOT, client=fake_client(raises=TimeoutError())))
        self.assertIsNone(explain.explain(SNAPSHOT, client=fake_client("   ")))

    def test_the_key_is_redacted_from_logs(self):
        env = {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key-123"}
        with mock.patch.object(config, "load_env", return_value=env):
            self.assertIn("sk-test-not-a-real-key-123", config.secret_fragments())


class TestNoLlmInDecisions(unittest.TestCase):
    """The same property `make no-llm-in-decisions` proves, kept in the test suite too."""
    SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "armm")
    DECIDE = ("guards", "ladder", "manager", "autopilot", "signals", "advisor", "weex_client",
              "store", "specs", "candles", "indicators", "origin", "scoreboard", "starvation",
              "research", "config")

    def imported(self, module):
        with open(os.path.join(self.SRC, module + ".py")) as f:
            tree = ast.parse(f.read())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[-1] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names |= {(n.module or "").split(".")[-1]} | {a.name for a in n.names}
        return names

    def test_nothing_that_decides_imports_the_model(self):
        for m in self.DECIDE:
            self.assertFalse(self.imported(m) & {"explain", "anthropic"}, m)

    def test_the_explainer_imports_nothing_that_trades(self):
        self.assertFalse(self.imported("explain") &
                         {"ladder", "manager", "autopilot", "guards", "weex_client", "advisor", "store"})


if __name__ == "__main__":
    unittest.main()
