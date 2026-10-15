from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from joint_expense_tracker.models import ParsedAlert
from joint_expense_tracker.rules import (
    apply_rules,
    joint_amount_for_status,
    tip_amount_from_percent,
    tip_percent_from_amount,
)
from joint_expense_tracker.services import (
    classify_transaction,
    create_manual_transaction,
    dashboard_data,
    export_month,
    insert_alert,
    update_transaction,
)
from joint_expense_tracker.web import classify


def add_transaction(
    conn: sqlite3.Connection, amount: float, status: str = "Review", joint_amount: float = 0, **fields
) -> int:
    values = {
        "transaction_datetime": "2026-05-01T12:00:00-04:00",
        "month": "2026-05",
        "amount": amount,
        "merchant": "Test Restaurant",
        "status": status,
        "joint_amount": joint_amount,
        "raw_text": "raw",
        "created_at": "now",
        "updated_at": "now",
        **fields,
    }
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    return conn.execute(
        f"INSERT INTO transactions ({columns}) VALUES ({placeholders})", list(values.values())
    ).lastrowid


def add_rule(conn: sqlite3.Connection, match_text: str, status: str, category: str | None = None) -> None:
    conn.execute(
        "INSERT INTO rules (match_text, match_field, default_status, priority, active, default_subcategory) "
        "VALUES (?, 'merchant_or_raw', ?, 1, 1, ?)",
        (match_text, status, category),
    )


def edit(conn: sqlite3.Connection, tx_id: int, **data) -> None:
    form = {"merchant": "Test Restaurant", "account_name": "", "card_last4": "", "joint_amount": 0}
    form.update({"tip_percent": "", "tip_amount": "", "note": ""})
    form.update(data)
    update_transaction(conn, tx_id, form)


