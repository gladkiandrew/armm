#!/usr/bin/env python3
"""ARMM signals — Andrew's entry setups, encoded exactly as specified.

v0.3 ADVISES. It never places an order and imports nothing that can: there is
no client.place_order call anywhere in this module or reachable from it.

Setup A — "Reload" (longer-term long). ALL required:
    * 1H RSI(14) < 40
    * price BELOW the lower anchored-VWAP band (aVWAP - K*sigma, K default 1.0)
    * MACD not applicable (explicitly ignored)

Setup B — "Momentum" (short-term leveraged long). ALL 3/3 required:
    * price above (or reclaiming) the anchored VWAP
    * 1H RSI confirming momentum: rising, crossed above `rsi_cross`, not > `rsi_ceiling`
    * MACD histogram positive AND expanding
  plus a higher-timeframe trend filter:
    * 4H EMA20 > EMA50 and EMA20 rising

The Setup B thresholds are the INITIAL rules from the brief and are explicitly
marked for optimisation — research.py sweeps them. They live in SetupBParams so
the harness and the live cockpit evaluate through identical code.
"""
from dataclasses import dataclass, field, asdict

import candles as candles_mod
import indicators as ind
import logsetup

log = logsetup.get("signals")

DEFAULT_K = 1.0
SETUP_A_RSI_MAX = 40.0


@dataclass
class SetupBParams:
    """Tunable Setup B thresholds. research.py sweeps exactly these."""
    rsi_cross: float = 50.0        # RSI must have crossed above this
    rsi_ceiling: float = 70.0      # ...but not be extended beyond this
    rsi_lookback: int = 3          # bars in which the cross must have happened
    macd_expansion_bars: int = 3   # histogram must expand over this many bars
    trend_fast: int = 20           # 4H EMA fast
    trend_slow: int = 50           # 4H EMA slow
    trend_filter: str = "ema"      # "ema" | "none"
    vwap_mode: str = "above"       # "above" (price > aVWAP) | "reclaim"
    reclaim_lookback: int = 5      # bars for a "reclaim" to count

    def label(self):
        t = ("none" if self.trend_filter == "none"
             else f"EMA{self.trend_fast}/{self.trend_slow}")
        return (f"cross{self.rsi_cross:g}/ceil{self.rsi_ceiling:g}/"
                f"exp{self.macd_expansion_bars}/{t}/{self.vwap_mode}")


