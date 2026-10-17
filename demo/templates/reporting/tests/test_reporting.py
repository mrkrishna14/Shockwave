from __future__ import annotations

from pathlib import Path

import pytest

from reporting_app.budgets import Budget, remaining_budget
from reporting_app.ledger import ledger_totals, record_payment_intents
from reporting_app.stripe_client import create_stripe_client
from reporting_app.summary import summarize_payment_intents
from tests.stripe_fixture_server import serve_fixtures

FIXTURES = Path(__file__).parent / "fixtures" / "payment_intents.json"
PAYMENT_INTENTS = ["pi_demo_10usd", "pi_demo_4000usd", "pi_demo_5000usd", "pi_demo_6000usd", "pi_demo_25eur"]


@pytest.fixture
def client():
    with serve_fixtures(FIXTURES) as server:
        yield create_stripe_client({"STRIPE_SECRET_KEY": "sk_test_offline_fixture", "STRIPE_API_BASE": server.base_url})


def test_summary_counts_payments_by_currency_and_status(client):
    assert summarize_payment_intents(PAYMENT_INTENTS, client) == {
        "eur": {"requires_capture": 1},
        "usd": {"processing": 1, "succeeded": 3},
    }


def test_ledger_records_stripe_amounts(client, tmp_path):
    db_path = tmp_path / "ledger.sqlite"
    assert record_payment_intents(PAYMENT_INTENTS, db_path, client) == 5
    assert ledger_totals(db_path) == {"eur": 2500, "usd": 1501000}


def test_budgets_are_local_whole_dollars():
    assert remaining_budget(Budget("support", amount=1200), spent_dollars=200) == 1000
