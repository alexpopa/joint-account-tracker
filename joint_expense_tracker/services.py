from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DB_PATH, EXPORT_DIR, PRESENT, STATUSES, ensure_data_dirs
from .db import current_month, get_db, rows_to_dicts, save_category, utc_now
from .models import ParsedAlert
from .parser import extract_account_name
from .rules import (
    apply_rules,
    joint_amount_for_status,
    normalize_status,
    tip_amount_from_percent,
    tip_percent_from_amount,
)

UNCATEGORIZED = "Uncategorized"
REFUND_WINDOW_DAYS = 60

# How much of a transaction is joint vs. the owner's own spending. Venmo is neither:
# it's paid back by the partner. Personal spending includes tips and the non-joint part of a Split.
JOINT_SPEND_SQL = "CASE WHEN status IN ('Joint', 'Split') THEN joint_amount ELSE 0 END"
PERSONAL_SPEND_SQL = """CASE
    WHEN status = 'Personal' THEN amount + tip_amount
    WHEN status = 'Split' THEN MAX(amount + tip_amount - joint_amount, 0)
    ELSE 0 END"""
SPENDING_STATUSES_SQL = "('Joint', 'Split', 'Personal')"


def resolve_status(status: str, category: str | None) -> tuple[str, str | None]:
    """Expand the "Present" shortcut to Personal + the Present category; validate everything else."""
    if status == PRESENT:
        return "Personal", PRESENT
    return normalize_status(status), category


def account_name_for(conn: sqlite3.Connection, card_last4: str | None) -> str | None:
    if not card_last4:
        return None
    row = conn.execute(
        "SELECT account_name FROM accounts WHERE card_last4 = ? AND active = 1",
        (card_last4,),
    ).fetchone()
    return str(row["account_name"]) if row else None


def _already_imported(conn: sqlite3.Connection, alert: ParsedAlert) -> bool:
    if alert.source_guid:
        query, key = "SELECT 1 FROM transactions WHERE source_guid = ?", alert.source_guid
    elif alert.source_message_id:
        query, key = (
            "SELECT 1 FROM transactions WHERE source_guid IS NULL AND source_message_id = ?",
            alert.source_message_id,
        )
    else:
        return False
    return conn.execute(query, (key,)).fetchone() is not None


def insert_alert(conn: sqlite3.Connection, alert: ParsedAlert) -> int | None:
    """Insert a parsed alert unless it was already imported. Returns the new row id, or None.

    Credits are not paired here: call pair_refund once the whole batch is inserted, so a
    re-charge that arrives right after a credit is visible (see pair_refund).
    """
    if _already_imported(conn, alert):
        return None
    alert.account_name = account_name_for(conn, alert.card_last4) or alert.account_name
    # Card payments, deposits, and credits stay Ignored; rules only classify purchases.
    if alert.status != "Ignored":
        alert = apply_rules(conn, alert)
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO transactions (
            source_message_id, source_guid, source_sender, source_date_raw,
            transaction_datetime, month, amount, merchant, card_last4,
            account_name, status, joint_amount, raw_text, created_at, updated_at, subcategory
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert.source_message_id,
            alert.source_guid,
            alert.source_sender,
            alert.source_date_raw,
            alert.transaction_datetime,
            alert.month,
            alert.amount,
            alert.merchant,
            alert.card_last4,
            alert.account_name,
            alert.status,
            round(alert.joint_amount, 2),
            alert.raw_text,
            now,
            now,
            save_category(conn, alert.status, alert.subcategory),
        ),
    )
    return int(cursor.lastrowid)


def _append_note(note: str | None, text: str) -> str:
    return f"{note} · {text}" if note else text