@dataclass
class Snapshot:
    symbol: str
    price: float = None
    as_of: int = None                  # open_time of the last CLOSED 1H bar
    anchor_ts: int = None
    anchor_index: int = None
    rsi: float = None
    rsi_prev: float = None
    rsi_rising: bool = None
    macd: float = None
    macd_signal: float = None
    macd_hist: float = None
    macd_hist_expanding: bool = None
    vwap: float = None
    vwap_upper: float = None
    vwap_lower: float = None
    vwap_sigma: float = None
    atr: float = None
    realized_vol: float = None
    trend_fast: float = None
    trend_slow: float = None
    trend_up: bool = None
    trend_label: str = ""
    setup_a: bool = False
    setup_b: bool = False
    setup_a_core: bool = False      # the core rule, before the v0.4 vetoes
    setup_b_core: bool = False
    regime: str = None              # "up" | "down" | "flat" (from BTC 4H)
    regime_detail: str = ""
    regime_source: str = "BTCUSDT"
    stabilized: bool = None         # regime-down only: is the fall stabilising?
    stabilized_detail: str = ""
    confirm_30m: bool = None        # does the 30m frame agree with the 1H?
    confirm_30m_detail: str = ""
    rsi_30m: float = None
    reasons_a: list = field(default_factory=list)
    reasons_b: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    bars_1h: int = 0
    bars_4h: int = 0
    error: str = None

    def to_dict(self):
        return asdict(self)

    # ---- rendering ----
    def verdict_line(self):
        a = "yes" if self.setup_a else f"no ({self._first_fail(self.reasons_a)})"
        b = "yes" if self.setup_b else f"no ({self._first_fail(self.reasons_b)})"
        return f"Setup A: {a} · Setup B: {b}"

    @staticmethod
    def _first_fail(reasons):
        for ok, text in reasons:
            if not ok:
                return text
        return "unmet"

    def render(self):
        """The /check readout."""
        if self.error:
            return f"⚠️ {self.symbol}: {self.error}"
        L = [f"🔭 {self.symbol} signal snapshot"]
        if self.price is None:
            L.append("price unavailable")
            return "\n".join(L + [self.verdict_line()])
        # Say WHICH price this is. Indicators run on closed bars only, so this
        # is the last finished 1H close — it will differ from the live ticker
        # mid-hour, and a trader comparing the two deserves to know why.
        when = ""
        if self.as_of:
            import datetime as _dt
            when = (" · last closed 1H bar "
                    + _dt.datetime.fromtimestamp(self.as_of / 1000, _dt.timezone.utc)
                    .strftime("%m-%d %H:%M UTC"))
        L.append(f"close {self.price:,.1f}{when}")
        if self.vwap is not None:
            pos = "above" if self.price > self.vwap else "below"
            band = ""
            if self.vwap_lower is not None and self.vwap_upper is not None:
                band = (f" band {self.vwap_lower:,.1f} … {self.vwap_upper:,.1f}"
                        + (f" σ {self.vwap_sigma:,.1f}"
                           if self.vwap_sigma is not None else ""))
            L.append(f"aVWAP {self.vwap:,.1f} (price {pos}){band}")
        if self.rsi is not None:
            arrow = "↑" if self.rsi_rising else ("↓" if self.rsi_rising is False else "→")
            prev = f" (prev {self.rsi_prev:.1f})" if self.rsi_prev is not None else ""
            L.append(f"RSI(14) 1H {self.rsi:.1f} {arrow}{prev}")
        if self.macd_hist is not None:
            exp = "expanding" if self.macd_hist_expanding else "contracting"
            detail = ""
            if self.macd is not None and self.macd_signal is not None:
                detail = f" (macd {self.macd:+.2f} / signal {self.macd_signal:+.2f})"
            L.append(f"MACD hist {self.macd_hist:+.2f} {exp}{detail}")
        if self.trend_up is not None:
            L.append(f"4H trend {'UP' if self.trend_up else 'DOWN'} ({self.trend_label})")
        if self.regime:
            icon = {"up": "🟩", "down": "🟥", "flat": "🟨"}.get(self.regime, "")
            L.append(f"{icon} regime {self.regime.upper()} ({self.regime_source} 4H) "
                     f"— {self.regime_detail}")
        if self.rsi_30m is not None:
            L.append(f"30m RSI {self.rsi_30m:.1f} — {self.confirm_30m_detail}")
        if self.atr is not None:
            vol = (f" · realized vol {self.realized_vol * 100:.2f}%/bar"
                   if self.realized_vol is not None else "")
            L.append(f"ATR(14) {self.atr:,.1f}{vol}")
        L.append("")
        L.append(self.verdict_line())
        for tag, reasons in (("A", self.reasons_a), ("B", self.reasons_b)):
            for ok, text in reasons:
                L.append(f"  {tag} {'✓' if ok else '✗'} {text}")
        for w in self.warnings:
            L.append(f"⚠️ {w}")
        L.append(f"({self.bars_1h} 1H bars, {self.bars_4h} 4H bars; signals advise — "
                 f"autopilot acts on DEMO only, manual entries stay yours)")
        return "\n".join(L)


# ---------------- anchor discovery ----------------

SWING_WINDOW = 5          # bars either side that a pivot low must undercut
SWING_MIN_RALLY_ATR = 1.5 # rally off the low, in ATR multiples
SWING_MIN_RALLY_PCT = 1.0 # ...with an absolute floor, in percent


def find_swing_low_anchor(candles, window=SWING_WINDOW,
                          min_rally_atr=SWING_MIN_RALLY_ATR,
                          min_rally_pct=SWING_MIN_RALLY_PCT):
    """Most recent SIGNIFICANT swing low, for `/anchor SYMBOL auto`.

    The heuristic, stated plainly so the number on screen is auditable:

      1. A pivot low is a bar whose low is the lowest of the `window` bars on
         each side of it (an 11-bar window by default). Bars within `window` of
         the series end are excluded — their right side has not formed yet, so
         calling them pivots would be look-ahead.
      2. A pivot is SIGNIFICANT if price subsequently rallied off it by at least
         max(1.5 x ATR(14) at that bar, 1.0% of the low). This drops the noise
         wiggles that would otherwise anchor the VWAP to a meaningless dip.
      3. The MOST RECENT significant pivot wins — an anchor is about the move
         you are currently trading, not the best-looking low on the chart.
      4. If nothing qualifies, fall back to the lowest low in the series and say
         so, rather than silently anchoring somewhere arbitrary.

    Returns (open_time_ms, description). Never raises on short input.
    """
    if not candles:
        return None, "no candles"
    if len(candles) < 2 * window + 2:
        c = min(candles, key=lambda x: x.low)
        return c.open_time, f"series too short for pivots — lowest low {c.low:,.1f}"

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr_series = ind.atr(highs, lows, closes, 14)

    for i in range(len(candles) - window - 1, window - 1, -1):
        left = lows[i - window:i]
        right = lows[i + 1:i + window + 1]
        if not left or not right:
            continue
        if lows[i] > min(left) or lows[i] > min(right):
            continue
        rally = max(highs[i + 1:]) - lows[i]
        a = atr_series[i] or 0.0
        threshold = max(a * min_rally_atr, lows[i] * min_rally_pct / 100.0)
        if rally >= threshold:
            pct = rally / lows[i] * 100.0
            return candles[i].open_time, (
                f"swing low {lows[i]:,.1f}, rallied {rally:,.1f} ({pct:.1f}%) "
                f"vs threshold {threshold:,.1f}")

    c = min(candles, key=lambda x: x.low)
    return c.open_time, (f"no pivot cleared the significance threshold — "
                         f"fell back to the lowest low {c.low:,.1f}")


