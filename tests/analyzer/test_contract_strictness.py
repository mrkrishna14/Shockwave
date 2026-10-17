"""Kanishk's modules against a deliberately strict, literal reading of CONTRACTS.md.

``tests/analyzer/fixtures/strict_contracts/shared/schemas.py`` stands in for Ishan's
``shared.schemas``: ``schema_version`` is required, optional values are required-nullable,
ids are ``uuid.UUID`` objects and nested classes have other names. Each test runs a script in
a fresh interpreter whose ``PYTHONPATH`` puts that module first, so ``discovery.contracts``
binds to it (``SHOCKWAVE_CONTRACTS`` unset), and fails on the first lenience a module relied on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STRICT_CONTRACTS = REPO_ROOT / "tests" / "analyzer" / "fixtures" / "strict_contracts"
VENV_BIN = REPO_ROOT / ".venv" / "bin"

# Shared prelude: binds to the strict module and defines the JSON round-trip check.
PRELUDE = """
import json
import uuid
from pathlib import Path

import discovery.contracts as contracts

assert contracts.CONTRACTS_SOURCE == "shared.schemas", contracts.CONTRACTS_SOURCE
REPO = Path.cwd()


def round_trip(record):
    text = record.model_dump_json()
    again = type(record).model_validate_json(text)
    assert again == record, type(record).__name__
    assert again.model_dump_json() == text, type(record).__name__
    json.loads(text)
    return again


def strict_ids(record):
    assert isinstance(record.id, uuid.UUID), (type(record).__name__, type(record.id))
