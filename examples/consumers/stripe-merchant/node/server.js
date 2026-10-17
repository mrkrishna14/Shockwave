// Minimal merchant server fixture for scanner tests (written for Shockwave;
// modeled on the inspected stripe-samples/accept-a-payment Payment Element
// server, not copied from it). Merchant routes below are the MERCHANT's own
// routes; the Stripe API operation is POST /v1/payment_intents via stripe-node.
const express = require("express");
const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY, {
  apiVersion: "2023-10-16",
});

const app = express();

app.get("/create-payment-intent", async (req, res) => {
  const paymentIntent = await stripe.paymentIntents.create({
    amount: 1999,
    currency: "usd",
    automatic_payment_methods: { enabled: true },
  });
  res.send({ clientSecret: paymentIntent.client_secret });
});

app.post("/webhook", (req, res) => {
  res.sendStatus(200);
});

module.exports = app;
