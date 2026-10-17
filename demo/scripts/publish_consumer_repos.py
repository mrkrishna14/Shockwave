"""Publish the prepared Shockwave Stripe demo consumer repositories as PRIVATE GitHub repositories.

DRY RUN BY DEFAULT. Without ``--execute`` this prints the exact plan (owner, repository names,
private visibility, REST calls, git commands and the manual browser steps for the GitHub Apps)
and does nothing else: no network access, no subprocess, no file writes.

Executing requires ALL of ``--owner <login> --execute --i-authorize-github-changes`` and a token
in ``$GITHUB_TOKEN``. For each repository in demo/consumer-repos.manifest.json it then:

1. refuses if the repository already exists (never overwrites, never force-pushes);
2. creates it with ``private: true`` (any other visibility is refused);
3. checks that GitHub reports it as private before pushing anything;
4. pushes the prepared local snapshot (``demo/.work/repos/<name>``, made by
   ``demo/scripts/demo.py prepare``) to ``refs/heads/main`` with git over HTTPS. The token comes
   from a credential helper that reads ``$GITHUB_TOKEN`` at run time, so it never appears on a
   command line, in git config, in a credential store or in this script's output;
5. verifies that GitHub's ``main`` points at the pushed commit.

It never deletes repositories, changes visibility, registers or installs GitHub Apps, or changes
account permissions: those stay manual browser steps, printed with the plan. The repositories are
team-created demo applications, not Stripe customer code; Stripe has not endorsed Shockwave.
API details were checked against GitHub's documentation on 2026-10-17
(demo/github/docs-verified.json).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSUMER_MANIFEST = REPO_ROOT / "demo" / "consumer-repos.manifest.json"
DEFAULT_WORK_DIR = REPO_ROOT / "demo" / ".work" / "repos"
READER_APP_MANIFEST = "demo/github/app-manifest.json"
PR_WRITER_APP_MANIFEST = "demo/github/app-manifest.pr-writer.json"
REGISTER_APP_PAGE = "demo/github/register-app.html"
PREPARE_COMMAND = ".venv/bin/python demo/scripts/demo.py prepare"

API_ROOT = "https://api.github.com"
GIT_ROOT = "https://github.com"
API_VERSION = "2026-03-10"  # docs-verified.json: rest-api-version
USER_AGENT = "shockwave-demo-publisher"
TOKEN_ENV = "GITHUB_TOKEN"
GIT_USERNAME_ENV = "SHOCKWAVE_GIT_USERNAME"
AUTHORIZE_FLAG = "--i-authorize-github-changes"
OWNER_PLACEHOLDER = "<OWNER>"
COMMIT_PLACEHOLDER = "<SNAPSHOT_COMMIT>"
REQUIRED_VISIBILITY = "private"
PUSH_TIMEOUT_SECONDS = 300

# Answers ``get`` with the username and the token read from the environment at run time.
# ``echo`` is a shell builtin, so the token is never an argument of any process.
# docs-verified.json: git-credential-helper-reset.
CREDENTIAL_HELPER = (
    f'!f() {{ test "$1" = get && echo "username=${{{GIT_USERNAME_ENV}}}" && echo "password=${{{TOKEN_ENV}}}"; }}; f'
)

_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_REPO_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Never published: installed dependencies, caches, local databases, env files and keys.
_FORBIDDEN_COMPONENTS = frozenset(
    {"node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store", ".env", ".work", ".cache"}
)
_FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".pyc")

NEVER = (
    "overwrite, rename or delete a repository (an existing repository stops the run before anything is created)",
    "force-push, or push to a repository this run did not just create",
    "create a public or internal repository, or change any repository's visibility",
    "register, install, suspend or uninstall a GitHub App, or change account or app permissions",
    "print, log, store or put on a command line the token",
)


class Refusal(Exception):
    """A precondition or authorization is missing: nothing on GitHub was changed by this step."""


class PublishError(Exception):
    """A GitHub or git operation failed; the message says what already exists."""


@dataclass(frozen=True)
class RepoPlan:
    name: str
    template: str
    language: str
    snapshot_dir: Path
    head_commit: str | None
    snapshot_problem: str | None


@dataclass(frozen=True)
class PublishPlan:
    owner: str | None
    owner_type: str
    visibility: str
    default_branch: str
    repositories: tuple[RepoPlan, ...]
    token_present: bool

    @property
    def owner_display(self) -> str:
        return self.owner or OWNER_PLACEHOLDER


# Plan ---------------------------------------------------------------------------------------


def validate_owner(owner: str) -> str:
    if not _OWNER_RE.fullmatch(owner):
        raise Refusal(f"--owner {owner!r} is not a GitHub account or organization login")
    return owner


def load_consumer_manifest(path: Path = CONSUMER_MANIFEST) -> dict[str, Any]:
    """The three consumer repositories; refuses anything that is not marked private."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("visibility") != REQUIRED_VISIBILITY:
        raise Refusal(f"{_display(path)} must declare visibility 'private', found {data.get('visibility')!r}")
    branch = data.get("default_branch")
    if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise Refusal(f"{_display(path)} has an invalid default_branch {branch!r}")
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise Refusal(f"{_display(path)} lists no repositories")
    names = [entry.get("name") for entry in repositories]
    for name in names:
        if not isinstance(name, str) or not _REPO_NAME_RE.fullmatch(name):
            raise Refusal(f"{_display(path)} has an invalid repository name {name!r}")
    if len(set(names)) != len(names):
        raise Refusal(f"{_display(path)} lists a repository twice")
    return data


