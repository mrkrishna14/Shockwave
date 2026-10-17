"""Displays a Stripe charge amount (negative control: a different service)."""

import stripe


def display_stripe_charge(charge_id: str) -> str:
    charge = stripe.Charge.retrieve(charge_id)
    return f"${charge.amount / 100:.2f}"
