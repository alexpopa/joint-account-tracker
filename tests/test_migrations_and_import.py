"""Schema migrations, deduplication, refund pairing, and backups."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from joint_expense_tracker import config
from joint_expense_tracker.db import MIGRATIONS, backup_database, get_db, init_db
from joint_expense_tracker.importer import describe_import_error
from joint_expense_tracker.models import ParsedAlert
from joint_expense_tracker.parser import parse_alert
from joint_expense_tracker.services import insert_alert, pair_refund

# The transactions/rules schema as shipped before versioned migrations existed.
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
INSERT INTO rules (match_text, default_status)
VALUES ('Sample Grocery', 'Joint'), ('Steam', 'Personal'), ('Zara', 'Her'), ('Florist', 'Wife');
-- 9:30 PM Eastern on Sep 30, stored in UTC and bucketed into October by the old code.
INSERT INTO transactions (source_message_id, source_guid, transaction_datetime, month, amount, merchant, status, raw_text)
VALUES ('101', 'guid-a', '2026-10-01T01:30:00+00:00', '2026-10', 50, 'Late Dinner', 'Joint', 'raw'),
       ('102', 'guid-b', '2026-09-15T16:00:00+00:00', '2026-09', 20, 'Lunch', 'Personal', 'raw'),
       ('103', 'guid-c', '2026-09-16T16:00:00+00:00', '2026-09', 30, 'Zara', 'Her', 'raw'),
       ('104', 'guid-d', '2026-09-17T16:00:00+00:00', '2026-09', 40, 'Florist', 'Wife', 'raw'),
       ('105', 'guid-e', '2026-05-23T16:00:00+00:00', '2026-05', 28.74, 'Amazon.com', 'Personal', 'raw'),
       ('106', 'guid-f', '2026-05-26T23:00:00+00:00', '2026-05', 28.74, 'Amazon.com', 'Review',
        'Chase: You have a $28.74 pending credit from Amazon.com.'),
       ('107', 'guid-g', '2026-02-06T12:00:00+00:00', '2026-02', 9.72, 'AMAZON MKTPLACE PMTS', 'Review',
        'Chase: You have a $9.72 pending credit from AMAZON MKTPLACE PMTS.');
INSERT INTO transactions (transaction_datetime, month, amount, merchant, status, raw_text)
VALUES ('2026-09-30T12:00:00', '2026-09', 5, 'Manual', 'Review', 'Manually added transaction');
"""


def apple_nanoseconds(moment: datetime) -> int:
    return int((moment - datetime(2001, 1, 1, tzinfo=UTC)).total_seconds() * 1_000_000_000)


def test_alert_month_uses_eastern_time() -> None:
    alert = parse_alert(
        "Chase Sapphire: You made a $50.00 transaction with LATE DINER on Sep 30, 2026 at 9:30 PM ET.",
        message_date_raw=apple_nanoseconds(datetime(2026, 10, 1, 1, 30, tzinfo=UTC)),
    )
    assert alert.month == "2026-09"
    assert alert.transaction_datetime == "2026-09-30T21:30:00-04:00"


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
        assert (rows["Zara"]["status"], rows["Zara"]["subcategory"]) == ("Venmo", None)
        assert (rows["Florist"]["status"], rows["Florist"]["subcategory"]) == ("Personal", "Present")
        refunded = conn.execute("SELECT status, note FROM transactions WHERE source_guid = 'guid-e'").fetchone()
        assert tuple(refunded) == ("Ignored", "Refunded on 2026-05-26 (was Personal)")
        credits = conn.execute("SELECT status FROM transactions WHERE raw_text LIKE '%pending credit%'").fetchall()
        assert [r["status"] for r in credits] == ["Ignored", "Ignored"]
        rules = [tuple(r) for r in conn.execute("SELECT match_text, default_status, default_subcategory FROM rules")]
        assert rules == [("Steam", "Personal", None), ("Zara", "Venmo", None), ("Florist", "Personal", "Present")]
        assert conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'polling_interval'").fetchone()[0] == 0
        names = [r["name"] for r in conn.execute("SELECT name FROM personal_subcategories")]
        assert "Food" not in names and "Groceries" in names
    assert list(config.BACKUP_DIR.glob(f"*-before-v{len(MIGRATIONS)}.sqlite")), "migrations must back up first"


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


def import_credit(conn, alert: ParsedAlert) -> int:
    credit_id = insert_alert(conn, alert)
    pair_refund(conn, credit_id)
    return credit_id


def status_and_note(conn, tx_id: int) -> tuple[str, str | None]:
    return tuple(conn.execute("SELECT status, note FROM transactions WHERE id = ?", (tx_id,)).fetchone())


