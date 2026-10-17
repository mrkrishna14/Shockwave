import importlib.util
from pathlib import Path
import sys
import pytest
from verification.runner.profiles import TrustedSnapshot
from verification.runner.service import passed

spec = importlib.util.spec_from_file_location('fixture_support', Path(__file__).with_name('support.py'))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


@pytest.mark.parametrize('language', ['python', 'node'])
def test_real_regression(tmp_path, language):
    s = support.setup(tmp_path, language)
    result = s.runner.verify_regression(s.plan, s.root)
    assert result.status == 'verified_regression', [vars(result.baseline), vars(result.proposed) if result.proposed else None]
    assert passed(result.baseline)
    assert result.proposed.tests_run == 2
    assert result.proposed.exit_code == 1
    assert s.artifacts.get(result.proposed.artifact_ids[0])


@pytest.mark.parametrize('case', ['baseline', 'zero', 'timeout', 'infra', 'unrelated', 'output'])
def test_inconclusive_cases(tmp_path, case):
    s = support.setup(tmp_path)
    content = {
        'baseline': 'def test_display_ten_dollars():\n    assert False\n',
        'zero': 'x = 1\n',
        'timeout': 'import time\ndef test_display_ten_dollars():\n    time.sleep(5)\n',
        'infra': 'import nonexistent_shockwave_module\n',
        'unrelated': 'import os, json\ndef test_display_ten_dollars():\n    if json.loads(os.environ["SW_RESPONSE"])["amount"] == 10: raise RuntimeError("not an assertion")\n',
        'output': 'print("x" * 100000)\ndef test_display_ten_dollars():\n    assert False\n',
    }[case]
    (s.root / 'test_display.py').write_text(content)
    s.runner.snapshots['snapshot'] = TrustedSnapshot.register_team_fixture(s.root, 'a' * 40)
    if case == 'timeout':
        s.plan.timeout_seconds = 1
    if case == 'output':
        s.runner.max_output = 128
    result = s.runner.verify_regression(s.plan, s.root)
    assert result.status == 'inconclusive'
    if case == 'timeout':
        assert result.baseline.timed_out


@pytest.mark.parametrize('case', ['sandbox', 'unknown_root', 'stale', 'revoked', 'unsafe_test'])
def test_fail_closed(tmp_path, case):
    s = support.setup(tmp_path)
    root = s.root
    if case == 'sandbox': s.plan.runner_profile = 'sandbox_python'
    if case == 'unknown_root': root = tmp_path
    if case == 'stale': (root / 'consumer.py').write_text('changed')
    if case == 'revoked': s.runner.consent_check = lambda _: False
    if case == 'unsafe_test': s.plan.test_files = ['../test.py']
    result = s.runner.verify_regression(s.plan, root)
    assert result.status == 'inconclusive'
    assert result.baseline.infrastructure_error
    assert result.baseline.tests_run == 0


def test_service_secrets_not_inherited(tmp_path, monkeypatch):
    s = support.setup(tmp_path)
    monkeypatch.setenv('SHOCKWAVE_TEST_SECRET', 'not-for-child')
    (s.root / 'test_display.py').write_text('import os\ndef test_display_ten_dollars():\n    assert "SHOCKWAVE_TEST_SECRET" not in os.environ\n')
    s.runner.snapshots['snapshot'] = TrustedSnapshot.register_team_fixture(s.root, 'a' * 40)
    assert s.runner.verify_regression(s.plan, s.root).status == 'compatible_within_tested_scope'
