from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedAlert:
    amount: float
    merchant: str
    card_last4: str | None
    transaction_datetime: str
    month: str
    raw_text: str
    source_message_id: str | None = None
    source_guid: str | None = None
    source_sender: str | None = None
    source_date_raw: str | None = None
    account_name: str | None = None
    status: str = "Review"
    joint_amount: float = 0.0
    # Category name; the column is called `subcategory` for historical reasons.
    subcategory: str | None = None
    # True for refunds/credits ("pending credit from ..."), which get paired with their purchase on import.
    is_credit: bool = False
