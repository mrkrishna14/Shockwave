"""Orchestration for the Shockwave Stripe demo environment (``demo/scripts/demo.py``).

The package only orchestrates existing engines: ``discovery`` (pinned Stripe contract
inventory), ``analyzer`` (``detect_changes``, ``analyze_impact``, ``validate_risk_evidence``)
and ``verification.scenarios`` (plans, the LOCAL RUNNER DOUBLE, interpretation and controls).
It writes no substitute scanner: until Noirit's scanner is integrated, consumer dependencies
come from the labeled FIXTURE ``demo/expected/dependencies.fixture.json``, checked line by line
against the prepared repositories before use.
"""
