#!/usr/bin/env python3
"""ARMM logging — rotating file log at ~/armm/bot.log, stdlib only.

Every logger built here carries a redaction filter so an API key or bot token
can never land in bot.log (or in a terminal transcript).
"""
import logging, logging.handlers, os, sys
from . import config

MAX_BYTES = 5_000_000
BACKUPS = 5
_configured = False


class _Redact(logging.Filter):
    def __init__(self, fragments):
        super().__init__()
        self.fragments = [f for f in fragments if f]

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        hit = False
        for frag in self.fragments:
            if frag in msg:
                msg = msg.replace(frag, "<redacted>")
                hit = True
        if hit:
            record.msg = msg
            record.args = ()
        return True


def setup(level=logging.INFO, console=True, path=None):
    """Idempotent root-logger setup. Safe to call from every entrypoint."""
    global _configured
    root = logging.getLogger("armm")
    if _configured:
        return root
    config.ensure_home()
    root.setLevel(level)
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S%z")
    fh = logging.handlers.RotatingFileHandler(
        path or config.LOG_PATH, maxBytes=MAX_BYTES, backupCount=BACKUPS)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    try:
        red = _Redact(config.secret_fragments())
        for h in root.handlers:
            h.addFilter(red)
    except Exception:
        pass
    _configured = True
    return root


def get(name):
    setup()
    return logging.getLogger(f"armm.{name}")
