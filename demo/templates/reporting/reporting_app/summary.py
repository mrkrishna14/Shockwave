"""Operations summary: how many payments, by currency and status."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from stripe import StripeClient

from reporting_app.stripe_client import create_stripe_client


def summarize_payment_intents(
    payment_intent_ids: Iterable[str], client: StripeClient | None = None
) -> dict[str, dict[str, int]]:
    """{currency: {status: count}} for the given PaymentIntents."""
    client = client or create_stripe_client()
    counts: Counter[tuple[str, str]] = Counter()
    for payment_intent_id in payment_intent_ids:
        payment_intent = client.v1.payment_intents.retrieve(payment_intent_id)
        counts[(payment_intent.currency, payment_intent.status)] += 1
    summary: dict[str, dict[str, int]] = {}
    for (currency, status), count in sorted(counts.items()):
        summary.setdefault(currency, {})[status] = count
    return summary
