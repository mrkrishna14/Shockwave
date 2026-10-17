"""PROMPT.md acceptance checks for the consumer scanner."""
from __future__ import annotations

import json
import os
import socket
import subprocess

import pytest

from conftest import (
    COMMIT, CONSUMER_ORG, CONSUMERS, DECOY_MERCHANT, DEMO_CHARGE, STRIPE_PI_CREATE, line_of, make_request, make_snapshot, tid, write,
)
from scanner.contracts import Dependency, ScanResult


def _uses(result, op=None):
    return [d for d in result.dependencies if op is None or d.usage.operation == op]


# 1. Python + TypeScript display consumers -> amount/divide/100 on demo-payments.
@pytest.mark.parametrize(
    "consumer,file,lang",
    [
        ("python-display", "python-display/demo_display/charges.py", "python"),
        ("typescript-display", "typescript-display/src/charges.ts", "typescript"),
    ],
)
def test_display_consumer_divide_dependency(scan, consumer, file, lang):
    result = scan(CONSUMERS)
    deps = [d for d in result.dependencies if d.evidence[-1].file == file]
    assert len(deps) == 1, [d.model_dump() for d in result.dependencies]
    d = deps[0]
    assert d.language == lang
    assert d.service_id == "demo-payments"
    assert d.endpoint_id == DEMO_CHARGE.id
    assert d.field_path == "response.amount"
    assert d.usage.operation == "divide" and d.usage.operand == 100
    assert d.origin.kind == "http_response"
    assert d.confidence == "inferred"  # crosses display_amount(charge) call boundary
    src = CONSUMERS / file
    use = d.evidence[-1]
    assert use.line_start == line_of(src, "/ 100")
    labels_lines = [e.line_start for e in d.evidence]
    assert line_of(src, "fetch(" if lang == "typescript" else "requests.get(") in labels_lines
    assert all(e.revision == COMMIT and e.visibility == "consumer_private" for e in d.evidence)
    assert d.repository_snapshot_id == tid("snap-consumer-1")


def test_negative_control_order_amount_not_attributed(scan):
    result = scan(CONSUMERS)
    files = {e.file for d in result.dependencies for e in d.evidence}
    assert not any("orders." in f for f in files)
    assert not any("stripe-merchant" in f for f in files)  # other service ignored
    assert len(result.dependencies) == 2


# 2. Stripe PaymentIntent -> POST /v1/payment_intents, never the merchant route.
@pytest.mark.parametrize("file,lang", [("node/server.js", "javascript"), ("python/server.py", "python")])
def test_stripe_payment_intent_maps_to_stripe_operation(scan, file, lang):
    result = scan(CONSUMERS / "stripe-merchant", service_id="stripe")
    deps = [d for d in result.dependencies if d.evidence[-1].file == file]
    assert len(deps) == 1
    d = deps[0]
    assert d.endpoint_id == STRIPE_PI_CREATE.id
    assert d.endpoint_id != DECOY_MERCHANT.id
    assert d.origin.kind == "sdk_response"
    assert d.field_path == "response.client_secret"
    assert d.usage.operation == "pass_through" and d.usage.destination == "clientSecret"
    assert d.confidence == "direct"
    assert d.evidence[0].line_start == line_of(CONSUMERS / "stripe-merchant" / file, "aymentIntent")


def test_services_stay_separate(scan):
    assert scan(CONSUMERS / "stripe-merchant", service_id="demo-payments").dependencies == []
    assert scan(CONSUMERS / "python-display", service_id="stripe").dependencies == []


# 3. Local aliasing in each language; shadowed/unrelated objects excluded.
PY_ALIAS = '''
import requests
from stripe import PaymentIntent as PI

BASE = "http://demo-payments.test"


def total(cid):
    r = requests.get(BASE + "/v1/charges/" + cid)
    body = r.json()
    c = body
    amt = c["amount"]
    if amt > 500:
        pass
    return amt / 100


def inline(cid):
    return requests.get(f"{BASE}/v1/charges/{cid}").json()["amount"] * 2


def keep(cid, db):
    charge = requests.get(f"{BASE}/v1/charges/{cid}").json()
    db.save(charge.get("currency"))


def foreign():
    other = requests.get("https://example.com/v1/charges/1").json()
    return other["amount"] / 100
'''