def pair_refund(conn: sqlite3.Connection, credit_id: int) -> int | None:
    """Match a credit to the purchase it refunds (same merchant and amount, up to 60 days earlier).

    Both are Ignored so the pair nets to zero, whatever the purchase was tagged as. This can
    lower an already-settled month's totals; the purchase's note records what it was.

    A same-merchant, same-amount charge within a day *after* the credit means a card hold
    was released and re-charged, not a refund, so nothing is paired.
    Returns the matched purchase id, if any.
    """
    credit = conn.execute("SELECT * FROM transactions WHERE id = ?", (credit_id,)).fetchone()
    credit_time = datetime.fromisoformat(credit["transaction_datetime"])
    earliest = (credit_time - timedelta(days=REFUND_WINDOW_DAYS)).isoformat(timespec="seconds")
    recharge_by = (credit_time + timedelta(days=1)).isoformat(timespec="seconds")
    recharge = conn.execute(
        """
        SELECT transaction_datetime FROM transactions
        WHERE merchant = ? COLLATE NOCASE AND ROUND(amount, 2) = ROUND(?, 2)
          AND raw_text NOT LIKE '%pending credit from%'
          AND transaction_datetime > ? AND transaction_datetime <= ?
        """,
        (credit["merchant"], credit["amount"], credit["transaction_datetime"], recharge_by),
    ).fetchone()
    if recharge:
        conn.execute(
            "UPDATE transactions SET note = ?, updated_at = ? WHERE id = ?",
            (_append_note(credit["note"], f"Hold released and re-charged on {recharge[0][:10]}"), utc_now(), credit_id),
        )
        return None
    purchase = conn.execute(
        """
        SELECT id, status, transaction_datetime, note FROM transactions
        WHERE id != ?
          AND merchant = ? COLLATE NOCASE
          AND ROUND(amount, 2) = ROUND(?, 2)
          AND status != 'Ignored'
          AND raw_text NOT LIKE '%pending credit from%'
          AND transaction_datetime BETWEEN ? AND ?
        ORDER BY transaction_datetime DESC
        LIMIT 1
        """,
        (credit_id, credit["merchant"], credit["amount"], earliest, credit["transaction_datetime"]),
    ).fetchone()
    if not purchase:
        return None
    purchase_date = purchase["transaction_datetime"][:10]
    credit_date = credit["transaction_datetime"][:10]
    was = purchase["status"]
    conn.execute(
        "UPDATE transactions SET status = 'Ignored', joint_amount = 0, subcategory = NULL, note = ?, updated_at = ? "
        "WHERE id = ?",
        (_append_note(purchase["note"], f"Refunded on {credit_date} (was {was})"), utc_now(), purchase["id"]),
    )
    conn.execute(
        "UPDATE transactions SET status = 'Ignored', note = ?, updated_at = ? WHERE id = ?",
        (_append_note(credit["note"], f"Refund of purchase on {purchase_date}"), utc_now(), credit_id),
    )
    return int(purchase["id"])


def create_manual_transaction(
    conn: sqlite3.Connection,
    transaction_date: str,
    merchant: str,
    amount: float,
    card_last4: str | None = None,
    status: str = "Review",
    joint_amount: float | None = None,
    note: str | None = None,
    category: str | None = None,
) -> int:
    """Create a transaction when there is no imported message to attach to it."""
    status, category = resolve_status(status, category)
    card_last4 = (card_last4 or "").strip() or None
    account_name = account_name_for(conn, card_last4)
    computed_joint_amount = joint_amount_for_status(status, amount, joint_amount)
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO transactions (
            transaction_datetime, month, amount, merchant, card_last4,
            account_name, status, joint_amount, note, raw_text, created_at, updated_at, subcategory
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{transaction_date}T12:00:00",
            transaction_date[:7],
            round(amount, 2),
            merchant.strip(),
            card_last4,
            account_name,
            status,
            round(computed_joint_amount, 2),
            (note or "").strip() or None,
            "Manually added transaction",
            now,
            now,
            save_category(conn, status, category),
        ),
    )
    return int(cursor.lastrowid)


