import importlib.util
from pathlib import Path
from dataclasses import replace
import pytest
from migration.patches import prepare_patch
from migration.publication import PublicationService
from integrations.github import GitHubError
from integrations.fakes import ContractDouble as D

spec = importlib.util.spec_from_file_location('migration_support', Path(__file__).parents[1] / 'verification/support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


@pytest.mark.parametrize('language', ['python', 'node'])
def test_migrate_validate_preserve_negative_control(tmp_path, language):
    s = support.setup(tmp_path, language)
    verification = s.runner.verify_regression(s.plan, s.root)
    original = (s.root / s.target.file).read_text()
    result = s.migrations.generate_migration(support.request(verification), s.risk, verification, s.root)
    assert result.status == 'patch_validated', result.warnings
    assert result.target_contract_snapshot_id == 'new-contract'
    assert result.validation.tests_run == 2
    receipt = s.migrations.validated[result.id]
    assert 'amount / 100' in receipt.edits[s.target.file]
    assert (s.root / s.target.file).read_text() == original
    assert s.artifacts.get(result.patch_artifact_id).startswith(b'--- a/')
    # Explicitly demonstrate incompatibility with the old contract.
    old = s.runner.execute(s.plan, s.root, False, edits=receipt.edits)
    assert old.result.exit_code == 1


@pytest.mark.parametrize('case', ['path', 'sha', 'service', 'expression'])
def test_bad_patch_rejected(tmp_path, case):
    s = support.setup(tmp_path)
    target = replace(s.target, **{
        'path': {'file': '../escape.py'}, 'sha': {'source_sha256': 'bad'},
        'service': {'service_id': 'stripe'}, 'expression': {'expression': 'amount / 100'},
    }[case])
    with pytest.raises(ValueError): prepare_patch(s.root, target)


def test_failed_validation_blocks_publication(tmp_path):
    s = support.setup(tmp_path)
    verification = s.runner.verify_regression(s.plan, s.root)
    s.runner.consent_check = lambda _: False
    result = s.migrations.generate_migration(support.request(verification), s.risk, verification, s.root)
    assert result.status == 'validation_failed'
    assert result.id not in s.migrations.validated
    publisher = PublicationService(None, s.migrations, lambda *_: True, live_enabled=True)
    with pytest.raises(GitHubError, match='validated_patch_required'):
        publisher.publish_migration(result, D(), D())


@pytest.mark.parametrize('case', ['stale', 'target', 'verification', 'consent'])
def test_migration_context_rejected(tmp_path, case):
    s = support.setup(tmp_path)
    verification = s.runner.verify_regression(s.plan, s.root)
    request = support.request(verification)
    if case == 'stale': (s.root / 'consumer.py').write_text('stale')
    if case == 'target': request.target_contract_snapshot_id = 'wrong'
    if case == 'verification': request.verification_id = 'wrong'
    if case == 'consent': s.migrations.consent_check = lambda _: False
    assert s.migrations.generate_migration(request, s.risk, verification, s.root).status == 'blocked'


def test_model_consent_and_bounded_context(tmp_path):
    s = support.setup(tmp_path)
    verification = s.runner.verify_regression(s.plan, s.root)
    calls = []
    s.migrations.model = lambda **kw: calls.append(kw) or 'response["amount"]'
    result = s.migrations.generate_migration(support.request(verification), s.risk, verification, s.root)
    assert result.status == 'blocked' and not calls
    s.migrations.allow_model = lambda _: True
    result = s.migrations.generate_migration(support.request(verification), s.risk, verification, s.root)
    assert result.status == 'patch_validated' and result.generation_mode == 'model'
    assert len(calls) == 1 and set(calls[0]) == {'expression', 'target', 'attempt'}