TS_ALIAS = '''
import Stripe from "stripe";
const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);

export async function a() {
  const intent = await stripe.paymentIntents.create({ amount: 1, currency: "usd" });
  const { amount, currency: cur } = intent;
  if (amount >= 100) {}
  const pi = intent;
  return pi.amount * 2;
}

export async function b(id: string) {
  const res = await fetch("http://demo-payments.test/v1/charges/" + id);
  const charge = (await res.json()) as { amount: number };
  return charge["amount"] / 100;
}
'''


def test_python_aliases(scan, tmp_path):
    write(tmp_path, {"app.py": PY_ALIAS})
    deps = scan(tmp_path).dependencies
    got = sorted((d.usage.operation, d.usage.operand, d.field_path, d.confidence) for d in deps)
    assert got == sorted([
        ("compare", 500, "response.amount", "inferred"),
        ("divide", 100, "response.amount", "inferred"),
        ("multiply", 2, "response.amount", "direct"),
        ("store", None, "response.currency", "direct"),
    ])
    assert all(d.endpoint_id == DEMO_CHARGE.id for d in deps)
    store = next(d for d in deps if d.usage.operation == "store")
    assert store.usage.destination == "db.save"


def test_typescript_aliases_and_destructuring(scan, tmp_path):
    write(tmp_path, {"src/app.ts": TS_ALIAS})
    stripe_deps = scan(tmp_path, service_id="stripe").dependencies
    got = sorted((d.usage.operation, d.usage.operand, d.field_path, d.confidence) for d in stripe_deps)
    assert got == sorted([
        ("compare", 100, "response.amount", "inferred"),
        ("multiply", 2, "response.amount", "inferred"),
        ("read", None, "response.currency", "inferred"),
    ])
    demo = scan(tmp_path).dependencies
    assert [(d.usage.operation, d.usage.operand, d.confidence) for d in demo] == [("divide", 100, "direct")]


PY_SHADOW = '''
import stripe


def make(stripe):
    i = stripe.PaymentIntent.create(amount=1)
    return i.amount / 100


def other():
    stripe = object()
    x = stripe.PaymentIntent.create()
    return x.amount / 100


def unrelated(charge):
    return charge["amount"] / 100


def looks_like_sdk():
    s = "stripe.PaymentIntent.create"
    amount = 1000
    return amount / 100
'''

TS_SHADOW = '''
import Stripe from "stripe";
const stripe = new Stripe(process.env.KEY);

function local() {
  const stripe = { paymentIntents: { create: (x: any) => x } };
  const r = stripe.paymentIntents.create({ amount: 5 });
  return r.amount / 100;
}

function param(stripe: any) {
  const r = stripe.paymentIntents.create({});
  return r.amount / 100;
}

const charge = { amount: 5 };
export const fee = charge.amount / 100;

function fakeFetch(fetch: any) {
  return fetch("http://demo-payments.test/v1/charges/1").amount / 100;
}
'''


def test_shadowed_and_unrelated_produce_nothing(scan, tmp_path):
    write(tmp_path, {"shadow.py": PY_SHADOW, "shadow.ts": TS_SHADOW})
    for service in ("stripe", "demo-payments"):
        result = scan(tmp_path, service_id=service)
        assert result.dependencies == [], [d.model_dump() for d in result.dependencies]
    result = scan(tmp_path, service_id="stripe")
    assert any(w.startswith("shadowed_symbol: shadow.py") for w in result.warnings)
    assert any(w.startswith("shadowed_symbol: shadow.ts") for w in result.warnings)


