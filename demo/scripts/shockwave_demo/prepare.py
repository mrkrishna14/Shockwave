"""``prepare``: build the three consumer repositories as deterministic local git snapshots.

For each repository in demo/consumer-repos.manifest.json the template is copied (without
installed packages or caches) to ``<work-dir>/repos/<name>``, ``git init`` (branch main, no
hooks, no remote) and ONE commit with a fixed identity and fixed dates, so identical template
content gives the same commit SHA on every run. node_modules is never committed (template
.gitignore). The checkout's stripe-node is installed with ``npm ci`` (``--offline``: npm cache
only; otherwise ``--prefer-offline``, which may reach the npm registry when the cache is empty)
and ``--ignore-scripts``; stripe-python must already be importable in this venv. Each consumer's
OWN test suite then runs on a copy of the prepared repository and its counts are recorded.
RepositorySnapshot records go to ``<work-dir>/snapshots.json`` (and ``results/snapshots.json``).
Nothing is pushed or published: demo/scripts/publish_consumer_repos.py does that, only when the
operating user authorizes it.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import shutil
import time
from collections.abc import Mapping

from discovery.contracts import RepositorySnapshot
from shockwave_demo.common import (
    CONSUMER_MANIFEST_PATH,
    CONSUMER_SUITE_LABEL,
    DEFAULT_NPM,
    IDS_PATH,
    LOCK_PATH,
    REPO_ROOT,
    SNAPSHOT_FETCHED_AT,
    TIMING_NOTE,
    DemoError,
    Layout,
    base_env,
    display_argv,
    display_path,
    git,
    log_tail,
    own_suite_command,
    read_json,
    remove_tree,
    run_process,
    scratch_copy,
    suite_counts,
    write_json,
)
from shockwave_demo.records import write_run_record

SNAPSHOTS_RESULT = "snapshots.json"
PREPARE_RESULT = "prepare.json"
COMMIT_MESSAGE = (
    "Shockwave Stripe demo consumer snapshot: {name}\n\n"
    "Team-created demo application prepared from {template} by `demo.py prepare`.\n"
    "Not a Stripe customer application; not affiliated with or endorsed by Stripe.\n"
)
_COPY_EXCLUDE = (".git", "node_modules", "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".DS_Store", "*.sqlite")
SNAPSHOT_LABEL = (
    "LOCAL snapshot: a deterministic local git commit of the team-created template (fixed identity and dates); "
    "not fetched from GitHub. fetched_at is the fixed commit date."
)


def committed_content_hash(repo) -> str:
    """sha256 over the committed files (sorted paths and bytes, from ``git ls-files``)."""
    digest = hashlib.sha256()
    for relative in sorted(item for item in git(["ls-files", "-z"], repo).split("\0") if item):
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update((repo / relative).read_bytes() + b"\0")
    return f"sha256:{digest.hexdigest()}"


def _sources(lock: Mapping) -> dict:
    return {item["key"]: item for item in lock["sources"]}


def check_python_sdk(lock: Mapping) -> str:
    expected = _sources(lock)["stripe-python"]["version"]
    try:
        installed = importlib.metadata.version("stripe")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed != expected:
        raise DemoError(
            f"stripe=={expected} must be importable in this Python ({installed or 'not installed'}); install the "
            "templates' requirements.txt into .venv"
        )
    return f"stripe=={installed} importable in this Python"


def install_node_modules(layout: Layout, repo, lock: Mapping) -> dict:
    """``npm ci`` in the prepared checkout (lockfile-exact; scripts disabled)."""
    expected = _sources(lock)["stripe-node"]["version"]
    mode = "--offline" if layout.offline else "--prefer-offline"
    argv = [str(DEFAULT_NPM), "ci", mode, "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"]
    env = base_env({"npm_config_update_notifier": "false"})
    completed = run_process(argv, repo, env=env, timeout=300)
    if completed.returncode != 0:
        hint = (
            "the npm cache does not hold the locked packages; run `.venv/bin/python demo/scripts/demo.py prepare` "
            "once with network access (it uses --prefer-offline), then rerun"
            if layout.offline
            else "check network access to the npm registry"
        )
        tail = log_tail(completed.stdout, layout, 600)["text"]
        raise DemoError(f"npm ci failed in {repo.name} ({hint}): {tail.strip()}")
    package = repo / "node_modules" / "stripe" / "package.json"
    installed = json.loads(package.read_text(encoding="utf-8")).get("version") if package.is_file() else None
    if installed != expected:
        raise DemoError(f"{repo.name}: node_modules has stripe {installed}, the lock pins {expected}")
    return {
        "argv": display_argv(argv, layout),
        "exit_code": completed.returncode,
        "installed": f"stripe@{installed}",
        "mode": "npm cache only" if layout.offline else "npm cache first, registry if needed",
    }


def snapshot_repository(layout: Layout, entry: Mapping, ids: Mapping) -> tuple[RepositorySnapshot, dict]:
    name = entry["name"]
    template = REPO_ROOT / entry["template"]
    if not template.is_dir():
        raise DemoError(f"missing template {entry['template']}")
    repo = layout.repo(name)
    layout.repos_dir.mkdir(parents=True, exist_ok=True)
    remove_tree(repo, inside=layout.repos_dir)
    shutil.copytree(template, repo, ignore=shutil.ignore_patterns(*_COPY_EXCLUDE))
    git(["init", "-q", "--template=", "-b", "main"], repo)
    git(["add", "-A"], repo)
    message = COMMIT_MESSAGE.format(name=name, template=entry["template"])
    git(["commit", "-q", "--no-verify", "-m", message], repo)
    commit = git(["rev-parse", "HEAD"], repo).strip()
    if git(["status", "--porcelain"], repo).strip():
        raise DemoError(f"{name}: the prepared repository is not clean after its commit")
    consumer = ids["consumers"][name]
    snapshot = RepositorySnapshot(
        id=consumer["repository_snapshot_id"],
        repository_connection_id=consumer["repository_connection_id"],
        commit_sha=commit,
        fetched_at=SNAPSHOT_FETCHED_AT,
        content_hash=committed_content_hash(repo),
    )
    files = sorted(item for item in git(["ls-files", "-z"], repo).split("\0") if item)
    return snapshot, {
        "repository": name,
        "template": entry["template"],
        "language": entry["language"],
        "branch": "main",
        "commit_sha": commit,
        "files_committed": len(files),
        "node_modules_committed": any(item.startswith("node_modules/") for item in files),
        "remote": None,
        "consumer_org_id": consumer["consumer_org_id"],
        "consumer_org_name": consumer["consumer_org_name"],
        "enrollment_id": consumer["enrollment_id"],
        "repository_connection_id": consumer["repository_connection_id"],
        "repository_snapshot_id": consumer["repository_snapshot_id"],
    }


def consumer_fixture(repo) -> dict:
    """The consumer's bundled fixture data (fixture set id and sha256), the suite's fixture revision."""
    for candidate in ("test/fixtures/payment_intents.json", "tests/fixtures/payment_intents.json"):
        path = repo / candidate
        if path.is_file():
            data = path.read_bytes()
            return {
                "path": candidate,
                "fixture_set_id": json.loads(data).get("fixture_set_id"),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
    return {"path": None, "fixture_set_id": None, "sha256": None}


def run_own_suite(
    layout: Layout,
    entry: Mapping,
    commit: str,
    run_id: str,
    *,
    stage: str,
    purpose: str,
    patch=None,
    replace_fixture: bytes | None = None,
) -> dict:
    """Run the consumer's own test command on a scratch copy of the prepared repository."""
    name = entry["name"]
    copy = layout.scratch_dir / f"suite-{run_id}" / name
    scratch_copy(layout.repo(name), copy, inside=layout.scratch_dir)
    try:
        if patch is not None:
            patch(copy)
        fixture = consumer_fixture(copy)
        if replace_fixture is not None and fixture["path"]:
            (copy / fixture["path"]).write_bytes(replace_fixture)
            fixture = consumer_fixture(copy)
        command = own_suite_command(entry, copy, layout.node)
        home = copy.parent / "home"
        home.mkdir(exist_ok=True)
        started = time.monotonic()
        completed = run_process(command.argv, copy, env=base_env(home=home), timeout=300)
        measured_ms = int((time.monotonic() - started) * 1000)
        counts = suite_counts(command.kind, completed.stdout)
    finally:
        remove_tree(copy.parent, inside=layout.scratch_dir)
    outcome = "passed" if completed.returncode == 0 and counts["tests"] > 0 and counts["failed"] == 0 else (
        "failed" if counts["failed"] else "inconclusive"
    )
    record = {
        "run_id": run_id,
        "stage": stage,
        "purpose": purpose,
        "runner": CONSUMER_SUITE_LABEL,
        "repository": name,
        "commit_sha": commit,
        "scenario_id": None,
        "phase": None,
        "fixture": {"source": "the consumer's bundled fixture data", **fixture},
        "command": {
            "declared": command.declared,
            "argv": command.shown,
            "cwd": display_path(copy, layout),
        },
        "exit_code": completed.returncode,
        "timed_out": False,
        "infrastructure_error": None,
        "tests_run": counts["tests"],
        "tests_passed": counts["passed"],
        "tests_failed": counts["failed"],
        "failing_tests": counts["failing_tests"],
        "outcome": outcome,
        "assertions": [],
        "duration_ms": 0,
    }
    write_run_record(layout, record, completed.stdout, measured_ms)
    return record


