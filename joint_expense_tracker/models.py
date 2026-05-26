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
