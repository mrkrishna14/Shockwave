"""Change detection and consumer impact analysis.

Contract entry points: ``detect_changes(old, new, declared_changes, *, api_project_id=None)``
and ``analyze_impact(change, dependencies)``. ``api_project_id`` is an additive keyword:
DiscoveryResult carries no project id, so a structural change with no declared change to
take it from raises ValueError unless the caller passes it (see analyzer/HANDOFF.md).
"""

from __future__ import annotations

from typing import Any

_EXPORTS = {
    "detect_changes": "analyzer.changes",
    "analyze_impact": "analyzer.impact",
}

__all__ = ["analyze_impact", "detect_changes"]


def __getattr__(name: str) -> Any:  # PEP 562: keep submodules importable independently
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'analyzer' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)
