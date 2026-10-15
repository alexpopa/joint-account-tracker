from __future__ import annotations

from joint_expense_tracker.charts import nice_ticks, stacked_columns
from joint_expense_tracker.db import get_db
from joint_expense_tracker.services import create_manual_transaction, dashboard_data, spending_data


def seed_month(conn) -> None:
    create_manual_transaction(conn, "2026-09-03", "Trader Joe's", 120, status="Joint", category="Groceries")
    create_manual_transaction(conn, "2026-09-04", "Electric", 80, status="Joint", category="Utilities")
    create_manual_transaction(conn, "2026-09-05", "Steam", 60, status="Personal", category="Video games")
    create_manual_transaction(conn, "2026-09-06", "Bodega", 15, status="Personal")
    create_manual_transaction(conn, "2026-09-07", "Flowers", 40, status="Present")
    create_manual_transaction(conn, "2026-09-08", "Her shoes", 90, status="Venmo")
    create_manual_transaction(conn, "2026-09-09", "Dinner", 100, status="Split", joint_amount=70, category="Dining")
    create_manual_transaction(conn, "2026-09-10", "Unreviewed", 999, status="Review")
    create_manual_transaction(conn, "2026-08-03", "Trader Joe's", 100, status="Joint", category="Groceries")


def test_spending_breaks_down_joint_and_personal_by_category(conn) -> None:
    seed_month(conn)
    data = spending_data(conn, "2026-09")

    assert data["totals"] == {"joint": 270.0, "personal": 145.0, "venmo": 90.0}
    assert [(r["category"], r["amount"]) for r in data["joint_by_category"]] == [
        ("Groceries", 120.0),
        ("Utilities", 80.0),
        ("Dining", 70.0),
    ]
    assert [(r["category"], r["amount"]) for r in data["personal_by_category"]] == [
        ("Video games", 60.0),
        ("Present", 40.0),
        ("Dining", 30.0),
        ("Uncategorized", 15.0),
    ]
    assert data["previous_totals"]["joint"] == 100.0
    assert round(data["joint_share"], 3) == round(270 / 415, 3)


def test_spending_trend_covers_twelve_months_and_filters_by_category(conn) -> None:
    seed_month(conn)

    trend = spending_data(conn, "2026-09")["trend"]
    assert len(trend) == 12 and trend[0]["month"] == "2025-10" and trend[-1]["month"] == "2026-09"
    assert (trend[-2]["joint"], trend[-1]["joint"], trend[-1]["personal"]) == (100.0, 270.0, 145.0)

    groceries = spending_data(conn, "2026-09", category="groceries")
    assert [p["joint"] for p in groceries["trend"][-2:]] == [100.0, 120.0]
    assert groceries["trend"][-1]["personal"] == 0
    assert groceries["totals"]["joint"] == 270.0, "the category filter narrows only the trend"


def test_chart_geometry() -> None:
    assert nice_ticks(0) == [0.0, 1.0]
    assert nice_ticks(950) == [0, 250, 500, 750, 1000]
    chart = stacked_columns([{"joint": 100, "personal": 50}, {"joint": 0, "personal": 0}], ["joint", "personal"])
    full, empty = chart["columns"]
    assert [segment["key"] for segment in full["segments"]] == ["joint", "personal"]
    assert empty["segments"] == []


