"""Dashboard view documents: consumer-private findings and the provider's RESTRICTED summary.

``results/consumers/<repository>/findings.json`` (kind ``shockwave-demo.consumer-findings@1``,
visibility consumer_private) is everything one consumer org may see about its own repository:
predicted risks with their evidence, their verification results and run ids, migration
validation, controls, supporting observations, unresolved dependencies, and the unaffected
dependencies listed separately with the reason they are unaffected.

``results/provider/restricted-summary.json`` (kind ``shockwave-demo.provider-restricted-summary@1``,
visibility provider_restricted) holds, per pseudonymous consumer label, only counts by risk
category, severity and verification status. It never contains consumer source, file names,
paths, line numbers, snippets, logs, patches, commit SHAs, ids of consumer records, or
organization and repository names.

Both are recomputed from the stage outputs after submit-change, execute and validate-migrations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from analyzer.impact import risk_category
from analyzer.rules import source_location
from discovery.contracts import AnalysisResult, APIChange, Dependency
from discovery.ids import stable_id
from shockwave_demo.common import (
    ANALYSIS_LABEL,
    CHANGE_MANIFEST_PATH,
    IDS_PATH,
    ISOLATION_LABEL,
    RUNNER_LABEL,
    SIMULATED_LABEL,
    Layout,
    read_json,
    write_json,
)

PROVIDER_SUMMARY_RESULT = "provider/restricted-summary.json"
PROVIDER_EXCLUDED = (
    "consumer source code and snippets",
    "file names, paths and line numbers",
    "logs and test output",
    "patches and migration artifacts",
    "commit SHAs and consumer record ids",
    "consumer organization and repository names",
)


def findings_result(repository: str) -> str:
    return f"consumers/{repository}/findings.json"


def _optional(layout: Layout, relative: str):
    path = layout.result_path(relative)
    return read_json(path) if path.is_file() else None


def _unaffected_reason(change: APIChange, dependency: Dependency) -> str:
    if dependency.field_path != change.field_path:
        return f"uses {dependency.field_path}; the simulated change affects {change.field_path} only"
    if str(dependency.endpoint_id) != str(change.endpoint_id):
        return "a different endpoint than the changed one"
    return "no impact rule matched this usage"


def _risk_entry(
    change: APIChange,
    risk,
    dependencies: Mapping[str, Dependency],
    result: Mapping | None,
    plan: Mapping | None,
    run_ids: list[str],
    migration: Mapping | None,
) -> dict:
    locations = [
        source_location(dependencies[str(item)]) for item in risk.dependency_ids if str(item) in dependencies
    ]
    if result is None:
        verification = {
            "status": "not_run",
            "summary": "Prediction only: verification has not been executed yet (run `demo.py execute`).",
        }
    else:
        verification = {
            "status": result["status"],
            "summary": result["summary"],
            "result_id": result["id"],
            "scenario_id": result["scenario_id"],
            "plan_id": None if plan is None else plan["id"],
            "runner_profile": None if plan is None else plan["runner_profile"],
            "runner": RUNNER_LABEL if plan is not None else None,
            "baseline": result["baseline"],
            "proposed": result["proposed"],
            "runs": run_ids,
        }
    return {
        "risk_id": str(risk.id),
        "category": risk_category(change, risk),
        "severity": risk.severity,
        "confidence": risk.confidence,
        "state": "predicted" if result is None or result["status"] == "not_run" else "executed",
        "locations": [item for item in locations if item],
        "explanation": risk.explanation,
        "suggested_action": risk.suggested_action,
        "verification": verification,
        "migration": None
        if migration is None
        else {
            "migration_id": migration["migration_id"],
            "status": migration["status"],
            "patch": migration["patch"]["patch"],
            "patched_proposed": migration["patched_proposed"]["status"],
            "deployment_assumption": migration["deployment_assumption"]["recorded_as"],
            "own_suite": migration["own_suite"]["outcome"],
            "deployment_caveat": migration["deployment_caveat"],
            "runs": [
                migration["patched_proposed"]["run_id"],
                migration["deployment_assumption"]["run_id"],
                migration["own_suite"]["run_id"],
            ],
        },
        "risk": risk.model_dump(mode="json"),
    }


def write_views(layout: Layout) -> None:
    change_doc = _optional(layout, "change/api-change.json")
    analysis_doc = _optional(layout, "change/analysis.json")
    snapshots = _optional(layout, "snapshots.json")
    if change_doc is None or analysis_doc is None or snapshots is None:
        return
    ids = read_json(IDS_PATH)
    change = APIChange.model_validate(change_doc)
    analysis = AnalysisResult.model_validate(analysis_doc)
    results = {item["risk_id"]: item for item in _optional(layout, "verification/results.json") or []}
    plans = {item["risk_id"]: item for item in _optional(layout, "verification/plans.json") or []}
    controls = (_optional(layout, "verification/controls.json") or {}).get("controls", [])
    observations = (_optional(layout, "verification/observations.json") or {}).get("observations", [])
    validation = _optional(layout, "migrations/validation.json") or {}
    migrations = {item["risk_id"]: item for item in validation.get("migrations", [])}
    run_ids: dict[str, list[str]] = {}
    for path in sorted((layout.results_dir / "runs").glob("execute.*.json")):
        record = read_json(path)
        if record.get("plan_id"):
            run_ids.setdefault(record["plan_id"], []).append(record["run_id"])
    provider_rows = []
    for repository, details in snapshots["repositories"].items():
        deps_doc = _optional(layout, f"consumers/{repository}/dependencies.json") or {"dependencies": []}
        dependencies = {item["id"]: Dependency.model_validate(item) for item in deps_doc["dependencies"]}
        snapshot_id = details["repository_snapshot_id"]
        risks = [risk for risk in analysis.risks if str(risk.repository_snapshot_id) == snapshot_id]
        in_risks = {str(item) for risk in risks for item in risk.dependency_ids}
        unresolved_ids = {str(item) for item in analysis.unresolved_dependency_ids}
        consumer = ids["consumers"][repository]
        entries = [
            _risk_entry(
                change,
                risk,
                dependencies,
                results.get(str(risk.id)),
                plans.get(str(risk.id)),
                run_ids.get(plans.get(str(risk.id), {}).get("id", ""), []),
                migrations.get(str(risk.id)),
            )
            for risk in risks
        ]
        document = {
            "kind": "shockwave-demo.consumer-findings@1",
            "visibility": "consumer_private",
            "visibility_note": (
                "Private to this consumer organization: it contains the repository's source locations, excerpts, "
                "run evidence and migration references. The provider sees only the restricted summary."
            ),
            "audience": {
                "consumer_org_id": consumer["consumer_org_id"],
                "consumer_org_name": consumer["consumer_org_name"],
                "consumer_org_label": consumer["consumer_org_label"],
            },
            "repository": {
                "name": repository,
                "commit_sha": details["commit_sha"],
                "repository_snapshot_id": snapshot_id,
                "repository_connection_id": details["repository_connection_id"],
                "enrollment_id": details["enrollment_id"],
            },
            "change": _change_view(change),
            "dependency_source": deps_doc.get("source"),
            "analysis": {"mode": analysis.analysis_mode, "label": ANALYSIS_LABEL, "warnings": analysis.warnings},
            "risks": entries,
            "unresolved": [
                {"dependency_id": key, "location": source_location(dep), "field_path": dep.field_path}
                for key, dep in dependencies.items()
                if key in unresolved_ids
            ],
            "unaffected": [
                {
                    "dependency_id": key,
                    "field_path": dep.field_path,
                    "usage": dep.usage.operation,
                    "location": source_location(dep),
                    "reason": _unaffected_reason(change, dep),
                }
                for key, dep in dependencies.items()
                if key not in in_risks and key not in unresolved_ids
            ],
            "negative_controls": deps_doc.get("negative_controls", []),
            "controls": [_control_view(item) for item in controls if item.get("repository") == repository],
            "observations": [item for item in observations if item.get("repository") == repository],
            "labels": [SIMULATED_LABEL, ANALYSIS_LABEL, RUNNER_LABEL, ISOLATION_LABEL],
        }
        write_json(layout.result_path(findings_result(repository)), document)
        provider_rows.append(
            (
                stable_id("provider-pseudonym", str(change.id), consumer["enrollment_id"]),
                [(entry["category"] or "unknown", entry["severity"], entry["verification"]["status"]) for entry in entries],
            )
        )
    write_json(layout.result_path(PROVIDER_SUMMARY_RESULT), provider_summary(change, analysis, provider_rows))


def _change_view(change: APIChange) -> dict:
    return {
        "change_id": str(change.id),
        "provider": "Stripe",
        "status": SIMULATED_LABEL,
        "service_id": change.service_id,
        "endpoint_id": str(change.endpoint_id),
        "endpoint": _endpoint_label(),
        "field_path": change.field_path,
        "kind": change.kind,
        "old_semantics": change.old_semantics,
        "new_semantics": change.new_semantics,
        "structural_breaking": change.structural_breaking,
    }


def _endpoint_label() -> str:
    endpoint = read_json(CHANGE_MANIFEST_PATH)["endpoint"]
    return f"{endpoint['method']} {endpoint['path']}"


def _control_view(control: Mapping) -> dict:
    return {
        key: control.get(key)
        for key in ("scenario_id", "variant", "status", "reason", "tests_run", "fixture_id", "fixture_sha256", "run_id")
    } | {"note": control.get("note"), "cases": control.get("cases", [])}


def _counts(values) -> dict:
    return dict(sorted(Counter(values).items()))


def provider_summary(change: APIChange, analysis: AnalysisResult, rows: list[tuple[str, list[tuple[str, str, str]]]]) -> dict:
    """Counts only, per pseudonymous consumer; see PROVIDER_EXCLUDED."""
    consumers = []
    everything: list[tuple[str, str, str]] = []
    for index, (_, risks) in enumerate(sorted(rows), start=1):
        everything += risks
        consumers.append(
            {
                "label": f"Consumer {index}",
                "risks": len(risks),
                "by_category": _counts(item[0] for item in risks),
                "by_severity": _counts(item[1] for item in risks),
                "by_verification_status": _counts(item[2] for item in risks),
            }
        )
    return {
        "kind": "shockwave-demo.provider-restricted-summary@1",
        "visibility": "provider_restricted",
        "provider": "Stripe",
        "provider_note": "Stripe is not a Shockwave customer and has not endorsed Shockwave; this view shows what a "
        "provider would see for its own (here SIMULATED) change.",
        "change": {
            "change_id": str(change.id),
            "status": SIMULATED_LABEL,
            "endpoint": _endpoint_label(),
            "field_path": change.field_path,
            "kind": change.kind,
        },
        "analysis_mode": analysis.analysis_mode,
        "privacy": {
            "excluded": list(PROVIDER_EXCLUDED),
            "pseudonyms": "Consumer labels are pseudonymous, ordered by a hash of the change and enrollment; they "
            "reveal no organization or repository name.",
        },
        "isolation": ISOLATION_LABEL,
        "consumers": consumers,
        "totals": {
            "consumers": len(consumers),
            "consumers_with_risks": sum(1 for item in consumers if item["risks"]),
            "risks": len(everything),
            "by_category": _counts(item[0] for item in everything),
            "by_severity": _counts(item[1] for item in everything),
            "by_verification_status": _counts(item[2] for item in everything),
        },
    }
