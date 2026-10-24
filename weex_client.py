#!/usr/bin/env python3
"""ARMM — thin WEEX contract API client (native REST, stdlib only).

Auth: Base64(HMAC-SHA256(secret, ts + METHOD + path[?query] + body)).
Demo mode routes every order/account call to /capi/v3/sim/* and maps symbols to
their SUSDT-quoted twins. Market data always uses the real symbol.

NO autonomous logic lives here. Pure API surface + rate pacing.
A timeout NEVER authorizes an automatic retry (WEEX guidance): _request raises.
"""
import base64, hashlib, hmac, json, os, threading, time
import urllib.error, urllib.parse, urllib.request

import config
import logsetup
from specs import market_symbol

log = logsetup.get("weex")

BASE = "https://api-contract.weex.com"

# Rate limits (brief invariant 7): 10 req/s general; sim orders 1 per 10s.
REQ_MIN_INTERVAL = 0.1
SIM_ORDER_MIN_INTERVAL = 10.0
LIVE_ORDER_MIN_INTERVAL = 0.2

LIVE_ORDER_PATH = "/capi/v3/order"
DEMO_ORDER_PATH = "/capi/v3/sim/order"


class WeexError(Exception):
    pass


class _Pacer:
    """Process-wide spacing between calls. Shared so manager + Telegram threads
    cannot together breach a limit that each respects on its own."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}

    def wait(self, key, interval):
        with self._lock:
            now = time.monotonic()
            last = self._last.get(key, 0.0)
            delay = interval - (now - last)
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._last[key] = now
        return


_pacer = _Pacer()


class WeexClient:
    def __init__(self, env_path=None, demo=True, timeout=15):
        self.demo = demo
        self.timeout = timeout
        env = config.load_env(env_path)
        try:
            self.key = env["WEEX_API_KEY"]
            self.secret = env["WEEX_API_SECRET"]
            self.passphrase = env["WEEX_API_PASSPHRASE"]
        except KeyError as e:
            raise WeexError(f"missing {e.args[0]} in {env_path or config.ENV_PATH}")

    # ---- plumbing ----
    def _sign(self, ts, method, path, body=""):
        msg = f"{ts}{method}{path}{body}"
        return base64.b64encode(
            hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

    def _request(self, method, path, params=None, body=None, private=True):
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                query = "?" + urllib.parse.urlencode(clean)
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = BASE + path + query
        req = urllib.request.Request(url, data=payload.encode() if payload else None,
                                     method=method)
        req.add_header("Content-Type", "application/json")
        if private:
            ts = str(int(time.time() * 1000))
            req.add_header("ACCESS-KEY", self.key)
            req.add_header("ACCESS-PASSPHRASE", self.passphrase)
            req.add_header("ACCESS-TIMESTAMP", ts)
            req.add_header("ACCESS-SIGN", self._sign(ts, method, path + query, payload))
        _pacer.wait("req", REQ_MIN_INTERVAL)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise WeexError(f"HTTP {e.code} {method} {path}: {e.read().decode()[:300]}")
        except urllib.error.URLError as e:
            # Includes socket timeouts. Raised, never retried here: an unanswered
            # order request may still have reached the matching engine.
            raise WeexError(f"network error {method} {path}: {e.reason}")

    def _sym(self, symbol):
        """Demo markets use simulated symbols: BTCUSDT -> BTCSUSDT (SUSDT-quoted)."""
        s = symbol.upper()
        if self.demo and s.endswith("USDT") and not s.endswith("SUSDT"):
            return s[:-4] + "SUSDT"
        if not self.demo:
            return market_symbol(s)
        return s

    @property
    def mode(self):
        return "DEMO" if self.demo else "LIVE"

    @property
    def order_path(self):
        return DEMO_ORDER_PATH if self.demo else LIVE_ORDER_PATH

    def routing_line(self, symbol):
        """The line the brief requires printed/sent before EVERY order."""
        where = (f"{DEMO_ORDER_PATH}  (DEMO — SUSDT paper account)" if self.demo
                 else f"{LIVE_ORDER_PATH}  (LIVE — REAL MONEY)")
        return f"routing: {where} | exchange symbol: {self._sym(symbol)}"

    # ---- market data (public, real symbols) ----
    def ticker(self, symbol):
        return self._request("GET", "/capi/v3/market/ticker/24hr",
                             params={"symbol": market_symbol(symbol)}, private=False)

    def last_price(self, symbol):
        r = self.ticker(symbol)
        row = r[0] if isinstance(r, list) and r else r
        if not row or "lastPrice" not in row:
            raise WeexError(f"no ticker for {symbol}: {str(r)[:160]}")
        return float(row["lastPrice"])

    def book_ticker(self, symbol):
        """Best bid/ask — the anti-cross reference for POST_ONLY entries."""
        r = self._request("GET", "/capi/v3/market/ticker/bookTicker",
                          params={"symbol": market_symbol(symbol)}, private=False)
        row = r[0] if isinstance(r, list) and r else r
        if not row or "bidPrice" not in row:
            raise WeexError(f"no book ticker for {symbol}: {str(r)[:160]}")
        return {"bid": float(row["bidPrice"]), "ask": float(row["askPrice"]),
                "bid_qty": float(row.get("bidQty") or 0),
                "ask_qty": float(row.get("askQty") or 0)}

    def exchange_info(self, symbol=None):
        return self._request("GET", "/capi/v3/market/exchangeInfo",
                             params={"symbol": market_symbol(symbol)} if symbol else None,
                             private=False)

    def klines(self, symbol, interval="1m", limit=100):
        """Most recent klines, up to 1000. NO time range.

        Rows arrive DESCENDING by open time with the in-progress bar appended
        LAST — always run them through candles.normalize() before use.
        """
        return self._request("GET", "/capi/v3/market/klines",
                             params={"symbol": market_symbol(symbol),
                                     "interval": interval, "limit": limit}, private=False)

    def history_klines(self, symbol, interval="1h", start_time=None, end_time=None,
                       limit=100, price_type=None):
        """Historical klines within a window. limit caps at 100 (>100 -> -1142).

        startTime WITHOUT endTime returns []; endTime alone is ignored. Pass
        both. Rows are DESCENDING, so deep history pages backward — see
        candles.history().
        """
        if limit > 100:
            raise WeexError("historyKlines caps limit at 100")
        params = {"symbol": market_symbol(symbol), "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        if price_type:
            params["priceType"] = price_type
        r = self._request("GET", "/capi/v3/market/historyKlines", params=params,
                          private=False)
        if isinstance(r, dict) and r.get("code"):
            raise WeexError(f"historyKlines {r.get('code')}: {r.get('msg')}")
        return r or []

    def depth(self, symbol, limit=20):
        return self._request("GET", "/capi/v3/market/depth",
                             params={"symbol": market_symbol(symbol), "limit": limit},
                             private=False)

    # ---- account ----
    def balance(self):
        path = "/capi/v3/sim/balance" if self.demo else "/capi/v3/account/balance"
        return self._request("GET", path)

    def equity(self):
        """Total balance across margin assets, as a float. 0.0 if unreadable."""
        rows = self.balance() or []
        if isinstance(rows, dict):
            rows = [rows]
        return sum(float(r.get("balance") or 0) for r in rows)

    def positions(self):
        path = ("/capi/v3/sim/position/allPosition" if self.demo
                else "/capi/v3/account/position/allPosition")
        return self._request("GET", path)

    def open_positions(self):
        """Normalized non-zero positions: symbol/side/size/entry/leverage/pnl."""
        out = []
        for p in (self.positions() or []):
            size = float(p.get("size", p.get("positionAmt", p.get("total", 0))) or 0)
            if size == 0:
                continue
            open_value = float(p.get("openValue") or 0)
            out.append({
                "symbol": market_symbol(str(p.get("symbol", ""))),
                "raw_symbol": p.get("symbol"),
                "side": (p.get("side") or p.get("positionSide") or "").upper(),
                "size": abs(size),
                "entry": (open_value / abs(size)) if size and open_value else None,
                "leverage": p.get("leverage"),
                "unrealized_pnl": float(p.get("unrealizePnl") or 0),
                "liq_price": float(p.get("liquidatePrice") or 0) or None,
            })
        return out

    def symbol_config(self, symbol=None):
        """Live only: the venue's margin mode + leverage per symbol."""
        if self.demo:
            raise WeexError("demo mode exposes no symbolConfig endpoint")
        return self._request("GET", "/capi/v3/account/symbolConfig",
                             params={"symbol": self._sym(symbol)} if symbol else None)

    def set_leverage(self, symbol, leverage, margin_type=None, side=None):
        """Live only: make the venue enforce our leverage cap, not just our math.

        The sim venue exposes no leverage endpoint (its four endpoints are
        balance, positions, order history and place order), so in DEMO the
        account's own leverage setting governs real margin and liquidation.
        """
        if self.demo:
            raise WeexError("demo mode exposes no leverage endpoint — the sim "
                            "account's own leverage setting applies")
        body = {"symbol": self._sym(symbol), "crossLeverage": str(int(leverage))}
        if margin_type:
            body["marginType"] = margin_type
        if side == "LONG":
            body["isolatedLongLeverage"] = str(int(leverage))
        elif side == "SHORT":
            body["isolatedShortLeverage"] = str(int(leverage))
        return self._request("POST", "/capi/v3/account/leverage", body=body)

    # ---- orders ----
    def place_order(self, symbol, side, position_side, qty, price=None,
                    order_type="LIMIT", tif="POST_ONLY", client_id=None,
                    tp_trigger=None, sl_trigger=None, working_type="MARK_PRICE",
                    reduce_only=None):
        """Place one order. Paced: demo is capped at 1 order / 10s.

        `reduce_only` is only sent when explicitly set. WEEX documents it on order
        *responses* but not on the place-order request; in hedge mode a close is
        already expressed by side+positionSide, so we leave it off by default.
        """
        body = {
            "symbol": self._sym(symbol), "side": side, "positionSide": position_side,
            "type": order_type, "quantity": str(qty),
            "newClientOrderId": client_id or f"armm-{int(time.time()*1000)}",
        }
        if order_type == "LIMIT":
            body["timeInForce"] = tif
            body["price"] = str(price)
        if tp_trigger:
            body["tpTriggerPrice"] = str(tp_trigger)
            body["TpWorkingType"] = working_type
        if sl_trigger:
            body["slTriggerPrice"] = str(sl_trigger)
            body["SlWorkingType"] = working_type
        if reduce_only is not None:
            body["reduceOnly"] = bool(reduce_only)
        _pacer.wait("order", SIM_ORDER_MIN_INTERVAL if self.demo else LIVE_ORDER_MIN_INTERVAL)
        log.info("%s place %s %s %s qty=%s px=%s cid=%s", self.mode, body["symbol"],
                 side, position_side, qty, price, body["newClientOrderId"])
        r = self._request("POST", self.order_path, body=body)
        if isinstance(r, dict) and r.get("success") is False:
            raise WeexError(f"order rejected: {r.get('errorCode')} {r.get('errorMessage')}")
        return r

    def order_history(self, symbol=None, limit=None):
        path = "/capi/v3/sim/order/history" if self.demo else "/capi/v3/order/history"
        params = {}
        if symbol:
            params["symbol"] = self._sym(symbol)
        if limit:
            params["limit"] = limit
        return self._request("GET", path, params=params or None) or []

    def open_orders(self, symbol=None):
        """Live only — sim exposes no open-orders endpoint (history is the proxy)."""
        if self.demo:
            raise WeexError("demo mode has no openOrders endpoint")
        return self._request("GET", "/capi/v3/openOrders",
                             params={"symbol": self._sym(symbol)} if symbol else None) or []

    def orders_snapshot(self, symbol=None, limit=50):
        """Best available view of orders for this mode. Demo: recent history
        (newest-first). Live: currently-open orders."""
        if self.demo:
            return self.order_history(symbol, limit=limit)
        return self.open_orders(symbol)

    def find_order(self, client_id, symbol=None, limit=100):
        """Look up one of our orders by client id — the idempotency check."""
        for o in (self.orders_snapshot(symbol, limit=limit) or []):
            if str(o.get("clientOrderId")) == str(client_id):
                return o
        if not self.demo:
            for o in (self.order_history(symbol, limit=limit) or []):
                if str(o.get("clientOrderId")) == str(client_id):
                    return o
        return None

    def get_order(self, order_id):
        if self.demo:
            raise WeexError("demo mode has no single-order endpoint")
        return self._request("GET", "/capi/v3/order", params={"orderId": order_id})

    def cancel_order(self, order_id, symbol=None):
        if self.demo:
            raise WeexError("Demo mode has no cancel endpoint — sim orders expire/fill on their own.")
        return self._request("DELETE", "/capi/v3/order",
                             params={"orderId": order_id,
                                     "symbol": self._sym(symbol) if symbol else None})

    def cancel_all(self, symbol=None):
        if self.demo:
            raise WeexError("Demo mode has no cancel endpoint — sim orders expire/fill on their own.")
        params = {"symbol": self._sym(symbol)} if symbol else None
        return self._request("DELETE", "/capi/v3/allOpenOrders", params=params)

    def place_tp_sl(self, symbol, position_side, plan_type, trigger_price,
                    qty=None, execute_price=None, client_algo_id=None,
                    trigger_price_type="MARK_PRICE"):
        """Live-only conditional TP/SL plan order (partial qty supported)."""
        if self.demo:
            raise WeexError("demo mode has no conditional-order endpoint — "
                            "use the atomic tp/slTriggerPrice on the entry")
        body = {
            "symbol": self._sym(symbol), "positionSide": position_side,
            "planType": plan_type, "triggerPrice": str(trigger_price),
            "clientAlgoId": client_algo_id or f"armm-a-{int(time.time()*1000)}",
            "triggerPriceType": trigger_price_type,
        }
        if qty:
            body["quantity"] = str(qty)
        if execute_price:
            body["executePrice"] = str(execute_price)
        _pacer.wait("order", LIVE_ORDER_MIN_INTERVAL)
        return self._request("POST", "/capi/v3/placeTpSlOrder", body=body)
