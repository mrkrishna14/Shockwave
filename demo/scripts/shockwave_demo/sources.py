"""``fetch-sources``: verify the pinned public Stripe sources.

Offline (default): the vendored OpenAPI copy is re-verified against its pin (sha256, size) and
the lock (commit, sha256, size, info.version, openapi version); the lock, the change manifest,
the fixture sets, the templates' SDK pins and API-version constants and this venv's stripe-python
must all agree on one API/SDK combination. Writes ``reports/sources/offline-check.json``.

``--network``: additionally a READ-ONLY shallow fetch of each pinned commit from public GitHub
into ``<cache-dir>/sources/<key>`` (``git init`` + ``git fetch --depth 1 --filter=blob:none
origin <sha>``; files are read with ``git cat-file``, which fetches only the blobs needed), then
checks the commit id, the paths named in the lock, the LICENSE files, and the versions
(stripe-python ``stripe/_version.py`` + ``stripe/_api_version.py``; stripe-node ``package.json`` +
``src/apiVersion.ts``; stripe-mock ``embedded/openapi/spec3.json`` info.version; accept-a-payment
API version strings). Writes ``<cache-dir>/sources/verification.json``. Nothing is pushed and no
credentials are used.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Mapping
from pathlib import Path

from discovery.pinned import STRIPE_PIN_PATH
from shockwave_demo.common import (
    CHANGE_MANIFEST_PATH,
    CONSUMER_MANIFEST_PATH,
    FIXTURE_SETS_DIR,
    LOCK_PATH,
    REPO_ROOT,
    DemoError,
    Layout,
    base_env,
    display_path,
    git_blob,
    git_executable,
    read_json,
    run_process,
    write_json,
)
from shockwave_demo.inventory import lock_problems, pinned_document

_API_CONSTANT = re.compile(r"STRIPE_API_VERSION\s*=\s*['\"]([^'\"]+)['\"]")
# The MIT grant and disclaimer (some MIT LICENSE files carry no "MIT License" title line).
_MIT = ("Permission is hereby granted, free of charge", "WITHOUT WARRANTY OF ANY KIND")
_LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE")


def _check(results: list[dict], name: str, ok: bool, detail: str) -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})


def offline_checks() -> list[dict]:
    lock = read_json(LOCK_PATH)
    pin = read_json(STRIPE_PIN_PATH)
    manifest = read_json(CHANGE_MANIFEST_PATH)
    sources = {item["key"]: item for item in lock["sources"]}
    api_version = lock["api_version"]
    checks: list[dict] = []
    problems = lock_problems(lock, pin, manifest)
    _check(checks, "lock consistency", not problems, "; ".join(problems) or f"one API version: {api_version}")
    try:
        document = pinned_document()
        spec = json.loads(document.content)
        data_ok = (
            hashlib.sha256(document.content).hexdigest() == sources["openapi"]["sha256"]
            and len(document.content) == sources["openapi"]["byte_size"]
        )
        detail = (
            f"{pin['vendored_file']}: sha256 {sources['openapi']['sha256'][:16]}..., {len(document.content)} bytes, "
            f"info.version {spec.get('info', {}).get('version')}, openapi {spec.get('openapi')}"
        )
        _check(
            checks,
            "vendored OpenAPI copy",
            data_ok
            and spec.get("info", {}).get("version") == api_version == sources["openapi"]["info_version"]
            and spec.get("openapi") == sources["openapi"]["openapi"],
            detail,
        )
    except (ValueError, OSError) as error:
        _check(checks, "vendored OpenAPI copy", False, str(error))
    license_path = STRIPE_PIN_PATH.parent / "LICENSE.stripe-openapi"
    license_ok = license_path.is_file() and all(marker in license_path.read_text(encoding="utf-8") for marker in _MIT)
    _check(checks, "vendored OpenAPI license", license_ok, f"{display_path(license_path)} (MIT)")
    for variant in ("baseline", "proposed"):
        fixture = read_json(FIXTURE_SETS_DIR / variant / "payment_intents.json")
        expected_id = manifest["fixtures"][variant]["fixture_set_id"]
        _check(
            checks,
            f"{variant} fixture set",
            fixture.get("api_version") == api_version and fixture.get("fixture_set_id") == expected_id,
            f"{expected_id}, api_version {fixture.get('api_version')}",
        )
    for entry in read_json(CONSUMER_MANIFEST_PATH)["repositories"]:
        template = REPO_ROOT / entry["template"]
        constants = sorted(
            {
                match
                for path in template.rglob("*")
                if path.is_file() and path.suffix in (".py", ".ts") and "node_modules" not in path.parts
                for match in _API_CONSTANT.findall(path.read_text(encoding="utf-8"))
            }
        )
        if (template / "package.json").is_file():
            package = json.loads((template / "package.json").read_text(encoding="utf-8"))
            locked = json.loads((template / "package-lock.json").read_text(encoding="utf-8"))
            declared = package.get("dependencies", {}).get("stripe")
            resolved = locked.get("packages", {}).get("node_modules/stripe", {}).get("version")
            sdk_ok = declared == resolved == sources["stripe-node"]["version"]
            sdk = f"stripe@{declared} (lock {resolved})"
        else:
            requirement = (template / "requirements.txt").read_text(encoding="utf-8")
            sdk_ok = f"stripe=={sources['stripe-python']['version']}" in requirement.split()
            sdk = " ".join(line for line in requirement.splitlines() if line.startswith("stripe"))
        _check(
            checks,
            f"template {entry['name']}",
            sdk_ok and constants == [api_version],
            f"{sdk}; STRIPE_API_VERSION {', '.join(constants) or 'not found'}",
        )
    try:
        installed = importlib.metadata.version("stripe")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    _check(
        checks,
        "venv stripe-python",
        installed == sources["stripe-python"]["version"],
        f"stripe=={installed} (lock {sources['stripe-python']['version']})",
    )
    return checks


# Network --------------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, timeout: int = 600) -> str:
    completed = run_process([git_executable(), *args], cwd, env=base_env({"GIT_TERMINAL_PROMPT": "0"}), timeout=timeout)
    if completed.returncode != 0:
        raise DemoError(f"git {' '.join(args[:2])} failed: {completed.stdout.strip()[-400:]}")
    return completed.stdout


def _read(cwd: Path, commit: str, path: str) -> str:
    return _git(["cat-file", "blob", f"{commit}:{path}"], cwd)


def _license(cwd: Path, commit: str) -> dict:
    names = [item for item in _git(["ls-tree", "--name-only", commit], cwd).splitlines() if item in _LICENSE_NAMES]
    if not names:
        return {"file": None, "mit": False}
    text = _read(cwd, commit, names[0])
    return {"file": names[0], "mit": all(marker in text for marker in _MIT)}


def _exists(cwd: Path, commit: str, path: str) -> bool:
    """Whether the commit's tree has ``path`` (trees only: no blob is fetched)."""
    return bool(_git(["ls-tree", "--name-only", commit, "--", path], cwd).strip())


