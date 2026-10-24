#!/usr/bin/env python3
"""ARMM order attribution — every order says where it came from.

Motivated by the AT5CHAT incident (2026-09-01): an acceptance-test order landed
in the live demo database indistinguishable from a real Telegram entry, so the
track record could not be trusted. From v0.4 the ORIGIN is encoded in the client
id itself, which means the exchange's own record carries it too — not just our
database, which is the thing an audit would doubt.

    armm-cli-e-1788287697607     manual CLI ladder
    armm-tg-e-1788287697607      manual Telegram ladder
    armm-auto-e-1788287697607    autopilot (demo-only by construction)
    armm-test-e-1788287697607    acceptance test / build verification

WEEX allows 1-36 chars matching ^[\\.A-Z\\:/a-z0-9_-]{1,36}$; the longest form
here is 27, leaving room for the tranche suffixes.
"""
import re
import threading
import time

CLI = "cli"
TELEGRAM = "tg"
AUTO = "auto"
TEST = "test"
ORIGINS = (CLI, TELEGRAM, AUTO, TEST)

MANUAL_ORIGINS = (CLI, TELEGRAM)     # a human typed it
LEGACY = "legacy"                    # pre-v0.4 ids, origin not recorded
UNKNOWN = "unknown"

_PREFIX = "armm"
_VALID_ID = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")

LABELS = {
    CLI: "CLI",
    TELEGRAM: "TELEGRAM",
    AUTO: "AUTOPILOT",
    TEST: "🧪 TEST ORDER",
    LEGACY: "legacy (pre-v0.4)",
    UNKNOWN: "unknown origin",
}

# Aliases so v0.2/v0.3 call sites that said source="telegram" keep working.
_ALIASES = {"telegram": TELEGRAM, "cockpit": TELEGRAM, "autopilot": AUTO,
            "command-line": CLI, "manual": CLI}


class OriginError(ValueError):
    pass


def normalize(origin):
    """Map a caller's word onto a canonical origin, or raise.

    Deliberately strict: an unrecognised origin is a bug in the caller, and
    silently defaulting it to 'cli' is exactly how a test order gets recorded
    as a real one.
    """
    if origin is None:
        raise OriginError("origin is required — one of " + ", ".join(ORIGINS))
    o = str(origin).strip().lower()
    o = _ALIASES.get(o, o)
    if o not in ORIGINS:
        raise OriginError(f"unknown origin {origin!r}; expected one of "
                          + ", ".join(ORIGINS))
    return o


def is_manual(origin):
    return normalize(origin) in MANUAL_ORIGINS


def label(origin):
    try:
        return LABELS[normalize(origin)]
    except OriginError:
        return LABELS.get(origin, LABELS[UNKNOWN])


_seq_lock = threading.Lock()
_last_ms = 0
_seq = 0


def entry_id(origin, now_ms=None):
    """Client id for an entry order.

    The millisecond alone is NOT enough: two entries raised in the same
    millisecond would produce the same id, and since the id is the idempotency
    key that means a UNIQUE violation at best and a silently-merged pair of
    orders at worst. A per-millisecond sequence number disambiguates.
    """
    global _last_ms, _seq
    o = normalize(origin)
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    with _seq_lock:
        if ts == _last_ms:
            _seq += 1
            suffix = f"-{_seq}"
        else:
            _last_ms, _seq = ts, 0
            suffix = ""
    return _check(f"{_PREFIX}-{o}-e-{ts}{suffix}")


def tranche_id(origin, position_id, index):
    """Client id for a take-profit tranche.

    DETERMINISTIC: the same position and index always produce the same id, which
    is what makes a mid-placement crash safe to resume (see manager.place_tranches).
    """
    o = normalize(origin)
    return _check(f"{_PREFIX}-{o}-tp{int(index)}-p{int(position_id)}")


def legacy_tranche_id(position_id, index):
    """The pre-v0.4 tranche id, still honoured so positions opened before this
    change are not re-placed under a new id after an upgrade."""
    return f"{_PREFIX}-tp{int(index)}-p{int(position_id)}"


def adopt_id(origin, symbol, side, now_ms=None):
    o = normalize(origin)
    ts = int(now_ms if now_ms is not None else time.time())
    return _check(f"{_PREFIX}-{o}-adopt-{symbol}-{side}-{ts}"[:36])


def parse(client_id):
    """Recover the origin from a client id. Never raises."""
    if not client_id:
        return UNKNOWN
    parts = str(client_id).split("-")
    if len(parts) < 2 or parts[0] != _PREFIX:
        return UNKNOWN
    if parts[1] in ORIGINS:
        return parts[1]
    return LEGACY          # armm-e-* / armm-tp1-p3 from v0.2/v0.3


def is_test(client_id):
    return parse(client_id) == TEST


def _check(cid):
    if not _VALID_ID.match(cid):
        raise OriginError(f"client id {cid!r} violates the venue's id pattern")
    return cid
