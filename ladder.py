#!/usr/bin/env python3
"""ARMM ladder engine v0.2 — laddered entry with an atomic stop.

DEMO MODE by default. Every entry path in the bot funnels through build_plan()
+ place_plan() here, and build_plan() calls guards.check_entry(). There is no
other way to reach the order endpoint.

Usage:
  python3 ladder.py BTCUSDT LONG --offset 40 --stop-pct 1 \
      --targets-pct 0.5,1,1.5 --notional 500
  python3 ladder.py BTCUSDT LONG --entry 108500 --stop 107400 \
      --targets 109000,109550,110100 --notional 2000 [--live]

Entry modes are mutually exclusive: --entry (absolute) | --offset (points off
the live last price) | --offset-pct (percent off it). The live price is fetched
immediately before placing, and a POST_ONLY entry that would cross the book is
repriced off the fresh book instead of turning into a taker.

Demo constraints: sim allows 1 order per 10s (the client paces it) and has no
cancel endpoint, so a demo run carries a single atomic TP; the full tranche
ladder is the manager's job and is validated live-tiny later.
"""
import argparse, json, sys, time

import guards
import logsetup
import origin as origin_mod
import specs
import store as store_mod
from weex_client import WeexClient, WeexError

log = logsetup.get("ladder")

VETO_RECHECK_SECONDS = 3.0     # how long after an ack to look for a POST_ONLY veto
VETO_RETRY_PAUSE = 10.0        # sim pacing floor before the single retry


def _pause(seconds):
    """Indirection so tests can run the placement path without real waiting.
    Production always sleeps: the veto check is only meaningful after the venue
    has had time to act on the order."""
    time.sleep(seconds)


class LadderError(Exception):
    pass


# ---------------- pure pricing math (unit-tested, no network) ----------------

def resolve_entry(side, last, book, tick, entry=None, offset=None, offset_pct=None,
                  maker=True):
    """Compute the entry price and say whether the book forced a reprice.

    Returns (price, reprice_reason_or_None). `book` is {"bid":..,"ask":..}.
    """
    given = [x for x in (entry, offset, offset_pct) if x is not None]
    if len(given) != 1:
        raise LadderError("exactly one of --entry / --offset / --offset-pct is required")

    if entry is not None:
        px = float(entry)
    elif offset is not None:
        px = float(last) - float(offset) if side == "LONG" else float(last) + float(offset)
    else:
        f = float(offset_pct) / 100.0
        px = float(last) * (1 - f) if side == "LONG" else float(last) * (1 + f)

    # Round away from the market so a maker entry stays a maker entry.
    px = specs.round_to_tick(px, tick, "down" if side == "LONG" else "up")

    if not maker or not book:
        return px, None

    bid, ask = float(book["bid"]), float(book["ask"])
    if side == "LONG" and px >= ask:
        newpx = specs.round_to_tick(specs.shift_ticks(ask, tick, -1), tick, "down")
        return newpx, f"entry {px} >= best ask {ask}; repriced to {newpx} to stay maker"
    if side == "SHORT" and px <= bid:
        newpx = specs.round_to_tick(specs.shift_ticks(bid, tick, 1), tick, "up")
        return newpx, f"entry {px} <= best bid {bid}; repriced to {newpx} to stay maker"
    return px, None


def derive_stop(side, entry, tick, stop=None, stop_pct=None):
    if stop is not None and stop_pct is not None:
        raise LadderError("use --stop or --stop-pct, not both")
    if stop is not None:
        px = float(stop)
    elif stop_pct is not None:
        f = float(stop_pct) / 100.0
        px = float(entry) * (1 - f) if side == "LONG" else float(entry) * (1 + f)
    else:
        raise LadderError("a stop is mandatory — pass --stop or --stop-pct")
    # Round away from entry: a stop never lands closer than asked.
    return specs.round_to_tick(px, tick, "down" if side == "LONG" else "up")


