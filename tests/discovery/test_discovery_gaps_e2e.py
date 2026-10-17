"""End to end: a contract that was not fully read is unknown, never absent.

The Demo Payments specs are split the standard multi-file way in ``tmp_path`` (a path item
becomes ``$ref: ./paths/<name>.yaml``, and that file is not supplied) and discovered against
the real demo FastAPI source. Parser (unresolved path items), service (gap patterns),
reconcile (not_compared) and ``detect_changes`` (no removal claims) must agree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from analyzer.changes import ProposedContractIncomplete, detect_changes
from discovery import discover_endpoints
from discovery.contracts import APIChange, DiscoveryInput, DiscoveryResult
from discovery.records import (
    PATTERN_UNPARSEABLE,
    PATTERN_UNREAD_DOCUMENT,
    PATTERN_UNRESOLVED_PATH_ITEM,
    FetchedDocument,
    contract_gaps,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "provider" / "demo_payments"
DEMO_SERVICE = DEMO_DIR / "service"
SPECS = {
    "v1": DEMO_DIR / "openapi" / "demo-payments.v1.yaml",
    "v2_proposed": DEMO_DIR / "openapi" / "demo-payments.v2.yaml",
}
DECLARED_CHANGE = DEMO_DIR / "changes" / "charge_amount_cents_to_dollars.json"
FIXTURE_IDS = json.loads((DEMO_DIR / "fixture_ids.json").read_text(encoding="utf-8"))
PROJECT_ID = FIXTURE_IDS["api_project_id"]
CHARGE = "/v1/charges/{id}"
REFUND = "/v1/refunds/{id}"
RECEIPT = "/v1/charges/{id}/receipt"  # served by the source, documented in no spec


def split_path_item(text: str, path: str, target: str) -> str:
    """Replace the path item under ``path`` with ``$ref: <target>`` (the multi-file layout)."""
    lines = text.splitlines(keepends=True)
    start = lines.index(f"  {path}:\n")
    end = start + 1
    while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip()):
        end += 1
    return "".join([*lines[: start + 1], f"    $ref: '{target}'\n", *lines[end:]])


def split_paths_object(text: str, replacement: str = "paths:\n  $ref: './paths.yaml'\n") -> str:
    """Replace the whole Paths Object with a ``$ref`` to another file (or ``replacement``)."""
    return re.sub(r"(?ms)^paths:\n.*?(?=^components:)", replacement, text)


def write_spec(tmp_path: Path, contract: str, text: str) -> Path:
    path = tmp_path / SPECS[contract].name
    path.write_text(text, encoding="utf-8")
    assert not (tmp_path / "paths").exists() and not (tmp_path / "paths.yaml").exists()
    return path


def request(contract: str, ref: str) -> DiscoveryInput:
    return DiscoveryInput(
        service_id=FIXTURE_IDS["service_id"],
        snapshot_id=FIXTURE_IDS["contract_snapshots"][contract],
        openapi_documents=[ref],
        source_snapshot_id=FIXTURE_IDS["source_snapshot_id"],
        frameworks=["fastapi"],
    )


def discover_file(contract: str, spec: Path) -> DiscoveryResult:
    ref = spec.name
    fetched = {ref: FetchedDocument(content=spec.read_bytes(), source_uri=ref)}
    return discover_endpoints(request(contract, ref), fetched, DEMO_SERVICE)


def statuses(result: DiscoveryResult) -> dict[tuple[str, str], str]:
    return {(endpoint.method, endpoint.path): endpoint.reconciliation for endpoint in result.endpoints}


def patterns(result: DiscoveryResult) -> list[str]:
    return list(result.model_dump(mode="json")["coverage"]["unsupported_patterns"])


def declared_change() -> APIChange:
    return APIChange.model_validate_json(DECLARED_CHANGE.read_text(encoding="utf-8"))


def original(contract: str) -> DiscoveryResult:
    return discover_file(contract, SPECS[contract])


def test_the_unsplit_demo_contracts_are_the_control() -> None:
    v1, v2 = original("v1"), original("v2_proposed")
    assert statuses(v1)[("GET", CHARGE)] == "documented_and_found"
    assert statuses(v1)[("GET", RECEIPT)] == "implementation_only"
    assert not contract_gaps(patterns(v1)).covers(CHARGE)
    changes = detect_changes(v1, v2, [declared_change()], api_project_id=PROJECT_ID)
    assert [(change.kind, change.field_path) for change in changes] == [("units_changed", "response.amount")]


def test_a_split_out_path_item_is_not_compared_and_is_a_coverage_gap(tmp_path: Path) -> None:
    text = split_path_item(SPECS["v1"].read_text(encoding="utf-8"), CHARGE, "./paths/charge.yaml")
    result = discover_file("v1", write_spec(tmp_path, "v1", text))
    found = statuses(result)
    assert found[("GET", CHARGE)] == "not_compared"
    assert f"{PATTERN_UNRESOLVED_PATH_ITEM} {CHARGE}" in patterns(result)
    assert contract_gaps(patterns(result)).covers(CHARGE)
    assert not contract_gaps(patterns(result)).documents_incomplete
    # What was read is still compared: the refund path item resolved, the receipt is undocumented.
    assert found[("GET", REFUND)] == "documented_not_found_in_scan"
    assert found[("GET", RECEIPT)] == "implementation_only"
    assert any("operations are unknown, not absent" in warning for warning in result.warnings)


def test_when_every_path_item_is_split_out_no_documented_route_is_implementation_only(
    tmp_path: Path,
) -> None:
    text = SPECS["v1"].read_text(encoding="utf-8")
    text = split_path_item(text, CHARGE, "./paths/charge.yaml")
    text = split_path_item(text, REFUND, "./paths/refund.yaml")
    result = discover_file("v1", write_spec(tmp_path, "v1", text))
    found = statuses(result)
    assert found[("GET", CHARGE)] == "not_compared"
    assert found[("GET", "/healthz")] == "not_compared"  # still outside the documented /v1 namespace
    gaps = contract_gaps(patterns(result))
    assert gaps.covers(CHARGE) and gaps.covers(REFUND)
    # The Paths Object's keys were read, and the receipt is not one of them: it alone stays
    # implementation_only. Every route on a documented path is unknown.
    only = {key for key, status in found.items() if status == "implementation_only"}
    assert only == {("GET", RECEIPT)}
    assert not any(gaps.covers(path) for _method, path in only)


@pytest.mark.parametrize(
    "paths",
    ["paths:\n  $ref: './paths.yaml'\n", "paths:\n  - ./paths.yaml\n"],
    ids=["paths-object-ref", "paths-not-a-mapping"],
)
def test_a_split_out_paths_object_labels_nothing_implementation_only(tmp_path: Path, paths: str) -> None:
    text = split_paths_object(SPECS["v1"].read_text(encoding="utf-8"), paths)
    spec = write_spec(tmp_path, "v1", text)
    result = discover_file("v1", spec)
    assert set(statuses(result).values()) == {"not_compared"}
    assert f"{PATTERN_UNPARSEABLE} {spec.name}" in patterns(result)
    assert contract_gaps(patterns(result)).documents_incomplete


@pytest.mark.parametrize("split", ["charge path item", "every path item", "paths object"])
def test_a_split_proposed_contract_never_claims_a_removal(tmp_path: Path, split: str) -> None:
    text = SPECS["v2_proposed"].read_text(encoding="utf-8")
    if split == "paths object":
        text = split_paths_object(text)
    else:
        text = split_path_item(text, CHARGE, "./paths/charge.yaml")
        if split == "every path item":
            text = split_path_item(text, REFUND, "./paths/refund.yaml")
    v2 = discover_file("v2_proposed", write_spec(tmp_path, "v2_proposed", text))
    with pytest.raises(ProposedContractIncomplete) as raised:
        detect_changes(original("v1"), v2, [declared_change()], api_project_id=PROJECT_ID)
    assert f"GET {CHARGE}" in raised.value.endpoints
    assert str(raised.value.snapshot_id) == str(FIXTURE_IDS["contract_snapshots"]["v2_proposed"])
    if split == "charge path item":
        assert raised.value.endpoints == [f"GET {CHARGE}"]
        assert raised.value.gaps == [f"{PATTERN_UNRESOLVED_PATH_ITEM} {CHARGE}"]
    with pytest.raises(ProposedContractIncomplete):
        detect_changes(original("v1"), v2, [], api_project_id=PROJECT_ID)


def test_a_proposed_document_that_was_never_supplied_is_unread_not_empty() -> None:
    ref = SPECS["v2_proposed"].relative_to(REPO_ROOT).as_posix()
    v2 = discover_endpoints(request("v2_proposed", ref), {}, DEMO_SERVICE)
    assert f"{PATTERN_UNREAD_DOCUMENT} {ref}" in patterns(v2)
    assert set(statuses(v2).values()) == {"not_compared"}
    with pytest.raises(ProposedContractIncomplete) as raised:
        detect_changes(original("v1"), v2, [declared_change()], api_project_id=PROJECT_ID)
    assert raised.value.endpoints == [f"GET {CHARGE}", f"GET {REFUND}"]
    assert raised.value.gaps == [f"{PATTERN_UNREAD_DOCUMENT} {ref}"]