def read_head_commit(repo_dir: Path) -> tuple[str | None, str | None]:
    """``(commit, problem)`` for a prepared snapshot, read from ``.git`` without running git."""
    git_dir = repo_dir / ".git"
    if not git_dir.is_dir():
        return None, f"not prepared (run `{PREPARE_COMMAND}`)"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[len("ref: ") :]
            if not ref.startswith("refs/heads/") or ".." in ref:
                return None, f"unexpected HEAD {head!r}"
            ref_file = git_dir / ref
            sha = ref_file.read_text(encoding="utf-8").strip() if ref_file.is_file() else _packed_ref(git_dir, ref)
        else:
            sha = head
    except OSError as exc:
        return None, f"unreadable git metadata ({exc.__class__.__name__})"
    if not sha or not _SHA_RE.fullmatch(sha):
        return None, f"no commit yet (run `{PREPARE_COMMAND}`)"
    return sha, None


def _packed_ref(git_dir: Path, ref: str) -> str | None:
    packed = git_dir / "packed-refs"
    if not packed.is_file():
        return None
    for line in packed.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(("#", "^")):
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    return None


def build_plan(
    manifest: Mapping[str, Any],
    *,
    owner: str | None,
    owner_type: str,
    work_dir: Path,
    token_present: bool,
) -> PublishPlan:
    repositories = []
    for entry in manifest["repositories"]:
        snapshot_dir = work_dir / entry["name"]
        head_commit, problem = read_head_commit(snapshot_dir)
        repositories.append(
            RepoPlan(
                name=entry["name"],
                template=str(entry.get("template", "")),
                language=str(entry.get("language", "")),
                snapshot_dir=snapshot_dir,
                head_commit=head_commit,
                snapshot_problem=problem,
            )
        )
    return PublishPlan(
        owner=owner,
        owner_type=owner_type,
        visibility=REQUIRED_VISIBILITY,
        default_branch=manifest["default_branch"],
        repositories=tuple(repositories),
        token_present=token_present,
    )


def repository_description(repo: RepoPlan) -> str:
    return (
        f"Team-created Shockwave demo consumer ({repo.language}) that calls the Stripe API through the "
        "official SDK. Not a Stripe customer application; not affiliated with or endorsed by Stripe."
    )


