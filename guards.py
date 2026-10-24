#!/usr/bin/env python3
"""ARMM guards — hard-coded risk rules. The strategy can NEVER override these.
Derived from Andrew's own written trading rules.

This module is the SINGLE gate. Every entry path — CLI, Telegram, manager —
calls check_entry() before any order leaves the process. There is no bypass
flag, and nothing here consults an LLM.
"""
import copy, datetime, json, os, threading, time

import config

STATE_PATH = config.GUARD_STATE_PATH

MAX_LEVERAGE = 5            # hard cap; vol-derived per-symbol cap can only LOWER it
MAX_NOTIONAL_USD = 2500     # per position
MAX_CONCURRENT = 3          # positions
COOLDOWN_MIN = 30           # per-symbol lockout after closing (anti-revenge/chase)
CHASE_MAX_PCT = 2.0         # no entry > this % above reference/anchor price
DAILY_LOSS_HALT_PCT = 6.0   # flatten & halt for the day
STOP_REQUIRED = True        # no entry without slTriggerPrice (atomic)
MAKER_ONLY = True           # POST_ONLY unless explicit override

_lock = threading.Lock()

# NOTE: always deep-copy this. It contains a nested dict, and a shallow copy
# leaves callers sharing `last_close` with the module default — record_close()
# would then mutate the blank template itself, so every later "fresh" state
# would arrive pre-populated with a cooldown that was never persisted.
_BLANK = {"last_close": {}, "day": "", "day_pnl": 0.0, "day_start_equity": 0.0,
          "halted": False, "halt_reason": "", "halt_source": ""}


def _today():
    return datetime.date.today().isoformat()


def _load():
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except Exception:
        s = {}
    out = copy.deepcopy(_BLANK)
    out.update(s or {})
    return out


def _save(s):
    config.ensure_home()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def _roll_day(s):
    """New trading day: PnL resets, and an automatic daily-loss halt lifts.
    A manual /halt is deliberate and survives the rollover."""
    today = _today()
    if s.get("day") != today:
        s["day"] = today
        s["day_pnl"] = 0.0
        s["day_start_equity"] = 0.0
        if s.get("halt_source") == "daily_loss":
            s["halted"] = False
            s["halt_reason"] = ""
            s["halt_source"] = ""
    return s


def state():
    """Current guard state, day-rolled and persisted."""
    with _lock:
        s = _roll_day(_load())
        _save(s)
        return s


class GuardViolation(Exception):
    pass


# ---------------- liquidation geometry (pure) ----------------

# WEEX does not publish a per-tier maintenance-margin rate on the public
# endpoints, so we use a conservative flat figure. Being conservative here means
# ESTIMATING LIQUIDATION AS CLOSER than it really is, which can only make the
# stop-behind-liquidation check fire earlier — the safe direction to be wrong in.
MAINTENANCE_MARGIN_RATE = 0.005      # 0.5%
LIQ_SAFETY_MARGIN = 0.80             # a stop must sit inside 80% of the liq distance


def est_liquidation_price(entry, leverage, side="LONG",
                          mmr=MAINTENANCE_MARGIN_RATE):
    """Rough isolated-margin liquidation price.

    For a long at L times leverage the position is wiped out roughly 1/L below
    entry, minus the maintenance margin the venue keeps back. This is an
    ESTIMATE: the venue's real figure depends on tier, funding and fees. It is
    used only to catch the catastrophic case — a 'stop' that sits at or beyond
    the point where the position no longer exists.
    """
    entry, leverage = float(entry), float(leverage)
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    frac = 1.0 / leverage - mmr
    frac = max(frac, 0.0005)          # never claim liquidation is 0% away
    return entry * (1 - frac) if side == "LONG" else entry * (1 + frac)


def stop_is_behind_liquidation(entry, stop, leverage, side="LONG",
                               safety=LIQ_SAFETY_MARGIN, mmr=MAINTENANCE_MARGIN_RATE):
    """True when the stop is at/beyond estimated liquidation (or too near it).

    A stop past liquidation is not a stop: the position is gone before the order
    can ever trigger, and the 'risk' the operator thinks they sized is fiction.
    Returns (is_bad, detail_dict).
    """
    liq = est_liquidation_price(entry, leverage, side, mmr)
    entry = float(entry)
    stop = float(stop)
    if side == "LONG":
        stop_dist = entry - stop
        liq_dist = entry - liq
    else:
        stop_dist = stop - entry
        liq_dist = liq - entry
    usable = liq_dist * safety
    bad = stop_dist >= usable
    return bad, {
        "est_liq_price": liq,
        "stop_distance": stop_dist,
        "liq_distance": liq_dist,
        "usable_distance": usable,
        "stop_pct": (stop_dist / entry * 100.0) if entry else 0.0,
        "liq_pct": (liq_dist / entry * 100.0) if entry else 0.0,
        "safety": safety,
    }


# ---------------- pure predicates (unit-testable, no I/O) ----------------

def daily_loss_breached(day_pnl, day_start_equity, limit_pct=DAILY_LOSS_HALT_PCT):
    """True once the day's realized PnL is a >limit_pct% drawdown on opening equity."""
    if not day_start_equity or day_start_equity <= 0:
        return False
    return (float(day_pnl) / float(day_start_equity)) * 100.0 <= -abs(limit_pct)


