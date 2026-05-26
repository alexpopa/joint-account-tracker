from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import ParsedAlert

AMOUNT_RE = re.compile(r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?", re.I)
LAST4_RE = re.compile(
    r"(?:ending(?:\s+in)?|card(?:\s+ending(?:\s+in)?)?|account(?:\s+ending(?:\s+in)?)?|acct)\s*(?:x{0,4}|[*]{0,4})\s*(\d{4})",
    re.I,
)

MERCHANT_PATTERNS = [
    re.compile(r"\btransaction\s+with\s+(.+?)(?:\s+on\s+\w{3,9}\s+\d{1,2},?\s+\d{4}|\s+on\s+\d{1,2}/\d{1,2}|\.\s*$|$)", re.I),
    re.compile(r"\btransaction\s+of\s+\$[\d,.]+\s+with\s+(.+?)(?:\s+on\s+\w{3,9}\s+\d{1,2},?\s+\d{4}|\s+on\s+\d{1,2}/\d{1,2}|\.\s*$|$)", re.I),
    re.compile(r"\bat\s+(.+?)(?:\s+on\s+\d{1,2}/\d{1,2}|\s+for\s+\$|\.\s*$|$)", re.I),
    re.compile(r"\bfrom\s+(.+?)(?:\s+on\s+\d{1,2}/\d{1,2}|\s+for\s+\$|\.\s*$|$)", re.I),
    re.compile(r"\bmerchant[:\s]+(.+?)(?:\.|$)", re.I),
]

ACCOUNT_NAME_RE = re.compile(r"^(Chase\s+.+?)(?::\s+)", re.I)


def extract_amount(text: str) -> float | None:
    match = AMOUNT_RE.search(text)
    if not match:
        return None
    dollars = match.group(1).replace(",", "")
    cents = match.group(2) or "00"
    return float(f"{dollars}.{cents}")


def extract_last4(text: str) -> str | None:
    match = LAST4_RE.search(text)
    if match:
        return match.group(1)
    fallback = re.search(r"\b(?:x|[*]){2,}(\d{4})\b", text, re.I)
    return fallback.group(1) if fallback else None


def extract_merchant(text: str) -> str:
    cleaned = " ".join(text.replace("\n", " ").split())
    if re.search(r"\bATM withdrawal\b", cleaned, re.I):
        return "ATM withdrawal"
    for pattern in MERCHANT_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            merchant = match.group(1)
            merchant = re.split(r"\s+(?:with|using|ending|card ending|was charged)\b", merchant, flags=re.I)[0]
            merchant = AMOUNT_RE.sub("", merchant)
            merchant = merchant.strip(" .,-:")
            if merchant and len(merchant) <= 80:
                return merchant
    return "Unknown"


def extract_account_name(text: str) -> str | None:
    cleaned = " ".join(text.replace("\n", " ").split())
    match = ACCOUNT_NAME_RE.search(cleaned)
    if not match:
        return None
    return match.group(1).strip()


def apple_timestamp_to_datetime(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None

    apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    candidates: list[datetime] = []
    for divisor in (1_000_000_000, 1_000_000, 1_000, 1):
        try:
            candidates.append(apple_epoch + timedelta(seconds=value / divisor))
        except OverflowError:
            continue
    for divisor in (1_000_000_000, 1_000_000, 1_000, 1):
        try:
            candidates.append(datetime.fromtimestamp(value / divisor, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            continue

    now = datetime.now(timezone.utc)
    plausible = [dt for dt in candidates if datetime(1998, 1, 1, tzinfo=timezone.utc) <= dt <= now + timedelta(days=2)]
    if plausible:
        return max(plausible)
    return None


def month_from_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def parse_alert(
    text: str,
    *,
    message_date_raw: object | None = None,
    source_message_id: str | None = None,
        source_guid: str | None = None,
    source_sender: str | None = None,
) -> ParsedAlert | None:
    amount = extract_amount(text)
    if amount is None:
        return None
    dt = apple_timestamp_to_datetime(message_date_raw) or datetime.now(timezone.utc)
    return ParsedAlert(
        amount=amount,
        merchant=extract_merchant(text),
        card_last4=extract_last4(text),
        transaction_datetime=dt.isoformat(timespec="seconds"),
        month=month_from_datetime(dt),
        raw_text=text,
        source_message_id=source_message_id,
        source_guid=source_guid,
        source_sender=source_sender,
        source_date_raw=str(message_date_raw) if message_date_raw is not None else None,
        account_name=extract_account_name(text),
    )
