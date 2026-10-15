from __future__ import annotations

from joint_expense_tracker.db import categories, delete_category, save_category
from joint_expense_tracker.models import ParsedAlert
from joint_expense_tracker.services import (
    classify_transaction,
    create_manual_transaction,
    insert_alert,
    update_transaction,
)


def category_of(conn, tx_id: int) -> str | None:
    return conn.execute("SELECT subcategory FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]


def test_category_names_are_normalized_and_reused_case_insensitively(conn) -> None:
    assert save_category(conn, "Personal", "  Board   games  ") == "Board games"
    assert save_category(conn, "Joint", "board GAMES") == "Board games"
    assert categories(conn).count("Board games") == 1


def test_fresh_database_has_starter_categories(conn) -> None:
    assert {"Groceries", "Dining", "Video games"} <= set(categories(conn))
    assert "Food" not in categories(conn)


def test_categories_apply_to_joint_split_and_personal_only(conn) -> None:
    for status in ("Joint", "Split", "Personal"):
        tx = create_manual_transaction(conn, "2026-09-09", "Store", 10, status=status, category="Groceries")
        assert category_of(conn, tx) == "Groceries", status
    for status in ("Wife", "Her", "Review", "Ignored"):
        tx = create_manual_transaction(conn, "2026-09-09", "Store", 10, status=status, category="Groceries")
        assert category_of(conn, tx) is None, status


def test_quick_classify_keeps_sets_or_clears_category(conn) -> None:
    tx = create_manual_transaction(conn, "2026-09-09", "Market", 30, status="Personal", category="Groceries")

    classify_transaction(conn, tx, "Joint")
    assert category_of(conn, tx) == "Groceries"

    classify_transaction(conn, tx, "Joint", category="Household")
    assert category_of(conn, tx) == "Household"

    classify_transaction(conn, tx, "Joint", category="")
    assert category_of(conn, tx) is None

    classify_transaction(conn, tx, "Personal", category="Dining")
    classify_transaction(conn, tx, "Wife")
    assert category_of(conn, tx) is None


def test_edit_sets_new_category(conn) -> None:
    tx = create_manual_transaction(conn, "2026-09-09", "Arcade", 25, status="Personal")
    update_transaction(conn, tx, {"status": "Personal", "merchant": "Arcade", "category": "Hobbies"})
    assert category_of(conn, tx) == "Hobbies"
    assert "Hobbies" in categories(conn)


def test_rule_sets_category_on_joint_import(conn) -> None:
    conn.execute(
        "INSERT INTO rules (match_text, default_status, default_subcategory, priority) VALUES ('TRADER JOE', 'Joint', 'Groceries', 1)"
    )
    assert insert_alert(
        conn, ParsedAlert(60, "TRADER JOE'S", None, "2026-09-09T18:00:00-04:00", "2026-09", "raw", source_guid="g")
    )
    tx = conn.execute("SELECT status, joint_amount, subcategory FROM transactions").fetchone()
    assert tuple(tx) == ("Joint", 60, "Groceries")


def test_delete_category_clears_transactions_and_rules(conn) -> None:
    tx = create_manual_transaction(conn, "2026-09-09", "Steam", 20, status="Personal", category="Video games")
    conn.execute(
        "INSERT INTO rules (match_text, default_status, default_subcategory) VALUES ('Steam', 'Personal', 'Video games')"
    )

    delete_category(conn, "video games")

    assert "Video games" not in categories(conn)
    assert category_of(conn, tx) is None
    assert conn.execute("SELECT default_subcategory FROM rules").fetchone()[0] is None