def entry_violations(symbol, notional, leverage, stop_price, entry_price, side,
                     reference_price=None, open_position_count=0,
                     last_close_ts=None, now=None, halted=False, halt_reason="",
                     day_pnl=0.0, day_start_equity=0.0):
    """Return the list of rule breaches. Pure — the caller supplies the state."""
    now = time.time() if now is None else now
    errs = []
    if halted:
        errs.append(f"TRADING HALTED: {halt_reason or 'kill switch engaged'} — /resume to clear")
    if leverage > MAX_LEVERAGE:
        errs.append(f"leverage {leverage}x > cap {MAX_LEVERAGE}x")
    if notional > MAX_NOTIONAL_USD:
        errs.append(f"notional ${notional:.0f} > cap ${MAX_NOTIONAL_USD}")
    if open_position_count >= MAX_CONCURRENT:
        errs.append(f"already {open_position_count} positions (max {MAX_CONCURRENT})")
    if STOP_REQUIRED and not stop_price:
        errs.append("no stop price — stops are mandatory, placed atomically with entry")
    if stop_price and entry_price:
        if side == "LONG" and float(stop_price) >= float(entry_price):
            errs.append("LONG stop must be below entry")
        if side == "SHORT" and float(stop_price) <= float(entry_price):
            errs.append("SHORT stop must be above entry")
    if reference_price:
        drift = (float(entry_price) - float(reference_price)) / float(reference_price) * 100
        if side == "LONG" and drift > CHASE_MAX_PCT:
            errs.append(f"chase guard: entry {drift:.1f}% above reference (max {CHASE_MAX_PCT}%)")
        if side == "SHORT" and -drift > CHASE_MAX_PCT:
            errs.append(f"chase guard: entry {-drift:.1f}% below reference (max {CHASE_MAX_PCT}%)")
    if last_close_ts and (now - last_close_ts) < COOLDOWN_MIN * 60:
        mins = int((COOLDOWN_MIN * 60 - (now - last_close_ts)) / 60) + 1
        errs.append(f"cooldown: {symbol} locked for {mins} more min (anti-revenge rule)")
    if daily_loss_breached(day_pnl, day_start_equity):
        pct = (float(day_pnl) / float(day_start_equity)) * 100.0
        errs.append(f"daily loss {pct:.1f}% breaches the {DAILY_LOSS_HALT_PCT}% halt")
    return errs


# ---------------- the gate ----------------

def check_entry(symbol, notional, leverage, stop_price, entry_price, side,
                reference_price=None, open_position_count=0, taker_override=False):
    """Raise GuardViolation on any breach. Returns True if clean."""
    s = state()
    errs = entry_violations(
        symbol, notional, leverage, stop_price, entry_price, side,
        reference_price=reference_price, open_position_count=open_position_count,
        last_close_ts=s["last_close"].get(symbol),
        halted=bool(s.get("halted")), halt_reason=s.get("halt_reason", ""),
        day_pnl=s.get("day_pnl", 0.0), day_start_equity=s.get("day_start_equity", 0.0))
    if errs:
        raise GuardViolation(" | ".join(errs))
    return True


# ---------------- state mutations ----------------

def record_close(symbol):
    """Start the per-symbol cooldown clock."""
    with _lock:
        s = _roll_day(_load())
        s["last_close"][symbol] = time.time()
        _save(s)


def set_day_start_equity(equity):
    """Anchor the daily-loss percentage. First call of the day wins."""
    with _lock:
        s = _roll_day(_load())
        if not s.get("day_start_equity"):
            s["day_start_equity"] = float(equity)
            _save(s)
        return s["day_start_equity"]


def record_pnl(delta):
    """Add realized PnL for the day; auto-halt once the daily loss limit trips.
    Returns (day_pnl, halted_now)."""
    with _lock:
        s = _roll_day(_load())
        s["day_pnl"] = float(s.get("day_pnl", 0.0)) + float(delta)
        tripped = False
        if not s.get("halted") and daily_loss_breached(s["day_pnl"], s.get("day_start_equity")):
            s["halted"] = True
            s["halt_source"] = "daily_loss"
            s["halt_reason"] = (f"daily loss {s['day_pnl']:.2f} on opening equity "
                                f"{s.get('day_start_equity'):.2f} "
                                f"(limit {DAILY_LOSS_HALT_PCT}%)")
            tripped = True
        _save(s)
        return s["day_pnl"], tripped


def halt(reason="manual kill switch", source="manual"):
    with _lock:
        s = _roll_day(_load())
        s["halted"] = True
        s["halt_reason"] = reason
        s["halt_source"] = source
        _save(s)
        return s


def resume():
    with _lock:
        s = _roll_day(_load())
        s["halted"] = False
        s["halt_reason"] = ""
        s["halt_source"] = ""
        _save(s)
        return s


def reset_state(path=None):
    """Wipe guard state. For tests ONLY — a suite that leaves a cooldown behind
    makes the next suite's entries mysteriously refuse."""
    global STATE_PATH
    if path:
        STATE_PATH = path
    with _lock:
        s = copy.deepcopy(_BLANK)
        s["day"] = _today()
        _save(s)
        return s


def is_halted():
    s = state()
    return bool(s.get("halted")), s.get("halt_reason", "")


def summary():
    """One-line human summary for /status."""
    s = state()
    bits = [f"leverage cap {MAX_LEVERAGE}x", f"notional cap ${MAX_NOTIONAL_USD}",
            f"max {MAX_CONCURRENT} positions", f"cooldown {COOLDOWN_MIN}m"]
    if s.get("halted"):
        bits.insert(0, f"HALTED ({s.get('halt_reason')})")
    return " | ".join(bits)