def test_every_page_renders(client) -> None:
    for path in ("/", "/review", "/spending", "/transactions", "/accounts", "/rules", "/settings", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert client.get("/analytics", follow_redirects=False).headers["location"] == "/spending"


def test_pages_use_display_labels(client) -> None:
    from joint_expense_tracker import config

    with get_db(config.DB_PATH) as conn:
        seed_month(conn)
        conn.execute("INSERT INTO rules (match_text, default_status) VALUES ('Zara', 'Venmo')")

    dashboard = client.get("/?month=2026-09").text
    assert "Venmo" in dashboard and "Present" in dashboard
    assert "Her</td>" not in dashboard and "Wife" not in dashboard
    assert 'data-status="Venmo">Venmo<' in client.get("/rules").text

    spending = client.get("/spending?month=2026-09").text
    assert "Groceries" in spending and "$270.00" in spending
    assert "Groceries · Last 12 months" in client.get("/spending?month=2026-09&category=Groceries").text

    transactions = client.get("/transactions?category=Groceries").text
    assert "Trader Joe&#39;s" in transactions and "Steam" not in transactions


def test_quick_classify_with_category_via_form(client) -> None:
    from joint_expense_tracker import config

    with get_db(config.DB_PATH) as conn:
        tx = create_manual_transaction(conn, "2026-09-03", "Market", 50, status="Review")

    response = client.post(
        f"/transactions/{tx}/classify",
        data={"status": "Joint", "category": "Groceries", "tip_amount": "", "return_to": "/?month=2026-09"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/?month=2026-09"
    with get_db(config.DB_PATH) as conn:
        row = conn.execute("SELECT status, subcategory, joint_amount FROM transactions WHERE id = ?", (tx,)).fetchone()
    assert tuple(row) == ("Joint", "Groceries", 50)


def test_failed_import_is_shown_on_dashboard(client) -> None:
    from joint_expense_tracker import config

    with get_db(config.DB_PATH) as conn:
        conn.execute("UPDATE settings SET value = '/nonexistent/chat.db' WHERE key = 'messages_db_path'")

    page = client.post("/import").text
    assert "Last import failed" in page and "Messages DB path" in page


def test_return_to_cannot_leave_the_site(client) -> None:
    response = client.post(
        "/transactions/1/classify", data={"status": "Joint", "return_to": "//evil.example"}, follow_redirects=False
    )
    assert response.headers["location"] == "/"


def test_review_cards_suggest_the_last_tag_per_merchant(conn) -> None:
    seed_month(conn)
    create_manual_transaction(conn, "2026-09-20", "TRADER JOE'S", 30, status="Review")
    create_manual_transaction(conn, "2026-09-21", "Flowers", 25, status="Review")
    create_manual_transaction(conn, "2026-09-22", "Her shoes", 50, status="Review")

    data = dashboard_data(conn, "2026-09")
    suggestions = {item["merchant"]: item["suggestion"] for item in data["needs_review"]}
    assert suggestions["TRADER JOE'S"] == {"status": "Joint", "category": "Groceries"}
    assert suggestions["Flowers"] == {"status": "Present", "category": None}
    assert suggestions["Her shoes"] == {"status": "Venmo", "category": None}
    assert suggestions["Unreviewed"] is None
    assert data["categories"][0] == "Groceries" and "Present" not in data["categories"]


def test_dashboard_shows_review_cards(client) -> None:
    from joint_expense_tracker import config

    with get_db(config.DB_PATH) as conn:
        seed_month(conn)

    page = client.get("/?month=2026-09").text
    assert 'data-merchant="Unreviewed"' in page and 'name="tip_amount"' in page
    assert client.get("/review?month=2026-09", follow_redirects=False).headers["location"] == "/?month=2026-09#review"


def test_split_cuts_a_purchase_into_two_rows_that_add_up(conn) -> None:
    from joint_expense_tracker.services import split_transaction, unsplit_transaction

    tx = create_manual_transaction(conn, "2026-09-12", "Dinner", 100, status="Review")
    rest = split_transaction(conn, tx, 70, "Joint", "Dining", tip_amount=20)

    rows = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id, status, amount, tip_amount, joint_amount, subcategory, note FROM transactions WHERE id IN (?, ?)",
            (tx, rest),
        )
    }
    assert (rows[tx]["status"], rows[tx]["amount"], rows[tx]["tip_amount"], rows[tx]["joint_amount"]) == (
        "Joint",
        70,
        0,
        70,
    )
    assert rows[tx]["subcategory"] == "Dining" and rows[tx]["note"] == "Split $70.00 of $120.00"
    assert (rows[rest]["status"], rows[rest]["amount"], rows[rest]["joint_amount"]) == ("Review", 50, 0)

    dashboard = dashboard_data(conn, "2026-09")
    assert dashboard["total_joint"] == 70 and dashboard["needs_review_count"] == 1
    assert dashboard["needs_review"][0]["suggestion"] is None, "a split piece isn't a 'last time' for the rest"

    # The rest can be tagged on its own, e.g. as the partner's.
    from joint_expense_tracker.services import classify_transaction

    classify_transaction(conn, rest, "Venmo")
    assert dashboard_data(conn, "2026-09")["total_venmo"] == 50
    classify_transaction(conn, rest, "Review")

    unsplit_transaction(conn, tx, rest, tip_amount=20)
    row = conn.execute("SELECT status, amount, tip_amount, joint_amount, note FROM transactions WHERE id = ?", (tx,))
    assert tuple(row.fetchone()) == ("Review", 100, 0, 0, None)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE id = ?", (rest,)).fetchone()[0] == 0


def test_split_rejects_amounts_outside_the_charge(conn) -> None:
    import pytest

    from joint_expense_tracker.services import split_transaction

    tx = create_manual_transaction(conn, "2026-09-12", "Dinner", 100, status="Review")
    for bad in (0, 100, 130):
        with pytest.raises(ValueError):
            split_transaction(conn, tx, bad, "Joint")
    with pytest.raises(ValueError):
        split_transaction(conn, tx, 50, "Split")


def test_split_from_a_review_card(client) -> None:
    from joint_expense_tracker import config

    with get_db(config.DB_PATH) as conn:
        tx = create_manual_transaction(conn, "2026-09-12", "Dinner", 40, status="Review")

    response = client.post(
        f"/transactions/{tx}/classify",
        data={"status": "Personal", "split_amount": "15", "tip_amount": "8", "return_to": "/"},
        headers={"Accept": "application/json"},
    )
    rest = response.json()["rest_id"]
    with get_db(config.DB_PATH) as conn:
        amounts = dict(
            conn.execute("SELECT status, amount FROM transactions WHERE id IN (?, ?)", (tx, rest)).fetchall()
        )
    assert amounts == {"Personal": 15, "Review": 33}

    too_big = client.post(f"/transactions/{rest}/classify", data={"status": "Joint", "split_amount": "40"})
    assert too_big.status_code == 422
    assert client.post(f"/transactions/{tx}/unsplit", data={"rest_id": rest, "tip_amount": "8"}).json() == {"ok": True}


def test_unsplit_rejects_unrelated_and_mismatched_purchases(client) -> None:
    from joint_expense_tracker import config
    from joint_expense_tracker.services import split_transaction

    with get_db(config.DB_PATH) as conn:
        first = create_manual_transaction(conn, "2026-09-12", "Dinner", 100, status="Review")
        second = create_manual_transaction(conn, "2026-09-12", "Dinner", 100, status="Review")
        # Identical dates, merchants, amounts and alert text still aren't a split.
        before = [tuple(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")]
    for parent, rest in ((first, second), (first, first)):
        assert client.post(f"/transactions/{parent}/unsplit", data={"rest_id": rest}).status_code == 409
    with get_db(config.DB_PATH) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")] == before
        first_rest = split_transaction(conn, first, 40, "Joint")
        second_rest = split_transaction(conn, second, 40, "Joint")
        before = [tuple(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")]
    assert client.post(f"/transactions/{first}/unsplit", data={"rest_id": second_rest}).status_code == 409
    with get_db(config.DB_PATH) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")] == before
    assert client.post(f"/transactions/{first}/unsplit", data={"rest_id": first_rest}).status_code == 200
    assert client.post(f"/transactions/{first}/unsplit", data={"rest_id": first_rest}).status_code == 409


def test_unsplit_uses_saved_amount_and_preserves_note(conn) -> None:
    from joint_expense_tracker.services import split_transaction, unsplit_transaction

    tx = create_manual_transaction(conn, "2026-09-12", "Dinner", 100, note="Birthday")
    rest = split_transaction(conn, tx, 70, "Joint", tip_amount=20)
    unsplit_transaction(conn, tx, rest, tip_amount=999)
    row = conn.execute("SELECT amount, note, status FROM transactions WHERE id = ?", (tx,)).fetchone()
    assert tuple(row) == (100, "Birthday", "Review")
    assert conn.execute("SELECT COUNT(*) FROM split_undo").fetchone()[0] == 0


def test_unsplit_rejects_changed_split_pieces(conn) -> None:
    import pytest

    from joint_expense_tracker.services import split_transaction, unsplit_transaction

    for piece in ("part", "rest"):
        tx = create_manual_transaction(conn, "2026-09-12", "Dinner", 100)
        rest = split_transaction(conn, tx, 70, "Joint")
        changed = tx if piece == "part" else rest
        conn.execute("UPDATE transactions SET note = 'Edited later' WHERE id = ?", (changed,))
        before = [tuple(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")]
        with pytest.raises(ValueError, match="can't be undone"):
            unsplit_transaction(conn, tx, rest)
        assert [tuple(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")] == before
