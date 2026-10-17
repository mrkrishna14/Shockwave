"""demo/migrations: prepared migration artifacts, validated by real runs of each consumer's own suite.

The patches are prepared migration artifacts (Kanishk); Sai's migration generator is not
integrated. Each one is exercised in a temporary, git-initialized copy of its template, so
`git apply` runs at the consumer repository root:

1. the original consumer suite passes on its bundled baseline fixture, and fails its business
   assertion on the proposed (SIMULATED, not announced by Stripe) fixture: the failure the
   migration addresses;
2. `git apply --check` and `git apply` succeed;
3. only the affected line, its comment and the bundled fixture data change (the fixture becomes
   the proposed set byte for byte); assertions, unrelated conversions and negative controls stay
   byte-identical;
4. the migrated suite passes on the proposed fixture with unchanged expectations;
5. the migrated code fails the same business assertion on the BASELINE fixture. That is
   deployment-assumption evidence (the migration is pinned to the proposed contract), never a
   regression.

The suites run with the LOCAL RUNNER DOUBLE's standing: trusted local fixtures; plain subprocess
(the repository venv's Python and Node); not a sandbox; not Sai's runner. Results are validated
within the tested scope only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import pytest

from verification.scenarios.interpret import PINNED_PATCH_CAVEAT

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "demo"
MIGRATIONS_PATH = DEMO / "migrations" / "migrations.json"
DEPENDENCIES_PATH = DEMO / "expected" / "dependencies.fixture.json"
CONSUMER_MANIFEST_PATH = DEMO / "consumer-repos.manifest.json"
CHANGE_MANIFEST_PATH = DEMO / "scenarios" / "stripe-payment-intent-amount-usd-major-units.manifest.json"
BASELINE_FIXTURE = DEMO / "stripe-fixtures" / "baseline" / "payment_intents.json"
PROPOSED_FIXTURE = DEMO / "stripe-fixtures" / "proposed" / "payment_intents.json"

LABEL = "prepared migration artifact (Kanishk); Sai's migration generator not integrated"
EXPECTED_MIGRATIONS = {
    "checkout-display-major-units": (
        "shockwave-stripe-checkout",
        "stripe.payment-intent-amount-units.checkout-display.typescript",
    ),
    "fraud-threshold-major-units": (
        "shockwave-stripe-fraud",
        "stripe.payment-intent-amount-units.fraud-threshold.python",
    ),
}
# Files a migration must never touch: unrelated conversions, negative controls and the assertions.
REQUIRED_UNCHANGED = {
    "checkout-display-major-units": {"src/tax.ts", "src/rewards.ts", "test/checkout.test.ts"},
    "fraud-threshold-major-units": {"fraud_service/velocity.py", "tests/test_review.py"},
}
PRUNED_DIRECTORIES = {".git", "node_modules", "__pycache__", ".pytest_cache"}
MAX_DETAIL_CHARS = 4000


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


MIGRATIONS = read_json(MIGRATIONS_PATH)
BY_ID: dict[str, dict] = {migration["id"]: migration for migration in MIGRATIONS["migrations"]}
DEPENDENCIES: dict[str, dict] = read_json(DEPENDENCIES_PATH)["dependencies"]


def template_dir(migration: dict) -> Path:
    return REPO_ROOT / migration["template"]


def touched(migration: dict, kind: str) -> str:
    (path,) = [item["path"] for item in migration["files_touched"] if item["kind"] == kind]
    return path


def tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root`` by relative POSIX path (git metadata, packages and caches pruned)."""
    hashes: dict[str, str] = {}
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(name for name in subdirectories if name not in PRUNED_DIRECTORIES)
        for name in files:
            if name.endswith((".pyc", ".sqlite")) or name == ".DS_Store":
                continue
            path = Path(directory) / name
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


# Patch parsing ---------------------------------------------------------------------------------


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    headers: list[str] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)  # (old line number, text)
    added: list[tuple[int, str]] = field(default_factory=list)  # (new line number, text)


HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FILE_HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)")


