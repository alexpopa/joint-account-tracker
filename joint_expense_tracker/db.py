from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    DB_PATH,
    DEFAULT_CHASE_KEYWORDS,
    DEFAULT_CHASE_SENDERS,
    DEFAULT_MESSAGES_DB,
    ensure_data_dirs,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    ensure_data_dirs()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    source_message_id TEXT UNIQUE,
    source_guid TEXT,
    source_sender TEXT,
    source_date_raw TEXT,
    transaction_datetime TEXT,
    month TEXT,
    amount REAL NOT NULL,
    tip_percent REAL,
    tip_amount REAL NOT NULL DEFAULT 0,
    merchant TEXT,
    card_last4 TEXT,
    account_name TEXT,
    status TEXT NOT NULL DEFAULT 'Review',
    joint_amount REAL NOT NULL DEFAULT 0,
    note TEXT,
    raw_text TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    card_last4 TEXT UNIQUE,
    account_name TEXT NOT NULL,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    match_text TEXT NOT NULL,
    match_field TEXT DEFAULT 'merchant_or_raw',
    default_status TEXT NOT NULL,
    default_joint_percent REAL,
    default_joint_amount REAL,
    priority INTEGER DEFAULT 100,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    messages_seen INTEGER DEFAULT 0,
    messages_imported INTEGER DEFAULT 0,
    error TEXT
);
"""


DEFAULT_RULES = [
    ("Sample Grocery", "merchant_or_raw", "Joint", None, None, 100, 1),
    ("Sample Utility", "merchant_or_raw", "Joint", None, None, 100, 1),
    ("Sample Game Store", "merchant_or_raw", "Personal", None, None, 100, 1),
    ("Sample Online Shop", "merchant_or_raw", "Review", None, None, 100, 1),
    ("Sample Warehouse", "merchant_or_raw", "Review", None, None, 100, 1),
]


def init_db(db_path: Path = DB_PATH) -> None:
    ensure_data_dirs()
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_transactions(conn)
        defaults: dict[str, Any] = {
            "messages_db_path": str(DEFAULT_MESSAGES_DB),
            "chase_sender_filters": ",".join(DEFAULT_CHASE_SENDERS),
            "chase_keyword_filters": ",".join(DEFAULT_CHASE_KEYWORDS),
            "polling_interval": "manual",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        for rule in DEFAULT_RULES:
            conn.execute(
                """
                INSERT INTO rules (
                    match_text, match_field, default_status, default_joint_percent,
                    default_joint_amount, priority, active
                )
                SELECT ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM rules WHERE lower(match_text) = lower(?)
                )
                """,
                (*rule, rule[0]),
            )


def _migrate_transactions(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "tip_percent" not in columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN tip_percent REAL")
    if "tip_amount" not in columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN tip_amount REAL NOT NULL DEFAULT 0")


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row and row["value"] is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
