from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    APP_TIMEZONE,
    BACKUP_DIR,
    BACKUPS_TO_KEEP,
    CATEGORIZED_STATUSES,
    DB_PATH,
    DEFAULT_CHASE_KEYWORDS,
    DEFAULT_CHASE_SENDERS,
    DEFAULT_MESSAGES_DB,
    PRESENT,
    STARTER_CATEGORIES,
    ensure_data_dirs,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def current_month() -> str:
    return local_now().strftime("%Y-%m")


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


# Current shape of every table. Older databases are brought up to date by
# _ensure_columns() and the numbered MIGRATIONS below.
TRANSACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    source_message_id TEXT,
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
    updated_at TEXT,
    subcategory TEXT
);
"""
TRANSACTION_COLUMNS = [
    "id",
    "source_message_id",
    "source_guid",
    "source_sender",
    "source_date_raw",
    "transaction_datetime",
    "month",
    "amount",
    "tip_percent",
    "tip_amount",
    "merchant",
    "card_last4",
    "account_name",
    "status",
    "joint_amount",
    "note",
    "raw_text",
    "created_at",
    "updated_at",
    "subcategory",
]

SCHEMA = (
    TRANSACTIONS_TABLE.format(name="transactions")
    + """
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
    active INTEGER DEFAULT 1,
    default_subcategory TEXT
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

-- Category names. The table name predates categories applying to Joint/Split spending.
CREATE TABLE IF NOT EXISTS personal_subcategories (name TEXT PRIMARY KEY COLLATE NOCASE);
"""
)

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_source_guid
    ON transactions(source_guid) WHERE source_guid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_transactions_month_status ON transactions(month, status);
"""

DEFAULT_SETTINGS = {
    "messages_db_path": str(DEFAULT_MESSAGES_DB),
    "chase_sender_filters": ",".join(DEFAULT_CHASE_SENDERS),
    "chase_keyword_filters": ",".join(DEFAULT_CHASE_KEYWORDS),
}

# Placeholder rules that older versions re-created on every startup.
LEGACY_SAMPLE_RULES = [
    "Sample Grocery",
    "Sample Utility",
    "Sample Game Store",
    "Sample Online Shop",
    "Sample Warehouse",
]


