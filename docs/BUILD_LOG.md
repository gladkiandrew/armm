# ARMM build log

What broke, why, and the fix — one section per release, kept verbatim from the
build. Newest last. Back to the [README](../README.md).

## Lessons from first-order day (2026-08-31)

- Demo symbols are SUSDT-quoted: BTCUSDT → BTCSUSDT (client auto-maps)
- Trade permission must be enabled on the key (read-only is WEEX default)
- BTC stepSize 0.0001 — now fetched from `exchangeInfo`, not hardcoded
- Demo rate limit: 1 order / 10s; demo has no cancel endpoint

## Lessons from v0.2 (2026-09-01)

- **`exchangeInfo` gives precisions, not step/tick strings.** `stepSize =
  10^-quantityPrecision`, `tickSize = 10^-pricePrecision`. BTC: 0.0001 / 0.1.
  The `symbol=` filter works; most of the 79KB payload is the `assets` list.
- **Sim `order/history` lists only TERMINAL orders.** A resting order is simply
  absent from it. "Not found" therefore means "still working", not "lost" — we
  hold the ack with its orderId either way. This is why `/status` reports ARMM's
  own tracked pending entries in demo instead of a false "0 open orders".
- **`time == updateTime` does NOT identify a POST_ONLY veto** — filled orders
  show it too. The reliable signal is `status == CANCELED and executedQty == 0`.
- **Sim has no leverage endpoint.** The demo account was running ~125–141x while
  the bot printed "at 5x": that number drives sizing and the guard cap, but the
  *account's* leverage governs real margin and liquidation. Live mode now calls
  `POST /capi/v3/account/leverage` before the entry so the cap is enforced on the
  venue; demo says so out loud instead of implying an enforcement it lacks.
- **Float arithmetic silently costs a step.** `0.1234 - 0.0814` is
  `0.041999999999999996`, which floors a whole step down. `floor_to_step` now
  settles the ratio to 9dp first, and tick shifts go through `Decimal`.
- **A veto retry must not chase.** Re-deriving the entry to the touch turns
  "buy 40 below" into "buy at the bid" — the exact behaviour the chase guard
  exists to stop. The retry re-runs the operator's original entry spec against
  fresh data (and re-runs the guards).
- **`TELEGRAM_CHAT_ID` was set to the bot's own user id**, so every push would
  have 403'd silently. Both the cockpit and the notifier now detect that and
  fall back to the `/start` binding flow.

## Lessons from v0.3 (2026-09-01)

- **`/market/klines` returns DESCENDING order with the in-progress bar appended
  LAST.** Feeding that array straight to an indicator produces silent garbage.
  `candles.normalize()` sorts ascending, de-duplicates, and drops the unfinished
  bar; nothing should read klines without it.
- **The two kline endpoints have opposite constraints.** `/market/klines` serves
  up to 1000 bars but has no time range (the brief's "300/request" is wrong for
  it). `/market/historyKlines` takes startTime/endTime but caps at 100 — and
  `startTime` *without* `endTime` returns `[]`, while `endTime` alone is ignored.
  Deep history therefore pages BACKWARD. ~400 days of 1H is available.
- **Backtest anchoring was the difference between meaning and noise.** Anchoring
  aVWAP to bar 0 of a 365-day series turns "price above aVWAP" into "above the
  yearly VWAP" — a different rule. Rolling the anchor to each newly *confirmed*
  swing low (confirmed 5 bars late, so no look-ahead) took Setup B from 9 trades
  a year to 59, and the sweep from 27 minutes to 24 seconds.
- **The sweep found nothing.** 160 rankable combos, and the gap between #1 and
  #20 is inside two standard errors of #1's own mean. No dimension's marginal
  spread clears the noise band either. The honest read is that this window
  cannot distinguish these parameters — keep the defaults. The report says so in
  those words rather than presenting a "best" set.
- **A `.PHONY` target with no rule is silently "already made".** A botched edit
  deleted `compile`/`test`/`secrets` and `make check` still printed GREEN. The
  `self-check` target now uses `make -q` to assert every prerequisite has a real
  recipe, and that guard is itself verified by removing a target.
- **The first setup alert was being swallowed.** `last_alert_ts` of 0 means
  "never alerted", but the cooldown compared `now - 0 < 3600` and stayed quiet.
  Invisible in production (epoch clocks are huge) and obvious under test clocks.

## Lessons from v0.4 (2026-09-01)

- **A `.PHONY` target with no rule passes vacuously** (found in v0.3, bitten
  again here): the `self-check` target now uses `make -q` to assert every
  prerequisite of `check` has a real recipe, and the guard is itself verified by
  deleting a target and confirming the failure.
- **`dict(_BLANK)` is a shallow copy.** `guards._load()` returned a state sharing
  the nested `last_close` dict with the module-level template, so the first
  `record_close()` in a process mutated the template permanently — every later
  "fresh" state arrived pre-populated with a cooldown that was never persisted.
  Invisible in production (the file usually exists), fatal across test suites.
- **Client ids collided within a millisecond.** `armm-auto-e-<ms>` is not unique
  under two entries in the same millisecond, and that id is the idempotency key.
  A per-millisecond sequence number now disambiguates.
- **The v0.4 vetoes silently zeroed the backtest.** With no 30m series the
  justification frame returns *unknown*, which correctly blocks a live entry —
  and incorrectly blocked every backtested signal, returning "0 trades" across
  the whole grid. research.py now measures the CORE rule and the report says the
  live signal count will be lower.
- **Research reports overwrote each other.** The filename carried only the date,
  so the HYPE sweep would have replaced the BTC one. It now includes the symbol.
- **The advisor short-circuited on a confirmed setup**, skipping the regime and
  30m checks. Those now run unconditionally; only the discretionary
  "no thesis here" heuristics are skipped when a setup already matches.
- **Six build orders were sitting in the track record.** Every pre-v0.4 position
  was an acceptance-test artifact but counted as MANUAL. a one-shot
  backfill relabelled them with a per-row justification, so the comparison starts clean.

## Lessons from v0.5 (2026-10-04)

- **A correct guard and a working system are different claims.** Every one of
  the 335 refusals was the right call; the bot still produced nothing for a
  month. "No trades" is not a status — it is the absence of one, and it needs a
  reason attached before it means anything.
- **`last_alert_ts == 0` bit for the third time.** v0.3 shipped it in the setup
  alert, v0.4's notes called it out, and the first draft of this module did it
  again: `now - 0 >= REALERT_SECONDS` is false under a test clock and true
  forever in production, so the alarm passes its own tests and never fires for a
  human, or vice versa. It is now a named function, `_cooldown_elapsed`, that
  treats falsy as NEVER rather than as a timestamp.
- **The alarm had to be wired at a wrapper, not at nine return statements.**
  `run_symbol` has nine exit paths; the thing being detected is a failure nobody
  noticed for a month. Recording health at each `return` would have been nine
  chances to forget, so `run_symbol` is now a wrapper around
  `_evaluate_symbol` and every path is counted by construction.
- **Telemetry must not be able to stop trading.** The tracker is wrapped in a
  bare `except` that logs and returns the result: a bug in the health monitor
  must never take down the thing it is monitoring.
- **The test that mattered most was the false-positive one.** `no_setup` must
  never accumulate. An alarm that fires in every quiet week is strictly worse
  than no alarm, because it teaches the operator to skim past the one that counts.
