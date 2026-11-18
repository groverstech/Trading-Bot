"""Persistent storage (SQLite, WAL mode).

All financial state - opportunities, orders, fills, positions, the cash
ledger, equity snapshots, incidents, the audit log and the live-trading
authorization trail - is stored here so that it survives restarts.

A single connection guarded by a re-entrant lock is used. SQLite operations
here are sub-millisecond, and serialising them keeps state transitions
(compare-and-set on trade stages) trivially atomic.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL);

-- One row per opportunity. ``id`` is the trade UID that links all six agents.
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    mode TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
    direction TEXT NOT NULL, strategy TEXT NOT NULL, timeframe TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE, stage TEXT NOT NULL, status TEXT NOT NULL,
    score REAL, signal TEXT NOT NULL, rugcheck TEXT, sizing TEXT, risk TEXT,
    execution TEXT, reject_reason TEXT, approval_token TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_stage ON trades(stage);
CREATE INDEX IF NOT EXISTS ix_trades_mode ON trades(mode, created_at);

CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT NOT NULL,
    trade_id TEXT, agent TEXT NOT NULL, event TEXT NOT NULL, level TEXT NOT NULL,
    message TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS ix_events_trade ON agent_events(trade_id);
CREATE INDEX IF NOT EXISTS ix_events_agent ON agent_events(agent, id);

CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, mode TEXT NOT NULL,
    market TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    type TEXT NOT NULL, purpose TEXT NOT NULL, qty TEXT NOT NULL,
    price TEXT, stop_price TEXT, status TEXT NOT NULL, exchange_id TEXT,
    filled_qty TEXT NOT NULL DEFAULT '0', avg_price TEXT, fee_usdt TEXT NOT NULL DEFAULT '0',
    created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_trade ON orders(trade_id);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, trade_id TEXT NOT NULL,
    mode TEXT NOT NULL, ts REAL NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL,
    fee_usdt TEXT NOT NULL, ref_price TEXT, slippage_usdt TEXT NOT NULL DEFAULT '0',
    exchange_trade_id TEXT, UNIQUE(client_id, exchange_trade_id));

CREATE TABLE IF NOT EXISTS positions (
    trade_id TEXT PRIMARY KEY, mode TEXT NOT NULL, market TEXT NOT NULL,
    symbol TEXT NOT NULL, direction TEXT NOT NULL, strategy TEXT NOT NULL,
    status TEXT NOT NULL, qty TEXT NOT NULL, entry_price TEXT NOT NULL,
    stop_price TEXT NOT NULL, target_price TEXT NOT NULL, leverage INTEGER NOT NULL,
    margin TEXT NOT NULL, liq_price TEXT, opened_at REAL NOT NULL, closed_at REAL,
    exit_price TEXT, exit_reason TEXT, gross_pnl TEXT NOT NULL DEFAULT '0',
    fees TEXT NOT NULL DEFAULT '0', funding TEXT NOT NULL DEFAULT '0',
    slippage TEXT NOT NULL DEFAULT '0', realized_pnl TEXT NOT NULL DEFAULT '0',
    last_funding_ts REAL);
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(mode, status);

-- Cash ledger. Balances are always derived by summing this table.
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT NOT NULL,
    account TEXT NOT NULL, trade_id TEXT, kind TEXT NOT NULL, amount TEXT NOT NULL,
    note TEXT);
CREATE INDEX IF NOT EXISTS ix_ledger ON ledger(mode, account);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT NOT NULL,
    reason TEXT NOT NULL, spot REAL NOT NULL, futures REAL NOT NULL,
    reserve REAL NOT NULL, total REAL NOT NULL, realized REAL NOT NULL,
    unrealized REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_eq ON equity_snapshots(mode, ts);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT NOT NULL,
    kind TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT NOT NULL,
    resolved_at REAL, resolved_by TEXT);

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY, venue TEXT NOT NULL, env TEXT NOT NULL,
    api_key_enc BLOB NOT NULL, api_secret_enc BLOB NOT NULL, key_hint TEXT NOT NULL,
    created_at REAL NOT NULL, verified_at REAL, verification TEXT);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
    last_seen REAL NOT NULL);

CREATE TABLE IF NOT EXISTS live_auth_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL,
    detail TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, params TEXT NOT NULL,
    result TEXT NOT NULL);
"""


# Additive column migrations: (table, column, definition)
MIGRATIONS = [
    ("positions", "protection", "TEXT"),
    ("fills", "net_qty", "TEXT"),
]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._depth = 0
        # executescript commits implicitly, so it runs outside tx()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            for table, col, ddl in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic transaction. Nested calls join the outer transaction."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return r[0] if r else None

    def meta_get(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key=?", (key,))

    def meta_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


def loads(s: str | None) -> Any:
    return json.loads(s) if s else None
