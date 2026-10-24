#!/usr/bin/env python3
"""ARMM persistent state — SQLite at ~/armm/armm.db.

Holds the position state machine (PENDING_ENTRY -> OPEN -> EXITING -> CLOSED)
and every order we have ever asked the exchange for. The UNIQUE constraint on
client_id is the duplicate-order backstop: after a crash the manager re-derives
the same deterministic client id, the insert fails, and nothing is placed twice.
"""
import json, os, sqlite3, threading, time

import config
import logsetup

log = logsetup.get("store")

PENDING_ENTRY = "PENDING_ENTRY"
OPEN = "OPEN"
EXITING = "EXITING"
CLOSED = "CLOSED"
ACTIVE_STATES = (PENDING_ENTRY, OPEN, EXITING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  leverage INTEGER,
  notional REAL,
  entry_price REAL,
  qty REAL,
  filled_qty REAL DEFAULT 0,
  avg_entry REAL,
  stop_price REAL,
  targets TEXT DEFAULT '[]',
  splits TEXT DEFAULT '[]',
  entry_client_id TEXT UNIQUE,
  entry_order_id TEXT,
  tranches_placed INTEGER DEFAULT 0,
  virtual_hits TEXT DEFAULT '[]',
  source TEXT DEFAULT 'cli',
  realized_pnl REAL DEFAULT 0,
  created_at REAL,
  updated_at REAL,
  closed_at REAL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER,
  kind TEXT NOT NULL,
  client_id TEXT NOT NULL UNIQUE,
  order_id TEXT,
  symbol TEXT,
  side TEXT,
  position_side TEXT,
  price REAL,
  qty REAL,
  status TEXT,
  reduce_only INTEGER DEFAULT 0,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  position_id INTEGER,
  kind TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_state ON positions(state);
CREATE INDEX IF NOT EXISTS idx_ord_pos ON orders(position_id);

-- v0.3 signal layer --------------------------------------------------------
CREATE TABLE IF NOT EXISTS anchors (
  symbol TEXT PRIMARY KEY,
  anchor_ts INTEGER NOT NULL,
  source TEXT,
  set_at REAL
);
CREATE TABLE IF NOT EXISTS watches (
  symbol TEXT PRIMARY KEY,
  added_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS autopilot (
  symbol TEXT PRIMARY KEY,
  enabled INTEGER DEFAULT 0,
  last_entry_ts REAL DEFAULT 0,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS setup_state (
  symbol TEXT NOT NULL,
  setup TEXT NOT NULL,
  active INTEGER DEFAULT 0,
  last_alert_ts REAL DEFAULT 0,
  updated_at REAL,
  PRIMARY KEY (symbol, setup)
);
"""

# Columns added after v0.2 shipped. Applied idempotently on every open so a
# running v0.2 database upgrades in place without a migration step.
ADDED_COLUMNS = [
    ("positions", "advisor_overridden", "INTEGER DEFAULT 0"),
    ("positions", "advisor_notes", "TEXT"),
    # v0.4
    ("setup_state", "auto_acted_ts", "REAL DEFAULT 0"),
    ("positions", "exit_price", "REAL"),
    ("positions", "exit_reason", "TEXT"),
    ("positions", "r_multiple", "REAL"),
    ("positions", "exit_source", "TEXT"),
]


class Store:
    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        if self.path != ":memory:":
            config.ensure_home()
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self):
        """Add post-v0.2 columns if they are missing. ALTER TABLE ADD COLUMN is
        cheap and non-locking, so the live service picks this up on restart
        without anyone running a migration by hand."""
        for table, column, decl in ADDED_COLUMNS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                log.info("migrated: %s.%s added", table, column)

    def close(self):
        with self._lock:
            self.db.close()

    # ---- positions ----
    def create_position(self, symbol, side, mode, leverage, notional, entry_price,
                        qty, stop_price, targets, splits, entry_client_id,
                        state=PENDING_ENTRY, source="cli"):
        now = time.time()
        with self._lock:
            cur = self.db.execute(
                """INSERT INTO positions (symbol, side, mode, state, leverage, notional,
                     entry_price, qty, stop_price, targets, splits, entry_client_id,
                     source, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol, side, mode, state, leverage, notional, entry_price, qty,
                 stop_price, json.dumps(targets), json.dumps(splits), entry_client_id,
                 source, now, now))
            self.db.commit()
            return cur.lastrowid

    def update_position(self, position_id, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"UPDATE positions SET {cols} WHERE id=?",
                            (*fields.values(), position_id))
            self.db.commit()

    def get_position(self, position_id):
        with self._lock:
            r = self.db.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        return dict(r) if r else None

    def position_by_client_id(self, client_id):
        with self._lock:
            r = self.db.execute("SELECT * FROM positions WHERE entry_client_id=?",
                                (client_id,)).fetchone()
        return dict(r) if r else None

    def active_positions(self, mode=None):
        q = f"SELECT * FROM positions WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})"
        args = list(ACTIVE_STATES)
        if mode:
            q += " AND mode=?"
            args.append(mode)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [dict(r) for r in rows]

    def open_position_count(self, mode=None):
        return len(self.active_positions(mode=mode))

    # ---- orders ----
    def add_order(self, position_id, kind, client_id, symbol, side, position_side,
                  price, qty, status="NEW", order_id=None, reduce_only=False):
        """Insert-or-ignore. Returns True if this call created the row (i.e. the
        caller now owns placing it), False if it already existed."""
        now = time.time()
        with self._lock:
            cur = self.db.execute(
                """INSERT OR IGNORE INTO orders (position_id, kind, client_id, order_id,
                     symbol, side, position_side, price, qty, status, reduce_only,
                     created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (position_id, kind, client_id, order_id, symbol, side, position_side,
                 price, qty, status, int(bool(reduce_only)), now, now))
            self.db.commit()
            return cur.rowcount == 1

    def update_order(self, client_id, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"UPDATE orders SET {cols} WHERE client_id=?",
                            (*fields.values(), client_id))
            self.db.commit()

    def get_order(self, client_id):
        with self._lock:
            r = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return dict(r) if r else None

    def orders_for(self, position_id, kind=None):
        q, args = "SELECT * FROM orders WHERE position_id=?", [position_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [dict(r) for r in rows]

    # ---- anchors (v0.3) ----
    def set_anchor(self, symbol, anchor_ts, source="manual"):
        with self._lock:
            self.db.execute(
                """INSERT INTO anchors (symbol, anchor_ts, source, set_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     anchor_ts=excluded.anchor_ts, source=excluded.source,
                     set_at=excluded.set_at""",
                (symbol.upper(), int(anchor_ts), source, time.time()))
            self.db.commit()

    def get_anchor(self, symbol):
        with self._lock:
            r = self.db.execute("SELECT * FROM anchors WHERE symbol=?",
                                (symbol.upper(),)).fetchone()
        return dict(r) if r else None

    def all_anchors(self):
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM anchors ORDER BY symbol")]

    # ---- watches (v0.3) ----
    def add_watch(self, symbol):
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO watches (symbol, added_at) VALUES (?,?)",
                (symbol.upper(), time.time()))
            self.db.commit()
            return cur.rowcount == 1

    def remove_watch(self, symbol):
        with self._lock:
            cur = self.db.execute("DELETE FROM watches WHERE symbol=?", (symbol.upper(),))
            self.db.commit()
            return cur.rowcount == 1

    def watches(self):
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT symbol FROM watches ORDER BY symbol")]

    # ---- edge-triggered setup alerts (v0.3) ----
    def setup_state(self, symbol, setup):
        with self._lock:
            r = self.db.execute("SELECT * FROM setup_state WHERE symbol=? AND setup=?",
                                (symbol.upper(), setup)).fetchone()
        return dict(r) if r else {"symbol": symbol.upper(), "setup": setup,
                                  "active": 0, "last_alert_ts": 0.0}

    def save_setup_state(self, symbol, setup, active, last_alert_ts, auto_acted_ts=None):
        """Persist the edge state. Deactivating CLEARS auto_acted_ts, which is
        what makes autopilot's one-entry-per-activation rule re-arm."""
        if not active:
            auto_acted_ts = 0.0
        elif auto_acted_ts is None:
            auto_acted_ts = float(self.setup_state(symbol, setup).get("auto_acted_ts") or 0)
        with self._lock:
            self.db.execute(
                """INSERT INTO setup_state (symbol, setup, active, last_alert_ts,
                                            auto_acted_ts, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(symbol, setup) DO UPDATE SET
                     active=excluded.active, last_alert_ts=excluded.last_alert_ts,
                     auto_acted_ts=excluded.auto_acted_ts,
                     updated_at=excluded.updated_at""",
                (symbol.upper(), setup, int(bool(active)), float(last_alert_ts),
                 float(auto_acted_ts or 0), time.time()))
            self.db.commit()

    # ---- autopilot (v0.4) ----
    def set_auto(self, symbol, enabled):
        with self._lock:
            self.db.execute(
                """INSERT INTO autopilot (symbol, enabled, updated_at) VALUES (?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (symbol.upper(), int(bool(enabled)), time.time()))
            self.db.commit()

    def auto_enabled(self, symbol):
        with self._lock:
            r = self.db.execute("SELECT enabled FROM autopilot WHERE symbol=?",
                                (symbol.upper(),)).fetchone()
        return bool(r and r[0])

    def auto_symbols(self):
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT symbol FROM autopilot WHERE enabled=1 ORDER BY symbol")]

    def record_auto_entry(self, symbol, ts=None):
        with self._lock:
            self.db.execute(
                """INSERT INTO autopilot (symbol, enabled, last_entry_ts, updated_at)
                   VALUES (?,1,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     last_entry_ts=excluded.last_entry_ts, updated_at=excluded.updated_at""",
                (symbol.upper(), float(ts if ts is not None else time.time()), time.time()))
            self.db.commit()

    def last_auto_entry_ts(self, symbol):
        with self._lock:
            r = self.db.execute("SELECT last_entry_ts FROM autopilot WHERE symbol=?",
                                (symbol.upper(),)).fetchone()
        return float(r[0]) if r and r[0] else 0.0

    # ---- small key/value scratch (v0.4) ----
    def get_meta(self, key, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        with self._lock:
            self.db.execute(
                """INSERT INTO meta (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (key, str(value), time.time()))
            self.db.commit()

    def meta_keys(self, prefix=""):
        """Meta keys carrying a prefix, sorted. Lets a subsystem enumerate its
        own per-symbol rows without a table of its own."""
        with self._lock:
            rows = self.db.execute(
                "SELECT key FROM meta WHERE key LIKE ? ORDER BY key",
                (prefix.replace("%", "") + "%",)).fetchall()
        return [r[0] for r in rows]

    def closed_positions(self, since=None, mode=None):
        q = "SELECT * FROM positions WHERE state=?"
        args = [CLOSED]
        if since is not None:
            q += " AND COALESCE(closed_at, updated_at, created_at) >= ?"
            args.append(float(since))
        if mode:
            q += " AND mode=?"
            args.append(mode)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [dict(r) for r in rows]

    # ---- events ----
    def log_event(self, kind, detail, position_id=None):
        with self._lock:
            self.db.execute("INSERT INTO events (ts, position_id, kind, detail) VALUES (?,?,?,?)",
                            (time.time(), position_id, kind, detail))
            self.db.commit()
        log.info("event %s pos=%s %s", kind, position_id, detail)

    def recent_events(self, limit=20):
        with self._lock:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(r) for r in rows]


_default = None
_default_lock = threading.Lock()


def default():
    """Process-wide Store, so ladder/manager/telegram share one database."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Store()
        return _default
