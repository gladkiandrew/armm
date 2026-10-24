"""ARMM outbound notifications — Telegram push, stdlib urllib, rate limited.

Sends go through a background queue so a slow Telegram API can never stall the
manager loop or the order path. Outbound is capped at 1 message/second.
Every alert is logged whether or not Telegram is configured.
"""
import json, queue, threading, time
import urllib.error, urllib.parse, urllib.request

import config
import logsetup

log = logsetup.get("notify")

API = "https://api.telegram.org"
MIN_INTERVAL = 1.0      # brief: max 1 outbound message per second
MAX_LEN = 3900          # Telegram hard limit is 4096


def api_url(token, method):
    return f"{API}/bot{token}/{method}"


def call(token, method, params=None, timeout=35):
    """One Telegram Bot API call. Raises on transport failure."""
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for k, v in (params or {}).items() if v is not None}).encode()
    req = urllib.request.Request(api_url(token, method), data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


_own_id_cache = {}


def _is_own_id(token, chat_id):
    """True when chat_id is the bot's own user id — an unreachable destination."""
    if token not in _own_id_cache:
        try:
            _own_id_cache[token] = str((call(token, "getMe", timeout=15)
                                        .get("result") or {}).get("id") or "")
        except Exception as e:
            log.warning("getMe failed, cannot validate the chat id: %s", e)
            _own_id_cache[token] = None
    own = _own_id_cache[token]
    return bool(own) and str(chat_id) == own


class Notifier:
    def __init__(self, token=None, chat_id=None, enabled=True):
        self.token = token or config.env_get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or config.env_get("TELEGRAM_CHAT_ID")
        if self.token and self.chat_id and _is_own_id(self.token, self.chat_id):
            log.error("TELEGRAM_CHAT_ID (%s) is the bot's own id — a bot cannot send to "
                      "itself. Push alerts are DISABLED until a real chat sends /start.",
                      self.chat_id)
            self.chat_id = ""
        self.enabled = bool(enabled and self.token and self.chat_id)
        self._q = queue.Queue()
        self._last = 0.0
        self._thread = None
        self._stop = threading.Event()
        if self.enabled:
            self._start()

    def _start(self):
        self._thread = threading.Thread(target=self._pump, name="notify", daemon=True)
        self._thread.start()

    def set_chat_id(self, chat_id):
        """Called once, after the first /start binds the cockpit to a chat."""
        self.chat_id = str(chat_id)
        if self.token and not self.enabled:
            self.enabled = True
            self._start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                text = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            gap = MIN_INTERVAL - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            try:
                r = call(self.token, "sendMessage",
                         {"chat_id": self.chat_id, "text": text[:MAX_LEN],
                          "disable_web_page_preview": True})
                if not r.get("ok"):
                    log.warning("telegram send rejected: %s", str(r)[:200])
            except (urllib.error.URLError, OSError, ValueError) as e:
                log.warning("telegram send failed: %s", e)
            self._last = time.monotonic()

    # ---- public ----
    def send(self, text):
        """Queue a message. No-op (logged) when Telegram is not configured."""
        if not self.enabled:
            log.info("[no-telegram] %s", text)
            return False
        self._q.put(text)
        return True

    def alert(self, text):
        """Log it AND push it. Use for anything Andrew must not miss."""
        log.info("ALERT %s", text)
        self.send(text)
        return True

    def stop(self):
        self._stop.set()


_default = None
_default_lock = threading.Lock()


def default():
    global _default
    with _default_lock:
        if _default is None:
            _default = Notifier()
        return _default