def parse_patch(text: str) -> tuple[str, list[FileDiff]]:
    """(description before the first ``diff --git`` line, per-file removed/added lines)."""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("diff --git "))
    diffs: list[FileDiff] = []
    in_header = False
    old_line = new_line = 0
    for line in lines[start:]:
        header = FILE_HEADER.fullmatch(line)
        hunk = HUNK.match(line)
        if line.startswith("diff --git "):
            assert header is not None, line
            diffs.append(FileDiff(header.group(1), header.group(2)))
            in_header = True
        elif hunk:
            old_line, new_line = int(hunk.group(1)), int(hunk.group(2))
            in_header = False
        elif in_header:
            diffs[-1].headers.append(line)  # index, mode, ---/+++ lines
        elif line.startswith("-"):
            diffs[-1].removed.append((old_line, line[1:]))
            old_line += 1
        elif line.startswith("+"):
            diffs[-1].added.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1
        elif not line.startswith("\\"):  # "\ No newline at end of file" is the only other line kind
            raise AssertionError(f"unexpected patch line {line!r}")
    return "\n".join(lines[:start]), diffs


# Consumer suite runs ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SuiteRun:
    """One run of a consumer's own test suite: per-test outcomes plus bounded failure details."""

    label: str
    returncode: int | None
    outcomes: dict[str, str]  # test id -> passed | failed | skipped
    details: dict[str, str]  # failed test id -> failure text (bounded)
    output_tail: str

    @property
    def tests_run(self) -> int:
        return sum(1 for outcome in self.outcomes.values() if outcome in ("passed", "failed"))

    @property
    def failed(self) -> list[str]:
        return sorted(test for test, outcome in self.outcomes.items() if outcome == "failed")

    @property
    def passed_everything(self) -> bool:
        return self.returncode == 0 and self.tests_run > 0 and not self.failed

    def describe(self) -> str:
        return f"{self.label}: exit {self.returncode}, outcomes {self.outcomes}\n{self.output_tail}"


TAP_RESULT = re.compile(r"^(ok|not ok) \d+ - (.*?)(?: # (SKIP|TODO)\b.*)?$")


def parse_tap(stdout: str) -> tuple[dict[str, str], dict[str, str]]:
    """Top-level results of ``node --test --test-reporter=tap`` (subtests are indented and ignored)."""
    outcomes: dict[str, str] = {}
    details: dict[str, str] = {}
    current: str | None = None
    block: list[str] = []
    for line in [*stdout.splitlines(), "1..0"]:
        top_level = TAP_RESULT.match(line) or line.startswith(("# Subtest:", "1.."))
        if top_level and current is not None:
            details[current] = "\n".join(block)[:MAX_DETAIL_CHARS]
            current, block = None, []
        match = TAP_RESULT.match(line)
        if match:
            name = match.group(2)
            if match.group(3):
                outcomes[name] = "skipped"
            else:
                outcomes[name] = "passed" if match.group(1) == "ok" else "failed"
                current = name if match.group(1) == "not ok" else None
        elif current is not None:
            block.append(line)
    return outcomes, details


def parse_junit(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Per-test outcomes from pytest's junit XML (xunit1: carries each test's file)."""
    if not path.is_file():
        return {}, {}
    outcomes: dict[str, str] = {}
    details: dict[str, str] = {}
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        test_id = f"{case.get('file')}::{case.get('name')}"
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            outcomes[test_id] = "failed"
            details[test_id] = f"{problem.get('message', '')}\n{problem.text or ''}"[:MAX_DETAIL_CHARS]
        elif case.find("skipped") is not None:
            outcomes[test_id] = "skipped"
        else:
            outcomes[test_id] = "passed"
    return outcomes, details


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Toolchain:
    python: Path
    node: Path | None
    run: Runner


def run_suite(label: str, checkout: Path, report_dir: Path, tools: Toolchain) -> SuiteRun:
    """Run the consumer's own test command in ``checkout`` (node --test, or pytest)."""
    package = checkout / "package.json"
    if package.is_file():
        script = shlex.split(read_json(package)["scripts"]["test"])
        assert script[:2] == ["node", "--test"], script  # the consumer's declared test command
        assert tools.node is not None
        argv = [str(tools.node), "--test", "--test-reporter=tap", *script[2:]]
        completed = tools.run(argv, checkout)
        outcomes, details = parse_tap(completed.stdout)
    else:
        report = report_dir / f"{label}.junit.xml"
        argv = [str(tools.python), "-m", "pytest", "-p", "no:cacheprovider", "-q", "-o", "junit_family=xunit1",
                f"--junitxml={report}"]
        completed = tools.run(argv, checkout)
        outcomes, details = parse_junit(report)
    tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-40:])
    return SuiteRun(label, completed.returncode, outcomes, details, tail)


