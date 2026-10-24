#!/usr/bin/env python3
"""ARMM Telegram cockpit — long-polling getUpdates (no webhook, no inbound port).

Security model:
  * TELEGRAM_CHAT_ID in ~/armm/.env is the ONLY chat this bot will ever obey.
  * If it is blank on first run, the first /start binds it and it is written
    back to ~/armm/.env. From then on every other chat id is refused and logged.
  * /ladder goes through ladder.build_plan(), which calls guards.check_entry().
    There is no Telegram-only order path.
  * LIVE placement additionally requires --allow-live-orders on the process AND
    an inline confirm tap. DEMO is the default everywhere.
"""
import argparse, json, os, sys, threading, time
import urllib.error

import datetime

from . import advisor as advisor_mod
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

log = logsetup.get("tg")

OFFSET_PATH = os.path.join(config.ARMM_HOME, "tg_offset")
DEMO_ROUTE = "/capi/v3/sim/order"
POLL_TIMEOUT = 25
CONFIRM_TTL = 120

HELP = """ARMM cockpit
/status — positions, open orders, balance, PnL today
/ladder SYMBOL LONG|SHORT offset=40 notional=500 stop=1% targets=0.5,1,1.5
    entry:   offset=<pts> | offsetpct=<pct> | entry=<price>
    stop:    stop=<pct> (a % of entry) | stopprice=<price>
    targets: targets=<pct,pct,..> | targetprices=<p,p,..>
    extras:  lev=5  splits=33,33,34  ref=<anchor price>  live
/halt — kill switch: no new entries
/resume — clear the halt
/last — recent order history

Signals (advisory only — v0.3 places no orders on its own):
/check SYMBOL — price, aVWAP ± band, RSI, MACD, 4H trend, setup verdicts
/anchor SYMBOL <YYYY-MM-DDTHH:MM | auto> — set the aVWAP anchor ('auto' finds
    the most recent significant swing low)
/watch SYMBOL — alert once when a setup confirms · /unwatch SYMBOL
/watching — list watched symbols and anchors

Autopilot (DEMO ONLY — structurally cannot reach the live endpoint):
/auto on SYMBOL — let the bot take its own setups on the demo account
/auto off [SYMBOL] — stop it (omit the symbol to stop everything)
/auto status — what autopilot is doing and why
/score — AUTO vs MANUAL track record
/help — this message
Demo by default. Every order prints its routing line first."""


def parse_ladder_args(tokens):
    """`/ladder BTCUSDT LONG offset=40 notional=500 stop=1% targets=0.5,1,1.5`

    Bare `stop=` / `targets=` numbers are PERCENTAGES (a trailing % is optional);
    absolute prices use stopprice= / targetprices=. Returns a kwargs dict for
    ladder.build_plan plus "live".
    """
    if len(tokens) < 2:
        raise ladder.LadderError("usage: /ladder SYMBOL LONG|SHORT offset=40 "
                                 "notional=500 stop=1% targets=0.5,1,1.5")
    symbol, side = tokens[0].upper(), tokens[1].upper()
    if side not in ("LONG", "SHORT"):
        raise ladder.LadderError(f"side must be LONG or SHORT, got {tokens[1]}")

    out = {"symbol": symbol, "side": side, "leverage": 5, "live": False}
    num = lambda v: float(str(v).strip().rstrip("%"))
    csv = lambda v: [x.strip().rstrip("%") for x in str(v).split(",") if x.strip()]

    for tok in tokens[2:]:
        if tok.lower() == "live":
            out["live"] = True
            continue
        if "=" not in tok:
            raise ladder.LadderError(f"unrecognised argument '{tok}' (expected key=value)")
        k, v = tok.split("=", 1)
        k = k.lower().replace("_", "").replace("-", "")
        if k == "offset":
            out["offset"] = num(v)
        elif k in ("offsetpct", "offsetpercent"):
            out["offset_pct"] = num(v)
        elif k == "entry":
            out["entry"] = num(v)
        elif k == "stop":
            out["stop_pct"] = num(v)
        elif k in ("stopprice", "stopabs"):
            out["stop"] = num(v)
        elif k == "targets":
            out["targets_pct"] = csv(v)
        elif k in ("targetprices", "targetsabs"):
            out["targets"] = csv(v)
        elif k == "splits":
            out["splits"] = csv(v)
        elif k == "notional":
            out["notional"] = num(v)
        elif k in ("lev", "leverage"):
            out["leverage"] = int(num(v))
        elif k in ("ref", "reference"):
            out["reference"] = num(v)
        else:
            raise ladder.LadderError(f"unknown option '{k}'")

    if "notional" not in out:
        raise ladder.LadderError("notional= is required")
    return out


