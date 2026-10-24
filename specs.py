#!/usr/bin/env python3
"""ARMM contract specs — per-symbol trading rules from /capi/v3/market/exchangeInfo.

WEEX exposes precisions rather than step/tick strings, so we derive:
  stepSize = 10^-quantityPrecision   tickSize = 10^-pricePrecision
Cached to ~/armm/specs.json with a 24h TTL so a startup never depends on the
network being up, and so we stop hardcoding "BTC stepSize 0.0001" (v0.1 lesson).

Pure rounding helpers live here too — they are the unit-tested core of sizing.
"""
import json, os, time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP

import config
import logsetup

log = logsetup.get("specs")

TTL_SECONDS = 24 * 3600


class SpecError(Exception):
    pass


# ---------------- pure math (no network, no state) ----------------

def decimals_for(step):
    """Number of decimal places implied by a step/tick size. 0.0001 -> 4."""
    exp = Decimal(str(step)).normalize().as_tuple().exponent
    return max(0, -int(exp))


def floor_to_step(value, step):
    """Round a quantity DOWN to a whole multiple of step. Never over-sizes.

    The ratio is settled to 9dp before flooring: 0.1234 - 0.0814 evaluates to
    0.041999999999999996 in binary float, and flooring that raw would silently
    cost a whole step. 1e-13 of representation noise must not shrink an order.
    """
    v, s = Decimal(str(value)), Decimal(str(step))
    if s <= 0:
        raise SpecError(f"bad step size {step}")
    ratio = (v / s).quantize(Decimal("1E-9"), rounding=ROUND_HALF_UP)
    return float(ratio.to_integral_value(rounding=ROUND_DOWN) * s)


def round_to_tick(value, tick, mode="nearest"):
    """Snap a price to the tick grid. mode: nearest | down | up."""
    v, t = Decimal(str(value)), Decimal(str(tick))
    if t <= 0:
        raise SpecError(f"bad tick size {tick}")
    rounding = {"nearest": ROUND_HALF_UP, "down": ROUND_DOWN, "up": ROUND_UP}[mode]
    return float((v / t).to_integral_value(rounding=rounding) * t)


def shift_ticks(price, tick, n):
    """Move `price` by n ticks in exact decimal arithmetic. 78346.4 - 1 tick is
    78346.3, not 78346.29999999999 (which floors to the wrong grid point)."""
    return float(Decimal(str(price)) + Decimal(n) * Decimal(str(tick)))


def fmt(value, step):
    """Format at exactly the venue's precision — no 0.0062999999 in a payload."""
    d = decimals_for(step)
    return f"{Decimal(str(value)).quantize(Decimal(1).scaleb(-d)):f}"


def market_symbol(symbol):
    """Demo symbols are SUSDT-quoted; market data always uses the real symbol."""
    s = symbol.upper()
    if s.endswith("SUSDT"):
        return s[:-5] + "USDT"
    return s


# ---------------- spec objects ----------------

FALLBACK = {
    # Last-resort only, and only for the symbol v0.1 verified by hand.
    "BTCUSDT": {"step_size": 0.0001, "tick_size": 0.1, "min_qty": 0.0001,
                "max_qty": 1200.0, "min_notional": None, "price_precision": 1,
                "qty_precision": 4, "max_leverage": 400, "maker_fee": 0.0002,
                "taker_fee": 0.0008, "buy_limit_ratio": 0.01, "sell_limit_ratio": 0.01,
                "source": "fallback"},
}


def _parse(sym):
    qp = int(sym.get("quantityPrecision", 4))
    pp = int(sym.get("pricePrecision", 1))
    return {
        "step_size": float(Decimal(1).scaleb(-qp)),
        "tick_size": float(Decimal(1).scaleb(-pp)),
        "qty_precision": qp,
        "price_precision": pp,
        "min_qty": float(sym.get("minOrderSize") or Decimal(1).scaleb(-qp)),
        "max_qty": float(sym.get("maxOrderSize") or 0) or None,
        "min_notional": float(sym["minNotional"]) if sym.get("minNotional") else None,
        "max_leverage": int(sym.get("maxLeverage") or 0) or None,
        "maker_fee": float(sym.get("makerFeeRate") or 0),
        "taker_fee": float(sym.get("takerFeeRate") or 0),
        "buy_limit_ratio": float(sym.get("buyLimitPriceRatio") or 0) or None,
        "sell_limit_ratio": float(sym.get("sellLimitPriceRatio") or 0) or None,
        "contract_val": float(sym.get("contractVal") or 0) or None,
        "source": "exchangeInfo",
    }


def _load_cache():
    try:
        with open(config.SPECS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    config.ensure_home()
    tmp = config.SPECS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    os.replace(tmp, config.SPECS_PATH)


def get(symbol, client=None, force=False, ttl=TTL_SECONDS, allow_fallback=True):
    """Return the spec dict for `symbol`. Cache-first, 24h TTL, network on miss."""
    sym = market_symbol(symbol)
    cache = _load_cache()
    hit = cache.get(sym)
    if hit and not force and (time.time() - hit.get("fetched_at", 0)) < ttl:
        return hit

    if client is not None:
        try:
            info = client.exchange_info(sym)
            symbols = info.get("symbols") if isinstance(info, dict) else None
            row = None
            for s in (symbols or []):
                if str(s.get("symbol", "")).upper() == sym:
                    row = s
                    break
            if row:
                spec = _parse(row)
                spec["symbol"] = sym
                spec["fetched_at"] = time.time()
                cache[sym] = spec
                _save_cache(cache)
                log.info("specs %s refreshed: step=%s tick=%s minQty=%s maxLev=%s",
                         sym, spec["step_size"], spec["tick_size"],
                         spec["min_qty"], spec["max_leverage"])
                return spec
            log.warning("exchangeInfo returned no row for %s", sym)
        except Exception as e:
            log.warning("specs fetch failed for %s: %s", sym, e)

    if hit:
        log.warning("using STALE cached specs for %s (age %.0fh)",
                    sym, (time.time() - hit.get("fetched_at", 0)) / 3600)
        return hit
    if allow_fallback and sym in FALLBACK:
        spec = dict(FALLBACK[sym])
        spec["symbol"] = sym
        spec["fetched_at"] = 0
        log.warning("using FALLBACK specs for %s — verify before sizing up", sym)
        return spec
    raise SpecError(f"no contract specs available for {sym} (network down and no cache)")


def refresh_all(client, symbols):
    out = {}
    for s in symbols:
        try:
            out[market_symbol(s)] = get(s, client=client, force=True)
        except SpecError as e:
            log.error("%s", e)
    return out


if __name__ == "__main__":
    import sys
    from weex_client import WeexClient
    syms = sys.argv[1:] or ["BTCUSDT"]
    c = WeexClient(demo=True)
    for s in syms:
        print(json.dumps(get(s, client=c, force=True), indent=1))
