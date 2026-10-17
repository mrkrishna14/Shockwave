"""Manual-review rules for incoming payments (USD only)."""

from __future__ import annotations

from dataclasses import dataclass

import stripe

from fraud_service.stripe_setup import configure_stripe


@dataclass(frozen=True)
class ReviewDecision:
    payment_intent_id: str
    needs_manual_review: bool
    reason: str


def review_payment(payment_intent_id: str) -> ReviewDecision:
    """Flags payments above USD $5,000.00 for manual review."""
    configure_stripe()
    payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    # Stripe reports USD amounts in cents: 500000 is $5,000.00.
    if payment_intent.amount > 500000:
        return ReviewDecision(payment_intent.id, True, "amount above $5,000.00")
    return ReviewDecision(payment_intent.id, False, "within automatic approval limit")