def derive_targets(side, entry, tick, targets=None, targets_pct=None):
    if targets and targets_pct:
        raise LadderError("use --targets or --targets-pct, not both")
    if targets:
        vals = [float(x) for x in targets]
    elif targets_pct:
        vals = []
        for p in targets_pct:
            f = float(p) / 100.0
            vals.append(float(entry) * (1 + f) if side == "LONG" else float(entry) * (1 - f))
    else:
        raise LadderError("at least one target is required")
    # Round away from entry so a target is never quietly tightened.
    mode = "up" if side == "LONG" else "down"
    out = [specs.round_to_tick(v, tick, mode) for v in vals]
    for t in out:
        if side == "LONG" and t <= entry:
            raise LadderError(f"LONG target {t} must be above entry {entry}")
        if side == "SHORT" and t >= entry:
            raise LadderError(f"SHORT target {t} must be below entry {entry}")
    return sorted(out, reverse=(side == "SHORT"))


def normalize_splits(splits, n):
    """Percent splits per target, defaulting to as-even-as-possible."""
    if not splits:
        base = round(100.0 / n, 4)
        out = [base] * n
        out[-1] = round(100.0 - base * (n - 1), 4)
        return out
    vals = [float(x) for x in splits]
    if len(vals) != n:
        raise LadderError(f"{len(vals)} splits for {n} targets")
    if abs(sum(vals) - 100.0) > 0.51:
        raise LadderError(f"splits sum to {sum(vals)}, not 100")
    return vals


def size_position(notional, entry, step, min_qty):
    qty = specs.floor_to_step(float(notional) / float(entry), step)
    if qty < min_qty or qty <= 0:
        raise LadderError(
            f"notional ${notional:.2f} at {entry} gives qty {qty}, below the "
            f"venue minimum {min_qty} — raise --notional")
    return qty


def tranche_plan(qty, targets, splits, step):
    """Split `qty` across targets. The last tranche absorbs the rounding
    remainder so the tranches always sum to exactly qty."""
    from decimal import Decimal
    total, allocated, out = Decimal(str(qty)), Decimal(0), []
    n = min(len(targets), len(splits))
    for i, (t, pct) in enumerate(zip(targets, splits)):
        if i == n - 1:
            q = specs.floor_to_step(total - allocated, step)   # remainder, exactly
        else:
            q = specs.floor_to_step(total * Decimal(str(pct)) / 100, step)
        allocated += Decimal(str(q))
        out.append({"index": i + 1, "price": t, "qty": q, "pct": pct})
    return [t for t in out if t["qty"] > 0]


# ---------------- the plan ----------------

class Plan:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def order_side(self):
        return "BUY" if self.side == "LONG" else "SELL"

    @property
    def close_side(self):
        return "SELL" if self.side == "LONG" else "BUY"

    @property
    def margin(self):
        return self.notional / self.leverage

    def describe(self, routing_line):
        lines = [
            f"[{self.mode}] {self.side} {self.symbol}: qty {specs.fmt(self.qty, self.spec['step_size'])} "
            f"@ {specs.fmt(self.entry, self.spec['tick_size'])} "
            f"(notional ${self.notional:.0f}, margin ${self.margin:.0f} at {self.leverage}x)",
            f"  stop {specs.fmt(self.stop, self.spec['tick_size'])} (atomic) | "
            f"targets {[specs.fmt(t, self.spec['tick_size']) for t in self.targets]} "
            f"| splits {self.splits} | entry {self.tif}",
            f"  {routing_line}",
        ]
        if self.entry_source:
            lines.insert(1, f"  entry from {self.entry_source} (last {self.last}, "
                            f"book {self.book['bid']}/{self.book['ask']})")
        if self.reprice_reason:
            lines.insert(2, f"  ! {self.reprice_reason}")
        tranches = tranche_plan(self.qty, self.targets, self.splits, self.spec["step_size"])
        lines.append("  tranches: " + ", ".join(
            f"T{t['index']} {specs.fmt(t['qty'], self.spec['step_size'])} @ "
            f"{specs.fmt(t['price'], self.spec['tick_size'])}" for t in tranches))
        if self.mode == "DEMO":
            lines.append("  DEMO limitation: sim has no cancel/conditional endpoint, so only "
                         "T1 is attached atomically; the manager tracks T2+ virtually.")
            lines.append(f"  DEMO limitation: sim has no leverage endpoint — {self.leverage}x "
                         "drives sizing and the guard cap here, but the sim account's own "
                         "leverage setting governs margin and liquidation.")
        return "\n".join(lines)


