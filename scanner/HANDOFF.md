# Scanner handoff — Noirit (consumer scanner + consumer fixtures)

Checkpoint: 2026-10-17. Built by Claude (claude-opus-5-5) on Noirit's request, **not by
Codex GPT-6 Astra** as the handoff pack targeted. No app repository existed (the local
`~/shockwave` folder held only the handoff pack, and no GitHub repo was linked), so per PROMPT.md
only the owned scaffold was created: `scanner/**`, `examples/consumers/**`, `tests/scanner/**`.
**Commit:** none. This is not yet a git repository. Copy it into Ishan's bootstrap on branch
`feat/consumer-scanner`.

## Implemented

- `scan_repository(request, snapshot, repo_root) -> ScanResult` in `scanner/service.py`
  (re-exported from `scanner`). Local, read-only, no network/subprocess/package install (tested).
- `walker.py`: walks only the snapshot root and never follows symlinks (escaping ones are
  flagged `symlink_escapes_root`). Excludes vendored dirs (`node_modules`, `vendor`, `.venv`, `dist`,
  `build`…), lockfiles, generated files (`.min.js`, `.d.ts`, `@generated` headers), binaries, non-UTF-8 files, and files over 512 KB.
  Every skip is recorded.
- Tree-sitter adapters: `adapters/python.py` and `adapters/typescript.py` (JS and TS, including TSX). Both
  normalize into one `Dependency` model. Python's `ast` module is not used.
- Origins (`mappings.py` has explicit mapped methods only):
  - Python `stripe.PaymentIntent.create/retrieve` (`import stripe [as s]`, `from stripe import PaymentIntent [as PI]`).
  - JS/TS `stripe.paymentIntents.create/retrieve` via default, named (`{ Stripe }`) or
    namespace imports, `require("stripe")` and `require("stripe")(key)`, `new Stripe()` / `Stripe()`.
  - Python `requests.get/post/put/patch/delete/request(...).json()` and JS/TS global
    `fetch(url, {method})` + `await res.json()`, where the URL is a literal, f-string/template,
    `+` concatenation or string constant starting with a provider base URL.
- Flow: assignment, plain alias (`c = charge`), field alias (`amt = c["amount"]`), JS
  destructuring (`const {amount, currency: cur} = x`), nested fields, `x.get("f")`, and
  bounded call-following into same-file top-level functions (depth ≤ 3). Aliases and
  call-following are labeled `inferred`; direct reads are `direct`.
- Usage: `divide`, `multiply` (with numeric operand), `compare`, `store` (attribute/subscript
  assignment or storage-like call such as `db.save`), `pass_through` (dict/object key, call
  argument, return), `read`.
- Shadowing: a parameter or local rebinding of `stripe`/`requests`/`fetch` drops the SDK binding
  and emits a `shadowed_symbol` warning.
- Visible gaps: `dynamic_url`, `dynamic_http_method`, `dynamic_field_access` (plus an
  `unresolved` dependency `response.*`), `origin_escape` (the whole response is passed somewhere
  not followed; emits an `unresolved` dependency `response`), `promise_then_chain`,
  `unmapped_sdk_method`, `unsupported_destructuring`, `unpacking_unsupported`,
  `unsupported_language`. Parse errors (`parse_error`) and adapter crashes (`adapter_error`) are
  isolated per file.
- Evidence: kind `source`, visibility `consumer_private`, `revision` = commit SHA. Lines are
  1-based and come from syntax nodes. The chain is origin call → `.json()` → alias/call site → use site
  (use site last). Excerpts are trimmed to 200 characters.
- Stable IDs: UUIDv5 over (connection, commit, enrollment, use-site file/line/column, canonical
  endpoint identity, field_path, operation). The same commit gives the same IDs even with a new
  snapshot record. Dependencies are ordered by use site.
- Consumers: `examples/consumers/{python-display,typescript-display,stripe-merchant}`. See
  `examples/consumers/README.md` for stable functions and runner commands.

## Commands and results (run in the cloud workspace: Python 3.13.16, Node 22.22.0)