def confirmed_pivot_lows(candles, window=SWING_WINDOW,
                         min_rally_atr=SWING_MIN_RALLY_ATR,
                         min_rally_pct=SWING_MIN_RALLY_PCT):
    """Pivot lows with their CONFIRMATION bar — the causal form of the anchor
    heuristic, for backtests.

    find_swing_low_anchor() judges significance using every bar after the low,
    which is fine live (all of it is already in the past) but is look-ahead
    inside a backtest. Here a pivot at index i is only confirmed at i+window,
    once its right side has actually formed, and its rally is measured over just
    those `window` bars. Nothing about bar i is known before i+window.

    Returns [(confirm_index, pivot_index)], ascending by confirm_index.
    """
    n = len(candles)
    if n < 2 * window + 2:
        return []
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr_series = ind.atr(highs, lows, closes, 14)

    out = []
    for i in range(window, n - window):
        if lows[i] > min(lows[i - window:i]) or lows[i] > min(lows[i + 1:i + window + 1]):
            continue
        rally = max(highs[i + 1:i + window + 1]) - lows[i]
        threshold = max((atr_series[i] or 0.0) * min_rally_atr,
                        lows[i] * min_rally_pct / 100.0)
        if rally >= threshold:
            out.append((i + window, i))
    return out


def rolling_anchor_indices(candles, window=SWING_WINDOW, **kw):
    """Anchor in force at each bar: the latest CONFIRMED pivot low, else bar 0.

    This is what a live operator running `/anchor auto` on a schedule would
    actually have had, which is the only anchor a backtest may honestly use.
    """
    pivots = confirmed_pivot_lows(candles, window=window, **kw)
    out = [0] * len(candles)
    p = 0
    current = 0
    for i in range(len(candles)):
        while p < len(pivots) and pivots[p][0] <= i:
            current = pivots[p][1]
            p += 1
        out[i] = current
    return out


# ---------------- evaluation ----------------
#
# Live and backtest run the SAME decision code. The only difference is that the
# backtest precomputes every indicator series once and then asks for bar i,
# instead of recomputing the whole history per bar (which is O(n^2) and made the
# grid sweep take hours). precompute() + evaluate_at() is the single path;
# evaluate_series() is a thin wrapper on it, so there is no second
# implementation that could drift away from what the cockpit reports.


class Context:
    """Precomputed indicator series for one (candles, anchor, params) triple."""

    __slots__ = ("symbol", "c1h", "c4h", "params", "k", "anchor_index", "anchor_ts",
                 "highs", "lows", "closes", "vols", "rsi", "macd_line", "sig_line",
                 "hist", "vwap", "upper", "lower", "sigma", "atr", "ema_fast4",
                 "ema_slow4", "map4", "warnings", "c30m", "regime", "regime_detail",
                 "regime_source")


