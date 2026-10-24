#!/usr/bin/env python3
"""ARMM v0.3 signal-layer tests — pure, offline. NO NETWORK, NO ORDERS.

Run: make check   (or: python3 -m unittest -v tests.test_signals)
"""
import logging
import math
import os
import tempfile
import unittest

# Isolate ARMM_HOME before the modules bind their paths (same rule as test_armm).
_TMP_HOME = tempfile.mkdtemp(prefix="armm-sigtest-")
os.environ["ARMM_HOME"] = _TMP_HOME

from armm import advisor
from armm import candles
from armm import config
from armm import indicators as ind
from armm import research
from armm import signals
from armm import store as store_mod

_armm_log = logging.getLogger("armm")
_armm_log.handlers.clear()
_armm_log.addHandler(logging.NullHandler())
_armm_log.setLevel(logging.CRITICAL)

# When both suites run in one process the first import wins, so assert the
# property that actually matters: we are NOT pointed at the operator's ~/armm.
_REAL_HOME = os.path.expanduser("~/armm")
assert config.ARMM_HOME != _REAL_HOME, "tests must not run against the real ~/armm"
assert config.ARMM_HOME.startswith(tempfile.gettempdir()), \
    f"ARMM_HOME must be a temp dir, got {config.ARMM_HOME}"

HOUR = 3_600_000


def mk(closes, highs=None, lows=None, vols=None, start=1_700_000_000_000, step=HOUR):
    """Build a candle series from closes, with sane synthetic OHLCV."""
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * 1.001
        l = lows[i] if lows else c * 0.999
        v = vols[i] if vols else 100.0
        o = closes[i - 1] if i else c
        out.append(candles.Candle(start + i * step, o, max(h, o, c), min(l, o, c),
                                  c, v, start + (i + 1) * step))
    return out


# =====================================================================
# indicators — validated against exact arithmetic and an independent
# reference implementation, not against a half-remembered web table.
# =====================================================================

WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]

# Regression fixture: produced by this implementation AND independently derived
# by exact rational arithmetic for the seed (see the reference test below).
WILDER_RSI_EXPECTED = [
    70.46414, 66.24961, 66.48094, 69.34685, 66.29471, 57.91503, 62.88071,
    63.20876, 56.01158, 62.33992, 54.67097, 50.38685, 40.01942, 41.49265,
    41.90239, 45.49950, 37.32277, 33.09048, 37.78877,
]


def reference_rsi(closes, period=14):
    """Deliberately different structure from indicators.rsi — a second opinion."""
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ups = [c if c > 0 else 0.0 for c in changes]
    downs = [-c if c < 0 else 0.0 for c in changes]
    au = sum(ups[:period]) / period
    ad = sum(downs[:period]) / period
    out = []
    for idx in range(period, len(changes) + 1):
        if idx > period:
            au = (au * (period - 1) + ups[idx - 1]) / period
            ad = (ad * (period - 1) + downs[idx - 1]) / period
        out.append(100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad))
    return out


class TestRSI(unittest.TestCase):
    def test_seed_matches_exact_rational_arithmetic(self):
        """Sum of gains 3.34 and losses 1.40 over the first 14 changes gives
        RS = (3.34/14)/(1.40/14) = 2.3857142857 and RSI = 70.4641350211."""
        from fractions import Fraction as F
        vals = [F(str(c)) for c in WILDER_CLOSES[:15]]
        g = sum((vals[i] - vals[i - 1]) for i in range(1, 15) if vals[i] > vals[i - 1])
        l = sum((vals[i - 1] - vals[i]) for i in range(1, 15) if vals[i] < vals[i - 1])
        exact = float(100 - 100 / (1 + (g / 14) / (l / 14)))
        self.assertAlmostEqual(exact, 70.4641350211, places=8)
        first = next(v for v in ind.rsi(WILDER_CLOSES, 14) if v is not None)
        self.assertAlmostEqual(first, exact, places=8)

    def test_matches_independent_reference_implementation(self):
        mine = [v for v in ind.rsi(WILDER_CLOSES, 14) if v is not None]
        theirs = reference_rsi(WILDER_CLOSES, 14)
        self.assertEqual(len(mine), len(theirs))
        for a, b in zip(mine, theirs):
            self.assertAlmostEqual(a, b, places=9)

    def test_regression_fixture(self):
        mine = [v for v in ind.rsi(WILDER_CLOSES, 14) if v is not None]
        self.assertEqual(len(mine), len(WILDER_RSI_EXPECTED))
        for a, b in zip(mine, WILDER_RSI_EXPECTED):
            self.assertAlmostEqual(a, b, places=4)

    def test_alignment_first_value_sits_at_index_period(self):
        r = ind.rsi(WILDER_CLOSES, 14)
        self.assertEqual(len(r), len(WILDER_CLOSES))
        self.assertIsNone(r[13])
        self.assertIsNotNone(r[14])

    def test_monotonic_series_saturate(self):
        self.assertAlmostEqual(ind.rsi(list(range(1, 40)), 14)[-1], 100.0, places=6)
        self.assertAlmostEqual(ind.rsi(list(range(40, 1, -1)), 14)[-1], 0.0, places=6)

    def test_flat_series_is_neutral_not_saturated(self):
        # No gains AND no losses: 50 is the honest answer, 100 would be a lie.
        self.assertAlmostEqual(ind.rsi([50.0] * 30, 14)[-1], 50.0, places=6)

    def test_too_short_returns_all_none(self):
        self.assertTrue(all(v is None for v in ind.rsi([1, 2, 3], 14)))


