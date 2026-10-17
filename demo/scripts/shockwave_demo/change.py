"""``submit-change``: the SIMULATED Stripe change, its detection and its predicted impact.

The declared APIChange is built from demo/scenarios/...manifest.json (units_changed on GET
/v1/payment_intents/{intent} response.amount, USD only, structural_breaking false) with
declared_change evidence citing the manifest and its fixture equivalences, plus the pinned
contract's openapi evidence for the operation and the amount field's documentation text.
``detect_changes`` runs twice on the baseline discovery and a proposed discovery of the SAME
pinned bytes under the proposed (simulated) snapshot id: without declarations (the schema-only
diff finds nothing, because the published schema is unchanged) and with the declaration.
``analyze_impact`` then runs in rules mode (deterministic; no LLM) against the consumers'
dependencies, matched through the baseline endpoint records, and ``validate_risk_evidence`` must
report no problem. Findings are written per consumer org; the provider gets only a restricted
summary (views.py).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from analyzer import analyze_impact, detect_changes
from analyzer.impact import validate_risk_evidence
from discovery.contracts import AnalysisResult, APIChange, Evidence
from discovery.ids import change_id, evidence_id
from shockwave_demo.common import (
    ANALYSIS_LABEL,
    CHANGE_MANIFEST_PATH,
    IDS_PATH,
    SIMULATED_LABEL,
    DemoError,
    Layout,
    count,
    read_json,
    read_result,
    repo_relative,
    write_json,
)
from shockwave_demo.inventory import discover_stripe, find_endpoint
from shockwave_demo.scan import load_dependencies

CHANGE_RESULT = "change/api-change.json"
DETECT_RESULT = "change/detect-changes.json"
ANALYSIS_RESULT = "change/analysis.json"
MAX_EXCERPT = 1000


def manifest_problems(manifest: Mapping) -> list[str]:
    """The manifest must say what the request requires before it is submitted."""
    problems = []
    expected = {
        "provider": "Stripe",
        "change_status": "simulated",
        "announced_by_provider": False,
        "kind": "units_changed",
    }
    problems += [f"{key} must be {value!r}" for key, value in expected.items() if manifest.get(key) != value]
    if manifest.get("structural_schema", {}).get("changed") is not False:
        problems.append("structural_schema.changed must be false for this bounded scenario")
    if manifest.get("currency_scope") != ["usd"]:
        problems.append("currency_scope must be ['usd']")
    for key in ("old_semantics", "new_semantics", "field_path", "endpoint", "fixtures", "scenarios", "disclaimer"):
        if not manifest.get(key):
            problems.append(f"missing {key}")
    return problems


def build_declared_change(manifest: Mapping, ids: Mapping, endpoint, docs_evidence: Mapping) -> APIChange:
    """The declared APIChange (SIMULATED; the analyzer learns the units change only from this)."""
    manifest_uri = repo_relative(CHANGE_MANIFEST_PATH)
    revision = f"sha256:{hashlib.sha256(CHANGE_MANIFEST_PATH.read_bytes()).hexdigest()}"
    method, path = manifest["endpoint"]["method"], manifest["endpoint"]["path"]
    semantics = Evidence(
        id=evidence_id("stripe-demo/simulated-change-manifest", manifest["manifest_id"], ""),
        kind="declared_change",
        visibility="provider_owned",
        source_uri=manifest_uri,
        revision=revision,
        file=None,
        line_start=None,
        line_end=None,
        json_pointer="",
        excerpt=(
            f"SIMULATED, not announced by Stripe: {method} {path} {manifest['field_path']} ({manifest['kind']}, "
            f"currency scope {', '.join(manifest['currency_scope']).upper()}): {manifest['old_semantics']} -> "
            f"{manifest['new_semantics']}. Published schema unchanged "
            f"({manifest['structural_schema']['published_type']})."
        )[:MAX_EXCERPT],
    )
    fixtures = manifest["fixtures"]
    equivalences = "; ".join(
        f"{item['payment_intent']} {item['baseline_amount']} -> {item['proposed_amount']} ({item['represents']})"
        for item in fixtures["equivalences"]
    )
    fixture_evidence = Evidence(
        id=evidence_id("stripe-demo/simulated-change-manifest", manifest["manifest_id"], "/fixtures/equivalences"),
        kind="declared_change",
        visibility="provider_owned",
        source_uri=manifest_uri,
        revision=revision,
        file=None,
        line_start=None,
        line_end=None,
        json_pointer="/fixtures/equivalences",
        excerpt=(
            f"Equivalent fixtures {fixtures['baseline']['fixture_set_id']} -> {fixtures['proposed']['fixture_set_id']}: "
            f"{equivalences}"
        )[:MAX_EXCERPT],
    )
    operation = [item for item in endpoint.evidence if item.kind == "openapi"]
    return APIChange(
        id=change_id(
            ids["baseline_contract_snapshot_id"],
            ids["proposed_contract_snapshot_id"],
            manifest["service_id"],
            method,
            path,
            manifest["field_path"],
            manifest["kind"],
        ),
        api_project_id=ids["api_project_id"],
        service_id=manifest["service_id"],
        endpoint_id=str(endpoint.id),
        old_snapshot_id=ids["baseline_contract_snapshot_id"],
        new_snapshot_id=ids["proposed_contract_snapshot_id"],
        field_path=manifest["field_path"],
        kind=manifest["kind"],
        old_semantics=manifest["old_semantics"],
        new_semantics=manifest["new_semantics"],
        structural_breaking=manifest["structural_schema"]["changed"],
        evidence=[semantics, fixture_evidence, *operation, Evidence.model_validate(docs_evidence)],
    )


def run_submit_change(layout: Layout) -> list[str]:
    manifest = read_json(CHANGE_MANIFEST_PATH)
    problems = manifest_problems(manifest)
    if problems:
        raise DemoError("the simulated-change manifest is incomplete: " + "; ".join(problems))
    ids = read_json(IDS_PATH)
    inventory = read_result(layout, "inventory.json", produced_by="inventory")
    dependencies = load_dependencies(layout)
    baseline = discover_stripe(ids["baseline_contract_snapshot_id"]).result
    proposed = discover_stripe(ids["proposed_contract_snapshot_id"]).result
    method, path = manifest["endpoint"]["method"], manifest["endpoint"]["path"]
    endpoint = find_endpoint(list(baseline.endpoints), method, path)
    docs = next(item for item in inventory["endpoint"]["evidence"] if item["kind"] == "docs")
    declared = build_declared_change(manifest, ids, endpoint, docs)
    schema_only = detect_changes(baseline, proposed, [], api_project_id=ids["api_project_id"])
    published = detect_changes(baseline, proposed, [declared], api_project_id=ids["api_project_id"])
    matches = [item for item in published if str(item.id) == str(declared.id)]
    if len(matches) != 1:
        raise DemoError("detect_changes did not publish the declared change")
    change = matches[0]
    records = [record for items in dependencies.values() for record in items]
    endpoints = list(baseline.endpoints)
    analysis: AnalysisResult = analyze_impact(change, records, endpoints=endpoints)
    if analysis.analysis_mode != "rules":
        raise DemoError(f"expected deterministic rules analysis, got {analysis.analysis_mode}")
    evidence_problems = validate_risk_evidence(analysis, change, records, endpoints=endpoints)
    if evidence_problems:
        raise DemoError("validate_risk_evidence reported problems: " + "; ".join(evidence_problems))
    write_json(layout.result_path(CHANGE_RESULT), change.model_dump(mode="json"))
    write_json(
        layout.result_path(DETECT_RESULT),
        {
            "kind": "shockwave-demo.detect-changes@1",
            "label": SIMULATED_LABEL,
            "baseline_snapshot_id": ids["baseline_contract_snapshot_id"],
            "proposed_snapshot_id": ids["proposed_contract_snapshot_id"],
            "proposed_contract": (
                "the SAME pinned bytes (sha256 "
                f"{inventory['contract']['sha256']}) discovered under the proposed (simulated) snapshot id: the "
                "published schema is unchanged; the semantics come only from the manifest"
            ),
            "schema_diff_only": [item.model_dump(mode="json") for item in schema_only],
            "schema_diff_only_count": len(schema_only),
            "with_declared_change": [item.model_dump(mode="json") for item in published],
            "note": "A units change is invisible to a schema diff: detect_changes finds 0 structural changes; the "
            "declared change is the only input that names it.",
        },
    )
    write_json(layout.result_path(ANALYSIS_RESULT), analysis.model_dump(mode="json"))
    from shockwave_demo.views import write_views

    write_views(layout)
    lines = [
        f"Submit change - {SIMULATED_LABEL}",
        f"  manifest    {repo_relative(CHANGE_MANIFEST_PATH)} ({manifest['manifest_id']})",
        (
            f"  change      {change.kind} {method} {path} {change.field_path} (USD only; structural_breaking="
            f"{str(change.structural_breaking).lower()}; id {str(change.id)[:8]})"
        ),
        f"  old         {change.old_semantics}",
        f"  new         {change.new_semantics}",
        (
            f"  detect      schema diff alone: {count(len(schema_only), 'change')}; with the declaration: "
            f"{count(len(published), 'change')}"
        ),
        f"  analysis    {ANALYSIS_LABEL}",
        (
            f"              {count(len(analysis.risks), 'predicted risk')}, "
            f"{count(len(analysis.unresolved_dependency_ids), 'unresolved dependency', 'unresolved dependencies')}; "
            "validate_risk_evidence: no problems"
        ),
    ]
    from analyzer.impact import risk_category

    by_snapshot = {
        record.repository_snapshot_id: repository for repository, items in dependencies.items() for record in items
    }
    for risk in analysis.risks:
        lines.append(
            f"    PREDICTED {risk_category(change, risk) or 'unknown':<10} {risk.severity}/{risk.confidence:<8} "
            f"{by_snapshot.get(risk.repository_snapshot_id, '?')} (not executed)"
        )
    lines.append("  wrote       results/change/*.json, consumer findings and the provider restricted summary")
    return lines


def load_change(layout: Layout) -> tuple[APIChange, AnalysisResult]:
    change = APIChange.model_validate(read_result(layout, CHANGE_RESULT, produced_by="submit-change"))
    analysis = AnalysisResult.model_validate(read_result(layout, ANALYSIS_RESULT, produced_by="submit-change"))
    return change, analysis