"""


def run_strict(script: str) -> str:
    """Run ``script`` against the strict contracts; return its stdout (it must exit 0)."""
    env = {key: value for key, value in os.environ.items() if key != "SHOCKWAVE_CONTRACTS"}
    env.update(
        PYTHONPATH=os.pathsep.join([str(REPO_ROOT), str(STRICT_CONTRACTS)]),
        PYTHONDONTWRITEBYTECODE="1",
        PATH=os.pathsep.join([str(VENV_BIN), env.get("PATH", "")]),
    )
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(PRELUDE) + textwrap.dedent(script)],
        check=False,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-8000:]
    assert "STRICT OK" in completed.stdout, completed.stdout[-4000:]
    return completed.stdout


def test_discovery_produces_strict_contract_records() -> None:
    run_strict(
        """
        from analyzer import demo
        from discovery import discover, discover_endpoints
        from discovery.pinned import STRIPE_PIN_PATH, load_pinned_document
        from discovery.records import FetchedDocument

        provider_ids = json.loads(demo.PROVIDER_IDS_PATH.read_text(encoding="utf-8"))

        # Stripe: public contract only, no source scan.
        stripe = provider_ids["stripe"]
        request = contracts.DiscoveryInput(
            service_id=stripe["service_id"],
            snapshot_id=stripe["contract_snapshot_id"],
            openapi_documents=[demo.STRIPE_DOCUMENT_REF],
            source_snapshot_id=None,
            frameworks=[],
        )
        document = load_pinned_document(STRIPE_PIN_PATH)
        result = discover(request, {demo.STRIPE_DOCUMENT_REF: document}, None).result
        assert isinstance(result.snapshot_id, uuid.UUID)
        assert len(result.endpoints) == 644, len(result.endpoints)
        strict_ids(result.endpoints[0])
        round_trip(result)
        assert demo.discover_stripe(provider_ids).summary["endpoint_count"] == 644

        # Demo Payments with the FastAPI scan.
        for contract in ("v1", "v2_proposed"):
            scanned = demo.discover_demo(provider_ids, contract)
            assert isinstance(scanned.snapshot_id, uuid.UUID)
            assert {e.reconciliation for e in scanned.endpoints} >= {"documented_and_found"}
            for endpoint in scanned.endpoints:
                strict_ids(endpoint)
                for item in endpoint.evidence:
                    strict_ids(item)
            round_trip(scanned)

        # Demo Payments without a source scan.
        spec = demo.DEMO_SPECS["v1"]
        ref = spec.relative_to(REPO).as_posix()
        request = contracts.DiscoveryInput(
            service_id=provider_ids["service_id"],
            snapshot_id=provider_ids["contract_snapshots"]["v1"],
            openapi_documents=[ref],
            source_snapshot_id=None,
            frameworks=[],
        )
        fetched = {ref: FetchedDocument(content=spec.read_bytes(), source_uri=ref)}
        unscanned = discover_endpoints(request, fetched, None)
        assert {e.reconciliation for e in unscanned.endpoints} == {"not_compared"}
        assert unscanned.coverage.unsupported_patterns == []
        round_trip(unscanned)
        print("STRICT OK")
        """
    )


def test_analysis_and_planning_produce_strict_contract_records() -> None:
    run_strict(
        """
        from analyzer import demo
        from analyzer.changes import detect_changes
        from analyzer.impact import analyze_impact, risk_category, validate_risk_evidence
        from analyzer.model_adapter import FixtureModelClient, ModelImpactAnalyzer
        from verification.scenarios.plans import (
            NoExecutableScenario,
            build_verification_plan,
            not_run_result,
        )

        provider_ids = json.loads(demo.PROVIDER_IDS_PATH.read_text(encoding="utf-8"))
        v1 = demo.discover_demo(provider_ids, "v1")
        v2 = demo.discover_demo(provider_ids, "v2_proposed")
        declared = [
            contracts.APIChange.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted((demo.DEMO_DIR / "changes").glob("*.json"))
        ]
        assert declared
        project_id = provider_ids["api_project_id"]
        schema_only = detect_changes(v1, v2, [], api_project_id=project_id)
        changes = detect_changes(v1, v2, declared, api_project_id=project_id)
        assert schema_only == []  # a units change is invisible to the schema
        assert len(changes) == 1, changes
        for change in [*schema_only, *changes]:
            strict_ids(change)
            round_trip(change)
        change = next(c for c in changes if str(c.id) == str(declared[0].id))

        dependencies = demo.load_dependencies()
        deps = list(dependencies.values())
        for dependency in deps:
            strict_ids(dependency)
            round_trip(dependency)
        endpoints = demo.analysis_endpoints(v1)

        analysis = analyze_impact(change, deps, endpoints=endpoints)
        assert analysis.schema_version == "shockwave.v1"
        assert analysis.analysis_mode == "rules"
        assert len(analysis.risks) == 4, len(analysis.risks)
        assert validate_risk_evidence(analysis, change, deps, endpoints=endpoints) == []
        categories = sorted(risk_category(change, risk) for risk in analysis.risks)
        assert categories == ["comparison", "conversion", "conversion", "storage"], categories
        round_trip(analysis)

        display = [dependencies[h] for h in ("python_display", "python_fraud_threshold", "python_storage")]
        model = ModelImpactAnalyzer(
            FixtureModelClient(REPO / "analyzer" / "fixtures" / "model_replay_display.json"),
            model_processing_allowed=lambda dependency: True,
        )
        replayed = model.analyze(change, display, endpoints=endpoints)
        assert replayed.analysis_mode == "fixture", replayed.analysis_mode
        assert replayed.risks
        assert validate_risk_evidence(replayed, change, display, endpoints=endpoints) == []
        round_trip(replayed)

        repositories = demo.consumer_repositories(
            json.loads(demo.CONSUMER_IDS_PATH.read_text(encoding="utf-8"))
        )
        config = demo.trusted_fixture_config(repositories)
        planned = not_run = 0
        for risk in analysis.risks:
            snapshot = repositories[str(risk.repository_snapshot_id)].snapshot
            round_trip(snapshot)
            try:
                plan = build_verification_plan(risk, snapshot, config=config)
            except NoExecutableScenario as missing:
                result = not_run_result(risk, f"Not run: {missing}")
                assert result.status == "not_run" and result.schema_version == "shockwave.v1"
                round_trip(result)
                not_run += 1
            else:
                strict_ids(plan)
                round_trip(plan)
                round_trip(not_run_result(risk, "Not run: not executed in this test"))
                planned += 1
        assert (planned, not_run) == (2, 2), (planned, not_run)
        print("STRICT OK")
        """
    )


def test_local_demo_run_and_interpretation_produce_strict_contract_records() -> None:
    run_strict(
        """
        from analyzer import demo
        from verification.scenarios.interpret import interpret_patched_run, interpret_verification

        run = demo.run_demo(execute_local=True, node_executable=str(REPO / ".venv" / "bin" / "node"))
        assert run.evidence_problems == []
        assert len(run.plans) == 2 and len(run.not_run) == 2
        assert run.local is not None and len(run.local) == 2
        for local in run.local:
            assert local.result.status == "verified_regression", local.result.status
            again = interpret_verification(local.plan, local.baseline, local.proposed)
            assert (str(again.id), again.status) == (str(local.result.id), local.result.status)
            assert again.schema_version == "shockwave.v1"
            assert local.patched is not None and local.patched.status == "validated"
            missing = interpret_patched_run(local.plan, None)
            assert missing.status != "validated", missing.status
            for execution in (local.baseline.execution, local.proposed.execution):
                round_trip(execution)
            round_trip(local.patched.execution)
            round_trip(local.result)
            round_trip(again)
        records = [
            run.discovery_v1,
            run.discovery_v2,
            *run.schema_only_changes,
            *run.changes,
            *run.dependencies.values(),
            *(repository.snapshot for repository in run.repositories.values()),
            run.analysis,
            *run.plans,
            *run.not_run,
        ]
        for record in records:
            round_trip(record)
        documents = demo.sample_documents(run)
        assert all(demo.render_sample(document) for document in documents.values())
        report = demo.render_report(run)
        assert "contracts: shared.schemas" in report
        print("STRICT OK")
        """
    )
