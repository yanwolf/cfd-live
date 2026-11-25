"""SQLite（放在 DATA_DIR，Coolify 掛 volume 就會保留）"""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, inst TEXT, op TEXT, payload TEXT,
  status TEXT, created INTEGER, not_before INTEGER DEFAULT 0, sent_at INTEGER, result TEXT);
CREATE INDEX IF NOT EXISTS actions_status ON actions(status);
CREATE TABLE IF NOT EXISTS deals (ticket INTEGER PRIMARY KEY, ts INTEGER, magic INTEGER, symbol TEXT, entry INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, inst TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS ext_trades (key TEXT PRIMARY KEY, inst TEXT, ts INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS ext_trades_inst ON ext_trades(inst, ts);
CREATE TABLE IF NOT EXISTS tg_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER, text TEXT, tries INTEGER DEFAULT 0,
  next_try INTEGER DEFAULT 0, sent_at INTEGER, gave_up INTEGER DEFAULT 0, last_err TEXT);
"""


def conn():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(os.path.join(config.DATA_DIR, "live.db"), check_same_thread=False, isolation_level=None)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute(f"PRAGMA synchronous={os.getenv('DB_SYNCHRONOUS', 'NORMAL')}")
            _conn.executescript(SCHEMA)
        return _conn


def q(sql, args=()):
    with _lock:
        return conn().execute(sql, args).fetchall()


def ex(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def exmany(sql, rows):
    with _lock:
        c = conn()
        c.execute("BEGIN")
        c.executemany(sql, rows)
        c.execute("COMMIT")


def kv_get(k, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r[0][0]) if r else default


def kv_set(k, v):
    ex("INSERT INTO kv(k, v, ts) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
       (k, json.dumps(v, ensure_ascii=False), int(time.time())))


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
