# Consumer fixtures (owner: Noirit)

Team-owned consumers of the **fictional** `demo-payments` service
(`http://demo-payments.test`, `GET /v1/charges/{id}`). Not Stripe. The optional
`stripe-merchant/` fixture is separate; see its FIXTURE.md.

## Stable interfaces

| Consumer | Pure function (Kanishk verifies) | HTTP origin | Negative control (must stay unchanged) |
|---|---|---|---|
| `python-display/` | `demo_display.charges.display_amount(response) -> str` | `fetch_charge_display(charge_id)` → `requests.get(...).json()` | `demo_display.orders.format_order_fee(order)` |
| `typescript-display/` | `src/charges.ts: displayAmount(response): string` | `fetchChargeDisplay(chargeId)` → `fetch(...)`, `res.json()` | `src/orders.ts: formatOrderFee(order)` |

Both divide `amount` by 100 (cents → dollars). Migration target lines:
`python-display/demo_display/charges.py` `dollars = response["amount"] / 100` and
`typescript-display/src/charges.ts` `const dollars = response.amount / 100;`.

## Test commands (Sai's runner profiles) — run from repo root

`SHOCKWAVE_RESPONSE_FIXTURE` = absolute path to the charge response JSON. Default is the local old fixture.

```
trusted_python: python -m pytest examples/consumers/python-display -q -p no:cacheprovider
trusted_node:   node --test examples/consumers/typescript-display/test/charges.test.ts   # Node >= 22.18 (type stripping)
```

No third-party packages are needed (the Python HTTP client is imported lazily and faked in tests; the
TS test replaces `globalThis.fetch`). Tests never touch the network.

| Fixture | Python result | TS result |
|---|---|---|
| old `{"amount":1000}` | 3 passed | pass 3 |
| proposed `{"amount":10}` | 2 failed: `assert '$0.10' == '$10.00'` | fail 2: `expected '$10.00'`, `actual '$0.10'` |
| proposed after removing only the `/ 100` above | 3 passed | pass 3 |

Relevant assertion for verification: `test_display_amount_shows_ten_dollars` /
`displayAmount shows ten dollars`. The order-fee test is the unrelated-conversion control.

## Fixture IDs

`*/fixtures/charge_old.json` and `charge_new.json` are **temporary local copies** that match
FIXTURES.md. Kanishk owns the canonical semantic response fixtures (proposed IDs
`demo-payments.charge.v1-cents` / `demo-payments.charge.v2-dollars`, pending his
confirmation). Replace the local copies by pointing `SHOCKWAVE_RESPONSE_FIXTURE` at his files.

Note: the migrated consumer is pinned to the new contract and is not backward-compatible with
old responses; rollout must be coordinated.
