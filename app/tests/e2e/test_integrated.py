"""The platform API flow driven through the owners' real modules (backend.integrated).

Real: Kanishk's discovery/detect_changes/analyze_impact/build_verification_plan, Noirit's
scan_repository, Sai's verify_regression/generate_migration. Labeled stand-ins: the repository
fetch copies Noirit's local team fixture (examples/consumers/python-display) instead of a GitHub
snapshot; invitations use the development inbox; publication stays disabled.
"""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from integrations.storage import tree_hash

from backend.demo import COMMIT
from backend.integrated import integrated_factory
from backend.main import CONSENT, create_app
from backend.store import stable


@pytest.fixture
def env(tmp_path):
    factory = integrated_factory(tmp_path / 'work')
    app = create_app(tmp_path / 'test.db', demo=True, adapters_factory=factory)
    clients = {}
    for persona in ('provider', 'consumer', 'other'):
        c = TestClient(app)
        assert c.post('/api/v1/demo/session', json={'persona': persona}).status_code == 200
        clients[persona] = c
    return app, clients, factory


def post(c, path, body=None):
    return c.post('/api/v1' + path, json=body, headers={'Idempotency-Key': str(uuid4())})


def get(c, path):
    return c.get('/api/v1' + path)


def run(c, path, body=None):
    r = post(c, path, body)
    assert r.status_code == 202, r.text
    job = get(c, '/jobs/' + r.json()['id']).json()
    assert job['status'] == 'succeeded', job
    return job


def onboard(p, c):
    r = post(p, '/api-projects', {'name': 'Demo Payments', 'service_id': 'demo-payments',
                                  'base_urls': ['http://demo-payments.test']})
    assert r.status_code == 201, r.text
    project = r.json()
    assert post(p, f"/api-projects/{project['id']}/invitations", {'recipient_email': 'consumer@example.test'}).status_code == 201
    token = get(c, '/demo/inbox').json()['items'][-1]['acceptance_url'].split('=')[1]
    r = post(c, '/invitations/accept', {'token': token, 'consumer_org_id': stable('consumer'),
                                        'consent_version': CONSENT, 'share_summary': True})
    assert r.status_code == 200, r.text
    enrollment = r.json()
    connect = post(c, '/github/connect', {'return_route': '/'}).json()
    assert c.get(connect['installation_url']).status_code == 200
    repos = get(c, '/github/repositories').json()['items']
    r = post(c, '/repository-connections', {'github_repository_id': repos[0]['github_repository_id'],
                                             'enrollment_id': enrollment['id'], 'scan_consent': True,
                                             'allow_pr_write': True})
    assert r.status_code == 201, r.text
    return project, enrollment, r.json()


def test_demo_payments_change_end_to_end_through_real_modules(env):
    _app, cs, factory = env
    p, c = cs['provider'], cs['consumer']
    project, enrollment, repo = onboard(p, c)

    # Noirit's scanner finds the divide-by-100 dependency in the team fixture.
    scan_job = run(c, f"/repository-connections/{repo['id']}/scans", {'enrollment_id': enrollment['id'], 'ref': COMMIT})
    run(p, f"/api-projects/{project['id']}/assessments", {})

    # Kanishk's detect_changes: one units_changed change, not structurally breaking.
    changes = get(p, f"/api-projects/{project['id']}/changes").json()['items']
    assert [(ch['kind'], ch['field_path'], ch['structural_breaking']) for ch in changes] == [
        ('units_changed', 'response.amount', False)]
    assert 'openapi' in [item['kind'] for item in changes[0]['evidence']]

    dependencies = list(factory.state.dependencies.values())
    assert [(d.field_path, d.usage.operation, d.usage.operand, d.evidence[-1].file) for d in dependencies] == [
        ('response.amount', 'divide', 100, 'demo_display/charges.py')]
    assert scan_job['status'] == 'succeeded'

    # Kanishk's analyze_impact: one high-severity conversion risk with an executable scenario.
    risks = get(c, '/consumer/risks').json()['items']
    assert len(risks) == 1
    risk = risks[0]
    assert risk['severity'] == 'high'
    assert any(item['source_uri'] == 'shockwave-scenario://demo-payments.charge-amount-units.display.python-display'
               for item in risk['evidence'])

    # Kanishk's plan + Sai's runner: baseline passes ($10.00), proposed fails on $0.10.
    run(c, f"/risks/{risk['id']}/verifications")
    verification = next(x for x in get(c, '/consumer/risks').json()['items'] if x['id'] == risk['id'])['verifications'][-1]
    assert verification['status'] == 'verified_regression', verification
    assert verification['baseline']['exit_code'] == 0 and verification['proposed']['exit_code'] == 1
    logs = get(c, '/artifacts/' + verification['artifact_ids'][0]).json()['content']
    assert 'expected $10.00, actual $0.10' in logs

    # Sai's migration: the patch removes only the '/ 100' and the unchanged scenario passes.
    job = run(c, f"/risks/{risk['id']}/migrations", {'mode': 'preview'})
    migration = get(c, '/migrations/' + job['result_id']).json()
    assert migration['status'] == 'patch_validated', migration
    patch = get(c, '/artifacts/' + migration['patch_artifact_id']).json()['content']
    assert '-    dollars = response["amount"] / 100' in patch and '+    dollars = response["amount"]' in patch
    assert 'orders.py' not in patch  # the unrelated order-fee conversion is untouched
    assert migration['pr_url'] is None

    # The provider sees only the restricted summary.
    summary = get(p, f"/api-projects/{project['id']}/impact?change_id={changes[0]['id']}").json()['items'][0]
    assert summary['verified_regression_count'] == 1 and summary['validated_fix_count'] == 1
    assert 'charges.py' not in json.dumps(summary) and 'excerpt' not in json.dumps(summary)


