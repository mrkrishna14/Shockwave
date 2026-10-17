"""Velocity limits on how often a customer may pay. Unrelated to Stripe amounts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VelocityLimit:
    window_minutes: int
    amount: int  # maximum number of payments in the window (not money)


DEFAULT_LIMIT = VelocityLimit(window_minutes=60, amount=5)


def exceeds_velocity(payments_in_window: int, limit: VelocityLimit = DEFAULT_LIMIT) -> bool:
    return payments_in_window > limit.amount