def classify_transaction(
    conn: sqlite3.Connection,
    transaction_id: int,
    status: str,
    joint_amount: float | None = None,
    tip_percent: float | None = None,
    tip_amount: float | None = None,
    category: str | None = None,
) -> None:
    """Quick-classify from a list row.

    `category=None` keeps the current category (if the new status carries one);
    an empty string clears it.
    """
    status, category = resolve_status(status, category)
    row = conn.execute("SELECT amount, subcategory FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if not row:
        return
    amount = float(row["amount"])
    if tip_amount is not None:
        tip_amount = round(max(0.0, tip_amount), 2)
        tip_percent = tip_percent_from_amount(amount, tip_amount)
    elif tip_percent is not None:
        tip_percent = max(0.0, tip_percent)
        tip_amount = tip_amount_from_percent(amount, tip_percent)
    else:
        tip_amount = 0.0
    computed = joint_amount_for_status(status, amount, joint_amount, tip_amount)
    conn.execute(
        """
        UPDATE transactions
        SET status = ?, joint_amount = ?, tip_percent = ?, tip_amount = ?, updated_at = ?, subcategory = ?
        WHERE id = ?
        """,
        (
            status,
            round(computed, 2),
            tip_percent if status != "Ignored" else None,
            round(tip_amount, 2),
            utc_now(),
            save_category(conn, status, row["subcategory"] if category is None else category),
            transaction_id,
        ),
    )


def _tip_values_from_edit(amount: float, data: dict[str, Any]) -> tuple[float | None, float]:
    raw_tip_amount = str(data.get("tip_amount") or "").strip()
    raw_tip_percent = str(data.get("tip_percent") or "").strip()
    if raw_tip_amount:
        tip_amount = max(0.0, float(raw_tip_amount))
        return tip_percent_from_amount(amount, tip_amount), round(tip_amount, 2)
    if raw_tip_percent:
        tip_percent = max(0.0, float(raw_tip_percent))
        return tip_percent, tip_amount_from_percent(amount, tip_percent)
    return None, 0.0


def update_transaction(conn: sqlite3.Connection, transaction_id: int, data: dict[str, Any]) -> None:
    status, category = resolve_status(str(data.get("status", "Review")), data.get("category"))
    current = conn.execute(
        "SELECT amount, account_name, raw_text, subcategory FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    if not current:
        return
    amount = float(current["amount"])
    tip_percent, tip_amount = _tip_values_from_edit(amount, data)
    joint_amount = joint_amount_for_status(status, amount, float(data.get("joint_amount") or 0), tip_amount)
    card_last4 = data.get("card_last4") or None
    account_name = (data.get("account_name") or "").strip() or account_name_for(conn, card_last4)
    if not account_name:
        account_name = current["account_name"] or extract_account_name(current["raw_text"] or "")
    conn.execute(
        """
        UPDATE transactions
        SET merchant = ?, card_last4 = ?, account_name = ?, status = ?, joint_amount = ?,
            tip_percent = ?, tip_amount = ?,
            note = ?, updated_at = ?, subcategory = ?
        WHERE id = ?
        """,
        (
            data.get("merchant") or "Unknown",
            card_last4,
            account_name,
            status,
            round(joint_amount, 2),
            tip_percent,
            round(tip_amount, 2),
            data.get("note") or None,
            utc_now(),
            save_category(conn, status, current["subcategory"] if category is None else category),
            transaction_id,
        ),
    )


def months(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT month FROM transactions ORDER BY month DESC").fetchall()
    return [str(row["month"]) for row in rows]


def normalize_month(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except TypeError, ValueError:
        return None
    return parsed.strftime("%Y-%m")


def month_shift(month: str, offset: int) -> str:
    year, month_number = (int(part) for part in month.split("-"))
    absolute_month = year * 12 + month_number - 1 + offset
    shifted_year, shifted_month = divmod(absolute_month, 12)
    return f"{shifted_year:04d}-{shifted_month + 1:02d}"


def month_label(month: str, fmt: str = "%b %Y") -> str:
    return datetime.strptime(month, "%Y-%m").strftime(fmt)


def last_import(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def dashboard_data(conn: sqlite3.Connection, month: str | None = None) -> dict[str, Any]:
    all_months = months(conn)
    selected = normalize_month(month) or (all_months[0] if all_months else current_month())
    params = (selected,)
    review_limit = 15
    totals_by_account = rows_to_dicts(
        conn.execute(
            """
            SELECT COALESCE(account_name, card_last4, 'Unknown') AS label,
                   COALESCE(card_last4, '') AS card_last4,
                   ROUND(SUM(CASE WHEN status IN ('Joint', 'Split') THEN joint_amount ELSE 0 END), 2) AS total
            FROM transactions
            WHERE month = ? AND status != 'Ignored'
            GROUP BY label, card_last4
            ORDER BY total DESC
            """,
            params,
        ).fetchall()
    )
    totals_by_status = rows_to_dicts(
        conn.execute(
            """
            SELECT status, COUNT(*) AS count,
                   ROUND(SUM(CASE WHEN status IN ('Joint', 'Split', 'Venmo') THEN joint_amount ELSE amount + tip_amount END), 2) AS total
            FROM transactions
            WHERE month = ? AND status != 'Ignored'
            GROUP BY status
            ORDER BY status
            """,
            params,
        ).fetchall()
    )
    needs_review_count = review_count(conn, selected)
    review = rows_to_dicts(
        conn.execute(
            """
            SELECT * FROM transactions
            WHERE month = ? AND status = 'Review'
            ORDER BY transaction_datetime DESC
            LIMIT ?
            """,
            (*params, review_limit),
        ).fetchall()
    )
    tags = last_tags(conn, [item["merchant"] for item in review])
    for item in review:
        item["suggestion"] = tags.get((item["merchant"] or "").lower())
    totals = conn.execute(
        f"""
        SELECT ROUND(COALESCE(SUM({JOINT_SPEND_SQL}), 0), 2) AS joint,
               ROUND(COALESCE(SUM(CASE WHEN status = 'Venmo' THEN joint_amount ELSE 0 END), 0), 2) AS venmo,
               ROUND(COALESCE(SUM({PERSONAL_SPEND_SQL}), 0), 2) AS personal,
               ROUND(COALESCE(SUM(CASE WHEN status = 'Personal' AND subcategory = ? COLLATE NOCASE THEN amount + tip_amount ELSE 0 END), 0), 2) AS present
        FROM transactions
        WHERE month = ?
        """,
        (PRESENT, *params),
    ).fetchone()
    return {
        "months": all_months,
        "selected_month": selected,
        "total_joint": totals["joint"],
        "total_venmo": totals["venmo"],
        "total_personal": totals["personal"],
        "total_present": totals["present"],
        "totals_by_account": totals_by_account,
        "totals_by_status": totals_by_status,
        "needs_review": review,
        "needs_review_count": needs_review_count,
        "needs_review_display_count": len(review),
        "needs_review_limit": review_limit,
        "categories": popular_categories(conn),
        "statuses": STATUSES,
        "last_import": last_import(conn),
    }


def review_count(conn: sqlite3.Connection, month: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM transactions WHERE month = ? AND status = 'Review'", (month,)).fetchone()
    return int(row[0])


def last_tags(conn: sqlite3.Connection, merchants: list[str]) -> dict[str, dict[str, Any]]:
    """The most recent Joint/Personal/Venmo tag for each merchant (case-insensitive), keyed by lowercased name.

    Personal + Present comes back as the "Present" shortcut so it can be re-applied in one tap.
    Pieces of a split are skipped: they say how one purchase was divided, not what the merchant usually is.
    """
    names = sorted({m.lower() for m in merchants if m})
    if not names:
        return {}
    rows = conn.execute(
        f"""
        SELECT LOWER(merchant) AS merchant, status, subcategory
        FROM transactions
        WHERE status IN ('Joint', 'Personal', 'Venmo') AND LOWER(merchant) IN ({", ".join("?" * len(names))})
          AND COALESCE(note, '') NOT LIKE '%Split $%' AND COALESCE(note, '') NOT LIKE '%Rest of split%'
        ORDER BY transaction_datetime DESC, id DESC
        """,
        names,
    ).fetchall()
    tags: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["merchant"] in tags:
            continue
        status, category = row["status"], row["subcategory"]
        if status == "Personal" and category and category.lower() == PRESENT.lower():
            status, category = PRESENT, None
        tags[row["merchant"]] = {"status": status, "category": category if status != "Venmo" else None}
    return tags


def popular_categories(conn: sqlite3.Connection) -> list[str]:
    """Category names, most used first. Present is left out: it has its own button."""
    rows = conn.execute(
        """
        SELECT c.name FROM personal_subcategories c
        LEFT JOIN transactions t ON t.subcategory = c.name COLLATE NOCASE AND t.status IN ('Joint', 'Split', 'Personal')
        WHERE c.name != ? COLLATE NOCASE
        GROUP BY c.name
        ORDER BY COUNT(t.id) DESC, c.name COLLATE NOCASE
        """,
        (PRESENT,),
    ).fetchall()
    return [row["name"] for row in rows]


def _ranked(amounts: dict[str, float]) -> list[dict[str, Any]]:
    total = sum(amounts.values())
    rows = [
        {"category": name, "amount": round(value, 2), "share": value / total if total else 0}
        for name, value in amounts.items()
        if round(value, 2) > 0
    ]
    return sorted(rows, key=lambda row: (-row["amount"], row["category"]))


def spending_data(
    conn: sqlite3.Connection,
    month: str | None = None,
    category: str | None = None,
    trend_months: int = 12,
) -> dict[str, Any]:
    """Where the money went in one month, plus a joint-vs-personal trend ending that month.

    Categories come from Joint, Split, and Personal rows (presents are the Personal
    "Present" category). With `category`, the trend is limited to that category.
    """
    all_months = months(conn)
    selected = normalize_month(month) or (all_months[0] if all_months else current_month())
    start = month_shift(selected, -(trend_months - 1))
    trend_month_values = [month_shift(start, offset) for offset in range(trend_months)]
    previous = month_shift(selected, -1)

    rows = conn.execute(
        f"""
        SELECT month,
               COALESCE(subcategory, ?) AS category,
               SUM({JOINT_SPEND_SQL}) AS joint,
               SUM({PERSONAL_SPEND_SQL}) AS personal
        FROM transactions
        WHERE month BETWEEN ? AND ? AND status IN {SPENDING_STATUSES_SQL}
        GROUP BY 1, 2
        """,
        (UNCATEGORIZED, start, selected),
    ).fetchall()

    joint_by_category: dict[str, float] = {}
    personal_by_category: dict[str, float] = {}
    overall = {m: {"joint": 0.0, "personal": 0.0} for m in trend_month_values}
    trend = {m: {"joint": 0.0, "personal": 0.0} for m in trend_month_values}
    for row in rows:
        joint, personal = float(row["joint"] or 0), float(row["personal"] or 0)
        overall[row["month"]]["joint"] += joint
        overall[row["month"]]["personal"] += personal
        if row["month"] == selected:
            joint_by_category[row["category"]] = joint_by_category.get(row["category"], 0.0) + joint
            personal_by_category[row["category"]] = personal_by_category.get(row["category"], 0.0) + personal
        if category is None or row["category"].lower() == category.lower():
            trend[row["month"]]["joint"] += joint
            trend[row["month"]]["personal"] += personal

    venmo = conn.execute(
        "SELECT month, ROUND(SUM(joint_amount), 2) AS total FROM transactions "
        "WHERE status = 'Venmo' AND month IN (?, ?) GROUP BY month",
        (selected, previous),
    ).fetchall()
    venmo_by_month = {row["month"]: float(row["total"] or 0) for row in venmo}

    def month_totals(m: str) -> dict[str, float]:
        return {
            "joint": round(overall[m]["joint"], 2),
            "personal": round(overall[m]["personal"], 2),
            "venmo": venmo_by_month.get(m, 0.0),
        }

    totals = month_totals(selected)
    previous_totals = month_totals(previous)
    spent = totals["joint"] + totals["personal"]
    return {
        "months": all_months,
        "selected_month": selected,
        "selected_label": month_label(selected, "%B %Y"),
        "previous_label": month_label(previous, "%b"),
        "category": category,
        "totals": totals,
        "previous_totals": previous_totals,
        "joint_share": totals["joint"] / spent if spent else 0,
        "joint_by_category": _ranked(joint_by_category),
        "personal_by_category": _ranked(personal_by_category),
        "trend": [
            {
                "month": m,
                "label": month_label(m, "%b"),
                "year": m[:4],
                "joint": round(values["joint"], 2),
                "personal": round(values["personal"], 2),
            }
            for m, values in trend.items()
        ],
    }


def export_month(month: str, db_path: Path = DB_PATH, export_dir: Path = EXPORT_DIR) -> tuple[Path, Path]:
    ensure_data_dirs()
    export_dir.mkdir(parents=True, exist_ok=True)
    transaction_path = export_dir / f"{month}_joint_expenses.csv"
    summary_path = export_dir / f"{month}_summary.csv"
    columns = {
        "transaction_datetime": "Date",
        "month": "Month",
        "merchant": "Merchant",
        "amount": "Amount",
        "tip_percent": "Tip Percent",
        "tip_amount": "Tip Amount",
        "card_last4": "Card Last4",
        "account_name": "Account Name",
        "status": "Status",
        "joint_amount": "Joint Amount",
        "note": "Note",
        "raw_text": "Raw Text",
        "subcategory": "Category",
    }
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM transactions WHERE month = ? ORDER BY transaction_datetime ASC",
            (month,),
        ).fetchall()
        with transaction_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns.values())
            writer.writerows([row[column] for column in columns] for row in rows)
        data = dashboard_data(conn, month)
        spending = spending_data(conn, month)
        with summary_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Metric", "Label", "Value"])
            writer.writerow(["total_joint_reimbursement", month, data["total_joint"]])
            writer.writerow(["total_venmo_request", month, data["total_venmo"]])
            writer.writerow(["total_personal_spending", month, data["total_personal"]])
            writer.writerow(["total_present_spending", month, data["total_present"]])
            for row in data["totals_by_account"]:
                writer.writerow(["total_by_account", row["label"], row["total"]])
            for row in data["totals_by_status"]:
                writer.writerow(["total_by_status", row["status"], row["total"]])
            for row in spending["joint_by_category"]:
                writer.writerow(["joint_by_category", row["category"], row["amount"]])
            for row in spending["personal_by_category"]:
                writer.writerow(["personal_by_category", row["category"], row["amount"]])
    return transaction_path, summary_path
