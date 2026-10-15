import sqlite3
from joint_expense_tracker.models import ParsedAlert
from joint_expense_tracker.services import insert_alert
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



def test_reused_message_rowid_is_still_imported(conn) -> None:
    first = ParsedAlert(
        10, "Store A", None, "2026-09-01T12:00:00-04:00", "2026-09", "raw", source_message_id="7", source_guid="guid-1"
    )
    same = ParsedAlert(
        10, "Store A", None, "2026-09-01T12:00:00-04:00", "2026-09", "raw", source_message_id="7", source_guid="guid-1"
    )
    # After a Messages database rebuild, a different message can get the same ROWID.
    reused_rowid = ParsedAlert(
        12, "Store B", None, "2026-09-02T12:00:00-04:00", "2026-09", "raw", source_message_id="7", source_guid="guid-2"
    )

    assert insert_alert(conn, first)
    assert not insert_alert(conn, same)
    assert insert_alert(conn, reused_rowid)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2



def credit(amount: float, merchant: str, when: str, guid: str) -> ParsedAlert:
    return ParsedAlert(
        amount,
        merchant,
        None,
        when,
        when[:7],
        f"Chase: A pending credit from {merchant} of ${amount}",
        status="Ignored",
        is_credit=True,
        source_guid=guid,
    )



def purchase(conn, amount: float, merchant: str, when: str, status: str = "Review") -> int:
    return conn.execute(
        "INSERT INTO transactions (transaction_datetime, month, amount, merchant, status, joint_amount, raw_text) "
        "VALUES (?, ?, ?, ?, ?, ?, 'You made a transaction')",
        (when, when[:7], amount, merchant, status, amount if status == "Joint" else 0),
    ).lastrowid



def status_and_note(conn, tx_id: int) -> tuple[str, str | None]:
    return tuple(conn.execute("SELECT status, note FROM transactions WHERE id = ?", (tx_id,)).fetchone())



def test_refund_ignores_unreviewed_purchase(conn) -> None:
    bought = purchase(conn, 23.45, "SP REDRAGONSHOP", "2026-09-13T10:00:00-04:00")
    assert insert_alert(conn, credit(23.45, "SP REDRAGONSHOP", "2026-09-15T07:12:30-04:00", "c1"))
    credit_id = conn.execute("SELECT MAX(id) FROM transactions").fetchone()[0]

    assert status_and_note(conn, bought) == ("Ignored", "Refunded on 2026-09-15")
    assert status_and_note(conn, credit_id) == ("Ignored", "Refund of purchase on 2026-09-13")



def test_refund_of_classified_purchase_is_flagged_not_changed(conn) -> None:
    bought = purchase(conn, 4.99, "SHOPRITE HOBOKEN S1", "2026-08-20T09:00:00-04:00", status="Joint")
    insert_alert(conn, credit(4.99, "SHOPRITE HOBOKEN S1", "2026-08-20T18:13:13-04:00", "c2"))
    credit_id = conn.execute("SELECT MAX(id) FROM transactions").fetchone()[0]

    assert status_and_note(conn, bought) == ("Joint", None)
    status, note = status_and_note(conn, credit_id)
    assert status == "Review"
    assert "Joint purchase on 2026-08-20" in note



def test_refund_without_matching_purchase_stays_ignored(conn) -> None:
    purchase(conn, 99, "OTHER STORE", "2026-09-01T10:00:00-04:00")
    purchase(conn, 23.45, "SP REDRAGONSHOP", "2026-05-01T10:00:00-04:00")  # outside the 60-day window
    insert_alert(conn, credit(23.45, "SP REDRAGONSHOP", "2026-09-15T07:12:30-04:00", "c3"))
    credit_id = conn.execute("SELECT MAX(id) FROM transactions").fetchone()[0]
    assert status_and_note(conn, credit_id) == ("Ignored", None)



def test_import_pairs_refund_when_purchase_is_in_the_same_batch(client, monkeypatch) -> None:
    from joint_expense_tracker import importer

    rows = [
        {
            "message_rowid": 2,
            "guid": "batch-credit",
            "sender": "24273",
            "date_raw": apple_nanoseconds(datetime(2026, 9, 15, 12, tzinfo=UTC)),
            "text": "Chase Freedom Visa: You have a $23.45 pending credit from SAMPLE SHOP.",
        },
        {
            "message_rowid": 1,
            "guid": "batch-purchase",
            "sender": "24273",
            "date_raw": apple_nanoseconds(datetime(2026, 9, 13, 12, tzinfo=UTC)),
            "text": "Chase Freedom Visa: You made a $23.45 transaction with SAMPLE SHOP on Sep 13, 2026 at 8:00 AM ET.",
        },
    ]
    monkeypatch.setattr(importer, "copy_messages_snapshot", lambda _: config.DB_PATH)
    monkeypatch.setattr(importer, "_open_snapshot_readonly", lambda _: sqlite3.connect(":memory:"))
    monkeypatch.setattr(importer, "_candidate_messages", lambda *args: rows)

    result = importer.import_messages()
    assert result.error is None and result.messages_imported == 2
    with get_db(config.DB_PATH) as conn:
        transactions = list(conn.execute("SELECT status, note FROM transactions ORDER BY source_message_id"))
    assert [tx["status"] for tx in transactions] == ["Ignored", "Ignored"]
    assert all(tx["note"] for tx in transactions)