```
python -m pytest tests/scanner -q -p no:cacheprovider                    -> 19 passed
python -m pytest examples/consumers/python-display -q -p no:cacheprovider  -> 3 passed (old fixture)
  with SHOCKWAVE_RESPONSE_FIXTURE=.../charge_new.json                        -> 2 failed, '$0.10' == '$10.00'
node --test examples/consumers/typescript-display/test/charges.test.ts   -> pass 3 / fail 0 (old)
  with SHOCKWAVE_RESPONSE_FIXTURE=.../charge_new.json                        -> pass 1 / fail 2, actual '$0.10'
Hand-simulated migration (remove "/ 100" in display only) + new fixture     -> Python 3 passed, TS pass 3
python -m scanner.samples.generate                                         -> regenerates samples (golden test)
python -m scanner.demo [PATH [SERVICE_ID]] [--json]                        -> readable scan summary
```

The tests cover every PROMPT.md acceptance check: Python+TS divide/100 on demo-payments with source lines
and commit; Stripe PaymentIntent → `POST /v1/payment_intents` even when a decoy merchant
`GET /create-payment-intent` sits in the catalog; aliases in both languages; shadowed and unrelated
`amount` values ignored; dynamic URLs, parse errors and skipped files visible; one crashing file isolated;
stable repeated scans; a cross-language Pydantic round-trip; and no socket or subprocess calls during the scan.

## Sample output

`scanner/samples/demo_payments_scan_result.json` (2 dependencies, Python + TS, both
`response.amount / divide / 100 / inferred`) and `scanner/samples/stripe_merchant_scan_result.json`
(2 dependencies, `response.client_secret → clientSecret`, `direct`). These use test-double endpoint
and snapshot IDs.

## Dependencies for Ishan (root manifests; not added by me)

`pydantic>=2`, `tree-sitter==0.26.0`, `tree-sitter-python==0.25.0`,
`tree-sitter-javascript==0.25.0`, `tree-sitter-typescript==0.23.2`, and `pytest` (dev).
Node ≥ 22.18 runs the TS consumer tests (no npm packages).

## Proposed interface changes (additive and optional; need Ishan's approval)

1. `ScanRequest.consumer_org_id: str | None`. `Dependency.consumer_org_id` is required, but
   ScanRequest/RepositorySnapshot don't carry it. The scanner raises `ValueError` without it.
   Alternative: the backend derives it from the enrollment and passes it in.
2. `ScanRequest.base_urls: list[str] | None` (from `APIProject.base_urls`). This is needed to attribute
   raw HTTP calls. Fallback: `mappings.KNOWN_BASE_URLS` (`demo-payments`, `stripe`).

`scanner/contracts.py` imports `shared.schemas` when it exists and otherwise uses
contract-shaped local doubles (`extra="forbid"`). If the canonical models lack the two fields above,
`getattr` keeps the call working, but `consumer_org_id` is still required.

## Live versus demo

Everything is local and deterministic (rules mode). No live GitHub, email, network or model calls.
Endpoint IDs, snapshot IDs and the old/new response fixtures are **temporary local doubles**. The
Stripe merchant fixture is an independently written reconstruction with no pinned upstream commit
(API version 2023-10-16).

## Known limitations

No flow merging across branches (last write wins). No closures, class attributes, cross-file imports, or
`.then()` chains. `requests.Session`, `httpx` and `axios` are unmapped. Only two Stripe methods are mapped.
Function-local imports are scoped approximately. Parse-recovered files may give partial results (flagged).
Whole-object escapes create `unresolved` dependencies, which Kanishk should treat as unknown, not safe.

## Next integration actions

- **Ishan:** approve or deny the two ScanRequest fields; add the dependencies; supply canonical endpoint
  and snapshot IDs; call `scan_repository` from the scan job using the server-resolved checkout path.
- **Kanishk:** confirm fixture IDs. `analyze_impact` should match `Dependency.endpoint_id` +
  `field_path == "response.amount"` + `usage.operation == "divide"`, and should route `unresolved`
  dependencies to `unresolved_dependency_ids`.
- **Sai:** wire `trusted_python` / `trusted_node` to the commands in examples/consumers/README.md, with
  `SHOCKWAVE_RESPONSE_FIXTURE`. The migration target is the use-site evidence line of the divide dependency.
