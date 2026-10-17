"""Stripe client construction for the reporting jobs.

STRIPE_SECRET_KEY must be a test-mode key (sk_test_...). STRIPE_API_BASE is optional and
points the official SDK at another Stripe-compatible base URL, such as a local response
fixture server in tests; when it is unset, the SDK talks to https://api.stripe.com.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from stripe import StripeClient

# Stripe API version these jobs were built and tested against (stripe-python 16.0.0 default).
STRIPE_API_VERSION = "2026-09-30.endive"


def create_stripe_client(env: Mapping[str, str] = os.environ) -> StripeClient:
    api_key = env.get("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set")
    if api_key.startswith(("sk_live_", "rk_live_")):
        raise RuntimeError("refusing a live-mode Stripe key: reporting runs with test-mode keys only")
    api_base = env.get("STRIPE_API_BASE")
    return StripeClient(
        api_key,
        stripe_version=STRIPE_API_VERSION,
        max_network_retries=0,
        base_addresses={"api": api_base} if api_base else None,
    )
