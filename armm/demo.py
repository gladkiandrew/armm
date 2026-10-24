#!/usr/bin/env python3
"""ARMM offline demo — `python3 -m armm.demo`. No keys, no network, no orders.

Three things, using the same code the live bot runs:
  1. The risk gate (guards.entry_violations) judging six proposed entries.
  2. Setup B backtested over a year of SYNTHETIC 1H candles, with the same
     signal path, trade simulator and statistics research.py uses.
  3. The honesty check: is the result distinguishable from noise?

The candles are a seeded random walk with trending and choppy regimes. They are
not market data and the numbers say nothing about real performance; they exist
so anyone can run the full pipeline end to end in a few seconds.
"""
import math
import random
import time

from . import guards, research, signals
from .candles import Candle

HOUR_MS = 3_600_000


def synthetic_candles(days=365, seed=7, start_price=60_000.0):
    """Seeded GBM with regime switches every ~2 weeks. Deterministic."""
    rng = random.Random(seed)
    t0 = 1_735_689_600_000                       # 2025-01-01 UTC
    price, out = start_price, []
    drift, vol = 0.0, 0.006
    for i in range(days * 24):
        if i % (24 * 14) == 0:                   # new regime
            drift = rng.choice([-0.0004, 0.0, 0.0003, 0.0006])
            vol = rng.choice([0.004, 0.006, 0.009])
        ret = drift + vol * rng.gauss(0, 1)
        o = price
        c = o * math.exp(ret)
        wick = abs(rng.gauss(0, vol / 2))
        h, l = max(o, c) * (1 + wick), min(o, c) * (1 - wick)
        v = rng.uniform(800, 1600) * (1 + 40 * abs(ret))
        out.append(Candle(t0 + i * HOUR_MS, o, h, l, c, v, t0 + (i + 1) * HOUR_MS - 1))
        price = c
    return out


def aggregate(c1h, hours=4):
    out = []
    for i in range(0, len(c1h) - hours + 1, hours):
        b = c1h[i:i + hours]
        out.append(Candle(b[0].open_time, b[0].open, max(x.high for x in b),
                          min(x.low for x in b), b[-1].close,
                          sum(x.volume for x in b), b[-1].close_time))
    return out


def show_guards():
    print("1) RISK GATE — every entry path calls this before an order leaves the process")
    now = time.time()
    cases = [
        ("clean long",            dict(notional=500,  leverage=3,  stop_price=77_400, entry_price=78_200, side="LONG")),
        ("oversized",             dict(notional=5000, leverage=3,  stop_price=77_400, entry_price=78_200, side="LONG")),
        ("too much leverage",     dict(notional=500,  leverage=20, stop_price=77_400, entry_price=78_200, side="LONG")),
        ("no stop",               dict(notional=500,  leverage=3,  stop_price=None,   entry_price=78_200, side="LONG")),
        ("chasing the move",      dict(notional=500,  leverage=3,  stop_price=79_000, entry_price=80_000, side="LONG",
                                       reference_price=78_000)),
        ("revenge trade",         dict(notional=500,  leverage=3,  stop_price=77_400, entry_price=78_200, side="LONG",
                                       last_close_ts=now - 300, now=now)),
    ]
    for name, kw in cases:
        errs = guards.entry_violations("BTCUSDT", **kw)
        verdict = "ALLOW" if not errs else "REFUSE  " + " | ".join(errs)
        print(f"   {name:<18} {verdict}")
    print()


def show_backtest():
    print("2) SETUP B BACKTEST — synthetic data, real decision code")
    c1h = synthetic_candles()
    c4h = aggregate(c1h)
    params = signals.SetupBParams()
    trades = research.backtest(c1h, c4h, params)
    s = research.summarize(trades)
    se = research.standard_error(s)
    print(f"   params        {params.label()}")
    print(f"   bars          {len(c1h)} x 1H, {len(c4h)} x 4H")
    print(f"   trades        {s['trades']}")
    if s["trades"]:
        pf = s["profit_factor"]
        print(f"   win rate      {s['win_rate']:.1f}%")
        print(f"   expectancy    {s['expectancy']:+.3f} R per trade (± {se:.3f} SE)")
        print(f"   total         {s['total_r']:+.2f} R, max drawdown {s['max_dd']:.2f} R")
        print(f"   profit factor {'inf' if pf == float('inf') else f'{pf:.2f}'}")
        exits = {}
        for t in trades:
            exits[t.reason] = exits.get(t.reason, 0) + 1
        print(f"   exits         " + ", ".join(f"{k}: {v}" for k, v in sorted(exits.items())))
    print()
    return s, se


def show_verdict(s, se):
    print("3) IS IT REAL?")
    if not s["trades"]:
        print("   no trades — nothing to judge")
    elif abs(s["expectancy"]) <= 2 * se:
        print(f"   |expectancy| {abs(s['expectancy']):.3f} R is inside 2 SE ({2 * se:.3f} R): "
              "indistinguishable from noise. Don't trade it, don't tune it.")
    else:
        print(f"   expectancy clears 2 SE — on synthetic data that's luck in the seed, "
              "which is exactly why research.py demands out-of-sample confirmation.")


def main():
    print("ARMM offline demo — no keys, no network, no orders\n")
    show_guards()
    s, se = show_backtest()
    show_verdict(s, se)


if __name__ == "__main__":
    main()