def create_repository_request(plan: PublishPlan, repo: RepoPlan) -> tuple[str, str, dict[str, Any]]:
    """``(method, path, body)``; the body always asks for a private repository.

    docs-verified.json: create-repository-user, create-repository-org.
    """
    body: dict[str, Any] = {
        "name": repo.name,
        "description": repository_description(repo),
        "private": True,
        "auto_init": False,
    }
    if plan.owner_type == "org":
        body["visibility"] = REQUIRED_VISIBILITY
        return "POST", f"/orgs/{plan.owner_display}/repos", body
    return "POST", "/user/repos", body


def remote_url(owner: str, name: str) -> str:
    return f"{GIT_ROOT}/{owner}/{name}.git"


def push_argv(plan: PublishPlan, repo: RepoPlan) -> list[str]:
    """git push of the snapshot commit to the new repository's default branch; never forced."""
    commit = repo.head_commit or COMMIT_PLACEHOLDER
    return [
        "git",
        "-C",
        str(repo.snapshot_dir),
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper={CREDENTIAL_HELPER}",
        "-c",
        "core.hooksPath=/dev/null",
        "push",
        "--no-verify",
        remote_url(plan.owner_display, repo.name),
        f"{commit}:refs/heads/{plan.default_branch}",
    ]


def git_environment(base_env: Mapping[str, str], username: str) -> dict[str, str]:
    """Environment for git: no prompts, no system/global config, no inherited GIT_* settings.

    The token stays only in ``GITHUB_TOKEN`` for the credential helper. docs-verified.json:
    git-environment.
    """
    env = {
        key: value
        for key, value in base_env.items()
        if not key.startswith(("GIT_", "GCM_")) and key not in {"SSH_ASKPASS", GIT_USERNAME_ENV}
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GCM_INTERACTIVE": "never",
            GIT_USERNAME_ENV: username,
        }
    )
    return env


def local_git_environment(base_env: Mapping[str, str]) -> dict[str, str]:
    """Environment for local, read-only git commands: no token, no prompts, no user config."""
    env = git_environment(base_env, "")
    env.pop(TOKEN_ENV, None)
    env.pop(GIT_USERNAME_ENV, None)
    return env


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _display_argv(argv: Sequence[str]) -> str:
    shown = list(argv)
    if len(shown) > 2 and shown[1] == "-C":
        shown[2] = _display(Path(shown[2]))
    return shlex.join(shown)


def manual_browser_steps(plan: PublishPlan) -> list[str]:
    owner = plan.owner_display
    names = ", ".join(repo.name for repo in plan.repositories)
    form = (
        "https://github.com/settings/apps/new"
        if plan.owner_type == "user"
        else f"https://github.com/organizations/{owner}/settings/apps/new"
    )
    settings = (
        "Settings > Applications > Installed GitHub Apps > Configure"
        if plan.owner_type == "user"
        else "Your organizations > Settings > Third-party Access > GitHub Apps > Configure"
    )
    return [
        f"A. Register the read-only Reader app from {READER_APP_MANIFEST}",
        (
            "  1. Optional: replace https://shockwave.example.invalid in the manifest with your Shockwave base "
            "URL (SHOCKWAVE_PUBLIC_BASE_URL). With the placeholders GitHub cannot deliver webhooks or "
            "redirects, so revocation events are not observed."
        ),
        (
            f"  2. Open {REGISTER_APP_PAGE} in a browser, choose the account {owner} ({plan.owner_type}), paste "
            f"the manifest and continue: the page POSTs it to {form} with a random state. Register the apps "
            "under the same account that owns the repositories: a private app (public: false) can only be "
            "installed on the account that owns it."
        ),
        (
            "  3. On GitHub, check the name, the permissions (Metadata: read, Contents: read) and the events, "
            'then click "Create GitHub App".'
        ),
        (
            "  4. GitHub redirects to the manifest's redirect_url with ?code=...&state=... (with the placeholder "
            "URL the page does not load, but the address bar shows the code): check that the state matches, "
            "then within one hour exchange the code with POST https://api.github.com/app-manifests/<code>/"
            "conversions. Keep the returned id, client_id, client_secret, pem and webhook_secret in a secret "
            "store outside this repository; never commit or paste them."
        ),
        "B. Install the Reader on ONLY the selected repositories",
        (
            f"  5. Open https://github.com/apps/<reader-app-slug>/installations/new, pick {owner}, choose "
            f'"Only select repositories" and select exactly: {names}. Never choose "All repositories".'
        ),
        f"  6. Check {settings}: Repository access must list only those repositories.",
        "C. PR Writer: only when a consumer explicitly authorizes migration PRs (not needed for the analysis demo)",
        (
            f"  7. Register {PR_WRITER_APP_MANIFEST} the same way (Metadata: read, Contents: write, "
            "Pull requests: write)."
        ),
        (
            '  8. Install it with "Only select repositories" on just the repositories authorized for '
            "migration PRs (none by default)."
        ),
        "D. This script automates none of A-C: it never registers or installs apps or changes permissions.",
    ]


