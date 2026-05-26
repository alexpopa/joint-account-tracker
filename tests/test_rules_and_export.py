from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from joint_expense_tracker.db import init_db
from joint_expense_tracker.models import ParsedAlert
from joint_expense_tracker.rules import apply_rules, joint_amount_for_status, tip_amount_from_percent, tip_percent_from_amount
from joint_expense_tracker.services import dashboard_data, export_month, insert_alert, update_transaction
from joint_expense_tracker.web import classify


def test_quick_joint_allows_blank_tip_percent(monkeypatch) -> None:
    captured = {}

    def fake_classify_transaction(transaction_id, status, joint_amount=None, tip_percent=None) -> None:
        captured.update(
            {
                "transaction_id": transaction_id,
                "status": status,
                "joint_amount": joint_amount,
                "tip_percent": tip_percent,
            }
        )

    monkeypatch.setattr("joint_expense_tracker.web.classify_transaction", fake_classify_transaction)

    response = classify(123, status="Joint", joint_amount=None, tip_percent="", return_to="/transactions")

    assert response.status_code == 303
    assert captured == {
        "transaction_id": 123,
        "status": "Joint",
        "joint_amount": None,
        "tip_percent": None,
    }


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


def test_dashboard_review_count_is_not_limited_to_preview(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for index in range(18):
        conn.execute(
            """
            INSERT INTO transactions (
                transaction_datetime, month, amount, merchant, status,
                joint_amount, raw_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"2026-05-{index + 1:02d}T12:00:00+00:00",
                "2026-05",
                10,
                f"Review {index}",
                "Review",
                0,
                "raw",
                "now",
                "now",
            ),
        )
    data = dashboard_data(conn, "2026-05")
    conn.close()

    assert data["needs_review_count"] == 18
    assert data["needs_review_display_count"] == 15
    assert len(data["needs_review"]) == 15


def test_dashboard_status_totals_mix_reimbursement_and_transaction_amounts(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [
        ("2026-05-01T12:00:00+00:00", "2026-05", 99.63, "Joint Store", "Joint", 111.63),
        ("2026-05-02T12:00:00+00:00", "2026-05", 25, "Personal Store", "Personal", 0),
        ("2026-05-03T12:00:00+00:00", "2026-05", 40, "Review Store", "Review", 0),
        ("2026-05-04T12:00:00+00:00", "2026-05", 15, "Ignored Store", "Ignored", 0),
    ]
    conn.executemany(
        """
        INSERT INTO transactions (
            transaction_datetime, month, amount, merchant, status,
            joint_amount, raw_text, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'raw', 'now', 'now')
        """,
        rows,
    )

    totals = {row["status"]: row["total"] for row in dashboard_data(conn, "2026-05")["totals_by_status"]}
    conn.close()

    assert totals == {
        "Joint": 111.63,
        "Personal": 25.0,
        "Review": 40.0,
    }


def test_dashboard_status_table_renders_total() -> None:
    template_dir = Path(__file__).resolve().parents[1] / "joint_expense_tracker" / "templates"
    env = Environment(loader=FileSystemLoader(template_dir), autoescape=select_autoescape())
    env.globals["url_for"] = lambda name, path: f"/static{path}"

    rendered = env.get_template("dashboard.html").render(
        months=["2026-05"],
        selected_month="2026-05",
        total_joint=111.63,
        totals_by_account=[],
        totals_by_status=[
            {"status": "Joint", "count": 3, "total": 111.63, "joint_total": 111.63},
        ],
        needs_review=[],
        needs_review_count=0,
        needs_review_display_count=0,
    )

    assert "Total" in rendered
    assert "$111.63" in rendered


def test_dashboard_status_table_renders_old_server_shape() -> None:
    template_dir = Path(__file__).resolve().parents[1] / "joint_expense_tracker" / "templates"
    env = Environment(loader=FileSystemLoader(template_dir), autoescape=select_autoescape())
    env.globals["url_for"] = lambda name, path: f"/static{path}"

    rendered = env.get_template("dashboard.html").render(
        months=["2026-05"],
        selected_month="2026-05",
        total_joint=111.63,
        totals_by_account=[],
        totals_by_status=[
            {"status": "Ignored", "count": 7, "gross": 30332.67, "joint_total": 0.0},
            {"status": "Joint", "count": 3, "gross": 99.63, "joint_total": 111.63},
            {"status": "Personal", "count": 13, "gross": 578.73, "joint_total": 0.0},
            {"status": "Review", "count": 104, "gross": 14978.58, "joint_total": 0.0},
        ],
        needs_review=[],
        needs_review_count=0,
        needs_review_display_count=0,
    )

    assert "$30332.67" not in rendered
    assert "Ignored" not in rendered
    assert "$111.63" in rendered
    assert "$578.73" in rendered
    assert "$14978.58" in rendered


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
