"""Flags large charges for manual review (threshold comparison)."""

from storefront.pricing import fetch_charge

REVIEW_THRESHOLD_CENTS = 50_000


def needs_manual_review(charge_id: str) -> bool:
    charge = fetch_charge(charge_id)
    return charge["amount"] >= REVIEW_THRESHOLD_CENTS
