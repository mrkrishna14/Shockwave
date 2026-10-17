"""Bounded single-line rendering of untrusted text, shared by discovery, analyzer and scenarios.

Provider documents, provider and consumer source files, model replies and harness
reports are untrusted. Any of their text that reaches a warning, an evidence excerpt,
a Risk explanation or a summary goes through ``clip_text`` first: C0/C1 controls,
zero-width and directional marks, line/paragraph separators, bidi embeddings,
overrides and isolates, and the BOM become spaces, whitespace runs collapse to one
space, and the result is cut to ``limit`` characters with a visible "..." marker.
"""

from __future__ import annotations

import re

_UNSAFE_TEXT = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]+")
TRUNCATION_MARK = "..."


def clean_text(value: object) -> str:
    """``value`` as one line with unsafe characters removed and whitespace collapsed (unbounded)."""
    return " ".join(_UNSAFE_TEXT.sub(" ", str(value)).split())


def clip_text_flagged(value: object, limit: int) -> tuple[str, bool]:
    """``clean_text(value)`` cut to at most ``limit`` characters; returns (text, truncated)."""
    text = clean_text(value)
    if len(text) <= limit:
        return text, False
    if limit <= len(TRUNCATION_MARK):
        return text[: max(limit, 0)], True
    return text[: limit - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK, True


def clip_text(value: object, limit: int = 200) -> str:
    """``clean_text(value)`` cut to at most ``limit`` characters."""
    return clip_text_flagged(value, limit)[0]


def has_unsafe_characters(value: str) -> bool:
    """True when ``value`` holds a character ``clean_text`` would remove (e.g. in a file name)."""
    return _UNSAFE_TEXT.search(value) is not None
