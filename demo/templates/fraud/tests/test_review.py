from __future__ import annotations

from pathlib import Path

import pytest

from fraud_service.review import review_payment
from fraud_service.stripe_setup import STRIPE_API_VERSION, configure_stripe
from fraud_service.velocity import exceeds_velocity
from tests.stripe_fixture_server import serve_fixtures

FIXTURES = Path(__file__).parent / "fixtures" / "payment_intents.json"


@pytest.fixture
def stripe_fixtures(monkeypatch):
    with serve_fixtures(FIXTURES) as server:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_offline_fixture")
        monkeypatch.setenv("STRIPE_API_BASE", server.base_url)
        yield server


@pytest.mark.parametrize(
    ("payment_intent_id", "needs_review"),
    [
        ("pi_demo_4000usd", False),  # below $5,000
        ("pi_demo_5000usd", False),  # exactly $5,000 is not above the limit
        ("pi_demo_6000usd", True),  # above $5,000
    ],
)
def test_payments_above_5000_dollars_need_review(stripe_fixtures, payment_intent_id, needs_review):
    decision = review_payment(payment_intent_id)
    assert decision.needs_manual_review is needs_review
    request = stripe_fixtures.requests[-1]
    assert (request.method, request.path) == ("GET", f"/v1/payment_intents/{payment_intent_id}")
    assert request.stripe_version == STRIPE_API_VERSION
    assert request.authorization == "Bearer sk_test_offline_fixture"


def test_velocity_limit_counts_payments_not_money():
    assert exceeds_velocity(6) is True
    assert exceeds_velocity(5) is False


def test_live_mode_keys_are_refused():
    with pytest.raises(RuntimeError, match="live-mode"):
        configure_stripe({"STRIPE_SECRET_KEY": "sk_live_not_allowed"})
