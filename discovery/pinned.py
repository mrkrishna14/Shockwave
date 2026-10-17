"""Load a pinned, vendored OpenAPI document (for example Stripe's public spec).

A pin (``pin.json``) records where the bytes came from -- repository, commit and the
commit-addressed raw URL -- and their sha256 and size. Loading re-verifies all of it,
so a tampered, truncated or swapped vendored file fails loudly instead of silently
changing the discovered inventory, and the revision label is derived from the pin
rather than hardcoded. Decompression is capped at the pinned size, so a gzip bomb
cannot exhaust memory. Nothing here touches the network: the dev-only
``examples/provider/stripe/refresh_pin.py`` is the only tool that re-fetches.
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from discovery.records import DiscoveryError, FetchedDocument

__all__ = ["STRIPE_PIN_PATH", "PinnedDocumentError", "load_pinned_document"]

STRIPE_PIN_PATH: Path = (
    Path(__file__).resolve().parents[1] / "examples" / "provider" / "stripe" / "pin.json"
)

MAX_PIN_FILE_BYTES = 64 * 1024
MAX_PINNED_DOCUMENT_BYTES = 64 * 1024 * 1024
_SIZE_PROBE = 1  # decompress at most this many bytes past the pinned size to detect excess
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{7,64}")
_VISIBILITIES = ("public", "provider_owned")


class PinnedDocumentError(DiscoveryError):
    """The pin or its vendored document is missing, malformed, or does not match."""


def load_pinned_document(pin_path: Path) -> FetchedDocument:
    """Read ``pin_path`` and return the verified vendored document stored beside it."""
    pin_path = Path(pin_path)
    pin = _read_pin(pin_path)
    source = _section(pin, "source")
    content = _section(pin, "content")

    repository = _string(source, "repository", "source")
    commit = _string(source, "commit", "source")
    if not _COMMIT.fullmatch(commit):
        raise PinnedDocumentError("source.commit must be a lowercase hex commit id")
    raw_url = _string(source, "raw_url", "source")
    if urlsplit(raw_url).scheme != "https" or commit not in raw_url:
        raise PinnedDocumentError("source.raw_url must be an https URL addressed by source.commit")

    expected_sha256 = _string(content, "sha256", "content")
    if not _SHA256.fullmatch(expected_sha256):
        raise PinnedDocumentError("content.sha256 must be 64 lowercase hex characters")
    expected_size = content.get("byte_size")
    if type(expected_size) is not int or not 0 <= expected_size <= MAX_PINNED_DOCUMENT_BYTES:
        raise PinnedDocumentError(
            f"content.byte_size must be an integer between 0 and {MAX_PINNED_DOCUMENT_BYTES}"
        )
    media_type = content.get("media_type")
    if media_type is not None and not isinstance(media_type, str):
        raise PinnedDocumentError("content.media_type must be a string")
    visibility = pin.get("visibility")
    if visibility not in _VISIBILITIES:
        raise PinnedDocumentError(f"visibility must be one of {', '.join(_VISIBILITIES)}")

    vendored = _vendored_path(pin_path, pin.get("vendored_file"))
    data = _read_vendored(vendored, expected_size, pin.get("vendored_encoding"))
    if len(data) != expected_size:
        raise PinnedDocumentError(
            f"vendored document is {len(data)} bytes; the pin expects {expected_size}"
        )
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise PinnedDocumentError("vendored document sha256 does not match the pin")
    return FetchedDocument(
        content=data,
        source_uri=raw_url,
        media_type=media_type,
        revision_label=f"{_repository_name(repository)}@{commit[:12]}",
        visibility=visibility,
    )


def _read_pin(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_PIN_FILE_BYTES + 1)
    except OSError as exc:
        raise PinnedDocumentError(f"cannot read pin file {path.name!r}: {_os_reason(exc)}") from None
    if len(raw) > MAX_PIN_FILE_BYTES:
        raise PinnedDocumentError(f"pin file {path.name!r} is larger than {MAX_PIN_FILE_BYTES} bytes")
    try:
        pin = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PinnedDocumentError(f"pin file {path.name!r} is not valid JSON") from None
    if not isinstance(pin, dict):
        raise PinnedDocumentError(f"pin file {path.name!r} must contain a JSON object")
    return pin


def _section(pin: dict[str, Any], name: str) -> dict[str, Any]:
    value = pin.get(name)
    if not isinstance(value, dict):
        raise PinnedDocumentError(f"pin is missing the {name!r} object")
    return value


def _string(section: dict[str, Any], key: str, section_name: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise PinnedDocumentError(f"pin is missing {section_name}.{key}")
    return value


def _vendored_path(pin_path: Path, name: Any) -> Path:
    """The vendored file must sit next to the pin: a plain file name, no traversal."""
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or any(character in name for character in "/\\\x00")
    ):
        raise PinnedDocumentError("vendored_file must be a plain file name next to the pin")
    return pin_path.parent / name


def _read_vendored(path: Path, expected_size: int, encoding: Any) -> bytes:
    if encoding in (None, "identity"):
        return _read_capped(path, expected_size)
    if encoding == "gzip":
        # Deflate never grows data by more than a few bytes per block plus a header.
        compressed = _read_capped(path, expected_size + expected_size // 1000 + 1024)
        return _gunzip_capped(compressed, expected_size)
    raise PinnedDocumentError("vendored_encoding must be 'gzip' or 'identity'")


def _read_capped(path: Path, cap: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(cap + 1)
    except OSError as exc:
        raise PinnedDocumentError(f"cannot read vendored file {path.name!r}: {_os_reason(exc)}") from None
    if len(data) > cap:
        raise PinnedDocumentError(f"vendored file {path.name!r} is larger than the pin allows")
    return data


def _gunzip_capped(data: bytes, expected_size: int) -> bytes:
    """Decompress one gzip member, never producing more than the pinned size + probe."""
    limit = expected_size + _SIZE_PROBE
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    chunks: list[bytes] = []
    produced = 0
    pending = data
    try:
        while not decompressor.eof:
            chunk = decompressor.decompress(pending, limit - produced)
            produced += len(chunk)
            chunks.append(chunk)
            if produced >= limit:
                raise PinnedDocumentError("vendored gzip expands beyond the pinned byte_size")
            pending = decompressor.unconsumed_tail
            if not pending and not chunk:
                break  # input exhausted without reaching the end of the stream
    except zlib.error:
        raise PinnedDocumentError("vendored gzip data is corrupt") from None
    if not decompressor.eof:
        raise PinnedDocumentError("vendored gzip data is truncated")
    if decompressor.unused_data:
        raise PinnedDocumentError("vendored gzip has unexpected trailing data")
    return b"".join(chunks)


def _repository_name(repository: str) -> str:
    """``https://github.com/stripe/openapi`` -> ``stripe/openapi``."""
    path = urlsplit(repository).path if "://" in repository else repository
    name = path.strip("/").removesuffix(".git")
    if not name:
        raise PinnedDocumentError("source.repository does not name a repository")
    return name


def _os_reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__