def precompute(symbol, c1h, c4h=None, anchor_ts=None, k=DEFAULT_K, params=None,
               anchor_mode="fixed", c30m=None, regime_c4h=None,
               regime_source="BTCUSDT"):
    """Compute every series once. Everything here is causal — series[i] depends
    only on bars <= i — so reading index i is free of look-ahead.

    anchor_mode="fixed"   : one anchor for the whole series (the cockpit; the
                            anchor comes from /anchor and sits in the db).
    anchor_mode="rolling" : the anchor moves to each newly CONFIRMED swing low,
                            which is what a backtest must use — pinning a year
                            of history to bar 0 makes "price above aVWAP" mean
                            "above the yearly VWAP", which is not the rule.
    """
    p = params or SetupBParams()
    ctx = Context()
    ctx.symbol, ctx.c1h, ctx.c4h, ctx.params, ctx.k = symbol, c1h, c4h or [], p, k
    ctx.warnings = []
    ctx.c30m = c30m or []
    # Regime comes from BTC's 4H structure — the whole book follows it. When no
    # separate BTC series is supplied we fall back to this symbol's own 4H,
    # which is exact for BTCUSDT and an approximation for anything else.
    ctx.regime_source = regime_source
    src4h = regime_c4h if regime_c4h else ctx.c4h
    if regime_c4h is None and symbol.upper() not in ("BTCUSDT", "BACKTEST"):
        ctx.regime_source = symbol.upper() + " (own 4H — BTC series not supplied)"
    ctx.regime, ctx.regime_detail = classify_regime(src4h)

    ctx.highs, ctx.lows, ctx.closes, ctx.vols = candles_mod.columns(c1h)
    ctx.rsi = ind.rsi(ctx.closes, 14)
    ctx.macd_line, ctx.sig_line, ctx.hist = ind.macd(ctx.closes)
    ctx.atr = ind.atr(ctx.highs, ctx.lows, ctx.closes, 14)

    if anchor_mode == "rolling":
        idxs = rolling_anchor_indices(c1h)
        ctx.anchor_index = idxs[-1] if idxs else 0
        ctx.anchor_ts = c1h[ctx.anchor_index].open_time if c1h else None
        ctx.vwap, ctx.upper, ctx.lower, ctx.sigma = ind.anchored_vwap_rolling(
            ctx.highs, ctx.lows, ctx.closes, ctx.vols, idxs, k=k)
        return _precompute_trend(ctx, p, c1h)

    anchor_index = 0
    if anchor_ts:
        found = candles_mod.index_at_or_after(c1h, anchor_ts)
        if found is None:
            ctx.warnings.append(
                f"anchor {anchor_ts} is after the last bar — using the series start")
        else:
            anchor_index = found
    if anchor_index >= max(0, len(c1h) - 2):
        ctx.warnings.append("anchor is too recent for a meaningful VWAP band")
        anchor_index = max(0, len(c1h) - 3)
    ctx.anchor_index = anchor_index
    ctx.anchor_ts = c1h[anchor_index].open_time if c1h else None
    ctx.vwap, ctx.upper, ctx.lower, ctx.sigma = ind.anchored_vwap(
        ctx.highs, ctx.lows, ctx.closes, ctx.vols, anchor_index=anchor_index, k=k)
    return _precompute_trend(ctx, p, c1h)


def _precompute_trend(ctx, p, c1h):
    """4H trend series + a map from each 1H bar to the last CLOSED 4H bar, so a
    backtest never reads a 4H candle that had not finished yet."""
    ctx.ema_fast4 = ctx.ema_slow4 = None
    ctx.map4 = []
    if ctx.c4h and p.trend_filter != "none":
        closes4 = [c.close for c in ctx.c4h]
        ctx.ema_fast4 = ind.ema(closes4, p.trend_fast)
        ctx.ema_slow4 = ind.ema(closes4, p.trend_slow)
        j = -1
        for c in c1h:
            while j + 1 < len(ctx.c4h) and ctx.c4h[j + 1].close_time <= c.close_time:
                j += 1
            ctx.map4.append(j)
    return ctx


