#!/usr/bin/env python3
"""ARMM guard-starvation detection — telling "blocked" apart from "quiet".

Between 2026-09-05 and 2026-10-03 autopilot refused 335 consecutive Setup B
entries, every single one for the same reason: the WEEX demo account sat at
~141x, which puts liquidation ~0.21% away while the strategy's stop sits 1.00%
away, so the stop-behind-liquidation check correctly refused every ladder. The
guard was right. The problem is that nothing ever said so: four weekly digests
reported "no trades", `/auto status` reported "ON", and a fully starved
autopilot was indistinguishable from a correctly idle one.

This module exists to make that distinction loud, and the distinction is the
whole design:

  * "The market is not offering a setup" is HEALTH. A quiet autopilot in a
    rangebound week is doing its job. These results are IDLE and accumulate
    nothing — if they counted, the alarm would fire constantly and be ignored,
    which is worse than no alarm.
  * "A setup confirmed and I then refused it" is a BLOCK. Something upstream
    of the market — leverage, config, a guard threshold, a broken plan — is
    standing between a live signal and an order. That is the only thing worth
    escalating, because that is the only thing a human can fix.

A long run of blocks sharing ONE reason is a config fault. A long run of blocks
with mixed reasons is not necessarily anything, and this module says "mixed"
rather than inventing a cause it cannot support.
"""
import json
import time

from . import store as store_mod

# What run_symbol() returns, bucketed by what it means for health.
BLOCKED = "blocked"
ENTERED = "entered"
IDLE = "idle"

# A setup confirmed and then something refused the entry. Only these count.
BLOCKED_REASONS = frozenset({
    "guard_refused",     # the hard risk gate said no
    "advisor_refused",   # the advisor objected and autopilot has no override
    "plan_failed",       # the ladder could not be built
    "order_failed",      # the venue rejected the order
    "unsafe_routing",    # the demo lock tripped (should be impossible)
})

# How many consecutive blocks before we call it a fault. Blocks arrive once per
# poll cycle (~5 min) while a setup stays live, so 10 escalates inside an hour
# of a genuinely stuck autopilot while staying quiet through the ordinary case
# of a guard declining one or two marginal entries.
STREAK_THRESHOLD = 10

# Don't nag. One alarm, then silence for a day unless it clears and recurs.
REALERT_SECONDS = 24 * 3600

# A single reason must own this much of the streak before we name it as THE
# cause. Below it we say "mixed" — ten blocks for five reasons is not one fault.
DOMINANCE = 0.8


def classify(result):
    """BLOCKED / ENTERED / IDLE for one autopilot run_symbol() result.

    Anything not explicitly a block is IDLE, deliberately: an unrecognised
    future reason code must never manufacture an alarm. Under-reporting a novel
    block is recoverable; crying wolf trains the operator to ignore the alarm
    that matters.
    """
    if not result:
        return IDLE
    if result.get("action") == "entered":
        return ENTERED
    return BLOCKED if result.get("reason") in BLOCKED_REASONS else IDLE


META_PREFIX = "starvation:"


def _blank():
    """A fresh clean-slate state EVERY call.

    Deliberately a function and not a module-level template: v0.4 shipped a bug
    where `dict(_BLANK)` shallow-copied a nested dict, so the first write in a
    process mutated the template permanently and every later "fresh" state
    arrived pre-populated. Building it fresh makes that class of bug impossible
    rather than merely absent.
    """
    return {"streak": 0, "reasons": {}, "first_ts": None, "last_ts": None,
            "last_alert_ts": 0.0, "last_detail": None}


