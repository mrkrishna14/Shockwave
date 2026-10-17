"""Bounded, symlink-safe reading of provider source files for the static route scanners.

The source root is a trusted directory resolved by the backend, but everything inside
it is untrusted data. Both route scanners (``source_fastapi`` and ``source_express``)
read files only through this module, so the same rules apply to every framework:

* Symlinks are never followed. A link inside an authorized checkout can point anywhere
  on the host, so symlinked files are counted as skipped and symlinked directories are
  not entered.
* Only regular files are opened (a FIFO or device could block or stream forever), and
  they are opened with ``O_NOFOLLOW`` so a file swapped for a link after listing is
  still not followed.
* Every read is bounded: bytes per file, number of files, total bytes and directory
  entries visited.
* Files are decoded as UTF-8 and returned as text. Nothing is imported or executed.
* A file whose path holds a control, bidi or other invisible character is skipped: its
  name could not appear in evidence, warnings or coverage without being altered.

Coverage stays honest: every relevant file is either scanned or counted as skipped,
and truncation produces a "scan truncated" warning instead of a silent cut-off. The
helpers below also shape the scanners' output: per-file notes and source excerpts are
untrusted text and are rendered through ``discovery.text``.
"""

from __future__ import annotations

import itertools
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from discovery.records import DiscoveryError, SourceRoute
from discovery.text import TRUNCATION_MARK, clip_text, has_unsafe_characters

SKIPPED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "site-packages",
        ".shockwave",
    }
)

MAX_EXCERPT_CHARS = 300
# Source read per excerpt (characters, or UTF-8 bytes for Python offsets). Spans of nested
# registrations overlap, so reading each span whole would make excerpts quadratic.
EXCERPT_WINDOW = 1024
MAX_FILE_NOTES = 50  # per-file warnings listed before the remainder is summarized
MAX_NOTE_CHARS = 300  # a note names a provider file, so it is clipped like any untrusted text

# Directory entries visited before the walk stops; generous so the file bound applies first.
_MIN_ENTRY_BUDGET = 100_000
_ENTRIES_PER_FILE = 20
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 1 << 20
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


@dataclass(frozen=True)
class SourceFile:
    path: str  # source-root-relative POSIX path
    text: str  # strict UTF-8 decode, leading BOM removed


@dataclass
class SourceFiles:
    """Readable relevant files (sorted by path) plus honest skip accounting."""

    files: list[SourceFile] = field(default_factory=list)
    files_skipped: int = 0
    warnings: list[str] = field(default_factory=list)  # walk-level, e.g. "scan truncated ..."
    file_notes: list[str] = field(default_factory=list)  # "<file>: skipped (<reason>)"


