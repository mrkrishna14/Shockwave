"""Source-file reading shared by the route scanners: unsafe file names, notes, excerpts.

A provider file name is untrusted data. It reaches evidence, warnings and coverage only
when it holds no control, bidi or other invisible character, and every note naming a
file is rendered through ``discovery.text``. Invisible characters are built with ``chr``
so this file stays plain ASCII.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from discovery._source_files import (
    MAX_EXCERPT_CHARS,
    MAX_NOTE_CHARS,
    bounded_notes,
    excerpt_text,
    unique_routes,
    unresolved_summary,
)
from discovery.records import SourceRoute
from discovery.source_express import scan_express_routes
from discovery.source_fastapi import scan_fastapi_routes
from discovery.text import clip_text, has_unsafe_characters

ESC, RLO = chr(0x1B), chr(0x202E)
UNSAFE_STEMS = [
    "a" + chr(0x0A) + "[OK] all endpoints verified safe" + ESC + "[2J",  # a forged line + escape
    "b" + RLO + "txt",  # right-to-left override
    "c" + chr(0x7F) + chr(0x9B) + "31m",  # DEL and a C1 control sequence introducer
    "d" + chr(0x2028) + "e",  # line separator
    "f" + chr(0x200B) + "g",  # zero-width space
]
SCANNERS = {
    "fastapi": (
        scan_fastapi_routes,
        ".py",
        "from fastapi import FastAPI\n\napp = FastAPI()\napp.get('/v1/x')(print)\n",
    ),
    "express": (
        scan_express_routes,
        ".js",
        "const app = require('express')();\napp.get('/v1/x', (req, res) => res.send('x'));\n",
    ),
}
SKIPPED = ": skipped (path holds control or invisible characters)"


def create(path: Path, text: str) -> bool:
    """Write ``text`` at ``path``; False when this file system rejects the name."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return False
    return True


@pytest.mark.parametrize("framework", sorted(SCANNERS))
def test_unsafe_file_names_are_skipped_counted_and_never_echoed(tmp_path: Path, framework: str) -> None:
    scanner, suffix, source = SCANNERS[framework]
    created = sum(create(tmp_path / f"{stem}{suffix}", source) for stem in UNSAFE_STEMS)
    created += create(tmp_path / f"dir{ESC}[31m" / f"inner{suffix}", source)
    if not created:
        pytest.skip("this file system rejects control characters in file names")
    assert create(tmp_path / f"safe{suffix}", source)
    scan = scanner(tmp_path)

    assert [route.file for route in scan.routes] == [f"safe{suffix}"]  # never Evidence.file
    assert (scan.files_scanned, scan.files_skipped) == (1, created)
    assert len([warning for warning in scan.warnings if warning.endswith(SKIPPED)]) == created
    for text in [*scan.warnings, *scan.unsupported_patterns]:
        assert text.isprintable() and not has_unsafe_characters(text), repr(text)
    if (tmp_path / f"{UNSAFE_STEMS[0]}{suffix}").exists():  # the forged line stays inside its note
        assert f"a [OK] all endpoints verified safe [2J{suffix}{SKIPPED}" in scan.warnings


def test_notes_are_single_line_clipped_and_de_duplicated() -> None:
    forged = "evil" + chr(0x0A) + "FORGED" + ESC + "[2J" + RLO + ".py: skipped (x)"

    assert bounded_notes([forged, forged, "b.py: skipped (y)"]) == [
        "b.py: skipped (y)",
        "evil FORGED [2J .py: skipped (x)",
    ]
    (note,) = bounded_notes(["d/" * 400 + "f.py: skipped (z)"])
    assert len(note) == MAX_NOTE_CHARS and note.endswith("...")


def test_excerpts_render_like_discovery_text() -> None:
    text = "app.get(" + chr(0x0A) + "  '/v1/x'," + chr(0x07) + RLO + " handler)"

    assert excerpt_text(text) == clip_text(text, MAX_EXCERPT_CHARS) == "app.get( '/v1/x', handler)"
    assert excerpt_text("app.get('/x',", truncated=True) == "app.get('/x',..."  # a cut window
    assert len(excerpt_text("x" * 1000)) == MAX_EXCERPT_CHARS


def source_route(raw_path: str, line: int = 3) -> SourceRoute:
    return SourceRoute(
        framework="fastapi",
        method="GET",
        path="/v1/x/{id}",
        raw_path=raw_path,
        param_mapping=(),
        file="app.py",
        line_start=line,
        line_end=line,
        handler=None,
        excerpt="",
        confidence="direct",
        include_in_schema=None,
    )


def test_route_dedupe_and_unresolved_summary_shared_by_both_scanners() -> None:
    kept, later = source_route("/v1/x/{id:int}"), source_route("/v1/x/{id}", line=9)

    assert unique_routes([source_route("/v1/x/{id}"), later, kept]) == [kept, later]
    assert unresolved_summary("express", {}) == []
    (summary,) = unresolved_summary("express", {"dynamic path": 2, "all methods": 1})
    assert summary.startswith("express: 3 route registration(s) could not be resolved")
    assert "(all methods: 1, dynamic path: 2); coverage is incomplete" in summary
