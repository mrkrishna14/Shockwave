"""Forwards the raw charge payload to an analytics queue (pass-through)."""

from storefront.pricing import fetch_charge


def export_charge(charge_id: str, queue: list) -> None:
    charge = fetch_charge(charge_id)
    queue.append({"type": "charge", "payload": charge})
