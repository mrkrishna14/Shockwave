"""Stripe SDK configuration for the fraud service.

STRIPE_SECRET_KEY must be a test-mode key (sk_test_...). STRIPE_API_BASE is optional and
points the official SDK at another Stripe-compatible base URL, such as a local response
fixture server in tests; when it is unset, the SDK talks to https://api.stripe.com.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import stripe

# Stripe API version this service was built and tested against (stripe-python 16.0.0 default).
STRIPE_API_VERSION = "2026-09-30.endive"


def configure_stripe(env: Mapping[str, str] = os.environ) -> None:
    api_key = env.get("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set")
    if api_key.startswith(("sk_live_", "rk_live_")):
        raise RuntimeError("refusing a live-mode Stripe key: this service runs with test-mode keys only")
    stripe.api_key = api_key
    stripe.api_version = STRIPE_API_VERSION
    stripe.max_network_retries = 0
    stripe.api_base = env.get("STRIPE_API_BASE") or stripe.DEFAULT_API_BASE
