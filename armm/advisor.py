#!/usr/bin/env python3
"""ARMM advisor — checks a manual ladder against the live signal snapshot.

It ADVISES. It never blocks, never places, never resizes. Andrew keeps final
authority: the advisor's only power is to make him tap twice and to write down
that he overrode it.

The canonical case this exists for is the bad demo entry of 2026-09-01: a long
opened into RSI > 60 with the 4H trend down. Nothing in the guards forbids it —
it breaks no hard risk rule — but it contradicts Andrew's own stated setups, and
he wanted the bot to say so before the order goes in.
"""
from dataclasses import dataclass, field

from . import guards
from . import logsetup
from . import signals

log = logsetup.get("advisor")

RSI_LONG_EXTENDED = 60.0    # a long above this is chasing, per Andrew's rules
RSI_SHORT_EXTENDED = 40.0   # mirror, for completeness (shorts are out of scope)


@dataclass
class Advice:
    symbol: str
    side: str
    contradictions: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    snapshot: object = None
    matched_setup: str = None

    @property
    def contradicted(self):
        return bool(self.contradictions)

    def render(self, verbose=True):
        if self.snapshot is not None and self.snapshot.error:
            return f"⚠️ advisor could not evaluate {self.symbol}: {self.snapshot.error}"
        if not self.contradictions:
            head = (f"✅ advisor: {self.side} {self.symbol} is consistent with "
                    + (f"Setup {self.matched_setup}" if self.matched_setup
                       else "the current readout"))
            return head + ("\n" + "\n".join(f"  · {n}" for n in self.notes)
                           if verbose and self.notes else "")
        L = [f"⚠️ ADVISOR: this {self.side} contradicts your own rules "
             f"({len(self.contradictions)} issue"
             f"{'s' if len(self.contradictions) > 1 else ''}):"]
        L += [f"  ✗ {c}" for c in self.contradictions]
        if verbose and self.notes:
            L += [f"  · {n}" for n in self.notes]
        L.append("")
        L.append("No setup matches. You keep the call — confirm to place anyway.")
        return "\n".join(L)

    def db_note(self):
        return " | ".join(self.contradictions)[:500]


def review(symbol, side, entry_price=None, snapshot=None, client=None,
           anchor_ts=None, params=None, stop_price=None, leverage=None,
           venue_leverage=None):
    """Compare an intended entry to the live snapshot. Returns Advice.

    Never raises on data problems — an advisor that crashes the order path would
    be worse than one that stays quiet.
    """
    side = (side or "").upper()
    try:
        snap = snapshot if snapshot is not None else signals.evaluate(
            symbol, anchor_ts=anchor_ts, client=client, params=params)
    except Exception as e:                      # advisory only: degrade, never block
        log.warning("advisor evaluation failed for %s: %s", symbol, e)
        adv = Advice(symbol=symbol, side=side)
        adv.notes.append(f"signal snapshot unavailable ({e}) — proceeding unadvised")
        return adv

    adv = Advice(symbol=symbol, side=side, snapshot=snap)

    # The liquidation check is about the ORDER, not the market, so it runs even
    # when the snapshot is unusable — a stop behind liquidation is fatal
    # regardless of what the indicators say.
    _review_liquidation(adv, entry_price, stop_price, leverage, venue_leverage)

    if snap.error:
        adv.notes.append(snap.error)
        return adv

    if snap.setup_a:
        adv.matched_setup = "A"
    if snap.setup_b:
        adv.matched_setup = "B" if not adv.matched_setup else "A+B"

    if side == "LONG":
        _review_long(adv, snap, entry_price)
    elif side == "SHORT":
        adv.notes.append("shorts are out of scope for v0.3 — no setup to check against")
    return adv


