"""Provider endpoint discovery: OpenAPI 3 parsing, literal source routes, reconciliation.

Contract entry point: ``discover_endpoints(request, fetched_inputs, source_root)``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["discover", "discover_endpoints"]


def __getattr__(name: str) -> Any:  # PEP 562: keep submodules importable independently
    if name in __all__:
        from discovery import service

        return getattr(service, name)
    raise AttributeError(f"module 'discovery' has no attribute {name!r}")
