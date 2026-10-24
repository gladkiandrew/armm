#!/usr/bin/env python3
"""ARMM autopilot — the strategy-as-coded, running against DEMO MONEY ONLY.

=============================================================================
THE DEMO LOCK — the single most important property of this file
=============================================================================
Autopilot originates entries without a human in the loop. The safety argument
is STRUCTURAL, not configurational:

  1. This module never imports, names, or can produce the live order path.
     The live order-path constant is neither imported nor named anywhere in
     this file. `make check` greps for it, and a unit test walks this
     module's AST looking for any call that could place a live order.
  2. Every client autopilot touches goes through demo_client(), which either
     constructs WeexClient(demo=True) — the literal True is written here, at the
     call site, not read from a flag, env var, config file or Telegram command —
     or validates a supplied client and RAISES on anything that is not demo.
  3. assert_demo() re-checks routing immediately before every single order. If
     the client's own routing ever reads live in an autopilot context we refuse,
     alert, and halt autopilot rather than proceed.
  4. There is no parameter, no override, no "force" flag anywhere in this file.
     Adding one would be a deliberate act, not a mistake.

There is deliberately no concept of "live autopilot" to switch on. Manual
/ladder keeps its own triple-gated live capability; that path does not run
through this module.

=============================================================================
Autopilot also has NO override authority over the advisor. A human may look at
a contradiction and take the trade anyway; autopilot may not. Any advisor
objection means no trade, full stop.
"""
import json
import time

from . import advisor as advisor_mod
from . import guards
from . import ladder
from . import logsetup
from . import origin as origin_mod
from . import signals
from . import starvation as starvation_mod
from . import store as store_mod
from .weex_client import WeexClient, WeexError, DEMO_ORDER_PATH

log = logsetup.get("autopilot")

# Entry recipes. Setup B's targets are the research defaults (+3/+4/+5%).
SETUP_B = {
    "offset_pct": 0.05,      # rest just under the last price, maker-first
    "stop_pct": 1.0,
    "targets_pct": ["3", "4", "5"],
    "splits": ["33", "33", "34"],
}
# Setup A buys the lower band, so the entry IS the band; the stop sits a
# configurable multiple of sigma below it, and targets are wider because a
# reload is a slower trade than a momentum push.
SETUP_A = {
    "stop_sigma_mult": 1.0,  # stop = lower_band - 1.0 * sigma
    "stop_pct_floor": 1.0,   # ...but never tighter than this
    "stop_pct_cap": 3.0,     # ...nor wider than this
    "targets_pct": ["2", "4", "6"],
    "splits": ["33", "33", "34"],
}

DEFAULT_NOTIONAL = 500.0
DEFAULT_LEVERAGE = 5
PER_SYMBOL_MIN_GAP_SECONDS = 4 * 3600     # max 1 autopilot entry per symbol per 4h


class AutopilotSafetyError(RuntimeError):
    """Raised when anything about the routing is not provably demo."""


# ---------------------------------------------------------------------------
# the lock
# ---------------------------------------------------------------------------

def assert_demo(client, where="autopilot"):
    """Prove this client can only reach the simulated venue. Raises otherwise.

    Checked on construction AND again immediately before every order, because a
    client object is mutable and 'it was demo when we built it' is not the same
    claim as 'it is demo right now'.
    """
    if client is None:
        raise AutopilotSafetyError(f"{where}: no client")
    if getattr(client, "demo", None) is not True:
        raise AutopilotSafetyError(
            f"{where}: REFUSING — client.demo is {getattr(client, 'demo', None)!r}, "
            f"not True. Autopilot is demo-only by construction.")
    path = getattr(client, "order_path", None)
    if path != DEMO_ORDER_PATH:
        raise AutopilotSafetyError(
            f"{where}: REFUSING — order path is {path!r}, expected {DEMO_ORDER_PATH!r}.")
    if getattr(client, "mode", None) != "DEMO":
        raise AutopilotSafetyError(
            f"{where}: REFUSING — client mode is {getattr(client, 'mode', None)!r}.")
    return True


def demo_client(client=None):
    """The ONLY way autopilot obtains a client.

    With no argument it builds a demo client here, with the literal True below.
    With an argument (tests, or the manager sharing its client) it validates and
    raises rather than quietly correcting — a caller that handed us a live
    client has a bug we must not paper over.
    """
    if client is None:
        return WeexClient(demo=True)          # <- hard-coded. Not a flag.
    assert_demo(client, "demo_client")
    return client


# ---------------------------------------------------------------------------
# plan construction
# ---------------------------------------------------------------------------

def setup_a_plan_kwargs(snap, cfg=None):
    """Setup A: buy the lower band, stop below it, wider targets."""
    c = dict(SETUP_A, **(cfg or {}))
    entry = snap.vwap_lower
    sigma = snap.vwap_sigma or 0.0
    stop = entry - c["stop_sigma_mult"] * sigma
    stop_pct = (entry - stop) / entry * 100.0 if entry else 0.0
    stop_pct = min(max(stop_pct, c["stop_pct_floor"]), c["stop_pct_cap"])
    return {"entry": entry, "stop_pct": stop_pct,
            "targets_pct": list(c["targets_pct"]), "splits": list(c["splits"])}


