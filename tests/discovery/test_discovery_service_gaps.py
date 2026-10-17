"""discover(): coverage-gap entries, exact "N more" counts, and credential-free references.

``analyzer.changes`` reads the coverage-gap vocabulary of ``discovery.records`` to avoid
claiming that an endpoint the proposed contract could not describe was removed, so these
tests pin that every unread part of a contract is reported and that no cap drops it. Parser
results with unresolved path items are constructed (``dataclasses.replace`` over a real
parse), so the service is pinned independently of how the parser finds them.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from discovery import service
from discovery.contracts import DiscoveryInput, DiscoveryResult
from discovery.inventory import summarize_inventory
from discovery.records import (
    PATTERN_NO_DOCUMENTS,
    PATTERN_UNPARSEABLE,
    PATTERN_UNREAD_DOCUMENT,
    PATTERN_UNRESOLVED_PATH_ITEM,
    FetchedDocument,
    ParsedDocument,
    contract_gaps,
)
from discovery.service import MAX_DOCUMENT_WARNINGS, MAX_UNSUPPORTED_PATTERNS, discover

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "provider" / "demo_payments"
DEMO_SERVICE = DEMO_DIR / "service"
SPEC_V1 = DEMO_DIR / "openapi" / "demo-payments.v1.yaml"
FIXTURE_IDS = json.loads((DEMO_DIR / "fixture_ids.json").read_text(encoding="utf-8"))
SERVICE_ID = FIXTURE_IDS["service_id"]
V1 = FIXTURE_IDS["contract_snapshots"]["v1"]
SOURCE_SNAPSHOT = FIXTURE_IDS["source_snapshot_id"]
V1_REF = "demo-payments.v1.yaml"
CHARGE_PATH = "/v1/charges/{id}"


def request(*refs: str, frameworks: tuple[str, ...] = ()) -> DiscoveryInput:
    return DiscoveryInput(
        service_id=SERVICE_ID,
        snapshot_id=V1,
        openapi_documents=list(refs),
        source_snapshot_id=SOURCE_SNAPSHOT if frameworks else None,
        frameworks=list(frameworks),
    )


def patterns_of(result: DiscoveryResult) -> list[str]:
    return result.model_dump(mode="json")["coverage"]["unsupported_patterns"]


def statuses(result: DiscoveryResult) -> dict[tuple[str, str], str]:
    return {(e.method, e.path): e.reconciliation for e in result.endpoints}


def parsed_as(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[ParsedDocument], ParsedDocument]
) -> None:
    """Make discover() see ``change(real parse)`` for every document it parses."""
    real = service.parse_openapi_document

    def parse(ref: str, fetched: FetchedDocument, **kwargs: object) -> ParsedDocument:
        return change(real(ref, fetched, **kwargs))

    monkeypatch.setattr(service, "parse_openapi_document", parse)


def charges_unresolved(parsed: ParsedDocument) -> ParsedDocument:
    """The demo spec split: the parser could not resolve the /v1/charges/{id} path item."""
    return dataclasses.replace(
        parsed,
        operations=[op for op in parsed.operations if op.path != CHARGE_PATH],
        unresolved_paths=[CHARGE_PATH],
        unsupported_patterns=[
            *parsed.unsupported_patterns,
            f"{PATTERN_UNRESOLVED_PATH_ITEM} {CHARGE_PATH}",
        ],
    )


# Coverage-gap entries -------------------------------------------------------------------


def test_every_requested_document_that_was_not_read_is_a_coverage_gap() -> None:
    outcome = discover(
        request("missing.yaml", "broken.yaml", "local-path.yaml", "  ", V1_REF),
        {
            "broken.yaml": b"openapi: 3.0.3\npaths: [unclosed\n",
            "local-path.yaml": "local-path.yaml",  # a path, not fetched bytes: never opened
            V1_REF: SPEC_V1.read_bytes(),
        },
        None,
    )

    assert patterns_of(outcome.result) == sorted(
        [
            f"{PATTERN_UNREAD_DOCUMENT} missing.yaml",
            f"{PATTERN_UNPARSEABLE} broken.yaml",
            f"{PATTERN_UNREAD_DOCUMENT} local-path.yaml",
            f"{PATTERN_UNREAD_DOCUMENT} (blank)",
        ]
    )
    gaps = contract_gaps(patterns_of(outcome.result))
    assert gaps.documents_incomplete and gaps.covers(CHARGE_PATH)


def test_a_request_without_documents_is_a_coverage_gap() -> None:
    outcome = discover(request(frameworks=("fastapi",)), {}, DEMO_SERVICE)

    assert PATTERN_NO_DOCUMENTS in patterns_of(outcome.result)
    assert contract_gaps(patterns_of(outcome.result)).documents_incomplete
    assert "implementation_only" not in statuses(outcome.result).values()


def test_a_fully_read_contract_reports_no_gap() -> None:
    outcome = discover(
        request(V1_REF, frameworks=("fastapi",)), {V1_REF: SPEC_V1.read_bytes()}, DEMO_SERVICE
    )

    gaps = contract_gaps(patterns_of(outcome.result))
    assert not gaps.documents_incomplete and not gaps.covers(CHARGE_PATH)


def test_coverage_gaps_survive_the_pattern_cap() -> None:
    names = tuple(f"framework{index:03d}" for index in range(MAX_UNSUPPORTED_PATTERNS + 50))
    outcome = discover(
        request("missing.yaml", V1_REF, frameworks=names), {V1_REF: SPEC_V1.read_bytes()}, None
    )

    patterns = patterns_of(outcome.result)
    assert f"{PATTERN_UNREAD_DOCUMENT} missing.yaml" in patterns
    assert len(patterns) == MAX_UNSUPPORTED_PATTERNS + 1
    others = len(names)  # one framework:unsupported entry per name
    assert patterns[-1] == f"... and {others - (MAX_UNSUPPORTED_PATTERNS - 1)} more"


# Unresolved path items ------------------------------------------------------------------


def test_an_unresolved_path_item_is_a_gap_and_its_route_is_not_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed_as(monkeypatch, charges_unresolved)
    outcome = discover(
        request(V1_REF, frameworks=("fastapi",)), {V1_REF: SPEC_V1.read_bytes()}, DEMO_SERVICE
    )

    assert statuses(outcome.result) == {
        ("GET", "/healthz"): "not_compared",
        ("GET", CHARGE_PATH): "not_compared",  # documented, operations unknown
        ("GET", "/v1/charges/{id}/receipt"): "implementation_only",  # never documented
        ("GET", "/v1/refunds/{id}"): "documented_not_found_in_scan",
    }
    assert f"{PATTERN_UNRESOLVED_PATH_ITEM} {CHARGE_PATH}" in patterns_of(outcome.result)
    gaps = contract_gaps(patterns_of(outcome.result))
    assert gaps.covers(CHARGE_PATH) and not gaps.covers("/v1/refunds/{id}")
    assert any("path item could not be resolved" in w for w in outcome.result.warnings)


def test_unresolved_path_items_the_parser_could_not_list_are_still_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"/v1/things{index}/{{id}}" for index in range(3)]
    parsed_as(
        monkeypatch,
        lambda parsed: dataclasses.replace(
            parsed,
            unresolved_paths=paths,
            # The parser's own cap listed one of them and left 2 + 5 other patterns out.
            unsupported_patterns=[f"{PATTERN_UNRESOLVED_PATH_ITEM} {paths[0]}"],
            patterns_omitted=7,
        ),
    )
    outcome = discover(request(V1_REF), {V1_REF: SPEC_V1.read_bytes()}, None)

    assert patterns_of(outcome.result) == [
        *sorted(f"{PATTERN_UNRESOLVED_PATH_ITEM} {path}" for path in paths),
        "... and 5 more",
    ]


def test_too_many_unresolved_path_items_collapse_into_one_unread_document_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"/v1/things{index:04d}/{{id}}" for index in range(MAX_UNSUPPORTED_PATTERNS + 1)]
    parsed_as(
        monkeypatch,
        lambda parsed: dataclasses.replace(
            parsed,
            unresolved_paths=paths,
            unsupported_patterns=[f"{PATTERN_UNRESOLVED_PATH_ITEM} {path}" for path in paths],
        ),
    )
    outcome = discover(
        request(V1_REF, frameworks=("django",)), {V1_REF: SPEC_V1.read_bytes()}, None
    )

    assert patterns_of(outcome.result) == [
        "framework:unsupported django",
        f"{PATTERN_UNREAD_DOCUMENT} {V1_REF}",
    ]
    assert contract_gaps(patterns_of(outcome.result)).covers(paths[-1])
    (note,) = [w for w in outcome.result.warnings if "recorded as one" in w]
    assert note.startswith(f"openapi document {V1_REF}: its {len(paths)} unresolved path item(s)")


def test_an_unresolved_path_that_cannot_be_shown_verbatim_marks_the_document_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    odd = "/v1/odd" + chr(0x202E) + "path"  # a bidi override: shown cleaned, it would not match
    parsed_as(monkeypatch, lambda parsed: dataclasses.replace(parsed, unresolved_paths=[odd]))
    outcome = discover(request(V1_REF), {V1_REF: SPEC_V1.read_bytes()}, None)

    assert patterns_of(outcome.result) == [f"{PATTERN_UNREAD_DOCUMENT} {V1_REF}"]
    assert contract_gaps(patterns_of(outcome.result)).covers(odd)
    assert any("cannot be listed verbatim" in w for w in outcome.result.warnings)


# Exact "N more" counts ------------------------------------------------------------------


def test_hidden_parser_warnings_and_patterns_are_counted_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extra, omitted = 10, 801  # past the service's per-document cap, and past the parser's
    warnings = [f"openapi: warning {index}" for index in range(MAX_DOCUMENT_WARNINGS + extra)]
    parsed_as(
        monkeypatch,
        lambda parsed: dataclasses.replace(
            parsed,
            warnings=warnings,
            warnings_omitted=omitted,
            unsupported_patterns=["openapi:remote-ref a.yaml", "openapi:remote-ref b.yaml"],
            patterns_omitted=300,
        ),
    )
    outcome = discover(request(V1_REF), {V1_REF: SPEC_V1.read_bytes()}, None)
    shown = outcome.result.warnings

    prefix = f"openapi document {V1_REF}: "
    assert len([w for w in shown if w.startswith(f"{prefix}warning ")]) == MAX_DOCUMENT_WARNINGS
    assert f"{prefix}{extra + omitted} more parser warning(s) not shown" in shown
    assert not any(w.startswith("... ") for w in shown)  # nothing else was dropped
    assert patterns_of(outcome.result) == [
        "openapi:remote-ref a.yaml",
        "openapi:remote-ref b.yaml",
        "... and 300 more",
    ]


def test_the_final_count_includes_warnings_behind_a_dropped_overflow_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit, extra, omitted = 20, 10, 5
    total = MAX_DOCUMENT_WARNINGS + extra + omitted  # parser warnings the document triggered
    warnings = [f"openapi: warning {index}" for index in range(MAX_DOCUMENT_WARNINGS + extra)]
    parsed_as(
        monkeypatch,
        lambda parsed: dataclasses.replace(parsed, warnings=warnings, warnings_omitted=omitted),
    )
    monkeypatch.setattr(service, "MAX_WARNINGS", limit)
    outcome = discover(request(V1_REF), {V1_REF: SPEC_V1.read_bytes()}, None)
    shown = outcome.result.warnings

    assert len(shown) == limit
    assert shown[-1] == f"... {total - (limit - 1)} more warning(s) not shown"


# Credential-free references -------------------------------------------------------------


SECRET_REF = (
    "https://ci-bot:SECRET-USERINFO@specs.example/demo-payments.yaml"
    "?access_token=SECRET-QUERY#SECRET-FRAGMENT"
)
SHOWN_REF = "https://specs.example/demo-payments.yaml"


@pytest.mark.parametrize(
    "supplied",
    [
        SPEC_V1.read_bytes(),
        bytearray(SPEC_V1.read_bytes()),
        FetchedDocument(SPEC_V1.read_bytes(), source_uri=""),
        FetchedDocument(SPEC_V1.read_bytes(), source_uri=SECRET_REF),
    ],
    ids=["bytes", "bytearray", "fetched-without-uri", "fetched-with-secret-uri"],
)
def test_credential_bearing_references_never_reach_results_warnings_or_summaries(
    supplied: object,
) -> None:
    missing = "https://u:SECRET-MISSING@specs.example/missing.yaml?sig=SECRET-MISSING"
    broken = "https://u:SECRET-BROKEN@specs.example/broken.yaml?sig=SECRET-BROKEN"
    unlisted = "https://u:SECRET-UNLISTED@specs.example/unlisted.yaml?sig=SECRET-UNLISTED"
    outcome = discover(
        request(SECRET_REF, SECRET_REF, missing, broken, frameworks=("fastapi",)),
        {SECRET_REF: supplied, broken: b"openapi: [", unlisted: b"{}"},
        DEMO_SERVICE,
    )
    clean = discover(
        request(SHOWN_REF, frameworks=("fastapi",)), {SHOWN_REF: SPEC_V1.read_bytes()}, DEMO_SERVICE
    )

    dumped = json.dumps(outcome.result.model_dump(mode="json"))
    summary = json.dumps(summarize_inventory(outcome))
    assert "SECRET" not in dumped and "SECRET" not in summary
    charge = next(e for e in outcome.result.endpoints if e.path == CHARGE_PATH)
    assert charge.evidence[0].source_uri == SHOWN_REF
    # Evidence identity comes from the content hash, so it is the same as for a clean URI.
    clean_charge = next(e for e in clean.result.endpoints if e.path == CHARGE_PATH)
    assert [item.id for item in charge.evidence] == [item.id for item in clean_charge.evidence]
    assert f"{PATTERN_UNREAD_DOCUMENT} https://specs.example/missing.yaml" in patterns_of(
        outcome.result
    )
    assert f"openapi document {SHOWN_REF} is listed more than once; it was read once" in (
        outcome.result.warnings
    )
    assert outcome.documents[0].document_ref == SECRET_REF  # internal lookup key only
