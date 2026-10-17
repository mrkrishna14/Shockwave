"""``validate-migrations``: check the prepared migration artifacts against the verified regressions.

For every verified regression the matching patch from demo/migrations/migrations.json (prepared
by Kanishk; Sai's migration generator is not integrated) is applied with ``git apply`` to a copy
of the prepared repository, and:

1. patched code + proposed fixture: ``interpret_patched_run`` (validated within the tested scope
   only; the reason ends with ``PINNED_PATCH_CAVEAT``);
2. patched code + baseline fixture: expected to fail the unchanged business expectation. It is
   recorded as deployment-assumption evidence (the patch is pinned to the proposed contract),
   never as a regression;
3. the consumer's OWN suite on the migrated copy (its bundled fixture data becomes the proposed
   variant through the patch; assertions are never changed);
4. scope: the patch changes exactly the declared files, the declared edits match, and the
   unrelated conversions, negative controls and test sources stay byte-identical.

All runs use the LOCAL RUNNER DOUBLE / the consumer's own command as plain local subprocesses.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from shockwave_demo.common import (
    CONSUMER_MANIFEST_PATH,
    DEPENDENCY_FIXTURE_PATH,
    MIGRATIONS_PATH,
    REPO_ROOT,
    RUNNER_LABEL,
    DemoError,
    Layout,
    display_path,
    git,
    git_blob,
    read_json,
    remove_tree,
    scratch_copy,
    sha256_file,
    short_scenario,
    write_json,
)
from shockwave_demo.execute import load_verification, phase_record, without_timing
from shockwave_demo.prepare import load_snapshots, run_own_suite
from verification.scenarios.dev_runner_double import run_phase_locally
from verification.scenarios.interpret import PINNED_PATCH_CAVEAT, interpret_patched_run

VALIDATION_RESULT = "migrations/validation.json"
DEPLOYMENT_EVIDENCE = (
    "deployment-assumption evidence: the migrated code run against current-contract (baseline) responses; the "
    "patch is pinned to the proposed contract, so this failure is expected and is never a regression"
)


def git_apply(patch: Path):
    """A ``patch`` callable for the runner double: ``git apply --check`` then ``git apply`` in the copy."""

    def apply(checkout: Path) -> None:
        git(["apply", "--check", str(patch)], checkout)
        git(["apply", str(patch)], checkout)

    return apply


def scope_check(copy: Path, commit: str, migration: Mapping, controls: list[Mapping]) -> dict:
    """What the applied patch changed, compared with what the migration declares (raises on any excess)."""
    changed = sorted(item for item in git(["diff", "--name-only", "HEAD"], copy).splitlines() if item)
    untracked = sorted(
        item
        for item in git(["ls-files", "--others", "--exclude-standard"], copy).splitlines()
        if item and item != "node_modules"  # the scratch copy links the installed packages
    )
    declared = sorted(item["path"] for item in migration["files_touched"])
    problems = []
    if changed != declared or untracked:
        problems.append(f"changed {changed} (+ new {untracked}), declared {declared}")
    for item in migration["files_touched"]:
        if item.get("becomes"):
            expected = (REPO_ROOT / item["becomes"]).read_bytes()
            if (copy / item["path"]).read_bytes() != expected:
                problems.append(f"{item['path']} is not byte-identical to {item['becomes']}")
    for edit in migration["edits"]:
        lines = (copy / edit["file"]).read_text(encoding="utf-8").splitlines()
        before = (git_blob(copy, commit, edit["file"]) or b"").decode("utf-8").splitlines()
        if not 1 <= edit["line"] <= min(len(lines), len(before)):
            problems.append(f"{edit['file']}:{edit['line']} is out of range")
            continue
        if before[edit["line"] - 1].strip() != edit["before"] or lines[edit["line"] - 1].strip() != edit["after"]:
            problems.append(f"{edit['file']}:{edit['line']} does not match the declared edit")
    unchanged = [item["path"] for item in migration["unchanged"]]
    for path in unchanged:
        if (copy / path).read_bytes() != git_blob(copy, commit, path):
            problems.append(f"{path} changed but must stay unchanged")
    control_lines = []
    for control in controls:
        current = (copy / control["file"]).read_text(encoding="utf-8").splitlines()
        original = (git_blob(copy, commit, control["file"]) or b"").decode("utf-8").splitlines()
        if current != original:
            problems.append(f"negative control {control['id']} ({control['file']}) changed")
        control_lines.append(f"{control['file']}:{control['line']} ({control['id']})")
    if problems:
        raise DemoError(f"migration {migration['id']} is out of scope: " + "; ".join(problems))
    return {
        "files_changed": changed,
        "files_declared": declared,
        "edits_verified": [f"{edit['file']}:{edit['line']} ({edit['role']})" for edit in migration["edits"]],
        "unchanged_verified": unchanged,
        "negative_controls_verified": control_lines,
    }


def run_validate_migrations(layout: Layout) -> list[str]:
    _, repositories = load_snapshots(layout)
    plans, results = load_verification(layout)
    migrations = read_json(MIGRATIONS_PATH)
    manifest = {entry["name"]: entry for entry in read_json(CONSUMER_MANIFEST_PATH)["repositories"]}
    controls_by_repo: dict[str, list] = {}
    for control in read_json(DEPENDENCY_FIXTURE_PATH)["negative_controls"]["items"]:
        controls_by_repo.setdefault(control["repository"], []).append(control)
    by_scenario = {item["scenario_id"]: item for item in migrations["migrations"]}
    plan_by_risk = {(str(plan.risk_id), plan.scenario_id): plan for plan in plans}
    lines = [f"Validate migrations - {migrations['label']}", f"  runner      {RUNNER_LABEL}"]
    validated = []
    for result in results:
        if result.status != "verified_regression":
            continue
        plan = plan_by_risk[(str(result.risk_id), result.scenario_id)]
        migration = by_scenario.get(plan.scenario_id)
        if migration is None:
            lines.append(f"  no migration artifact for {plan.scenario_id}")
            continue
        repository = migration["repository"]
        details = repositories[repository]
        commit = details["commit_sha"]
        patch_path = REPO_ROOT / migration["patch"]
        patch_info = {
            "migration_id": migration["id"],
            "patch": migration["patch"],
            "sha256": sha256_file(patch_path),
            "applied_with": "git apply --check <patch>; git apply <patch> (in the run's copy only)",
        }
        apply = git_apply(patch_path)
        short = short_scenario(plan.scenario_id)
        work = layout.scratch_dir / f"migrate-{short}"
        try:
            patched_run = run_phase_locally(
                plan, layout.repo(repository), "patched", work_dir=work, node_executable=layout.node, patch=apply
            )
            patched_record, _ = phase_record(
                layout,
                run_id=f"migration.{migration['id']}.patched-proposed",
                stage="validate-migrations",
                purpose="migrated code against the proposed (simulated) fixture, unchanged expectations",
                plan=plan,
                phase="patched",
                classify_as="patched",
                run=patched_run,
                repository=repository,
                commit=commit,
                checkout=display_path(work / "checkout-patched", layout),
                patch=patch_info,
            )
            patched = interpret_patched_run(plan, without_timing(patched_run))
            deployment_run = run_phase_locally(
                plan, layout.repo(repository), "baseline", work_dir=work, node_executable=layout.node, patch=apply
            )
            deployment_record, deployment = phase_record(
                layout,
                run_id=f"migration.{migration['id']}.patched-baseline",
                stage="validate-migrations",
                purpose=DEPLOYMENT_EVIDENCE,
                plan=plan,
                phase="baseline",
                classify_as="baseline",
                run=deployment_run,
                repository=repository,
                commit=commit,
                checkout=display_path(work / "checkout-baseline", layout),
                patch=patch_info,
            )
            copy = scratch_copy(layout.repo(repository), work / "scope" / repository, inside=layout.scratch_dir)
            apply(copy)
            scope = scope_check(copy, commit, migration, controls_by_repo.get(repository, []))
        finally:
            remove_tree(work, inside=layout.scratch_dir)
        own = run_own_suite(
            layout,
            manifest[repository],
            commit,
            f"migration.{migration['id']}.own-suite",
            stage="validate-migrations",
            purpose="the consumer's own suite on the migrated copy (bundled fixture data = proposed variant via the patch)",
            patch=apply,
        )
        failed_cases = [case for case in deployment_record["assertions"] if case["status"] != "passed"]
        if deployment.outcome == "passed":
            raise DemoError(
                f"{migration['id']}: migrated code passed against baseline responses; the recorded deployment "
                "assumption (pinned to the proposed contract) does not hold - review the migration"
            )
        if not failed_cases:
            raise DemoError(
                f"{migration['id']}: the patched-baseline run produced no assertion evidence ({deployment.reason}); "
                "it cannot be recorded as deployment-assumption evidence"
            )
        status = "validated" if patched.status == "validated" and own["outcome"] == "passed" else "not_validated"
        validated.append(
            {
                "migration_id": migration["id"],
                "label": migration["label"],
                "repository": repository,
                "commit_sha": commit,
                "scenario_id": plan.scenario_id,
                "risk_id": str(plan.risk_id),
                "plan_id": str(plan.id),
                "verification_result_id": str(result.id),
                "patch": patch_info,
                "status": status,
                "patched_proposed": {
                    "run_id": patched_record["run_id"],
                    "status": patched.status,
                    "reason": patched.reason,
                    "assertions": patched_record["assertions"],
                },
                "deployment_assumption": {
                    "run_id": deployment_record["run_id"],
                    "recorded_as": DEPLOYMENT_EVIDENCE,
                    "outcome": deployment.outcome,
                    "reason": deployment.reason,
                    "failed_cases": failed_cases,
                    "expected_failure_observed": bool(failed_cases),
                    "is_regression": False,
                },
                "own_suite": {
                    key: own[key]
                    for key in ("run_id", "outcome", "tests_run", "tests_passed", "tests_failed", "failing_tests", "fixture")
                }
                | {"command": own["command"]["declared"]},
                "scope": scope,
                "deployment_caveat": PINNED_PATCH_CAVEAT,
            }
        )
        lines += [
            f"  {migration['id']} ({repository}): {status}",
            (
                f"    patched + proposed fixture:  {patched.status} (ends with the pinned-patch caveat: "
                f"{patched.reason.endswith(PINNED_PATCH_CAVEAT)})"
            ),
            "    patched + baseline fixture:  "
            + "; ".join(f"{case['case_id']} {case['detail']}" for case in failed_cases)
            + " -> deployment-assumption evidence, not a regression",
            f"    own suite on migrated copy:  {own['outcome']} - {own['tests_passed']}/{own['tests_run']} passed",
            (
                f"    scope: changed {', '.join(scope['files_changed'])}; unchanged verified: "
                f"{', '.join(scope['unchanged_verified'])}"
            ),
        ]
    document = {
        "kind": "shockwave-demo.migration-validation@1",
        "label": migrations["label"],
        "runner": RUNNER_LABEL,
        "validation_scope": migrations["validation_scope"],
        "deployment_caveat": PINNED_PATCH_CAVEAT,
        "deployment_assumption": migrations["deployment_assumption"],
        "simulated_change": migrations["simulated_change"],
        "migrations": validated,
        "not_prepared": migrations["not_prepared"],
    }
    write_json(layout.result_path(VALIDATION_RESULT), document)
    from shockwave_demo.views import write_views

    write_views(layout)
    lines.append(f"  caveat      {PINNED_PATCH_CAVEAT}")
    for item in migrations["not_prepared"]:
        lines.append(f"  not prepared: {item['repository']} - {item['reason'][:150]}...")
    return lines