def test_environment_switch_selects_the_integrated_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv('SHOCKWAVE_ADAPTERS', 'integrated')
    monkeypatch.setenv('SHOCKWAVE_WORK_DIR', str(tmp_path / 'work'))
    app = create_app(tmp_path / 'env.db', demo=True)
    client = TestClient(app)
    assert client.get('/api/v1/health').json()['integration_mode'] == 'integrated'
    assert client.post('/api/v1/demo/session', json={'persona': 'consumer'}).status_code == 200
    capabilities = client.get('/api/v1/capabilities').json()
    assert capabilities['mode'] == 'integrated' and 'scan' in capabilities['real_modules']


def test_backend_restarts_between_steps_do_not_break_verification_or_migration(tmp_path):
    # Integrated state is rebuilt from the work directory and the stored records after a restart.
    def fresh():
        app = create_app(tmp_path / 'restart.db', demo=True, adapters_factory=integrated_factory(tmp_path / 'work'))
        clients = {}
        for persona in ('provider', 'consumer'):
            client = TestClient(app)
            assert client.post('/api/v1/demo/session', json={'persona': persona}).status_code == 200
            clients[persona] = client
        return clients['provider'], clients['consumer']

    p, c = fresh()
    project, enrollment, repo = onboard(p, c)
    run(c, f"/repository-connections/{repo['id']}/scans", {'enrollment_id': enrollment['id'], 'ref': COMMIT})
    run(p, f"/api-projects/{project['id']}/assessments", {})

    p, c = fresh()  # restart before verification
    risk = get(c, '/consumer/risks').json()['items'][0]
    run(c, f"/risks/{risk['id']}/verifications")
    verification = next(x for x in get(c, '/consumer/risks').json()['items'] if x['id'] == risk['id'])['verifications'][-1]
    assert verification['status'] == 'verified_regression', verification

    p, c = fresh()  # restart before the migration
    job = run(c, f"/risks/{risk['id']}/migrations", {'mode': 'preview'})
    assert get(c, '/migrations/' + job['result_id']).json()['status'] == 'patch_validated'


def test_scan_rejects_path_traversal_before_fetch(env, tmp_path):
    _app, cs, _factory = env
    _project, enrollment, repo = onboard(cs['provider'], cs['consumer'])
    marker=tmp_path/'outside'/'marker';marker.parent.mkdir();marker.write_text('keep')
    for ref in ('..','../../../../outside','not-a-commit'):
        response=post(cs['consumer'],f"/repository-connections/{repo['id']}/scans",
                      {'enrollment_id':enrollment['id'],'ref':ref})
        assert response.status_code==422
    assert marker.read_text()=='keep'


def test_repeated_ref_keeps_each_snapshot_immutable(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'consumer.py').write_text('before')
    factory=integrated_factory(tmp_path/'work',{'acme/storefront':source})
    connection=SimpleNamespace(id=str(uuid4()),full_name='acme/storefront')
    adapter=factory(consumer_org_id=str(uuid4()))
    first=adapter.fetch_repository(connection,COMMIT)
    (source/'consumer.py').write_text('after')
    second=adapter.fetch_repository(connection,COMMIT)
    assert factory.state.snapshot(first.id).root!=factory.state.snapshot(second.id).root
    assert (factory.state.snapshot(first.id).root/'consumer.py').read_text()=='before'
    assert (factory.state.snapshot(second.id).root/'consumer.py').read_text()=='after'
    (factory.state.snapshot(first.id).root/'consumer.py').write_text('tampered')
    assert not factory.state.registered(first.id)


def test_registry_merges_process_writes_and_loads_new_snapshots(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'consumer.py').write_text('fixture')
    work=tmp_path/'work'
    first=integrated_factory(work,{'acme/storefront':source})
    second=integrated_factory(work,{'acme/storefront':source})
    connection=SimpleNamespace(id=str(uuid4()),full_name='acme/storefront')
    one=first().fetch_repository(connection,COMMIT)
    two=second().fetch_repository(connection,COMMIT)
    assert second.state.snapshot(one.id).content_hash==one.content_hash
    restarted=integrated_factory(work,{'acme/storefront':source})
    assert {one.id,two.id}.issubset(restarted.state.snapshots)


