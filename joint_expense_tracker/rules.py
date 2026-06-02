from __future__ import annotations

import sqlite3

from .config import STATUSES
from .models import ParsedAlert


def tip_amount_from_percent(amount: float, tip_percent: float | None = None) -> float:
    if tip_percent is None:
        return 0.0
    return max(0.0, round(amount * (float(tip_percent) / 100.0), 2))


def tip_percent_from_amount(amount: float, tip_amount: float | None = None) -> float | None:
    if amount <= 0 or tip_amount is None:
        return None
    return round((max(0.0, float(tip_amount)) / amount) * 100.0, 2)


def joint_amount_for_status(
    status: str,
    amount: float,
    joint_amount: float | None = None,
    tip_amount: float = 0.0,
) -> float:
    if status in {"Joint", "Her"}:
        return amount + max(0.0, tip_amount)
    if status == "Split":
        return max(0.0, min(amount, float(joint_amount or 0.0)))
    return 0.0


def normalize_status(status: str) -> str:
    return status if status in STATUSES else "Review"


def apply_rules(conn: sqlite3.Connection, alert: ParsedAlert) -> ParsedAlert:
    rows = conn.execute(
        """
        SELECT * FROM rules
        WHERE active = 1
        ORDER BY priority ASC, id ASC
        """
    ).fetchall()
    haystacks = {
        "merchant": alert.merchant or "",
        "raw": alert.raw_text or "",
        "merchant_or_raw": f"{alert.merchant or ''}\n{alert.raw_text or ''}",
    }
    for row in rows:
        match_text = str(row["match_text"] or "").lower()
        field = str(row["match_field"] or "merchant_or_raw")
        haystack = haystacks.get(field, haystacks["merchant_or_raw"]).lower()
        if match_text and match_text in haystack:
            status = normalize_status(str(row["default_status"]))
            alert.status = status
            if status in {"Joint", "Her"}:
                alert.joint_amount = alert.amount
            elif status == "Split":
                if row["default_joint_amount"] is not None:
                    alert.joint_amount = joint_amount_for_status(status, alert.amount, float(row["default_joint_amount"]))
                elif row["default_joint_percent"] is not None:
                    alert.joint_amount = joint_amount_for_status(
                        status,
                        alert.amount,
                        alert.amount * float(row["default_joint_percent"]),
                    )
                else:
                    alert.joint_amount = 0.0
            else:
                alert.joint_amount = 0.0
            return alert
    return alert
