from __future__ import annotations

from joint_expense_tracker.charts import nice_ticks, stacked_columns
from joint_expense_tracker.db import get_db
from joint_expense_tracker.services import create_manual_transaction, spending_data


def seed_month(conn) -> None:
    create_manual_transaction(conn, "2026-09-03", "Trader Joe's", 120, status="Joint", category="Groceries")
    create_manual_transaction(conn, "2026-09-04", "Electric", 80, status="Joint", category="Utilities")
    create_manual_transaction(conn, "2026-09-05", "Steam", 60, status="Personal", category="Video games")
    create_manual_transaction(conn, "2026-09-06", "Bodega", 15, status="Personal")
    create_manual_transaction(conn, "2026-09-07", "Flowers", 40, status="Wife")
    create_manual_transaction(conn, "2026-09-08", "Her shoes", 90, status="Her")
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
    for path in ("/", "/spending", "/transactions", "/accounts", "/rules", "/settings", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert client.get("/analytics", follow_redirects=False).headers["location"] == "/spending"


def test_pages_use_display_labels(client) -> None:
    from joint_expense_tracker import config

    with get_db(config.DB_PATH) as conn:
        seed_month(conn)
        conn.execute("INSERT INTO rules (match_text, default_status) VALUES ('Zara', 'Her')")

    dashboard = client.get("/?month=2026-09").text
    assert "Venmo" in dashboard and "Present" in dashboard
    assert "<td>Her</td>" not in dashboard and "<td>Wife</td>" not in dashboard
    assert "<td>Venmo</td>" in client.get("/rules").text

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

