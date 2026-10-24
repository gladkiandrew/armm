#!/usr/bin/env python3
"""ARMM position manager — the long-running half of the bot (systemd service).

Responsibilities:
  * poll orders + positions every 5s and drive the state machine
      PENDING_ENTRY -> OPEN -> EXITING -> CLOSED   (persisted in ~/armm/armm.db)
  * on entry fill, run the tranche engine
  * reconcile internal state against the exchange every 60s and alert on drift
  * heartbeat to Telegram every 6h

Capability gating, not two code paths: compute_tranches() and the dispatch loop
in place_tranches() are identical in both modes. Only the final wire call is
gated — LIVE places real reduceOnly maker limits; DEMO records the same tranche
rows as VIRTUAL, because the sim venue exposes no cancel and no conditional-order
endpoint, so a demo position can only carry the single atomic TP attached to the
entry. The full ladder is validated live-tiny later, exercising the same engine.
"""
import argparse, json, signal, sys, threading, time

from . import autopilot as autopilot_mod
from . import candles as candles_mod
from . import config
from . import guards
from . import ladder
from . import logsetup
from . import notify as notify_mod
from . import origin as origin_mod
from . import signals
from . import specs
from . import store as store_mod
from .weex_client import WeexClient, WeexError

log = logsetup.get("manager")

POLL_SECONDS = 5
RECONCILE_SECONDS = 60
HEARTBEAT_SECONDS = 6 * 3600

# Signal layer (v0.3). Setups are computed from CLOSED 1H bars, so evaluating
# faster than a few minutes just burns rate limit for an identical answer.
SIGNAL_SECONDS = 300
SETUP_REALERT_SECONDS = 3600
WEEKLY_DIGEST_WEEKDAY = 6      # Sunday (Monday=0)
WEEKLY_DIGEST_HOUR = 18        # local time

TERMINAL_ORDER_STATES = ("CANCELED", "REJECTED", "EXPIRED")


class Capabilities:
    """What the venue actually lets us do in this mode."""

    def __init__(self, demo):
        self.demo = demo
        self.cancel = not demo
        self.open_orders = not demo
        self.conditional_orders = not demo
        self.real_tranches = not demo

    def describe(self):
        return ("DEMO: single atomic TP only (no cancel, no conditional orders) — "
                "tranches tracked virtually" if self.demo else
                "LIVE: reduceOnly maker tranches + cancel available")


def compute_tranches(qty, targets, splits, step):
    """Pure. The tranche engine both modes share."""
    return ladder.tranche_plan(qty, targets, splits, step)


def r_multiple(pos, exit_price):
    """Realised R against the position's own stop distance. None if unknowable."""
    entry = pos.get("avg_entry") or pos.get("entry_price")
    stop = pos.get("stop_price")
    if not entry or not stop or not exit_price:
        return None
    risk = (entry - stop) if pos.get("side") == "LONG" else (stop - entry)
    if risk <= 0:
        return None
    move = (exit_price - entry) if pos.get("side") == "LONG" else (entry - exit_price)
    return move / risk


def tranche_client_id(position_id, index, origin="cli"):
    """Deterministic, so a restart mid-placement can never double-place:
    the same id is re-derived, the UNIQUE insert fails, and we skip."""
    return origin_mod.tranche_id(origin, position_id, index)


def tranche_id_candidates(position_id, index, origin="cli"):
    """The current id plus the pre-v0.4 one.

    v0.4 put the origin into tranche ids. A position opened under v0.3 already
    has rows (and possibly live orders) under the OLD id, so both must be
    checked before placing — otherwise the upgrade itself would double-place
    every open position's ladder.
    """
    return (tranche_client_id(position_id, index, origin),
            origin_mod.legacy_tranche_id(position_id, index))


