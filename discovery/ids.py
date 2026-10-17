"""Deterministic string-UUID identifiers for records produced by Kanishk's modules.

IDs are UUIDv5 values derived from the identity of the thing they name, so re-running
discovery or analysis over the same inputs yields the same IDs (idempotent upserts),
and independent modules computing an ID for the same identity agree. Endpoint record
IDs are snapshot-specific; cross-snapshot comparisons use (service_id, method, path).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

SHOCKWAVE_NAMESPACE = uuid.UUID("6d23cf21-5e81-40ae-90bd-1e464873ee68")
_SEPARATOR = "\x1f"


def stable_id(kind: str, *parts: object) -> str:
    """UUIDv5 over ``kind`` and ordered ``parts``; ``None`` parts are encoded distinctly."""
    encoded = [kind, *("\x00" if part is None else str(part) for part in parts)]
    return str(uuid.uuid5(SHOCKWAVE_NAMESPACE, _SEPARATOR.join(encoded)))


def endpoint_id(snapshot_id: str, service_id: str, method: str, path: str) -> str:
    return stable_id("endpoint", snapshot_id, service_id, method.upper(), path)


EndpointIdentity = tuple[str, str, str]  # (service_id, METHOD, normalized path)


def endpoint_identities(endpoints: Iterable[Any]) -> dict[str, EndpointIdentity]:
    """Map Endpoint record ids to their snapshot-independent identity.

    Pass the Endpoint records of every snapshot involved (the change's old contract
    snapshot and the catalog a consumer scan was keyed to), so records of the same
    endpoint from different snapshots compare equal.
    """
    return {
        str(endpoint.id): (str(endpoint.service_id), str(endpoint.method).upper(), str(endpoint.path))
        for endpoint in endpoints
    }


def same_endpoint(
    left_id: object, right_id: object, identities: Mapping[str, EndpointIdentity] | None = None
) -> bool:
    """Whether two Endpoint record ids name the same endpoint.

    Without ``identities`` only the identical record id matches (same snapshot). With
    them, records from different snapshots match when their identities are equal; an id
    missing from the map never matches anything but itself.
    """
    left, right = str(left_id), str(right_id)
    if left == right:
        return True
    if not identities:
        return False
    left_identity, right_identity = identities.get(left), identities.get(right)
    return left_identity is not None and left_identity == right_identity


def evidence_id(*parts: object) -> str:
    return stable_id("evidence", *parts)


def change_id(
    old_snapshot_id: str,
    new_snapshot_id: str,
    service_id: str,
    method: str,
    path: str,
    field_path: str,
    kind: str,
) -> str:
    return stable_id(
        "change", old_snapshot_id, new_snapshot_id, service_id, method.upper(), path, field_path, kind
    )


def risk_id(change_id_: str, enrollment_id: str, repository_snapshot_id: str, category: str) -> str:
    return stable_id("risk", change_id_, enrollment_id, repository_snapshot_id, category)


def verification_plan_id(risk_id_: str, repository_snapshot_id: str, scenario_id: str) -> str:
    return stable_id("verification-plan", risk_id_, repository_snapshot_id, scenario_id)


def verification_result_id(plan_id: str, *parts: object) -> str:
    return stable_id("verification-result", plan_id, *parts)


def fixture_uuid(name: str) -> str:
    """Placeholder demo IDs (until Ishan publishes canonical fixture IDs)."""
    return stable_id("demo-fixture", name)
