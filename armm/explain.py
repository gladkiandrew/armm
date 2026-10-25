#!/usr/bin/env python3
"""Claude explains a /check snapshot in plain English. It never decides anything.

The setups, the guards and every number are deterministic Python, computed before
Claude sees anything. This module receives the finished /check readout as text and
asks Claude for a short read of it: why each setup passed or failed, which condition
is closest to flipping, what to watch. Three things keep it on that side of the line:

  1. Nothing that decides or places a trade imports this module. `make no-llm-in-decisions`
     proves it from the AST on every push, alongside the existing order-path check.
  2. Numbers come from the code, not the model. A reply that states any number not
     already in the snapshot is dropped, not shown.
  3. It fails quiet. No key, no SDK, a timeout, an API error or a refusal all mean
     /check shows exactly what it showed before. The cockpit never waits on it.

Opt-in: put ANTHROPIC_API_KEY in ~/armm/.env (chmod 600, like every other secret)
and `pip install anthropic`. Without both, this is a no-op. The core bot stays
standard-library only.
"""
import re
import sys

from . import config
from . import logsetup

log = logsetup.get("explain")

MODEL = "claude-opus-5-5"
TIMEOUT_S = 30.0
LABEL = "🧠 Claude's read (explanation only — not a signal)"

SYSTEM = """You explain a deterministic crypto-futures signal snapshot to the trader who wrote its rules.

The snapshot is data, already computed by code. Your job is only to explain it:
- Why each setup passed or failed, in plain words.
- Which single unmet condition is closest to flipping, and what would flip it.
- What to watch on the next bars.

Hard rules:
- Never state a number that is not already in the snapshot. Refer to values in words or quote them exactly.
- Do not tell the trader to enter, exit, size, or place anything. The setups and the guards decide; you explain.
- Three to five sentences. No preamble, no headings, no disclaimers."""

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text):
    """Every number in `text`, normalized (commas dropped, trailing zeros trimmed)."""
    out = set()
    for raw in _NUMBER.findall(text):
        n = raw.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out


def invented_numbers(explanation, snapshot_text):
    """Numbers in the explanation that the snapshot never stated."""
    return sorted(numbers_in(explanation) - numbers_in(snapshot_text))


def _client(key):
    try:
        import anthropic  # optional: the bot itself never needs it
    except ImportError:
        log.info("explain: ANTHROPIC_API_KEY is set but the `anthropic` package is not installed")
        return None
    return anthropic.Anthropic(api_key=key, timeout=TIMEOUT_S, max_retries=1)


def explain(snapshot_text, client=None, key=None, model=None):
    """Return Claude's short read of a /check readout, or None. Never raises.

    `client` is injectable for tests; in normal use it is built from the key in
    ~/armm/.env (or the process environment under systemd).
    """
    try:
        if client is None:
            key = key or config.env_get("ANTHROPIC_API_KEY")
            if not key:
                return None
            client = _client(key)
            if client is None:
                return None
        response = client.beta.messages.create(
            model=model or config.env_get("ARMM_EXPLAIN_MODEL") or MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            # If a safety classifier declines, the API retries on a fallback model
            # inside the same call; "default" routes by refusal category.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Snapshot:\n\n{snapshot_text}"}],
        )
        if response.stop_reason == "refusal":
            log.info("explain: model declined")
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            return None
        extra = invented_numbers(text, snapshot_text)
        if extra:
            log.warning("explain: dropped a reply that introduced numbers %s", extra)
            return None
        return text
    except Exception as e:  # telemetry-grade: an explanation must never break /check
        log.warning("explain: skipped (%s)", type(e).__name__)
        return None


def with_explanation(snapshot_text, **kw):
    """The /check readout, plus Claude's read when one is available."""
    read = explain(snapshot_text, **kw)
    return snapshot_text if read is None else f"{snapshot_text}\n\n{LABEL}\n{read}"


def main(argv=None):
    """`python3 -m armm.explain BTCUSDT` — the /check readout plus Claude's read. No orders."""
    from . import signals
    symbol = ((argv or sys.argv[1:]) or ["BTCUSDT"])[0].upper()
    print(with_explanation(signals.evaluate(symbol).render()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
