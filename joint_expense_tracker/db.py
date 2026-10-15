from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    BACKUP_DIR,
    BACKUPS_TO_KEEP,
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
        for table, column in [("transactions", "subcategory"), ("rules", "default_subcategory")]:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        conn.execute("CREATE TABLE IF NOT EXISTS personal_subcategories (name TEXT PRIMARY KEY COLLATE NOCASE)")
        conn.executemany(
            "INSERT OR IGNORE INTO personal_subcategories (name) VALUES (?)", [("Food",), ("Video games",)]
        )
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
        conn.commit()
        _run_migrations(conn, db_path)


MIGRATIONS = []


def _run_migrations(conn: sqlite3.Connection, db_path: Path) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(MIGRATIONS):
        return
    has_data = conn.execute("SELECT EXISTS (SELECT 1 FROM transactions)").fetchone()[0]
    if has_data:
        backup_database(db_path, label=f"before-v{len(MIGRATIONS)}")
    for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN")
        try:
            migration(conn)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def backup_database(db_path: Path = DB_PATH, *, label: str | None = None, backup_dir: Path = BACKUP_DIR) -> Path | None:
    """Copy the database into backup_dir.

    Without a label this is the daily backup: at most one per day, keeping the newest
    BACKUPS_TO_KEEP. Labeled backups (e.g. before a migration) are always written and never pruned.
    Returns the path written, or None if today's daily backup already exists.
    """
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    target = backup_dir / (f"joint_expenses-{stamp}-{label}.sqlite" if label else f"joint_expenses-{stamp}.sqlite")
    if target.exists() and not label:
        return None
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    if not label:
        daily = sorted(backup_dir.glob("joint_expenses-????-??-??.sqlite"))
        for old in daily[:-BACKUPS_TO_KEEP]:
            old.unlink()
    return target


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


def personal_subcategories(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM personal_subcategories ORDER BY name COLLATE NOCASE")]


def save_subcategory(conn: sqlite3.Connection, status: str, name: str | None) -> str | None:
    if status != "Personal" or not name or not name.strip():
        return None
    name = " ".join(name.split())[:80]
    conn.execute("INSERT OR IGNORE INTO personal_subcategories (name) VALUES (?)", (name,))
    return conn.execute("SELECT name FROM personal_subcategories WHERE name = ?", (name,)).fetchone()["name"]
