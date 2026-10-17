"""Offline checks for the prepared private-GitHub onboarding (task B).

Covers demo/github/*.json, demo/github/register-app.html, demo/GITHUB_SETUP.md and
demo/scripts/publish_consumer_repos.py. Self-contained: it uses no fixture from
tests/demo/conftest.py. Nothing here reaches the network: the publish script runs in
subprocesses behind a guard that makes every socket connection and child process raise, and its
execution path is exercised in-process with fake GitHub and git doubles. The only real child
process is a local ``git credential fill`` that checks the credential helper.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GITHUB_DIR = REPO_ROOT / "demo" / "github"
READER_MANIFEST = GITHUB_DIR / "app-manifest.json"
PR_WRITER_MANIFEST = GITHUB_DIR / "app-manifest.pr-writer.json"
FLOW = GITHUB_DIR / "onboarding-flow.json"
DOCS = GITHUB_DIR / "docs-verified.json"
REGISTER_PAGE = GITHUB_DIR / "register-app.html"
SETUP_DOC = REPO_ROOT / "demo" / "GITHUB_SETUP.md"
SCRIPT = REPO_ROOT / "demo" / "scripts" / "publish_consumer_repos.py"
CONSUMER_MANIFEST = REPO_ROOT / "demo" / "consumer-repos.manifest.json"

CHECKED_AT = "2026-10-17"
TEAM = {"Ishan", "Sai", "Noirit", "Kanishk"}
AUTHORIZATIONS = {"sign_in", "repository_access", "analysis_consent", "pr_write"}
REQUIRED_STEPS = [
    "provider_invitation",
    "authenticated_consumer_acceptance",
    "github_app_installation",
    "explicit_private_repository_selection",
    "analysis_consent",
    "pinned_snapshot_fetch",
    "scan",
    "private_consumer_findings",
    "restricted_provider_summary",
]
NEVER_VISIBLE_REQUIRED = {"consumer_source", "private_paths", "code_snippets", "logs", "patch_artifacts"}
MANIFEST_KEYS = {
    "name",
    "url",
    "hook_attributes",
    "redirect_url",
    "callback_urls",
    "setup_url",
    "description",
    "public",
    "default_events",
    "default_permissions",
    "request_oauth_on_install",
    "setup_on_update",
}
DEFAULT_DELIVERED_EVENTS = {"installation", "installation_repositories", "github_app_authorization"}
PLACEHOLDER_BASE = "https://shockwave.example.invalid/"
FAKE_TOKEN = "test-token-not-real-0123456789abcdef"  # never a real credential
TOKEN_PATTERNS = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|sk_live_[A-Za-z0-9]+)"
)

# Runs the publish script with every socket connection and child process blocked, so a test
# fails loudly (traceback, exit 1) instead of reaching the network.
NETWORK_GUARD = r"""
import os, runpy, socket, subprocess, sys

def _blocked(*args, **kwargs):
    raise RuntimeError("blocked by test_github_setup: no network access and no child processes")

socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
subprocess.Popen = _blocked
os.system = _blocked
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        raise ValueError(f"duplicate keys: {sorted(duplicates)}")
    return dict(pairs)


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant {value}")


def load_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
    )


@pytest.fixture(scope="module")
def flow() -> dict[str, Any]:
    return load_json(FLOW)


@pytest.fixture(scope="module")
def docs() -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in load_json(DOCS)["entries"]}


@pytest.fixture(scope="module")
def consumer_names() -> list[str]:
    return [entry["name"] for entry in load_json(CONSUMER_MANIFEST)["repositories"]]


@pytest.fixture(scope="module")
def publisher() -> Iterator[ModuleType]:
    """The publish script imported in-process (no bytecode written)."""
    name = "shockwave_demo_publish_consumer_repos"
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    yield module
    sys.modules.pop(name, None)