class TestMovingAverages(unittest.TestCase):
    def test_sma_basic(self):
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 5)[-1], 3.0)
        self.assertIsNone(ind.sma([1, 2, 3], 5)[-1])

    def test_ema_of_a_constant_is_that_constant(self):
        self.assertAlmostEqual(ind.ema([7.0] * 50, 10)[-1], 7.0, places=9)

    def test_ema_seeds_from_the_sma(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        self.assertAlmostEqual(ind.ema(vals, 3)[2], 2.0, places=9)   # SMA(1,2,3)
        k = 2 / 4
        self.assertAlmostEqual(ind.ema(vals, 3)[3], (4.0 - 2.0) * k + 2.0, places=9)

    def test_wilder_smoothing_differs_from_ema(self):
        vals = [float(i) for i in range(1, 40)]
        self.assertNotAlmostEqual(ind.wilder_smooth(vals, 14)[-1], ind.ema(vals, 14)[-1],
                                  places=3)

    def test_alignment_is_preserved(self):
        for fn in (lambda v: ind.sma(v, 5), lambda v: ind.ema(v, 5),
                   lambda v: ind.wilder_smooth(v, 5)):
            out = fn([float(i) for i in range(20)])
            self.assertEqual(len(out), 20)
            self.assertIsNone(out[3])
            self.assertIsNotNone(out[4])


class TestMACD(unittest.TestCase):
    def test_macd_of_a_constant_is_zero(self):
        line, sig, hist = ind.macd([100.0] * 120)
        self.assertAlmostEqual(line[-1], 0.0, places=9)
        self.assertAlmostEqual(hist[-1], 0.0, places=9)

    def test_rising_series_gives_positive_macd(self):
        line, sig, hist = ind.macd([float(i) for i in range(1, 200)])
        self.assertGreater(line[-1], 0)

    def test_falling_series_gives_negative_macd(self):
        line, _, _ = ind.macd([float(i) for i in range(200, 1, -1)])
        self.assertLess(line[-1], 0)

    def test_histogram_is_line_minus_signal(self):
        line, sig, hist = ind.macd([100 + 10 * math.sin(i / 7) for i in range(200)])
        for l, s, h in zip(line, sig, hist):
            if h is not None:
                self.assertAlmostEqual(h, l - s, places=9)

    def test_signal_starts_after_the_macd_line(self):
        line, sig, hist = ind.macd([float(i) for i in range(1, 60)])
        first_line = next(i for i, v in enumerate(line) if v is not None)
        first_sig = next(i for i, v in enumerate(sig) if v is not None)
        self.assertEqual(first_line, 25)                  # slow(26) - 1
        self.assertEqual(first_sig, first_line + 8)       # + signal(9) - 1

    def test_fast_must_be_shorter_than_slow(self):
        with self.assertRaises(ind.IndicatorError):
            ind.macd([1.0] * 50, fast=26, slow=12)


class TestAnchoredVWAP(unittest.TestCase):
    def test_single_bar_vwap_is_its_typical_price(self):
        v, u, l, s = ind.anchored_vwap([10], [8], [9], [100], 0)
        self.assertAlmostEqual(v[0], 9.0, places=9)       # (10+8+9)/3
        self.assertEqual(s[0], 0.0)
        self.assertEqual(u[0], l[0])

    def test_volume_weighting_is_applied(self):
        # Two bars, typical prices 10 and 20, volumes 1 and 3 -> (10+60)/4 = 17.5
        v, _, _, _ = ind.anchored_vwap([10, 20], [10, 20], [10, 20], [1, 3], 0)
        self.assertAlmostEqual(v[-1], 17.5, places=9)

    def test_anchor_index_excludes_earlier_bars(self):
        highs = lows = closes = [100.0, 100.0, 50.0, 50.0]
        v, _, _, _ = ind.anchored_vwap(highs, lows, closes, [1, 1, 1, 1], anchor_index=2)
        self.assertAlmostEqual(v[-1], 50.0, places=9)
        self.assertIsNone(v[0])
        self.assertIsNone(v[1])

    def test_bands_are_symmetric_k_sigma(self):
        h = l = c = [10.0, 12.0, 14.0, 16.0]
        v, u, lo, s = ind.anchored_vwap(h, l, c, [1] * 4, 0, k=2.0)
        self.assertAlmostEqual(u[-1] - v[-1], 2.0 * s[-1], places=9)
        self.assertAlmostEqual(v[-1] - lo[-1], 2.0 * s[-1], places=9)

    def test_zero_volume_falls_back_to_mean_typical_price(self):
        v, _, _, _ = ind.anchored_vwap([10, 20], [10, 20], [10, 20], [0, 0], 0)
        self.assertAlmostEqual(v[-1], 15.0, places=9)

    def test_bad_anchor_index_raises(self):
        with self.assertRaises(ind.IndicatorError):
            ind.anchored_vwap([1], [1], [1], [1], anchor_index=5)


class TestATRAndVol(unittest.TestCase):
    def test_true_range_uses_the_previous_close(self):
        tr = ind.true_range([10, 12], [9, 11], [9.5, 11.5])
        self.assertAlmostEqual(tr[1], 2.5, places=9)      # 12 - 9.5

    def test_atr_of_constant_range_is_that_range(self):
        n = 40
        a = ind.atr([11.0] * n, [9.0] * n, [10.0] * n, 14)
        self.assertAlmostEqual(a[-1], 2.0, places=6)

    def test_realized_vol_of_a_flat_series_is_zero(self):
        self.assertAlmostEqual(ind.realized_volatility([100.0] * 50), 0.0, places=12)

    def test_realized_vol_is_positive_when_price_moves(self):
        self.assertGreater(ind.realized_volatility(
            [100 + 5 * math.sin(i) for i in range(50)]), 0)


class TestShapeHelpers(unittest.TestCase):
    def test_crossed_above_detects_a_fresh_cross(self):
        self.assertTrue(ind.crossed_above([45, 48, 52], 50, 3))

    def test_crossed_above_is_false_when_already_above_throughout(self):
        self.assertFalse(ind.crossed_above([55, 56, 57], 50, 3))

    def test_crossed_above_is_false_below_the_level(self):
        self.assertFalse(ind.crossed_above([45, 46, 47], 50, 3))

    def test_rising_and_slope(self):
        self.assertTrue(ind.rising([1, 2, 3, 4], 3))
        self.assertFalse(ind.rising([4, 3, 2, 1], 3))
        self.assertAlmostEqual(ind.slope([1, 2, 3, 4], 3), 3.0, places=9)
        self.assertIsNone(ind.slope([1, 2], 5))


# =====================================================================
# candles — the venue's ordering traps
# =====================================================================

class TestCandleNormalization(unittest.TestCase):
    def test_descending_rows_are_sorted_ascending(self):
        rows = [[3 * HOUR, 3, 3, 3, 3, 1, 4 * HOUR],
                [2 * HOUR, 2, 2, 2, 2, 1, 3 * HOUR],
                [1 * HOUR, 1, 1, 1, 1, 1, 2 * HOUR]]
        out = candles.normalize(rows, "1h", drop_incomplete=False)
        self.assertEqual([c.open_time for c in out], [HOUR, 2 * HOUR, 3 * HOUR])

    def test_live_bar_appended_last_is_still_placed_correctly(self):
        """/market/klines returns DESCENDING with the in-progress bar LAST."""
        rows = [[3 * HOUR, 3, 3, 3, 3, 1, 4 * HOUR],
                [2 * HOUR, 2, 2, 2, 2, 1, 3 * HOUR],
                [4 * HOUR, 4, 4, 4, 4, 1, 5 * HOUR]]     # live bar, appended last
        out = candles.normalize(rows, "1h", drop_incomplete=False)
        self.assertEqual([c.open_time for c in out],
                         [2 * HOUR, 3 * HOUR, 4 * HOUR])

    def test_incomplete_bar_is_dropped(self):
        rows = [[1 * HOUR, 1, 1, 1, 1, 1, 2 * HOUR],
                [2 * HOUR, 2, 2, 2, 2, 1, 3 * HOUR]]
        # "Now" is inside the second bar, so only the first has closed.
        out = candles.normalize(rows, "1h", drop_incomplete=True,
                                now_ms=2 * HOUR + 60_000)
        self.assertEqual([c.open_time for c in out], [HOUR])

    def test_duplicates_are_collapsed(self):
        rows = [[HOUR, 1, 1, 1, 1, 1, 2 * HOUR], [HOUR, 1, 1, 1, 1, 1, 2 * HOUR]]
        self.assertEqual(len(candles.normalize(rows, "1h", drop_incomplete=False)), 1)

    def test_unsupported_interval_is_rejected(self):
        with self.assertRaises(candles.CandleError):
            candles.interval_ms("7h")

    def test_index_at_or_after(self):
        c = mk([1, 2, 3, 4], start=0)
        self.assertEqual(candles.index_at_or_after(c, 0), 0)
        self.assertEqual(candles.index_at_or_after(c, 2 * HOUR), 2)
        self.assertIsNone(candles.index_at_or_after(c, 99 * HOUR))


# =====================================================================
# setups — fixture candles engineered to trigger and to miss
# =====================================================================

def series_with_rsi_near(target, n=120, base=100.0):
    """Build closes whose final RSI(14) lands near `target` by bisection."""
    lo, hi = -5.0, 5.0
    for _ in range(60):
        drift = (lo + hi) / 2
        closes = [base]
        for i in range(1, n):
            closes.append(closes[-1] * (1 + drift / 100.0 + 0.0004 * math.sin(i)))
        r = ind.rsi(closes, 14)[-1]
        if r is None:
            break
        if r < target:
            lo = drift
        else:
            hi = drift
    return closes


class TestSetupA(unittest.TestCase):
    """AT3: triggers on (RSI 35, price below lower band), not on (RSI 45, same)."""

    def _snapshot_with(self, rsi_target, with_30m=True):
        closes = series_with_rsi_near(rsi_target)
        c1h = mk(closes)
        c4h = mk(closes[::4])
        # v0.4 requires a 30m frame; build one that agrees with the 1H so this
        # test still measures the CORE Setup A rule.
        c30m = mk(series_with_rsi_near(rsi_target), step=HOUR // 2) if with_30m else None
        return signals.evaluate_series("FIX", c1h, c4h, c30m=c30m)

    def test_triggers_at_rsi_35_below_the_band(self):
        snap = self._snapshot_with(35.0)
        self.assertLess(snap.rsi, 40.0)
        if snap.price < snap.vwap_lower:
            self.assertTrue(snap.setup_a_core, snap.reasons_a)
            self.assertTrue(snap.setup_a, snap.reasons_a)
        else:                                    # band not breached -> must NOT fire
            self.assertFalse(snap.setup_a_core)

    def test_does_not_trigger_at_rsi_45(self):
        snap = self._snapshot_with(45.0)
        self.assertGreaterEqual(snap.rsi, 40.0)
        self.assertFalse(snap.setup_a)
        self.assertFalse(snap.setup_a_core)
        self.assertTrue(any(not ok and "NOT below 40" in txt for ok, txt in snap.reasons_a))

    def test_a_missing_30m_frame_blocks_the_entry(self):
        """Unknown is not permission — an entry needs positive 30m confirmation."""
        snap = self._snapshot_with(35.0, with_30m=False)
        if snap.setup_a_core:
            self.assertFalse(snap.setup_a, "no 30m frame must veto the entry")
            self.assertTrue(any("30m FRAME unavailable" in txt
                                for _, txt in snap.reasons_a))

    def test_rsi_and_band_are_both_required(self):
        # Synthetic snapshot: RSI passes, band fails -> setup must be False.
        s = signals.Snapshot(symbol="X", price=100.0, rsi=35.0, vwap_lower=90.0)
        signals._apply_setup_a(s)
        self.assertFalse(s.setup_a)
        s2 = signals.Snapshot(symbol="X", price=89.0, rsi=35.0, vwap_lower=90.0)
        signals._apply_setup_a(s2)
        self.assertTrue(s2.setup_a)

    def test_macd_is_explicitly_ignored_by_setup_a(self):
        s = signals.Snapshot(symbol="X", price=89.0, rsi=35.0, vwap_lower=90.0,
                             macd_hist=-999.0)
        signals._apply_setup_a(s)
        self.assertTrue(s.setup_a, "MACD must not gate Setup A")
        self.assertTrue(any("not applicable" in t for _, t in s.reasons_a))


class TestSetupB(unittest.TestCase):
    def _b(self, **over):
        s = signals.Snapshot(symbol="X", price=110.0, vwap=100.0, rsi=55.0,
                             rsi_rising=True, macd_hist=1.0,
                             macd_hist_expanding=True, trend_up=True)
        for k, v in over.items():
            setattr(s, k, v)
        signals._apply_setup_b(s, signals.SetupBParams(), [45.0, 50.0, 55.0],
                               [110.0], [100.0])
        return s

    def test_all_three_plus_trend_confirms(self):
        self.assertTrue(self._b().setup_b)

    def test_price_below_vwap_fails(self):
        self.assertFalse(self._b(price=90.0).setup_b)

    def test_extended_rsi_fails(self):
        s = self._b(rsi=75.0)
        self.assertFalse(s.setup_b)
        self.assertTrue(any("EXTENDED" in t for _, t in s.reasons_b))

    def test_rsi_below_the_cross_fails(self):
        s = self._b(rsi=45.0)
        self.assertFalse(s.setup_b)

    def test_negative_macd_histogram_fails(self):
        s = self._b(macd_hist=-1.0)
        self.assertFalse(s.setup_b)
        self.assertTrue(any("not positive" in t for _, t in s.reasons_b))

    def test_contracting_histogram_fails(self):
        s = self._b(macd_hist_expanding=False)
        self.assertFalse(s.setup_b)
        self.assertTrue(any("contracting" in t for _, t in s.reasons_b))

    def test_downtrend_fails(self):
        s = self._b(trend_up=False)
        self.assertFalse(s.setup_b)
        self.assertTrue(any("trend down" in t for _, t in s.reasons_b))

    def test_every_reason_is_recorded_even_when_passing(self):
        s = self._b()
        self.assertEqual(len(s.reasons_b), 4)
        self.assertTrue(all(ok for ok, _ in s.reasons_b))


class TestEvaluationPlumbing(unittest.TestCase):
    def test_short_series_reports_an_error_rather_than_guessing(self):
        snap = signals.evaluate_series("X", mk([100.0] * 10), [])
        self.assertIsNotNone(snap.error)
        self.assertFalse(snap.setup_a)
        self.assertFalse(snap.setup_b)

    def test_evaluate_at_matches_evaluate_series(self):
        """The backtest fast path must agree with the cockpit path exactly."""
        closes = [100 + 10 * math.sin(i / 9) + i * 0.05 for i in range(300)]
        c1h, c4h = mk(closes), mk(closes[::4])
        ctx = signals.precompute("X", c1h, c4h)
        for i in (150, 220, 299):
            a = signals.evaluate_at(ctx, i)
            b = signals.evaluate_series("X", c1h, c4h, upto=i + 1)
            self.assertEqual(a.setup_a, b.setup_a)
            self.assertEqual(a.setup_b, b.setup_b)
            self.assertAlmostEqual(a.rsi, b.rsi, places=9)
            self.assertAlmostEqual(a.vwap, b.vwap, places=9)
            self.assertAlmostEqual(a.macd_hist, b.macd_hist, places=9)

    def test_evaluate_at_has_no_lookahead(self):
        """A snapshot at bar i must not change when future bars are appended."""
        closes = [100 + 10 * math.sin(i / 9) for i in range(200)]
        c1h, c4h = mk(closes), mk(closes[::4])
        short = signals.evaluate_series("X", c1h[:150], c4h)
        ctx_long = signals.precompute("X", c1h, c4h)
        long_at = signals.evaluate_at(ctx_long, 149)
        self.assertAlmostEqual(short.rsi, long_at.rsi, places=9)
        self.assertAlmostEqual(short.macd_hist, long_at.macd_hist, places=9)
        self.assertEqual(short.setup_b, long_at.setup_b)

    def test_explain_flag_never_changes_a_verdict(self):
        """The backtest runs with explain=False for speed. If that could change
        a setup boolean, every research result would be about different rules
        than the cockpit reports."""
        closes = [100 + 12 * math.sin(i / 11) + i * 0.03 for i in range(400)]
        c1h, c4h = mk(closes), mk(closes[::4])
        for mode in ("fixed", "rolling"):
            ctx = signals.precompute("X", c1h, c4h, anchor_mode=mode)
            for i in range(80, 400, 7):
                loud = signals.evaluate_at(ctx, i, explain=True)
                quiet = signals.evaluate_at(ctx, i, explain=False)
                self.assertEqual(loud.setup_a, quiet.setup_a, f"{mode}@{i} setup A")
                self.assertEqual(loud.setup_b, quiet.setup_b, f"{mode}@{i} setup B")

    def test_rolling_anchor_has_no_lookahead(self):
        """A rolling anchor at bar i must not move when later bars are added."""
        closes = [100 + 12 * math.sin(i / 11) for i in range(300)]
        c1h = mk(closes)
        short = signals.rolling_anchor_indices(c1h[:200])
        long = signals.rolling_anchor_indices(c1h)
        self.assertEqual(short, long[:200])

    def test_confirmed_pivots_lag_their_low(self):
        closes = [100 + 12 * math.sin(i / 11) for i in range(300)]
        for confirm_i, pivot_i in signals.confirmed_pivot_lows(mk(closes)):
            self.assertGreater(confirm_i, pivot_i,
                               "a pivot cannot be known before its right side forms")

    def test_rolling_and_fixed_agree_when_no_pivot_exists(self):
        closes = [100.0 + i * 0.01 for i in range(200)]     # monotonic: no pivots
        c1h = mk(closes)
        self.assertEqual(set(signals.rolling_anchor_indices(c1h)), {0})

    def test_params_label_is_stable(self):
        p = signals.SetupBParams(rsi_cross=50, rsi_ceiling=70)
        self.assertEqual(p.label(), signals.SetupBParams(rsi_cross=50,
                                                         rsi_ceiling=70).label())


class TestSwingLowAnchor(unittest.TestCase):
    def test_finds_a_pivot_low_and_explains_itself(self):
        closes = ([100] * 10 + [95, 90, 85, 80, 75] + [70] + [75, 82, 90, 98, 105]
                  + [110] * 10)
        ts, why = signals.find_swing_low_anchor(mk([float(c) for c in closes]))
        self.assertIsNotNone(ts)
        self.assertIn("swing low", why)

    def test_excludes_the_unformed_right_edge(self):
        """A low inside the last `window` bars has no right side yet, so it can
        never be chosen as a pivot — picking it would be look-ahead."""
        closes = [float(x) for x in (
            [100] * 10 + [95, 90, 85] + [92, 98, 104, 110] + [110] * 12
            + [80, 70, 60])]              # a much lower low, but at the raw edge
        c = mk(closes)
        ts, why = signals.find_swing_low_anchor(c, window=5)
        self.assertIn("swing low", why, "an earlier pivot should have qualified")
        self.assertLess(ts, c[-5].open_time,
                        "the anchor must predate the unformed right edge")

    def test_falls_back_loudly_when_nothing_qualifies(self):
        closes = [100.0 + 0.01 * math.sin(i) for i in range(60)]
        ts, why = signals.find_swing_low_anchor(closes and mk(closes))
        self.assertIsNotNone(ts)
        self.assertIn("fell back", why)

    def test_short_series_does_not_raise(self):
        ts, why = signals.find_swing_low_anchor(mk([1.0, 2.0, 3.0]))
        self.assertIsNotNone(ts)


# =====================================================================
# advisor — AT5
# =====================================================================

class TestAdvisor(unittest.TestCase):
    def _snap(self, **over):
        s = signals.Snapshot(symbol="BTCUSDT", price=100.0, rsi=50.0, trend_up=True,
                             macd_hist=1.0, vwap=100.0, vwap_upper=105.0,
                             vwap_lower=95.0)
        for k, v in over.items():
            setattr(s, k, v)
        return s

    def test_the_canonical_bad_entry_is_flagged(self):
        """RSI > 60 with the 4H trend down — the 2026-09-01 demo entry."""
        adv = advisor.review("BTCUSDT", "LONG", entry_price=100.0,
                             snapshot=self._snap(rsi=65.0, trend_up=False))
        self.assertTrue(adv.contradicted)
        self.assertEqual(len(adv.contradictions), 2)
        blob = " ".join(adv.contradictions)
        self.assertIn("RSI", blob)
        self.assertIn("4H trend is DOWN", blob)
        self.assertIn("ADVISOR", adv.render())
        self.assertIn("confirm to place anyway", adv.render())

    def test_a_confirmed_setup_is_not_contradicted(self):
        s = self._snap(rsi=65.0, trend_up=False)
        s.setup_a = True
        adv = advisor.review("BTCUSDT", "LONG", snapshot=s)
        self.assertFalse(adv.contradicted)
        self.assertEqual(adv.matched_setup, "A")

    def test_chasing_above_the_upper_band_is_flagged(self):
        adv = advisor.review("BTCUSDT", "LONG", entry_price=110.0,
                             snapshot=self._snap(rsi=50.0))
        self.assertTrue(any("chase zone" in c for c in adv.contradictions))

    def test_a_neutral_readout_produces_no_contradiction(self):
        adv = advisor.review("BTCUSDT", "LONG", entry_price=100.0, snapshot=self._snap())
        self.assertFalse(adv.contradicted)

    def test_advisor_never_raises_on_a_broken_snapshot(self):
        broken = signals.Snapshot(symbol="X")
        broken.error = "no candles"
        adv = advisor.review("X", "LONG", snapshot=broken)
        self.assertFalse(adv.contradicted)       # must not block on missing data

    def test_db_note_is_bounded(self):
        adv = advisor.review("BTCUSDT", "LONG", entry_price=100.0,
                             snapshot=self._snap(rsi=65.0, trend_up=False))
        self.assertLessEqual(len(adv.db_note()), 500)


# =====================================================================
# edge-triggered alerts — AT4
# =====================================================================

class FakeNotifier:
    def __init__(self):
        self.sent = []

    def alert(self, text):
        self.sent.append(text)

    def send(self, text):
        self.sent.append(text)


class TestEdgeTriggeredAlerts(unittest.TestCase):
    def setUp(self):
        from armm import manager
        self.manager_mod = manager
        self.store = store_mod.Store(":memory:")
        self.notify = FakeNotifier()
        self.store.add_watch("BTCUSDT")

    def _mgr(self):
        return self.manager_mod.Manager(demo=True, client=object(), store=self.store,
                                        notifier=self.notify)

    def _fire(self, m, on, t):
        snap = signals.Snapshot(symbol="BTCUSDT", price=1.0)
        snap.setup_a = on
        return m._maybe_alert("BTCUSDT", "A", on, snap, t)

    def test_alerts_once_on_the_rising_edge(self):
        m = self._mgr()
        self.assertTrue(self._fire(m, True, 1000.0))
        self.assertEqual(len(self.notify.sent), 1)

    def test_stays_quiet_while_the_setup_remains_true(self):
        m = self._mgr()
        self._fire(m, True, 1000.0)
        for t in (1005.0, 1010.0, 9000.0, 99000.0):
            self.assertFalse(self._fire(m, True, t))
        self.assertEqual(len(self.notify.sent), 1, "a persistent setup must not spam")

    def test_re_alerts_only_after_the_cooldown(self):
        m = self._mgr()
        self._fire(m, True, 1000.0)
        self._fire(m, False, 1100.0)                       # setup drops out
        self.assertFalse(self._fire(m, True, 1200.0),
                         "inside the 1h cooldown it must stay quiet")
        self.assertEqual(len(self.notify.sent), 1)
        later = 1000.0 + self.manager_mod.SETUP_REALERT_SECONDS + 1
        self._fire(m, False, later - 1)
        self.assertTrue(self._fire(m, True, later))
        self.assertEqual(len(self.notify.sent), 2)

    def test_edge_state_survives_a_restart(self):
        m = self._mgr()
        self._fire(m, True, 1000.0)
        fresh = self._mgr()                                # new Manager, same store
        self.assertFalse(self._fire(fresh, True, 1010.0),
                         "an already-active setup must not re-alert on restart")

    def test_no_watches_means_no_work(self):
        self.store.remove_watch("BTCUSDT")
        self.assertEqual(self._mgr().check_watches(), [])


class TestStoreSignalTables(unittest.TestCase):
    def setUp(self):
        self.s = store_mod.Store(":memory:")

    def test_anchor_round_trip_and_upsert(self):
        self.s.set_anchor("BTCUSDT", 111, "auto")
        self.assertEqual(self.s.get_anchor("BTCUSDT")["anchor_ts"], 111)
        self.s.set_anchor("BTCUSDT", 222, "manual")
        self.assertEqual(self.s.get_anchor("BTCUSDT")["anchor_ts"], 222)
        self.assertEqual(self.s.get_anchor("BTCUSDT")["source"], "manual")

    def test_watch_add_remove_is_idempotent(self):
        self.assertTrue(self.s.add_watch("BTCUSDT"))
        self.assertFalse(self.s.add_watch("BTCUSDT"))
        self.assertEqual(self.s.watches(), ["BTCUSDT"])
        self.assertTrue(self.s.remove_watch("BTCUSDT"))
        self.assertFalse(self.s.remove_watch("BTCUSDT"))

    def test_symbols_are_upper_cased(self):
        self.s.add_watch("btcusdt")
        self.assertEqual(self.s.watches(), ["BTCUSDT"])

    def test_advisor_columns_exist_after_migration(self):
        cols = {r[1] for r in self.s.db.execute("PRAGMA table_info(positions)")}
        self.assertIn("advisor_overridden", cols)
        self.assertIn("advisor_notes", cols)


# =====================================================================
# research harness
# =====================================================================

class TestResearch(unittest.TestCase):
    def test_stop_is_taken_before_a_target_in_the_same_bar(self):
        """Pessimistic fill: a bar spanning both must resolve as the stop."""
        bars = [candles.Candle(0, 100.0, 100.0, 100.0, 100.0, 1, HOUR),
                candles.Candle(HOUR, 100.0, 106.0, 98.0, 99.0, 1, 2 * HOUR)]
        t = research.simulate_trade(bars, 1)
        self.assertEqual(t.reason, "stop")
        self.assertLess(t.pnl_pct, 0)

    def test_a_clean_run_to_all_targets_wins(self):
        bars = [candles.Candle(0, 100.0, 100.0, 100.0, 100.0, 1, HOUR)]
        for i in range(1, 8):
            px = 100.0 + i
            bars.append(candles.Candle(i * HOUR, px, px + 0.5, px - 0.2, px, 1,
                                       (i + 1) * HOUR))
        t = research.simulate_trade(bars, 1)
        self.assertGreater(t.pnl_pct, 0)
        self.assertEqual(t.reason, "all targets")

    def test_fees_are_charged(self):
        """A trade that exits exactly at entry must lose the fees, not break even."""
        bars = [candles.Candle(0, 100.0, 100.0, 100.0, 100.0, 1, HOUR)]
        for i in range(1, research.MAX_HOLD_BARS + 2):
            bars.append(candles.Candle(i * HOUR, 100.0, 100.2, 99.5, 100.0, 1,
                                       (i + 1) * HOUR))
        t = research.simulate_trade(bars, 1)
        self.assertLess(t.pnl_pct, 0)
        self.assertAlmostEqual(t.pnl_pct, -(research.MAKER_FEE + research.TAKER_FEE) * 100,
                               places=6)

    def test_summarize_of_no_trades_is_all_zero(self):
        s = research.summarize([])
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["avg_r"], 0.0)

    def test_grid_skips_ceilings_below_the_cross(self):
        for p in research.build_grid():
            self.assertGreater(p.rsi_ceiling, p.rsi_cross)

    def test_grid_covers_the_documented_dimensions(self):
        combos = research.build_grid()
        self.assertEqual({p.vwap_mode for p in combos}, {"above", "reclaim"})
        self.assertEqual({p.macd_expansion_bars for p in combos}, {2, 3, 4})
        self.assertIn("none", {p.trend_filter for p in combos})

    def test_report_says_so_when_nothing_is_rankable(self):
        c1h = mk([100.0] * 100)
        text = research.render_report("BTCUSDT", 60, c1h, [], [], [])
        self.assertIn("No parameter combination produced at least", text)

    def test_report_flags_a_winner_inside_the_noise_band(self):
        p = signals.SetupBParams()
        near = [(p, {"trades": 50, "win_rate": 50.0, "avg_r": 0.10 - i * 0.0005,
                     "expectancy": 0.1, "total_r": 5.0, "max_dd": 1.0,
                     "profit_factor": 1.1, "stdev_r": 2.0}) for i in range(25)]
        text = research.render_report("BTCUSDT", 365, mk([100.0] * 100), near,
                                      near, [])
        self.assertIn("INSIDE the noise band", text)

    def test_standard_error_shrinks_with_sample_size(self):
        small = research.standard_error({"trades": 10, "stdev_r": 2.0})
        big = research.standard_error({"trades": 1000, "stdev_r": 2.0})
        self.assertGreater(small, big)


class TestNoOrderPathInSignalLayer(unittest.TestCase):
    """AT7: v0.3 must not have added a way to place an order."""

    SIGNAL_MODULES = ("indicators.py", "signals.py", "candles.py", "research.py",
                      "advisor.py")

    FORBIDDEN_CALLS = {"place_order", "place_tp_sl", "cancel_all", "cancel_order",
                       "close_positions", "place_plan", "set_leverage"}

    def test_signal_modules_never_call_an_order_function(self):
        """Parse the AST and look at actual CALLS — a substring scan would trip
        over the word 'place_order' appearing in a docstring that promises the
        opposite."""
        import ast
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "armm")
        for name in self.SIGNAL_MODULES:
            with open(os.path.join(src, name)) as f:
                tree = ast.parse(f.read(), filename=name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                called = (fn.attr if isinstance(fn, ast.Attribute)
                          else fn.id if isinstance(fn, ast.Name) else None)
                self.assertNotIn(called, self.FORBIDDEN_CALLS,
                                 f"{name}:{node.lineno} calls {called}()")

    def test_signal_modules_hold_no_order_endpoint_literal(self):
        """No string constant naming an order endpoint, docstrings excluded."""
        import ast
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "armm")
        for name in self.SIGNAL_MODULES:
            with open(os.path.join(src, name)) as f:
                tree = ast.parse(f.read(), filename=name)
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    d = ast.get_docstring(node, clean=False)
                    if d:
                        docstrings.add(d)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in docstrings:
                        continue
                    self.assertNotIn("/capi/v3/order", node.value,
                                     f"{name}:{node.lineno} embeds an order endpoint")
                    self.assertNotIn("/capi/v3/sim/order", node.value,
                                     f"{name}:{node.lineno} embeds an order endpoint")

    def test_live_order_path_constant_is_unchanged(self):
        from armm import weex_client
        self.assertEqual(weex_client.LIVE_ORDER_PATH, "/capi/v3/order")
        self.assertEqual(weex_client.DEMO_ORDER_PATH, "/capi/v3/sim/order")

    def test_research_declares_itself_offline(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "armm")
        with open(os.path.join(src, "research.py")) as f:
            src = f.read()
        self.assertIn("OFFLINE", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
