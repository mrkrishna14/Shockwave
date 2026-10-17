"""Shared helpers for scanner tests. Run from repo root: python -m pytest tests/scanner -q"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanner.contracts import Endpoint, RepositorySnapshot, ScanRequest  # noqa: E402

CONSUMERS = REPO_ROOT / "examples" / "consumers"
COMMIT = "3f9c2a1b7d4e5f60718293a4b5c6d7e8f9012345"

# Test-double catalog. Real endpoint/snapshot IDs are generated once by Ishan.
DEMO_CHARGE = Endpoint(
    id="ep-demo-payments-get-v1-charges-id", service_id="demo-payments", method="GET",
    path="/v1/charges/{id}", snapshot_id="snap-demo-payments-old",
)
STRIPE_PI_CREATE = Endpoint(
    id="ep-stripe-post-v1-payment-intents", service_id="stripe", method="POST",
    path="/v1/payment_intents", snapshot_id="snap-stripe-openapi",
)
STRIPE_PI_RETRIEVE = Endpoint(
    id="ep-stripe-get-v1-payment-intents-intent", service_id="stripe", method="GET",
    path="/v1/payment_intents/{intent}", snapshot_id="snap-stripe-openapi",
)
# Deliberately wrong decoy: a merchant route mislabeled as stripe. The scanner
# must never map SDK calls to it.
DECOY_MERCHANT = Endpoint(
    id="ep-decoy-merchant-create-payment-intent", service_id="stripe", method="GET",
    path="/create-payment-intent", snapshot_id="snap-stripe-openapi",
)
CATALOG = [DEMO_CHARGE, STRIPE_PI_CREATE, STRIPE_PI_RETRIEVE, DECOY_MERCHANT]


def make_snapshot(snapshot_id: str = "snap-consumer-1", commit: str = COMMIT) -> RepositorySnapshot:
    return RepositorySnapshot(
        id=snapshot_id, repository_connection_id="rc-consumer-1", commit_sha=commit,
        fetched_at=datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc), content_hash="sha256:test",
    )


def make_request(service_id: str, snapshot: RepositorySnapshot, languages=None) -> ScanRequest:
    return ScanRequest(
        repository_snapshot_id=snapshot.id, enrollment_id="enr-1", service_id=service_id,
        endpoint_catalog=CATALOG, supported_languages=languages or ["python", "javascript", "typescript"],
        consumer_org_id="org-consumer-1",
    )


@pytest.fixture
def scan():
    from scanner import scan_repository

    def _scan(root: Path, service_id: str = "demo-payments", snapshot=None, languages=None):
        snap = snapshot or make_snapshot()
        return scan_repository(make_request(service_id, snap, languages), snap, Path(root))

    return _scan


def write(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    return root


def line_of(path: Path, needle: str) -> int:
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in {path}")
