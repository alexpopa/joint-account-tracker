from __future__ import annotations

from datetime import datetime, timezone

from joint_expense_tracker.parser import extract_account_name, extract_amount, extract_last4, month_from_datetime, parse_alert


def test_extract_amount_dollar_format() -> None:
    assert extract_amount("Chase alert: $1,234.56 was charged") == 1234.56


def test_extract_amount_usd_format() -> None:
    assert extract_amount("Purchase alert: USD 12.34 at TEST STORE") == 12.34


def test_extract_amount_uncommaed_four_digits() -> None:
    assert extract_amount("Starting at $3858 and changing daily") == 3858.00


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


def test_parse_chase_transaction_with_november_merchant_not_time() -> None:
    parsed = parse_alert("Chase Freedom Unlimited Visa: You made a $33.19 transaction with SHOPRITE HOBOKEN S1 on Nov 28, 2025 at 9:29 PM ET.")
    assert parsed is not None
    assert parsed.merchant == "SHOPRITE HOBOKEN S1"


def test_parse_chase_online_transaction_with_merchant_not_time() -> None:
    parsed = parse_alert("Chase Freedom Visa: You made an online, phone, or mail transaction of $15.14 with PP*GOOGLE YOUTUBE SU on Nov 28, 2025 at 2:00 PM ET.")
    assert parsed is not None
    assert parsed.merchant == "PP*GOOGLE YOUTUBE SU"


def test_parse_chase_external_transfer_recipient_not_time() -> None:
    parsed = parse_alert("Chase acct 6932: Your $1,598.50 external transfer to VENMO on Nov 17, 2025 at 1:41 AM ET")
    assert parsed is not None
    assert parsed.merchant == "VENMO"


def test_parse_chase_credit_card_payment_is_ignored() -> None:
    parsed = parse_alert("Chase acct 6932: The $8,593.44 pmt made to CHASE CARD 5668 on May 4, 2026 at 4:47 PM ET was more than the $125.00 limit in your Alerts settings.")
    assert parsed is not None
    assert parsed.amount == 8593.44
    assert parsed.merchant == "CHASE CARD 5668"
    assert parsed.status == "Ignored"


def test_parse_chase_credit_card_external_transfer_is_ignored() -> None:
    parsed = parse_alert("Chase acct 6932: Your $198.47 external transfer to CHASE CREDIT CRD on May 20, 2026 at 4:24 AM ET was more than the $125.00 in your Alerts settings.")
    assert parsed is not None
    assert parsed.merchant == "CHASE CREDIT CRD"
    assert parsed.status == "Ignored"


def test_parse_chase_received_payment_is_ignored() -> None:
    parsed = parse_alert("Chase Freedom Visa: We've received your $198.47 payment.")
    assert parsed is not None
    assert parsed.merchant == "Payment received"
    assert parsed.status == "Ignored"


def test_parse_chase_scheduled_statement_payment_is_ignored() -> None:
    parsed = parse_alert("Chase Sapphire Preferred Visa: Your automatic stmnt balance payment is scheduled. Min due: $72.00 Statement bal: $6,564.17.")
    assert parsed is not None
    assert parsed.amount == 72.00
    assert parsed.merchant == "Statement balance payment"
    assert parsed.status == "Ignored"


def test_parse_chase_pending_credit_merchant_is_ignored() -> None:
    parsed = parse_alert("Chase Freedom Unlimited Visa: You have a $62.32 pending credit from ZOLA.COM*REGISTRY.\x02iI\x01")
    assert parsed is not None
    assert parsed.merchant == "ZOLA.COM*REGISTRY"
    assert parsed.status == "Ignored"


def test_parse_chase_wire_transfer_to_merchant() -> None:
    parsed = parse_alert("Chase acct 6932: A $1,040.66 wire transfer to PARK CHATEAU ESTA... on Mar 16, 2026 8:06AM ET was above the limit in your alert settings.")
    assert parsed is not None
    assert parsed.merchant == "PARK CHATEAU ESTA"
    assert parsed.status == "Review"


def test_parse_chase_message_inside_attributed_body_noise() -> None:
    text = "\x04 streamtyped NSMutableAttributedString NSString +8Chase Freedom Visa: We've received your $280.71 payment.\x02iI\x01"
    parsed = parse_alert(text)
    assert parsed is not None
    assert parsed.merchant == "Payment received"
    assert parsed.account_name == "Chase Freedom Visa"
    assert parsed.status == "Ignored"


def test_parse_chase_direct_deposit_is_ignored_not_timestamp_merchant() -> None:
    parsed = parse_alert("Chase acct 6932: Your recent $3,538.70 Direct Deposit posted on May 15, 2026 at 3:54 AM ET was more than the $0.00 limit in your Alerts settings.")
    assert parsed is not None
    assert parsed.merchant == "Direct Deposit"
    assert parsed.status == "Ignored"


def test_parse_rejects_non_financial_text_with_dollars() -> None:
    text = "I will check whether the apartment has a balcony. Home 001-321 is available 2026-05-13, starting at $3858."
    assert parse_alert(text) is None


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