def run_prepare(layout: Layout) -> list[str]:
    lock = read_json(LOCK_PATH)
    manifest = read_json(CONSUMER_MANIFEST_PATH)
    ids = read_json(IDS_PATH)
    lines = ["Prepare consumer repositories - team-created demo applications (not Stripe customers)"]
    lines.append(f"  python SDK  {check_python_sdk(lock)}")
    node_version = run_process([layout.node, "--version"], REPO_ROOT).stdout.strip()
    if not re.fullmatch(r"v(\d+)\.(\d+)\.\d+", node_version) or tuple(
        int(part) for part in node_version[1:].split(".")[:2]
    ) < (22, 18):
        raise DemoError(f"Node.js >= 22.18 is required at {layout.node} (found {node_version or 'nothing'})")
    lines.append(f"  node        {node_version} ({display_argv([layout.node])[0]})")
    snapshots: list[RepositorySnapshot] = []
    repositories: dict[str, dict] = {}
    suites: dict[str, dict] = {}
    for entry in manifest["repositories"]:
        snapshot, details = snapshot_repository(layout, entry, ids)
        repo = layout.repo(entry["name"])
        installed = ""
        if (repo / "package.json").is_file():
            install = install_node_modules(layout, repo, lock)
            # The install mode (cache only or not) varies by invocation: kept out of the normalized results.
            write_json(layout.raw_runs_dir / f"prepare.{entry['name']}.npm-ci.json", {"kind": "shockwave-demo.install@1", **install})
            details["dependency"] = f"{install['installed']} (node_modules via npm ci; not committed)"
            installed = f"; {install['installed']} via npm ci ({install['mode']})"
        else:
            details["dependency"] = f"stripe=={_sources(lock)['stripe-python']['version']} (this Python)"
        snapshots.append(snapshot)
        repositories[entry["name"]] = details
        lines.append(
            f"  {entry['name']:<27} commit {snapshot.commit_sha[:12]} ({details['files_committed']} files, branch main, "
            f"no remote){installed}"
        )
    write_snapshots(layout, snapshots, repositories)
    for entry in manifest["repositories"]:
        name = entry["name"]
        record = run_own_suite(
            layout,
            entry,
            repositories[name]["commit_sha"],
            f"prepare.{name}.own-suite",
            stage="prepare",
            purpose="the consumer's own test suite on the prepared commit (its bundled baseline fixture data)",
        )
        suites[name] = record
        lines.append(
            f"  own suite   {name}: {record['outcome']} - {record['tests_passed']}/{record['tests_run']} passed "
            f"(exit {record['exit_code']}; {record['command']['declared']})"
        )
        if record["outcome"] != "passed":
            raise DemoError(f"{name}: its own test suite did not pass on the prepared commit ({record['outcome']})")
    write_json(
        layout.result_path(PREPARE_RESULT),
        {
            "kind": "shockwave-demo.prepare@1",
            "label": SNAPSHOT_LABEL,
            "runner": CONSUMER_SUITE_LABEL,
            "timing": TIMING_NOTE,
            "repositories": {
                name: {
                    "commit_sha": details["commit_sha"],
                    "files_committed": details["files_committed"],
                    "node_modules_committed": details["node_modules_committed"],
                    "dependency": details["dependency"],
                    "own_suite": {
                        key: suites[name][key]
                        for key in ("run_id", "outcome", "tests_run", "tests_passed", "tests_failed", "exit_code")
                    }
                    | {"command": suites[name]["command"]["declared"], "fixture": suites[name]["fixture"]},
                }
                for name, details in repositories.items()
            },
        },
    )
    lines.append(f"  wrote       {display_path(layout.snapshots_path, layout)} and results/{SNAPSHOTS_RESULT}, {PREPARE_RESULT}")
    return lines


