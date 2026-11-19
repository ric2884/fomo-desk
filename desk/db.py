from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    mint TEXT PRIMARY KEY,
    name TEXT, symbol TEXT, pool TEXT,
    migrated_at REAL NOT NULL,
    signature TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    mint TEXT NOT NULL, ts REAL NOT NULL,
    price REAL, liq REAL, mcap REAL, vol_m5 REAL,
    buys_m5 INTEGER, sells_m5 INTEGER,
    PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS rugcheck (
    mint TEXT NOT NULL, ts REAL NOT NULL, raw_json TEXT NOT NULL,
    PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS heartbeats (name TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_coins_migrated ON coins(migrated_at);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con
