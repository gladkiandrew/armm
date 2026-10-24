#!/usr/bin/env python3
"""ARMM scoreboard — the pure-rules autopilot record against the manual one.

The whole point of demo autopilot is comparison: Andrew's discretionary judgment
versus the strategy exactly as coded, on the same market, over the same weeks.
This module reads the database and reports both, side by side, without flattering
either.

Honesty rules baked in:
  * TEST orders are excluded from every statistic. They exist to prove the
    machinery works, not to pad a record.
  * Positions whose exit price is unknown are counted as UNRESOLVED and reported
    separately — never silently dropped, never assumed flat.
  * Demo exit prices are observed one poll cycle after the position vanished,
    not fill prices, because the sim venue records no fill for an atomic
    TP/SL trigger. Every rendering says so.
  * A sample this small is stated as such rather than dressed up with decimals
    it cannot support.
"""
import statistics
import time

import origin as origin_mod
import starvation
import store as store_mod

MIN_TRADES_FOR_CONFIDENCE = 20


def _bucket(origin):
    """AUTO vs MANUAL. Legacy/unknown rows are their own bucket, not folded in."""
    o = origin_mod.parse(origin) if str(origin).startswith("armm-") else origin
    try:
        o = origin_mod.normalize(o)
    except origin_mod.OriginError:
        return "LEGACY"
    if o == origin_mod.AUTO:
        return "AUTO"
    if o == origin_mod.TEST:
        return "TEST"
    return "MANUAL"


def collect(store=None, since=None):
    """Group CLOSED positions into AUTO / MANUAL / LEGACY buckets."""
    store = store or store_mod.default()
    rows = store.closed_positions(since=since)
    out = {"AUTO": [], "MANUAL": [], "LEGACY": []}
    excluded_tests = 0
    for p in rows:
        b = _bucket(p.get("source") or origin_mod.parse(p.get("entry_client_id")))
        if b == "TEST":
            excluded_tests += 1
            continue
        out[b].append(p)
    return out, excluded_tests


def summarize(positions):
    """Stats for one bucket. Unresolved positions are counted, not guessed at."""
    resolved = [p for p in positions if p.get("r_multiple") is not None]
    unresolved = len(positions) - len(resolved)
    overrides = sum(1 for p in positions if p.get("advisor_overridden"))
    if not resolved:
        return {"trades": 0, "unresolved": unresolved, "overrides": overrides,
                "win_rate": None, "avg_r": None, "expectancy": None,
                "total_r": 0.0, "max_dd": 0.0}
    rs = [float(p["r_multiple"]) for p in resolved]
    wins = [r for r in rs if r > 0]
    equity = peak = max_dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(rs),
        "unresolved": unresolved,
        "overrides": overrides,
        "win_rate": len(wins) / len(rs) * 100.0,
        "avg_r": statistics.fmean(rs),
        "expectancy": statistics.fmean(rs),
        "total_r": sum(rs),
        "max_dd": max_dd,
        "stdev_r": statistics.pstdev(rs) if len(rs) > 1 else 0.0,
    }


def _fmt(stats, label, key):
    """`label` is what the reader sees; `key` is the bucket identity."""
    if stats["trades"] == 0:
        base = f"{label}: no resolved trades yet"
        extra = []
        if stats["unresolved"]:
            extra.append(f"{stats['unresolved']} unresolved")
        if stats["overrides"]:
            extra.append(f"{stats['overrides']} advisor override(s)")
        return base + (" (" + " · ".join(extra) + ")" if extra else "")
    L = [f"{label}: {stats['trades']} trades · win {stats['win_rate']:.0f}% · "
         f"avg {stats['avg_r']:+.2f}R · total {stats['total_r']:+.1f}R · "
         f"maxDD {stats['max_dd']:.1f}R"]
    extra = []
    if stats["unresolved"]:
        extra.append(f"{stats['unresolved']} unresolved")
    if key == "MANUAL" and stats["overrides"]:
        extra.append(f"{stats['overrides']} advisor override(s)")
    if extra:
        L.append("   (" + " · ".join(extra) + ")")
    return "\n".join(L)


def render(store=None, since=None, title="📊 ARMM scoreboard — AUTO vs MANUAL"):
    buckets, excluded = collect(store, since)
    auto = summarize(buckets["AUTO"])
    manual = summarize(buckets["MANUAL"])
    legacy = summarize(buckets["LEGACY"])

    L = [title, ""]
    L.append(_fmt(auto, "AUTO   (strategy as coded, demo)", "AUTO"))
    L.append(_fmt(manual, "MANUAL (your calls)", "MANUAL"))
    if legacy["trades"] or legacy["unresolved"]:
        L.append(_fmt(legacy, "LEGACY (pre-v0.4, origin not recorded)", "LEGACY"))
    L.append("")

    # The comparison, stated only when it can carry weight.
    if auto["trades"] and manual["trades"]:
        diff = auto["avg_r"] - manual["avg_r"]
        thin = min(auto["trades"], manual["trades"]) < MIN_TRADES_FOR_CONFIDENCE
        lead = "AUTO" if diff > 0 else "MANUAL"
        L.append(f"Difference: {lead} is ahead by {abs(diff):.2f}R per trade.")
        if thin:
            L.append(f"⚠️ Too few trades to mean anything — one side has under "
                     f"{MIN_TRADES_FOR_CONFIDENCE}. Read this as a tally, not a verdict.")
    elif auto["trades"] or manual["trades"]:
        L.append("Only one side has resolved trades — nothing to compare yet.")
    else:
        L.append("No resolved trades on either side yet.")

    if excluded:
        L.append(f"({excluded} test order(s) excluded from these numbers)")
    L.append("Exit prices in DEMO are observed one poll cycle after the position "
             "closed, not fills — the sim venue records no fill for an atomic "
             "TP/SL trigger. Treat R as accurate to ±1 cycle.")
    return "\n".join(L)


def weekly_digest(store=None, now=None):
    """The Sunday summary: the last 7 days plus the running total.

    Any active block leads, ABOVE the statistics. Four of these digests went out
    in September 2026 reporting "no trades" while autopilot was refusing every
    confirmed setup for the same reason — the number was true and the digest was
    still misleading, because "no trades" read as "quiet week".
    """
    now = time.time() if now is None else now
    week = render(store, since=now - 7 * 86400,
                  title="🗓 ARMM weekly summary — last 7 days")
    body = week + "\n\n" + render(store, title="— running total —")
    block = starvation.digest_section(store)
    return (block + "\n" + body) if block else body
