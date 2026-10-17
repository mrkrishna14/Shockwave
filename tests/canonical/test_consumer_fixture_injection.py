"""Sai's runner on Noirit's consumer fixtures: the response reaches the consumer's own tests.

Noirit's display consumers read the charge response from the file named by
SHOCKWAVE_RESPONSE_FIXTURE; the runner also passes it inline as SW_RESPONSE. Without the file
the proposed run would silently reuse the consumer's default (old) fixture and pass.
"""
import shutil
from pathlib import Path

from integrations.fakes import ContractDouble as D
from integrations.fakes import fake_models
from integrations.storage import ArtifactStore
from verification.runner.profiles import Scenario, TrustedSnapshot
from verification.runner.service import RunnerService

ROOT = Path(__file__).resolve().parents[2]


def test_noirit_python_consumer_regression_through_sais_runner(tmp_path):
    root = tmp_path / 'python-display'
    shutil.copytree(ROOT / 'examples/consumers/python-display', root,
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    old = {'id': 'ch_demo', 'object': 'charge', 'amount': 1000, 'currency': 'usd'}
    new = {'id': 'ch_demo', 'object': 'charge', 'amount': 10, 'currency': 'usd'}
    scenario = Scenario('noirit-python-display', 'old-fixture', 'new-fixture', old, new,
                        ('tests/test_display.py',), frozenset(['test_display_amount_shows_ten_dollars']), 'new-contract')
    snapshot = TrustedSnapshot.register_team_fixture(root, 'b' * 40)
    runner = RunnerService(ArtifactStore(tmp_path / 'artifacts'), {'snapshot': snapshot}, {scenario.id: scenario},
                           factory=fake_models, consent_check=lambda _: True)
    plan = D(id='plan', risk_id='risk', repository_snapshot_id='snapshot', scenario_id=scenario.id,
             runner_profile='trusted_python', test_files=list(scenario.test_files),
             old_fixture_id='old-fixture', new_fixture_id='new-fixture', expected_behavior='$10.00',
             timeout_seconds=60)
    result = runner.verify_regression(plan, root)
    assert result.status == 'verified_regression', result
    assert result.baseline.exit_code == 0 and result.proposed.exit_code == 1
    assert result.baseline.tests_run == result.proposed.tests_run == 3
