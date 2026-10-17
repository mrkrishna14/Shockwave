"""Single import point for the shockwave.v1 contract models used by Kanishk's modules.

Ishan owns the canonical Pydantic models (``shared/schemas.py``). When that module
is importable it is used; otherwise the contract-shaped local doubles are used.
Set ``SHOCKWAVE_CONTRACTS=doubles`` to force the doubles while ``shared/`` is in flux.

Only top-level models are re-exported. Build nested values (coverage, origin,
usage) from plain dicts so code does not depend on nested class names.
"""

from __future__ import annotations

import importlib.util
import os

SCHEMA_VERSION = "shockwave.v1"


def _canonical_schemas_available() -> bool:
    if os.environ.get("SHOCKWAVE_CONTRACTS", "").strip().lower() == "doubles":
        return False
    try:
        return importlib.util.find_spec("shared.schemas") is not None
    except (ImportError, ValueError):
        return False


if _canonical_schemas_available():
    from shared.schemas import (  # type: ignore[import-not-found]
        AnalysisResult,
        APIChange,
        Dependency,
        DiscoveryInput,
        DiscoveryResult,
        Endpoint,
        Evidence,
        ExecutionResult,
        RepositorySnapshot,
        Risk,
        VerificationPlan,
        VerificationResult,
    )

    CONTRACTS_SOURCE = "shared.schemas"
else:
    from discovery._contract_doubles import (
        AnalysisResult,
        APIChange,
        Dependency,
        DiscoveryInput,
        DiscoveryResult,
        Endpoint,
        Evidence,
        ExecutionResult,
        RepositorySnapshot,
        Risk,
        VerificationPlan,
        VerificationResult,
    )

    CONTRACTS_SOURCE = "local-doubles"

__all__ = [
    "CONTRACTS_SOURCE",
    "SCHEMA_VERSION",
    "APIChange",
    "AnalysisResult",
    "Dependency",
    "DiscoveryInput",
    "DiscoveryResult",
    "Endpoint",
    "Evidence",
    "ExecutionResult",
    "RepositorySnapshot",
    "Risk",
    "VerificationPlan",
    "VerificationResult",
]
