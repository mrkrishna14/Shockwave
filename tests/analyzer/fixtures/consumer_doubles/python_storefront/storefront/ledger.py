"""Persists charge amounts for reconciliation (storage sink)."""

import sqlite3

from storefront.pricing import fetch_charge


def record_charge(connection: sqlite3.Connection, charge_id: str) -> None:
    charge = fetch_charge(charge_id)
    connection.execute(
        "INSERT INTO ledger (charge_id, amount_cents) VALUES (?, ?)",
        (charge["id"], charge["amount"]),
    )
