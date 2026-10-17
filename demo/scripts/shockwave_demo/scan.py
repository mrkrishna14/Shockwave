"""``scan``: consumer dependencies for each prepared repository.

The labeled FIXTURE (demo/expected/dependencies.fixture.json, "fixture (Noirit scanner not
integrated)") is always loaded and checked: EVERY evidence item must match the prepared commit (the
file exists in the commit, the lines are in range, the excerpt equals the line's text), and any drift
stops the run; its placeholder revision is then bound to the prepared commit SHA. Negative controls
are checked the same way.

When Noirit's scanner is importable (``scanner.scan_repository(request: ScanRequest, snapshot,
repo_root, *, consumer_org_id)``) it runs on every prepared repository against the baseline Stripe
endpoint catalog. Its records are used for a repository only when they reproduce every use site the
fixture expects (file, line, response field); otherwise the repository keeps the fixture records and
the run records exactly which sites the scanner found and missed. ``--use-fixture`` skips the scanner.
Output: one consumer-private dependency file per consumer under results/consumers/<repo>/.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Mapping
from pathlib import Path

from discovery.contracts import Dependency
from shockwave_demo.common import (
    DEPENDENCY_FIXTURE_PATH,
    IDS_PATH,
    SCAN_FIXTURE_LABEL,
    DemoError,
    Layout,
    count,
    git_blob,
    read_json,
    read_result,
    repo_relative,
    write_json,
)
from shockwave_demo.prepare import load_snapshots

SCANNER_MODULE = "scanner"
SCANNER_FUNCTION = "scan_repository"
SCANNER_LABEL = "Noirit's scanner (scanner.scan_repository)"


def dependencies_result(repository: str) -> str:
    return f"consumers/{repository}/dependencies.json"


def _scanner():
    if importlib.util.find_spec(SCANNER_MODULE) is None:
        return None
    module = importlib.import_module(SCANNER_MODULE)
    function = getattr(module, SCANNER_FUNCTION, None)
    if not callable(function):
        raise DemoError(
            f"a '{SCANNER_MODULE}' package is importable but exposes no {SCANNER_FUNCTION}(...) with the documented "
            "interface (see demo/scripts/shockwave_demo/scan.py); rerun with --use-fixture to use the labeled fixture"
        )
    return function


def _line_problem(lines: list[str], file: str, line_start: int | None, line_end: int | None, excerpt: str | None) -> str | None:
    if line_start is None:
        return f"{file}: no line number"
    end = line_end or line_start
    if not 1 <= line_start <= end <= len(lines):
        return f"{file}:{line_start}-{end} is outside the file ({len(lines)} lines)"
    if line_start != end:
        return f"{file}:{line_start}-{end}: the fixture's excerpt rule covers single lines only"
    if excerpt is not None and lines[line_start - 1].strip() != excerpt:
        return f"{file}:{line_start}: excerpt {excerpt!r} != committed line {lines[line_start - 1].strip()!r}"
    return None


class _CommitFiles:
    """Files of one prepared commit, read through git (the commit is the authority, not the work tree)."""

    def __init__(self, repo: Path, commit: str) -> None:
        self.repo, self.commit = repo, commit
        self._cache: dict[str, list[str] | None] = {}

    def lines(self, file: str) -> list[str] | None:
        if file not in self._cache:
            data = git_blob(self.repo, self.commit, file)
            self._cache[file] = None if data is None else data.decode("utf-8").splitlines()
        return self._cache[file]


def bind_fixture_records(
    fixture: Mapping, repository: str, commit: str, repo: Path, snapshot_id: str
) -> tuple[list[Dependency], list[dict], int]:
    """The repository's fixture records verified against ``commit`` and bound to it (raises on drift)."""
    placeholder = fixture["revision_binding"]["placeholders"][repository]
    files = _CommitFiles(repo, commit)
    problems: list[str] = []
    bound: list[Dependency] = []
    checked = 0
    handles = [handle for handle, note in fixture["annotations"].items() if note["repository"] == repository]
    for handle in handles:
        record = dict(fixture["dependencies"][handle])
        if record["repository_snapshot_id"] != snapshot_id:
            problems.append(f"{handle}: repository_snapshot_id differs from the prepared snapshot")
        evidence = []
        for item in record["evidence"]:
            item = dict(item)
            if item["kind"] == "source":
                if item["source_uri"] != f"repo://{repository}" or item["revision"] != placeholder:
                    problems.append(f"{handle}: evidence {item['id']} is not an unbound item of {repository}")
                lines = files.lines(item["file"])
                if lines is None:
                    problems.append(f"{handle}: {item['file']} is not in commit {commit[:12]}")
                else:
                    problem = _line_problem(lines, item["file"], item["line_start"], item["line_end"], item["excerpt"])
                    if problem:
                        problems.append(f"{handle}: {problem}")
                checked += 1
                item["revision"] = commit
            evidence.append(item)
        record["evidence"] = evidence
        bound.append(Dependency.model_validate(record))
    controls = []
    for control in fixture["negative_controls"]["items"]:
        if control["repository"] != repository:
            continue
        lines = files.lines(control["file"])
        targets = [(control["line"], control["excerpt"])] + [
            (item["line"], item["excerpt"]) for item in control.get("related_lines", [])
        ]
        for line, excerpt in targets:
            problem = (
                f"{control['file']} is not in commit {commit[:12]}"
                if lines is None
                else _line_problem(lines, control["file"], line, line, excerpt)
            )
            if problem:
                problems.append(f"negative control {control['id']}: {problem}")
        controls.append(
            {
                "id": control["id"],
                "file": control["file"],
                "line": control["line"],
                "symbol": control["symbol"],
                "reason": control["reason"],
            }
        )
    cited = {
        (item.file, line)
        for dependency in bound
        for item in dependency.evidence
        if item.file and item.line_start is not None
        for line in range(item.line_start, (item.line_end or item.line_start) + 1)
    }
    for control in controls:
        if (control["file"], control["line"]) in cited:
            problems.append(f"negative control {control['id']} is cited as dependency evidence")
    if problems:
        raise DemoError(
            f"fixture drift in {repository} at commit {commit[:12]} (refusing to bind): " + "; ".join(problems)
        )
    return bound, controls, checked


