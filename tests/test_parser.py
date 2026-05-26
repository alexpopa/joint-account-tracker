from __future__ import annotations

from datetime import datetime, timezone

from joint_expense_tracker.parser import extract_account_name, extract_amount, extract_last4, month_from_datetime, parse_alert


def test_extract_amount_dollar_format() -> None:
    assert extract_amount("Chase alert: $1,234.56 was charged") == 1234.56


def test_extract_amount_usd_format() -> None:
    assert extract_amount("Purchase alert: USD 12.34 at TEST STORE") == 12.34


def test_extract_last4_common_formats() -> None:
    assert extract_last4("card ending in 1234 was used") == "1234"
    assert extract_last4("account ending 5678") == "5678"


def test_parse_alert_best_effort() -> None:
    parsed = parse_alert("Chase Alert: $24.10 at Sample Grocery on 05/01 card ending in 1234.")
    assert parsed is not None
    assert parsed.amount == 24.10
    assert parsed.card_last4 == "1234"
    assert parsed.merchant == "Sample Grocery"


def test_parse_chase_transaction_with_merchant() -> None:
    parsed = parse_alert("Chase Sapphire Preferred Visa: You made a $80.18 transaction with SAMPLE RESTAURANT on May 25, 2026 at 8:17 PM ET.")
    assert parsed is not None
    assert parsed.amount == 80.18
    assert parsed.merchant == "SAMPLE RESTAURANT"
    assert parsed.account_name == "Chase Sapphire Preferred Visa"


def test_parse_dot_com_merchant() -> None:
    parsed = parse_alert("Chase Freedom Unlimited Visa: You made a $10.62 transaction with Example.com on May 25, 2026 at 8:47 PM ET.")
    assert parsed is not None
    assert parsed.merchant == "Example.com"


def test_extract_account_name_and_acct_last4() -> None:
    text = "Chase acct 4242: Your $300.00 ATM withdrawal on May 25, 2026 at 1:47 PM ET was more than the $125.00 in your Alerts settings."
    assert extract_last4(text) == "4242"
    assert extract_account_name(text) == "Chase acct 4242"


def test_month_derivation() -> None:
    assert month_from_datetime(datetime(2026, 5, 14, tzinfo=timezone.utc)) == "2026-05"