def _review_liquidation(adv, entry_price, stop_price, leverage, venue_leverage=None):
    """A stop at or beyond estimated liquidation is not a stop.

    The position ceases to exist before the order can trigger, so the risk the
    operator sized is fiction. This is the one advisor check that does not
    depend on the market read at all.

    `venue_leverage` matters more than the requested figure. v0.2 found the demo
    account running at ~141x while the bot printed "at 5x": the guards cap what
    we ASK for, but the ACCOUNT's setting is what governs margin and liquidation.
    When we know the venue's number we use the worse of the two, and when we do
    not know it we say so instead of implying the 5x figure was verified.
    """
    if not (entry_price and stop_price and leverage):
        return
    effective = float(leverage)
    source = f"{leverage}x requested"
    if venue_leverage:
        effective = max(effective, float(venue_leverage))
        source = (f"{venue_leverage}x on the venue" if float(venue_leverage) > float(leverage)
                  else f"{leverage}x requested")
    try:
        bad, d = guards.stop_is_behind_liquidation(
            entry_price, stop_price, effective, side=adv.side or "LONG")
    except (ValueError, ZeroDivisionError) as e:
        adv.notes.append(f"liquidation check skipped: {e}")
        return
    if bad:
        adv.contradictions.append(
            f"STOP BEHIND LIQUIDATION: at {source}, liquidation is ~"
            f"{d['liq_pct']:.2f}% away ({d['est_liq_price']:,.1f}) but the stop is "
            f"{d['stop_pct']:.2f}% away — the position is gone before the stop can "
            f"trigger. Lower the ACCOUNT leverage on the exchange, or widen nothing "
            f"and do not take this trade.")
        return
    note = (f"stop {d['stop_pct']:.2f}% vs est. liquidation {d['liq_pct']:.2f}% "
            f"at {source} — inside the safe band")
    if not venue_leverage:
        note += (" (the venue's own account leverage is unverified; it governs the "
                 "real liquidation price)")
    adv.notes.append(note)


def _review_long(adv, snap, entry_price):
    """A long is contradicted when it fights the readout.

    Two tiers. The UNCONDITIONAL checks (market regime, the 30m frame) run even
    when a setup is confirmed: a confirmed setup in a broken regime should be
    impossible, and if it happens anyway the advisor saying nothing would be the
    worst outcome. The DISCRETIONARY checks below them only apply when no setup
    matches — they exist to catch a hand-typed entry with no thesis behind it,
    not to second-guess a setup that has already passed its own rules.
    """
    if snap.regime == signals.REGIME_DOWN:
        adv.contradictions.append(
            f"market regime is DOWN ({snap.regime_source}: {snap.regime_detail}) "
            f"— Setup B is vetoed outright and Setup A needs stabilisation first")
    if snap.confirm_30m is False:
        adv.contradictions.append(f"30m frame disagrees — {snap.confirm_30m_detail}")

    if adv.matched_setup:
        adv.notes.append(f"Setup {adv.matched_setup} is currently satisfied")
        adv.notes.append(snap.verdict_line())
        return

    if snap.rsi is not None and snap.rsi > RSI_LONG_EXTENDED:
        adv.contradictions.append(
            f"RSI(14) 1H is {snap.rsi:.1f} (> {RSI_LONG_EXTENDED:g}) — buying "
            f"extension, not a reload; Setup A wants RSI < {signals.SETUP_A_RSI_MAX:g}")
    if snap.trend_up is False:
        adv.contradictions.append(
            f"4H trend is DOWN ({snap.trend_label}) — Setup B requires the higher "
            f"timeframe to be up")
    if snap.macd_hist is not None and snap.macd_hist < 0 and snap.rsi is not None \
            and snap.rsi > signals.SETUP_A_RSI_MAX:
        adv.contradictions.append(
            f"MACD histogram is {snap.macd_hist:+.2f} (negative) with RSI "
            f"{snap.rsi:.1f} — momentum is against the entry and it is not a "
            f"Setup A reload either")
    if (entry_price and snap.vwap is not None and snap.vwap_upper is not None
            and entry_price > snap.vwap_upper):
        adv.contradictions.append(
            f"entry {entry_price:,.1f} is above the upper aVWAP band "
            f"{snap.vwap_upper:,.1f} — that is the chase zone")

    if not adv.contradictions:
        adv.notes.append("no setup is confirmed, but nothing in the readout "
                         "contradicts the entry either")
        adv.notes.append(snap.verdict_line())
