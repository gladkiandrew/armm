#!/usr/bin/env python3
"""ARMM candles — kline fetching, normalization, pagination and CSV cache.

This is the ONLY place that knows how WEEX serves klines, which is a minefield
(all verified empirically 2026-09-01):

  GET /capi/v3/market/klines
      up to 1000 bars, NO time range. Rows come back DESCENDING by open time,
      EXCEPT the current in-progress bar, which is appended LAST. Feeding that
      array to an indicator unsorted produces silent garbage.

  GET /capi/v3/market/historyKlines
      supports startTime/endTime but caps at limit=100 (limit>100 is rejected
      with -1142). startTime WITHOUT endTime returns []. endTime alone is
      ignored. Rows are DESCENDING. Deep history therefore pages BACKWARD.

normalize() sorts ascending and de-duplicates, and every fetch here drops the
in-progress bar by default: an indicator computed on a half-formed candle
changes under your feet and would flip a signal mid-hour.
"""
import csv, os, time
from collections import namedtuple

import config
import logsetup
from specs import market_symbol
from weex_client import WeexError

log = logsetup.get("candles")

CACHE_DIR = os.path.join(config.ARMM_HOME, "klines")
KLINES_MAX = 1000          # /market/klines hard cap
HISTORY_MAX = 100          # /market/historyKlines hard cap (limit>100 -> -1142)

Candle = namedtuple("Candle", "open_time open high low close volume close_time")

INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000,
    "1d": 86_400_000, "1w": 604_800_000,
}


class CandleError(Exception):
    pass


def interval_ms(interval):
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise CandleError(f"unsupported interval {interval!r}; "
                          f"allowed: {', '.join(INTERVAL_MS)}")


def to_candle(row):
    return Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                  float(row[4]), float(row[5]), int(row[6]))


def normalize(rows, interval=None, drop_incomplete=True, now_ms=None):
    """Rows (raw arrays or Candles) -> ascending, de-duplicated Candle list.

    Both WEEX kline endpoints return DESCENDING order, and /market/klines
    appends the live bar last. Sorting by open_time is what makes either safe.
    """
    out = {}
    for r in rows or []:
        c = r if isinstance(r, Candle) else to_candle(r)
        out[c.open_time] = c
    candles = [out[k] for k in sorted(out)]
    if drop_incomplete and candles:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        # close_time is the bar's exclusive end; a bar is finished once now
        # has passed it. Fall back to the interval when close_time is absent.
        step = interval_ms(interval) if interval else None
        while candles:
            last = candles[-1]
            end = last.close_time or ((last.open_time + step) if step else None)
            if end is not None and now < end:
                candles.pop()
            else:
                break
    return candles


# ---------------- fetching ----------------

def recent(client, symbol, interval="1h", limit=300, drop_incomplete=True):
    """Most recent `limit` finished candles via /market/klines (max 1000)."""
    if limit > KLINES_MAX:
        raise CandleError(f"/market/klines serves at most {KLINES_MAX} bars; "
                          f"use history() for more")
    # Ask for one extra so dropping the in-progress bar still leaves `limit`.
    raw = client.klines(symbol, interval=interval,
                        limit=min(KLINES_MAX, limit + 1))
    candles = normalize(raw, interval=interval, drop_incomplete=drop_incomplete)
    return candles[-limit:]


def history(client, symbol, interval="1h", start_ms=None, end_ms=None,
            max_bars=5000, drop_incomplete=True):
    """Deep history via backward pagination of /market/historyKlines.

    Pages from end_ms backwards in 100-bar requests until start_ms is reached,
    the venue runs out of data, or max_bars is hit. Every request goes through
    the client's 10 req/s pacer.
    """
    step = interval_ms(interval)
    now = int(time.time() * 1000)
    end = int(end_ms or now)
    start = int(start_ms if start_ms is not None else end - max_bars * step)
    if start >= end:
        raise CandleError("start_ms must be before end_ms")

    collected, cursor, stalls = {}, end, 0
    while cursor > start and len(collected) < max_bars:
        raw = client.history_klines(symbol, interval=interval, start_time=start,
                                    end_time=cursor, limit=HISTORY_MAX)
        rows = [to_candle(r) for r in (raw or [])]
        fresh = [c for c in rows if c.open_time not in collected]
        if not fresh:
            stalls += 1
            if stalls >= 2:
                log.info("history for %s %s exhausted at %s", symbol, interval, cursor)
                break
            cursor -= HISTORY_MAX * step        # nudge past a gap in the data
            continue
        stalls = 0
        for c in fresh:
            collected[c.open_time] = c
        oldest = min(c.open_time for c in fresh)
        if oldest <= start:
            break
        cursor = oldest - 1                      # strictly older next page
        log.debug("paged %s %s back to %s (%d bars)", symbol, interval,
                  oldest, len(collected))

    candles = normalize(list(collected.values()), interval=interval,
                        drop_incomplete=drop_incomplete)
    return [c for c in candles if c.open_time >= start]


def load(client, symbol, interval="1h", days=60, use_cache=True, refresh=False):
    """Candles for the last `days`, CSV-cached under ~/armm/klines/.

    The cache is append-only and keyed by (symbol, interval); a second run over
    the same window costs zero requests.
    """
    sym = market_symbol(symbol)
    step = interval_ms(interval)
    now = int(time.time() * 1000)
    start = now - int(days * 86_400_000)

    cached = [] if refresh else (read_cache(sym, interval) if use_cache else [])
    have_from = cached[0].open_time if cached else None
    have_to = cached[-1].open_time if cached else None

    fetched = []
    if not cached or have_from > start + step:
        fetched += history(client, sym, interval, start_ms=start,
                           end_ms=(have_from - 1) if cached else None,
                           max_bars=int(days * 86_400_000 / step) + 10)
    if cached and have_to < now - 2 * step:
        fetched += history(client, sym, interval, start_ms=have_to,
                           end_ms=now, max_bars=HISTORY_MAX * 20)

    candles = normalize(cached + fetched, interval=interval)
    if use_cache and fetched:
        write_cache(sym, interval, candles)
    out = [c for c in candles if c.open_time >= start]
    log.info("loaded %d %s %s candles (%d from cache, %d fetched)",
             len(out), sym, interval, len(cached), len(fetched))
    return out


# ---------------- CSV cache ----------------

def cache_path(symbol, interval):
    return os.path.join(CACHE_DIR, f"{market_symbol(symbol)}-{interval}.csv")


def read_cache(symbol, interval):
    path = cache_path(symbol, interval)
    try:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)                   # header
            return normalize([to_candle(r) for r in reader if r],
                             interval=interval, drop_incomplete=False)
    except FileNotFoundError:
        return []
    except (ValueError, IndexError) as e:
        log.warning("kline cache %s is corrupt (%s) — ignoring it", path, e)
        return []


def write_cache(symbol, interval, candles):
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    path = cache_path(symbol, interval)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time"])
        for c in candles:
            w.writerow([c.open_time, c.open, c.high, c.low, c.close, c.volume, c.close_time])
    os.replace(tmp, path)
    return path


# ---------------- column views ----------------

def columns(candles):
    """(highs, lows, closes, volumes) — the shape indicators.py wants."""
    return ([c.high for c in candles], [c.low for c in candles],
            [c.close for c in candles], [c.volume for c in candles])


def index_at_or_after(candles, ts_ms):
    """Index of the first candle at/after ts_ms — the aVWAP anchor index."""
    for i, c in enumerate(candles):
        if c.open_time >= ts_ms:
            return i
    return None