def test_commonjs_and_named_imports(scan, tmp_path):
    write(tmp_path, {
        "a.cjs": 'const Stripe = require("stripe");\nconst client = Stripe(process.env.K);\n'
                 'async function f() { const p = await client.paymentIntents.retrieve("pi_1"); return p.amount / 100; }\n',
        "b.mjs": 'import { Stripe as S } from "stripe";\nconst c = new S(process.env.K);\n'
                 'export async function g() { const p = await c.paymentIntents.create({}); return p.status === "succeeded"; }\n',
        "c.mjs": 'import Stripe from "stripe";\nconst c = new Stripe(process.env.K);\n'
                 'export async function h() { const p = await c.paymentIntents.cancel("pi_1"); return p.amount / 100; }\n',
    })
    result = scan(tmp_path, service_id="stripe")
    got = sorted((d.evidence[-1].file, d.endpoint_id, d.usage.operation) for d in result.dependencies)
    assert got == [
        ("a.cjs", tid("ep-stripe-get-v1-payment-intents-intent"), "divide"),
        ("b.mjs", tid("ep-stripe-post-v1-payment-intents"), "compare"),
    ]
    # Unmapped SDK methods are reported, not guessed.
    assert any(p.startswith("unmapped_sdk_method: c.mjs") for p in result.coverage.unsupported_patterns)


# 4. Dynamic URLs, parse errors, skipped files are visible; one bad file is isolated.
def test_dynamic_urls_and_escapes_are_reported(scan, tmp_path):
    write(tmp_path, {
        "dyn.py": "import requests, os\n\ndef f(u):\n    return requests.get(u).json()['amount'] / 100\n\n"
                  "def g():\n    return requests.get(os.environ['API'] + '/v1/charges/1').json()\n",
        "dyn.ts": "export async function f(base: string) {\n  const r = await fetch(`${base}/v1/charges/1`);\n"
                  "  return r.then((x: any) => x.json());\n}\n"
                  "export async function k(id: string) {\n  const r = await fetch(`http://demo-payments.test/v1/charges/${id}`);\n"
                  "  const body = await r.json();\n  const key = 'amount';\n  console.log(body);\n  return body[key];\n}\n",
    })
    result = scan(tmp_path)
    pats = result.coverage.unsupported_patterns
    assert "dynamic_url: dyn.py:4 (requests.get: URL host is not a literal)" in pats
    assert any(p.startswith("dynamic_url: dyn.py:7") for p in pats)
    assert any(p.startswith("dynamic_url: dyn.ts:2") for p in pats)
    assert any(p.startswith("dynamic_field_access: dyn.ts:10") for p in pats)
    assert any(p.startswith("origin_escape: dyn.ts:9") for p in pats)
    unresolved = [d for d in result.dependencies if d.confidence == "unresolved"]
    assert {d.field_path for d in unresolved} == {"response", "response.*"}
    assert not [d for d in result.dependencies if d.confidence == "direct"]


def test_parse_error_and_bad_file_do_not_erase_results(scan, tmp_path, monkeypatch):
    write(tmp_path, {
        "good.py": (CONSUMERS / "python-display/demo_display/charges.py").read_text(),
        "broken.py": "def broken(:\n    return (\n",
        "boom.ts": "export const x = 1;\n",
    })
    from scanner.adapters import typescript as ts_adapter

    real_run = ts_adapter.TypeScriptAdapter.run

    def flaky(self):
        if self.ctx.file == "boom.ts":
            raise RuntimeError("simulated adapter crash")
        return real_run(self)

    monkeypatch.setattr(ts_adapter.TypeScriptAdapter, "run", flaky)
    result = scan(tmp_path)
    assert [d.field_path for d in result.dependencies] == ["response.amount"]
    assert any(w.startswith("parse_error: broken.py:1") for w in result.warnings)
    assert "adapter_error: boom.ts (RuntimeError)" in result.warnings
    assert result.coverage.files_scanned == 2 and result.coverage.files_skipped == 1


