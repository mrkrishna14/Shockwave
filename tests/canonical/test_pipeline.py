"""Cross-owner integration with Ishan's actual models and native subprocess execution."""
from dataclasses import replace
from pathlib import Path
import hashlib
import shutil
from uuid import uuid4

import pytest
from shared import schemas as S
from discovery.contracts import CONTRACTS_SOURCE
from integrations.storage import ArtifactStore
from migration.stripe_demo import prepare_demo, identity, ROOT
from migration.patches import prepare_patch
from verification.runner.profiles import Scenario, TrustedSnapshot
from verification.runner.service import RunnerService, passed
from verification.scenarios.catalog import get_scenario


def migration_request(context, verification):
    return S.MigrationRequest(risk_id=context.risk.id, verification_id=verification.id,
        repository_snapshot_id=context.snapshot.id,
        target_contract_snapshot_id=context.target.target_contract_snapshot_id, mode='preview')


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'checkout'
    shutil.copytree(ROOT / 'demo/stripe-checkout', root)
    return prepare_demo(tmp_path / 'results', root=root)


def test_stripe_sdk_end_to_end_and_negative_control(checkout):
    c = checkout
    assert CONTRACTS_SOURCE == 'shared.schemas'
    original = (c.root / c.target.file).read_bytes()
    result = c.runner.verify_regression(c.plan, c.root)
    assert isinstance(result, S.VerificationResult)
    assert result.status == 'verified_regression', result.model_dump()
    assert result.baseline.tests_run == result.proposed.tests_run == 2
    assert result.baseline.exit_code == 0 and result.proposed.exit_code == 1
    migration = c.migrations.generate_migration(migration_request(c, result), c.risk, result, c.root)
    assert isinstance(migration, S.MigrationResult)
    assert migration.status == 'patch_validated', migration.model_dump()
    assert migration.validation.tests_run == 2
    receipt = c.migrations.validated[migration.id]
    updated = receipt.edits[c.target.file]
    assert 'const dollars = payment.amount;' in updated
    assert '(amount / 100).toFixed(2)' in updated
    assert (c.root / c.target.file).read_bytes() == original
    old = c.runner.execute(c.plan, c.root, False, edits=receipt.edits)
    assert old.relevant_failure and old.result.exit_code == 1
    # Execute the untouched native negative control against the patched source.
    local = Scenario('local-control', 'old', 'new', {}, {}, ('test/local.test.mjs',),
        frozenset(['local cents calculation remains unchanged']), c.target.target_contract_snapshot_id)
    c.runner.scenarios[local.id] = local
    local_plan = c.plan.model_copy(update=dict(scenario_id=local.id, test_files=list(local.test_files),
        old_fixture_id='old', new_fixture_id='new'))
    control = c.runner.execute(local_plan, c.root, True, edits=receipt.edits)
    assert passed(control.result) and control.assertion_seen
    wrong_edits = {c.target.file:updated.replace('(amount / 100)', 'amount')}
    wrong_patch = c.runner.execute(c.plan, c.root, True, edits=wrong_edits)
    assert not passed(wrong_patch.result) and not wrong_patch.assertion_seen
    assert not (c.root / '.shockwave').exists()
    assert any(b'api.stripe.com/v1/payment_intents/pi_demo' in c.artifacts.get(key)
               for key in result.artifact_ids)