def evaluate_at(ctx, i, explain=True):
    """Snapshot as of 1H bar index `i` (inclusive). Same code the cockpit runs.

    `explain=False` skips reason strings and the descriptive-only extras (ATR,
    realized vol). The setup booleans are computed identically either way.
    """
    p = ctx.params
    snap = Snapshot(symbol=ctx.symbol)
    if i < 0 or i >= len(ctx.c1h):
        snap.error = f"bar index {i} outside 0..{len(ctx.c1h) - 1}"
        return snap
    if i < 59:
        snap.error = f"need at least 60 1H bars, have {i + 1}"
        return snap

    snap.bars_1h, snap.bars_4h = i + 1, len(ctx.c4h)
    if explain:
        snap.warnings = list(ctx.warnings)
    snap.price = ctx.closes[i]
    snap.as_of = ctx.c1h[i].open_time
    snap.anchor_index, snap.anchor_ts = ctx.anchor_index, ctx.anchor_ts
    snap.regime, snap.regime_detail = ctx.regime, ctx.regime_detail
    snap.regime_source = ctx.regime_source

    snap.rsi = ctx.rsi[i]
    snap.rsi_prev = ctx.rsi[i - 1] if i > 0 else None
    if snap.rsi is not None and snap.rsi_prev is not None:
        snap.rsi_rising = snap.rsi > snap.rsi_prev

    snap.macd, snap.macd_signal, snap.macd_hist = (
        ctx.macd_line[i], ctx.sig_line[i], ctx.hist[i])
    if snap.macd_hist is not None:
        n = p.macd_expansion_bars
        window = ctx.hist[max(0, i - n):i + 1]
        snap.macd_hist_expanding = (
            len(window) == n + 1 and all(v is not None for v in window)
            and all(window[j] < window[j + 1] for j in range(len(window) - 1)))

    snap.vwap, snap.vwap_upper = ctx.vwap[i], ctx.upper[i]
    snap.vwap_lower, snap.vwap_sigma = ctx.lower[i], ctx.sigma[i]
    if explain:                        # descriptive only — never gates a setup
        snap.atr = ctx.atr[i]
        snap.realized_vol = ind.realized_volatility(ctx.closes[max(0, i - 200):i + 1])

    snap.trend_label = ("disabled" if p.trend_filter == "none"
                        else f"EMA{p.trend_fast} vs EMA{p.trend_slow}")
    if p.trend_filter == "none":
        snap.trend_up = True
    elif ctx.ema_fast4 is None:
        if explain:
            snap.warnings.append("no 4H candles supplied — trend filter unevaluated")
    else:
        j = ctx.map4[i] if i < len(ctx.map4) else -1
        if j < 1 or ctx.ema_fast4[j] is None or ctx.ema_slow4[j] is None:
            if explain:
                snap.warnings.append(
                    f"not enough closed 4H history for EMA{p.trend_slow}")
        else:
            snap.trend_fast, snap.trend_slow = ctx.ema_fast4[j], ctx.ema_slow4[j]
            prev = ctx.ema_fast4[j - 1]
            snap.trend_up = bool(ctx.ema_fast4[j] > ctx.ema_slow4[j]
                                 and prev is not None and ctx.ema_fast4[j] > prev)

    _apply_setup_a(snap, explain=explain)
    rsi_w = [v for v in ctx.rsi[max(0, i - p.rsi_lookback):i + 1] if v is not None]
    n = p.reclaim_lookback
    close_w = ctx.closes[max(0, i - n + 1):i + 1]
    vwap_w = ctx.vwap[max(0, i - n + 1):i + 1]
    _apply_setup_b(snap, p, rsi_w, close_w, vwap_w, explain=explain)
    _apply_v04_vetoes(snap, p, ctx.closes[:i + 1], ctx.lower[:i + 1],
                      ctx.rsi[:i + 1], ctx.c30m, explain=explain)
    return snap


def evaluate_series(symbol, c1h, c4h, anchor_ts=None, k=DEFAULT_K, params=None,
                    upto=None, c30m=None, regime_c4h=None):
    """Pure evaluation over candle lists. No network — this is what tests drive.

    `upto` (exclusive index into c1h) evaluates the series as it looked then.
    """
    if upto is not None:
        c1h = c1h[:upto]
    if len(c1h) < 60:
        s = Snapshot(symbol=symbol)
        s.error = f"need at least 60 1H bars, have {len(c1h)}"
        return s
    ctx = precompute(symbol, c1h, c4h, anchor_ts=anchor_ts, k=k, params=params,
                     c30m=c30m, regime_c4h=regime_c4h)
    return evaluate_at(ctx, len(c1h) - 1)


# ---------------- v0.4 entry vetoes ----------------
#
# These are ENTRY-TIME gates layered on top of the core setups. They apply to
# every entry, manual or autopilot. research.py backtests the CORE rules only,
# so a live signal count will be lower than a backtested one — the report says so.

REGIME_UP, REGIME_DOWN, REGIME_FLAT = "up", "down", "flat"
STABILIZE_RSI_BARS = 2       # consecutive rising RSI bars that count as stabilising