def _source_paths(source: Mapping) -> list[str]:
    """Repository paths named by the lock (entries such as ``StripeClient.v1...`` name APIs, not files)."""
    paths = [source["path"]] if source.get("path") else []
    for value in source.get("paths", {}).values():
        candidate = str(value).split(" ")[0]
        if "/" in candidate:
            paths.append(candidate)
    return paths


def verify_source(cache: Path, source: Mapping, api_version: str) -> dict:
    key, commit = source["key"], source["commit"]
    directory = cache / key
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / ".git").is_dir():
        _git(["init", "-q", "--template="], directory)
        _git(["remote", "add", "origin", source["repository"]], directory)
    _git(["fetch", "-q", "--depth", "1", "--filter=blob:none", "--no-tags", "origin", commit], directory)
    fetched = _git(["rev-parse", "FETCH_HEAD"], directory).strip()
    checks: list[dict] = []
    _check(checks, "commit", fetched == commit, f"FETCH_HEAD {fetched}")
    for path in _source_paths(source):
        _check(checks, f"path {path}", _exists(directory, commit, path), "present at the pinned commit")
    license_info = _license(directory, commit)
    _check(checks, "license", license_info["mit"] and source.get("license") == "MIT", f"{license_info['file']} (MIT)")
    if key == "openapi":
        data = git_blob(directory, commit, source["path"], timeout=600) or b""
        digest = hashlib.sha256(data).hexdigest()
        version = json.loads(data).get("info", {}).get("version")
        _check(checks, "spec sha256", digest == source["sha256"], digest)
        _check(checks, "spec info.version", version == api_version, str(version))
    elif key == "stripe-python":
        version_text = _read(directory, commit, "stripe/_version.py")
        api_text = _read(directory, commit, "stripe/_api_version.py")
        _check(checks, "stripe/_version.py", f'"{source["version"]}"' in version_text, version_text.strip()[-60:])
        _check(checks, "stripe/_api_version.py", api_version in api_text, api_version)
    elif key == "stripe-node":
        package = json.loads(_read(directory, commit, "package.json"))
        api_text = _read(directory, commit, "src/apiVersion.ts")
        _check(checks, "package.json version", package.get("version") == source["version"], str(package.get("version")))
        _check(checks, "src/apiVersion.ts", api_version in api_text, api_version)
    elif key == "stripe-mock":
        embedded = json.loads(_read(directory, commit, source["paths"]["embedded_spec"]))
        version = embedded.get("info", {}).get("version")
        _check(checks, "embedded spec info.version", version == source["embedded_spec_version"], str(version))
    elif key == "accept-a-payment":
        node = _read(directory, commit, source["paths"]["node_server"])
        python = _read(directory, commit, source["paths"]["python_server"])
        _check(checks, "sample API version (node)", "2023-10-16" in node, "apiVersion 2023-10-16 (older; reference only)")
        _check(checks, "sample API version (python)", "2023-10-16" in python, "api_version 2023-10-16 (older; reference only)")
    return {"key": key, "repository": source["repository"], "commit": commit, "checks": checks}