def write_snapshots(layout: Layout, snapshots: list[RepositorySnapshot], repositories: Mapping[str, dict]) -> None:
    document = {
        "kind": "shockwave-demo.repository-snapshots@1",
        "label": SNAPSHOT_LABEL,
        "records": [snapshot.model_dump(mode="json") for snapshot in snapshots],
        "repositories": dict(repositories),
    }
    write_json(layout.snapshots_path, document)
    write_json(layout.result_path(SNAPSHOTS_RESULT), document)


def load_snapshots(layout: Layout) -> tuple[dict[str, RepositorySnapshot], dict[str, dict]]:
    """(snapshot id -> RepositorySnapshot, repository name -> details); requires ``prepare``."""
    if not layout.snapshots_path.is_file():
        raise DemoError(f"missing {display_path(layout.snapshots_path, layout)}: run `demo.py prepare` first")
    document = read_json(layout.snapshots_path)
    snapshots = {record["id"]: RepositorySnapshot.model_validate(record) for record in document["records"]}
    for name, details in document["repositories"].items():
        repo = layout.repo(name)
        if not (repo / ".git").is_dir():
            raise DemoError(f"{name} is not prepared at {display_path(repo, layout)}: run `demo.py prepare` first")
        head = git(["rev-parse", "HEAD"], repo).strip()
        if head != details["commit_sha"]:
            raise DemoError(f"{name}: HEAD {head[:12]} differs from the recorded snapshot; rerun `demo.py prepare`")
    return snapshots, document["repositories"]
