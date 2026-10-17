"""The deterministic reference results: their index, comparison and the checked-in copy.

``results/`` under the reports directory holds only normalized documents (durations 0, no
absolute paths, no timestamps except the fixed snapshot date, no log text), so a fresh ``all``
reproduces demo/expected/results byte for byte. ``index.json`` lists every file with its kind,
audience and shape.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from shockwave_demo.common import EXPECTED_RESULTS_DIR, Layout, read_json, write_json

INDEX = "index.json"
# relative path pattern -> (kind, audience, description)
DESCRIPTIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        "inventory.json",
        "shockwave-demo.inventory@1",
        "provider",
        ("Pinned Stripe contract: measured counts, provenance, provider-source-unavailable statement, "
        "GET /v1/payment_intents/{intent} record with parameters and amount docs"),
    ),
    (
        "snapshots.json",
        "shockwave-demo.repository-snapshots@1",
        "internal",
        "RepositorySnapshot records (shockwave.v1) of the three prepared local commits",
    ),
    ("prepare.json", "shockwave-demo.prepare@1", "internal", "Per repository: commit, dependency, own-suite counts"),
    (
        "consumers/*/dependencies.json",
        "shockwave-demo.consumer-dependencies@1",
        "consumer_private",
        ("Dependency records (shockwave.v1) bound to the prepared commit; source = FIXTURE until Noirit's scanner "
        "is integrated"),
    ),
    (
        "consumers/*/findings.json",
        "shockwave-demo.consumer-findings@1",
        "consumer_private",
        ("Risks with evidence, verification, migration, controls and observations; unresolved and unaffected "
        "dependencies listed separately"),
    ),
    (
        "provider/restricted-summary.json",
        "shockwave-demo.provider-restricted-summary@1",
        "provider",
        ("Counts by category/severity/verification status per pseudonymous consumer; no consumer source, paths, "
        "lines, snippets, logs or patches"),
    ),
    ("change/api-change.json", "shockwave.v1 APIChange", "provider", "The declared SIMULATED change"),
    (
        "change/detect-changes.json",
        "shockwave-demo.detect-changes@1",
        "provider",
        "detect_changes without (0 structural changes) and with the declaration",
    ),
    (
        "change/analysis.json",
        "shockwave.v1 AnalysisResult",
        "internal",
        "Predicted risks (rules mode) for all consumers; never shown to the provider as is",
    ),
    ("verification/plans.json", "shockwave.v1 VerificationPlan[]", "internal", "Plans for risks with a bound scenario"),
    (
        "verification/results.json",
        "shockwave.v1 VerificationResult[]",
        "internal",
        "LOCAL RUNNER DOUBLE results and not_run results (summary prefixed with the runner label)",
    ),
    (
        "verification/controls.json",
        "shockwave-demo.controls@1",
        "consumer_private",
        "Control results (shockwave.control_result.v1) under both fixture variants; not tied to any risk",
    ),
    (
        "verification/observations.json",
        "shockwave-demo.observations@1",
        "consumer_private",
        "Supporting observations (the storage-risk consumer's own suite against the simulated fixture)",
    ),
    (
        "migrations/validation.json",
        "shockwave-demo.migration-validation@1",
        "consumer_private",
        ("Patched-proposed validation, patched-baseline deployment-assumption evidence, own suite and scope checks "
        "per migration"),
    ),
    (
        "runs/*.json",
        "shockwave-demo.run-record@1",
        "consumer_private",
        ("One normalized record per executed run: argv, exit code, tests, assertion evidence, commit, fixture id + "
        "sha256 (log tails only in reports/runs)"),
    ),
)


def _describe(relative: str) -> dict:
    for pattern, kind, audience, description in DESCRIPTIONS:
        if Path(relative).match(pattern) and relative.count("/") == pattern.count("/"):
            return {"kind": kind, "audience": audience, "description": description}
    return {"kind": None, "audience": None, "description": None}


# Outputs per pipeline stage (results/ globs; run-record globs apply to results/runs and runs/).
STAGE_OUTPUTS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("prepare", ("snapshots.json", "prepare.json"), ("prepare.*.json",)),
    ("scan", ("consumers/*/dependencies.json",), ()),
    ("submit-change", ("change/*.json", "consumers/*/findings.json", "provider/*.json"), ()),
    ("execute", ("verification/*.json",), ("execute.*.json", "control.*.json")),
    ("validate-migrations", ("migrations/*.json",), ("migration.*.json",)),
)


def invalidate_downstream(layout: Layout, stage: str) -> list[str]:
    """Delete the outputs of the stages after ``stage``: they were derived from what ``stage`` replaces."""
    names = [name for name, _, _ in STAGE_OUTPUTS]
    if stage not in names:
        return []
    removed = []
    for _, results, runs in STAGE_OUTPUTS[names.index(stage) + 1 :]:
        paths = [path for pattern in results for path in layout.results_dir.glob(pattern)]
        for root in (layout.results_dir / "runs", layout.raw_runs_dir):
            paths += [path for pattern in runs for path in root.glob(pattern)]
        for path in sorted(paths):
            if path.is_file():
                path.unlink()
                removed.append(path.relative_to(layout.reports_dir).as_posix())
    return removed


def write_index(layout: Layout) -> None:
    root = layout.results_dir
    if not root.is_dir():
        return
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.json") if path.is_file())
    files = [item for item in files if item != INDEX]
    write_json(
        root / INDEX,
        {
            "kind": "shockwave-demo.results-index@1",
            "normalization": "durations 0; no absolute paths ($WORK_DIR/... for scratch paths); no log text; the only "
            "timestamp is the fixed snapshot date 2026-10-17T12:00:00Z; keys sorted, 2-space indent",
            "audiences": {
                "consumer_private": "visible only to the consumer org that owns the repository",
                "provider": "visible to the provider (Stripe's view in this demo); never consumer source",
                "internal": "backend records (contract-shaped) from which the views are derived",
            },
            "files": {item: _describe(item) for item in files},
        },
    )


def compare_with_expected(layout: Layout, expected: Path = EXPECTED_RESULTS_DIR) -> dict:
    """Files missing, unexpected or different between results/ and the checked-in reference."""

    def listing(root: Path) -> set[str]:
        return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()} if root.is_dir() else set()

    produced, reference = listing(layout.results_dir), listing(expected)
    different = sorted(
        item
        for item in produced & reference
        if not filecmp.cmp(layout.results_dir / item, expected / item, shallow=False)
    )
    return {
        "missing": sorted(reference - produced),
        "unexpected": sorted(produced - reference),
        "different": different,
        "identical": len(produced & reference) - len(different),
    }


def update_expected(layout: Layout, expected: Path = EXPECTED_RESULTS_DIR) -> int:
    """Replace demo/expected/results with the current results/ (deterministic documents only)."""
    if not (layout.results_dir / INDEX).is_file():
        return 0
    read_json(layout.results_dir / INDEX)
    if expected.exists():
        shutil.rmtree(expected)
    shutil.copytree(layout.results_dir, expected)
    return sum(1 for path in expected.rglob("*") if path.is_file())