def test_refund_ignores_unreviewed_purchase(conn) -> None:
    bought = purchase(conn, 23.45, "SP REDRAGONSHOP", "2026-09-13T10:00:00-04:00")
    credit_id = import_credit(conn, credit(23.45, "SP REDRAGONSHOP", "2026-09-15T07:12:30-04:00", "c1"))

    assert status_and_note(conn, bought) == ("Ignored", "Refunded on 2026-09-15 (was Review)")
    assert status_and_note(conn, credit_id) == ("Ignored", "Refund of purchase on 2026-09-13")


def test_refund_ignores_already_classified_purchase(conn) -> None:
    bought = purchase(conn, 4.99, "SHOPRITE HOBOKEN S1", "2026-08-20T09:00:00-04:00", status="Joint")
    credit_id = import_credit(conn, credit(4.99, "SHOPRITE HOBOKEN S1", "2026-08-20T18:13:13-04:00", "c2"))

    assert status_and_note(conn, bought) == ("Ignored", "Refunded on 2026-08-20 (was Joint)")
    assert conn.execute("SELECT joint_amount FROM transactions WHERE id = ?", (bought,)).fetchone()[0] == 0
    assert status_and_note(conn, credit_id) == ("Ignored", "Refund of purchase on 2026-08-20")


def test_released_hold_followed_by_recharge_is_not_a_refund(conn) -> None:
    older = purchase(conn, 4.99, "SHOPRITE HOBOKEN S1", "2026-07-03T11:41:00-04:00", status="Joint")
    credit_id = insert_alert(conn, credit(4.99, "SHOPRITE HOBOKEN S1", "2026-08-20T18:13:13-04:00", "c4"))
    recharge = purchase(conn, 4.99, "SHOPRITE HOBOKEN S1", "2026-08-20T18:14:02-04:00", status="Joint")

    assert pair_refund(conn, credit_id) is None
    assert status_and_note(conn, older) == ("Joint", None)
    assert status_and_note(conn, recharge) == ("Joint", None)
    assert status_and_note(conn, credit_id) == ("Ignored", "Hold released and re-charged on 2026-08-20")


def test_refund_without_matching_purchase_stays_ignored(conn) -> None:
    purchase(conn, 99, "OTHER STORE", "2026-09-01T10:00:00-04:00")
    purchase(conn, 23.45, "SP REDRAGONSHOP", "2026-05-01T10:00:00-04:00")  # outside the 60-day window
    credit_id = import_credit(conn, credit(23.45, "SP REDRAGONSHOP", "2026-09-15T07:12:30-04:00", "c3"))
    assert status_and_note(conn, credit_id) == ("Ignored", None)


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


def test_import_errors_explain_full_disk_access() -> None:
    message = describe_import_error(PermissionError("[Errno 1] Operation not permitted: 'chat.db'"))
    assert "Full Disk Access" in message
    assert "python" in message.lower()


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


def test_import_from_messages_pairs_refunds_in_the_same_batch(tmp_path) -> None:
    from joint_expense_tracker.importer import import_messages

    for suffix in ("", "-wal", "-shm"):
        Path(f"{config.DB_PATH}{suffix}").unlink(missing_ok=True)
    init_db()
    chat_db = tmp_path / "chat.db"
    messages = sqlite3.connect(chat_db)
    messages.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
                              date INTEGER, is_from_me INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES (1, '24273');
        """
    )
    alerts = [
        (
            "g-buy",
            "Chase Sapphire: You made a $23.45 transaction with SP REDRAGONSHOP on Sep 13, 2026 at 10:00 AM ET.",
            datetime(2026, 9, 13, 14, 0, tzinfo=UTC),
        ),
        (
            "g-credit",
            "Chase Sapphire: You have a $23.45 pending credit from SP REDRAGONSHOP.",
            datetime(2026, 9, 15, 11, 12, tzinfo=UTC),
        ),
    ]
    for rowid, (guid, text, sent) in enumerate(alerts, start=1):
        messages.execute(
            "INSERT INTO message VALUES (?, ?, ?, NULL, ?, 0, 1)", (rowid, guid, text, apple_nanoseconds(sent))
        )
    messages.commit()
    messages.close()
    with get_db() as conn:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'messages_db_path'", (str(chat_db),))

    result = import_messages()

    assert result.error is None and result.messages_imported == 2
    with get_db() as conn:
        statuses = [r["status"] for r in conn.execute("SELECT status FROM transactions ORDER BY transaction_datetime")]
    assert statuses == ["Ignored", "Ignored"]


def test_split_undo_migration_preserves_existing_rows(tmp_path) -> None:
    path = tmp_path / "before-split-undo.sqlite"
    init_db(path)
    with get_db(path) as conn:
        from joint_expense_tracker.services import create_manual_transaction

        create_manual_transaction(conn, "2026-09-12", "Dinner", 100, note="Split $100.00 of $150.00")
        before = [tuple(r) for r in conn.execute("SELECT * FROM transactions")]
        conn.execute("DROP TABLE split_undo")
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) - 1}")
    init_db(path)
    init_db(path)
    with get_db(path) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM transactions")] == before
        assert conn.execute("SELECT COUNT(*) FROM split_undo").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
