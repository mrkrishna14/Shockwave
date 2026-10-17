"""Endpoint-inventory summary for provider dashboards, computed from a ``DiscoveryOutcome``.

Every number is counted from the records themselves (endpoints, their evidence, document
provenance), never configured or hardcoded, so a Stripe inventory shows whatever the
pinned document actually contains, and its version and content hash say which document
that was. The summary holds provider-side discovery data only; a discovery outcome has no
consumer information to leak. Document references and URLs are shown through
``display_uri``, without userinfo, query or fragment, so a credential-bearing reference
never reaches a dashboard or a written sample.
"""

from __future__ import annotations

import dataclasses
from collections import Counter

from discovery.reconcile import display_uri
from discovery.records import DocumentProvenance
from discovery.service import DiscoveryOutcome

__all__ = ["RECONCILIATION_STATUSES", "summarize_inventory"]

RECONCILIATION_STATUSES = (
    "documented_and_found",
    "implementation_only",
    "documented_not_found_in_scan",
    "conflict",
    "not_compared",
)


def summarize_inventory(outcome: DiscoveryOutcome) -> dict:
    """JSON-serializable counts and provenance for one discovery outcome."""
    result = outcome.result
    endpoints = result.endpoints
    by_reconciliation = dict.fromkeys(RECONCILIATION_STATUSES, 0)
    by_reconciliation.update(Counter(endpoint.reconciliation for endpoint in endpoints))
    return {
        "schema_version": result.schema_version,
        "service_id": result.service_id,
        "snapshot_id": str(result.snapshot_id),
        "endpoint_count": len(endpoints),
        "documented_operation_count": sum(
            1 for endpoint in endpoints if any(item.kind == "openapi" for item in endpoint.evidence)
        ),
        "source_route_count": len(outcome.source_routes),
        "by_method": dict(sorted(Counter(endpoint.method for endpoint in endpoints).items())),
        "by_reconciliation": by_reconciliation,
        "documents": [_document(provenance) for provenance in outcome.documents],
        # Dump only the coverage field: the endpoint schemas can be tens of megabytes.
        "coverage": result.model_dump(mode="json", include={"coverage"})["coverage"],
        "warning_count": len(result.warnings),
    }


def _document(provenance: DocumentProvenance) -> dict:
    summary = dataclasses.asdict(provenance)
    summary["document_ref"] = display_uri(provenance.document_ref)
    summary["source_uri"] = display_uri(provenance.source_uri)
    summary["server_urls"] = [display_uri(url) for url in provenance.server_urls]
    return summary
