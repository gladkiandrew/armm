# ARMM — a risk-first execution engine for crypto futures

![tests](https://github.com/gladkiandrew/armm/actions/workflows/tests.yml/badge.svg)

ARMM is the execution assistant I built for my own crypto futures trading. I keep
the call; the bot places the orders, enforces my written risk rules, tells me
when my setups form, and flags when a trade contradicts them. A demo-locked
autopilot trades the same rules on simulated money so the coded strategy can be
measured against my discretionary record.

**No LLM is anywhere near the order path.** Signals, sizing, guards and execution
are deterministic Python. I build with AI coding agents (Claude Code); the
design, rules and every acceptance test are mine.

| | |
|---|---|
| Language | Python 3 (standard library only) |
| Venue | WEEX USDT-margined perpetuals (native REST client) |
| State | SQLite state machine |
| Interface | CLI + Telegram cockpit (chat-id lockdown) |
| Tests | 270+ offline unit tests, no network, no keys |
| Runs on | a self-managed Linux VPS under systemd |

## What's worth reading

- **`guards.py`**: the single risk gate. Max 5x leverage, max $2,500 notional, max 3 positions, mandatory atomic stop, cooldowns, chase guard, −6% daily auto-halt, kill switch. Every entry path calls it, and it runs again right before the wire call.
- **`autopilot.py`**: demo-only *by construction*, not by setting. It never imports the live order path, hard-codes `demo=True` at every call site, and re-checks routing before every order. `make demo-lock` proves all three.
- **`research.py`**: the parameter sweep, anchored to confirmed swing lows to avoid look-ahead. It found that the best parameter sets were statistically indistinguishable, so the report says "keep the defaults" instead of presenting a fake best set.
- **`starvation.py`**: tells "the market offered nothing" apart from "a setup fired and something blocked it." Built after autopilot sat for a month refusing 335 valid signals with no alarm.
- **The lessons sections below**: what broke, why, and the fix. They are the real build log.

## Quickstart (offline, no keys)

```bash
git clone https://github.com/gladkiandrew/armm && cd armm
make compile test secrets no-new-order-paths   # offline checks, no keys needed
# `make check` adds the runtime demo-lock proof, which needs demo API credentials
python3 ladder.py BTCUSDT LONG --entry 78200 --stop 77400 --targets 78600,79000,79400 --notional 500 --dry-run
```

Credentials, if you ever run it against an exchange, live only in `~/armm/.env` (chmod 600). Nothing secret is in this repo.

## Run

```bash
# auto-priced entry: 40 points below the live last price, 1% stop, 3 targets
python3 ladder.py BTCUSDT LONG --offset 40 --stop-pct 1 --targets-pct 0.5,1,1.5 --notional 500

# absolute prices still work
python3 ladder.py BTCUSDT LONG --entry 78200 --stop 77400 --targets 78600,79000,79400 --notional 500

# see the plan and the routing line without placing anything
python3 ladder.py BTCUSDT LONG --offset 40 --stop-pct 1 --targets-pct 0.5,1,1.5 --notional 500 --dry-run

# position manager + Telegram cockpit (this is what systemd runs)
python3 manager.py --symbols BTCUSDT --telegram

# one-shot reconcile against the exchange
python3 manager.py --reconcile-only
```

Demo by default everywhere. `--live` requires typing CONFIRM; Telegram `live`
additionally requires the process to have been started with `--allow-live-orders`.
Credentials come from `~/armm/.env` (chmod 600, never in the repo).

## Files

| file | role |
|---|---|
| `config.py` | paths + the only reader/writer of `~/armm/.env` |
| `logsetup.py` | rotating `~/armm/bot.log` with secret redaction |
| `specs.py` | per-symbol contract specs (24h cache) + all rounding math |
| `weex_client.py` | native REST client (POST_ONLY, atomic SL/TP, sim routing, rate pacing) |
| `guards.py` | the single risk gate — every entry path calls `check_entry` |
| `ladder.py` | entry pricing, sizing, tranche math, placement + veto retry (CLI **and** library) |
| `store.py` | SQLite state machine at `~/armm/armm.db` |
| `notify.py` | Telegram push, queued at max 1 msg/sec |
| `manager.py` | poll → fill → tranche ladder → reconcile loop (systemd service) |
| `tg.py` | Telegram cockpit (long-poll, chat-id lockdown) |
| `indicators.py` | RSI/MACD/EMA/ATR/anchored-VWAP maths — pure, zero network |
| `candles.py` | kline fetch, normalization, backward pagination, CSV cache |
| `signals.py` | Setup A/B evaluation + the swing-low anchor heuristic |
| `advisor.py` | compares an intended ladder against the live snapshot |
| `research.py` | offline Setup B parameter sweep → a markdown report |
| `origin.py` | order attribution — every client id says where it came from |
| `autopilot.py` | the demo-locked strategy engine |
| `scoreboard.py` | AUTO vs MANUAL track record |
| `starvation.py` | tells "blocked" apart from "quiet" — the health alarm |
| `demo_lock_check.py` | runtime half of acceptance test 1 |
| `test_armm.py` + `test_signals.py` + `test_autopilot.py` + `test_starvation.py` | 276 offline unit tests |

## Telegram

```
/status   positions, open orders, balance, PnL today, guard state
/ladder BTCUSDT LONG offset=40 notional=500 stop=1% targets=0.5,1,1.5
/halt     kill switch — no new entries (existing stops stay)
/resume   clear the halt
/last     recent order history

/check SYMBOL     price, aVWAP ± band, RSI, MACD, 4H trend, setup verdicts
/anchor SYMBOL <YYYY-MM-DDTHH:MM | auto>   set the aVWAP anchor
/watch SYMBOL     alert once when a setup confirms   ·   /unwatch SYMBOL
/watching         list watched symbols and their anchors

/auto on SYMBOL   let the bot take its own setups — DEMO ACCOUNT ONLY
/auto off [SYM]   stop it (omit the symbol to stop everything)
/auto status      what autopilot is doing, and why it is not trading
/score            AUTO vs MANUAL track record
```

`/auto status` names any active block and its cause, so "ON" can never again
mean "ON and structurally unable to trade".

`stop=` and `targets=` are **percentages**; use `stopprice=` / `targetprices=` for
absolute values. The plan and its routing line are sent before anything is placed.

**First run:** if `TELEGRAM_CHAT_ID` is blank, send `/start` from your Telegram
account — that chat is written into `~/armm/.env` and every other chat id is
refused and logged from then on.

## Ops

```bash
make check                # py_compile + 61 unit tests + secret scan. Side-effect free.
make install-service      # prints the manual systemd install steps
tail -f ~/armm/bot.log    # rotating, 5MB x 5
```

`armm-bot.service` is a template, installed manually. It has no `--live` and no
`--allow-live-orders`: switching this bot to real money is a deliberate, reviewed
edit, not a flag someone can trip over.

## The setups (v0.3)

**Setup A — "Reload"** (longer-term long), all required:
1H RSI(14) < 40 · price below the lower aVWAP band (aVWAP − 1.0σ). MACD is
explicitly not applicable.

**Setup B — "Momentum"** (short-term leveraged long), all 3/3 plus a filter:
price above/reclaiming the aVWAP · 1H RSI rising, past 50, not above 70 ·
MACD histogram positive and expanding · 4H EMA20 > EMA50 and rising.

Setup B's thresholds are the brief's *initial* values and are marked for
optimisation — `research.py` sweeps them. `/watch` alerts once on the false→true
edge with a 1h re-alert cooldown, so a setup that stays true for six hours does
not produce six hours of messages.

### The advisor

Before any manual ladder, the bot compares the entry to the live snapshot. It
**never blocks** — Andrew keeps final authority — but a contradicted trade costs
an explicit extra confirmation tap and is written to the database
(`positions.advisor_overridden` + `advisor_notes`). The canonical case is a long
into RSI > 60 with the 4H trend down: no hard risk rule forbids it, but it
contradicts Andrew's own stated setups, so the bot says so first.

```bash
python3 research.py --symbol BTCUSDT --days 365   # → ~/armm/research/
python3 manager.py --check BTCUSDT                # one-shot snapshot, no orders
```

## Autopilot (v0.4)

Autopilot exists to answer one question: **how does the strategy-as-coded compare
to Andrew's own discretionary record?** It runs the identical `evaluate()` path
the cockpit uses, takes Setup A/B entries on the demo account, and `/score` puts
the two records side by side.

The demo lock is structural, and `make demo-lock` proves it three ways:
1. `autopilot.py` never names or imports the live order path.
2. Every `WeexClient` it constructs passes the **literal** `True` for `demo=`,
   written at the call site — not read from a flag, env var or command.
3. No function in the module accepts a `live`/`force`-style parameter.

Plus, at runtime, `assert_demo()` re-checks `demo`, `order_path` and `mode`
immediately before every order. If routing ever reads non-demo it refuses,
disables autopilot for every symbol, halts trading and says so loudly.

**Autopilot has no override authority.** A human may look at an advisor
contradiction and take the trade anyway; autopilot may not. Any objection at all
means no trade. It is also edge-triggered (one entry per activation of a setup),
rate-limited to one entry per symbol per 4h, obeys `/halt` instantly, and will
not resume without both `/resume` and a persisted `/auto on`.

### Order attribution

Every client id encodes its origin, so the *exchange's* record carries it too —
not just our database:

```
armm-cli-e-1788287697607     manual CLI ladder
armm-tg-e-1788287697607      manual Telegram ladder
armm-auto-e-1788287697607    autopilot (demo-only)
armm-test-e-1788287697607    acceptance test / build verification
```

`/last` shows the origin per order; test orders announce themselves as 🧪 TEST
ORDER; and `/score` excludes them from every statistic.

### Entry gates added in v0.4 (manual and auto alike)

- **Stop behind liquidation** — estimated from leverage. Uses the venue's own
  account leverage when we have observed it, because that is what actually
  governs liquidation; the requested figure is only what the guards capped.
- **Market regime** — from BTC's 4H EMA structure. Setup B is vetoed outright in
  a broken regime; Setup A additionally requires stabilisation.
- **30m justification frame** — Setup A needs the 30m oversold too (RSI < 40);
  Setup B needs the 30m not to contradict (RSI not extended, MACD histogram not
  negative). A missing 30m frame is *unknown*, and unknown is not permission.

## Guards (hard-coded, unbypassable)

Max 5x leverage · max $2,500 notional · max 3 concurrent positions · mandatory
atomic stop · 30-min per-symbol cooldown after a close · 2% chase guard vs the
anchor · −6% daily-loss auto-halt · manual kill switch. `guards.check_entry` is
called by the CLI, by Telegram, and again immediately before the wire call.

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

## Starvation detection (v0.5)

Autopilot ran from 2026-09-05 to 2026-10-03 taking **zero** trades. It was not
broken and the market was not quiet: 335 confirmed Setup B signals were each
refused by the stop-behind-liquidation check, because the demo account sat at
~141x where liquidation is 0.21% away and the strategy's stop is 1.00% away.
Every refusal was correct. The failure was that nothing said so — four weekly
digests reported "no trades", `/auto status` reported "ON", and a structurally
blocked autopilot was indistinguishable from a correctly idle one.

`starvation.py` makes the distinction, and the distinction is the whole design:

- **"The market is not offering a setup" is HEALTH.** `no_setup`,
  `rate_limited`, `already_acted`, `position_open`, `disabled`, `halted` — these
  accumulate nothing. If they counted, the alarm would fire constantly and be
  learned-past, which is worse than no alarm.
- **"A setup confirmed and I then refused it" is a BLOCK.** `guard_refused`,
  `advisor_refused`, `plan_failed`, `order_failed`, `unsafe_routing` — something
  upstream of the market stands between a live signal and an order, and that is
  the only thing a human can fix.

An unrecognised reason code counts as idle, deliberately: under-reporting a
novel block is recoverable, crying wolf is not.

Ten consecutive blocks (~an hour at the 5-minute poll) raises one Telegram
alarm, then goes quiet for 24h. It names the dominant cause only when a single
reason owns ≥80% of the streak — otherwise it says `mixed`, because ten blocks
for five reasons is not one config fault. The streak persists in `meta` across
restarts (`Restart=always` would otherwise reset it forever) and an entry clears
it. Any active block **leads** the weekly digest, above the statistics.

Replayed against the real September event log, it fires on **2026-09-05 13:50**,
45 minutes into the block, and 11 times across the month.

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

## v0.5 roadmap

Live-tiny validation of the real reduceOnly tranche ladder · cancel/replace
maker repricing · daily-loss halt wired to realized fills (`userTrades`) ·
out-of-sample validation of any parameter change · shorts. Auto-proposed and
auto-placed entries remain explicitly out of scope.