@dataclass
class MigrationRuns:
    migration: dict
    checkout: Path
    template_tree: dict[str, str]
    original_on_baseline: SuiteRun
    original_on_proposed: SuiteRun
    tree_before: dict[str, str]
    apply_check: subprocess.CompletedProcess[str]
    apply: subprocess.CompletedProcess[str] | None = None
    tree_after: dict[str, str] | None = None
    migrated_on_proposed: SuiteRun | None = None
    migrated_on_baseline: SuiteRun | None = None


@pytest.fixture(scope="module", params=sorted(EXPECTED_MIGRATIONS))
def runs(
    request: pytest.FixtureRequest,
    venv_python: Path,
    git_executable: str,
    run_command: Runner,
    template_copy: Callable[..., Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> MigrationRuns:
    """Every run for one migration, in order, in one temporary git-initialized template copy."""
    git = git_executable
    migration = BY_ID[request.param]
    typescript = (template_dir(migration) / "package.json").is_file()
    node = request.getfixturevalue("node_executable") if typescript else None
    toolchain = Toolchain(python=venv_python, node=node, run=run_command)
    checkout = template_copy(Path(migration["template"]).name)
    reports = tmp_path_factory.mktemp(f"{migration['id']}-reports")
    run = toolchain.run
    initialized = run([git, "init", "-q"], checkout)
    assert initialized.returncode == 0, initialized.stderr
    bundled = checkout / touched(migration, "consumer_fixture_data")
    assert bundled.read_bytes() == BASELINE_FIXTURE.read_bytes(), "the template must ship the baseline fixture set"

    original_on_baseline = run_suite("original_on_baseline", checkout, reports, toolchain)
    shutil.copyfile(PROPOSED_FIXTURE, bundled)
    original_on_proposed = run_suite("original_on_proposed", checkout, reports, toolchain)
    shutil.copyfile(BASELINE_FIXTURE, bundled)

    patch = str(REPO_ROOT / migration["patch"])
    result = MigrationRuns(
        migration=migration,
        checkout=checkout,
        template_tree=tree_hashes(template_dir(migration)),
        original_on_baseline=original_on_baseline,
        original_on_proposed=original_on_proposed,
        tree_before=tree_hashes(checkout),
        apply_check=run([git, "apply", "--check", "--whitespace=error", patch], checkout),
    )
    if result.apply_check.returncode != 0:
        return result
    result.apply = run([git, "apply", "--whitespace=error", patch], checkout)
    if result.apply.returncode != 0:
        return result
    result.tree_after = tree_hashes(checkout)
    result.migrated_on_proposed = run_suite("migrated_on_proposed", checkout, reports, toolchain)
    shutil.copyfile(BASELINE_FIXTURE, bundled)
    result.migrated_on_baseline = run_suite("migrated_on_baseline", checkout, reports, toolchain)
    return result


def business_tests(migration: dict) -> list[str]:
    assertion = migration["business_assertion"]
    return sorted(assertion["cases"].values()) if "cases" in assertion else [assertion["test"]]


def assert_business_assertion_failed(run: SuiteRun, migration: dict, expectation: dict) -> None:
    """``run`` failed exactly the expected business tests, at the business assertion line, with the expected value."""
    assert run.returncode not in (0, None), run.describe()
    assert run.tests_run > 0, run.describe()
    assert run.failed == sorted(expectation["failing_tests"]), run.describe()
    assertion = migration["business_assertion"]
    location = re.escape(f"{assertion['file']}:{assertion['line']}") + r"(?::\d+)?\b"
    for test in run.failed:
        detail = run.details[test]
        assert re.search(location, detail), f"{test} did not fail at the business assertion:\n{detail}"
        if "cases" in assertion:  # pytest: `assert decision.needs_manual_review is <expected>`
            expected = test.endswith("-True]")
            assert f"assert {not expected} is {expected}" in detail, detail
        else:  # node: assert.equal(displayTotal, '$10.00')
            assert "expected: '$10.00'" in detail, detail
            assert f"actual: '{expectation['actual']}'" in detail, detail
    unaffected = sorted(set(run.outcomes) - set(run.failed))
    assert unaffected and all(run.outcomes[test] == "passed" for test in unaffected), run.describe()


# Static checks of the artifacts -------------------------------------------------------------------


def test_migrations_manifest_labels_ids_and_caveat() -> None:
    assert MIGRATIONS["label"] == LABEL
    assert MIGRATIONS["deployment_caveat"] == PINNED_PATCH_CAVEAT
    assert MIGRATIONS["deployment_caveat_source"] == "verification.scenarios.interpret.PINNED_PATCH_CAVEAT"
    assert "never as a regression" in MIGRATIONS["deployment_assumption"]
    assert "LOCAL RUNNER DOUBLE" in MIGRATIONS["validation_runner"]
    assert "not a sandbox; not Sai's runner" in MIGRATIONS["validation_runner"]
    assert MIGRATIONS["validation_scope"].startswith("Validated within the tested scope only")
    change = read_json(CHANGE_MANIFEST_PATH)
    assert MIGRATIONS["simulated_change"]["manifest_id"] == change["manifest_id"]
    assert "not announced by Stripe" in MIGRATIONS["simulated_change"]["status"]
    repositories = {item["name"]: item["template"] for item in read_json(CONSUMER_MANIFEST_PATH)["repositories"]}
    assert sorted(BY_ID) == sorted(EXPECTED_MIGRATIONS)
    for migration_id, (repository, scenario_id) in EXPECTED_MIGRATIONS.items():
        migration = BY_ID[migration_id]
        assert migration["label"] == LABEL
        assert migration["deployment_caveat"] == PINNED_PATCH_CAVEAT
        assert (migration["repository"], migration["scenario_id"]) == (repository, scenario_id)
        assert migration["template"] == repositories[repository]
        assert scenario_id in change["scenarios"]
        assert migration["patch"] == f"demo/migrations/{migration_id}.patch"
        reference = migration["addresses_dependency"]
        assert reference["id"] == DEPENDENCIES[reference["handle"]]["id"]
        assert REQUIRED_UNCHANGED[migration_id] <= {item["path"] for item in migration["unchanged"]}
        expectations = migration["consumer_suite_expectations"]
        assert expectations["original_on_baseline"]["outcome"] == expectations["migrated_on_proposed"]["outcome"] == "pass"
        assert expectations["original_on_proposed"]["outcome"] == expectations["migrated_on_baseline"]["outcome"] == "fail"
        assert "never a regression" in expectations["migrated_on_baseline"]["meaning"]
    assert [item["repository"] for item in MIGRATIONS["not_prepared"]] == ["shockwave-stripe-reporting"]


@pytest.mark.parametrize("migration_id", sorted(EXPECTED_MIGRATIONS))
def test_patch_changes_only_the_affected_line_its_comment_and_the_bundled_fixture(migration_id: str) -> None:
    migration = BY_ID[migration_id]
    assert_patch_scope(migration, (REPO_ROOT / migration["patch"]).read_text(encoding="utf-8"))


def assert_patch_scope(migration: dict, patch_text: str) -> None:
    """The patch touches only the declared files: the affected line, its comment and the fixture data."""
    preamble, diffs = parse_patch(patch_text)
    assert LABEL in preamble and "SIMULATED, not announced by Stripe" in preamble
    assert PINNED_PATCH_CAVEAT in preamble
    assert {diff.new_path for diff in diffs} == {item["path"] for item in migration["files_touched"]}
    for diff in diffs:
        assert diff.old_path == diff.new_path, "no renames"
        assert not any(header.startswith(("new file", "deleted file", "old mode", "new mode")) for header in diff.headers)
        if diff.new_path.startswith(("test/", "tests/")):  # no test code or assertion changes, only fixture data
            assert diff.new_path == touched(migration, "consumer_fixture_data"), diff.new_path
    source = next(diff for diff in diffs if diff.new_path == touched(migration, "source"))
    template_lines = (template_dir(migration) / source.new_path).read_text(encoding="utf-8").splitlines()
    edits = migration["edits"]
    assert [(line, text.strip()) for line, text in source.removed] == [(edit["line"], edit["before"]) for edit in edits]
    assert [(line, text.strip()) for line, text in source.added] == [(edit["line"], edit["after"]) for edit in edits]
    for edit in edits:
        assert edit["file"] == source.new_path
        assert template_lines[edit["line"] - 1].strip() == edit["before"], f"template drift at {edit['file']}:{edit['line']}"
    comment, affected = (next(edit for edit in edits if edit["role"] == role) for role in ("comment", "affected_line"))
    assert len(edits) == 2 and comment["line"] == affected["line"] - 1
    assert comment["before"].startswith(("//", "#")) and comment["after"].startswith(("//", "#"))
    # The affected line is the usage line of the dependency this migration addresses.
    usage = DEPENDENCIES[migration["addresses_dependency"]["handle"]]["evidence"][0]
    assert (usage["file"], usage["line_start"], usage["excerpt"]) == (source.new_path, affected["line"], affected["before"])


def test_business_assertions_are_the_consumers_real_assertion_lines() -> None:
    for migration in BY_ID.values():
        assertion = migration["business_assertion"]
        lines = (template_dir(migration) / assertion["file"]).read_text(encoding="utf-8").splitlines()
        assert lines[assertion["line"] - 1].strip() == assertion["excerpt"], f"template drift at {assertion['file']}"


# Real runs ---------------------------------------------------------------------------------------


def test_original_suite_passes_on_the_baseline_fixture(runs: MigrationRuns) -> None:
    run = runs.original_on_baseline
    assert run.passed_everything, run.describe()
    assert all(run.outcomes.get(test) == "passed" for test in business_tests(runs.migration)), run.describe()


def test_original_code_fails_the_business_assertion_on_the_proposed_fixture(runs: MigrationRuns) -> None:
    expectation = runs.migration["consumer_suite_expectations"]["original_on_proposed"]
    assert_business_assertion_failed(runs.original_on_proposed, runs.migration, expectation)


def test_patch_applies_with_git_apply_at_the_repository_root(runs: MigrationRuns) -> None:
    assert runs.tree_before == runs.template_tree, "the copy must be the pristine template before the patch"
    assert runs.apply_check.returncode == 0, runs.apply_check.stderr
    assert runs.apply is not None and runs.apply.returncode == 0, getattr(runs.apply, "stderr", "not applied")


def test_only_the_declared_files_change_and_the_fixture_becomes_the_proposed_set(runs: MigrationRuns) -> None:
    assert runs.tree_after is not None, "patch did not apply"
    migration = runs.migration
    assert sorted(runs.tree_after) == sorted(runs.tree_before), "the patch must not add or delete files"
    changed = {path for path, digest in runs.tree_before.items() if runs.tree_after[path] != digest}
    assert changed == {item["path"] for item in migration["files_touched"]}
    proposed = hashlib.sha256(PROPOSED_FIXTURE.read_bytes()).hexdigest()
    assert runs.tree_after[touched(migration, "consumer_fixture_data")] == proposed


def test_unrelated_logic_and_assertions_stay_byte_identical(runs: MigrationRuns) -> None:
    assert runs.tree_after is not None, "patch did not apply"
    for path in REQUIRED_UNCHANGED[runs.migration["id"]]:
        assert runs.tree_after[path] == runs.template_tree[path], path
    tests_dir = touched(runs.migration, "consumer_fixture_data").split("/")[0]
    for path, digest in runs.template_tree.items():
        if path.startswith(f"{tests_dir}/") and path != touched(runs.migration, "consumer_fixture_data"):
            assert runs.tree_after[path] == digest, f"test file changed: {path}"


def test_migrated_suite_passes_on_the_proposed_fixture(runs: MigrationRuns) -> None:
    run = runs.migrated_on_proposed
    assert run is not None, "patch did not apply"
    assert run.passed_everything, run.describe()
    assert all(run.outcomes.get(test) == "passed" for test in business_tests(runs.migration)), run.describe()
    assert sorted(run.outcomes) == sorted(runs.original_on_baseline.outcomes), "the same tests ran"


def test_migrated_code_fails_the_business_assertion_on_the_baseline_fixture_deployment_assumption(
    runs: MigrationRuns,
) -> None:
    """Deployment-assumption evidence: the migration is pinned to the proposed contract (not a regression)."""
    run = runs.migrated_on_baseline
    assert run is not None, "patch did not apply"
    expectation = runs.migration["consumer_suite_expectations"]["migrated_on_baseline"]
    assert_business_assertion_failed(run, runs.migration, expectation)