def row(conn: sqlite3.Connection, tx_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()


def test_quick_joint_allows_blank_tip_percent(monkeypatch) -> None:
    captured = {}

    def fake_classify_transaction(conn, transaction_id, status, joint_amount, tip_percent, tip_amount, category):
        captured.update(
            transaction_id=transaction_id,
            status=status,
            joint_amount=joint_amount,
            tip_percent=tip_percent,
            tip_amount=tip_amount,
            category=category,
        )

    monkeypatch.setattr("joint_expense_tracker.web.classify_transaction", fake_classify_transaction)

    response = classify(
        123, status="Joint", joint_amount=None, tip_percent="", tip_amount="", category=None, return_to="/transactions"
    )

    assert response.status_code == 303
    assert captured == {
        "transaction_id": 123,
        "status": "Joint",
        "joint_amount": None,
        "tip_percent": None,
        "tip_amount": None,
        "category": None,
    }


def test_status_joint_amount_logic() -> None:
    assert joint_amount_for_status("Joint", 30) == 30
    assert joint_amount_for_status("Joint", 30, tip_amount=6) == 36
    assert joint_amount_for_status("Her", 30) == 30
    assert joint_amount_for_status("Her", 30, tip_amount=6) == 36
    assert joint_amount_for_status("Personal", 30) == 0
    assert joint_amount_for_status("Ignored", 30) == 0
    assert joint_amount_for_status("Split", 30, 12.5) == 12.5
    assert joint_amount_for_status("Split", 30, 99) == 30
    assert tip_amount_from_percent(80.18, 20) == 16.04
    assert tip_percent_from_amount(80.18, 16.04) == 20.0


def test_create_manual_transaction_uses_account_and_status_amount(conn) -> None:
    conn.execute("INSERT INTO accounts (card_last4, account_name) VALUES (?, ?)", ("1234", "House Card"))

    tx_id = create_manual_transaction(
        conn, "2026-07-10", "Corner Market", 42.50, card_last4="1234", status="Joint", note="Entered after missed alert"
    )
    tx = row(conn, tx_id)

    assert tx["transaction_datetime"] == "2026-07-10T12:00:00"
    assert tx["month"] == "2026-07"
    assert tx["account_name"] == "House Card"
    assert tx["status"] == "Joint"
    assert tx["joint_amount"] == 42.5
    assert tx["note"] == "Entered after missed alert"


def test_wife_status_is_included_in_personal_total(conn) -> None:
    gift = create_manual_transaction(conn, "2026-07-10", "Gift Shop", 42.50, status="Wife")
    create_manual_transaction(conn, "2026-07-11", "Game Store", 17.50, status="Personal")

    data = dashboard_data(conn, "2026-07")
    totals = {r["status"]: r["total"] for r in data["totals_by_status"]}

    assert row(conn, gift)["joint_amount"] == 0
    assert data["total_personal"] == 60
    assert data["total_present"] == 42.5
    assert totals == {"Personal": 17.5, "Wife": 42.5}


def test_personal_total_includes_tips_and_the_personal_part_of_splits(conn) -> None:
    dinner = add_transaction(conn, 50, month="2026-07")
    classify_transaction(conn, dinner, "Personal", tip_amount=10)
    split = add_transaction(conn, 40, month="2026-07")
    classify_transaction(conn, split, "Split", joint_amount=15)

    data = dashboard_data(conn, "2026-07")

    assert data["total_personal"] == 60 + 25
    assert data["total_joint"] == 15


def test_rules_can_classify_wife_spending(conn) -> None:
    add_rule(conn, "Wife Dinner", "Wife")
    alert = ParsedAlert(
        80, "Wife Dinner", "1234", "2026-07-12T12:00:00-04:00", "2026-07", "Chase alert $80.00 at Wife Dinner"
    )

    assert apply_rules(conn, alert).status == "Wife"


def test_rules_do_not_override_ignored_credits(conn) -> None:
    add_rule(conn, "PSE&G", "Joint")
    credit = ParsedAlert(
        30,
        "PSE&G",
        None,
        "2026-07-12T12:00:00-04:00",
        "2026-07",
        "Chase: A pending credit from PSE&G of $30.00",
        status="Ignored",
        is_credit=True,
        source_guid="g1",
    )

    assert insert_alert(conn, credit)
    assert conn.execute("SELECT status, joint_amount FROM transactions").fetchone()[:] == ("Ignored", 0)


def test_edit_tip_amount_updates_percent_and_joint_amount(conn) -> None:
    tx_id = add_transaction(conn, 80.18)
    edit(conn, tx_id, status="Joint", tip_amount="16.04")
    tx = row(conn, tx_id)
    assert (tx["tip_amount"], tx["tip_percent"], tx["joint_amount"]) == (16.04, 20.0, 96.22)


def test_quick_classify_tip_amount_updates_existing_tip_fields(conn) -> None:
    tx_id = add_transaction(conn, 80.18)
    classify_transaction(conn, tx_id, "Joint", tip_amount=16.04)
    tx = row(conn, tx_id)
    assert (tx["status"], tx["tip_amount"], tx["tip_percent"], tx["joint_amount"]) == ("Joint", 16.04, 20.0, 96.22)


def test_quick_personal_saves_tip_amount_without_joint_amount(conn) -> None:
    tx_id = add_transaction(conn, 50)
    classify_transaction(conn, tx_id, "Personal", tip_amount=10)
    tx = row(conn, tx_id)
    assert (tx["status"], tx["tip_amount"], tx["tip_percent"], tx["joint_amount"]) == ("Personal", 10, 20.0, 0)


def test_quick_her_saves_venmo_amount_with_tip(conn) -> None:
    tx_id = add_transaction(conn, 50)
    classify_transaction(conn, tx_id, "Her", tip_amount=10)
    tx = row(conn, tx_id)
    assert (tx["status"], tx["tip_amount"], tx["tip_percent"], tx["joint_amount"]) == ("Her", 10, 20.0, 60)


def test_edit_tip_does_not_force_joint_status(conn) -> None:
    tx_id = add_transaction(conn, 50)
    edit(conn, tx_id, status="Review", tip_percent="20")
    tx = row(conn, tx_id)
    assert (tx["status"], tx["tip_percent"], tx["tip_amount"], tx["joint_amount"]) == ("Review", 20, 10, 0)


def test_edit_preserves_account_name_when_last4_blank(conn) -> None:
    raw = "Chase Sapphire Preferred Visa: You made a $80.18 transaction with SAMPLE RESTAURANT on May 25, 2026 at 8:17 PM ET."
    tx_id = add_transaction(
        conn,
        80.18,
        "Joint",
        92.18,
        merchant="SAMPLE RESTAURANT",
        account_name="Chase Sapphire Preferred Visa",
        raw_text=raw,
    )
    edit(conn, tx_id, merchant="SAMPLE RESTAURANT", status="Joint", joint_amount=92.18, tip_amount="12")
    assert row(conn, tx_id)["account_name"] == "Chase Sapphire Preferred Visa"


def test_edit_can_set_account_name_without_last4(conn) -> None:
    tx_id = add_transaction(conn, 80.18)
    edit(conn, tx_id, status="Review", account_name="Chase Sapphire Preferred Visa")
    tx = row(conn, tx_id)
    assert tx["card_last4"] is None
    assert tx["account_name"] == "Chase Sapphire Preferred Visa"


def test_dashboard_review_count_is_not_limited_to_preview(conn) -> None:
    for index in range(18):
        add_transaction(
            conn, 10, transaction_datetime=f"2026-05-{index + 1:02d}T12:00:00-04:00", merchant=f"Review {index}"
        )
    data = dashboard_data(conn, "2026-05")

    assert data["needs_review_count"] == 18
    assert data["needs_review_display_count"] == 15
    assert len(data["needs_review"]) == 15


def test_dashboard_status_totals_mix_reimbursement_and_transaction_amounts(conn) -> None:
    add_transaction(conn, 99.63, "Joint", 111.63)
    add_transaction(conn, 25, "Personal")
    add_transaction(conn, 40, "Review")
    add_transaction(conn, 15, "Ignored")
    add_transaction(conn, 70, "Her", 82)

    totals = {r["status"]: r["total"] for r in dashboard_data(conn, "2026-05")["totals_by_status"]}

    assert totals == {"Her": 82.0, "Joint": 111.63, "Personal": 25.0, "Review": 40.0}


def test_dashboard_separates_joint_and_venmo_totals(conn) -> None:
    add_transaction(conn, 100, "Joint", 120, account_name="Card A")
    add_transaction(conn, 40, "Split", 15, account_name="Card A")
    add_transaction(conn, 70, "Her", 82, account_name="Card A")

    data = dashboard_data(conn, "2026-05")

    assert data["total_joint"] == 135.0
    assert data["total_venmo"] == 82.0
    assert data["totals_by_account"][0]["total"] == 135.0


def test_rule_application_joint(conn) -> None:
    add_rule(conn, "Corner Grocery", "Joint", "Groceries")
    alert = ParsedAlert(
        10, "Corner Grocery", "1234", "2026-05-01T12:00:00-04:00", "2026-05", "Chase alert $10.00 at Corner Grocery"
    )
    applied = apply_rules(conn, alert)
    assert (applied.status, applied.joint_amount, applied.subcategory) == ("Joint", 10, "Groceries")


def test_csv_export(conn, db_path: Path, tmp_path: Path) -> None:
    add_rule(conn, "Power Company", "Joint", "Utilities")
    assert insert_alert(
        conn,
        ParsedAlert(
            20,
            "Power Company",
            "1234",
            "2026-05-02T12:00:00-04:00",
            "2026-05",
            "Chase alert $20.00 at Power Company card ending in 1234",
            source_message_id="sample-1",
            source_guid="g-1",
        ),
    )
    conn.commit()

    transaction_csv, summary_csv = export_month("2026-05", db_path=db_path, export_dir=tmp_path / "exports")

    with transaction_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert (rows[0]["Merchant"], rows[0]["Status"], rows[0]["Category"]) == ("Power Company", "Joint", "Utilities")
    assert "Tip Percent" in rows[0]
    assert "Tip Amount" in rows[0]
    with summary_csv.open(newline="", encoding="utf-8") as f:
        summary = list(csv.reader(f))
    assert ["total_joint_reimbursement", "2026-05", "20.0"] in summary
    assert ["joint_by_category", "Utilities", "20.0"] in summary
