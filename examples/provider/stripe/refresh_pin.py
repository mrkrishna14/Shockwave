"""Developer tool: re-pin the vendored Stripe OpenAPI document to an exact commit.

    python examples/provider/stripe/refresh_pin.py --commit <40-hex sha> \
        [--path latest/openapi.spec3.json]

Downloads ``https://raw.githubusercontent.com/stripe/openapi/<sha>/<path>`` over HTTPS
only (host allowlist; redirects to any other host or scheme are refused; 60 s timeout;
64 MiB cap), checks it is an OpenAPI 3 JSON document, writes the gzip beside pin.json
deterministically (mtime=0, so identical bytes give an identical file) and updates
pin.json: commit, path, raw_url, sha256, byte_size and retrieved_at (UTC). The commit
message/date cannot be known without another API, so they are cleared when the commit
changes rather than left stale.

Discovery and tests never run this; they only read the vendored copy through
``discovery.pinned.load_pinned_document``, which re-verifies hash and size.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_HOST = "raw.githubusercontent.com"
REPOSITORY = "stripe/openapi"
DEFAULT_SPEC_PATH = "latest/openapi.spec3.json"
DEFAULT_VENDORED_FILE = "openapi.spec3.json.gz"
TIMEOUT_SECONDS = 60
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
PIN_PATH = Path(__file__).resolve().with_name("pin.json")

_COMMIT = re.compile(r"[0-9a-f]{40}")
_SPEC_PATH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*\.json")
_PLAIN_FILE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


class RefreshError(Exception):
    """A refusal raised before anything is written (validation, download, document check)."""


def validate_commit(commit: str) -> str:
    """A full 40-character commit SHA (case-insensitive input, lowercase output)."""
    normalized = commit.strip().lower()
    if not _COMMIT.fullmatch(normalized):
        raise RefreshError("--commit must be a full 40-character hex commit SHA")
    return normalized


def validate_spec_path(path: str) -> str:
    """A relative POSIX path to a .json file inside the repository, without traversal."""
    if not _SPEC_PATH.fullmatch(path) or ".." in path.split("/"):
        raise RefreshError("--path must be a relative path to a .json file, e.g. latest/openapi.spec3.json")
    return path


def raw_url(commit: str, spec_path: str) -> str:
    return f"https://{ALLOWED_HOST}/{REPOSITORY}/{commit}/{spec_path}"


def check_url(url: str) -> None:
    """Only plain https://raw.githubusercontent.com/... URLs may be fetched or followed."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != ALLOWED_HOST
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
    ):
        raise RefreshError(f"refusing to fetch {url[:200]!r}: only https://{ALLOWED_HOST}/ is allowed")


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> bytes:
    check_url(url)
    opener = urllib.request.build_opener(_AllowlistRedirectHandler)
    request = urllib.request.Request(url, headers={"User-Agent": "shockwave-refresh-pin"})
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        check_url(response.geturl())
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
            raise RefreshError(f"download is larger than {MAX_DOWNLOAD_BYTES} bytes")
        body = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise RefreshError(f"download is larger than {MAX_DOWNLOAD_BYTES} bytes")
    return body


def check_document(body: bytes) -> None:
    """The bytes must be a JSON object declaring OpenAPI 3 (content is never executed)."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RefreshError("downloaded file is not valid UTF-8 JSON") from None
    version = document.get("openapi") if isinstance(document, dict) else None
    if not isinstance(version, str) or not version.startswith("3."):
        raise RefreshError("downloaded file is not an OpenAPI 3 document")


def updated_pin(
    pin: dict[str, Any], *, commit: str, spec_path: str, body: bytes, retrieved_at: str
) -> dict[str, Any]:
    """A new pin dict (the input is not modified) describing ``body`` at ``commit``."""
    new_pin = json.loads(json.dumps(pin))
    source = new_pin.setdefault("source", {})
    if source.get("commit") != commit:
        source["commit_message"] = None
        source["commit_date"] = None
    source["commit"] = commit
    source["path"] = spec_path
    source["raw_url"] = raw_url(commit, spec_path)
    new_pin["retrieved_at"] = retrieved_at
    content = new_pin.setdefault("content", {})
    content["sha256"] = hashlib.sha256(body).hexdigest()
    content["byte_size"] = len(body)
    content["media_type"] = "application/json"
    new_pin.setdefault("vendored_file", DEFAULT_VENDORED_FILE)
    new_pin["vendored_encoding"] = "gzip"
    return new_pin


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-pin the vendored Stripe OpenAPI document.")
    parser.add_argument("--commit", required=True, help="full 40-character stripe/openapi commit SHA")
    parser.add_argument("--path", default=DEFAULT_SPEC_PATH, help=f"spec path (default {DEFAULT_SPEC_PATH})")
    parser.add_argument("--pin", type=Path, default=PIN_PATH, help="pin.json to update (default: beside this tool)")
    args = parser.parse_args(argv)
    try:
        commit = validate_commit(args.commit)
        spec_path = validate_spec_path(args.path)
        pin = json.loads(args.pin.read_text(encoding="utf-8"))
        if not isinstance(pin, dict):
            raise RefreshError(f"{args.pin} does not contain a JSON object")
        vendored_name = pin.get("vendored_file") or DEFAULT_VENDORED_FILE
        if not isinstance(vendored_name, str) or not _PLAIN_FILE_NAME.fullmatch(vendored_name):
            raise RefreshError("pin vendored_file must be a plain file name")
        body = fetch(raw_url(commit, spec_path))
        check_document(body)
        retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_pin = updated_pin(pin, commit=commit, spec_path=spec_path, body=body, retrieved_at=retrieved_at)
        _atomic_write(args.pin.with_name(vendored_name), gzip.compress(body, compresslevel=9, mtime=0))
        _atomic_write(args.pin, (json.dumps(new_pin, indent=2) + "\n").encode("utf-8"))
    except (RefreshError, OSError, ValueError) as exc:
        print(f"refresh_pin: {exc}", file=sys.stderr)
        return 1
    print(f"pinned {REPOSITORY}@{commit[:12]}: {len(body)} bytes, sha256 {new_pin['content']['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