def setup_b_plan_kwargs(snap, cfg=None):
    """Setup B: maker entry just under the last price, tight stop, +3/4/5%."""
    c = dict(SETUP_B, **(cfg or {}))
    return {"offset_pct": c["offset_pct"], "stop_pct": c["stop_pct"],
            "targets_pct": list(c["targets_pct"]), "splits": list(c["splits"])}


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class Autopilot:
    def __init__(self, store=None, notify=None, client=None,
                 notional=DEFAULT_NOTIONAL, leverage=DEFAULT_LEVERAGE):
        # Raises immediately if handed anything that is not demo.
        self.client = demo_client(client)
        assert_demo(self.client, "Autopilot.__init__")
        self.store = store or store_mod.default()
        self.notify = notify
        self.notional = float(notional)
        self.leverage = int(leverage)
        # Watches for the failure mode where autopilot is ON, signals confirm,
        # and every one is refused for the same reason — indistinguishable from
        # a quiet market until v0.5 made it say so.
        self.starvation = starvation_mod.Tracker(self.store)

    # ---- enable / disable ----
    def enabled_symbols(self):
        return self.store.auto_symbols()

    def is_enabled(self, symbol):
        return self.store.auto_enabled(symbol)

    def enable(self, symbol):
        self.store.set_auto(symbol, True)
        self.store.log_event("auto_on", f"{symbol.upper()} autopilot enabled (DEMO)")

    def disable(self, symbol=None):
        if symbol:
            self.store.set_auto(symbol, False)
            self.store.log_event("auto_off", f"{symbol.upper()} autopilot disabled")
            return [symbol.upper()]
        off = self.store.auto_symbols()
        for s in off:
            self.store.set_auto(s, False)
        self.store.log_event("auto_off", f"autopilot disabled for {', '.join(off) or 'nothing'}")
        return off

    def status_lines(self):
        on = self.enabled_symbols()
        halted, reason = guards.is_halted()
        L = [f"🤖 autopilot: {'ON for ' + ', '.join(on) if on else 'OFF (no symbols)'}",
             f"routing: {DEMO_ORDER_PATH} — DEMO ONLY, structurally (no live mode exists)"]
        if halted:
            L.append(f"⛔ HALTED: {reason} — /resume then /auto on to restart")
        for s in on:
            last = self.store.last_auto_entry_ts(s)
            if last:
                mins = int((time.time() - last) / 60)
                L.append(f"• {s}: last auto entry {mins}m ago "
                         f"(min gap {PER_SYMBOL_MIN_GAP_SECONDS // 3600}h)")
            else:
                L.append(f"• {s}: no auto entry yet")
            L.extend(self.starvation.summary_lines(s))
        return L

    # ---- the cycle ----
    def run_symbol(self, symbol, snapshot=None, now=None):
        """Evaluate one symbol, place at most one demo ladder, and track health.

        A thin wrapper so that EVERY exit path of _evaluate_symbol feeds the
        starvation tracker — nine `return` statements is nine chances to forget,
        and the thing we are detecting is precisely a failure that went
        unnoticed for a month.
        """
        now = time.time() if now is None else now
        result = self._evaluate_symbol(symbol, snapshot=snapshot, now=now)
        try:
            alarm = self.starvation.record(result, now=now)
        except Exception:                       # never let telemetry break trading
            log.exception("starvation tracking failed for %s", symbol)
            return result
        if alarm:
            self._announce(alarm)
        return result

    def _evaluate_symbol(self, symbol, snapshot=None, now=None):
        """Evaluate one symbol and place at most one demo ladder.

        Returns a dict describing what happened and why — every branch reports,
        so a quiet autopilot can always be asked what it has been thinking.
        """
        now = time.time() if now is None else now
        symbol = symbol.upper()

        halted, reason = guards.is_halted()
        if halted:
            return self._skip(symbol, "halted", f"trading halted: {reason}")
        if not self.is_enabled(symbol):
            return self._skip(symbol, "disabled", "autopilot is off for this symbol")

        snap = snapshot if snapshot is not None else self._snapshot(symbol)
        if snap.error:
            return self._skip(symbol, "no_data", snap.error)

        setup = "B" if snap.setup_b else ("A" if snap.setup_a else None)
        if setup is None:
            return self._skip(symbol, "no_setup", snap.verdict_line())

        # Edge trigger: act once per ACTIVATION of the setup. The flag is
        # cleared when the setup drops out (see store.save_setup_state), so a
        # setup that stays true for hours produces exactly one entry.
        state = self.store.setup_state(symbol, setup)
        if state.get("active") and state.get("auto_acted_ts"):
            return self._skip(symbol, "already_acted",
                              f"setup {setup} already traded on this activation")

        last = self.store.last_auto_entry_ts(symbol)
        if last and (now - last) < PER_SYMBOL_MIN_GAP_SECONDS:
            wait = int((PER_SYMBOL_MIN_GAP_SECONDS - (now - last)) / 60)
            return self._skip(symbol, "rate_limited",
                              f"autopilot took {symbol} {int((now - last) / 60)}m ago; "
                              f"{wait}m until the next one is allowed")

        open_here = [p for p in self.store.active_positions(mode="DEMO")
                     if p["symbol"] == symbol]
        if open_here:
            return self._skip(symbol, "position_open",
                              f"already holding {symbol} (#{open_here[0]['id']})")

        return self._attempt(symbol, setup, snap, now)

    def venue_leverage(self, symbol):
        """The account leverage last observed on the exchange for this symbol."""
        raw = self.store.get_meta(f"venue_leverage:{symbol.upper()}")
        try:
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def _snapshot(self, symbol):
        anchor = self.store.get_anchor(symbol)
        return signals.evaluate(symbol, anchor_ts=(anchor or {}).get("anchor_ts"),
                                client=self.client)

    def _attempt(self, symbol, setup, snap, now):
        kwargs = (setup_b_plan_kwargs(snap) if setup == "B"
                  else setup_a_plan_kwargs(snap))
        try:
            plan = ladder.build_plan(self.client, symbol, "LONG", self.notional,
                                     leverage=self.leverage, **kwargs)
        except guards.GuardViolation as e:
            self._announce(f"🤖 AUTOPILOT stood down on {symbol} (setup {setup})\n"
                           f"🚫 guards refused: {e}")
            return self._skip(symbol, "guard_refused", str(e))
        except (ladder.LadderError, WeexError) as e:
            log.warning("autopilot could not build a %s plan for %s: %s", setup, symbol, e)
            return self._skip(symbol, "plan_failed", str(e))

        # Autopilot has NO override authority. Any objection ends the attempt.
        advice = advisor_mod.review(symbol, "LONG", entry_price=plan.entry,
                                    snapshot=snap, client=self.client,
                                    stop_price=plan.stop, leverage=plan.leverage,
                                    venue_leverage=self.venue_leverage(symbol))
        if advice.contradicted:
            detail = "; ".join(advice.contradictions)
            self.store.log_event("auto_refused",
                                 f"{symbol} setup {setup}: {detail}")
            self._announce(
                f"🤖 AUTOPILOT stood down on {symbol} (setup {setup})\n"
                f"The advisor objected, and autopilot has no override:\n"
                + "\n".join(f"  ✗ {c}" for c in advice.contradictions))
            return self._skip(symbol, "advisor_refused", detail)

        # Final routing proof, immediately before the wire call.
        try:
            assert_demo(self.client, "Autopilot._attempt")
        except AutopilotSafetyError as e:
            self._emergency_halt(str(e))
            return self._skip(symbol, "unsafe_routing", str(e))

        self._announce(
            f"🤖 AUTOPILOT entering {symbol} — setup {setup} confirmed\n"
            f"{plan.describe(self.client.routing_line(symbol))}\n\n"
            f"Justification:\n{snap.render()}")

        try:
            result = ladder.place_plan(self.client, plan, store=self.store,
                                       notify=self.notify, advice=advice,
                                       origin=origin_mod.AUTO)
        except (WeexError, guards.GuardViolation) as e:
            self._announce(f"🤖 AUTOPILOT order failed on {symbol}: {e}")
            return self._skip(symbol, "order_failed", str(e))

        self.store.record_auto_entry(symbol, now)
        st = self.store.setup_state(symbol, setup)
        self.store.save_setup_state(symbol, setup, True, st.get("last_alert_ts") or now,
                                    auto_acted_ts=now)
        self.store.log_event("auto_entry",
                             json.dumps({"symbol": symbol, "setup": setup,
                                         "status": result.get("status"),
                                         "client_id": result.get("client_id")}),
                             result.get("position_id"))
        return {"symbol": symbol, "action": "entered", "setup": setup,
                "status": result.get("status"), "client_id": result.get("client_id"),
                "position_id": result.get("position_id")}

    def _emergency_halt(self, detail):
        """Routing read non-demo inside autopilot: stop everything, loudly."""
        log.error("AUTOPILOT SAFETY TRIP: %s", detail)
        self.disable()
        guards.halt(f"autopilot safety trip: {detail}", source="autopilot_safety")
        self.store.log_event("auto_safety_trip", detail)
        self._announce("🛑 AUTOPILOT SAFETY TRIP — routing did not read DEMO.\n"
                       f"{detail}\nAutopilot disabled for every symbol and trading "
                       f"halted. This should be impossible; investigate before /resume.")

    def _skip(self, symbol, code, detail):
        log.info("autopilot %s: %s (%s)", symbol, code, detail)
        return {"symbol": symbol, "action": "skipped", "reason": code, "detail": detail}

    def _announce(self, text):
        log.info("%s", text.replace("\n", " | ")[:400])
        if self.notify:
            self.notify.alert(text)
