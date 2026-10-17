"""Finance ledger export: copies PaymentIntent amounts into the reporting database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from stripe import StripeClient

from reporting_app.stripe_client import create_stripe_client

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_intent_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL  -- smallest currency unit, as Stripe reports it
)
"""


def record_payment_intents(
    payment_intent_ids: Iterable[str], db_path: Path, client: StripeClient | None = None
) -> int:
    """Upserts each PaymentIntent's amount into the ledger; returns the number of rows written."""
    client = client or create_stripe_client()
    written = 0
    with sqlite3.connect(db_path) as connection:
        connection.execute(SCHEMA)
        for payment_intent_id in payment_intent_ids:
            payment_intent = client.v1.payment_intents.retrieve(payment_intent_id)
            connection.execute(
                "INSERT OR REPLACE INTO payments (payment_intent_id, currency, amount_minor) VALUES (?, ?, ?)",
                (payment_intent.id, payment_intent.currency, payment_intent.amount),
            )
            written += 1
    return written


def ledger_totals(db_path: Path) -> dict[str, int]:
    """Total recorded amount per currency, in the smallest currency unit."""
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT currency, SUM(amount_minor) FROM payments GROUP BY currency ORDER BY currency"
        ).fetchall()
    return {currency: total for currency, total in rows}
