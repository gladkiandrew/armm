import contextlib
import io
import unittest

from armm import demo, guards, research, signals


class TestDemo(unittest.TestCase):
    def test_synthetic_data_is_deterministic(self):
        a, b = demo.synthetic_candles(days=30), demo.synthetic_candles(days=30)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 30 * 24)
        for c in a:
            self.assertLessEqual(c.low, min(c.open, c.close))
            self.assertGreaterEqual(c.high, max(c.open, c.close))

    def test_aggregate_4h(self):
        c1h = demo.synthetic_candles(days=2)
        c4h = demo.aggregate(c1h)
        self.assertEqual(len(c4h), 12)
        self.assertEqual(c4h[0].open, c1h[0].open)
        self.assertEqual(c4h[0].close, c1h[3].close)

    def test_demo_cases_match_the_gate(self):
        self.assertEqual(guards.entry_violations("BTCUSDT", 500, 3, 77_400, 78_200, "LONG"), [])
        self.assertTrue(guards.entry_violations("BTCUSDT", 5000, 3, 77_400, 78_200, "LONG"))

    def test_backtest_runs_on_synthetic_year(self):
        c1h = demo.synthetic_candles()
        trades = research.backtest(c1h, demo.aggregate(c1h), signals.SetupBParams())
        self.assertGreater(len(trades), 0)

    def test_demo_main_runs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            demo.main()
        out = buf.getvalue()
        self.assertIn("REFUSE", out)
        self.assertIn("IS IT REAL?", out)


if __name__ == "__main__":
    unittest.main()
