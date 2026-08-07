from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from .config import settings


class DB:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._init()

    def _init(self):
        with self.lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS strategy_state(
                    strategy TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    balance REAL NOT NULL,
                    initial_balance REAL NOT NULL,
                    params_json TEXT NOT NULL,
                    live_eligible INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    score REAL NOT NULL,
                    entry REAL NOT NULL,
                    stop REAL NOT NULL,
                    tps_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    entry REAL NOT NULL,
                    stop REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    remaining_qty REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    opened_at INTEGER NOT NULL,
                    trailing_atr REAL NOT NULL,
                    stage TEXT NOT NULL,
                    tp1_hit INTEGER NOT NULL DEFAULT 0,
                    tp2_hit INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER NOT NULL,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry REAL NOT NULL,
                    exit REAL NOT NULL,
                    qty REAL NOT NULL,
                    gross_pnl REAL NOT NULL,
                    fees REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    r_multiple REAL NOT NULL,
                    reason TEXT NOT NULL,
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER NOT NULL,
                    features_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS adjustments(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oi_snapshots(
                    symbol TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    oi REAL NOT NULL,
                    PRIMARY KEY(symbol, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_trades_strategy_closed ON trades(strategy, closed_at);
                CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
                CREATE INDEX IF NOT EXISTS idx_oi_symbol_ts ON oi_snapshots(symbol, ts);
                """
            )
            self.conn.commit()

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def ensure_strategy(self, name: str, display_name: str, stage: str, params: dict, initial: float):
        now = int(time.time() * 1000)
        self.execute(
            """INSERT INTO strategy_state(strategy,display_name,stage,balance,initial_balance,params_json,updated_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(strategy) DO NOTHING""",
            (name, display_name, stage, initial, initial, json.dumps(params), now),
        )

    def update_strategy(self, name: str, *, stage: str | None = None, params: dict | None = None, live_eligible: bool | None = None):
        state = self.one("SELECT * FROM strategy_state WHERE strategy=?", (name,))
        if not state:
            return
        self.execute(
            "UPDATE strategy_state SET stage=?, params_json=?, live_eligible=?, updated_at=? WHERE strategy=?",
            (
                stage or state["stage"],
                json.dumps(params if params is not None else json.loads(state["params_json"])),
                int(live_eligible if live_eligible is not None else state["live_eligible"]),
                int(time.time() * 1000),
                name,
            ),
        )

    def adjust_balance(self, strategy: str, delta: float):
        self.execute(
            "UPDATE strategy_state SET balance=balance+?, updated_at=? WHERE strategy=?",
            (delta, int(time.time() * 1000), strategy),
        )

    def log_adjustment(self, strategy: str, kind: str, before: dict, after: dict, reason: str, accepted: bool):
        self.execute(
            "INSERT INTO adjustments(strategy,kind,before_json,after_json,reason,accepted,created_at) VALUES(?,?,?,?,?,?,?)",
            (strategy, kind, json.dumps(before), json.dumps(after), reason, int(accepted), int(time.time() * 1000)),
        )


db = DB(settings.db_path)
