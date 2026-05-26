from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from joint_expense_tracker.db import init_db
from joint_expense_tracker.models import ParsedAlert
from joint_expense_tracker.rules import apply_rules, joint_amount_for_status, tip_amount_from_percent, tip_percent_from_amount
from joint_expense_tracker.services import export_month, insert_alert, update_transaction


def test_status_joint_amount_logic() -> None:
    assert joint_amount_for_status("Joint", 30) == 30
    assert joint_amount_for_status("Joint", 30, tip_amount=6) == 36
    assert joint_amount_for_status("Personal", 30) == 0
    assert joint_amount_for_status("Ignored", 30) == 0
    assert joint_amount_for_status("Split", 30, 12.5) == 12.5
    assert joint_amount_for_status("Split", 30, 99) == 30
    assert tip_amount_from_percent(80.18, 20) == 16.04
    assert tip_percent_from_amount(80.18, 16.04) == 20.0


def test_edit_tip_amount_updates_percent_and_joint_amount(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_datetime, month, amount, merchant, status, joint_amount,
            raw_text, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-05-01T12:00:00+00:00", "2026-05", 80.18, "Test Restaurant", "Review", 0, "raw", "now", "now"),
    )
    tx_id = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    update_transaction(
        conn,
        tx_id,
        {
            "merchant": "Test Restaurant",
            "account_name": "",
            "card_last4": "",
            "status": "Joint",
            "joint_amount": 0,
            "tip_percent": "",
            "tip_amount": "16.04",
            "note": "",
        },
    )
    row = conn.execute("SELECT tip_percent, tip_amount, joint_amount FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    assert row["tip_amount"] == 16.04
    assert row["tip_percent"] == 20.0
    assert row["joint_amount"] == 96.22


def test_edit_tip_does_not_force_joint_status(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_datetime, month, amount, merchant, status, joint_amount,
            raw_text, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-05-01T12:00:00+00:00", "2026-05", 50, "Test Restaurant", "Review", 0, "raw", "now", "now"),
    )
    tx_id = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    update_transaction(
        conn,
        tx_id,
        {
            "merchant": "Test Restaurant",
            "account_name": "",
            "card_last4": "",
            "status": "Review",
            "joint_amount": 0,
            "tip_percent": "20",
            "tip_amount": "",
            "note": "",
        },
    )
    row = conn.execute("SELECT status, tip_percent, tip_amount, joint_amount FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    assert row["status"] == "Review"
    assert row["tip_percent"] == 20
    assert row["tip_amount"] == 10
    assert row["joint_amount"] == 0


def test_edit_preserves_account_name_when_last4_blank(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    raw = "Chase Sapphire Preferred Visa: You made a $80.18 transaction with SAMPLE RESTAURANT on May 25, 2026 at 8:17 PM ET."
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_datetime, month, amount, merchant, account_name, status,
            joint_amount, raw_text, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-05-26T00:18:02+00:00", "2026-05", 80.18, "SAMPLE RESTAURANT", "Chase Sapphire Preferred Visa", "Joint", 92.18, raw, "now", "now"),
    )
    tx_id = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    update_transaction(
        conn,
        tx_id,
        {
            "merchant": "SAMPLE RESTAURANT",
            "account_name": "",
            "card_last4": "",
            "status": "Joint",
            "joint_amount": 92.18,
            "tip_percent": "",
            "tip_amount": "12",
            "note": "",
        },
    )
    row = conn.execute("SELECT account_name FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    assert row["account_name"] == "Chase Sapphire Preferred Visa"


def test_edit_can_set_account_name_without_last4(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_datetime, month, amount, merchant, status,
            joint_amount, raw_text, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-05-26T00:18:02+00:00", "2026-05", 80.18, "SAMPLE RESTAURANT", "Review", 0, "raw", "now", "now"),
    )
    tx_id = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    update_transaction(
        conn,
        tx_id,
        {
            "merchant": "SAMPLE RESTAURANT",
            "account_name": "Chase Sapphire Preferred Visa",
            "card_last4": "",
            "status": "Review",
            "joint_amount": 0,
            "tip_percent": "",
            "tip_amount": "",
            "note": "",
        },
    )
    row = conn.execute("SELECT card_last4, account_name FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    assert row["card_last4"] is None
    assert row["account_name"] == "Chase Sapphire Preferred Visa"


def test_rule_application_joint(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    alert = ParsedAlert(
        amount=10,
        merchant="Sample Grocery",
        card_last4="1234",
        transaction_datetime="2026-05-01T12:00:00+00:00",
        month="2026-05",
        raw_text="Chase alert $10.00 at Sample Grocery card ending in 1234",
    )
    applied = apply_rules(conn, alert)
    conn.close()
    assert applied.status == "Joint"
    assert applied.joint_amount == 10


def test_csv_export(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    export_dir = tmp_path / "exports"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    inserted = insert_alert(
        conn,
        ParsedAlert(
            amount=20,
            merchant="Sample Utility",
            card_last4="1234",
            transaction_datetime="2026-05-02T12:00:00+00:00",
            month="2026-05",
            raw_text="Chase alert $20.00 at Sample Utility card ending in 1234",
            source_message_id="sample-1",
        ),
    )
    conn.commit()
    conn.close()
    assert inserted
    transaction_csv, summary_csv = export_month("2026-05", db_path=db_path, export_dir=export_dir)
    assert transaction_csv.exists()
    assert summary_csv.exists()
    with transaction_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["Merchant"] == "Sample Utility"
    assert rows[0]["Status"] == "Joint"
    assert "Tip Percent" in rows[0]
    assert "Tip Amount" in rows[0]
