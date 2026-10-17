"""Displays refund amounts (negative control: a different, unchanged endpoint)."""

import requests

from storefront.pricing import DEMO_PAYMENTS_BASE_URL


def display_refund_amount(refund_id: str) -> str:
    refund = requests.get(f"{DEMO_PAYMENTS_BASE_URL}/v1/refunds/{refund_id}", timeout=5).json()
    return f"${refund['amount'] / 100:.2f}"
