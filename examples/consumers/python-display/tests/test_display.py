"""Native tests for the Python display consumer.

SHOCKWAVE_RESPONSE_FIXTURE selects the demo-payments charge response used by
the regression test. Default: the local old (cents) fixture. Both the old
and proposed fixtures represent $10, so the expectation is always "$10.00".
"""
import json
import os
import sys
import types
from pathlib import Path

from demo_display import display_amount, fetch_charge_display, format_order_fee

HERE = Path(__file__).resolve().parent.parent
FIXTURE = Path(os.environ.get("SHOCKWAVE_RESPONSE_FIXTURE", HERE / "fixtures" / "charge_old.json"))


def test_display_amount_shows_ten_dollars():
    response = json.loads(FIXTURE.read_text())
    assert display_amount(response) == "$10.00"


def test_order_fee_negative_control():
    assert format_order_fee({"amount": 250}) == "$2.50"


def test_fetch_charge_display_uses_demo_endpoint(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return json.loads(FIXTURE.read_text())

    fake = types.ModuleType("requests")
    fake.get = lambda url, timeout=None: calls.append(url) or FakeResponse()
    monkeypatch.setitem(sys.modules, "requests", fake)  # no network
    assert fetch_charge_display("ch_demo") == "$10.00"
    assert calls == ["http://demo-payments.test/v1/charges/ch_demo"]
