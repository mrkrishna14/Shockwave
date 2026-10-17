"""Storefront price display (contract double of Noirit's Python consumer fixture)."""

import os

import requests

DEMO_PAYMENTS_BASE_URL = os.environ.get("DEMO_PAYMENTS_BASE_URL", "http://demo-payments.test")


def fetch_charge(charge_id: str) -> dict:
    response = requests.get(f"{DEMO_PAYMENTS_BASE_URL}/v1/charges/{charge_id}", timeout=5)
    response.raise_for_status()
    return response.json()


def display_charge_price(charge_id: str) -> str:
    charge = fetch_charge(charge_id)
    return f"${charge['amount'] / 100:.2f}"
