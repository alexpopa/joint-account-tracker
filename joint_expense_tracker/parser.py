from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import ParsedAlert

AMOUNT_RE = re.compile(r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?", re.I)
LAST4_RE = re.compile(
    r"(?:ending(?:\s+in)?|card(?:\s+ending(?:\s+in)?)?|account(?:\s+ending(?:\s+in)?)?|acct)\s*(?:x{0,4}|[*]{0,4})\s*(\d{4})",
    re.I,
)
DATE_BOUNDARY = r"(?:\s+on\s+\w{3,9}\s+\d{1,2},?\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?\s*ET)?|\s+on\s+\d{1,2}/\d{1,2}|\.\s*$|$)"

MERCHANT_PATTERNS = [
    re.compile(rf"\btransaction\s+with\s+(.+?){DATE_BOUNDARY}", re.I),
    re.compile(rf"\btransaction\s+of\s+\$[\d,.]+\s+with\s+(.+?){DATE_BOUNDARY}", re.I),
    re.compile(rf"\bexternal\s+transfer\s+to\s+(.+?){DATE_BOUNDARY}", re.I),
    re.compile(rf"\bwire\s+transfer\s+to\s+(.+?){DATE_BOUNDARY}", re.I),
    re.compile(rf"\btransfer\s+to\s+(.+?){DATE_BOUNDARY}", re.I),
    re.compile(rf"\bpmt\s+made\s+to\s+(.+?){DATE_BOUNDARY}", re.I),
    re.compile(r"\bpending\s+credit\s+from\s+(.+?)(?:\.\s*(?:[\x00-\x1f]|$)|[\x00-\x1f]|\s{2,}|$)", re.I),
    re.compile(r"\b(?:we(?:'|’)ve|we have)\s+received\s+your\s+\$[\d,.]+\s+payment\b", re.I),
    re.compile(r"\bautomatic\s+stmnt\s+balance\s+payment\s+is\s+scheduled\b", re.I),
    re.compile(rf"\bDirect\s+Deposit\s+posted{DATE_BOUNDARY}", re.I),
    re.compile(rf"\bat\s+(?!\d{{1,2}}:\d{{2}}\s)(.+?)(?:\s+on\s+\w{{3,9}}\s+\d{{1,2}},?\s+\d{{4}}|\s+on\s+\d{{1,2}}/\d{{1,2}}|\s+for\s+\$|\.\s*$|$)", re.I),
    re.compile(rf"\bfrom\s+(.+?)(?:\s+on\s+\w{{3,9}}\s+\d{{1,2}},?\s+\d{{4}}|\s+on\s+\d{{1,2}}/\d{{1,2}}|\s+for\s+\$|\.\s*$|$)", re.I),
    re.compile(r"\bmerchant[:\s]+(.+?)(?:\.|$)", re.I),
]

ACCOUNT_NAME_RE = re.compile(r"(Chase\s+[^:]{1,80}?)(?::\s+)", re.I)
CREDIT_CARD_PAYMENT_RE = re.compile(
    r"\b(?:pmt\s+made\s+to\s+CHASE\s+CARD|external\s+transfer\s+to\s+CHASE\s+CREDIT\s+CRD|payment\s+to\s+CHASE\s+(?:CARD|CREDIT)|(?:we(?:'|’)ve|we have)\s+received\s+your\s+\$[\d,.]+\s+payment|automatic\s+stmnt\s+balance\s+payment\s+is\s+scheduled)\b",
    re.I,
)
DIRECT_DEPOSIT_RE = re.compile(r"\bDirect\s+Deposit\s+posted\b", re.I)
PENDING_CREDIT_RE = re.compile(r"\bpending\s+credit\s+from\b", re.I)
ALERT_SIGNAL_RE = re.compile(
    r"\b(?:Chase|Purchase\s+alert|transaction\s+(?:with|of)|was\s+charged|card\s+ending|external\s+transfer|wire\s+transfer|ATM\s+withdrawal|Direct\s+Deposit|pmt\s+made|pending\s+credit|received\s+your\s+\$[\d,.]+\s+payment|stmnt\s+balance\s+payment)\b",
    re.I,
)


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
    if DIRECT_DEPOSIT_RE.search(cleaned):
        return "Direct Deposit"
    if re.search(r"\bATM withdrawal\b", cleaned, re.I):
        return "ATM withdrawal"
    for pattern in MERCHANT_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            if not match.groups():
                if re.search(r"\breceived\s+your\s+\$[\d,.]+\s+payment\b", cleaned, re.I):
                    merchant = "Payment received"
                elif re.search(r"\bautomatic\s+stmnt\s+balance\s+payment\s+is\s+scheduled\b", cleaned, re.I):
                    merchant = "Statement balance payment"
                else:
                    merchant = "Direct Deposit"
            else:
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


def default_status_for_alert(text: str) -> str:
    cleaned = " ".join(text.replace("\n", " ").split())
    if CREDIT_CARD_PAYMENT_RE.search(cleaned) or DIRECT_DEPOSIT_RE.search(cleaned) or PENDING_CREDIT_RE.search(cleaned):
        return "Ignored"
    return "Review"


def looks_like_financial_alert(text: str) -> bool:
    return ALERT_SIGNAL_RE.search(text) is not None


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
    if not looks_like_financial_alert(text):
        return None
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
        status=default_status_for_alert(text),
    )
