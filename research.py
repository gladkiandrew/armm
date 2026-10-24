#!/usr/bin/env python3
"""ARMM research harness — Setup B parameter discovery. OFFLINE, READ-ONLY.

This module imports no order path and calls no private endpoint. It pulls public
klines, replays Setup B across a parameter grid, and writes a ranked report into
the research folder for Andrew to read. It cannot place an order; `make check` asserts it.

Fill model — deliberately pessimistic, because a backtest that flatters itself
is worse than none:
  * A signal on bar i is entered at bar i+1's OPEN. No same-bar fills, no
    "we'd have got the low" luck.
  * Stop and targets are checked against each subsequent bar's HIGH/LOW. When a
    bar's range covers both the stop and a target, the STOP is taken first — the
    pessimistic assumption, since intrabar order is unknowable from OHLC.
  * Tranche exits at +3/+4/+5% (the brief's ladder) with a 1% stop, sized
    33/33/34. R is measured against the 1% stop distance.
  * Fees are charged at the WEEX maker/taker rates on both sides.

Honest-stats rules (also from the brief): a combo needs MIN_TRADES to be ranked
at all, the report shows the top 5 AND the spread across the whole grid, and if
the winner is inside the noise band of the field that is stated in the report
rather than buried.

Usage:
  python3 research.py --symbol BTCUSDT --days 120
  python3 research.py --symbol BTCUSDT --days 120 --out /tmp/report.md
"""
import argparse, datetime, itertools, math, os, statistics, sys

import candles as candles_mod
import logsetup
import signals

log = logsetup.get("research")

VAULT_RESEARCH = os.path.expanduser(os.path.join(os.environ.get("ARMM_HOME", "~/armm"), "research"))

MIN_TRADES = 30            # brief: fewer than this and a combo is not ranked
TARGETS_PCT = (3.0, 4.0, 5.0)
SPLITS = (33.0, 33.0, 34.0)
STOP_PCT = 1.0
MAKER_FEE = 0.0002
TAKER_FEE = 0.0008
MAX_HOLD_BARS = 72         # 3 days on 1H; an unresolved trade is closed at market

GRID = {
    "rsi_cross": [45.0, 50.0, 55.0],
    "rsi_ceiling": [65.0, 70.0, 75.0],
    "macd_expansion_bars": [2, 3, 4],
    "trend": [("ema", 20, 50), ("ema", 50, 100), ("none", 0, 0)],
    "vwap_mode": ["above", "reclaim"],
}


# ---------------- trade simulation ----------------

class Trade:
    __slots__ = ("entry_i", "entry", "stop", "targets", "exits", "exit_i",
                 "exit_price", "pnl_pct", "r", "reason")

    def __init__(self, entry_i, entry):
        self.entry_i = entry_i
        self.entry = entry
        self.stop = entry * (1 - STOP_PCT / 100.0)
        self.targets = [entry * (1 + t / 100.0) for t in TARGETS_PCT]
        self.exits = []
        self.exit_i = None
        self.exit_price = None
        self.pnl_pct = 0.0
        self.r = 0.0
        self.reason = ""


def simulate_trade(bars, entry_i):
    """Replay one long from bars[entry_i]'s OPEN. Returns a resolved Trade."""
    t = Trade(entry_i, bars[entry_i].open)
    remaining = list(SPLITS)
    filled_pct = 0.0
    weighted = 0.0

    for j in range(entry_i, min(entry_i + MAX_HOLD_BARS, len(bars))):
        bar = bars[j]
        # Pessimistic: if the bar covers the stop, the stop goes first.
        if bar.low <= t.stop:
            rest = sum(remaining)
            weighted += t.stop * rest
            filled_pct += rest
            t.exit_i, t.exit_price, t.reason = j, t.stop, "stop"
            remaining = []
            break
        for k, tgt in enumerate(t.targets):
            if remaining and k < len(remaining) and remaining[k] > 0 and bar.high >= tgt:
                weighted += tgt * remaining[k]
                filled_pct += remaining[k]
                t.exits.append((k + 1, tgt))
                remaining[k] = 0.0
        if sum(remaining) <= 0:
            t.exit_i, t.exit_price, t.reason = j, t.targets[-1], "all targets"
            break

    if sum(remaining) > 0:                       # timed out — mark to market
        j = min(entry_i + MAX_HOLD_BARS, len(bars)) - 1
        close = bars[j].close
        rest = sum(remaining)
        weighted += close * rest
        filled_pct += rest
        t.exit_i, t.exit_price = j, close
        t.reason = t.reason or "time stop"

    avg_exit = weighted / filled_pct if filled_pct else t.entry
    gross = (avg_exit - t.entry) / t.entry * 100.0
    fees = (MAKER_FEE + TAKER_FEE) * 100.0       # maker in, taker out (pessimistic)
    t.pnl_pct = gross - fees
    t.r = t.pnl_pct / STOP_PCT
    return t


