"""Explicit, tested SDK and HTTP mappings.

Only methods listed here are mapped. A string that merely resembles an SDK
call is never mapped. Stripe mappings describe *outbound client* calls to
Stripe's public API (service_id ``stripe``); they say nothing about a
merchant's own routes. ``demo-payments`` is a fictional, team-controlled
service and is unrelated to Stripe.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SdkOperation:
    service_id: str
    method: str
    path: str  # template, matched against the catalog with params wildcarded
    library: str
    call: str  # canonical display form, e.g. "stripe.PaymentIntent.create"


# Python stripe SDK: qualified symbol -> operation.
PYTHON_SDK_METHODS: dict[str, SdkOperation] = {
    "stripe.PaymentIntent.create": SdkOperation(
        "stripe", "POST", "/v1/payment_intents", "stripe-python", "stripe.PaymentIntent.create"
    ),
    "stripe.PaymentIntent.retrieve": SdkOperation(
        "stripe", "GET", "/v1/payment_intents/{intent}", "stripe-python", "stripe.PaymentIntent.retrieve"
    ),
}

# stripe-node: methods on a client instance (``new Stripe(key)`` / ``require('stripe')(key)``).
JS_SDK_CLIENT_METHODS: dict[str, SdkOperation] = {
    "paymentIntents.create": SdkOperation(
        "stripe", "POST", "/v1/payment_intents", "stripe-node", "stripe.paymentIntents.create"
    ),
    "paymentIntents.retrieve": SdkOperation(
        "stripe", "GET", "/v1/payment_intents/{intent}", "stripe-node", "stripe.paymentIntents.retrieve"
    ),
}

# Package names whose import yields the SDK.
PYTHON_SDK_MODULES = {"stripe"}
JS_SDK_PACKAGES = {"stripe"}

# Fallback base URLs per service when ScanRequest.base_urls is not supplied.
KNOWN_BASE_URLS: dict[str, list[str]] = {
    "demo-payments": ["http://demo-payments.test"],
    "stripe": ["https://api.stripe.com"],
}

# Python HTTP client functions: qualified name -> HTTP method (None = first arg).
PYTHON_HTTP_FUNCS: dict[str, str | None] = {
    "requests.get": "GET",
    "requests.post": "POST",
    "requests.put": "PUT",
    "requests.patch": "PATCH",
    "requests.delete": "DELETE",
    "requests.request": None,
}

# Call names treated as storage sinks when a field value is passed to them.
STORE_CALL_SUFFIXES = (
    "save", "insert", "insert_one", "insertOne", "execute", "executemany", "put",
    "write", "upsert", "store", "persist", "setItem",
)