def init_db(db_path: Path = DB_PATH) -> None:
    ensure_data_dirs()
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        _run_migrations(conn, db_path)
        conn.executescript(INDEXES)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table was first created."""
    added = [
        ("transactions", "tip_percent", "REAL"),
        ("transactions", "tip_amount", "REAL NOT NULL DEFAULT 0"),
        ("transactions", "subcategory", "TEXT"),
        ("rules", "default_subcategory", "TEXT"),
    ]
    for table, column, definition in added:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migration_cleanup_defaults(conn: sqlite3.Connection) -> None:
    """Drop sample rules and the unused polling setting; seed starter categories."""
    conn.executemany(
        "DELETE FROM rules WHERE match_text = ?",
        [(name,) for name in LEGACY_SAMPLE_RULES],
    )
    conn.execute("DELETE FROM settings WHERE key = 'polling_interval'")
    conn.executemany(
        "INSERT OR IGNORE INTO personal_subcategories (name) VALUES (?)",
        [(name,) for name in STARTER_CATEGORIES],
    )
    # "Food" was an old default seed, superseded by Groceries and Dining. Keep it if anything uses it.
    conn.execute(
        """
        DELETE FROM personal_subcategories
        WHERE name = 'Food'
          AND NOT EXISTS (SELECT 1 FROM transactions WHERE subcategory = 'Food' COLLATE NOCASE)
          AND NOT EXISTS (SELECT 1 FROM rules WHERE default_subcategory = 'Food' COLLATE NOCASE)
        """
    )


def _migration_local_time(conn: sqlite3.Connection) -> None:
    """Re-express imported UTC timestamps in Eastern time so late-evening purchases land in the right month."""
    rows = conn.execute(
        "SELECT id, transaction_datetime FROM transactions WHERE transaction_datetime LIKE '%+00:00'"
    ).fetchall()
    for row in rows:
        local = datetime.fromisoformat(row["transaction_datetime"]).astimezone(APP_TIMEZONE)
        conn.execute(
            "UPDATE transactions SET transaction_datetime = ?, month = ? WHERE id = ?",
            (local.isoformat(timespec="seconds"), local.strftime("%Y-%m"), row["id"]),
        )


def _migration_dedupe_by_guid(conn: sqlite3.Connection) -> None:
    """Deduplicate imports by the permanent Messages GUID instead of the reusable ROWID.

    Old databases declared `source_message_id UNIQUE`, which SQLite can only drop by
    rebuilding the table.
    """
    unique_on_rowid = any(
        index["unique"]
        and [info["name"] for info in conn.execute(f"PRAGMA index_info('{index['name']}')")] == ["source_message_id"]
        for index in conn.execute("PRAGMA index_list(transactions)")
    )
    duplicate = conn.execute(
        "SELECT source_guid FROM transactions WHERE source_guid IS NOT NULL GROUP BY source_guid HAVING COUNT(*) > 1"
    ).fetchone()
    if duplicate:
        raise RuntimeError(f"Cannot add unique GUID index: duplicate source_guid {duplicate[0]!r}")
    if not unique_on_rowid:
        return
    columns = ", ".join(TRANSACTION_COLUMNS)
    conn.execute("DROP TABLE IF EXISTS transactions_rebuild")
    conn.execute(TRANSACTIONS_TABLE.format(name="transactions_rebuild"))
    conn.execute(f"INSERT INTO transactions_rebuild ({columns}) SELECT {columns} FROM transactions")
    conn.execute("DROP TABLE transactions")
    conn.execute("ALTER TABLE transactions_rebuild RENAME TO transactions")


def _migration_plain_status_names(conn: sqlite3.Connection) -> None:
    """Store statuses as the UI names them: Her -> Venmo, and Wife -> Personal with the Present category."""
    conn.execute("INSERT OR IGNORE INTO personal_subcategories (name) VALUES (?)", (PRESENT,))
    conn.execute("UPDATE transactions SET status = 'Venmo' WHERE status = 'Her'")
    conn.execute("UPDATE transactions SET status = 'Personal', subcategory = ? WHERE status = 'Wife'", (PRESENT,))
    conn.execute("UPDATE rules SET default_status = 'Venmo' WHERE default_status = 'Her'")
    conn.execute(
        "UPDATE rules SET default_status = 'Personal', default_subcategory = ? WHERE default_status = 'Wife'",
        (PRESENT,),
    )


def _migration_settle_existing_refunds(conn: sqlite3.Connection) -> None:
    """Ignore credits imported before they were auto-ignored, and pair each with its purchase."""
    from .services import pair_refund  # services imports db, so import at call time

    credit_filter = "raw_text LIKE '%pending credit from%'"
    conn.execute(
        f"UPDATE transactions SET status = 'Ignored', joint_amount = 0, subcategory = NULL WHERE {credit_filter}"
    )
    for row in conn.execute(
        f"SELECT id FROM transactions WHERE {credit_filter} ORDER BY transaction_datetime"
    ).fetchall():
        pair_refund(conn, row["id"])


# Each migration runs once, in order; PRAGMA user_version records how many have run.
# Append new migrations to the end. Never edit or reorder ones that have shipped.
MIGRATIONS = [
    _migration_cleanup_defaults,
    _migration_local_time,
    _migration_dedupe_by_guid,
    _migration_plain_status_names,
    _migration_settle_existing_refunds,
]


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
    stamp = local_now().strftime("%Y-%m-%d")
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


def categories(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM personal_subcategories ORDER BY name COLLATE NOCASE")]


def save_category(conn: sqlite3.Connection, status: str, name: str | None) -> str | None:
    """Register a category name (case-insensitively) and return its canonical spelling.

    Returns None when the status doesn't carry categories or no name was given.
    """
    if status not in CATEGORIZED_STATUSES or not name or not name.strip():
        return None
    name = " ".join(name.split())[:80]
    conn.execute("INSERT OR IGNORE INTO personal_subcategories (name) VALUES (?)", (name,))
    return conn.execute("SELECT name FROM personal_subcategories WHERE name = ?", (name,)).fetchone()["name"]


def delete_category(conn: sqlite3.Connection, name: str) -> None:
    """Remove a category and clear it from any transactions and rules that used it."""
    conn.execute("UPDATE transactions SET subcategory = NULL WHERE subcategory = ? COLLATE NOCASE", (name,))
    conn.execute("UPDATE rules SET default_subcategory = NULL WHERE default_subcategory = ? COLLATE NOCASE", (name,))
    conn.execute("DELETE FROM personal_subcategories WHERE name = ?", (name,))