class Tracker:
    """Per-symbol block-streak accounting, persisted in the store's meta table.

    Persisted rather than in-memory because `armm-bot.service` is
    `Restart=always`: an in-memory streak would reset on every restart and the
    slow-burn fault this module exists to catch is precisely the kind that
    outlives a process.
    """

    def __init__(self, store=None):
        self.store = store if store is not None else store_mod.default()

    # ---- persistence ----
    def _key(self, symbol):
        return META_PREFIX + symbol.upper()

    def state(self, symbol):
        raw = self.store.get_meta(self._key(symbol))
        if not raw:
            return _blank()
        try:
            st = json.loads(raw)
        except (TypeError, ValueError):
            return _blank()
        base = _blank()
        base.update(st)
        # A corrupted or hand-edited row must not crash the poll loop.
        if not isinstance(base.get("reasons"), dict):
            base["reasons"] = {}
        return base

    def _save(self, symbol, st):
        self.store.set_meta(self._key(symbol), json.dumps(st))

    # ---- the cycle hook ----
    def record(self, result, now=None):
        """Fold one run_symbol() result into the streak."""
        now = time.time() if now is None else now
        symbol = (result or {}).get("symbol")
        if not symbol:
            return None
        kind = classify(result)
        if kind == IDLE:
            # Deliberately neither increments nor clears. See the test.
            return None

        st = self.state(symbol)
        if kind == ENTERED:
            if st["streak"]:
                self._save(symbol, _blank())
            return None

        reason = result.get("reason")
        st["streak"] += 1
        st["reasons"][reason] = st["reasons"].get(reason, 0) + 1
        st["first_ts"] = st["first_ts"] if st["first_ts"] is not None else now
        st["last_ts"] = now
        st["last_detail"] = result.get("detail")

        alarm = None
        if st["streak"] >= STREAK_THRESHOLD and _cooldown_elapsed(
                st.get("last_alert_ts"), now):
            st["last_alert_ts"] = now
            alarm = self._alarm_text(symbol, st)
            self.store.log_event(
                "starvation_alarm",
                f"{symbol}: {st['streak']} consecutive blocked entries, "
                f"cause={dominant_reason(st['reasons'])[0]}")
        self._save(symbol, st)
        return alarm

    # ---- reporting ----
    def summary_lines(self, symbol):
        """Lines for /auto status. Empty when there is nothing wrong to say.

        Silence when healthy is deliberate: a status command that always prints
        a health paragraph trains the reader to skim past it.
        """
        st = self.state(symbol)
        if not st["streak"]:
            return []
        reason, share = dominant_reason(st["reasons"])
        cause = (f"mixed reasons" if reason == MIXED
                 else f"{reason} ({share:.0%})")
        lines = [f"  ⚠️ blocked: {st['streak']} consecutive refused "
                 f"entr{'y' if st['streak'] == 1 else 'ies'} over "
                 f"{_span(st['first_ts'], st['last_ts'])} — {cause}"]
        if st.get("last_detail"):
            lines.append(f"     last: {st['last_detail']}")
        return lines

    def _alarm_text(self, symbol, st):
        reason, share = dominant_reason(st["reasons"])
        span = _span(st["first_ts"], st["last_ts"])
        head = (f"⚠️ AUTOPILOT STARVED on {symbol} — "
                f"{st['streak']} consecutive blocked entries over {span}.")
        if reason == MIXED:
            cause = ("Cause: MIXED — no single reason owns the streak, so this "
                     "is not one config fault. Breakdown: "
                     + ", ".join(f"{k} x{v}" for k, v in
                                 sorted(st["reasons"].items(), key=lambda kv: -kv[1])))
        else:
            cause = (f"Cause: {reason} accounts for {share:.0%} of them — "
                     f"one repeating fault, not market conditions.")
        return "\n".join([
            head,
            "",
            "This is NOT a quiet market. Every one of these was a CONFIRMED "
            "setup that something then refused, so a live signal is reaching a "
            "wall upstream of the order.",
            "",
            cause,
            "",
            f"Most recent refusal said:\n  {st.get('last_detail') or '(no detail)'}",
            "",
            "Fix the cause or /auto off — leaving it ON produces no trades and "
            "no track record.",
        ])


MIXED = "mixed"


def dominant_reason(reasons):
    """(reason, share) when one reason owns the streak, else (MIXED, share).

    Honesty rule: naming a cause we cannot support is worse than saying we
    don't know, because the operator acts on the name.
    """
    if not reasons:
        return MIXED, 0.0
    total = sum(reasons.values())
    reason, count = max(reasons.items(), key=lambda kv: kv[1])
    share = count / total if total else 0.0
    return (reason, share) if share >= DOMINANCE else (MIXED, share)


def starved(store=None, threshold=STREAK_THRESHOLD):
    """[(symbol, state)] for every symbol currently carrying a real block.

    Driven off the meta rows rather than the enabled-symbol list, so a symbol
    that was switched off while blocked still reports — "I turned it off and
    forgot why" is the same failure wearing a different hat.
    """
    store = store or store_mod.default()
    tracker = Tracker(store)
    out = []
    for key in store.meta_keys(META_PREFIX):
        symbol = key[len(META_PREFIX):]
        st = tracker.state(symbol)
        if st["streak"] >= threshold:
            out.append((symbol, st))
    return out


def digest_section(store=None, threshold=STREAK_THRESHOLD):
    """The block report that leads the weekly digest, or "" when all is well."""
    rows = starved(store, threshold)
    if not rows:
        return ""
    L = ["🚧 AUTOPILOT BLOCKED — read this before the numbers below.",
         "",
         "These symbols had confirmed setups that were refused every time. The "
         "zero trades below are NOT a quiet market; they are a wall upstream of "
         "the order path.",
         ""]
    for symbol, st in rows:
        reason, share = dominant_reason(st["reasons"])
        cause = "mixed reasons" if reason == MIXED else f"{reason} ({share:.0%})"
        L.append(f"• {symbol}: {st['streak']} refused entries over "
                 f"{_span(st['first_ts'], st['last_ts'])} — {cause}")
        if st.get("last_detail"):
            L.append(f"    {st['last_detail']}")
    L.append("")
    return "\n".join(L)


def _cooldown_elapsed(last_alert_ts, now):
    """True when we are clear to alarm again.

    A falsy `last_alert_ts` means NEVER ALERTED, and must not be arithmetic'd
    as a timestamp — v0.3 shipped exactly that bug in the setup-alert cooldown
    (`now - 0 < 3600` stays true forever under a test clock and swallowed the
    very first alert). Treated as a distinct case here, on purpose.
    """
    if not last_alert_ts:
        return True
    return (float(now) - float(last_alert_ts)) >= REALERT_SECONDS


def _span(first_ts, last_ts):
    """Human span of the streak, in the largest unit that keeps it readable."""
    if first_ts is None or last_ts is None:
        return "an unknown period"
    secs = max(0.0, float(last_ts) - float(first_ts))
    if secs < 90 * 60:
        return f"{int(secs // 60)}m"
    if secs < 48 * 3600:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f} days"
