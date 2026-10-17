"""The website flow with Stripe as the API provider, on the owners' real modules.

Real: discovery of the pinned Stripe contract, Noirit's scanner on the team's Stripe consumer apps,
Kanishk's change detection / analysis / case scenarios (runner double: real consumer code, real
stripe SDK requests to a local response fixture), and the prepared migration patches re-tested.
The proposed Stripe change is hypothetical (not announced by Stripe).
"""
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.demo import COMMIT
from backend.integrated import integrated_factory
from backend.main import CONSENT, create_app
from backend.store import stable


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


def clients(db, work):
    app = create_app(db, demo=True, adapters_factory=integrated_factory(work))
    found = {}
    for persona in ('provider', 'consumer'):
        client = TestClient(app)
        assert client.post('/api/v1/demo/session', json={'persona': persona}).status_code == 200
        found[persona] = client
    return found['provider'], found['consumer']


def artifact(c, artifact_id):
    return get(c, '/artifacts/' + artifact_id).json()['content']


def proof(log, phase):
    for line in log.splitlines():
        if line.startswith('SHOCKWAVE-PROOF '):
            document = json.loads(line.removeprefix('SHOCKWAVE-PROOF '))
            if document['phase'] == phase:
                return document
    return None


def test_stripe_provider_end_to_end_with_a_restart(tmp_path):
    db, work = tmp_path / 'stripe.db', tmp_path / 'work'
    p, c = clients(db, work)

    project = post(p, '/api-projects', {'name': 'Stripe', 'service_id': 'stripe', 'base_urls': ['https://api.stripe.com']}).json()
    run(p, f"/api-projects/{project['id']}/discoveries", {})
    endpoints = get(p, f"/api-projects/{project['id']}/endpoints?limit=100&filter=payment_intents").json()['items']
    assert any(e['method'] == 'GET' and e['path'] == '/v1/payment_intents/{intent}' for e in endpoints)

    invite = post(p, f"/api-projects/{project['id']}/invitations", {'recipient_email': 'consumer@example.test'})
    assert invite.status_code == 201, invite.text
    enrollment = post(c, '/invitations/accept', {'invitation_id': invite.json()['id'], 'consumer_org_id': stable('consumer'),
                                                 'consent_version': CONSENT, 'share_summary': True}).json()
    c.get(post(c, '/github/connect', {'return_route': '/'}).json()['installation_url'])
    options = get(c, '/github/repositories').json()['items']
    assert sorted(o['full_name'] for o in options) == ['acme/fraud-review', 'acme/stripe-checkout']
    for option in options:
        repo = post(c, '/repository-connections', {'github_repository_id': option['github_repository_id'],
                                                   'enrollment_id': enrollment['id'], 'scan_consent': True,
                                                   'allow_pr_write': True}).json()
        assert repo['full_name'] == option['full_name']
        run(c, f"/repository-connections/{repo['id']}/scans", {'enrollment_id': enrollment['id'], 'ref': COMMIT})
    run(p, f"/api-projects/{project['id']}/assessments", {})

    changes = get(p, f"/api-projects/{project['id']}/changes").json()['items']
    assert [(ch['kind'], ch['field_path'], ch['structural_breaking'], ch['service_id']) for ch in changes] == [
        ('units_changed', 'response.amount', False, 'stripe')]

    p, c = clients(db, work)  # backend restart between assessment and verification
    def use_site(risk):
        return [e for e in risk['evidence'] if e['kind'] == 'source' and e.get('file')][-1]['file']

    risks = {use_site(r): r for r in get(c, '/consumer/risks').json()['items']}
    assert set(risks) == {'src/orderSummary.ts', 'fraud_service/review.py'}
    assert all(r['severity'] == 'high' for r in risks.values())

    expected = {'src/orderSummary.ts': ('$10.00', '$0.10'), 'fraud_service/review.py': (True, False)}
    for file, risk in risks.items():
        run(c, f"/risks/{risk['id']}/verifications")
        verification = next(x for x in get(c, '/consumer/risks').json()['items'] if x['id'] == risk['id'])['verifications'][-1]
        assert verification['status'] == 'verified_regression', verification
        log = artifact(c, verification['artifact_ids'][0])
        baseline, proposed = proof(log, 'baseline'), proof(log, 'proposed')
        assert (baseline['expected'], baseline['actual']) == (expected[file][0], expected[file][0])
        assert (proposed['expected'], proposed['actual']) == expected[file]

        job = run(c, f"/risks/{risk['id']}/migrations", {'mode': 'preview'})
        migration = get(c, '/migrations/' + job['result_id']).json()
        assert migration['status'] == 'patch_validated', migration
        patch = artifact(c, migration['patch_artifact_id'])
        if file == 'src/orderSummary.ts':
            assert '-  const totalDollars = paymentIntent.amount / 100;' in patch
            assert '+++ b/src/tax.ts' not in patch and '+++ b/src/rewards.ts' not in patch
        else:
            assert '> 500000' in patch and '> 5000:' in patch and '+++ b/fraud_service/velocity.py' not in patch

    summary = get(p, f"/api-projects/{project['id']}/impact?change_id={changes[0]['id']}").json()['items'][0]
    assert summary['verified_regression_count'] == 2 and summary['validated_fix_count'] == 2
    text = json.dumps(summary)
    assert 'orderSummary' not in text and 'review.py' not in text and 'excerpt' not in text
