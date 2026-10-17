"""Negative control: an unrelated ``amount`` that is NOT a demo-payments field.

Order amounts are internal cents values; a migration must leave this alone.
"""


def format_order_fee(order: dict) -> str:
    fee = order["amount"] / 100
    return f"${fee:,.2f}"


def sample_order_fee() -> str:
    charge = {"id": "local", "amount": 250}  # a local dict named charge is not origin evidence
    return format_order_fee(charge)
