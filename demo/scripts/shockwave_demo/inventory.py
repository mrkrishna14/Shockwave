"""``inventory``: discover the pinned, unmodified Stripe OpenAPI contract and measure it.

Discovery runs on the vendored public document only (``discovery.pinned`` re-verifies its
sha256 and size against ``examples/provider/stripe/pin.json``), with NO provider source:
Stripe's server implementation is not accessible, and SDKs, stripe-mock and sample apps are
not provider source. Every count comes from ``discovery`` at run time; nothing is hardcoded.
The selected operation's record (GET /v1/payment_intents/{intent}) is extracted with its
parameters and the ``amount`` field's published type and documentation text as docs evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache

from discovery import discover
from discovery.contracts import DiscoveryInput, Endpoint, Evidence
from discovery.ids import evidence_id
from discovery.inventory import summarize_inventory
from discovery.pinned import STRIPE_PIN_PATH, load_pinned_document
from discovery.records import FetchedDocument
from discovery.service import DiscoveryOutcome
from discovery.text import clip_text
from shockwave_demo.common import (
    CHANGE_MANIFEST_PATH,
    IDS_PATH,
    LOCK_PATH,
    PROVIDER_SOURCE_STATEMENT,
    DemoError,
    Layout,
    count,
    read_json,
    repo_relative,
    write_json,
)

STRIPE_DOCUMENT_REF = "stripe/openapi:latest/openapi.spec3.json"
SERVICE_ID = "stripe"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
INVENTORY_RESULT = "inventory.json"
INVENTORY_LABEL = (
    "authentic pinned public Stripe OpenAPI contract (unmodified); public contract discovery, not Stripe "
    "production source"
)


@cache
def pinned_document() -> FetchedDocument:
    return load_pinned_document(STRIPE_PIN_PATH)


@cache
def raw_document() -> dict:
    """The pinned spec parsed directly (for documentation strings and independent checks)."""
    return json.loads(pinned_document().content)


def discover_stripe(snapshot_id: str) -> DiscoveryOutcome:
    """The pinned Stripe document discovered under ``snapshot_id``; no provider source (unavailable)."""
    request = DiscoveryInput(
        service_id=SERVICE_ID,
        snapshot_id=snapshot_id,
        openapi_documents=[STRIPE_DOCUMENT_REF],
        source_snapshot_id=None,  # Stripe provider source is unavailable: never scanned, never manufactured
        frameworks=[],
    )
    return discover(request, {STRIPE_DOCUMENT_REF: pinned_document()}, None)


def find_endpoint(endpoints: list[Endpoint], method: str, path: str) -> Endpoint:
    matches = [item for item in endpoints if (item.method, item.path) == (method.upper(), path)]
    if len(matches) != 1:
        raise DemoError(f"expected exactly one {method} {path} in the discovered contract, found {len(matches)}")
    return matches[0]


def json_pointer_get(document: object, pointer: str) -> object:
    node = document
    for token in pointer.lstrip("/").split("/") if pointer else []:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            raise DemoError(f"the pinned Stripe document has no {pointer}")
    return node


def lock_problems(lock: Mapping, pin: Mapping, manifest: Mapping) -> list[str]:
    """Disagreements between the source lock, the vendored pin and the change manifest."""
    problems = []
    sources = {item["key"]: item for item in lock.get("sources", [])}
    openapi = sources.get("openapi", {})
    api_version = lock.get("api_version")
    if pin["source"]["commit"] != openapi.get("commit"):
        problems.append("pin.json commit differs from the lock's openapi commit")
    if pin["content"]["sha256"] != openapi.get("sha256") or pin["content"]["byte_size"] != openapi.get("byte_size"):
        problems.append("pin.json sha256/size differ from the lock's openapi entry")
    if openapi.get("path") and not pin["source"]["raw_url"].endswith("/" + openapi["path"]):
        problems.append("pin.json raw_url does not name the lock's openapi path")
    versions = {
        "openapi.info_version": openapi.get("info_version"),
        "stripe-python.api_version": sources.get("stripe-python", {}).get("api_version"),
        "stripe-node.api_version": sources.get("stripe-node", {}).get("api_version"),
        "stripe-mock.embedded_spec_version": sources.get("stripe-mock", {}).get("embedded_spec_version"),
    }
    problems += [f"{name} {value!r} != lock api_version {api_version!r}" for name, value in versions.items() if value != api_version]
    baseline = manifest.get("baseline_contract", {})
    if (baseline.get("commit"), baseline.get("sha256"), baseline.get("api_version")) != (
        openapi.get("commit"),
        openapi.get("sha256"),
        api_version,
    ):
        problems.append("the change manifest's baseline_contract differs from the lock")
    if baseline.get("modified") is not False:
        problems.append("the change manifest must state the baseline contract is unmodified")
    return problems


def _parameters(raw: Mapping, json_pointer: str) -> list[dict]:
    operation = json_pointer_get(raw, json_pointer)
    parameters = []
    for item in operation.get("parameters", []) if isinstance(operation, Mapping) else []:
        schema = item.get("schema", {}) if isinstance(item, Mapping) else {}
        parameters.append(
            {
                "name": item.get("name"),
                "in": item.get("in"),
                "required": bool(item.get("required", False)),
                "type": schema.get("type"),
                "items_type": (schema.get("items") or {}).get("type") if isinstance(schema.get("items"), Mapping) else None,
                "max_length": schema.get("maxLength"),
                "description": clip_text(item.get("description") or "", 300) or None,
            }
        )
    return parameters


def endpoint_record(endpoint: Endpoint, pin: Mapping) -> dict:
    """GET /v1/payment_intents/{intent}: parameters, response amount type and its docs evidence."""
    raw = raw_document()
    openapi_items = [item for item in endpoint.evidence if item.kind == "openapi" and item.json_pointer]
    if not openapi_items:
        raise DemoError(f"{endpoint.method} {endpoint.path} has no openapi evidence")
    operation_pointer = openapi_items[0].json_pointer
    response = json_pointer_get(raw, f"{operation_pointer}/responses/200/content/application~1json/schema")
    reference = response.get("$ref") if isinstance(response, Mapping) else None
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise DemoError("the 200 response schema of the operation is not a local $ref")
    amount_pointer = f"{reference[1:]}/properties/amount"
    raw_amount = json_pointer_get(raw, amount_pointer)
    discovered_amount = endpoint.response_schema.get("properties", {}).get("amount", {})
    if not isinstance(raw_amount, Mapping) or raw_amount.get("type") != discovered_amount.get("type"):
        raise DemoError("the discovered amount schema differs from the pinned document")
    commit = pin["source"]["commit"]
    docs = Evidence(
        id=evidence_id("stripe-demo/openapi-docs", commit, amount_pointer),
        kind="docs",
        visibility="public",
        source_uri=pin["source"]["raw_url"],
        revision=commit,
        file=None,
        line_start=None,
        line_end=None,
        json_pointer=amount_pointer,
        excerpt=clip_text(str(raw_amount.get("description", "")), 1000),
    )
    return {
        "endpoint_id": str(endpoint.id),
        "snapshot_id": str(endpoint.snapshot_id),
        "service_id": endpoint.service_id,
        "method": endpoint.method,
        "path": endpoint.path,
        "operation_id": endpoint.operation_id,
        "json_pointer": operation_pointer,
        "reconciliation": endpoint.reconciliation,
        "parameters": _parameters(raw, operation_pointer),
        "response": {
            "schema": reference,
            "title": endpoint.response_schema.get("title"),
            "required": endpoint.response_schema.get("required", []),
            "property_count": len(endpoint.response_schema.get("properties", {})),
            "amount": {
                "json_pointer": amount_pointer,
                "type": raw_amount.get("type"),
                "description": raw_amount.get("description"),
            },
        },
        "evidence": [item.model_dump(mode="json") for item in [*openapi_items, docs]],
    }


def independent_counts(raw: Mapping) -> dict:
    """Operation, path, method and component-schema counts from the raw document (no discovery code)."""
    by_method: dict[str, int] = {}
    paths = raw.get("paths", {})
    for item in paths.values():
        for method in HTTP_METHODS:
            if isinstance(item, Mapping) and method in item:
                by_method[method.upper()] = by_method.get(method.upper(), 0) + 1
    return {
        "paths": len(paths),
        "operations": sum(by_method.values()),
        "by_method": dict(sorted(by_method.items())),
        "component_schemas": len(raw.get("components", {}).get("schemas", {})),
    }


def build_inventory() -> dict:
    lock = read_json(LOCK_PATH)
    pin = read_json(STRIPE_PIN_PATH)
    manifest = read_json(CHANGE_MANIFEST_PATH)
    ids = read_json(IDS_PATH)
    problems = lock_problems(lock, pin, manifest)
    if problems:
        raise DemoError("source lock is inconsistent: " + "; ".join(problems))
    outcome = discover_stripe(ids["baseline_contract_snapshot_id"])
    summary = summarize_inventory(outcome)
    (document,) = summary["documents"]
    raw = raw_document()
    measured = {
        "paths": document["path_count"],
        "operations": document["operation_count"],
        "by_method": summary["by_method"],
        "component_schemas": document["component_schema_count"],
    }
    if measured != independent_counts(raw):
        raise DemoError("discovery counts disagree with a direct count of the pinned document")
    if document["info_version"] != lock["api_version"] or document["sha256"] != pin["content"]["sha256"]:
        raise DemoError("the discovered document's version or sha256 differs from the lock")
    endpoint = find_endpoint(list(outcome.result.endpoints), manifest["endpoint"]["method"], manifest["endpoint"]["path"])
    if str(endpoint.id) != ids["endpoint"]["baseline_endpoint_id"]:
        raise DemoError("the discovered endpoint id differs from demo/expected/demo-ids.json")
    sources = {item["key"]: item for item in lock["sources"]}
    return {
        "kind": "shockwave-demo.inventory@1",
        "label": INVENTORY_LABEL,
        "provider": "Stripe",
        "provider_note": (
            "Stripe is the API provider whose public contract is analyzed. Stripe is not a Shockwave customer and "
            "has not endorsed Shockwave."
        ),
        "provider_source_access": {"status": "unavailable", "statement": PROVIDER_SOURCE_STATEMENT},
        "contract": {
            "repository": pin["source"]["repository"],
            "commit": pin["source"]["commit"],
            "path": sources["openapi"]["path"],
            "raw_url": pin["source"]["raw_url"],
            "license": sources["openapi"]["license"],
            "vendored_copy": repo_relative(STRIPE_PIN_PATH.parent / pin["vendored_file"]),
            "sha256": document["sha256"],
            "byte_size": document["byte_size"],
            "openapi_version": document["openapi_version"],
            "api_version": document["info_version"],
            "snapshot_id": ids["baseline_contract_snapshot_id"],
            "modified": False,
            "lock_consistent": True,
        },
        "counts": {
            **measured,
            "endpoints": summary["endpoint_count"],
            "documented_operations": summary["documented_operation_count"],
            "by_reconciliation": summary["by_reconciliation"],
            "source": "computed at run time by discovery.discover from the pinned bytes, and re-checked against a "
            "direct parse of the same bytes; never hardcoded",
        },
        "reconciliation_note": (
            "Every operation is not_compared: no Stripe server source was scanned (it is unavailable), so documented "
            "operations are neither confirmed nor contradicted by an implementation."
        ),
        "discovery": {
            "coverage": summary["coverage"],
            "warning_count": summary["warning_count"],
            "warnings": list(outcome.result.warnings),
            "revision_label": document["revision_label"],
        },
        "selected_versions": {
            "api_version": lock["api_version"],
            "stripe_python": sources["stripe-python"]["version"],
            "stripe_node": sources["stripe-node"]["version"],
            "rationale": lock["selection_rationale"],
            "version_mismatch": sources["accept-a-payment"]["version_mismatch"],
            "stripe_mock": sources["stripe-mock"]["decision"],
        },
        "evidence_boundaries": lock["evidence_boundaries"],
        "endpoint": endpoint_record(endpoint, pin),
    }


def run_inventory(layout: Layout) -> list[str]:
    document = build_inventory()
    write_json(layout.result_path(INVENTORY_RESULT), document)
    counts = document["counts"]
    contract = document["contract"]
    endpoint = document["endpoint"]
    methods = ", ".join(f"{method} {number}" for method, number in counts["by_method"].items())
    amount = endpoint["response"]["amount"]
    return [
        f"Stripe API inventory - {INVENTORY_LABEL}",
        f"  contract   {contract['repository']} @ {contract['commit'][:12]} {contract['path']}",
        f"  version    OpenAPI {contract['openapi_version']}, API {contract['api_version']}",
        f"  sha256     {contract['sha256']} ({contract['byte_size']} bytes; matches the lock)",
        (
            f"  measured   {count(counts['operations'], 'operation')} over {count(counts['paths'], 'path')} "
            f"({methods}); {count(counts['component_schemas'], 'component schema')}"
        ),
        f"  {PROVIDER_SOURCE_STATEMENT}",
        f"  reconciled {counts['by_reconciliation']['not_compared']} not_compared (no server source to compare)",
        (
            f"  endpoint   {endpoint['method']} {endpoint['path']} ({endpoint['operation_id']}, id "
            f"{endpoint['endpoint_id'][:8]}); parameters: "
            + ", ".join(f"{item['name']} ({item['in']})" for item in endpoint["parameters"])
        ),
        f"  amount     type {amount['type']}; docs: {clip_text(amount['description'], 110)}",
        f"  wrote      {layout.result_path(INVENTORY_RESULT).relative_to(layout.reports_dir).as_posix()} under the reports dir",
    ]
