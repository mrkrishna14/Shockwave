"""Department budgets, maintained by finance in the reporting app itself (not from Stripe)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    department: str
    amount: int  # whole dollars, entered by finance


def remaining_budget(budget: Budget, spent_dollars: int) -> int:
    return budget.amount - spent_dollars