class Manager:
    def __init__(self, demo=True, symbols=None, poll=POLL_SECONDS,
                 reconcile_every=RECONCILE_SECONDS, notifier=None, store=None,
                 client=None, once=False):
        self.demo = demo
        self.client = client or WeexClient(demo=demo)
        self.caps = Capabilities(demo)
        self.store = store or store_mod.default()
        self.notify = notifier if notifier is not None else notify_mod.default()
        self.symbols = [s.upper() for s in (symbols or ["BTCUSDT"])]
        self.poll = poll
        self.reconcile_every = reconcile_every
        self.once = once
        self._stop = threading.Event()
        self._last_reconcile = 0.0
        self._last_heartbeat = 0.0
        self._last_signals = 0.0
        # Autopilot is DEMO-ONLY by construction. In a live-mode manager we do
        # not build one at all — there is no such thing as live autopilot.
        self.autopilot = None
        if self.demo:
            try:
                self.autopilot = autopilot_mod.Autopilot(
                    store=self.store, notify=self.notify, client=self.client)
            except autopilot_mod.AutopilotSafetyError as e:
                log.error("autopilot NOT started: %s", e)
        else:
            log.info("live-mode manager: autopilot is not constructed "
                     "(autopilot is demo-only by design)")

    # ---------------- lifecycle ----------------

    def stop(self, *_):
        log.info("stop requested")
        self._stop.set()

    def run(self):
        log.info("manager starting — mode=%s symbols=%s | %s",
                 self.client.mode, ",".join(self.symbols), self.caps.describe())
        try:
            guards.set_day_start_equity(self.client.equity())
        except WeexError as e:
            log.warning("could not anchor day-start equity: %s", e)
        # Restart safety: reconcile BEFORE doing anything that could place an order.
        self.reconcile(startup=True)
        self._last_reconcile = time.monotonic()
        self._last_heartbeat = time.monotonic()
        self.notify.alert(f"🤖 ARMM manager up [{self.client.mode}] — "
                          f"{len(self.store.active_positions(mode=self.client.mode))} tracked positions")
        while not self._stop.is_set():
            try:
                self.tick()
            except WeexError as e:
                log.warning("poll error: %s", e)
            except Exception:
                log.exception("unexpected error in manager tick")
            if self.once:
                break
            self._stop.wait(self.poll)
        log.info("manager stopped")

    def tick(self):
        for pos in self.store.active_positions(mode=self.client.mode):
            self.advance(pos)
        now = time.monotonic()
        if now - self._last_reconcile >= self.reconcile_every:
            self._last_reconcile = now
            self.reconcile()
        if now - self._last_heartbeat >= HEARTBEAT_SECONDS:
            self._last_heartbeat = now
            self.heartbeat()
        if now - self._last_signals >= SIGNAL_SECONDS:
            self._last_signals = now
            self.check_watches()
            self.run_autopilot()
            self.maybe_weekly_digest()

    def heartbeat(self):
        n = len(self.store.active_positions(mode=self.client.mode))
        halted, reason = guards.is_halted()
        state = f" | HALTED: {reason}" if halted else ""
        auto = self.store.auto_symbols()
        auto_txt = (" | autopilot ON: " + ", ".join(auto)) if auto else " | autopilot off"
        self.notify.alert(f"💓 ARMM alive [{self.client.mode}], {n} positions"
                          f"{state}{auto_txt}")

    # ---------------- signal layer (v0.3, advisory only) ----------------

    def check_watches(self, now=None):
        """Evaluate every watched symbol and alert on a setup EDGE.

        Edge-triggered by design: a setup that stays true for six hours must not
        produce six hours of alerts. State lives in the setup_state table, so the
        edge survives a restart — a setup already active when the process died
        does not re-fire on boot.

        This path is advisory. It computes indicators and sends messages; it
        cannot place an order, and nothing it returns feeds the order path.
        """
        watched = self.store.watches()
        if not watched:
            return []
        now = time.time() if now is None else now
        fired = []
        for symbol in watched:
            try:
                snap = self.evaluate_symbol(symbol)
            except Exception as e:
                log.warning("signal evaluation failed for %s: %s", symbol, e)
                continue
            if snap.error:
                log.info("signal snapshot for %s unavailable: %s", symbol, snap.error)
                continue
            for setup, is_on in (("A", snap.setup_a), ("B", snap.setup_b)):
                if self._maybe_alert(symbol, setup, is_on, snap, now):
                    fired.append((symbol, setup))
        return fired

    def evaluate_symbol(self, symbol):
        anchor = self.store.get_anchor(symbol)
        return signals.evaluate(symbol, anchor_ts=(anchor or {}).get("anchor_ts"),
                                client=self.client)

    def _maybe_alert(self, symbol, setup, is_on, snap, now):
        """One alert per false->true transition, with a re-alert cooldown."""
        state = self.store.setup_state(symbol, setup)
        was_on = bool(state.get("active"))
        last = float(state.get("last_alert_ts") or 0)

        if not is_on:
            if was_on:
                self.store.save_setup_state(symbol, setup, False, last)
                self.store.log_event("setup_off", f"{symbol} setup {setup} no longer met")
            return False

        if was_on:
            return False                     # still true — already alerted
        # `last` of 0 means we have NEVER alerted for this symbol/setup. Without
        # the truthiness check the very first alert is swallowed whenever the
        # clock reads less than the cooldown since the epoch.
        if last and now - last < SETUP_REALERT_SECONDS:
            # Flipped off and back on inside the cooldown: record the state but
            # stay quiet, so a setup flickering on the boundary cannot spam.
            self.store.save_setup_state(symbol, setup, True, last)
            log.info("%s setup %s re-confirmed inside the %ds cooldown — not alerting",
                     symbol, setup, SETUP_REALERT_SECONDS)
            return False

        self.store.save_setup_state(symbol, setup, True, now)
        self.store.log_event("setup_alert", f"{symbol} setup {setup} confirmed")
        headline = ("🟢 SETUP A forming — Reload (longer-term long)" if setup == "A"
                    else "🚀 SETUP B 3/3 confirmed — Momentum")
        self.notify.alert(f"{headline}\n\n{snap.render()}")
        return True

    def maybe_weekly_digest(self, now=None):
        """Post the AUTO-vs-MANUAL summary once on Sunday evening.

        Keyed on the ISO year+week already posted, so a restart during the
        window cannot produce a second one.
        """
        import datetime
        from . import scoreboard
        stamp = datetime.datetime.fromtimestamp(now or time.time())
        if stamp.weekday() != WEEKLY_DIGEST_WEEKDAY or stamp.hour < WEEKLY_DIGEST_HOUR:
            return False
        iso = stamp.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        if self.store.get_meta("weekly_digest") == key:
            return False
        self.store.set_meta("weekly_digest", key)
        self.notify.alert(scoreboard.weekly_digest(self.store))
        self.store.log_event("weekly_digest", key)
        return True

    def run_autopilot(self, now=None):
        """Give autopilot a turn. Returns the per-symbol results.

        /halt is checked inside Autopilot.run_symbol on every symbol, so a halt
        raised mid-cycle stops the remaining symbols in the same pass rather
        than after it.
        """
        if not self.autopilot:
            return []
        symbols = self.autopilot.enabled_symbols()
        if not symbols:
            return []
        out = []
        for sym in symbols:
            try:
                out.append(self.autopilot.run_symbol(sym, now=now))
            except autopilot_mod.AutopilotSafetyError as e:
                log.error("autopilot safety error on %s: %s", sym, e)
                out.append({"symbol": sym, "action": "skipped",
                            "reason": "safety", "detail": str(e)})
            except Exception:
                log.exception("autopilot failed on %s", sym)
        return out

    # ---------------- state machine ----------------

    def _exchange_order(self, symbol, client_id):
        try:
            return self.client.find_order(client_id, symbol)
        except WeexError as e:
            log.warning("order lookup failed for %s: %s", client_id, e)
            return None

    def advance(self, pos):
        """Drive one position one step. Never places an order without a fill."""
        state = pos["state"]
        if state == store_mod.PENDING_ENTRY:
            self._check_entry(pos)
        elif state in (store_mod.OPEN, store_mod.EXITING):
            self._check_exit(pos)

    def _check_entry(self, pos):
        o = self._exchange_order(pos["symbol"], pos["entry_client_id"])
        if not o:
            return
        status = str(o.get("status", "")).upper()
        filled = float(o.get("executedQty") or 0)
        self.store.update_order(pos["entry_client_id"], status=status)
        if filled > 0 and status in ("FILLED", "PARTIALLY_FILLED"):
            avg = float(o.get("avgPrice") or pos["entry_price"] or 0) or pos["entry_price"]
            self.store.update_position(pos["id"], state=store_mod.OPEN,
                                       filled_qty=filled, avg_entry=avg)
            self.store.log_event("entry_filled",
                                 json.dumps({"qty": filled, "avg": avg}), pos["id"])
            self.notify.alert(
                f"{origin_mod.label(pos.get('source'))} · 🟢 FILL {pos['symbol']} "
                f"{pos['side']} {filled} @ {avg} [{pos['mode']}] — placing tranche ladder\n"
                f"order {pos['entry_client_id']}")
            self.place_tranches(self.store.get_position(pos["id"]))
        elif status in TERMINAL_ORDER_STATES:
            self.store.update_position(pos["id"], state=store_mod.CLOSED,
                                       closed_at=time.time())
            self.store.log_event("entry_" + status.lower(), json.dumps(o), pos["id"])
            self.notify.alert(f"{origin_mod.label(pos.get('source'))} · ⚠️ entry {status} "
                              f"{pos['symbol']} {pos['side']} @ {pos['entry_price']} "
                              f"— nothing resting")

    def _check_exit(self, pos):
        """Watch the position size and the tranche orders."""
        live = None
        for p in self._safe_positions():
            if p["symbol"] == pos["symbol"] and p["side"] == pos["side"]:
                live = p
                break

        if live is None:
            self._close_position(pos, "position no longer on the exchange")
            return

        if self.caps.real_tranches:
            for row in self.store.orders_for(pos["id"], kind="TP"):
                if row["status"] in ("FILLED",) + TERMINAL_ORDER_STATES:
                    continue
                o = self._exchange_order(pos["symbol"], row["client_id"])
                if not o:
                    continue
                st = str(o.get("status", "")).upper()
                if st != row["status"]:
                    self.store.update_order(row["client_id"], status=st)
                    if st == "FILLED":
                        self.store.update_position(pos["id"], state=store_mod.EXITING)
                        self.notify.alert(
                            f"🎯 TP{row['kind']} hit {pos['symbol']} {row['qty']} @ {row['price']}")
        else:
            self._check_virtual_tranches(pos)

    def _check_virtual_tranches(self, pos):
        """DEMO: no cancel and no conditional orders, so the T2+ tranches cannot
        rest on the venue. Report the crossings instead — honestly labelled."""
        try:
            last = self.client.last_price(pos["symbol"])
        except WeexError:
            return
        hits = set(json.loads(pos.get("virtual_hits") or "[]"))
        targets = json.loads(pos.get("targets") or "[]")
        changed = False
        for i, t in enumerate(targets, start=1):
            if i in hits:
                continue
            crossed = (last >= t) if pos["side"] == "LONG" else (last <= t)
            if crossed:
                hits.add(i)
                changed = True
                note = ("filled by the atomic TP" if i == 1 else
                        "VIRTUAL — demo cannot rest this tranche")
                self.store.log_event("virtual_target_crossed",
                                     json.dumps({"target": i, "price": t, "last": last}),
                                     pos["id"])
                self.notify.alert(f"🎯 T{i} crossed {pos['symbol']} @ {last} "
                                  f"(target {t}) — {note}")
        if changed:
            self.store.update_position(pos["id"], virtual_hits=json.dumps(sorted(hits)),
                                       state=store_mod.EXITING)

    def _close_position(self, pos, reason):
        exit_price, exit_source = self._observed_exit(pos)
        fields = {"state": store_mod.CLOSED, "closed_at": time.time(),
                  "exit_reason": reason}
        if exit_price:
            fields["exit_price"] = exit_price
            fields["exit_source"] = exit_source
            r = r_multiple(pos, exit_price)
            if r is not None:
                fields["r_multiple"] = r
        self.store.update_position(pos["id"], **fields)
        self.store.log_event("position_closed",
                             f"{reason} (exit {exit_price} via {exit_source})", pos["id"])
        guards.record_close(pos["symbol"])   # starts the 30m anti-revenge cooldown
        self.notify.alert(f"{origin_mod.label(pos.get('source'))} · 🔵 CLOSED "
                          f"{pos['symbol']} {pos['side']} — {reason}. "
                          f"{guards.COOLDOWN_MIN}m cooldown armed.")

    def _observed_exit(self, pos):
        """Best available exit price, and an HONEST label for where it came from.

        The sim venue records no fill for an atomic TP/SL trigger — the v0.2
        round trip left nothing in order history — so in DEMO the truest thing we
        can say is 'the price when the manager noticed the position was gone'.
        That is accurate to within one poll cycle, and the scoreboard labels it
        as an estimate rather than pretending it is a fill price.
        """
        # Live: prefer a real filled TP order if we have one.
        if not self.demo:
            for row in self.store.orders_for(pos["id"], kind="TP"):
                if row.get("status") == "FILLED" and row.get("price"):
                    return float(row["price"]), "tp_fill"
        try:
            return self.client.last_price(pos["symbol"]), "observed_at_close"
        except WeexError as e:
            log.warning("could not read an exit price for #%s: %s", pos["id"], e)
            return None, None

    # ---------------- tranche engine ----------------

    def place_tranches(self, pos):
        """Same engine in both modes; only the wire call is capability-gated.

        Crash safety: the row is written as PENDING_SEND *before* the wire call,
        so a crash mid-placement leaves an ambiguous row rather than a silent
        gap. On restart that row is resolved against the exchange by its
        deterministic client id — adopted if the order got there, re-sent with
        the SAME id if it did not. The id is the idempotency key, so even a
        racing double-send is rejected by the venue rather than doubling up.
        """
        step, tick = self._step_for(pos["symbol"]), self._tick_for(pos["symbol"])
        qty = float(pos["filled_qty"] or pos["qty"])
        targets = json.loads(pos["targets"] or "[]")
        splits = json.loads(pos["splits"] or "[]")
        tranches = compute_tranches(qty, targets, splits, step)
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        placed = adopted = resent = 0

        who = pos.get("source") or origin_mod.CLI
        for t in tranches:
            cid, legacy_cid = tranche_id_candidates(pos["id"], t["index"], who)
            # A row under the pre-v0.4 id means this ladder is already tracked.
            legacy_row = self.store.get_order(legacy_cid)
            if legacy_row:
                log.info("tranche %s already tracked under the legacy id %s (%s) — "
                         "not re-placing", cid, legacy_cid, legacy_row["status"])
                continue
            initial = "PENDING_SEND" if self.caps.real_tranches else "VIRTUAL"
            fresh = self.store.add_order(
                pos["id"], "TP", cid, pos["symbol"], close_side, pos["side"],
                t["price"], t["qty"], status=initial, reduce_only=True)

            if t["index"] == 1:
                # T1 already rides on the entry as the atomic tpTriggerPrice.
                self.store.update_order(cid, status="ATOMIC")
                continue
            if not self.caps.real_tranches:
                continue

            if not fresh:
                row = self.store.get_order(cid) or {}
                if row.get("status") != "PENDING_SEND":
                    log.info("tranche %s already settled as %s — no duplicate sent",
                             cid, row.get("status"))
                    continue
                # Ambiguous: we crashed around the wire call. Ask the exchange.
                if self._exchange_has(pos["symbol"], cid):
                    self.store.update_order(cid, status="NEW")
                    adopted += 1
                    log.warning("tranche %s was already on the exchange — adopted, "
                                "not replaced", cid)
                    continue
                resent += 1
                log.warning("tranche %s never reached the exchange — re-sending "
                            "under the same client id", cid)
            elif self._exchange_has(pos["symbol"], cid):
                self.store.update_order(cid, status="NEW")
                adopted += 1
                continue

            try:
                ack = self.client.place_order(
                    pos["symbol"], close_side, pos["side"], specs.fmt(t["qty"], step),
                    price=specs.fmt(t["price"], tick), tif="POST_ONLY",
                    client_id=cid, reduce_only=True)
                self.store.update_order(cid, order_id=str((ack or {}).get("orderId") or ""),
                                        status="NEW")
                placed += 1
            except WeexError as e:
                self.store.update_order(cid, status="ERROR")
                self.notify.alert(f"❌ tranche {cid} rejected: {e}")
                log.error("tranche %s failed: %s", cid, e)

        self.store.update_position(pos["id"], tranches_placed=len(tranches))
        msg = (f"📊 tranche ladder {pos['symbol']}: {len(tranches)} levels, "
               f"{placed} placed"
               + (f", {resent} re-sent after a crash" if resent else "")
               + (f", {adopted} adopted from the exchange" if adopted else "")
               + ("" if self.caps.real_tranches else
                  " (DEMO: T1 atomic, T2+ tracked virtually — no cancel endpoint)"))
        self.notify.alert(msg)
        return tranches

    def _step_for(self, symbol):
        try:
            return specs.get(symbol, client=self.client)["step_size"]
        except specs.SpecError:
            return 0.0001

    def _tick_for(self, symbol):
        try:
            return specs.get(symbol, client=self.client)["tick_size"]
        except specs.SpecError:
            return 0.1

    def _exchange_has(self, symbol, client_id):
        return self._exchange_order(symbol, client_id) is not None

    # ---------------- reconciliation ----------------

    def _safe_positions(self):
        try:
            positions = self.client.open_positions()
        except WeexError as e:
            log.warning("position fetch failed: %s", e)
            return []
        # The ACCOUNT's leverage governs real liquidation, and it is only
        # readable while a position exists. Remember it for the advisor, which
        # otherwise has to trust the figure we merely requested.
        for p in positions:
            lev = p.get("leverage")
            if lev:
                try:
                    self.store.set_meta(f"venue_leverage:{p['symbol']}", int(float(lev)))
                except (TypeError, ValueError):
                    pass
        return positions

    def reconcile(self, startup=False):
        """Compare internal state to the exchange. Log and alert on any drift.

        On startup this runs BEFORE the first tick, so a manager killed
        mid-position rebuilds from exchange truth and resumes without ever
        re-placing an order it already placed.
        """
        tag = "startup reconcile" if startup else "reconcile"
        exch = self._safe_positions()
        tracked = self.store.active_positions(mode=self.client.mode)
        drift = []

        exch_keys = {(p["symbol"], p["side"]) for p in exch}
        for pos in tracked:
            key = (pos["symbol"], pos["side"])
            if pos["state"] == store_mod.PENDING_ENTRY:
                o = self._exchange_order(pos["symbol"], pos["entry_client_id"])
                if o is None:
                    # Absence only means "missing" where we can enumerate resting
                    # orders. The sim venue lists TERMINAL orders only, so an
                    # absent demo entry is simply still working — provided the
                    # venue acked it with an order id. No ack, no order.
                    if self.caps.open_orders or not pos["entry_order_id"]:
                        drift.append(f"{pos['symbol']} #{pos['id']} PENDING_ENTRY but the "
                                     f"exchange has no order {pos['entry_client_id']}")
                    continue
                self._check_entry(pos)
                continue
            if key not in exch_keys:
                drift.append(f"{pos['symbol']} #{pos['id']} tracked {pos['state']} "
                             f"but flat on the exchange")
                self._close_position(pos, "reconcile: flat on the exchange")

        tracked_keys = {(p["symbol"], p["side"]) for p in
                        self.store.active_positions(mode=self.client.mode)}
        for p in exch:
            if (p["symbol"], p["side"]) not in tracked_keys:
                drift.append(f"UNTRACKED {p['symbol']} {p['side']} size {p['size']} "
                             f"open on the exchange")
                self._adopt(p)

        if drift:
            detail = "; ".join(drift)
            self.store.log_event("reconcile_drift", detail)
            self.notify.alert(f"🔎 {tag}: DRIFT — {detail}")
        else:
            log.info("%s: clean (%d tracked, %d on exchange)", tag, len(tracked), len(exch))
        return drift

    def _adopt(self, p):
        """Take an untracked exchange position into the DB rather than ignoring
        it — an unmanaged position with no stop is the worst possible state."""
        cid = origin_mod.adopt_id(origin_mod.CLI, p["symbol"], p["side"])
        pid = self.store.create_position(
            p["symbol"], p["side"], self.client.mode, 0, 0.0, p.get("entry") or 0.0,
            p["size"], None, [], [], cid, state=store_mod.OPEN, source="adopted")
        self.store.log_event("adopted", json.dumps(p), pid)
        self.notify.alert(f"⚠️ adopted UNTRACKED {p['symbol']} {p['side']} size {p['size']} "
                          f"— it has no ARMM stop on file. Check the exchange.")
        return pid


