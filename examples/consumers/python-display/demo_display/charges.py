"""Displays charges from the fictional demo-payments service (not Stripe).

Assumes the current contract: ``amount`` is in minor units (cents).
"""

DEMO_BASE_URL = "http://demo-payments.test"


def display_amount(response: dict) -> str:
    """Format a demo-payments charge response as USD, e.g. "$10.00"."""
    dollars = response["amount"] / 100
    return f"${dollars:,.2f}"


def fetch_charge_display(charge_id: str) -> str:
    """Thin HTTP integration: GET /v1/charges/{id} and format the amount."""
    import requests  # imported lazily so display tests need no HTTP client

    resp = requests.get(f"{DEMO_BASE_URL}/v1/charges/{charge_id}", timeout=5)
    resp.raise_for_status()
    charge = resp.json()
    return display_amount(charge)