def test_invalid_registry_entries_do_not_prevent_startup(tmp_path):
    work=tmp_path/'work';work.mkdir()
    (work/'registry.json').write_text('{"bad": {}}')
    assert integrated_factory(work).state.snapshots=={}
    (work/'registry.json').write_text('{corrupt')
    assert integrated_factory(work).state.snapshots=={}
    assert list(work.glob('registry.invalid.*.json'))


def test_registry_rejects_root_outside_snapshot_area(tmp_path):
    source=tmp_path/'outside';source.mkdir();(source/'source.py').write_text('private')
    work=tmp_path/'work';work.mkdir()
    (work/'registry.json').write_text(json.dumps({str(uuid4()):{
        'root':str(source),'commit_sha':COMMIT,'content_hash':tree_hash(source),
        'repository_connection_id':str(uuid4()),'fetched_at':'2026-10-17T00:00:00+00:00'}}))
    assert integrated_factory(work).state.snapshots=={}
    assert (source/'source.py').read_text()=='private'


def test_revocation_purges_copied_source_and_registry(env):
    app, cs, factory=env
    _project,enrollment,repo=onboard(cs['provider'],cs['consumer'])
    run(cs['consumer'],f"/repository-connections/{repo['id']}/scans",{'enrollment_id':enrollment['id']})
    snapshot_id=next(iter(factory.state.records))
    root=factory.state.snapshot(snapshot_id).root
    assert root.is_dir()
    assert cs['consumer'].delete('/api/v1/enrollments/'+enrollment['id']).status_code==200
    assert not root.exists() and snapshot_id not in factory.state.records
    with app.state.store.read() as db:
        assert db.execute('SELECT count(*) FROM snapshot_cleanup').fetchone()[0]==0


def test_restart_finishes_committed_snapshot_cleanup(env):
    app, cs, factory=env
    _project,enrollment,repo=onboard(cs['provider'],cs['consumer'])
    run(cs['consumer'],f"/repository-connections/{repo['id']}/scans",{'enrollment_id':enrollment['id']})
    snapshot_id=next(iter(factory.state.records));root=factory.state.snapshot(snapshot_id).root
    with app.state.store.tx() as db:
        db.execute('INSERT INTO snapshot_cleanup VALUES(?,?)',(snapshot_id,'2026-10-17T00:00:00+00:00'))
    create_app(app.state.store.path,demo=True,adapters_factory=integrated_factory(factory.state.work_dir))
    assert not root.exists()
    with app.state.store.read() as db:
        assert db.execute('SELECT count(*) FROM snapshot_cleanup').fetchone()[0]==0


def test_failed_scan_discards_fetched_source(env, monkeypatch):
    from backend.integrated import IntegratedAdapters

    app, cs, factory=env
    _project,enrollment,repo=onboard(cs['provider'],cs['consumer'])

    def fail_after_fetch(*_args, **_kwargs):
        raise RuntimeError('scanner failed')

    monkeypatch.setattr(IntegratedAdapters,'scan_repository',fail_after_fetch)
    response=post(cs['consumer'],f"/repository-connections/{repo['id']}/scans",
                  {'enrollment_id':enrollment['id'],'ref':COMMIT})
    assert response.status_code==202
    assert get(cs['consumer'],'/jobs/'+response.json()['id']).json()['status']=='failed'
    assert factory.state.records=={}
    assert list(factory.state.snapshot_directory().iterdir())==[]


def test_lost_scan_claim_discards_fetched_source(env, monkeypatch):
    app, cs, factory=env
    _project,enrollment,repo=onboard(cs['provider'],cs['consumer'])
    monkeypatch.setattr(app.state.queue,'finish',lambda *_args: False)
    response=post(cs['consumer'],f"/repository-connections/{repo['id']}/scans",
                  {'enrollment_id':enrollment['id'],'ref':COMMIT})
    assert response.status_code==202
    assert factory.state.records=={}
    assert list(factory.state.snapshot_directory().iterdir())==[]


def test_orphan_sweep_removes_unpersisted_registered_copy(env):
    from shared.schemas import RepositoryConnection

    app, cs, factory=env
    _project,_enrollment,repo=onboard(cs['provider'],cs['consumer'])
    snapshot=factory(consumer_org_id=stable('consumer')).fetch_repository(
        RepositoryConnection.model_validate({k:v for k,v in repo.items() if k!='enrollment_permissions'}),COMMIT)
    root=factory.state.snapshot(snapshot.id).root
    assert root.is_dir()
    factory.state._last_orphan_sweep=0
    assert factory.state.prune_orphans(app.state.store,grace_seconds=0)==1
    assert not root.exists() and snapshot.id not in factory.state.records