def build_plan(client, symbol, side, notional, leverage=5, entry=None, offset=None,
               offset_pct=None, stop=None, stop_pct=None, targets=None,
               targets_pct=None, splits=None, reference=None, step=None,
               tif="POST_ONLY", open_position_count=None, spec=None):
    """Fetch fresh market data, price the ladder, and run it past the guards.

    Raises guards.GuardViolation or LadderError. Places nothing.
    """
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise LadderError(f"side must be LONG or SHORT, got {side}")

    spec = dict(spec or specs.get(symbol, client=client))
    if step:                                   # --step remains an override
        spec["step_size"] = float(step)

    last = client.last_price(symbol)
    book = client.book_ticker(symbol)
    px, reprice = resolve_entry(side, last, book, spec["tick_size"], entry=entry,
                                offset=offset, offset_pct=offset_pct,
                                maker=(tif == "POST_ONLY"))
    stop_px = derive_stop(side, px, spec["tick_size"], stop=stop, stop_pct=stop_pct)
    tgts = derive_targets(side, px, spec["tick_size"], targets=targets, targets_pct=targets_pct)
    split_pcts = normalize_splits(splits, len(tgts))
    qty = size_position(notional, px, spec["step_size"], spec["min_qty"])

    if spec.get("max_qty") and qty > spec["max_qty"]:
        raise LadderError(f"qty {qty} exceeds venue max order size {spec['max_qty']}")

    if open_position_count is None:
        try:
            open_position_count = len(client.open_positions())
        except WeexError as e:
            log.warning("could not fetch positions for the concurrency guard: %s", e)
            open_position_count = 0

    entry_source = ("--entry (absolute)" if entry is not None else
                    f"--offset {offset}" if offset is not None else
                    f"--offset-pct {offset_pct}%")

    # THE GATE. Nothing below this line runs if a rule is breached.
    guards.check_entry(symbol, notional, leverage, stop_px, px, side,
                       reference_price=reference,
                       open_position_count=open_position_count)

    inputs = dict(symbol=symbol, side=side, notional=notional, leverage=leverage,
                  entry=entry, offset=offset, offset_pct=offset_pct, stop=stop,
                  stop_pct=stop_pct, targets=targets, targets_pct=targets_pct,
                  splits=splits, reference=reference, step=step, tif=tif)

    return Plan(inputs=inputs,
                symbol=symbol, side=side, mode=client.mode, entry=px, stop=stop_px,
                targets=tgts, splits=split_pcts, qty=qty, notional=float(notional),
                leverage=int(leverage), spec=spec, book=book, last=last,
                reference=reference, reprice_reason=reprice, tif=tif,
                entry_source=entry_source,
                open_position_count=open_position_count)


# ---------------- placement ----------------

def apply_leverage(client, plan):
    """Push the guard's leverage cap onto the venue before the order goes in.

    Sizing math alone does not bound risk: the exchange applies the ACCOUNT's
    leverage to margin and liquidation. Live mode sets it; demo cannot (the sim
    venue has no leverage endpoint), so we say so out loud rather than let the
    printed "at 5x" imply an enforcement that is not there.
    """
    try:
        r = client.set_leverage(plan.symbol, plan.leverage, side=plan.side)
        log.info("venue leverage for %s set to %sx: %s", plan.symbol, plan.leverage, r)
        return True
    except WeexError as e:
        if client.demo:
            log.warning("DEMO: leverage not enforced on the venue (%s) — the sim "
                        "account's own setting governs margin and liquidation; "
                        "'%sx' here drives sizing and the guard cap only",
                        e, plan.leverage)
        else:
            log.error("could not set venue leverage for %s: %s", plan.symbol, e)
        return False