def classify_regime(c4h, fast=20, slow=50):
    """Market regime from a 4H EMA structure. Returns (regime, detail).

    up   : EMA20 > EMA50 and EMA20 rising — structure intact
    down : EMA20 < EMA50 — structure broken
    flat : EMA20 > EMA50 but rolling over — no longer confirming
    """
    if not c4h or len(c4h) < slow + 2:
        return None, f"insufficient 4H history ({len(c4h or [])} bars, need {slow + 2})"
    closes = [c.close for c in c4h]
    ef, es = ind.ema(closes, fast), ind.ema(closes, slow)
    if ef[-1] is None or es[-1] is None or ef[-2] is None:
        return None, "4H EMAs undefined"
    rising = ef[-1] > ef[-2]
    if ef[-1] < es[-1]:
        return REGIME_DOWN, (f"EMA{fast} {ef[-1]:,.1f} < EMA{slow} {es[-1]:,.1f} "
                             f"— structure broken")
    if rising:
        return REGIME_UP, (f"EMA{fast} {ef[-1]:,.1f} > EMA{slow} {es[-1]:,.1f} and rising")
    return REGIME_FLAT, (f"EMA{fast} {ef[-1]:,.1f} > EMA{slow} {es[-1]:,.1f} "
                         f"but rolling over")


def assess_stabilization(closes, lower_band_series, rsi_series,
                         bars=STABILIZE_RSI_BARS):
    """Is a fall stabilising? Returns (bool, detail).

    Two accepted forms, per the v0.4 brief:
      (a) RECLAIM — the previous closed bar was below the lower band and the
          latest one closed back inside it.
      (b) RSI TURN — RSI has risen for `bars` consecutive bars.

    NOTE ON (a): Setup A's own core rule requires the latest close to be BELOW
    the lower band, so (a) and Setup A can never both be true at the same instant
    — a reclaim means the dip is over. In regime-down, (b) is therefore the
    operative test for Setup A. (a) is implemented and reported anyway because it
    is the honest reading of the brief and it is the condition that would matter
    if the band rule is ever relaxed to "traded below" rather than "closed below".
    """
    reclaim = False
    if len(closes) >= 2 and len(lower_band_series) >= 2:
        prev_lb, cur_lb = lower_band_series[-2], lower_band_series[-1]
        if prev_lb is not None and cur_lb is not None:
            reclaim = closes[-2] < prev_lb and closes[-1] >= cur_lb
    if reclaim:
        return True, "reclaim: previous bar closed below the band, this one closed back inside"

    defined = [v for v in rsi_series if v is not None]
    if len(defined) >= bars + 1:
        turn = all(defined[-(i + 1)] > defined[-(i + 2)] for i in range(bars))
        if turn:
            return True, f"RSI has risen {bars} bars running ({defined[-bars - 1]:.1f} → {defined[-1]:.1f})"
    return False, f"no reclaim and RSI has not risen {bars} bars running"


def assess_30m(snap, c30m, params):
    """Does the 30m frame agree with the 1H setup? Returns (ok, detail, rsi30).

    Exact rules:
      Setup A — the 30m must ALSO be oversold: 30m RSI(14) < 40. A 1H reload
                while the 30m is still mid-range means the flush is unfinished.
      Setup B — the 30m must merely NOT CONTRADICT: 30m RSI <= the Setup B
                ceiling (not already extended on the faster frame) AND 30m MACD
                histogram >= 0 (the faster frame is not rolling over).
    A missing 30m series is reported as unknown and does NOT silently pass.
    """
    if not c30m or len(c30m) < 60:
        return None, f"30m frame unavailable ({len(c30m or [])} bars)", None
    closes30 = [c.close for c in c30m]
    rsi30 = ind.rsi(closes30, 14)[-1]
    _, _, hist30 = ind.macd(closes30)
    h30 = hist30[-1]
    if rsi30 is None:
        return None, "30m RSI undefined", None

    if snap.setup_a_core:
        ok = rsi30 < SETUP_A_RSI_MAX
        return ok, (f"30m RSI {rsi30:.1f} "
                    f"{'is' if ok else 'is NOT'} below {SETUP_A_RSI_MAX:g} "
                    f"(Setup A needs the 30m oversold too)"), rsi30
    if snap.setup_b_core:
        not_extended = rsi30 <= params.rsi_ceiling
        not_rolling = h30 is not None and h30 >= 0
        ok = bool(not_extended and not_rolling)
        bits = []
        if not not_extended:
            bits.append(f"30m RSI {rsi30:.1f} extended (> {params.rsi_ceiling:g})")
        if not not_rolling:
            bits.append(f"30m MACD hist {h30:+.2f} negative")
        return ok, ("30m does not contradict the 1H" if ok
                    else "30m contradicts: " + ", ".join(bits)), rsi30
    return None, f"30m RSI {rsi30:.1f} (no 1H setup to confirm)", rsi30


