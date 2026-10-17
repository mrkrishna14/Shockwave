# Stripe merchant scan fixture (optional)

Purpose: real-SDK dependency evidence for the authentic Stripe story. Not part of
the fictional demo-payments migration, and never a source of Stripe provider routes.

- Provenance: independently written minimal reconstruction modeled on
  `stripe-samples/accept-a-payment` `payment-element/server/{node,python}` as
  inspected in SOURCES.md (Oct 16, 2026). No upstream file was downloaded or
  copied; **no upstream commit is pinned** — pin one if the real sample is
  ever scanned instead.
- Pinned Stripe API version: `2023-10-16` (matches the inspected samples). Do not
  silently compare it with a latest-spec scan.
- Merchant routes (consumer-owned, NOT Stripe inventory): `GET /create-payment-intent`,
  `POST /webhook` (node only).
- Expected scanner output (service_id `stripe`): `response.client_secret`,
  origin `stripe.paymentIntents.create` / `stripe.PaymentIntent.create`, endpoint
  `POST /v1/payment_intents`, usage `pass_through` → `clientSecret`, confidence `direct`.
- These files are not executable without `express`/`flask`/`stripe`; they are scan
  fixtures only and have no native tests.