def test_skipped_files_are_recorded(scan, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("import requests\n")
    repo = tmp_path / "repo"
    write(repo, {
        "node_modules/stripe/index.js": "module.exports = 1;\n",
        "vendor/lib.py": "x = 1\n",
        "package-lock.json": "{}",
        "dist/app.min.js": "var a=1;",
        "types.d.ts": "export {};",
        "gen.py": "# @generated by protoc\nx = 1\n",
        "bin.py": b"\x00\x01\x02binary",
        "big.py": "x = 1\n" * 200_000,
        "latin1.py": "s = '\xe9'\n".encode("latin-1"),
        "ok.py": "x = 1\n",
    })
    try:  # Windows needs Developer Mode/admin to create symlinks
        os.symlink(outside / "secret.py", repo / "escape.py")
        os.symlink(repo / "ok.py", repo / "inner_link.py")
        symlinks = True
    except OSError:
        symlinks = False
    result = scan(repo)
    w = set(result.warnings)
    for expected in [
        "skipped_dir: node_modules/ (excluded_dir)",
        "skipped_dir: vendor/ (excluded_dir)",
        "skipped_dir: dist/ (excluded_dir)",
        "skipped_file: package-lock.json (lockfile)",
        "skipped_file: types.d.ts (generated)",
        "skipped_file: gen.py (generated)",
        "skipped_file: bin.py (binary)",
        "skipped_file: big.py (too_large)",
        "skipped_file: latin1.py (not_utf8)",
    ] + ([
        "skipped_file: escape.py (symlink_escapes_root)",
        "skipped_file: inner_link.py (symlink_inside_root_not_followed)",
    ] if symlinks else []):
        assert expected in w, expected
    assert result.coverage.files_scanned == 1
    assert result.coverage.files_skipped == (8 if symlinks else 6)


def test_unsupported_language_request_is_visible(scan, tmp_path):
    write(tmp_path, {"a.py": "x = 1\n", "b.ts": "export const y = 2;\n"})
    result = scan(tmp_path, languages=["python", "go"])
    assert "unsupported_language: go" in result.coverage.unsupported_patterns
    assert "skipped_file: b.ts (language_not_requested:typescript)" in result.warnings


# 5. Stable identity/order; one Pydantic model across languages.
def test_repeated_scans_are_stable(scan):
    a = scan(CONSUMERS).model_dump(mode="json")
    b = scan(CONSUMERS).model_dump(mode="json")
    assert a == b
    # Same commit fetched into a new snapshot record: dependency identity is stable.
    c = scan(CONSUMERS, snapshot=make_snapshot(tid("snap-consumer-2")))
    assert [d["id"] for d in a["dependencies"]] == [d.id for d in c.dependencies]
    # A different commit yields different identities.
    d = scan(CONSUMERS, snapshot=make_snapshot(tid("snap-consumer-3"), commit="0" * 40))
    assert not set(x["id"] for x in a["dependencies"]) & set(x.id for x in d.dependencies)


def test_cross_language_results_validate_against_one_model(scan):
    result = scan(CONSUMERS)
    round_trip = ScanResult.model_validate(json.loads(result.model_dump_json()))
    assert round_trip == result
    assert round_trip.schema_version == "shockwave.v1"
    assert {d.language for d in round_trip.dependencies} == {"python", "typescript"}
    assert all(type(d) is Dependency for d in round_trip.dependencies)
    shapes = {(d.field_path, d.usage.operation, d.usage.operand, d.endpoint_id) for d in round_trip.dependencies}
    assert len(shapes) == 1


# 6. No network, package installation, or consumer test execution during scanning.
def test_scan_is_offline_and_executes_nothing(scan, monkeypatch):
    def forbid(*a, **k):
        raise AssertionError("scanner attempted network/process execution")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setattr(os, "system", forbid)
    monkeypatch.setattr(os, "execv", forbid)
    result = scan(CONSUMERS)
    assert len(result.dependencies) == 2


def test_requires_consumer_org_and_matching_snapshot(tmp_path):
    from scanner import scan_repository

    snap = make_snapshot()
    req = make_request("demo-payments", snap)
    with pytest.raises(ValueError):
        scan_repository(req, snap, tmp_path)  # no consumer_org_id
    with pytest.raises(ValueError):
        scan_repository(req.model_copy(update={"repository_snapshot_id": tid("other")}), snap, tmp_path,
                        consumer_org_id=CONSUMER_ORG)