def _veto_check(client, symbol, client_id):
    """A POST_ONLY order that would have crossed comes back CANCELED with nothing
    filled. Returns the order row if vetoed, else None.

    NOTE (verified 2026-09-01): the sim order-history endpoint lists only
    TERMINAL orders. A resting order is simply absent, so "not found" means
    "still working", not "lost" — we hold the ack with its orderId either way.
    """
    try:
        o = client.find_order(client_id, symbol)
    except WeexError as e:
        log.warning("could not read back order %s: %s", client_id, e)
        return None
    if not o:
        return None
    if str(o.get("status", "")).upper() == "CANCELED" and float(o.get("executedQty") or 0) == 0:
        return o
    return None


def place_plan(client, plan, store=None, notify=None, allow_retry=True, source="cli",
               advice=None, origin=None):
    """Place the entry (atomic stop + first target). One retry on a POST_ONLY veto.

    `advice` is an advisor.Advice the caller already showed the operator. It is
    recorded, never enforced — if it is contradicted and we got here, the
    operator overrode it deliberately and that fact belongs in the database.

    `origin` (cli | tg | auto | test) is stamped into the client id, so the
    EXCHANGE's own record says where the order came from — not just our db.

    Returns a result dict: {"status": RESTING|FILLED|VETOED|..., ...}
    """
    store = store if store is not None else store_mod.default()
    # Belt and braces: the gate again, immediately before the wire call.
    guards.check_entry(plan.symbol, plan.notional, plan.leverage, plan.stop,
                       plan.entry, plan.side, reference_price=plan.reference,
                       open_position_count=plan.open_position_count)

    who = origin_mod.normalize(origin if origin is not None else source)
    step, tick = plan.spec["step_size"], plan.spec["tick_size"]
    attempts = []
    apply_leverage(client, plan)

    for attempt in (1, 2):
        client_id = origin_mod.entry_id(who)
        position_id = store.create_position(
            plan.symbol, plan.side, plan.mode, plan.leverage, plan.notional,
            plan.entry, plan.qty, plan.stop, plan.targets, plan.splits,
            client_id, source=who)
        if advice is not None and getattr(advice, "contradicted", False):
            store.update_position(position_id, advisor_overridden=1,
                                  advisor_notes=advice.db_note())
            store.log_event("advisor_override",
                            f"{plan.side} {plan.symbol} placed against advice: "
                            f"{advice.db_note()}", position_id)
            log.warning("ADVISOR OVERRIDDEN for %s %s: %s", plan.side, plan.symbol,
                        advice.db_note())
        store.add_order(position_id, "ENTRY", client_id, client._sym(plan.symbol),
                        plan.order_side, plan.side, plan.entry, plan.qty, status="SENT")

        log.info("%s", plan.describe(client.routing_line(plan.symbol)))
        try:
            ack = client.place_order(
                plan.symbol, plan.order_side, plan.side,
                specs.fmt(plan.qty, step), price=specs.fmt(plan.entry, tick),
                tif=plan.tif, sl_trigger=specs.fmt(plan.stop, tick),
                tp_trigger=specs.fmt(plan.targets[0], tick), client_id=client_id)
        except WeexError as e:
            store.update_position(position_id, state=store_mod.CLOSED,
                                  closed_at=time.time())
            store.update_order(client_id, status="ERROR")
            store.log_event("entry_error", str(e), position_id)
            if notify:
                notify.alert(f"{origin_mod.label(who)} · ❌ entry error "
                             f"{plan.symbol} {plan.side}: {e}")
            raise

        order_id = str((ack or {}).get("orderId") or "")
        store.update_order(client_id, order_id=order_id, status="NEW")
        store.update_position(position_id, entry_order_id=order_id)
        store.log_event("entry_placed",
                        json.dumps({"client_id": client_id, "order_id": order_id,
                                    "price": plan.entry, "qty": plan.qty}), position_id)
        attempts.append({"client_id": client_id, "order_id": order_id,
                         "price": plan.entry, "position_id": position_id})

        _pause(VETO_RECHECK_SECONDS)
        vetoed = _veto_check(client, plan.symbol, client_id)
        if not vetoed:
            row = None
            try:
                row = client.find_order(client_id, plan.symbol)
            except WeexError:
                pass
            status = str((row or {}).get("status") or "NEW").upper()
            filled = float((row or {}).get("executedQty") or 0)
            inferred = row is None
            if filled > 0:
                store.update_position(position_id, state=store_mod.OPEN,
                                      filled_qty=filled,
                                      avg_entry=float(row.get("avgPrice") or plan.entry))
            store.update_order(client_id, status=status)
            if notify:
                notify.alert(f"{origin_mod.label(who)} · entry {status} "
                             f"{plan.symbol} {plan.side} "
                             f"{specs.fmt(plan.qty, step)} @ {specs.fmt(plan.entry, tick)} "
                             f"[{plan.mode}] stop {specs.fmt(plan.stop, tick)}\n"
                             f"order {client_id}")
            result = {"status": "FILLED" if filled > 0 else ("RESTING" if status in
                      ("NEW", "PARTIALLY_FILLED") else status),
                      "order": row, "ack": ack, "position_id": position_id,
                      "client_id": client_id, "attempts": attempts, "plan": plan}
            if inferred:
                result["note"] = (f"status inferred: the venue acked orderId {order_id} "
                                  f"and the order is absent from history, which lists "
                                  f"only terminal orders — i.e. it is working, not filled "
                                  f"and not cancelled.")
            return result

        # POST_ONLY veto: the price moved into us between quote and ack.
        store.update_order(client_id, status="CANCELED")
        store.update_position(position_id, state=store_mod.CLOSED, closed_at=time.time())
        store.log_event("postonly_veto",
                        json.dumps({"client_id": client_id, "price": plan.entry}), position_id)
        log.warning("POST_ONLY veto on %s @ %s (attempt %d)", plan.symbol, plan.entry, attempt)
        if notify:
            notify.alert(f"{origin_mod.label(who)} · ⚠️ POST_ONLY veto {plan.symbol} @ "
                         f"{specs.fmt(plan.entry, tick)} (attempt {attempt}) — price moved")

        if attempt == 2 or not allow_retry:
            return {"status": "VETOED", "order": vetoed, "attempts": attempts,
                    "plan": plan,
                    "message": "POST_ONLY vetoed twice — price is moving away from "
                               "the maker side; no order is resting."}

        # Retry exactly once, re-derived from the operator's ORIGINAL intent
        # against fresh market data. Deliberately NOT a jump to the touch: an
        # "--offset 40" entry must stay 40 points below the market on the retry.
        # Turning it into a bid-join would be exactly the chase the guards exist
        # to stop. build_plan re-runs those guards on the new price. The client's
        # own 10s sim pacing is what actually gates the second placement.
        _pause(VETO_RETRY_PAUSE)
        plan = build_plan(client, **plan.inputs)
        plan.reprice_reason = ("retry after POST_ONLY veto: re-derived from the "
                               "original entry spec against fresh market data")

    return {"status": "VETOED", "attempts": attempts, "plan": plan}