def _apply_v04_vetoes(snap, params, closes, lower_band_series, rsi_series, c30m,
                      explain=True):
    """Layer the regime + stabilization + 30m gates onto the core setups."""
    snap.setup_a_core, snap.setup_b_core = snap.setup_a, snap.setup_b

    # --- Setup B: blocked outright in a broken regime ---
    if snap.setup_b and snap.regime == REGIME_DOWN:
        snap.setup_b = False
        if explain:
            snap.reasons_b.append(
                (False, f"REGIME VETO: {snap.regime_source} 4H structure is broken "
                        f"({snap.regime_detail}) — Setup B is blocked outright"))

    # --- Setup A: needs stabilization in a broken regime ---
    if snap.setup_a and snap.regime == REGIME_DOWN:
        ok, detail = assess_stabilization(closes, lower_band_series, rsi_series)
        snap.stabilized, snap.stabilized_detail = ok, detail
        if not ok:
            snap.setup_a = False
        if explain:
            snap.reasons_a.append(
                (ok, f"regime is DOWN so Setup A needs stabilisation — {detail}"))

    # --- both: the 30m frame must justify the entry ---
    ok30, detail30, rsi30 = assess_30m(snap, c30m, params)
    snap.confirm_30m, snap.confirm_30m_detail, snap.rsi_30m = ok30, detail30, rsi30
    if ok30 is False:
        if snap.setup_a:
            snap.setup_a = False
            if explain:
                snap.reasons_a.append((False, f"30m FRAME: {detail30}"))
        if snap.setup_b:
            snap.setup_b = False
            if explain:
                snap.reasons_b.append((False, f"30m FRAME: {detail30}"))
    elif ok30 is None and (snap.setup_a_core or snap.setup_b_core):
        # Unknown is not permission. An entry needs positive confirmation.
        if snap.setup_a:
            snap.setup_a = False
            if explain:
                snap.reasons_a.append((False, f"30m FRAME unavailable: {detail30}"))
        if snap.setup_b:
            snap.setup_b = False
            if explain:
                snap.reasons_b.append((False, f"30m FRAME unavailable: {detail30}"))


def _apply_setup_a(snap, explain=True):
    """Reload: RSI < 40 AND price below the lower band. MACD ignored by design.

    The booleans below are the ONLY decision logic; `explain` controls whether we
    also build the human-readable reason strings. The backtest turns it off
    because formatting eight strings per bar dominated the sweep's runtime — it
    changes no verdict, only whether the verdict can narrate itself.
    """
    ok_rsi = snap.rsi is not None and snap.rsi < SETUP_A_RSI_MAX
    ok_band = (snap.vwap_lower is not None and snap.price is not None
               and snap.price < snap.vwap_lower)
    snap.setup_a = bool(ok_rsi and ok_band)

    if not explain:
        return
    reasons = []
    if snap.rsi is None:
        reasons.append((False, "RSI unavailable"))
    else:
        reasons.append((ok_rsi, f"1H RSI {snap.rsi:.1f} is "
                                f"{'below' if ok_rsi else 'NOT below'} {SETUP_A_RSI_MAX:g}"))
    if snap.vwap_lower is None:
        reasons.append((False, "aVWAP band unavailable"))
    else:
        reasons.append((ok_band, f"price {snap.price:,.1f} is "
                                 f"{'below' if ok_band else 'NOT below'} the lower band "
                                 f"{snap.vwap_lower:,.1f}"))
    reasons.append((True, "MACD not applicable to Setup A"))
    snap.reasons_a = reasons


