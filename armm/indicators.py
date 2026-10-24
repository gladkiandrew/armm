#!/usr/bin/env python3
"""ARMM indicators — pure functions over price series. ZERO network, zero state.

Everything here is deterministic and unit-tested. Nothing in this module knows
what a WEEX kline is: callers pass plain lists of floats (or Candle tuples from
candles.py). That keeps the signal maths testable against published reference
values instead of against whatever the venue happened to return today.

Conventions:
  * Series are ASCENDING in time (oldest first). candles.normalize() guarantees it.
  * Functions return a list the SAME LENGTH as the input, left-padded with None
    where the indicator is not yet defined. Callers read [-1] for "now" and can
    index back safely without an offset bug.
"""
import math

__all__ = ["sma", "ema", "wilder_smooth", "rsi", "macd", "true_range", "atr",
           "anchored_vwap", "anchored_vwap_rolling", "stdev", "realized_volatility", "slope", "rising",
           "typical_price", "crossed_above", "IndicatorError"]


class IndicatorError(Exception):
    pass


def _clean(values, name="series"):
    out = [float(v) for v in values]
    if any(math.isnan(v) or math.isinf(v) for v in out):
        raise IndicatorError(f"{name} contains NaN/inf")
    return out


# ---------------- averages ----------------

def sma(values, period):
    """Simple moving average, left-padded with None."""
    if period <= 0:
        raise IndicatorError("period must be positive")
    vals = _clean(values)
    out = [None] * len(vals)
    if len(vals) < period:
        return out
    running = sum(vals[:period])
    out[period - 1] = running / period
    for i in range(period, len(vals)):
        running += vals[i] - vals[i - period]
        out[i] = running / period
    return out