def _venue_leverage(store, symbol):
    raw = store.get_meta(f"venue_leverage:{symbol.upper()}")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _fmt_ts(ms):
    return datetime.datetime.fromtimestamp(int(ms) / 1000,
                                           datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_iso_ms(text):
    """Parse an ISO-8601 datetime to epoch ms. A naive value is read as UTC."""
    s = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"could not parse {text!r} — use YYYY-MM-DDTHH:MM or 'auto'")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


class Cockpit:
    def __init__(self, demo=True, notifier=None, store=None, manager=None,
                 allow_live=False, token=None):
        self.token = token or config.env_get("TELEGRAM_BOT_TOKEN")
        if not self.token:
            raise SystemExit("TELEGRAM_BOT_TOKEN missing from ~/armm/.env")
        self.chat_id = (config.env_get("TELEGRAM_CHAT_ID") or "").strip()
        self.bot_id = None
        self._validate_chat_id()
        self.demo = demo
        self.allow_live = allow_live
        self.manager = manager
        self.store = store or store_mod.default()
        self.notify = notifier if notifier is not None else notify_mod.default()
        self.client = WeexClient(demo=demo)
        self._live_client = None
        self._pending = {}
        self._auto = None
        self._stop = threading.Event()
        self._offset = self._load_offset()
        self._refused = set()

    def _validate_chat_id(self):
        """A bot cannot message itself. TELEGRAM_CHAT_ID set to the bot's own id
        is a misconfiguration that would make every push silently 403, so treat
        it as unbound and fall back to the first-/start binding flow."""
        try:
            me = notify_mod.call(self.token, "getMe", timeout=15)
            self.bot_id = str((me.get("result") or {}).get("id") or "")
        except Exception as e:
            log.warning("getMe failed, cannot validate the chat id: %s", e)
            return
        if self.chat_id and self.bot_id and self.chat_id == self.bot_id:
            log.error("TELEGRAM_CHAT_ID (%s) is this bot's OWN id — a bot cannot send "
                      "to itself. Treating the cockpit as UNBOUND; send /start from "
                      "your Telegram account to claim it.", self.chat_id)
            self.chat_id = ""

    # ---- offset persistence so a restart does not replay old commands ----
    def _load_offset(self):
        try:
            with open(OFFSET_PATH) as f:
                return int(f.read().strip())
        except Exception:
            return 0

    def _save_offset(self, offset):
        config.ensure_home()
        try:
            with open(OFFSET_PATH, "w") as f:
                f.write(str(offset))
        except OSError as e:
            log.warning("could not persist update offset: %s", e)

    # ---- transport ----
    def api(self, method, params=None, timeout=POLL_TIMEOUT + 10):
        return notify_mod.call(self.token, method, params, timeout=timeout)

    def reply(self, chat_id, text, keyboard=None):
        if str(chat_id) == str(self.chat_id):
            # Authorized chat: go through the shared 1 msg/sec queue.
            if keyboard is None:
                self.notify.send(text)
                return
        try:
            self.api("sendMessage", {"chat_id": chat_id, "text": text[:3900],
                                     "disable_web_page_preview": True,
                                     "reply_markup": keyboard}, timeout=20)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("reply failed: %s", e)

    # ---- auth ----
    def authorized(self, chat_id):
        return self.chat_id and str(chat_id) == str(self.chat_id)

    def bind_chat(self, chat_id):
        """First /start on a fresh install claims the cockpit, permanently."""
        if self.bot_id and str(chat_id) == self.bot_id:
            log.error("refusing to bind the cockpit to the bot's own id")
            return
        self.chat_id = str(chat_id)
        config.set_env_value("TELEGRAM_CHAT_ID", self.chat_id)
        self.notify.set_chat_id(self.chat_id)
        log.warning("cockpit bound to chat id %s (written to ~/armm/.env)", self.chat_id)
        self.reply(chat_id, "✅ ARMM cockpit bound to this chat. Every other chat is "
                            "refused from now on.\n\n" + HELP)

    def refuse(self, chat_id, text):
        """Ignored and logged — never answered, so an attacker learns nothing."""
        if chat_id not in self._refused:
            self._refused.add(chat_id)
            self.notify.alert(f"🚫 command from UNAUTHORIZED chat {chat_id} ignored")
        log.warning("REFUSED chat_id=%s text=%r (authorized chat is %s)",
                    chat_id, text[:80], self.chat_id)
        self.store.log_event("unauthorized_command",
                             json.dumps({"chat_id": chat_id, "text": text[:200]}))

    # ---- loop ----
    def stop(self, *_):
        self._stop.set()

    def run(self):
        if not self.chat_id:
            msg = ("TELEGRAM_CHAT_ID is blank. Open Telegram, find your bot, and send "
                   "/start — the first /start received will claim this cockpit and be "
                   "written to ~/armm/.env. All other chats are refused afterwards.")
            log.warning(msg)
            print(msg, file=sys.stderr)
        log.info("cockpit polling (mode=%s, live orders %s)",
                 self.client.mode, "ALLOWED" if self.allow_live else "blocked")
        while not self._stop.is_set():
            try:
                r = self.api("getUpdates", {"offset": self._offset, "timeout": POLL_TIMEOUT,
                                            "allowed_updates": ["message", "callback_query"]})
            except (urllib.error.URLError, OSError, ValueError) as e:
                log.warning("getUpdates failed: %s", e)
                self._stop.wait(5)
                continue
            if not r.get("ok"):
                log.warning("getUpdates not ok: %s", str(r)[:200])
                self._stop.wait(5)
                continue
            for upd in r.get("result", []):
                self._offset = max(self._offset, upd["update_id"] + 1)
                self._save_offset(self._offset)
                try:
                    self.handle(upd)
                except Exception:
                    log.exception("error handling update %s", upd.get("update_id"))

    def poll_once(self):
        """One getUpdates round — used by tests and by --once."""
        r = self.api("getUpdates", {"offset": self._offset, "timeout": 0})
        for upd in r.get("result", []):
            self._offset = max(self._offset, upd["update_id"] + 1)
            self._save_offset(self._offset)
            self.handle(upd)
        return r

    # ---- dispatch ----
    def handle(self, upd):
        if "callback_query" in upd:
            return self._handle_callback(upd["callback_query"])
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat_id = str(msg.get("chat", {}).get("id"))
        text = (msg.get("text") or "").strip()
        if not text:
            return

        if not self.chat_id:
            if text.split()[0].split("@")[0].lower() == "/start":
                return self.bind_chat(chat_id)
            log.warning("unbound cockpit ignoring %r from %s — send /start to bind",
                        text[:40], chat_id)
            return
        if not self.authorized(chat_id):
            return self.refuse(chat_id, text)

        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        handlers = {
            "/start": lambda: self.reply(chat_id, HELP),
            "/help": lambda: self.reply(chat_id, HELP),
            "/status": lambda: self.cmd_status(chat_id),
            "/last": lambda: self.cmd_last(chat_id, args),
            "/halt": lambda: self.cmd_halt(chat_id, args),
            "/resume": lambda: self.cmd_resume(chat_id),
            "/ladder": lambda: self.cmd_ladder(chat_id, args),
            "/check": lambda: self.cmd_check(chat_id, args),
            "/anchor": lambda: self.cmd_anchor(chat_id, args),
            "/watch": lambda: self.cmd_watch(chat_id, args),
            "/unwatch": lambda: self.cmd_unwatch(chat_id, args),
            "/watching": lambda: self.cmd_watching(chat_id),
            "/auto": lambda: self.cmd_auto(chat_id, args),
            "/score": lambda: self.cmd_score(chat_id, args),
        }
        fn = handlers.get(cmd)
        if not fn:
            return self.reply(chat_id, f"unknown command {cmd}\n\n{HELP}")
        log.info("command %s from %s", cmd, chat_id)
        try:
            fn()
        except (WeexError, ladder.LadderError, specs.SpecError) as e:
            self.reply(chat_id, f"error: {e}")

    # ---- commands ----
    def cmd_status(self, chat_id):
        lines = [f"📊 ARMM status [{self.client.mode}]"]
        try:
            rows = self.client.balance() or []
            for r in (rows if isinstance(rows, list) else [rows]):
                lines.append(f"balance {r.get('asset')}: {float(r.get('balance') or 0):.2f} "
                             f"(avail {float(r.get('availableBalance') or 0):.2f}, "
                             f"uPnL {float(r.get('unrealizePnl') or 0):+.2f})")
        except WeexError as e:
            lines.append(f"balance: unavailable ({e})")

        try:
            pos = self.client.open_positions()
            if pos:
                for p in pos:
                    entry = f"{p['entry']:.2f}" if p["entry"] else "?"
                    lines.append(f"• {p['symbol']} {p['side']} size {p['size']} "
                                 f"@ {entry} uPnL {p['unrealized_pnl']:+.2f} "
                                 f"lev {p['leverage']}")
            else:
                lines.append("• no open positions on the exchange")
        except WeexError as e:
            lines.append(f"positions: unavailable ({e})")

        try:
            if self.client.demo:
                # The sim venue has no openOrders endpoint and its history lists
                # only TERMINAL orders, so a resting order is invisible there.
                # Report ARMM's own pending entries instead of a false zero.
                pending = [p for p in self.store.active_positions(mode="DEMO")
                           if p["state"] == store_mod.PENDING_ENTRY]
                lines.append(f"open orders: {len(pending)} pending entry(ies) tracked by ARMM "
                             f"(sim exposes no open-orders endpoint)")
                for p in pending[:5]:
                    lines.append(f"  – {p['symbol']} {p['side']} {p['qty']} @ "
                                 f"{p['entry_price']} order {p['entry_order_id']}")
            else:
                resting = self.client.open_orders() or []
                lines.append(f"open orders: {len(resting)}")
                for o in resting[:5]:
                    lines.append(f"  – {o.get('symbol')} {o.get('side')} {o.get('origQty')} "
                                 f"@ {o.get('price')} {o.get('status')}")
        except WeexError as e:
            lines.append(f"open orders: unavailable ({e})")

        tracked = self.store.active_positions(mode=self.client.mode)
        lines.append(f"tracked (db): {len(tracked)}"
                     + (" — " + ", ".join(f"#{p['id']} {p['symbol']} {p['state']}"
                                          for p in tracked) if tracked else ""))
        g = guards.state()
        lines.append(f"PnL today: {g.get('day_pnl', 0.0):+.2f} "
                     f"(opening equity {g.get('day_start_equity', 0.0):.2f})")
        lines.append("guards: " + guards.summary())
        self.reply(chat_id, "\n".join(lines))

    def cmd_last(self, chat_id, args):
        symbol = args[0].upper() if args else "BTCUSDT"
        rows = self.client.order_history(symbol, limit=10) or []
        if not rows:
            return self.reply(chat_id, f"no order history for {symbol}")
        out = [f"🧾 last orders {symbol} [{self.client.mode}] (newest first)"]
        for o in rows[:10]:
            cid = o.get("clientOrderId")
            who = origin_mod.parse(cid)
            tag = origin_mod.label(who)
            out.append(f"[{tag}] {o.get('status')} {o.get('side')} {o.get('origQty')} @ "
                       f"{o.get('price')} filled {o.get('executedQty')}\n"
                       f"    {cid}")
        self.reply(chat_id, "\n".join(out))

    def cmd_halt(self, chat_id, args):
        reason = " ".join(args) or "manual kill switch from Telegram"
        guards.halt(reason)
        self.store.log_event("halt", reason)
        self.reply(chat_id, f"🛑 HALTED — no new entries. Reason: {reason}\n"
                            f"Existing positions keep their atomic stops. /resume to clear.")

    def cmd_resume(self, chat_id):
        guards.resume()
        self.store.log_event("resume", "from Telegram")
        self.reply(chat_id, "▶️ resumed — new entries allowed again.")

    # ---- signal layer (v0.3) ----
    def snapshot(self, symbol):
        """Evaluate a symbol using its stored anchor. Read-only market data."""
        anchor = self.store.get_anchor(symbol)
        return signals.evaluate(symbol, anchor_ts=(anchor or {}).get("anchor_ts"),
                                client=self.client)

    def cmd_check(self, chat_id, args):
        symbol = (args[0].upper() if args else "BTCUSDT")
        snap = self.snapshot(symbol)
        anchor = self.store.get_anchor(symbol)
        text = snap.render()
        if anchor:
            text += (f"\nanchor {_fmt_ts(anchor['anchor_ts'])} "
                     f"({anchor.get('source')})")
        else:
            text += "\nno anchor set — using the series start. /anchor SYMBOL auto"
        self.reply(chat_id, text)

    def cmd_anchor(self, chat_id, args):
        if not args:
            return self.reply(chat_id, "usage: /anchor SYMBOL <YYYY-MM-DDTHH:MM | auto>")
        symbol = args[0].upper()
        spec = args[1] if len(args) > 1 else "auto"

        if spec.lower() == "auto":
            try:
                c1h = candles_mod.recent(self.client, symbol, "1h", limit=400)
            except (WeexError, candles_mod.CandleError) as e:
                return self.reply(chat_id, f"❌ could not load candles: {e}")
            ts, why = signals.find_swing_low_anchor(c1h)
            if ts is None:
                return self.reply(chat_id, f"❌ could not find an anchor: {why}")
            self.store.set_anchor(symbol, ts, source="auto")
            self.store.log_event("anchor_set", f"{symbol} auto {ts}: {why}")
            return self.reply(chat_id,
                              f"⚓ {symbol} anchor set to {_fmt_ts(ts)} (auto)\n{why}\n\n"
                              + self.snapshot(symbol).render())
        try:
            ts = _parse_iso_ms(spec)
        except ValueError as e:
            return self.reply(chat_id, f"❌ {e}")
        self.store.set_anchor(symbol, ts, source="manual")
        self.store.log_event("anchor_set", f"{symbol} manual {ts}")
        self.reply(chat_id, f"⚓ {symbol} anchor set to {_fmt_ts(ts)} (manual)\n\n"
                            + self.snapshot(symbol).render())

    def cmd_watch(self, chat_id, args):
        if not args:
            return self.reply(chat_id, "usage: /watch SYMBOL")
        symbol = args[0].upper()
        added = self.store.add_watch(symbol)
        if not self.store.get_anchor(symbol):
            try:
                c1h = candles_mod.recent(self.client, symbol, "1h", limit=400)
                ts, why = signals.find_swing_low_anchor(c1h)
                if ts:
                    self.store.set_anchor(symbol, ts, source="auto")
            except (WeexError, candles_mod.CandleError):
                pass
        msg = (f"👁 watching {symbol}" if added else f"already watching {symbol}")
        anchor = self.store.get_anchor(symbol)
        if anchor:
            msg += f" (anchor {_fmt_ts(anchor['anchor_ts'])}, {anchor.get('source')})"
        msg += ("\nYou get ONE alert when a setup confirms, then a 1h cooldown "
                "before it can fire again.")
        self.reply(chat_id, msg)

    def cmd_unwatch(self, chat_id, args):
        if not args:
            return self.reply(chat_id, "usage: /unwatch SYMBOL")
        symbol = args[0].upper()
        removed = self.store.remove_watch(symbol)
        self.reply(chat_id, f"🚫 stopped watching {symbol}" if removed
                   else f"{symbol} was not being watched")

    def cmd_watching(self, chat_id):
        watched = self.store.watches()
        if not watched:
            return self.reply(chat_id, "not watching anything. /watch BTCUSDT")
        L = ["👁 watching:"]
        for sym in watched:
            a = self.store.get_anchor(sym)
            st_a = self.store.setup_state(sym, "A")
            st_b = self.store.setup_state(sym, "B")
            L.append(f"• {sym} anchor "
                     + (f"{_fmt_ts(a['anchor_ts'])} ({a.get('source')})" if a else "unset")
                     + f" · A {'active' if st_a['active'] else 'idle'}"
                     + f" · B {'active' if st_b['active'] else 'idle'}")
        self.reply(chat_id, "\n".join(L))

    # ---- autopilot (v0.4) ----
    def _autopilot(self):
        """Build the demo-locked autopilot. Raises if this cockpit is live."""
        if self._auto is None:
            # Deliberately NOT self.client: a cockpit started with --live holds a
            # live client, and handing it to autopilot must fail loudly rather
            # than be silently corrected.
            self._auto = autopilot_mod.Autopilot(store=self.store, notify=self.notify,
                                                 client=None if not self.client.demo
                                                 else self.client)
        return self._auto

    def cmd_auto(self, chat_id, args):
        if not args:
            return self.reply(chat_id, "usage: /auto on SYMBOL | /auto off [SYMBOL] "
                                       "| /auto status")
        sub = args[0].lower()
        try:
            auto = self._autopilot()
        except autopilot_mod.AutopilotSafetyError as e:
            return self.reply(chat_id, f"🛑 autopilot refused to start: {e}")

        if sub == "status":
            return self.reply(chat_id, "\n".join(auto.status_lines()))

        if sub == "on":
            if len(args) < 2:
                return self.reply(chat_id, "usage: /auto on SYMBOL")
            symbol = args[1].upper()
            halted, reason = guards.is_halted()
            if halted:
                return self.reply(chat_id,
                                  f"⛔ trading is halted ({reason}). /resume first, "
                                  f"then /auto on {symbol}.")
            if not self.store.get_anchor(symbol):
                try:
                    c1h = candles_mod.recent(self.client, symbol, "1h", limit=400)
                    ts, _ = signals.find_swing_low_anchor(c1h)
                    if ts:
                        self.store.set_anchor(symbol, ts, source="auto")
                except (WeexError, candles_mod.CandleError):
                    pass
            auto.enable(symbol)
            return self.reply(
                chat_id,
                f"🤖 AUTOPILOT ON for {symbol} — DEMO ACCOUNT ONLY.\n"
                f"It will take its own Setup A/B entries on {DEMO_ROUTE}, at most one "
                f"per symbol per {autopilot_mod.PER_SYMBOL_MIN_GAP_SECONDS // 3600}h, "
                f"and it cannot override the advisor on anything.\n"
                f"/auto off {symbol} or /halt stops it immediately.\n\n"
                + "\n".join(auto.status_lines()))

        if sub == "off":
            symbol = args[1].upper() if len(args) > 1 else None
            stopped = auto.disable(symbol)
            return self.reply(chat_id, f"🛑 autopilot OFF for "
                                       f"{', '.join(stopped) if stopped else 'nothing'}")
        return self.reply(chat_id, f"unknown /auto subcommand {sub!r}")

    def cmd_score(self, chat_id, args):
        from . import scoreboard
        self.reply(chat_id, scoreboard.render(self.store))

    def cmd_ladder(self, chat_id, args):
        try:
            kw = parse_ladder_args(args)
        except ladder.LadderError as e:
            return self.reply(chat_id, f"❌ {e}")

        live = kw.pop("live", False)
        if live and not self.allow_live:
            return self.reply(chat_id,
                              "❌ LIVE orders are disabled on this process. Restart the "
                              "manager with --allow-live-orders if you really mean it.")
        client = self._client_for(live)

        try:
            plan = ladder.build_plan(client, **kw)
        except guards.GuardViolation as e:
            self.store.log_event("guard_rejection", f"telegram: {e}")
            return self.reply(chat_id, f"🚫 REFUSED by guards: {e}")
        except (ladder.LadderError, WeexError, specs.SpecError) as e:
            return self.reply(chat_id, f"❌ {e}")

        # The routing line goes out BEFORE anything is placed, always.
        body = plan.describe(client.routing_line(plan.symbol))

        # Advisor (v0.3): advisory only. It cannot block the trade, but a trade
        # that contradicts Andrew's own setups costs an explicit extra tap and
        # is written to the db as an override.
        anchor = self.store.get_anchor(plan.symbol)
        advice = advisor_mod.review(plan.symbol, plan.side, entry_price=plan.entry,
                                    client=client,
                                    anchor_ts=(anchor or {}).get("anchor_ts"),
                                    stop_price=plan.stop, leverage=plan.leverage,
                                    venue_leverage=_venue_leverage(self.store, plan.symbol))
        body += "\n\n" + advice.render()

        if live or advice.contradicted:
            token = f"c{int(time.time() * 1000) % 10_000_000}"
            self._pending[token] = {"plan": plan, "client": client, "ts": time.time(),
                                    "advice": advice, "live": live}
            label = ("✅ CONFIRM LIVE" if live and not advice.contradicted else
                     "⚠️ CONFIRM LIVE ANYWAY" if live else "⚠️ CONFIRM ANYWAY")
            kb = {"inline_keyboard": [[
                {"text": label, "callback_data": f"go:{token}"},
                {"text": "✖️ cancel", "callback_data": f"no:{token}"}]]}
            header = ("⚠️ LIVE ORDER — REAL MONEY\n" if live else "")
            return self.reply(chat_id, f"{header}{body}\n\n"
                                       f"Confirm within {CONFIRM_TTL}s.", keyboard=kb)

        self.reply(chat_id, body + "\n\nplacing…")
        self._execute(chat_id, plan, client, advice=advice)

    def _client_for(self, live):
        if not live:
            return self.client
        if self._live_client is None:
            self._live_client = WeexClient(demo=False)
        return self._live_client

    def _execute(self, chat_id, plan, client, advice=None):
        try:
            r = ladder.place_plan(client, plan, store=self.store, notify=self.notify,
                                  advice=advice, origin=origin_mod.TELEGRAM)
        except guards.GuardViolation as e:
            self.store.log_event("guard_rejection", f"telegram place: {e}")
            return self.reply(chat_id, f"🚫 REFUSED by guards: {e}")
        except WeexError as e:
            return self.reply(chat_id, f"❌ order error: {e}")
        msg = f"result: {r['status']}"
        if r.get("message"):
            msg += f"\n{r['message']}"
        if r.get("order"):
            o = r["order"]
            msg += (f"\norderId {o.get('orderId')} status {o.get('status')} "
                    f"@ {o.get('price')}")
        if r["status"] == "RESTING":
            msg += "\nEntry is resting; the manager will track the fill."
        if advice is not None and advice.contradicted:
            msg += f"\n📝 logged as an advisor override: {advice.db_note()}"
        self.reply(chat_id, msg)

    def _handle_callback(self, cq):
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id"))
        data = cq.get("data") or ""
        try:
            self.api("answerCallbackQuery", {"callback_query_id": cq.get("id")}, timeout=15)
        except Exception:
            pass
        if not self.authorized(chat_id):
            return self.refuse(chat_id, f"callback {data}")
        action, _, token = data.partition(":")
        entry = self._pending.pop(token, None)
        if not entry:
            return self.reply(chat_id, "that confirmation expired — run /ladder again")
        plan, client = entry["plan"], entry["client"]
        advice = entry.get("advice")
        if time.time() - entry["ts"] > CONFIRM_TTL:
            return self.reply(chat_id, "confirmation expired — run /ladder again")
        if action != "go":
            self.store.log_event("advisor_heeded",
                                 f"{plan.side} {plan.symbol} cancelled at the confirm step")
            return self.reply(chat_id, "cancelled — nothing placed")
        note = ("confirmed (advisor OVERRIDDEN)" if advice is not None
                and advice.contradicted else "confirmed")
        self.reply(chat_id, f"{note}\n{client.routing_line(plan.symbol)}\nplacing…")
        self._execute(chat_id, plan, client, advice=advice)


def main(argv=None):
    p = argparse.ArgumentParser(description="ARMM Telegram cockpit")
    p.add_argument("--live", action="store_true", help="read/report LIVE account")
    p.add_argument("--allow-live-orders", action="store_true",
                   help="permit '/ladder ... live' (off by default)")
    p.add_argument("--once", action="store_true", help="drain pending updates and exit")
    a = p.parse_args(argv)
    c = Cockpit(demo=not a.live, allow_live=a.allow_live_orders)
    if a.once:
        print(json.dumps(c.poll_once())[:400])
        return 0
    c.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