def run_fetch_sources(layout: Layout, *, network: bool = False) -> list[str]:
    checks = offline_checks()
    lines = ["Fetch/verify pinned public Stripe sources" + (" (network: read-only GitHub fetch)" if network else " (offline)")]
    lines += [f"  {'ok  ' if item['ok'] else 'FAIL'} {item['check']}: {item['detail']}" for item in checks]
    write_json(layout.reports_dir / "sources" / "offline-check.json", {"kind": "shockwave-demo.sources-offline@1", "checks": checks})
    failed = [item["check"] for item in checks if not item["ok"]]
    if network:
        lock = read_json(LOCK_PATH)
        cache = layout.cache_dir / "sources"
        verified = []
        for source in lock["sources"]:
            if not source.get("commit"):
                continue
            result = verify_source(cache, source, lock["api_version"])
            verified.append(result)
            bad = [item["check"] for item in result["checks"] if not item["ok"]]
            failed += [f"{source['key']}: {item}" for item in bad]
            lines.append(
                f"  {'ok  ' if not bad else 'FAIL'} {source['key']} @ {source['commit'][:12]}: "
                + ", ".join(f"{item['check']}" + ("" if item["ok"] else " FAILED") for item in result["checks"])
            )
        write_json(cache / "verification.json", {"kind": "shockwave-demo.sources-verification@1", "sources": verified})
        lines.append(f"  wrote {display_path(cache / 'verification.json', layout)}")
    else:
        lines.append("  (pass --network for a read-only shallow fetch of each pinned commit from public GitHub)")
    if failed:
        raise DemoError("source verification failed: " + "; ".join(failed) + "\n" + "\n".join(lines))
    return lines