def plan_to_dict(plan: PublishPlan) -> dict[str, Any]:
    owner = plan.owner_display
    preflight: list[dict[str, Any]] = [
        {
            "method": "GET",
            "url": f"{API_ROOT}/user",
            "expect": "200; for a user account the login must equal --owner, otherwise nothing is created",
        }
    ]
    repositories = []
    for repo in plan.repositories:
        method, path, body = create_repository_request(plan, repo)
        repositories.append(
            {
                "name": repo.name,
                "full_name": f"{owner}/{repo.name}",
                "visibility": plan.visibility,
                "template": repo.template,
                "language": repo.language,
                "snapshot": {
                    "path": _display(repo.snapshot_dir),
                    "prepared": repo.head_commit is not None,
                    "commit": repo.head_commit,
                    "problem": repo.snapshot_problem,
                },
                "api_calls": [
                    {
                        "method": "GET",
                        "url": f"{API_ROOT}/repos/{owner}/{repo.name}",
                        "expect": "404 for every repository before any is created; 200 or 301 (exists) refuses the run",
                    },
                    {
                        "method": method,
                        "url": f"{API_ROOT}{path}",
                        "body": body,
                        "expect": "201 and the response reports private: true; otherwise stop before pushing",
                    },
                    {
                        "method": "GET",
                        "url": f"{API_ROOT}/repos/{owner}/{repo.name}/commits/{plan.default_branch}",
                        "expect": "200 and sha equals the snapshot commit",
                    },
                ],
                "git_commands": [_display_argv(push_argv(plan, repo))],
            }
        )
    return {
        "schema": "shockwave-demo.github-publication-plan@1",
        "mode": "dry-run",
        "github_changes_made": False,
        "network_used": False,
        "owner": owner,
        "owner_type": plan.owner_type,
        "visibility": plan.visibility,
        "default_branch": plan.default_branch,
        "api_root": API_ROOT,
        "api_version": API_VERSION,
        "request_headers": {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer <${TOKEN_ENV}; never printed>",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
        "token_env": TOKEN_ENV,
        "token_present": plan.token_present,
        "token_handling": (
            f"read from ${TOKEN_ENV} only when executing; sent only in the Authorization header to "
            f"{API_ROOT} and to git through a credential helper that reads the environment; never printed, "
            "logged, written to git config or a credential store, or placed on a command line"
        ),
        "execute_requires": ["--owner <login>", "--execute", AUTHORIZE_FLAG, f"{TOKEN_ENV} in the environment"],
        "never": list(NEVER),
        "preflight_calls": preflight,
        "repositories": repositories,
        "manual_browser_steps": manual_browser_steps(plan),
    }


def render_plan_text(plan: PublishPlan, *, executing: bool) -> str:
    data = plan_to_dict(plan)
    owner = data["owner"]
    lines = [
        "SHOCKWAVE STRIPE DEMO: PRIVATE GITHUB REPOSITORY PUBLICATION PLAN",
        (
            "Mode: EXECUTE (authorized by the operator)."
            if executing
            else "Mode: DRY RUN (default). Nothing is created, pushed or installed; no network access."
        ),
        (
            "The repositories are team-created demo consumers, not Stripe customer code; Stripe has not "
            "endorsed Shockwave. The Stripe change used later in the demo is SIMULATED."
        ),
        "",
        f"Owner: {owner} ({plan.owner_type} account)" + ("" if plan.owner else "  <- pass --owner <login>"),
        f"Visibility: {plan.visibility} (enforced; public and internal are refused)",
        f"REST API: {API_ROOT}  X-GitHub-Api-Version: {API_VERSION}",
        f"Token: ${TOKEN_ENV} is " + ("set" if plan.token_present else "not set")
        + " (needed only with --execute; the value is never printed or stored)",
        "",
        f"Preflight: GET {API_ROOT}/user -> the token's login must equal the owner (user accounts).",
    ]
    for index, repo in enumerate(data["repositories"], start=1):
        snapshot = repo["snapshot"]
        state = f"commit {snapshot['commit']}" if snapshot["prepared"] else f"NOT READY: {snapshot['problem']}"
        lines += [
            "",
            f"Repository {index}/{len(data['repositories'])}: {repo['full_name']} ({repo['visibility']})",
            f"  template: {repo['template']}   language: {repo['language']}",
            f"  prepared snapshot: {snapshot['path']} ({state})",
        ]
        step = 0
        for call in repo["api_calls"]:
            step += 1
            body = f"  {json.dumps(call['body'], sort_keys=True)}" if "body" in call else ""
            lines.append(f"  {step}. {call['method']} {call['url']}{body}")
            lines.append(f"     expect: {call['expect']}")
            if call["method"] == "POST":
                for command in repo["git_commands"]:
                    step += 1
                    lines.append(f"  {step}. {command}")
                    lines.append(
                        f"     the credential helper reads ${TOKEN_ENV} at run time; no --force; hooks disabled"
                    )
    lines += ["", "This script never:"]
    lines += [f"  - {item}" for item in NEVER]
    if not executing:
        lines += [
            "",
            "To execute (operator only, after authorizing this destination and action):",
            f"  {PREPARE_COMMAND}",
            "  read -rs GITHUB_TOKEN && export GITHUB_TOKEN   # a token able to create private repositories and push",
            f"  .venv/bin/python demo/scripts/publish_consumer_repos.py --owner {owner} "
            + ("--owner-type org " if plan.owner_type == "org" else "")
            + f"--execute {AUTHORIZE_FLAG}",
        ]
    lines += ["", "MANUAL BROWSER STEPS (GitHub Apps; never automated by this script)"]
    lines += data["manual_browser_steps"]
    return "\n".join(lines)


# Execution ----------------------------------------------------------------------------------


class GitHubClient:
    """Minimal GitHub REST client: JSON in and out, no redirects followed, token never echoed."""

    def __init__(self, token: str, *, api_root: str = API_ROOT, timeout: float = 30.0) -> None:
        self._token = token
        self._api_root = api_root
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self._api_root + path, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PublishError(f"{method} {path}: network error ({reason})") from None
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 3xx as HTTPError so the Authorization header never follows a redirect


Runner = Callable[..., subprocess.CompletedProcess]


def run_subprocess(argv: Sequence[str], *, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """Run ``argv``; a program that cannot start or times out becomes a failed result, not a traceback."""
    try:
        return subprocess.run(
            list(argv), env=dict(env), capture_output=True, text=True, timeout=PUSH_TIMEOUT_SECONDS, check=False
        )
    except OSError as exc:  # e.g. not installed, or "Bad CPU type in executable"
        return subprocess.CompletedProcess(list(argv), 127, "", f"cannot run {argv[0]}: {exc.strerror or exc}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"{argv[0]} timed out after {PUSH_TIMEOUT_SECONDS} s")


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


def forbidden_paths(paths: Sequence[str]) -> list[str]:
    bad = []
    for path in paths:
        parts = path.split("/")
        name = parts[-1]
        if (
            any(part in _FORBIDDEN_COMPONENTS for part in parts)
            or name.startswith(".env.")
            or name.endswith(_FORBIDDEN_SUFFIXES)
        ):
            bad.append(path)
    return bad


def check_snapshot(repo: RepoPlan, *, run: Runner, env: Mapping[str, str]) -> None:
    """Local, read-only checks of the commit that would be pushed."""
    if repo.head_commit is None:
        raise Refusal(f"{repo.name}: snapshot {_display(repo.snapshot_dir)} is {repo.snapshot_problem}")
    listing = run(
        ["git", "-C", str(repo.snapshot_dir), "ls-tree", "-r", "-z", "--name-only", repo.head_commit],
        env=local_git_environment(env),
    )
    if listing.returncode != 0:
        detail = (listing.stderr or "").strip()[-300:]
        raise Refusal(
            f"{repo.name}: cannot list commit {repo.head_commit} in {_display(repo.snapshot_dir)} ({detail}); "
            "a working git must be first on PATH"
        )
    paths = [path for path in listing.stdout.split("\0") if path]
    if not paths:
        raise Refusal(f"{repo.name}: commit {repo.head_commit} is empty")
    bad = forbidden_paths(paths)
    if bad:
        raise Refusal(f"{repo.name}: commit {repo.head_commit} contains files that must not be published: {bad[:5]}")


def _message(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"][:300]
    return "no message"


def _reported_private(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("private") is True
        and payload.get("visibility", REQUIRED_VISIBILITY) == REQUIRED_VISIBILITY
    )


def execute_plan(
    plan: PublishPlan,
    *,
    client: Any,
    run: Runner,
    env: Mapping[str, str],
    out: Callable[[str], None],
) -> list[dict[str, str]]:
    """Create each repository privately and push its snapshot; stop at the first problem."""
    if not plan.owner:
        raise Refusal("--owner <login> is required with --execute")
    if plan.visibility != REQUIRED_VISIBILITY:
        raise Refusal(f"refusing visibility {plan.visibility!r}: only private repositories are created")
    for repo in plan.repositories:
        check_snapshot(repo, run=run, env=env)

    status, user = client.request("GET", "/user")
    login = user.get("login") if status == 200 and isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise PublishError(f"GET /user failed (HTTP {status}: {_message(user)}); nothing was created")
    if plan.owner_type == "user" and login.lower() != plan.owner.lower():
        raise Refusal(
            f"the token belongs to {login!r}, not to --owner {plan.owner!r} (use --owner-type org for an "
            "organization); nothing was created"
        )

    for repo in plan.repositories:
        status, payload = client.request("GET", f"/repos/{plan.owner}/{repo.name}")
        if status in (200, 301):
            raise Refusal(
                f"{plan.owner}/{repo.name} already exists; this script never overwrites, reuses or "
                "force-pushes an existing repository. Nothing was created."
            )
        if status != 404:
            raise PublishError(
                f"cannot tell whether {plan.owner}/{repo.name} exists (HTTP {status}: {_message(payload)}); "
                "nothing was created"
            )

    published: list[dict[str, str]] = []
    for repo in plan.repositories:
        done = ", ".join(item["full_name"] for item in published) or "none"
        full_name = f"{plan.owner}/{repo.name}"
        method, path, body = create_repository_request(plan, repo)
        status, payload = client.request(method, path, body)
        if status != 201:
            raise PublishError(
                f"creating {full_name} failed (HTTP {status}: {_message(payload)}). Already published: {done}."
            )
        if not _reported_private(payload):
            raise PublishError(
                f"{full_name} was created but GitHub does not report it as private; nothing was pushed to it. "
                f"Review it in the browser (this script never deletes or changes repositories). "
                f"Already published: {done}."
            )
        out(f"created {full_name} (private)")
        pushed = run(push_argv(plan, repo), env=git_environment(env, login))
        if pushed.returncode != 0:
            detail = redact((pushed.stderr or "").strip()[-500:], env.get(TOKEN_ENV))
            raise PublishError(
                f"git push to {full_name} failed (exit {pushed.returncode}): {detail}. The repository exists "
                f"and may be empty; nothing was force-pushed. Already published: {done}."
            )
        status, commit = client.request("GET", f"/repos/{plan.owner}/{repo.name}/commits/{plan.default_branch}")
        remote_sha = commit.get("sha") if status == 200 and isinstance(commit, dict) else None
        if remote_sha != repo.head_commit:
            raise PublishError(
                f"{full_name}: {plan.default_branch} is {remote_sha!r} after the push, expected "
                f"{repo.head_commit} (HTTP {status}). Already published: {done}."
            )
        out(f"pushed {full_name} {plan.default_branch} = {repo.head_commit}")
        published.append({"full_name": full_name, "commit": repo.head_commit, "visibility": REQUIRED_VISIBILITY})
    return published


# CLI ----------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Publish the three prepared Shockwave Stripe demo consumer repositories as PRIVATE GitHub "
            "repositories. Dry run by default: prints the plan and changes nothing."
        ),
        epilog=(
            f"Executing needs --owner, --execute, {AUTHORIZE_FLAG} and ${TOKEN_ENV}. "
            "Existing repositories are never overwritten; nothing is force-pushed; apps are never installed."
        ),
    )
    parser.add_argument("--owner", help="GitHub user or organization login that will own the repositories")
    parser.add_argument("--owner-type", choices=("user", "org"), default="user", help="default: user")
    parser.add_argument("--visibility", default=REQUIRED_VISIBILITY, help="only 'private' is accepted")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR, help="prepared snapshots directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the dry-run plan as JSON")
    mode.add_argument("--execute", action="store_true", help="perform the plan (requires authorization)")
    parser.add_argument(
        AUTHORIZE_FLAG,
        dest="authorized",
        action="store_true",
        help="confirms the operating user authorized creating and pushing these repositories",
    )
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    token = env.get(TOKEN_ENV, "").strip()
    try:
        if args.visibility != REQUIRED_VISIBILITY:
            raise Refusal(
                f"refusing visibility {args.visibility!r}: this script only creates private repositories"
            )
        owner = validate_owner(args.owner) if args.owner is not None else None
        manifest = load_consumer_manifest()
        plan = build_plan(
            manifest,
            owner=owner,
            owner_type=args.owner_type,
            work_dir=args.work_dir,
            token_present=bool(token),
        )
        if not args.execute:
            if args.json:
                print(json.dumps(plan_to_dict(plan), indent=2))
            else:
                print(render_plan_text(plan, executing=False))
                if args.authorized:
                    print(f"\nNote: {AUTHORIZE_FLAG} has no effect without --execute.")
            return 0
        if not args.authorized:
            raise Refusal(
                f"--execute also requires {AUTHORIZE_FLAG}, given only after the operating user has authorized "
                "this destination and action"
            )
        if owner is None:
            raise Refusal("--execute requires --owner <login>")
        if not token:
            raise Refusal(f"--execute requires a token in ${TOKEN_ENV}; it is read from the environment only")
        not_ready = [f"{repo.name}: {repo.snapshot_problem}" for repo in plan.repositories if repo.head_commit is None]
        if not_ready:
            raise Refusal("snapshots are not prepared: " + "; ".join(not_ready))
        print(render_plan_text(plan, executing=True))
        print("\nEXECUTING")
        published = execute_plan(plan, client=GitHubClient(token), run=run_subprocess, env=env, out=print)
    except Refusal as exc:
        print(f"REFUSED: {redact(str(exc), token)}", file=sys.stderr)
        return 2
    except PublishError as exc:
        print(f"FAILED: {redact(str(exc), token)}", file=sys.stderr)
        return 1
    print(f"\nPublished {len(published)} private repositories:")
    for item in published:
        print(f"  {item['full_name']} @ {item['commit']}")
    print("Next: the manual GitHub App steps above (A-C). Nothing was registered or installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
