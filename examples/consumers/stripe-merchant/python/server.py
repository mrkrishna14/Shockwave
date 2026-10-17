"""Minimal merchant server fixture for scanner tests (written for Shockwave;
modeled on the inspected stripe-samples/accept-a-payment Payment Element
server, not copied from it). /create-payment-intent is the MERCHANT's route;
the Stripe API operation is POST /v1/payment_intents via stripe-python.
"""
import os

import stripe
from flask import Flask, jsonify

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
stripe.api_version = "2023-10-16"

app = Flask(__name__)


@app.route("/create-payment-intent", methods=["GET"])
def create_payment():
    intent = stripe.PaymentIntent.create(
        amount=1999,
        currency="usd",
        automatic_payment_methods={"enabled": True},
    )
    return jsonify({"clientSecret": intent.client_secret})