# ---------------- CLI ----------------

def _csv(v):
    return [x.strip() for x in v.split(",") if x.strip()]


def main(argv=None):
    p = argparse.ArgumentParser(description="ARMM ladder entry (demo by default)")
    p.add_argument("symbol")
    p.add_argument("side", choices=["LONG", "SHORT", "long", "short"])
    entry_g = p.add_mutually_exclusive_group(required=True)
    entry_g.add_argument("--entry", type=float, help="absolute entry price")
    entry_g.add_argument("--offset", type=float,
                         help="points below last (LONG) / above last (SHORT)")
    entry_g.add_argument("--offset-pct", type=float, help="same, in percent")
    stop_g = p.add_mutually_exclusive_group(required=True)
    stop_g.add_argument("--stop", type=float, help="absolute stop price")
    stop_g.add_argument("--stop-pct", type=float, help="stop as %% from the final entry")
    tgt_g = p.add_mutually_exclusive_group(required=True)
    tgt_g.add_argument("--targets", type=_csv, help="comma-separated absolute prices")
    tgt_g.add_argument("--targets-pct", type=_csv, help="comma-separated %% from entry")
    p.add_argument("--splits", type=_csv, help="tranche %% per target, e.g. 33,33,34")
    p.add_argument("--notional", type=float, required=True)
    p.add_argument("--leverage", type=int, default=5)
    p.add_argument("--reference", type=float, help="anchor/aVWAP price for the chase guard")
    p.add_argument("--step", type=float, help="override the venue quantity step size")
    p.add_argument("--tif", default="POST_ONLY",
                   choices=["POST_ONLY", "GTC", "IOC", "FOK"])
    p.add_argument("--live", action="store_true", help="LIVE trading (default: demo)")
    p.add_argument("--yes", action="store_true", help="skip the CONFIRM prompt")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and the routing line, place nothing")
    p.add_argument("--test-order", action="store_true",
                   help="label this order as a TEST (armm-test-* client id) — use "
                        "for acceptance tests so they never pollute the track record")
    p.add_argument("--no-advisor", action="store_true",
                   help="skip the signal advisor (it never blocks; this only "
                        "silences it and is still recorded as an override)")
    a = p.parse_args(argv)

    client = WeexClient(demo=not a.live)
    try:
        plan = build_plan(client, a.symbol.upper(), a.side.upper(), a.notional,
                          leverage=a.leverage, entry=a.entry, offset=a.offset,
                          offset_pct=a.offset_pct, stop=a.stop, stop_pct=a.stop_pct,
                          targets=a.targets, targets_pct=a.targets_pct,
                          splits=a.splits, reference=a.reference, step=a.step,
                          tif=a.tif)
    except guards.GuardViolation as e:
        print(f"REFUSED by guards: {e}", file=sys.stderr)
        return 2
    except (LadderError, WeexError, specs.SpecError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(plan.describe(client.routing_line(a.symbol)))

    # Advisor: never blocks, but a contradicted entry costs an extra keystroke.
    advice = None
    if not a.no_advisor:
        import advisor
        anchor = store_mod.default().get_anchor(a.symbol.upper())
        advice = advisor.review(a.symbol.upper(), a.side.upper(),
                                entry_price=plan.entry, client=client,
                                anchor_ts=(anchor or {}).get("anchor_ts"),
                                stop_price=plan.stop, leverage=plan.leverage)
        print()
        print(advice.render())

    if a.dry_run:
        print("dry-run: nothing placed")
        return 0
    if advice is not None and advice.contradicted and not a.yes:
        if input("\nType OVERRIDE to place against this advice: ") != "OVERRIDE":
            print("aborted — advisor not overridden")
            return 1
    if a.live and not a.yes:
        if input("Type CONFIRM to place LIVE orders: ") != "CONFIRM":
            print("aborted")
            return 1

    try:
        r = place_plan(client, plan, advice=advice,
                       origin=origin_mod.TEST if a.test_order else origin_mod.CLI)
    except (WeexError, guards.GuardViolation) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"result: {r['status']}")
    for key in ("message", "note"):
        if r.get(key):
            print(r[key])
    if r.get("order"):
        print("order:", json.dumps(r["order"]))
    if r["status"] == "RESTING":
        print("Entry is resting. Run manager.py to track the fill and the tranche ladder.")
    return 0 if r["status"] in ("RESTING", "FILLED") else 3


if __name__ == "__main__":
    sys.exit(main())
