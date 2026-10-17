# Three-minute demo script

**Before you start:**
1. Run `.venv/bin/python demo/scripts/demo.py prepare` once, with network, so the npm cache has
   `stripe@23.0.0`.
2. Open two terminals at the repository root.
3. Run `.venv/bin/python demo/scripts/demo.py reset` so the run starts clean.

Say it once, up front: **"Stripe is the real provider here, but the change is simulated. Stripe
hasn't proposed it, and Stripe isn't a customer."**

---

### 0:00–0:30 · The real contract

```sh
.venv/bin/python demo/scripts/demo.py inventory
```

**Point at:**
- the pinned commit `969d8c3`, API `2026-09-30.endive`, and the sha256 that matches the lock;
- the **644 operations over 454 paths**, measured from Stripe's published spec, not typed in;
- `GET /v1/payment_intents/{intent}`, where `amount` is an integer documented in the "smallest
  currency unit";
- **provider source access: unavailable**. We only claim what the public contract shows.

### 0:30–1:00 · The consumers and what depends on `amount`

```sh
.venv/bin/python demo/scripts/demo.py prepare --offline && .venv/bin/python demo/scripts/demo.py scan
```

**Say:** "Three team-built apps on the official Stripe SDKs:
- a TypeScript checkout that divides `amount` by 100 for display;
- a Python fraud service that flags anything over $5,000 by comparing with `500000`;
- a reporting job that stores `amount` in a ledger."

**Point at:**
- `checkout ... real scan` and `fraud ... real scan`: Noirit's scanner found the `/ 100`
  conversion and the `> 500000` comparison itself.
- For reporting, the scanner's misses are listed (its `StripeClient(...).v1` style isn't mapped
  yet), and that app uses the labeled, line-verified fixture.
- The unrelated look-alikes (basis-points `/ 100`, local `.amount` fields) are confirmed **not**
  to be dependencies.

### 1:00–1:30 · Submit the simulated change

```sh
.venv/bin/python demo/scripts/demo.py submit-change
```

**Point at:**
- `schema diff alone: 0 changes`. The schema doesn't change: `amount` stays an integer. Only the
  provider's declared semantics reveal the change.
- Three predicted risks: checkout conversion **high**, fraud comparison **high**, reporting
  storage **medium**.
- Every result is labeled as predicted (`not executed`) until it's run.

### 1:30–2:20 · Prove it: run the real code against old vs. new responses

```sh
.venv/bin/python demo/scripts/demo.py execute
```

**Point at:**
- **Checkout:** baseline passes; proposed gives **"expected $10.00, actual $0.10"**:
  `verified_regression`. The tax-note guard (an unrelated `/ 100`) still passes.
- **Fraud:** the $6,000 payment is **not flagged** any more (`expected True, actual False`), while
  the $4,000 and exactly-$5,000 guards still pass: `verified_regression`.
- **Reporting:** the currency/status summary passes on **both** versions (the unaffected control).
  The ledger storage stays a **potential** risk, `not_run`, with the missing evidence listed. We
  don't claim what we didn't prove.

**Optional, in terminal 2:** `.venv/bin/python demo/scripts/demo.py fixture-server --variant proposed`,
then `curl http://127.0.0.1:8765/v1/payment_intents/pi_demo_10usd` shows `"amount": 10`.

### 2:20–3:00 · Tested fixes, with an honest caveat

```sh
.venv/bin/python demo/scripts/demo.py validate-migrations
```

**Point at:**
- Both targeted patches are **validated** against the proposed responses with the **same**
  expectations: still $10.00, still flagged above $5,000.
- Unrelated conversions are untouched.
- The patched checkout shows **$1000.00** against today's responses. Each fix is pinned to the new
  contract, so it must ship with the provider's cutover, not before.

**Close:** "The provider gets a restricted summary: counts only, no consumer code, paths or
logs. Each consumer sees its own evidence and a tested patch."

---

**Fallback:** run `.venv/bin/python demo/scripts/demo.py all` for the whole run in one command
(about 20 s).

**Labels to keep on screen:**
- `SIMULATED change`
- `LOCAL RUNNER DOUBLE (not a sandbox, not Sai's runner)`
- `deterministic rules (no LLM)`
- `FIXTURE` where the scanner didn't attribute a call
