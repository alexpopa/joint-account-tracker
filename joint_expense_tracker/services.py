from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

from .config import DB_PATH, EXPORT_DIR, STATUSES, ensure_data_dirs
from .db import get_db, rows_to_dicts, utc_now
from .models import ParsedAlert
from .parser import extract_account_name
from .rules import apply_rules, joint_amount_for_status, normalize_status, tip_amount_from_percent, tip_percent_from_amount


def account_name_for(conn: sqlite3.Connection, card_last4: str | None) -> str | None:
    if not card_last4:
        return None
    row = conn.execute(
        "SELECT account_name FROM accounts WHERE card_last4 = ? AND active = 1",
        (card_last4,),
    ).fetchone()
    return str(row["account_name"]) if row else None


def insert_alert(conn: sqlite3.Connection, alert: ParsedAlert) -> bool:
    alert.account_name = account_name_for(conn, alert.card_last4) or alert.account_name
    alert = apply_rules(conn, alert)
    now = utc_now()
    try:
        conn.execute(
            """
            INSERT INTO transactions (
                source_message_id, source_guid, source_sender, source_date_raw,
                transaction_datetime, month, amount, merchant, card_last4,
                account_name, status, joint_amount, raw_text, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def classify_transaction(
    transaction_id: int,
    status: str,
    joint_amount: float | None = None,
    tip_percent: float | None = None,
) -> None:
    status = normalize_status(status)
    with get_db() as conn:
        row = conn.execute("SELECT amount FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if not row:
            return
        amount = float(row["amount"])
        tip_amount = tip_amount_from_percent(amount, tip_percent) if status == "Joint" else 0.0
        computed = joint_amount_for_status(status, amount, joint_amount, tip_amount)
        conn.execute(
            """
            UPDATE transactions
            SET status = ?, joint_amount = ?, tip_percent = ?, tip_amount = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, round(computed, 2), tip_percent if status == "Joint" else None, round(tip_amount, 2), utc_now(), transaction_id),
        )


def _tip_values_from_edit(amount: float, data: dict[str, Any], status: str) -> tuple[float | None, float]:
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
    status = normalize_status(str(data.get("status", "Review")))
    current = conn.execute("SELECT amount, account_name, raw_text FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if not current:
        return
    amount = float(current["amount"])
    tip_percent, tip_amount = _tip_values_from_edit(amount, data, status)
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
            note = ?, updated_at = ?
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
            transaction_id,
        ),
    )


def months(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT month FROM transactions ORDER BY month DESC").fetchall()
    return [str(row["month"]) for row in rows]


def dashboard_data(conn: sqlite3.Connection, month: str | None = None) -> dict[str, Any]:
    all_months = months(conn)
    selected = month or (all_months[0] if all_months else utc_now()[:7])
    params = (selected,)
    review_limit = 15
    totals_by_account = rows_to_dicts(
        conn.execute(
            """
            SELECT COALESCE(account_name, card_last4, 'Unknown') AS label,
                   COALESCE(card_last4, '') AS card_last4,
                   ROUND(SUM(joint_amount), 2) AS total
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
                   ROUND(SUM(CASE WHEN status IN ('Joint', 'Split') THEN joint_amount ELSE amount END), 2) AS total,
                   ROUND(SUM(joint_amount), 2) AS joint_total
            FROM transactions
            WHERE month = ? AND status != 'Ignored'
            GROUP BY status
            ORDER BY status
            """,
            params,
        ).fetchall()
    )
    needs_review_count = conn.execute(
        "SELECT COUNT(*) AS count FROM transactions WHERE month = ? AND status = 'Review'",
        params,
    ).fetchone()["count"]
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
    total_joint = conn.execute(
        "SELECT ROUND(COALESCE(SUM(joint_amount), 0), 2) AS total FROM transactions WHERE month = ? AND status != 'Ignored'",
        params,
    ).fetchone()["total"]
    return {
        "months": all_months,
        "selected_month": selected,
        "total_joint": total_joint or 0,
        "totals_by_account": totals_by_account,
        "totals_by_status": totals_by_status,
        "needs_review": review,
        "needs_review_count": needs_review_count,
        "needs_review_display_count": len(review),
        "needs_review_limit": review_limit,
        "statuses": STATUSES,
    }


def export_month(month: str, db_path: Path = DB_PATH, export_dir: Path = EXPORT_DIR) -> tuple[Path, Path]:
    ensure_data_dirs()
    export_dir.mkdir(parents=True, exist_ok=True)
    transaction_path = export_dir / f"{month}_joint_expenses.csv"
    summary_path = export_dir / f"{month}_summary.csv"
    with get_db(db_path) as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT transaction_datetime, month, merchant, amount, tip_percent, tip_amount,
                       card_last4, account_name, status, joint_amount, note, raw_text
                FROM transactions
                WHERE month = ?
                ORDER BY transaction_datetime ASC
                """,
                (month,),
            ).fetchall()
        )
        with transaction_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Month", "Merchant", "Amount", "Tip Percent", "Tip Amount", "Card Last4", "Account Name", "Status", "Joint Amount", "Note", "Raw Text"])
            for row in rows:
                writer.writerow(
                    [
                        row["transaction_datetime"],
                        row["month"],
                        row["merchant"],
                        row["amount"],
                        row["tip_percent"],
                        row["tip_amount"],
                        row["card_last4"],
                        row["account_name"],
                        row["status"],
                        row["joint_amount"],
                        row["note"],
                        row["raw_text"],
                    ]
                )
        data = dashboard_data(conn, month)
        with summary_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Metric", "Label", "Value"])
            writer.writerow(["total_joint_reimbursement", month, data["total_joint"]])
            for row in data["totals_by_account"]:
                writer.writerow(["total_by_account", row["label"], row["total"]])
            for row in data["totals_by_status"]:
                writer.writerow(["total_by_status", row["status"], row["joint_total"]])
    return transaction_path, summary_path
