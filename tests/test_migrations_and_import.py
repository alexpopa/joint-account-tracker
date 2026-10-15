import sqlite3
from datetime import UTC, datetime
from joint_expense_tracker.parser import parse_alert
from joint_expense_tracker import config
from joint_expense_tracker.db import MIGRATIONS, get_db, init_db
from joint_expense_tracker.db import backup_database


LEGACY_SCHEMA = """
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY, source_message_id TEXT UNIQUE, source_guid TEXT, source_sender TEXT,
    source_date_raw TEXT, transaction_datetime TEXT, month TEXT, amount REAL NOT NULL, merchant TEXT,
    card_last4 TEXT, account_name TEXT, status TEXT NOT NULL DEFAULT 'Review', joint_amount REAL NOT NULL DEFAULT 0,
    note TEXT, raw_text TEXT NOT NULL, created_at TEXT, updated_at TEXT
);
CREATE TABLE rules (
    id INTEGER PRIMARY KEY, match_text TEXT NOT NULL, match_field TEXT DEFAULT 'merchant_or_raw',
    default_status TEXT NOT NULL, default_joint_percent REAL, default_joint_amount REAL,
    priority INTEGER DEFAULT 100, active INTEGER DEFAULT 1
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE personal_subcategories (name TEXT PRIMARY KEY COLLATE NOCASE);
INSERT INTO personal_subcategories VALUES ('Food'), ('Video games');
INSERT INTO settings VALUES ('polling_interval', 'manual');
INSERT INTO rules (match_text, default_status) VALUES ('Sample Grocery', 'Joint'), ('Steam', 'Personal');
-- 9:30 PM Eastern on Sep 30, stored in UTC and bucketed into October by the old code.
INSERT INTO transactions (source_message_id, source_guid, transaction_datetime, month, amount, merchant, status, raw_text)
VALUES ('101', 'guid-a', '2026-10-01T01:30:00+00:00', '2026-10', 50, 'Late Dinner', 'Joint', 'raw'),
       ('102', 'guid-b', '2026-09-15T16:00:00+00:00', '2026-09', 20, 'Lunch', 'Personal', 'raw');
INSERT INTO transactions (transaction_datetime, month, amount, merchant, status, raw_text)
VALUES ('2026-09-30T12:00:00', '2026-09', 5, 'Manual', 'Review', 'Manually added transaction');
"""


def test_daily_backup_is_written_once_and_pruned(db_path, tmp_path, monkeypatch) -> None:
    backups = tmp_path / "backups"
    monkeypatch.setattr("joint_expense_tracker.db.BACKUPS_TO_KEEP", 2)
    for stamp in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (backups / f"joint_expenses-{stamp}.sqlite").parent.mkdir(exist_ok=True)
        (backups / f"joint_expenses-{stamp}.sqlite").write_bytes(b"")

    written = backup_database(db_path, backup_dir=backups)
    assert written is not None and written.stat().st_size > 0
    assert backup_database(db_path, backup_dir=backups) is None
    assert len(list(backups.glob("joint_expenses-????-??-??.sqlite"))) == 2



def test_legacy_database_is_migrated_once(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(LEGACY_SCHEMA)
    legacy.close()

    init_db(path)
    init_db(path)

    with get_db(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        rows = {r["merchant"]: r for r in conn.execute("SELECT * FROM transactions")}
        assert (rows["Late Dinner"]["transaction_datetime"], rows["Late Dinner"]["month"]) == (
            "2026-09-30T21:30:00-04:00",
            "2026-09",
        )
        assert rows["Lunch"]["month"] == "2026-09"
        assert rows["Manual"]["transaction_datetime"] == "2026-09-30T12:00:00"
        assert rows["Late Dinner"]["tip_amount"] == 0 and rows["Late Dinner"]["subcategory"] is None
        assert [r["match_text"] for r in conn.execute("SELECT match_text FROM rules")] == ["Steam"]
        assert conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'polling_interval'").fetchone()[0] == 0
        names = [r["name"] for r in conn.execute("SELECT name FROM personal_subcategories")]
        assert "Food" not in names and "Groceries" in names
    assert list(config.BACKUP_DIR.glob(f"*-before-v{len(MIGRATIONS)}.sqlite")), "migrations must back up first"



def apple_nanoseconds(moment: datetime) -> int:
    return int((moment - datetime(2001, 1, 1, tzinfo=UTC)).total_seconds() * 1_000_000_000)



def test_alert_month_uses_eastern_time() -> None:
    alert = parse_alert(
        "Chase Sapphire: You made a $50.00 transaction with LATE DINER on Sep 30, 2026 at 9:30 PM ET.",
        message_date_raw=apple_nanoseconds(datetime(2026, 10, 1, 1, 30, tzinfo=UTC)),
    )
    assert alert.month == "2026-09"
    assert alert.transaction_datetime == "2026-09-30T21:30:00-04:00"