def _use_site(dependency: Dependency) -> tuple[str, int, str] | None:
    """(file, line, field) of the use site: the last located source item (scanner order)."""
    for item in reversed(dependency.evidence):
        if item.kind == "source" and item.file and item.line_start is not None:
            return item.file, item.line_start, dependency.field_path
    return None


def _scan_repository(scanner, repo: Path, snapshot, consumer: Mapping, catalog: list) -> list[Dependency]:
    """Noirit's scanner through its real interface; records re-validated as shockwave.v1 Dependency."""
    from scanner.contracts import RepositorySnapshot as ScannerSnapshot
    from scanner.contracts import ScanRequest

    scanner_snapshot = ScannerSnapshot.model_validate(snapshot.model_dump(mode="json"))
    request = ScanRequest(
        repository_snapshot_id=str(scanner_snapshot.id),
        enrollment_id=consumer["enrollment_id"],
        service_id="stripe",
        endpoint_catalog=[endpoint.model_dump(mode="json") for endpoint in catalog],
        supported_languages=["python", "javascript", "typescript"],
    )
    result = scanner(request, scanner_snapshot, repo, consumer_org_id=consumer["consumer_org_id"])
    return [Dependency.model_validate(item.model_dump(mode="json")) for item in result.dependencies]


def run_scan(layout: Layout, *, use_fixture: bool = False) -> list[str]:
    snapshots, repositories = load_snapshots(layout)
    ids = read_json(IDS_PATH)
    scanner = None if use_fixture else _scanner()
    fixture = read_json(DEPENDENCY_FIXTURE_PATH)
    catalog = []
    if scanner is not None:
        inventory = read_result(layout, "inventory.json", produced_by="inventory")
        from shockwave_demo.inventory import discover_stripe

        catalog = list(discover_stripe(inventory["contract"]["snapshot_id"]).result.endpoints)
        lines = [f"Scan consumers - {SCANNER_LABEL}, checked site by site against the line-verified fixture"]
    else:
        lines = [f"Scan consumers - {SCAN_FIXTURE_LABEL}", f"  {fixture['label']}"]
    for repository, details in repositories.items():
        consumer = ids["consumers"][repository]
        snapshot = snapshots[details["repository_snapshot_id"]]
        repo = layout.repo(repository)
        expected, controls, checked = bind_fixture_records(
            fixture, repository, details["commit_sha"], repo, details["repository_snapshot_id"]
        )
        records, source, label, comparison = expected, SCAN_FIXTURE_LABEL, fixture["label"], None
        if scanner is not None:
            scanned = _scan_repository(scanner, repo, snapshot, consumer, catalog)
            wanted = {_use_site(record) for record in expected}
            found = {_use_site(record) for record in scanned}
            comparison = {
                "scanner_found": sorted(f"{f}:{n} {field}" for f, n, field in wanted & found),
                "scanner_missed": sorted(f"{f}:{n} {field}" for f, n, field in wanted - found),
                "scanner_extra": sorted(f"{f}:{n} {field}" for f, n, field in found - wanted),
            }
            if not comparison["scanner_missed"]:
                records, source = scanned, SCANNER_LABEL
                label = f"{SCANNER_LABEL}: real scan; reproduced all {len(wanted)} expected use sites"
            else:
                label = (
                    f"{fixture['label']} Noirit's scanner ran but missed {len(comparison['scanner_missed'])} of "
                    f"{len(wanted)} expected use sites, so this repository uses the fixture."
                )
        cited = {(item.file, item.line_start) for record in records for item in record.evidence if item.file}
        for control in controls:
            if (control["file"], control["line"]) in cited:
                raise DemoError(f"negative control {control['id']} is cited as dependency evidence in {repository}")
        document = {
            "kind": "shockwave-demo.consumer-dependencies@1",
            "visibility": "consumer_private",
            "audience": {
                "consumer_org_id": consumer["consumer_org_id"],
                "consumer_org_name": consumer["consumer_org_name"],
                "consumer_org_label": consumer["consumer_org_label"],
            },
            "repository": repository,
            "repository_snapshot_id": details["repository_snapshot_id"],
            "commit_sha": details["commit_sha"],
            "source": source,
            "label": label,
            "fixture": repo_relative(DEPENDENCY_FIXTURE_PATH) if source == SCAN_FIXTURE_LABEL else None,
            "revision_binding": (
                f"{checked} source evidence items verified against commit {details['commit_sha']} (file, line, "
                "excerpt) and bound to it"
                if source == SCAN_FIXTURE_LABEL
                else "revisions as reported by the scanner (the prepared commit)"
            ),
            "scanner_comparison": comparison,
            "dependencies": [record.model_dump(mode="json") for record in records],
            "negative_controls": controls,
        }
        write_json(layout.result_path(dependencies_result(repository)), document)
        origin = "real scan" if source == SCANNER_LABEL else "FIXTURE"
        lines.append(
            f"  {repository:<27} {origin}: {count(len(records), 'dependency', 'dependencies')}, "
            f"{count(len(controls), 'negative control')} confirmed absent from dependencies; "
            f"{checked} fixture evidence lines verified at {details['commit_sha'][:12]}"
        )
        if comparison and comparison["scanner_missed"]:
            lines.append(f"    scanner missed: {', '.join(comparison['scanner_missed'])}")
    lines.append("  wrote       results/consumers/<repository>/dependencies.json (consumer-private)")
    return lines


def load_dependencies(layout: Layout) -> dict[str, list[Dependency]]:
    """repository -> its Dependency records (requires ``scan``)."""
    _, repositories = load_snapshots(layout)
    return {
        repository: [
            Dependency.model_validate(item)
            for item in read_result(layout, dependencies_result(repository), produced_by="scan")["dependencies"]
        ]
        for repository in repositories
    }