# ---------------- CLI ----------------

def main(argv=None):
    p = argparse.ArgumentParser(description="ARMM position manager (demo by default)")
    p.add_argument("--symbols", default="BTCUSDT", help="comma-separated watch list")
    p.add_argument("--poll", type=int, default=POLL_SECONDS)
    p.add_argument("--reconcile", type=int, default=RECONCILE_SECONDS)
    p.add_argument("--live", action="store_true", help="LIVE mode (default: demo)")
    p.add_argument("--telegram", action="store_true",
                   help="also run the Telegram cockpit in this process")
    p.add_argument("--allow-live-orders", action="store_true",
                   help="permit /ladder ... live from Telegram (off by default)")
    p.add_argument("--once", action="store_true", help="single tick, then exit")
    p.add_argument("--reconcile-only", action="store_true",
                   help="reconcile against the exchange, print drift, exit")
    p.add_argument("--auto-once", metavar="SYMBOL",
                   help="run one autopilot cycle for SYMBOL and exit (DEMO only)")
    p.add_argument("--check", metavar="SYMBOL",
                   help="print a signal snapshot for SYMBOL and exit (no orders)")
    a = p.parse_args(argv)

    if a.live:
        print("LIVE MODE: the manager will manage REAL positions.", file=sys.stderr)

    m = Manager(demo=not a.live, symbols=a.symbols.split(","), poll=a.poll,
                reconcile_every=a.reconcile, once=a.once)

    if a.auto_once:
        mgr = Manager(demo=True, symbols=[a.auto_once])   # demo-only by construction
        print(json.dumps(mgr.autopilot.run_symbol(a.auto_once.upper()), indent=1))
        return 0

    if a.check:
        mgr = Manager(demo=not a.live, symbols=[a.check])
        print(mgr.evaluate_symbol(a.check.upper()).render())
        return 0

    if a.reconcile_only:
        drift = m.reconcile(startup=True)
        print("drift:" if drift else "reconcile clean")
        for d in drift:
            print(" -", d)
        return 0

    signal.signal(signal.SIGTERM, m.stop)
    signal.signal(signal.SIGINT, m.stop)

    if a.telegram:
        from . import tg
        cockpit = tg.Cockpit(demo=not a.live, notifier=m.notify, store=m.store,
                             manager=m, allow_live=a.allow_live_orders)
        threading.Thread(target=cockpit.run, name="cockpit", daemon=True).start()

    m.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