def ema(values, period):
    """Exponential moving average, seeded with the SMA of the first `period`
    values (the TA-Lib / StockCharts convention), left-padded with None."""
    if period <= 0:
        raise IndicatorError("period must be positive")
    vals = _clean(values)
    out = [None] * len(vals)
    if len(vals) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(vals[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(vals)):
        prev = (vals[i] - prev) * k + prev
        out[i] = prev
    return out


def wilder_smooth(values, period):
    """Wilder's smoothing: avg = (prev*(n-1) + x) / n. Seeded with the SMA.

    This is NOT the same as ema(period); Wilder's is equivalent to an EMA with
    k = 1/n rather than 2/(n+1). RSI and ATR both use this one.
    """
    if period <= 0:
        raise IndicatorError("period must be positive")
    vals = _clean(values)
    out = [None] * len(vals)
    if len(vals) < period:
        return out
    prev = sum(vals[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(vals)):
        prev = (prev * (period - 1) + vals[i]) / period
        out[i] = prev
    return out


# ---------------- momentum ----------------

def rsi(closes, period=14):
    """Wilder's RSI. Matches the published reference values in his book.

    Returns a list aligned to `closes`; the first defined value sits at index
    `period` (it needs `period` price CHANGES, i.e. period+1 closes).
    """
    vals = _clean(closes, "closes")
    out = [None] * len(vals)
    if len(vals) <= period:
        return out

    gains, losses = [], []
    for i in range(1, len(vals)):
        change = vals[i] - vals[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0   # flat series is neutral, not 100
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram — each aligned to `closes`.

    The signal EMA is seeded from the first `signal` DEFINED macd values, so the
    histogram starts at index (slow-1) + (signal-1), not at slow-1.
    """
    if fast >= slow:
        raise IndicatorError("fast period must be shorter than slow")
    vals = _clean(closes, "closes")
    n = len(vals)
    ef, es = ema(vals, fast), ema(vals, slow)
    macd_line = [None] * n
    for i in range(n):
        if ef[i] is not None and es[i] is not None:
            macd_line[i] = ef[i] - es[i]

    defined = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line, hist = [None] * n, [None] * n
    if len(defined) >= signal:
        sig_vals = ema([v for _, v in defined], signal)
        for (idx, _), s in zip(defined, sig_vals):
            if s is not None:
                signal_line[idx] = s
                hist[idx] = macd_line[idx] - s
    return macd_line, signal_line, hist


# ---------------- volatility ----------------

def true_range(highs, lows, closes):
    h, l, c = _clean(highs), _clean(lows), _clean(closes)
    if not (len(h) == len(l) == len(c)):
        raise IndicatorError("high/low/close series must be the same length")
    out = [None] * len(h)
    if h:
        out[0] = h[0] - l[0]
    for i in range(1, len(h)):
        out[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return out


def atr(highs, lows, closes, period=14):
    tr = true_range(highs, lows, closes)
    return wilder_smooth([v for v in tr], period)


def stdev(values, sample=True):
    """Standard deviation of a whole list. sample=True -> n-1 denominator."""
    vals = _clean(values)
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / ((n - 1) if sample else n)
    return math.sqrt(var)


def realized_volatility(closes, window=None, periods_per_year=None):
    """Std dev of log returns. Annualized when periods_per_year is given."""
    vals = _clean(closes, "closes")
    if len(vals) < 3:
        return 0.0
    rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals))
            if vals[i - 1] > 0 and vals[i] > 0]
    if window:
        rets = rets[-window:]
    if len(rets) < 2:
        return 0.0
    sd = stdev(rets)
    return sd * math.sqrt(periods_per_year) if periods_per_year else sd


# ---------------- anchored VWAP ----------------

def typical_price(high, low, close):
    return (float(high) + float(low) + float(close)) / 3.0


def anchored_vwap(highs, lows, closes, volumes, anchor_index=0, k=1.0):
    """Anchored VWAP with sigma bands, cumulative from `anchor_index`.

    band = vwap +/- k * sigma, where sigma is the running standard deviation of
    the TYPICAL PRICE since the anchor (the definition the ARMM brief specifies
    — note it is a price dispersion measure, not a volume-weighted variance).

    Returns (vwap, upper, lower, sigma), each aligned to the input series with
    None before the anchor.
    """
    h, l, c, v = _clean(highs), _clean(lows), _clean(closes), _clean(volumes)
    n = len(h)
    if not (len(l) == len(c) == len(v) == n):
        raise IndicatorError("OHLCV series must be the same length")
    if not (0 <= anchor_index < n):
        raise IndicatorError(f"anchor_index {anchor_index} outside 0..{n - 1}")

    vwap = [None] * n
    upper = [None] * n
    lower = [None] * n
    sig = [None] * n

    pv_sum = 0.0
    vol_sum = 0.0
    tps = []
    for i in range(anchor_index, n):
        tp = typical_price(h[i], l[i], c[i])
        tps.append(tp)
        pv_sum += tp * v[i]
        vol_sum += v[i]
        # Zero-volume bars would otherwise divide by zero; fall back to the mean
        # typical price, which is what a VWAP degenerates to without volume.
        vwap[i] = (pv_sum / vol_sum) if vol_sum > 0 else (sum(tps) / len(tps))
        s = stdev(tps) if len(tps) > 1 else 0.0
        sig[i] = s
        upper[i] = vwap[i] + k * s
        lower[i] = vwap[i] - k * s
    return vwap, upper, lower, sig


def anchored_vwap_rolling(highs, lows, closes, volumes, anchor_indices, k=1.0):
    """Anchored VWAP where the anchor MOVES over time — one O(n) forward pass.

    `anchor_indices[i]` is the anchor in force at bar i. Whenever it changes the
    accumulators reset, so this is exactly anchored_vwap() re-run from the new
    anchor, without the O(n^2) of actually re-running it per bar.

    Sigma uses Welford's online algorithm rather than sum-of-squares: at BTC
    price magnitudes the naive form loses most of its significant digits to
    catastrophic cancellation.
    """
    h, l, c, v = _clean(highs), _clean(lows), _clean(closes), _clean(volumes)
    n = len(h)
    if not (len(l) == len(c) == len(v) == len(anchor_indices) == n):
        raise IndicatorError("OHLCV and anchor series must be the same length")

    vwap = [None] * n
    upper = [None] * n
    lower = [None] * n
    sig = [None] * n

    current = object()
    pv = vol = 0.0
    count = 0
    mean = m2 = 0.0
    tp_sum = 0.0
    for i in range(n):
        a = anchor_indices[i]
        if a != current:                       # anchor moved: start again from it
            current = a
            pv = vol = tp_sum = 0.0
            count = 0
            mean = m2 = 0.0
            for j in range(a, i):
                tp = typical_price(h[j], l[j], c[j])
                pv += tp * v[j]
                vol += v[j]
                tp_sum += tp
                count += 1
                d = tp - mean
                mean += d / count
                m2 += d * (tp - mean)
        tp = typical_price(h[i], l[i], c[i])
        pv += tp * v[i]
        vol += v[i]
        tp_sum += tp
        count += 1
        d = tp - mean
        mean += d / count
        m2 += d * (tp - mean)

        vwap[i] = (pv / vol) if vol > 0 else (tp_sum / count)
        s = math.sqrt(m2 / (count - 1)) if count > 1 else 0.0
        sig[i] = s
        upper[i] = vwap[i] + k * s
        lower[i] = vwap[i] - k * s
    return vwap, upper, lower, sig


# ---------------- shape helpers ----------------

def slope(series, lookback=3):
    """Change across the last `lookback` DEFINED values. None if insufficient."""
    vals = [v for v in series if v is not None]
    if len(vals) < lookback + 1:
        return None
    return vals[-1] - vals[-1 - lookback]


def rising(series, lookback=3):
    s = slope(series, lookback)
    return None if s is None else s > 0


def crossed_above(series, level, lookback=3):
    """True if the series was <= level within the lookback and is now > level."""
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return False
    if vals[-1] <= level:
        return False
    return any(v <= level for v in vals[-(lookback + 1):-1])