def backtest(c1h, c4h, params, warmup=120):
    """Replay Setup B over the series. One position at a time, no pyramiding.

    Uses signals.precompute + evaluate_at — the SAME decision code the cockpit
    runs, just with the indicator series computed once instead of per bar.

    The aVWAP anchor ROLLS: it moves to each newly confirmed swing low, which is
    what an operator running `/anchor auto` would have had. Pinning a year of
    history to bar 0 would silently turn "price above aVWAP" into "price above
    the yearly VWAP" — a different rule that happens to be trivially true in an
    uptrend, which would make the whole sweep meaningless.
    """
    ctx = signals.precompute("BACKTEST", c1h, c4h, params=params,
                             anchor_mode="rolling")
    trades = []
    i, n = warmup, len(c1h)
    while i < n - 1:
        # setup_b_CORE: this sweep is about Setup B's own parameters. The v0.4
        # entry gates (market regime, the 30m frame, the liquidation check) are
        # applied at entry time, not part of the rule being optimised — and
        # feeding them a backtest with no 30m series would veto every single
        # signal, silently returning "0 trades" for the whole grid.
        if signals.evaluate_at(ctx, i, explain=False).setup_b_core:
            t = simulate_trade(c1h, i + 1)       # next bar's OPEN
            trades.append(t)
            i = (t.exit_i or i) + 1              # flat before looking again
        else:
            i += 1
    return trades


# ---------------- statistics ----------------