@dataclass
class _Listing:
    # (relative path, skip reason or None when the file may be read)
    candidates: list[tuple[str, str | None]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: bool = False


class _SkipFile(Exception):
    """A relevant file that is counted as skipped, with a human-readable reason."""


def suffix_filter(
    extensions: Iterable[str], excluded_suffixes: Iterable[str] = ()
) -> Callable[[str], bool]:
    """Predicate over a file name: has one of ``extensions`` and none of ``excluded_suffixes``."""
    wanted = tuple(extensions)
    excluded = tuple(excluded_suffixes)
    return lambda name: name.endswith(wanted) and not (excluded and name.endswith(excluded))


def read_source_files(
    source_root: Path,
    *,
    is_relevant: Callable[[str], bool],
    max_files: int,
    max_file_bytes: int,
) -> SourceFiles:
    """List, bound and decode the relevant files under ``source_root`` without following links."""
    root = checked_source_root(source_root)
    _check_bound("max_files", max_files)
    _check_bound("max_file_bytes", max_file_bytes)

    entry_budget = max(_MIN_ENTRY_BUDGET, max_files * _ENTRIES_PER_FILE)
    listing = _list_candidates(root, is_relevant, entry_budget)
    result = SourceFiles(file_notes=list(listing.notes))
    if listing.truncated:
        result.warnings.append(
            f"scan truncated: directory walk stopped after {entry_budget} entries; "
            "files beyond that point were not counted"
        )

    candidates = sorted(listing.candidates, key=lambda candidate: candidate[0])
    if len(candidates) > max_files:
        beyond = len(candidates) - max_files
        candidates = candidates[:max_files]
        result.files_skipped += beyond
        result.warnings.append(
            f"scan truncated: {beyond} file(s) beyond max_files={max_files} were not scanned"
        )

    total_bytes = 0
    for index, (relative, listing_skip) in enumerate(candidates):
        try:
            if listing_skip is not None:
                raise _SkipFile(listing_skip)
            if has_unsafe_characters(relative):
                raise _SkipFile("path holds control or invisible characters")
            if not _representable(relative):
                raise _SkipFile("path is not a portable POSIX path")
            data = _read_regular_file(root.joinpath(*relative.split("/")), max_file_bytes)
            total_bytes += len(data)
            if total_bytes > _MAX_TOTAL_BYTES:
                remaining = len(candidates) - index
                result.files_skipped += remaining
                result.warnings.append(
                    f"scan truncated: total size limit of {_MAX_TOTAL_BYTES} bytes reached; "
                    f"{remaining} file(s) were not scanned"
                )
                break
            result.files.append(SourceFile(path=relative, text=_decode(data)))
        except _SkipFile as skip:
            result.files_skipped += 1
            result.file_notes.append(f"{relative}: skipped ({skip})")
    return result


def checked_source_root(source_root: Path) -> Path:
    root = Path(source_root)
    if not root.is_dir():
        # Never echo the internal checkout path back to callers.
        raise DiscoveryError("source_root is not a directory")
    return root


def bounded_notes(notes: Iterable[str], limit: int = MAX_FILE_NOTES) -> list[str]:
    """Sorted, de-duplicated per-file notes; anything past ``limit`` is summarized, not dropped.

    Notes name provider files, so each one is rendered with ``discovery.text.clip_text``:
    a file name cannot add a line, a terminal escape or a bidi override to the warnings.
    """
    ordered = sorted({clip_text(note, MAX_NOTE_CHARS) for note in notes})
    if len(ordered) <= limit:
        return ordered
    return [*ordered[:limit], f"{len(ordered) - limit} more file warning(s) not listed"]


def excerpt_text(text: str, limit: int = MAX_EXCERPT_CHARS, *, truncated: bool = False) -> str:
    """Source excerpt as ``discovery.text.clip_text`` renders it: one line, at most ``limit``
    characters. ``truncated`` says ``text`` is only the start of a longer span (a window)."""
    return clip_text(text + TRUNCATION_MARK if truncated else text, limit)


def format_location(kind: str, file: str, line: int) -> str:
    """Unsupported-pattern entry, e.g. ``fastapi:dynamic-prefix pkg/refunds.py:7``."""
    return f"{kind} {file}:{line}"


def unique_routes(routes: Iterable[SourceRoute]) -> list[SourceRoute]:
    """One route per (path, method, file, line), keeping the smallest raw path; sorted."""
    unique: dict[tuple[str, str, str, int], SourceRoute] = {}
    for route in routes:
        key = (route.path, route.method, route.file, route.line_start)
        current = unique.get(key)
        if current is None or route.raw_path < current.raw_path:
            unique[key] = route
    return sorted(
        unique.values(),
        key=lambda route: (route.path, route.method, route.file, route.line_start, route.raw_path),
    )


def unresolved_summary(framework: str, reasons: Mapping[str, int]) -> list[str]:
    """The warning counting registrations a scanner found but did not emit, by reason."""
    if not reasons:
        return []
    total = sum(reasons.values())
    details = ", ".join(f"{reason}: {count}" for reason, count in sorted(reasons.items()))
    message = (
        f"{framework}: {total} route registration(s) could not be resolved to a concrete path "
        f"and were not emitted ({details}); coverage is incomplete, see unsupported_patterns"
    )
    return [message]


def _check_bound(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DiscoveryError(f"{name} must be a positive integer")


def _list_candidates(
    root: Path, is_relevant: Callable[[str], bool], entry_budget: int
) -> _Listing:
    """Iterative depth-first walk in sorted order; never enters symlinked or skipped dirs."""
    listing = _Listing()
    stack: list[tuple[str, str]] = [("", os.fspath(root))]
    visited = 0
    while stack:
        relative_dir, absolute_dir = stack.pop()
        remaining = entry_budget - visited
        try:
            with os.scandir(absolute_dir) as iterator:
                entries = list(itertools.islice(iterator, remaining + 1))
        except OSError:
            listing.notes.append(f"{relative_dir or '.'}: skipped (directory could not be listed)")
            continue
        if len(entries) > remaining:
            # scandir order is arbitrary, so nothing from a partially listed directory is used.
            listing.truncated = True
            return listing
        visited += len(entries)
        subdirectories: list[tuple[str, str]] = []
        for entry in sorted(entries, key=lambda entry: entry.name):
            relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
            try:
                if entry.is_symlink():
                    if is_relevant(entry.name):
                        listing.candidates.append((relative, "symbolic link not followed"))
                    elif entry.is_dir():  # stat of the link target only; never entered
                        listing.notes.append(f"{relative}: skipped (symlinked directory not followed)")
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIPPED_DIRECTORY_NAMES:
                        subdirectories.append((relative, entry.path))
                elif entry.is_file(follow_symlinks=False) and is_relevant(entry.name):
                    listing.candidates.append((relative, None))
            except OSError:
                if is_relevant(entry.name):
                    listing.candidates.append((relative, "could not be inspected"))
        stack.extend(reversed(subdirectories))
    return listing


def _representable(relative: str) -> bool:
    """Paths that Evidence.file can carry: no backslashes or drive-like prefixes."""
    return "\\" not in relative and not (len(relative) > 1 and relative[1] == ":")


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    try:
        descriptor = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        raise _SkipFile("could not be opened") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _SkipFile("not a regular file")
        if info.st_size > max_bytes:
            raise _SkipFile(f"larger than {max_bytes} bytes")
        data = _read_at_most(descriptor, max_bytes + 1)
    except OSError as exc:
        raise _SkipFile("could not be read") from exc
    finally:
        os.close(descriptor)
    if len(data) > max_bytes:  # grew after fstat
        raise _SkipFile(f"larger than {max_bytes} bytes")
    return data


def _read_at_most(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _SkipFile("not valid UTF-8") from exc
    return text.removeprefix("\ufeff")  # a UTF-8 BOM is not source text