def run_script(*args: str, token: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    if token is not None:
        env["GITHUB_TOKEN"] = token
    return subprocess.run(
        [sys.executable, "-B", "-s", "-c", NETWORK_GUARD, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def assert_no_traceback(result: subprocess.CompletedProcess[str]) -> None:
    assert "Traceback" not in result.stderr, result.stderr
    assert "blocked by test_github_setup" not in result.stderr + result.stdout


# Static assets --------------------------------------------------------------------------------


def test_github_json_files_parse_strictly() -> None:
    paths = sorted(GITHUB_DIR.glob("*.json"))
    assert {path.name for path in paths} >= {
        "app-manifest.json",
        "app-manifest.pr-writer.json",
        "onboarding-flow.json",
        "docs-verified.json",
    }
    for path in paths:
        assert isinstance(load_json(path), dict), path


def test_reader_manifest_is_read_only() -> None:
    manifest = load_json(READER_MANIFEST)
    assert manifest["default_permissions"] == {"metadata": "read", "contents": "read"}
    assert all(level == "read" for level in manifest["default_permissions"].values())
    assert "write" not in json.dumps(manifest["default_permissions"])
    assert "admin" not in json.dumps(manifest["default_permissions"])
    assert manifest["public"] is False
    assert manifest["request_oauth_on_install"] is False  # sign-in and installation stay separate
    assert manifest["callback_urls"] == [PLACEHOLDER_BASE + "auth/github/callback"]  # sign-in only
    assert manifest["default_events"] == ["repository"]


def test_pr_writer_manifest_has_only_pr_permissions() -> None:
    manifest = load_json(PR_WRITER_MANIFEST)
    assert manifest["default_permissions"] == {"metadata": "read", "contents": "write", "pull_requests": "write"}
    assert manifest["public"] is False
    assert manifest["request_oauth_on_install"] is False
    assert "callback_urls" not in manifest  # no sign-in through the PR Writer
    assert manifest["default_events"] == []


@pytest.mark.parametrize("path", [READER_MANIFEST, PR_WRITER_MANIFEST], ids=["reader", "pr_writer"])
def test_manifests_use_documented_keys_and_placeholder_urls(path: Path) -> None:
    manifest = load_json(path)
    assert set(manifest) <= MANIFEST_KEYS
    assert {"name", "url", "hook_attributes", "default_permissions", "public"} <= set(manifest)
    assert 0 < len(manifest["name"]) <= 34
    assert manifest["hook_attributes"]["active"] is True
    urls = [manifest["url"], manifest["hook_attributes"]["url"], manifest["redirect_url"], manifest["setup_url"]]
    urls += manifest.get("callback_urls", [])
    assert all(url.startswith(PLACEHOLDER_BASE) for url in urls), urls
    assert not DEFAULT_DELIVERED_EVENTS & set(manifest["default_events"])
    assert manifest["setup_on_update"] is True


def test_reader_and_pr_writer_are_distinct_apps() -> None:
    reader, writer = load_json(READER_MANIFEST), load_json(PR_WRITER_MANIFEST)
    assert reader["name"] != writer["name"]
    assert reader["hook_attributes"]["url"] != writer["hook_attributes"]["url"]
    assert reader["redirect_url"] != writer["redirect_url"]


def test_docs_verified_entries_are_complete(docs: dict[str, dict[str, Any]]) -> None:
    data = load_json(DOCS)
    assert data["checked_at"] == CHECKED_AT
    ids = [entry["id"] for entry in data["entries"]]
    assert len(ids) == len(set(ids))
    for entry in data["entries"]:
        assert entry["url"].startswith(("https://docs.github.com/", "https://git-scm.com/")), entry["id"]
        assert entry["checked_at"] == CHECKED_AT, entry["id"]
        assert isinstance(entry["fact"], str) and len(entry["fact"]) > 20, entry["id"]
        assert isinstance(entry["verified"], bool), entry["id"]
        if not entry["verified"]:
            assert entry.get("reason"), entry["id"]
    required = {
        "manifest-flow",
        "manifest-parameters",
        "manifest-code-exchange",
        "minimum-permissions",
        "permissions-required-github-apps",
        "permission-change-approval",
        "install-selected-repositories",
        "user-access-token-web-flow",
        "installation-access-token",
        "webhook-installation",
        "webhook-installation-repositories",
        "webhook-github-app-authorization",
        "tarball-snapshot",
        "create-repository-user",
        "create-repository-org",
        "rest-api-version",
    }
    assert required <= set(docs)
    assert all(docs[entry_id]["verified"] for entry_id in required)
    assert any(not entry["verified"] for entry in data["entries"])  # unverified facts are recorded, not hidden


def test_manifest_keys_match_the_verified_parameter_list(docs: dict[str, dict[str, Any]]) -> None:
    fact = docs["manifest-parameters"]["fact"]
    for key in MANIFEST_KEYS:
        assert key in fact, key


def test_api_version_is_consistent(docs: dict[str, dict[str, Any]], flow: dict[str, Any], publisher: ModuleType) -> None:
    version = docs["rest-api-version"]["value"]
    assert docs["rest-api-version"]["verified"] is True
    assert flow["github_api_version"] == version
    assert publisher.API_VERSION == version


# Onboarding flow ------------------------------------------------------------------------------


def test_flow_has_required_steps_in_order(flow: dict[str, Any]) -> None:
    steps = flow["steps"]
    assert [step["id"] for step in steps] == REQUIRED_STEPS
    assert [step["order"] for step in steps] == list(range(1, len(REQUIRED_STEPS) + 1))
    states = flow["states"]
    assert [step["from_state"] for step in steps] == states[:-1]
    assert [step["to_state"] for step in steps] == states[1:]
    for step in steps:
        assert isinstance(step["actor"], str) and step["actor"], step["id"]
        assert step["owner"] in TEAM, step["id"]
        assert set(step["collaborators"]) <= TEAM, step["id"]
        assert isinstance(step["github"], list), step["id"]
        assert step["stores"] and all(isinstance(item, str) for item in step["stores"]), step["id"]
        assert isinstance(step["provider_visible"], list), step["id"]
        assert isinstance(step["revocation"], str) and step["revocation"], step["id"]
        assert isinstance(step["demo_status"], str) and step["demo_status"], step["id"]


def test_flow_owners_follow_team_responsibilities(flow: dict[str, Any]) -> None:
    owners = {step["id"]: step["owner"] for step in flow["steps"]}
    assert owners["provider_invitation"] == "Ishan"
    assert owners["authenticated_consumer_acceptance"] == "Ishan"
    assert owners["github_app_installation"] == "Sai"
    assert owners["explicit_private_repository_selection"] == "Ishan"
    assert owners["analysis_consent"] == "Ishan"
    assert owners["pinned_snapshot_fetch"] == "Sai"
    assert owners["scan"] == "Noirit"
    assert owners["private_consumer_findings"] == "Kanishk"
    assert owners["restricted_provider_summary"] == "Ishan"
    pr_steps = {step["id"]: step["owner"] for step in flow["optional_pr_branch"]["steps"]}
    assert pr_steps == {"pr_write_authorization": "Ishan", "migration_pr_publication": "Sai"}


def _github_entries(flow: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for step in flow["steps"] + flow["optional_pr_branch"]["steps"]:
        for entry in step["github"]:
            yield step["id"], entry


def test_flow_github_calls_cite_verified_docs(flow: dict[str, Any], docs: dict[str, dict[str, Any]]) -> None:
    entries = list(_github_entries(flow))
    assert entries
    for step_id, entry in entries:
        assert entry["kind"] in {"browser", "oauth", "rest", "redirect", "webhook", "git"}, step_id
        assert entry["docs"], (step_id, entry["call"])
        for doc_id in entry["docs"]:
            assert docs[doc_id]["verified"] is True, (step_id, doc_id)
    for step in flow["steps"]:
        for doc_id in step.get("unverified_caveats", []):
            assert docs[doc_id]["verified"] is False, doc_id
    for section in ("reader", "pr_writer"):
        for doc_id in flow["apps"][section]["docs"]:
            assert docs[doc_id]["verified"] is True, doc_id
    for name, authorization in flow["authorizations"].items():
        for doc_id in authorization["docs"]:
            assert docs[doc_id]["verified"] is True, (name, doc_id)


def test_flow_github_usage_per_step(flow: dict[str, Any]) -> None:
    steps = {step["id"]: step for step in flow["steps"]}
    for step_id in ("authenticated_consumer_acceptance", "github_app_installation",
                    "explicit_private_repository_selection", "pinned_snapshot_fetch"):
        assert steps[step_id]["github"], step_id
    for step_id in ("provider_invitation", "analysis_consent", "scan", "private_consumer_findings",
                    "restricted_provider_summary"):
        assert steps[step_id]["github"] == [], step_id
    install_calls = " ".join(entry["call"] for entry in steps["github_app_installation"]["github"])
    assert "Only select repositories" in install_calls
    assert "GET /user/installations" in install_calls  # the setup URL installation_id is never trusted
    fetch_calls = " ".join(entry["call"] for entry in steps["pinned_snapshot_fetch"]["github"])
    assert "POST /app/installations/{installation_id}/access_tokens" in fetch_calls
    assert "repository_ids" in fetch_calls and "contents: read" in fetch_calls
    assert "write" not in fetch_calls
    assert "/tarball/" in fetch_calls and "/commits/" in fetch_calls


def test_four_authorizations_are_separate(flow: dict[str, Any]) -> None:
    authorizations = flow["authorizations"]
    assert set(authorizations) == AUTHORIZATIONS
    for name, authorization in authorizations.items():
        for field in ("grants", "mechanism", "granted_by", "stored_as", "effect_of_revocation", "owner"):
            assert isinstance(authorization[field], str) and authorization[field], (name, field)
        assert authorization["does_not_grant"] and authorization["revoked_by"], name
    assert {"repository_access", "analysis_consent", "pr_write"} <= set(authorizations["sign_in"]["does_not_grant"])
    assert {"analysis_consent", "pr_write"} <= set(authorizations["repository_access"]["does_not_grant"])
    assert {"repository_access", "pr_write"} <= set(authorizations["analysis_consent"]["does_not_grant"])

    steps = {step["id"]: step for step in flow["steps"]}
    for step in flow["steps"] + flow["optional_pr_branch"]["steps"]:
        assert set(step["requires_authorizations"]) <= AUTHORIZATIONS, step["id"]
    for step_id in ("pinned_snapshot_fetch", "scan", "private_consumer_findings", "restricted_provider_summary"):
        assert {"repository_access", "analysis_consent"} <= set(steps[step_id]["requires_authorizations"]), step_id
        assert "pr_write" not in steps[step_id]["requires_authorizations"], step_id
    for step_id in ("authenticated_consumer_acceptance", "explicit_private_repository_selection", "analysis_consent"):
        assert "sign_in" in steps[step_id]["requires_authorizations"], step_id
    requiring_pr_write = [
        step["id"]
        for step in flow["steps"] + flow["optional_pr_branch"]["steps"]
        if "pr_write" in step["requires_authorizations"]
    ]
    assert requiring_pr_write == ["migration_pr_publication"]
    assert flow["apps"]["decision"] == "two_apps"
    assert flow["apps"]["reasons"] and flow["apps"]["tradeoffs"] and flow["apps"]["rejected_alternative"]


def test_provider_never_sees_consumer_private_material(flow: dict[str, Any]) -> None:
    visibility = flow["provider_visibility"]
    never = set(visibility["never_visible"])
    allowed = set(visibility["allowed_fields"])
    assert NEVER_VISIBLE_REQUIRED <= never
    assert not allowed & never
    # The provider's own contract identifiers are not consumer data; everything else must not even
    # sound like consumer material.
    provider_owned = {"summary.change_id", "summary.endpoint", "summary.field_path"}
    assert provider_owned <= allowed
    for field in provider_owned:
        assert "provider" in visibility["allowed_fields"][field], field
    forbidden_words = ("source", "path", "snippet", "log", "patch", "commit", "repository", "url", "link")
    for field in allowed - provider_owned:
        assert not any(word in field.lower() for word in forbidden_words), field
    for step in flow["steps"] + flow["optional_pr_branch"]["steps"]:
        assert set(step["provider_visible"]) <= allowed, step["id"]
    steps = {step["id"]: step for step in flow["steps"]}
    for step_id in ("github_app_installation", "explicit_private_repository_selection", "pinned_snapshot_fetch",
                    "scan", "private_consumer_findings"):
        assert not any(field.startswith("summary.") for field in steps[step_id]["provider_visible"]), step_id
    for step_id in ("pinned_snapshot_fetch", "scan", "private_consumer_findings"):
        assert steps[step_id]["provider_visible"] == [], step_id
    for step in flow["optional_pr_branch"]["steps"]:
        assert step["provider_visible"] == [], step["id"]
    assert steps["restricted_provider_summary"]["provider_visible"]


def test_revocation_stops_new_processing_and_publication(flow: dict[str, Any], docs: dict[str, dict[str, Any]]) -> None:
    events = flow["revocation_events"]
    triggers = {(event["app"], event["event"], event["action"]) for event in events}
    assert {
        ("reader", "installation", "deleted"),
        ("reader", "installation", "suspend"),
        ("reader", "installation_repositories", "removed"),
        ("reader", "github_app_authorization", "revoked"),
        (None, "consent_withdrawn", None),
        (None, "repository_deselected", None),
        ("pr_writer", "installation", "deleted"),
        ("pr_writer", "installation_repositories", "removed"),
        (None, "pr_authorization_withdrawn", None),
    } <= triggers
    for event in events:
        assert event["affects_authorization"] in AUTHORIZATIONS
        assert event["effect"]
        if event["affects_authorization"] in {"repository_access", "analysis_consent"}:
            assert {"new_processing", "publication"} <= set(event["stops"]), event
        if event["affects_authorization"] == "pr_write":
            assert "pr_publication" in event["stops"], event
        if event["source"] == "github_webhook":
            assert event["docs"] and all(docs[doc_id]["verified"] for doc_id in event["docs"]), event
    added = [event for event in flow["non_revocation_events"] if event["action"] == "added"]
    assert added and "never selected or consented automatically" in added[0]["effect"]


def test_queued_jobs_recheck_before_fetch_scan_and_publication(flow: dict[str, Any]) -> None:
    guards = flow["job_guards"]
    checks, checkpoints = set(guards["checks"]), guards["checkpoints"]
    assert {
        "before_snapshot_fetch",
        "before_scan",
        "before_consumer_findings_publication",
        "before_provider_summary_publication",
        "before_pr_branch_push",
        "before_pr_open",
    } <= set(checkpoints)
    for name, required in checkpoints.items():
        assert set(required) <= checks, name
        assert {"reader_installation_active", "repository_connection_active", "analysis_consent_active"} <= set(
            required
        ), name
    assert "repository_in_installation" in checkpoints["before_snapshot_fetch"]
    for name in ("before_pr_branch_push", "before_pr_open"):
        assert {"pr_writer_installation_active", "pr_authorization_active"} <= set(checkpoints[name]), name
    assert "branch_policy" in checkpoints["before_pr_branch_push"]


def test_flow_carries_the_required_labels(flow: dict[str, Any]) -> None:
    labels = " ".join(flow["labels"].values())
    for phrase in (
        "SIMULATED",
        "not a Shockwave customer",
        "FIXTURE until Noirit's scanner exists",
        "LOCAL RUNNER DOUBLE",
        "not a sandbox; not Sai's runner",
        "no LLM",
        "provider source discovery is unavailable",
        "does NOT prove cross-company identity isolation",
    ):
        assert phrase in labels, phrase
    assert "Nothing has been created, pushed or installed" in flow["status"]


def test_environment_variable_names_are_documented(flow: dict[str, Any]) -> None:
    text = SETUP_DOC.read_text(encoding="utf-8")
    names = [variable["name"] for variable in flow["environment_variables"]]
    assert "GITHUB_TOKEN" in names
    for name in names:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", name), name
        assert f"`{name}`" in text, name


def test_setup_doc_states_what_was_and_was_not_done() -> None:
    text = SETUP_DOC.read_text(encoding="utf-8")
    flat = re.sub(r"[*_`]", "", text)
    assert "nothing has been created, pushed or installed" in flat.lower()
    assert "does not prove cross-company identity isolation" in flat
    assert "demo/github/docs-verified.json" in text
    assert "--i-authorize-github-changes" in text
    for heading in ("## Four separate authorizations", "## Why two GitHub Apps", "## Testing revocation",
                    "## Coordination notes for Sai", "## Coordination notes for Ishan",
                    "## Verified and unverified facts"):
        assert heading in text, heading


def test_register_page_posts_only_to_github() -> None:
    html = REGISTER_PAGE.read_text(encoding="utf-8")
    assert "<script src" not in html and "<link" not in html  # no external resources
    assert "https://github.com/settings/apps/new" in html
    assert "https://github.com/organizations/" in html
    hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", html))
    assert hosts <= {"github.com"}, hosts
    assert 'name="manifest"' in html and "crypto.getRandomValues" in html


def test_no_secrets_in_github_assets() -> None:
    for path in [*GITHUB_DIR.iterdir(), SETUP_DOC, SCRIPT]:
        if path.is_file():
            assert not TOKEN_PATTERNS.search(path.read_text(encoding="utf-8")), path


# Publish script: CLI (subprocess, network blocked) -------------------------------------------


def test_dry_run_json_plan_lists_only_private_repositories(
    tmp_path: Path, consumer_names: list[str], docs: dict[str, dict[str, Any]]
) -> None:
    result = run_script("--json", "--owner", "demo-owner", "--work-dir", str(tmp_path))
    assert_no_traceback(result)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["github_changes_made"] is False and plan["network_used"] is False
    assert plan["token_present"] is False
    assert plan["visibility"] == "private"
    assert [repo["name"] for repo in plan["repositories"]] == consumer_names
    for repo in plan["repositories"]:
        assert repo["visibility"] == "private"
        assert repo["full_name"] == f"demo-owner/{repo['name']}"
        assert repo["snapshot"]["prepared"] is False  # the empty work dir has no snapshots
        posts = [call for call in repo["api_calls"] if call["method"] == "POST"]
        assert len(posts) == 1
        assert posts[0]["url"] == "https://api.github.com/user/repos"
        assert posts[0]["body"]["private"] is True
        assert posts[0]["body"]["name"] == repo["name"]
        assert repo["api_calls"][0]["method"] == "GET"  # existence check comes first
        for command in repo["git_commands"]:
            assert "--force" not in command and " -f " not in command and "+" not in command.split()[-1]
            assert "refs/heads/main" in command
    assert plan["api_version"] == docs["rest-api-version"]["value"]
    assert plan["request_headers"]["X-GitHub-Api-Version"] == plan["api_version"]
    assert FAKE_TOKEN not in result.stdout


def test_dry_run_json_plan_for_an_organization(tmp_path: Path) -> None:
    result = run_script("--json", "--owner", "demo-org", "--owner-type", "org", "--work-dir", str(tmp_path))
    assert_no_traceback(result)
    assert result.returncode == 0, result.stderr
    for repo in json.loads(result.stdout)["repositories"]:
        (post,) = [call for call in repo["api_calls"] if call["method"] == "POST"]
        assert post["url"] == "https://api.github.com/orgs/demo-org/repos"
        assert post["body"]["private"] is True and post["body"]["visibility"] == "private"


def test_dry_run_default_work_dir_is_the_prepared_snapshot_directory() -> None:
    result = run_script("--json")
    assert_no_traceback(result)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["owner"] == "<OWNER>"
    for repo in plan["repositories"]:
        assert repo["snapshot"]["path"] == f"demo/.work/repos/{repo['name']}"


def test_dry_run_text_prints_plan_and_manual_browser_steps(tmp_path: Path, consumer_names: list[str]) -> None:
    result = run_script("--owner", "demo-owner", "--work-dir", str(tmp_path))
    assert_no_traceback(result)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "DRY RUN" in out and "Nothing is created, pushed or installed" in out
    assert "Visibility: private" in out
    for name in consumer_names:
        assert f"demo-owner/{name} (private)" in out
    assert "MANUAL BROWSER STEPS" in out
    assert '"Only select repositories"' in out and 'Never choose "All repositories"' in out
    assert "demo/github/app-manifest.json" in out and "demo/github/app-manifest.pr-writer.json" in out
    assert "--force" not in out.replace("no --force", "")


def test_execute_without_authorization_flag_is_refused(tmp_path: Path) -> None:
    result = run_script("--execute", "--owner", "demo-owner", "--work-dir", str(tmp_path), token=FAKE_TOKEN)
    assert_no_traceback(result)
    assert result.returncode == 2
    assert "REFUSED" in result.stderr and "--i-authorize-github-changes" in result.stderr
    assert FAKE_TOKEN not in result.stdout + result.stderr


def test_execute_without_token_is_refused(tmp_path: Path) -> None:
    result = run_script(
        "--execute", "--i-authorize-github-changes", "--owner", "demo-owner", "--work-dir", str(tmp_path)
    )
    assert_no_traceback(result)
    assert result.returncode == 2
    assert "REFUSED" in result.stderr and "GITHUB_TOKEN" in result.stderr


def test_execute_without_owner_is_refused(tmp_path: Path) -> None:
    result = run_script("--execute", "--i-authorize-github-changes", "--work-dir", str(tmp_path), token=FAKE_TOKEN)
    assert_no_traceback(result)
    assert result.returncode == 2
    assert "--owner" in result.stderr
    assert FAKE_TOKEN not in result.stdout + result.stderr


def test_execute_with_unprepared_snapshots_is_refused(tmp_path: Path) -> None:
    result = run_script(
        "--execute", "--i-authorize-github-changes", "--owner", "demo-owner", "--work-dir", str(tmp_path),
        token=FAKE_TOKEN,
    )
    assert_no_traceback(result)
    assert result.returncode == 2
    assert "not prepared" in result.stderr
    assert FAKE_TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("visibility", ["public", "internal"])
def test_non_private_visibility_is_refused(tmp_path: Path, visibility: str) -> None:
    for extra in ([], ["--execute", "--i-authorize-github-changes", "--owner", "demo-owner"]):
        result = run_script("--visibility", visibility, "--work-dir", str(tmp_path), *extra, token=FAKE_TOKEN)
        assert_no_traceback(result)
        assert result.returncode == 2
        assert f"refusing visibility '{visibility}'" in result.stderr


def test_invalid_owner_is_refused(tmp_path: Path) -> None:
    result = run_script("--owner", "evil/owner", "--work-dir", str(tmp_path))
    assert_no_traceback(result)
    assert result.returncode == 2
    assert "not a GitHub account" in result.stderr


def test_token_value_is_never_printed(tmp_path: Path) -> None:
    for args in (("--owner", "demo-owner"), ("--json", "--owner", "demo-owner")):
        result = run_script(*args, "--work-dir", str(tmp_path), token=FAKE_TOKEN)
        assert_no_traceback(result)
        assert result.returncode == 0, result.stderr
        assert FAKE_TOKEN not in result.stdout + result.stderr
    assert "$GITHUB_TOKEN is set" in run_script("--work-dir", str(tmp_path), token=FAKE_TOKEN).stdout


# Publish script: execution path (in-process doubles, no network) ------------------------------


class FakeGitHub:
    """Answers the publish script's REST calls; records every call."""

    def __init__(self, *, login: str = "demo-owner", existing: set[str] | None = None, private: bool = True) -> None:
        self.login = login
        self.existing = existing or set()
        self.private = private
        self.pushed: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/user":
            return 200, {"login": self.login}
        if method == "GET" and parts[0] == "repos" and len(parts) == 3:
            return (200, {"name": parts[2]}) if parts[2] in self.existing else (404, {"message": "Not Found"})
        if method == "POST" and (path == "/user/repos" or (parts[0] == "orgs" and parts[-1] == "repos")):
            visibility = "private" if self.private else "public"
            return 201, {"name": body["name"], "private": self.private, "visibility": visibility}
        if method == "GET" and parts[0] == "repos" and parts[3] == "commits":
            return 200, {"sha": self.pushed.get(parts[2])}
        raise AssertionError(f"unexpected GitHub call {method} {path}")


class FakeGit:
    """Stands in for git: lists commit paths and records pushes."""

    def __init__(self, github: FakeGitHub, *, paths: tuple[str, ...] = ("package.json", "src/checkout.ts"),
                 push_stderr: str = "", push_code: int = 0) -> None:
        self.github = github
        self.paths = paths
        self.push_stderr = push_stderr
        self.push_code = push_code
        self.commands: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.commands.append((list(argv), dict(env)))
        if "ls-tree" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="\0".join(self.paths) + "\0", stderr="")
        if "push" in argv:
            if self.push_code == 0:
                name = argv[-2].rsplit("/", 1)[1].removesuffix(".git")
                self.github.pushed[name] = argv[-1].split(":", 1)[0]
            return subprocess.CompletedProcess(argv, self.push_code, stdout="", stderr=self.push_stderr)
        raise AssertionError(f"unexpected git command {argv}")


def prepare_snapshots(work_dir: Path, names: list[str]) -> dict[str, str]:
    commits = {}
    for index, name in enumerate(names):
        git_dir = work_dir / name / ".git"
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        sha = f"{index + 1:x}" * 40
        (git_dir / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
        commits[name] = sha
    return commits


def make_plan(publisher: ModuleType, work_dir: Path, *, owner: str = "demo-owner", owner_type: str = "user") -> Any:
    manifest = publisher.load_consumer_manifest()
    return publisher.build_plan(manifest, owner=owner, owner_type=owner_type, work_dir=work_dir, token_present=True)


ENV = {"PATH": "/usr/bin:/bin", "GITHUB_TOKEN": FAKE_TOKEN, "GIT_TRACE": "1", "GIT_ASKPASS": "/bin/echo"}


def test_execute_creates_private_repositories_and_pushes_without_force(
    publisher: ModuleType, tmp_path: Path, consumer_names: list[str]
) -> None:
    commits = prepare_snapshots(tmp_path, consumer_names)
    github = FakeGitHub()
    git = FakeGit(github)
    messages: list[str] = []
    published = publisher.execute_plan(make_plan(publisher, tmp_path), client=github, run=git, env=ENV,
                                       out=messages.append)

    assert [item["full_name"] for item in published] == [f"demo-owner/{name}" for name in consumer_names]
    assert all(item["visibility"] == "private" for item in published)
    assert github.pushed == commits

    methods = [(method, path) for method, path, _ in github.calls]
    assert methods[0] == ("GET", "/user")
    existence = [index for index, (m, p) in enumerate(methods) if m == "GET" and p.count("/") == 3 and p != "/user"]
    first_post = min(index for index, (m, _) in enumerate(methods) if m == "POST")
    assert len(existence) == len(consumer_names) and max(existence) < first_post  # all checked before any create
    posts = [body for method, _, body in github.calls if method == "POST"]
    assert [body["name"] for body in posts] == consumer_names
    assert all(body["private"] is True and body["auto_init"] is False for body in posts)
    assert all(path == "/user/repos" for method, path, _ in github.calls if method == "POST")

    pushes = [(argv, env) for argv, env in git.commands if "push" in argv]
    assert len(pushes) == len(consumer_names)
    for argv, env in pushes:
        assert "--force" not in argv and "-f" not in argv and not argv[-1].startswith("+")
        assert argv[-1].endswith(":refs/heads/main")
        assert argv[-2].startswith("https://github.com/demo-owner/")
        assert all(FAKE_TOKEN not in part for part in argv)
        assert argv[argv.index("push") - 1] == "core.hooksPath=/dev/null" and "--no-verify" in argv
        assert argv[3:5] == ["-c", "credential.helper="]  # resets keychain or other stored helpers first
        assert env["GITHUB_TOKEN"] == FAKE_TOKEN  # only in the environment, for the credential helper
        assert env["SHOCKWAVE_GIT_USERNAME"] == "demo-owner"
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GIT_TRACE" not in env and "GIT_ASKPASS" not in env
    for argv, env in git.commands:
        if "ls-tree" in argv:
            assert "GITHUB_TOKEN" not in env
    assert not any(FAKE_TOKEN in message for message in messages)


def test_execute_refuses_existing_repository_before_creating_anything(
    publisher: ModuleType, tmp_path: Path, consumer_names: list[str]
) -> None:
    prepare_snapshots(tmp_path, consumer_names)
    github = FakeGitHub(existing={consumer_names[-1]})
    git = FakeGit(github)
    with pytest.raises(publisher.Refusal, match="already exists"):
        publisher.execute_plan(make_plan(publisher, tmp_path), client=github, run=git, env=ENV, out=lambda _: None)
    assert not [call for call in github.calls if call[0] == "POST"]
    assert not [argv for argv, _ in git.commands if "push" in argv]


def test_execute_stops_before_pushing_when_github_does_not_report_private(
    publisher: ModuleType, tmp_path: Path, consumer_names: list[str]
) -> None:
    prepare_snapshots(tmp_path, consumer_names)
    github = FakeGitHub(private=False)
    git = FakeGit(github)
    with pytest.raises(publisher.PublishError, match="not report it as private"):
        publisher.execute_plan(make_plan(publisher, tmp_path), client=github, run=git, env=ENV, out=lambda _: None)
    assert len([call for call in github.calls if call[0] == "POST"]) == 1
    assert not [argv for argv, _ in git.commands if "push" in argv]


def test_execute_refuses_a_token_from_another_account(
    publisher: ModuleType, tmp_path: Path, consumer_names: list[str]
) -> None:
    prepare_snapshots(tmp_path, consumer_names)
    github = FakeGitHub(login="someone-else")
    with pytest.raises(publisher.Refusal, match="belongs to"):
        publisher.execute_plan(make_plan(publisher, tmp_path), client=github, run=FakeGit(github), env=ENV,
                               out=lambda _: None)
    assert github.calls == [("GET", "/user", None)]


def test_execute_for_an_organization_uses_the_org_endpoint(
    publisher: ModuleType, tmp_path: Path, consumer_names: list[str]
) -> None:
    prepare_snapshots(tmp_path, consumer_names)
    github = FakeGitHub(login="org-member")
    plan = make_plan(publisher, tmp_path, owner="demo-org", owner_type="org")
    publisher.execute_plan(plan, client=github, run=FakeGit(github), env=ENV, out=lambda _: None)
    posts = [(path, body) for method, path, body in github.calls if method == "POST"]
    assert posts and all(path == "/orgs/demo-org/repos" for path, _ in posts)
    assert all(body["private"] is True and body["visibility"] == "private" for _, body in posts)


def test_execute_refuses_snapshots_with_files_that_must_not_be_published(
    publisher: ModuleType, tmp_path: Path, consumer_names: list[str]
) -> None:
    prepare_snapshots(tmp_path, consumer_names)
    github = FakeGitHub()
    git = FakeGit(github, paths=("package.json", "node_modules/stripe/package.json", ".env"))
    with pytest.raises(publisher.Refusal, match="must not be published"):
        publisher.execute_plan(make_plan(publisher, tmp_path), client=github, run=git, env=ENV, out=lambda _: None)
    assert github.calls == []  # refused locally, before any GitHub call


def test_execute_refuses_unprepared_snapshots(publisher: ModuleType, tmp_path: Path) -> None:
    github = FakeGitHub()
    with pytest.raises(publisher.Refusal, match="not prepared"):
        publisher.execute_plan(make_plan(publisher, tmp_path), client=github, run=FakeGit(github), env=ENV,
                               out=lambda _: None)
    assert github.calls == []


def test_push_failure_message_never_contains_the_token(
    publisher: ModuleType, tmp_path: Path, consumer_names: list[str]
) -> None:
    prepare_snapshots(tmp_path, consumer_names)
    github = FakeGitHub()
    git = FakeGit(github, push_code=128, push_stderr=f"fatal: could not read Password: {FAKE_TOKEN}")
    with pytest.raises(publisher.PublishError) as failure:
        publisher.execute_plan(make_plan(publisher, tmp_path), client=github, run=git, env=ENV, out=lambda _: None)
    assert FAKE_TOKEN not in str(failure.value)
    assert "[REDACTED]" in str(failure.value) and "nothing was force-pushed" in str(failure.value)


def runnable_git() -> str | None:
    """A git that starts from this interpreter (a PATH entry may hold a binary for another CPU type)."""
    candidates = (shutil.which("git", path=os.pathsep.join(("/usr/bin", "/bin"))), shutil.which("git"))
    for candidate in dict.fromkeys(candidate for candidate in candidates if candidate):
        try:
            probe = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def test_runner_reports_programs_that_cannot_start(publisher: ModuleType, tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-git")
    result = publisher.run_subprocess([missing, "--version"], env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 127
    assert result.stderr.startswith(f"cannot run {missing}")


def test_credential_helper_supplies_the_token_from_the_environment(publisher: ModuleType, tmp_path: Path) -> None:
    """Real ``git credential fill`` (local only): the helper answers from the environment."""
    git = runnable_git()
    if git is None:
        pytest.skip("no runnable git found")
    env = publisher.git_environment(
        {"PATH": os.pathsep.join((str(Path(git).parent), "/usr/bin", "/bin")), "HOME": str(tmp_path),
         "GITHUB_TOKEN": FAKE_TOKEN},
        "demo-owner",
    )
    argv = [git, "-c", "credential.helper=", "-c", f"credential.helper={publisher.CREDENTIAL_HELPER}",
            "credential", "fill"]
    result = subprocess.run(argv, input="protocol=https\nhost=github.com\n\n", env=env, capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "username=demo-owner" in result.stdout.splitlines()
    assert f"password={FAKE_TOKEN}" in result.stdout.splitlines()
    assert all(FAKE_TOKEN not in part for part in argv)


def test_dry_run_plan_matches_the_in_process_plan(publisher: ModuleType, tmp_path: Path) -> None:
    plan = make_plan(publisher, tmp_path)
    data = publisher.plan_to_dict(plan)
    result = run_script("--json", "--owner", "demo-owner", "--work-dir", str(tmp_path), token=FAKE_TOKEN)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["repositories"] == data["repositories"]