def summarize(trades):
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "avg_r": 0.0, "expectancy": 0.0,
                "max_dd": 0.0, "total_r": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0}
    rs = [t.r for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(rs) * 100.0,
        "avg_r": statistics.fmean(rs),
        "expectancy": statistics.fmean(rs),      # in R, per trade
        "total_r": sum(rs),
        "max_dd": max_dd,
        "avg_win": statistics.fmean(wins) if wins else 0.0,
        "avg_loss": statistics.fmean(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "stdev_r": statistics.pstdev(rs) if len(rs) > 1 else 0.0,
    }


def standard_error(stats):
    """SE of the mean R — the noise band a ranking must clear to mean anything."""
    n, sd = stats["trades"], stats.get("stdev_r", 0.0)
    return (sd / math.sqrt(n)) if n > 1 else 0.0


def build_grid():
    combos = []
    for cross, ceil, exp, trend, vwap in itertools.product(
            GRID["rsi_cross"], GRID["rsi_ceiling"], GRID["macd_expansion_bars"],
            GRID["trend"], GRID["vwap_mode"]):
        if ceil <= cross:
            continue                             # a ceiling under the cross is empty
        kind, fast, slow = trend
        combos.append(signals.SetupBParams(
            rsi_cross=cross, rsi_ceiling=ceil, macd_expansion_bars=exp,
            trend_filter=kind, trend_fast=fast or 20, trend_slow=slow or 50,
            vwap_mode=vwap))
    return combos


# ---------------- report ----------------

def render_report(symbol, days, c1h, results, ranked, unranked):
    today = datetime.date.today().isoformat()
    fmt_ts = lambda ms: datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    L = [
        "---", "type: note", f'title: "ARMM Research — Setup B parameter sweep ({symbol})"',
        'parent: "[[ARMM]]"', f"created: {today}", "tags: [armm, research, trading, backtest]",
        "---", "",
        f"# Setup B parameter sweep — {symbol}", "",
        f"Generated {today} by `research.py`. **Offline backtest — no orders were placed, "
        f"no private endpoint was called.**", "",
        "## Data", "",
        f"- {len(c1h)} 1H bars, {fmt_ts(c1h[0].open_time)} → {fmt_ts(c1h[-1].open_time)} "
        f"(~{days} days)",
        f"- {len(results)} parameter combinations tested",
        f"- Ranked: {len(ranked)} (>= {MIN_TRADES} trades) · "
        f"Not ranked: {len(unranked)} (too few trades)",
        "",
        "## Method", "",
        f"- Entry at the NEXT bar's open after a Setup B signal. No same-bar fills.",
        f"- Exits: +{'/+'.join(str(t) for t in TARGETS_PCT)}% tranches at "
        f"{'/'.join(str(int(s)) for s in SPLITS)}, stop -{STOP_PCT}%.",
        f"- When one bar spans both the stop and a target, the STOP is taken first.",
        f"- Fees: {MAKER_FEE * 100:.2f}% maker in + {TAKER_FEE * 100:.2f}% taker out.",
        f"- Unresolved after {MAX_HOLD_BARS} bars → closed at market.",
        f"- R is measured against the {STOP_PCT}% stop distance.",
        f"- One position at a time; no pyramiding.",
        f"- The CORE Setup B rule is measured. Live entries face additional v0.4 "
        f"gates (BTC market regime, the 30m justification frame, and the "
        f"stop-vs-liquidation check) applied at entry time, which are not part of "
        f"the parameter set being swept — so the LIVE signal count will be lower "
        f"than the trade counts below.",
        f"- aVWAP anchor ROLLS to each newly CONFIRMED swing low (a pivot is only "
        f"confirmed {signals.SWING_WINDOW} bars after it forms), mirroring "
        f"`/anchor auto`. No look-ahead.",
        "",
    ]

    if not ranked:
        L += ["## Result", "",
              f"**No parameter combination produced at least {MIN_TRADES} trades over "
              f"this window.** Nothing here is rankable — the honest conclusion is that "
              f"this sample cannot distinguish these parameters. Widen `--days` before "
              f"drawing any conclusion.", ""]
        if unranked:
            L += ["Best of the unrankable (shown ONLY to prove the harness ran — "
                  "do not tune on these):", "",
                  "| params | trades | win% | avg R |", "|---|---|---|---|"]
            for p, s in unranked[:5]:
                L.append(f"| `{p.label()}` | {s['trades']} | {s['win_rate']:.0f} | "
                         f"{s['avg_r']:+.3f} |")
            L.append("")
        return "\n".join(L)

    best_p, best_s = ranked[0]
    se = standard_error(best_s)
    L += ["## Top 5", "",
          "| # | params | trades | win% | avg R | expectancy | total R | max DD (R) | PF |",
          "|---|---|---|---|---|---|---|---|---|"]
    for i, (p, s) in enumerate(ranked[:5], 1):
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        L.append(f"| {i} | `{p.label()}` | {s['trades']} | {s['win_rate']:.0f} | "
                 f"{s['avg_r']:+.3f} | {s['expectancy']:+.3f} | {s['total_r']:+.1f} | "
                 f"{s['max_dd']:.1f} | {pf} |")
    L.append("")

    # --- sensitivity / honesty section ---
    avg_rs = [s["avg_r"] for _, s in ranked]
    spread = max(avg_rs) - min(avg_rs)
    twentieth = ranked[min(19, len(ranked) - 1)]
    gap = best_s["avg_r"] - twentieth[1]["avg_r"]
    noise = 2 * se
    L += ["## Parameter sensitivity — read this before tuning", "",
          f"- Best avg R **{best_s['avg_r']:+.3f}** over {best_s['trades']} trades; "
          f"standard error of that mean is **±{se:.3f}** R.",
          f"- Across all {len(ranked)} ranked combos avg R spans "
          f"{min(avg_rs):+.3f} … {max(avg_rs):+.3f} (spread {spread:.3f} R).",
          f"- Gap between #1 and #{min(20, len(ranked))}: **{gap:+.3f} R**, "
          f"against a ~{noise:.3f} R noise band (2×SE).",
          ""]
    if gap < noise:
        L += ["> ⚠️ **The winner is INSIDE the noise band.** The gap between the best and "
              "the 20th-best combination is smaller than two standard errors of the best "
              "combo's own mean. This sweep does **not** identify a superior parameter "
              "set — treat the top of the table as a tie and do not tune on it. Collect "
              "more history or accept the defaults.", ""]
    else:
        L += ["> The winner clears the noise band, which is weak evidence it is genuinely "
              "better — not proof. Confirm out-of-sample before trusting it.", ""]

    # --- per-dimension marginals ---
    L += ["## Marginals (avg R by single parameter, all else varying)", ""]
    dims = []
    for dim, getter in (("RSI cross", lambda p: f"{p.rsi_cross:g}"),
                        ("RSI ceiling", lambda p: f"{p.rsi_ceiling:g}"),
                        ("MACD expansion bars", lambda p: str(p.macd_expansion_bars)),
                        ("Trend filter", lambda p: "none" if p.trend_filter == "none"
                         else f"EMA{p.trend_fast}/{p.trend_slow}"),
                        ("aVWAP mode", lambda p: p.vwap_mode)):
        buckets = {}
        for p, s in ranked:
            buckets.setdefault(getter(p), []).append(s["avg_r"])
        if not buckets:
            continue
        means = {k: statistics.fmean(v) for k, v in buckets.items()}
        spread = max(means.values()) - min(means.values())
        dims.append((dim, spread, means))
        L.append(f"**{dim}**: " + " · ".join(
            f"{k} → {means[k]:+.3f} ({len(buckets[k])})" for k in sorted(buckets)))
    L.append("")

    # --- which dimension actually separates? ---
    if dims:
        dims.sort(key=lambda d: d[1], reverse=True)
        L += ["### Which dimension actually separates", "",
              "Marginal spread = best minus worst bucket mean for that parameter, "
              "averaged over everything else. A spread inside the noise band is a "
              "parameter this data cannot speak to.", "",
              "| dimension | marginal spread (R) | verdict |", "|---|---|---|"]
        for dim, spread, means in dims:
            verdict = ("**separates** — worth keeping" if spread > noise
                       else "inside the noise band — no evidence either way")
            L.append(f"| {dim} | {spread:.3f} | {verdict} |")
        L.append("")
        top_dim, top_spread, top_means = dims[0]
        if top_spread > noise:
            best_bucket = max(top_means, key=top_means.get)
            worst_bucket = min(top_means, key=top_means.get)
            L += [f"> The one dimension that clears the noise band is **{top_dim}**: "
                  f"`{best_bucket}` averages {top_means[best_bucket]:+.3f} R against "
                  f"`{worst_bucket}` at {top_means[worst_bucket]:+.3f} R. That is the "
                  f"only parameter this sweep gives real evidence about — and note it "
                  f"is a filter that REMOVES trades rather than a threshold to tune.", ""]
        else:
            L += ["> No single dimension clears the noise band. This sweep provides no "
                  "evidence for changing any parameter from its default.", ""]

    if unranked:
        L += [f"## Not ranked ({len(unranked)} combos under {MIN_TRADES} trades)", "",
              "Listed for completeness so the grid is not silently truncated:", "",
              "| params | trades |", "|---|---|"]
        for p, s in unranked[:15]:
            L.append(f"| `{p.label()}` | {s['trades']} |")
        if len(unranked) > 15:
            L.append(f"| … {len(unranked) - 15} more | |")
        L.append("")

    L += ["## Caveats", "",
          "- One symbol, one regime, one window. Nothing here is out-of-sample.",
          "- No slippage model beyond fees; a 1% stop on a fast move can gap through.",
          "- The stop-before-target rule makes results conservative, not correct.",
          "- Setup B is a LONG-only rule; this window's directional bias is baked in.",
          "- v0.3 does not act on any of this. Parameter changes remain Andrew's call.",
          ""]
    return "\n".join(L)


# ---------------- CLI ----------------

def main(argv=None):
    p = argparse.ArgumentParser(description="ARMM Setup B research harness (offline)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--out", help="output path (default: ~/armm/research)")
    p.add_argument("--min-trades", type=int, default=MIN_TRADES)
    p.add_argument("--limit-combos", type=int, help="test only the first N combos")
    p.add_argument("--refresh", action="store_true", help="ignore the kline cache")
    a = p.parse_args(argv)

    from weex_client import WeexClient
    client = WeexClient(demo=True)               # public klines only

    log.info("loading %d days of %s candles", a.days, a.symbol)
    c1h = candles_mod.load(client, a.symbol, "1h", days=a.days, refresh=a.refresh)
    c4h = candles_mod.load(client, a.symbol, "4h", days=a.days + 30, refresh=a.refresh)
    if len(c1h) < 200:
        print(f"error: only {len(c1h)} 1H bars available — need at least 200",
              file=sys.stderr)
        return 2

    combos = build_grid()
    if a.limit_combos:
        combos = combos[:a.limit_combos]
    log.info("testing %d parameter combinations over %d bars", len(combos), len(c1h))

    results = []
    for i, params in enumerate(combos, 1):
        trades = backtest(c1h, c4h, params)
        stats = summarize(trades)
        results.append((params, stats))
        log.info("[%d/%d] %s -> %d trades, avg R %+.3f", i, len(combos),
                 params.label(), stats["trades"], stats["avg_r"])

    ranked = sorted([r for r in results if r[1]["trades"] >= a.min_trades],
                    key=lambda r: r[1]["avg_r"], reverse=True)
    unranked = sorted([r for r in results if r[1]["trades"] < a.min_trades],
                      key=lambda r: r[1]["trades"], reverse=True)

    report = render_report(a.symbol, a.days, c1h, results, ranked, unranked)
    out = a.out or os.path.join(
        VAULT_RESEARCH,
        f"setup-b-{a.symbol.upper()}-{datetime.date.today().isoformat()}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(report)
    print(f"wrote {out}")
    print(f"  {len(results)} combos tested · {len(ranked)} ranked · {len(unranked)} unranked")
    if ranked:
        bp, bs = ranked[0]
        print(f"  best: {bp.label()} — {bs['trades']} trades, avg R {bs['avg_r']:+.3f} "
              f"(SE ±{standard_error(bs):.3f})")
    else:
        print(f"  no combo reached {a.min_trades} trades — nothing is rankable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
