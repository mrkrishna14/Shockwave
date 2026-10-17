"""Verification scenarios: plans, fixtures, native harnesses and evidence interpretation.

Contract entry point: ``build_verification_plan(risk, snapshot)``. Execution belongs to
Sai's runner (``verification/runner``); this package describes and interprets. Control
scenarios (unaffected paths, never tied to a Risk) are staged with ``stage_control`` and
interpreted with ``interpret_control``; ``run_control_locally`` runs one on the local
runner double.
"""

from __future__ import annotations

from typing import Any

_EXPORTS = {
    "AmbiguousScenario": "verification.scenarios.plans",
    "build_verification_plan": "verification.scenarios.plans",
    "NoExecutableScenario": "verification.scenarios.plans",
    "not_run_result": "verification.scenarios.plans",
    "stage_scenario": "verification.scenarios.staging",
    "interpret_verification": "verification.scenarios.interpret",
    "interpret_patched_run": "verification.scenarios.interpret",
    "ControlResult": "verification.scenarios.controls",
    "CONTROL_SCENARIO_IDS": "verification.scenarios.controls",
    "interpret_control": "verification.scenarios.controls",
    "run_control_locally": "verification.scenarios.controls",
    "stage_control": "verification.scenarios.staging",
    "control_argv_hint": "verification.scenarios.staging",
}

__all__ = [
    "CONTROL_SCENARIO_IDS",
    "AmbiguousScenario",
    "ControlResult",
    "NoExecutableScenario",
    "build_verification_plan",
    "control_argv_hint",
    "interpret_control",
    "interpret_patched_run",
    "interpret_verification",
    "not_run_result",
    "run_control_locally",
    "stage_control",
    "stage_scenario",
]


def __getattr__(name: str) -> Any:  # PEP 562: keep submodules importable independently
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'verification.scenarios' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)