def _apply_setup_b(snap, p, rsi_window, closes, vwap_series, explain=True):
    """Momentum: aVWAP + RSI + MACD, all three, plus the 4H trend filter."""
    # ---- decision ----
    was_below = None
    if snap.vwap is None or snap.price is None:
        ok_vwap = False
    elif p.vwap_mode == "reclaim":
        was_below = any(c < v for c, v in zip(closes, vwap_series) if v is not None)
        ok_vwap = snap.price > snap.vwap and was_below
    else:
        ok_vwap = snap.price > snap.vwap

    crossed = False
    if snap.rsi is None:
        ok_rsi = False
    else:
        crossed = ind.crossed_above(rsi_window, p.rsi_cross, p.rsi_lookback)
        ok_rsi = bool(snap.rsi > p.rsi_cross and (crossed or snap.rsi_rising)
                      and snap.rsi <= p.rsi_ceiling)

    ok_macd = bool(snap.macd_hist is not None and snap.macd_hist > 0
                   and snap.macd_hist_expanding)
    ok_trend = bool(snap.trend_up)

    snap.setup_b = bool(ok_vwap and ok_rsi and ok_macd and ok_trend)
    if not explain:
        return

    # ---- narration ----
    reasons = []
    if snap.vwap is None:
        reasons.append((False, "aVWAP unavailable"))
    elif p.vwap_mode == "reclaim":
        if ok_vwap:
            detail = (f"price {snap.price:,.1f} is reclaiming aVWAP {snap.vwap:,.1f} "
                      f"(was below within {p.reclaim_lookback} bars)")
        elif snap.price <= snap.vwap:
            detail = f"price {snap.price:,.1f} is NOT above aVWAP {snap.vwap:,.1f}"
        else:
            detail = (f"price {snap.price:,.1f} is above aVWAP {snap.vwap:,.1f} but was "
                      f"never below it in the last {p.reclaim_lookback} bars (no reclaim)")
        reasons.append((ok_vwap, detail))
    else:
        reasons.append((ok_vwap, f"price {snap.price:,.1f} is "
                                 f"{'above' if ok_vwap else 'NOT above'} aVWAP "
                                 f"{snap.vwap:,.1f}"))

    if snap.rsi is None:
        reasons.append((False, "RSI unavailable"))
    else:
        if snap.rsi > p.rsi_ceiling:
            detail = f"RSI {snap.rsi:.1f} is EXTENDED (> {p.rsi_ceiling:g})"
        elif snap.rsi <= p.rsi_cross:
            detail = f"RSI {snap.rsi:.1f} has not cleared {p.rsi_cross:g}"
        elif not (crossed or snap.rsi_rising):
            detail = f"RSI {snap.rsi:.1f} above {p.rsi_cross:g} but not rising"
        else:
            detail = (f"RSI {snap.rsi:.1f} > {p.rsi_cross:g}, rising, "
                      f"not extended (<= {p.rsi_ceiling:g})")
        reasons.append((ok_rsi, detail))

    if snap.macd_hist is None:
        reasons.append((False, "MACD unavailable"))
    elif snap.macd_hist <= 0:
        reasons.append((ok_macd, f"MACD hist {snap.macd_hist:+.2f} not positive"))
    elif not snap.macd_hist_expanding:
        reasons.append((ok_macd, f"MACD hist {snap.macd_hist:+.2f} positive but contracting"))
    else:
        reasons.append((ok_macd, f"MACD hist {snap.macd_hist:+.2f} positive and expanding"))

    if snap.trend_up is None:
        reasons.append((False, f"4H trend unevaluated ({snap.trend_label})"))
    else:
        reasons.append((ok_trend, f"4H trend {'up' if snap.trend_up else 'down'} "
                                  f"({snap.trend_label})"))
    snap.reasons_b = reasons


# ---------------- network-facing convenience ----------------

REGIME_SYMBOL = "BTCUSDT"


def evaluate(symbol, anchor_ts=None, client=None, k=DEFAULT_K, params=None,
             bars_1h=400, bars_4h=200, bars_30m=200):
    """Fetch candles and evaluate. Read-only: klines are public market data.

    Pulls four series: the symbol's 1H (the setup frame), its 4H (its own trend
    filter), its 30m (the v0.4 justification frame), and BTC's 4H (the market
    regime every symbol is judged against).
    """
    from weex_client import WeexClient, WeexError
    client = client or WeexClient(demo=True)
    try:
        c1h = candles_mod.recent(client, symbol, "1h", limit=bars_1h)
        c4h = candles_mod.recent(client, symbol, "4h", limit=bars_4h)
    except (WeexError, candles_mod.CandleError) as e:
        s = Snapshot(symbol=symbol)
        s.error = f"could not load candles: {e}"
        return s

    c30m = []
    try:
        c30m = candles_mod.recent(client, symbol, "30m", limit=bars_30m)
    except (WeexError, candles_mod.CandleError) as e:
        log.warning("30m frame unavailable for %s: %s", symbol, e)

    regime_c4h = None
    if symbol.upper() != REGIME_SYMBOL:
        try:
            regime_c4h = candles_mod.recent(client, REGIME_SYMBOL, "4h", limit=bars_4h)
        except (WeexError, candles_mod.CandleError) as e:
            log.warning("BTC regime series unavailable: %s", e)

    return evaluate_series(symbol, c1h, c4h, anchor_ts=anchor_ts, k=k, params=params,
                           c30m=c30m, regime_c4h=regime_c4h)