@pytest.mark.parametrize('case', ['scenario', 'source', 'service', 'prefix'])
def test_stripe_patch_scope_rejects_wrong_evidence(checkout, case):
    c = checkout
    target = c.target
    if case == 'scenario':
        target = replace(target, plan=c.plan.model_copy(update={'scenario_id':'scenario'}))
    elif case == 'source':
        target = replace(target, file='src/other.ts')
    elif case == 'service':
        target = replace(target, service_id='other-provider')
    else:
        file = c.root / target.file
        file.write_text(file.read_text().replace('payment.amount / 100', 'payment.amount / 1000'))
        target = replace(target, source_sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    with pytest.raises((ValueError, KeyError)):
        prepare_patch(c.root, target)


@pytest.mark.parametrize('case', ['sandbox', 'tampered_plan', 'missing_sdk', 'bad_report'])
def test_catalog_inconclusive_is_not_a_regression(checkout, case):
    c = checkout
    if case == 'sandbox':
        c.plan = c.plan.model_copy(update={'runner_profile':'sandbox_node'})
    elif case == 'tampered_plan':
        c.plan = c.plan.model_copy(update={'new_fixture_id':'wrong-fixture'})
    else:
        if case == 'missing_sdk':
            shutil.rmtree(c.root / 'node_modules/stripe')
        else:
            path = c.root / 'src/checkout.ts'
            path.write_text("import { writeFileSync } from 'node:fs';\n" + path.read_text() +
                "\nprocess.on('exit', () => writeFileSync(process.env.SHOCKWAVE_REPORT_PATH!, '{}'));\n")
        c.runner.snapshots[c.snapshot.id] = TrustedSnapshot.register_team_fixture(c.root, c.snapshot.commit_sha)
    result = c.runner.verify_regression(c.plan, c.root)
    assert result.status == 'inconclusive'
    assert result.baseline.infrastructure_error
    assert result.proposed is None


@pytest.mark.parametrize('language,folder', [('python','python_storefront'),('typescript','ts_storefront')])
def test_kanishk_catalog_runs_through_sai_runner(tmp_path, language, folder):
    root = tmp_path / 'source'
    shutil.copytree(ROOT / 'tests/verification/fixtures/catalog_consumers' / folder, root)
    spec = get_scenario('demo-payments.charge-amount-units.display.' + language)
    plan = S.VerificationPlan(id=identity('legacy-plan'), risk_id=identity('legacy-risk'),
        repository_snapshot_id=identity('legacy-snapshot'), scenario_id=spec.scenario_id,
        runner_profile=spec.trusted_profile, test_files=[spec.staged_test_file],
        old_fixture_id=spec.old_fixture_id, new_fixture_id=spec.new_fixture_id,
        expected_behavior=spec.expected_behavior, timeout_seconds=20)
    scenario = Scenario.from_catalog(plan, identity('legacy-contract'))
    runner = RunnerService(ArtifactStore(tmp_path / 'artifacts'),
        {plan.repository_snapshot_id:TrustedSnapshot.register_team_fixture(root, 'b'*40)},
        {scenario.id:scenario}, consent_check=lambda _:True)
    result = runner.verify_regression(plan, root)
    assert result.status == 'verified_regression', result.model_dump()
    assert result.baseline.tests_run == result.proposed.tests_run == 1


def test_github_and_email_adapters_use_canonical_models(tmp_path):
    import io
    import tarfile
    from datetime import datetime, timezone
    from integrations.github import GitHubAdapter, GitHubError, Response
    from integrations.fakes import FakeGitHubTransport
    from integrations.email import EmailService, DevelopmentInbox
    now = datetime.now(timezone.utc)
    connection = S.RepositoryConnection(id=identity('connection'), consumer_org_id=identity('consumer'),
        installation_id='1', github_repository_id='2', full_name='team/demo', default_branch='main',
        status='connected', scan_consent_at=now, allow_model_processing=False, allow_pr_write=True)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        entry = tarfile.TarInfo('root/checkout.ts')
        entry.size = 4
        tar.addfile(entry, io.BytesIO(b'//ok'))
    sha = 'a' * 40
    routes = {
        ('GET','/user/installations?per_page=100&page=1'):Response(200, {'installations':[{'id':1}]}, {}),
        ('GET','/app/installations/1'):Response(200, {'id':1,'suspended_at':None}, {}),
        ('POST','/app/installations/1/access_tokens'):Response(201, {'token':'fake','permissions':{'contents':'read'}}, {}),
        ('GET','/installation/repositories?per_page=100&page=1'):Response(200, {'repositories':[{'id':2,'full_name':'team/demo'}]}, {}),
        ('GET',f'/repos/team/demo/commits/{sha}'):Response(200, {'sha':sha}, {}),
        ('GET',f'/repos/team/demo/tarball/{sha}'):Response(200, archive.getvalue(), {}),
    }
    client = GitHubAdapter(FakeGitHubTransport(routes), lambda:'fake', tmp_path / 'cache', lambda _:True)
    assert client.verify_installation_binding(connection.installation_id, 'fake', state='fixture',
        consume_state=lambda value:value == 'fixture')['id'] == 1
    snapshot = client.fetch_repository(connection, sha)
    assert isinstance(snapshot, S.RepositorySnapshot)
    assert snapshot.repository_connection_id == connection.id
    assert client.fetch_repository(connection, sha).content_hash == snapshot.content_hash
    routes[('GET','/installation/repositories?per_page=100&page=1')] = Response(200, {'repositories':[]}, {})
    with pytest.raises(GitHubError, match='repository_removed'):
        client.fetch_repository(connection, sha)
    client.purge_repository(connection.github_repository_id)
    assert not client.snapshot_roots
    email = EmailService(DevelopmentInbox(tmp_path / 'inbox', environment='test'), tmp_path / 'email.sqlite')
    first = email.send_invitation('consumer@example.test','https://example.test/invite/fixture','fixture')
    second = email.send_invitation('consumer@example.test','https://example.test/invite/fixture','fixture')
    assert isinstance(first, S.DeliveryResult)
    assert first.status == 'development_inbox' and first == second
    email.db.close()


def test_stripe_publication_with_canonical_authorization_and_retry(checkout, tmp_path):
    # Reuse only the stateful HTTP double. No outbound API writes occur.
    import importlib.util
    from datetime import datetime, timezone
    from integrations.github import GitHubAdapter, GitHubError
    from migration.publication import PublicationService
    module_spec = importlib.util.spec_from_file_location('publication_http_fixture',
        ROOT / 'tests/migration/test_publication.py')
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    c = checkout
    verification = c.runner.verify_regression(c.plan, c.root)
    migration = c.migrations.generate_migration(migration_request(c, verification), c.risk, verification, c.root)
    now = datetime.now(timezone.utc)
    connection = S.RepositoryConnection(id=identity('connection'), consumer_org_id=identity('consumer'),
        installation_id='1', github_repository_id='2', full_name='team/demo', default_branch='main',
        status='connected', scan_consent_at=now, allow_model_processing=False, allow_pr_write=True)
    authorization = S.PublicationAuthorization(user_id=identity('user'), consumer_org_id=connection.consumer_org_id,
        repository_connection_id=connection.id, expected_commit=c.snapshot.commit_sha, consent_timestamp=now)
    transport = module.PublicationHTTPDouble()
    github = GitHubAdapter(transport, lambda:'fake', tmp_path / 'cache', lambda _:True)
    publisher = PublicationService(github, c.migrations,
        lambda auth, conn, receipt: (auth.consumer_org_id == conn.consumer_org_id
            and auth.repository_connection_id == conn.id and auth.expected_commit == receipt.commit_sha),
        live_enabled=True)
    first = publisher.publish_migration(migration, connection, authorization)
    second = publisher.publish_migration(migration, connection, authorization)
    assert isinstance(first, S.MigrationResult)
    assert first.status == 'pr_opened' and first.pr_url == second.pr_url
    assert transport.pr_creations == 1
    bad = authorization.model_copy(update={'consumer_org_id':str(uuid4())})
    with pytest.raises(GitHubError, match='not_authorized'):
        publisher.publish_migration(migration, connection, bad)
