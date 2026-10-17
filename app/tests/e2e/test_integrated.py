"""The platform API flow driven through the owners' real modules (backend.integrated).

Real: Kanishk's discovery/detect_changes/analyze_impact/build_verification_plan, Noirit's
scan_repository, Sai's verify_regression/generate_migration. Labeled stand-ins: the repository
fetch copies Noirit's local team fixture (examples/consumers/python-display) instead of a GitHub
snapshot; invitations use the development inbox; publication stays disabled.
"""
import json
from uuid import uuid4

import pytest
from backend.demo import COMMIT
from backend.integrated import integrated_factory
from backend.main import CONSENT, create_app
from backend.store import stable
from fastapi.testclient import TestClient


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
    invite = post(p, f"/api-projects/{project['id']}/invitations", {'recipient_email': 'consumer@example.test'})
    assert invite.status_code == 201
    r = post(c, '/invitations/accept', {'invitation_id': invite.json()['id'], 'consumer_org_id': stable('consumer'),
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
