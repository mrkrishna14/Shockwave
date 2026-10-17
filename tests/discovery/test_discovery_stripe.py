"""Pinned real Stripe OpenAPI: integrity, computed inventory, and parse cost.

Every expected number is recomputed here from the vendored bytes or from pin.json;
nothing about Stripe's inventory is hardcoded. Run with ``-s`` to see measurements.
Nothing in this module touches the network (the refresh tool is checked offline only).
"""

from __future__ import annotations

import collections
import gzip
import hashlib
import importlib.util
import json
import shutil
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from discovery.openapi import MAX_OUTPUT_DEPTH, parse_openapi_document
from discovery.pinned import STRIPE_PIN_PATH, PinnedDocumentError, load_pinned_document
from discovery.records import (
    ORIGIN_KEY,
    REF_DIGEST_KEY,
    REF_STATUS_KEY,
    ExpansionLimits,
    FetchedDocument,
    ParsedDocument,
)

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MAX_PARSE_SECONDS = 20.0
MAX_TOTAL_SCHEMA_BYTES = 40_000_000
# A document_ref no other test uses, so the timed parse below is never a cache hit.
DOCUMENT_REF = "stripe-openapi:test_discovery_stripe"


@pytest.fixture(scope="module")
def pin() -> dict[str, Any]:
    return json.loads(STRIPE_PIN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stripe() -> tuple[FetchedDocument, dict[str, Any], ParsedDocument, float]:
    fetched = load_pinned_document(STRIPE_PIN_PATH)
    raw = json.loads(fetched.content)  # independent view used to compute expectations
    start = time.perf_counter()
    parsed = parse_openapi_document(DOCUMENT_REF, fetched)
    return fetched, raw, parsed, time.perf_counter() - start


def strict_resolve(document: Any, pointer: str) -> Any:
    """RFC 6901 resolution that does not follow ``$ref`` (pointers must be literal)."""
    node = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[token] if isinstance(node, dict) else node[int(token)]
    return node


def schema_nodes(parsed: ParsedDocument) -> list[tuple[dict, int]]:
    """Every dict in every request/response schema, with its container level (root: 1)."""
    found: list[tuple[dict, int]] = []
    for op in parsed.operations:
        stack: list[tuple[Any, int]] = [(op.request_schema, 1), (op.response_schema, 1)]
        while stack:
            node, level = stack.pop()
            if isinstance(node, dict):
                found.append((node, level))
                stack.extend((child, level + 1) for child in node.values())
            elif isinstance(node, list):
                stack.extend((child, level + 1) for child in node)
    return found


def preserved_refs(parsed: ParsedDocument) -> collections.Counter[str]:
    counts: collections.Counter[str] = collections.Counter()
    for op in parsed.operations:
        stack: list[Any] = [op.request_schema, op.response_schema]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if REF_STATUS_KEY in node:
                    counts[node[REF_STATUS_KEY]] += 1
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return counts


# Integrity and computed inventory ----------------------------------------------------------


def test_pinned_document_matches_its_pin(stripe: tuple, pin: dict[str, Any]) -> None:
    fetched, _raw, _parsed, _elapsed = stripe
    repository = pin["source"]["repository"].split("://", 1)[-1].split("/", 1)[1].removesuffix(".git")
    assert fetched.source_uri == pin["source"]["raw_url"]
    assert fetched.revision_label == f"{repository}@{pin['source']['commit'][:12]}"
    assert fetched.visibility == pin["visibility"]
    assert fetched.media_type == pin["content"]["media_type"]
    assert len(fetched.content) == pin["content"]["byte_size"]
    assert hashlib.sha256(fetched.content).hexdigest() == pin["content"]["sha256"]


def test_inventory_and_provenance_are_computed_from_the_document(
    stripe: tuple, pin: dict[str, Any]
) -> None:
    fetched, raw, parsed, _elapsed = stripe
    expected_operations = {
        (method.upper(), path) for path, item in raw["paths"].items() for method in item if method in HTTP_METHODS
    }
    provenance = parsed.provenance
    assert provenance.operation_count == len(parsed.operations) == len(expected_operations)
    assert {(op.method, op.documented_path) for op in parsed.operations} == expected_operations
    assert provenance.path_count == len(raw["paths"])
    assert provenance.component_schema_count == len(raw["components"]["schemas"])
    assert provenance.openapi_version == raw["openapi"]
    assert provenance.openapi_version.startswith("3.")
    assert provenance.info_version == raw["info"]["version"]
    assert provenance.info_title == raw["info"]["title"]
    assert provenance.sha256 == pin["content"]["sha256"]
    assert provenance.byte_size == pin["content"]["byte_size"]
    assert provenance.source_uri == pin["source"]["raw_url"]
    assert provenance.revision_label == fetched.revision_label
    assert provenance.visibility == "public"
    assert provenance.server_urls == tuple(server["url"] for server in raw.get("servers", []))
    assert provenance.document_ref == DOCUMENT_REF


def test_every_operation_json_pointer_resolves_in_the_raw_document(stripe: tuple) -> None:
    _fetched, raw, parsed, _elapsed = stripe
    for op in parsed.operations:
        target = strict_resolve(raw, op.json_pointer)
        assert isinstance(target, dict), op.json_pointer
        assert target.get("operationId") == op.operation_id


def test_retrieve_charge_comes_straight_from_the_document(stripe: tuple) -> None:
    _fetched, raw, parsed, _elapsed = stripe
    raw_operation = raw["paths"]["/v1/charges/{charge}"]["get"]
    (operation,) = [
        op for op in parsed.operations if op.method == "GET" and op.documented_path == "/v1/charges/{charge}"
    ]
    assert operation.operation_id == raw_operation["operationId"]
    raw_schema = raw_operation["responses"]["200"]["content"]["application/json"]["schema"]
    response = operation.response_schema
    assert response[ORIGIN_KEY] == raw_schema["$ref"][1:]
    target = strict_resolve(raw, raw_schema["$ref"][1:])
    assert response["properties"]["amount"]["type"] == target["properties"]["amount"]["type"]
    assert response["properties"]["amount"]["description"] == target["properties"]["amount"]["description"]


def test_stripe_has_no_remote_references(stripe: tuple) -> None:
    _fetched, _raw, parsed, _elapsed = stripe
    assert not [pattern for pattern in parsed.unsupported_patterns if pattern.startswith("openapi:remote-ref")]


def test_stripe_parse_cost_and_catalog_size(stripe: tuple) -> None:
    fetched, _raw, parsed, elapsed = stripe
    request_bytes = [len(json.dumps(op.request_schema)) for op in parsed.operations]
    response_bytes = [len(json.dumps(op.response_schema)) for op in parsed.operations]
    total = sum(request_bytes) + sum(response_bytes)
    start = time.perf_counter()
    cached = parse_openapi_document(DOCUMENT_REF, fetched)
    cache_hit_seconds = time.perf_counter() - start
    statuses = dict(sorted(preserved_refs(parsed).items()))
    print(
        f"\n[stripe] cold parse {elapsed:.2f}s | cache hit {cache_hit_seconds:.2f}s"
        f" | operations {len(parsed.operations)}"
        f" | total request+response schema JSON {total / 1e6:.1f} MB"
        f" (requests {sum(request_bytes) / 1e6:.1f} MB)"
        f" | largest response_schema {max(response_bytes) / 1e3:.0f} KB"
        f" | preserved $refs by status {statuses}"
    )
    assert elapsed < MAX_PARSE_SECONDS
    assert total < MAX_TOTAL_SCHEMA_BYTES
    assert cached == parsed


def test_stripe_needs_none_of_the_document_wide_bounds(stripe: tuple) -> None:
    fetched, _raw, parsed, _elapsed = stripe
    # The document budget and operation cap are safety bounds: Stripe must not touch them.
    relaxed = parse_openapi_document(
        DOCUMENT_REF + ":relaxed",
        fetched,
        limits=ExpansionLimits(max_document_nodes=10**9, max_operations=10**9),
    )
    def schemas(result: ParsedDocument) -> list[tuple]:
        return [(op.method, op.path, op.request_schema, op.response_schema) for op in result.operations]

    assert schemas(relaxed) == schemas(parsed)
    assert relaxed.warnings == parsed.warnings
    assert not any("document-wide" in warning or "output depth" in warning for warning in parsed.warnings)
    assert parsed.unresolved_paths == [] and parsed.warnings_omitted == parsed.patterns_omitted == 0


def test_stripe_references_carry_target_fingerprints(stripe: tuple) -> None:
    _fetched, _raw, parsed, _elapsed = stripe
    nodes = schema_nodes(parsed)
    kept = [node for node, _level in nodes if REF_STATUS_KEY in node]
    inlined = [node for node, _level in nodes if ORIGIN_KEY in node and REF_STATUS_KEY not in node]
    assert kept and inlined
    # Stripe has only local references, so every kept or inlined one has a fingerprint.
    assert all(str(node.get(REF_DIGEST_KEY, "")).startswith("sha256:") for node in kept + inlined)
    assert max(level for _node, level in nodes) < MAX_OUTPUT_DEPTH // 4  # far from the bound


# Tampering -------------------------------------------------------------------------------------


@pytest.fixture
def pin_copy(tmp_path: Path, pin: dict[str, Any]) -> Path:
    for name in (STRIPE_PIN_PATH.name, pin["vendored_file"]):
        shutil.copyfile(STRIPE_PIN_PATH.with_name(name), tmp_path / name)
    return tmp_path / STRIPE_PIN_PATH.name


def rewrite(pin_path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    data = json.loads(pin_path.read_text(encoding="utf-8"))
    change(data)
    pin_path.write_text(json.dumps(data), encoding="utf-8")


def test_an_intact_copy_still_loads(pin_copy: Path) -> None:
    assert load_pinned_document(pin_copy).content == load_pinned_document(STRIPE_PIN_PATH).content


def test_wrong_sha256_is_rejected(pin_copy: Path) -> None:
    rewrite(pin_copy, lambda data: data["content"].update(sha256="0" * 64))
    with pytest.raises(PinnedDocumentError, match="sha256"):
        load_pinned_document(pin_copy)


def test_truncated_gzip_is_rejected(pin_copy: Path, pin: dict[str, Any]) -> None:
    vendored = pin_copy.with_name(pin["vendored_file"])
    data = vendored.read_bytes()
    vendored.write_bytes(data[: len(data) // 2])
    with pytest.raises(PinnedDocumentError, match="truncated"):
        load_pinned_document(pin_copy)


def test_wrong_byte_size_is_rejected(pin_copy: Path) -> None:
    rewrite(pin_copy, lambda data: data["content"].update(byte_size=data["content"]["byte_size"] + 1))
    with pytest.raises(PinnedDocumentError, match="bytes"):
        load_pinned_document(pin_copy)


def test_decompression_stops_at_the_pinned_size(tmp_path: Path, pin: dict[str, Any]) -> None:
    bomb = tmp_path / "bomb.json.gz"
    bomb.write_bytes(gzip.compress(b" " * (16 * 1024 * 1024), mtime=0))  # ~16 KB compressed
    # 64 KiB pinned: the compressed file passes the read cap, so decompression is what stops.
    small = dict(pin, vendored_file=bomb.name, content=dict(pin["content"], byte_size=64 * 1024))
    (tmp_path / "pin.json").write_text(json.dumps(small), encoding="utf-8")
    start = time.perf_counter()
    with pytest.raises(PinnedDocumentError, match="beyond the pinned byte_size"):
        load_pinned_document(tmp_path / "pin.json")
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["source"].pop("raw_url"),
        lambda data: data["source"].update(commit="not-a-commit"),
        lambda data: data["source"].update(raw_url="https://example.test/latest/openapi.spec3.json"),
        lambda data: data.update(visibility="secret"),
        lambda data: data.update(vendored_file="../pin.json"),
        lambda data: data.update(vendored_file="missing.json.gz"),
        lambda data: data.update(vendored_encoding="zip"),
        lambda data: data["content"].update(byte_size="4663536"),
        lambda data: data.pop("content"),
    ],
)
def test_malformed_pins_are_rejected(pin_copy: Path, change: Callable[[dict[str, Any]], None]) -> None:
    rewrite(pin_copy, change)
    with pytest.raises(PinnedDocumentError):
        load_pinned_document(pin_copy)


def test_unreadable_pin_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PinnedDocumentError):
        load_pinned_document(tmp_path / "absent.json")
    (tmp_path / "pin.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PinnedDocumentError, match="not valid JSON"):
        load_pinned_document(tmp_path / "pin.json")


# Refresh tool (offline checks only) ---------------------------------------------------------


@pytest.fixture(scope="module")
def refresh_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "stripe_refresh_pin_tool", STRIPE_PIN_PATH.with_name("refresh_pin.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_refresh_tool_validates_inputs_before_any_fetch(
    refresh_tool: ModuleType, pin: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_fetch(_url: str) -> bytes:
        raise AssertionError("validation must fail before any download")

    monkeypatch.setattr(refresh_tool, "fetch", no_fetch)
    commit = pin["source"]["commit"]
    assert refresh_tool.validate_commit(commit.upper()) == commit
    for bad in ("abc", "g" * 40, "1" * 39, "1" * 41):
        with pytest.raises(refresh_tool.RefreshError):
            refresh_tool.validate_commit(bad)
    assert refresh_tool.validate_spec_path(pin["source"]["path"]) == pin["source"]["path"]
    for bad in ("../x.json", "/abs.json", "latest/spec.yaml", "a//b.json", "a/../b.json"):
        with pytest.raises(refresh_tool.RefreshError):
            refresh_tool.validate_spec_path(bad)
    assert refresh_tool.main(["--commit", "xyz", "--pin", str(STRIPE_PIN_PATH)]) == 1
    assert refresh_tool.raw_url(commit, pin["source"]["path"]) == pin["source"]["raw_url"]


def test_refresh_tool_only_allows_the_raw_github_host(refresh_tool: ModuleType) -> None:
    refresh_tool.check_url("https://raw.githubusercontent.com/stripe/openapi/abc/latest/x.json")
    for bad in (
        "http://raw.githubusercontent.com/stripe/openapi/abc/x.json",
        "https://raw.githubusercontent.com.evil.test/x.json",
        "https://user@raw.githubusercontent.com/x.json",
        "https://raw.githubusercontent.com:8443/x.json",
        "https://evil.test/x.json",
        "file:///etc/passwd",
    ):
        with pytest.raises(refresh_tool.RefreshError):
            refresh_tool.check_url(bad)
    handler = refresh_tool._AllowlistRedirectHandler()
    request = urllib.request.Request("https://raw.githubusercontent.com/stripe/openapi/abc/x.json")
    with pytest.raises(refresh_tool.RefreshError):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.test/x.json")


def test_refresh_tool_rewrites_pin_fields_without_mutating_the_input(
    refresh_tool: ModuleType, pin: dict[str, Any]
) -> None:
    original = json.loads(json.dumps(pin))
    body = b'{"openapi": "3.0.0"}'
    refresh_tool.check_document(body)
    with pytest.raises(refresh_tool.RefreshError):
        refresh_tool.check_document(b'{"swagger": "2.0"}')
    updated = refresh_tool.updated_pin(
        pin, commit="a" * 40, spec_path="latest/openapi.spec3.json", body=body, retrieved_at="2026-01-01T00:00:00Z"
    )
    assert pin == original
    assert updated["source"]["commit"] == "a" * 40
    assert updated["source"]["raw_url"].endswith(f"/{'a' * 40}/latest/openapi.spec3.json")
    assert updated["source"]["commit_message"] is None  # stale metadata is cleared, not kept
    assert updated["content"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert updated["content"]["byte_size"] == len(body)
    assert updated["retrieved_at"] == "2026-01-01T00:00:00Z"
