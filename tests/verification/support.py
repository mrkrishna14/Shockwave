"""Locally owned temporary fixtures until Noirit/Kanishk supply canonical fixtures."""
import hashlib
from pathlib import Path
import shutil
from integrations.fakes import ContractDouble as D, fake_models
from integrations.storage import ArtifactStore
from verification.runner.profiles import TrustedSnapshot, Scenario
from verification.runner.service import RunnerService
from migration.patches import ApprovedTarget
from migration.service import MigrationService


def setup(tmp_path, language='python'):
    source = Path(__file__).parent / 'fixtures' / language
    root = tmp_path / 'source'
    shutil.copytree(source, root, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    tests = ('test_display.py',) if language == 'python' else ('display.test.cjs',)
    assertion = 'test_display_ten_dollars' if language == 'python' else 'display_ten_dollars'
    scenario = Scenario('scenario', 'old-fixture', 'new-fixture', {'amount': 1000}, {'amount': 10},
        tests, frozenset([assertion]), 'new-contract')
    snapshot = TrustedSnapshot.register_team_fixture(root, 'a' * 40)
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    runner = RunnerService(artifacts, {'snapshot': snapshot}, {'scenario': scenario},
        factory=fake_models, consent_check=lambda _: True)
    plan = D(id='plan', risk_id='risk', repository_snapshot_id='snapshot', scenario_id='scenario',
        runner_profile='trusted_' + language.replace('node', 'node'), test_files=list(tests),
        old_fixture_id='old-fixture', new_fixture_id='new-fixture', expected_behavior='$10.00', timeout_seconds=10)
    file = 'consumer.py' if language == 'python' else 'consumer.cjs'
    expression = 'response["amount"] / 100' if language == 'python' else 'response.amount / 100'
    target = ApprovedTarget('risk', 'snapshot', snapshot.commit_sha, 'demo-payments', file,
        2 if language == 'python' else 1, expression, hashlib.sha256((root / file).read_bytes()).hexdigest(),
        'new-contract', plan)
    migrations = MigrationService(runner, artifacts, {'risk': target}, factory=fake_models,
        consent_check=lambda _: True)
    risk = D(id='risk', repository_snapshot_id='snapshot')
    return D(root=root, runner=runner, plan=plan, artifacts=artifacts, target=target,
        migrations=migrations, risk=risk)


def request(verification):
    return D(risk_id='risk', verification_id=verification.id, repository_snapshot_id='snapshot',
             target_contract_snapshot_id='new-contract', mode='preview')
