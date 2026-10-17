"""Verification plans: which executable scenario can prove a predicted Risk, and how.

``build_verification_plan(risk, snapshot)`` is the contract entry point (CONTRACTS.md).
Scenario selection already happened during impact analysis: it records a
``shockwave-scenario://<scenario_id>`` test-evidence pointer on the Risk only when an
executable scenario is bound to the (change, dependency) pair. The plan resolves that
pointer against the catalog, counting only pointers minted for one of the Risk's own
dependencies (``catalog.bound_scenario_ids``): a pointer copied from another Risk or
smuggled in through change evidence proves nothing. A Risk without a bound pointer
(storage-only, or a usage no catalog scenario is bound to) raises ``NoExecutableScenario``,
as does one naming two scenarios (``AmbiguousScenario``) or a control scenario (controls
are never tied to risks), and its verification stays ``not_run``: a prediction is never
upgraded to verified without its own executable proof. Conversion (divide) and comparison
(compare) risks can both carry a pointer, each to a scenario of its own category. Building
a plan executes nothing; status stays predicted/not_run until Sai's runner supplies
execution evidence for ``verification.scenarios.interpret``.

Runner profile: the trusted profile (a plain local subprocess) is chosen only for
repository connections explicitly configured as trusted local demo fixtures. Every
other repository gets the sandbox profile, which Sai's runner must refuse with an
infrastructure error (interpreted as inconclusive) when no sandbox is available. The
harnesses' network guards are best-effort in-process interception, so the runner must
isolate the network for every profile, trusted ones included.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from discovery.contracts import (
    SCHEMA_VERSION,
    RepositorySnapshot,
    Risk,
    VerificationPlan,
    VerificationResult,
)
from discovery.ids import verification_plan_id, verification_result_id
from verification.scenarios.catalog import (
    ScenarioSpec,
    UnknownScenario,
    bound_scenario_ids,
    get_scenario,
    scenario_ids_from_evidence,
)
from verification.scenarios.staging import clip_text

TRUSTED_CONNECTIONS_ENV = "SHOCKWAVE_TRUSTED_REPOSITORY_CONNECTIONS"
NOT_RUN_SCENARIO_ID = "unavailable"
_DEFAULT_NOT_RUN_REASON = "Not run: no executable scenario is available for this risk; it remains a prediction."


class NoExecutableScenario(LookupError):
    """No executable scenario is bound to the Risk, so its verification stays not_run."""


class AmbiguousScenario(NoExecutableScenario, ValueError):
    """The Risk names more than one bound scenario; no single plan can prove it (not_run)."""


@dataclass(frozen=True)
class PlanConfig:
    """Which repository connections are explicitly trusted local demo fixtures."""

    trusted_repository_connection_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        ids = self.trusted_repository_connection_ids
        if isinstance(ids, str):
            raise TypeError("trusted_repository_connection_ids must be a collection of ids, not a string")
        object.__setattr__(
            self, "trusted_repository_connection_ids", frozenset(_canonical_id(item) for item in ids)
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PlanConfig:
        """Read the comma-separated SHOCKWAVE_TRUSTED_REPOSITORY_CONNECTIONS (unset: trust none)."""
        source = os.environ if environ is None else environ
        raw = source.get(TRUSTED_CONNECTIONS_ENV, "")
        return cls(frozenset(part for part in (piece.strip() for piece in raw.split(",")) if part))

    def is_trusted(self, repository_connection_id: object) -> bool:
        return _canonical_id(repository_connection_id) in self.trusted_repository_connection_ids


def build_verification_plan(
    risk: Risk,
    snapshot: RepositorySnapshot,
    *,
    config: PlanConfig | None = None,
) -> VerificationPlan:
    """Plan the executable scenario recorded on ``risk`` for the pinned ``snapshot``.

    Raises NoExecutableScenario when the Risk carries no resolvable scenario pointer bound
    to one of its own dependencies (AmbiguousScenario, also a ValueError, when it names
    more than one), and ValueError when the snapshot is not the one the Risk was derived
    from.
    """
    if str(risk.repository_snapshot_id) != str(snapshot.id):
        raise ValueError(
            f"risk {risk.id} was derived from repository snapshot {risk.repository_snapshot_id}, "
            f"not {snapshot.id}"
        )
    spec = _scenario_for(risk)
    config = PlanConfig.from_env() if config is None else config
    trusted = config.is_trusted(snapshot.repository_connection_id)
    return VerificationPlan(
        id=verification_plan_id(str(risk.id), str(snapshot.id), spec.scenario_id),
        risk_id=str(risk.id),
        repository_snapshot_id=str(snapshot.id),
        scenario_id=spec.scenario_id,
        runner_profile=spec.trusted_profile if trusted else spec.sandbox_profile,
        test_files=[spec.staged_test_file, *spec.additional_test_files],
        old_fixture_id=spec.old_fixture_id,
        new_fixture_id=spec.new_fixture_id,
        expected_behavior=spec.expected_behavior,
        timeout_seconds=spec.timeout_seconds,
    )


def not_run_result(risk: Risk, reason: str) -> VerificationResult:
    """The truthful result for a Risk that cannot be (or has not been) executed.

    No plan exists, so the id is keyed by the Risk and its snapshot; re-recording the
    same Risk as not_run upserts one record whatever the wording of ``reason``.
    """
    return VerificationResult(
        schema_version=SCHEMA_VERSION,
        id=verification_result_id(NOT_RUN_SCENARIO_ID, str(risk.id), str(risk.repository_snapshot_id)),
        risk_id=str(risk.id),
        repository_snapshot_id=str(risk.repository_snapshot_id),
        status="not_run",
        baseline=None,
        proposed=None,
        scenario_id=NOT_RUN_SCENARIO_ID,
        summary=reason if reason.strip() else _DEFAULT_NOT_RUN_REASON,
        artifact_ids=[],
    )


def _scenario_for(risk: Risk) -> ScenarioSpec:
    scenario_ids = bound_scenario_ids(risk.evidence, risk.dependency_ids)
    if not scenario_ids:
        unbound = scenario_ids_from_evidence(list(risk.evidence))
        detail = (
            f"its scenario pointer(s) ({', '.join(clip_text(item, 120) for item in unbound)}) are not "
            "bound to the risk's own dependencies"
            if unbound
            else "comparison-only, storage-only or unbound dependency"
        )
        raise NoExecutableScenario(
            f"no executable scenario is bound to risk {risk.id} ({detail}); its verification status "
            "stays not_run"
        )
    if len(scenario_ids) > 1:
        named = ", ".join(clip_text(item, 120) for item in scenario_ids)
        raise AmbiguousScenario(
            f"risk {risk.id} references {len(scenario_ids)} scenarios ({named}); a conversion or comparison risk "
            "is keyed per bound scenario, so exactly one executable scenario is expected"
        )
    try:
        spec = get_scenario(scenario_ids[0])
    except UnknownScenario:
        raise NoExecutableScenario(
            f"scenario {clip_text(scenario_ids[0], 120)!r} referenced by risk {risk.id} is not in this "
            "catalog; its verification status stays not_run"
        ) from None
    if spec.kind != "risk":
        raise NoExecutableScenario(
            f"scenario {spec.scenario_id} referenced by risk {risk.id} is a control; controls are not tied to "
            "risks, so its verification status stays not_run"
        )
    return spec


def _canonical_id(value: object) -> str:
    text = str(value).strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text  # never equal to a canonical UUID, so it can never grant trust
