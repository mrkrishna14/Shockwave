"""Run the demo-payments cents-to-dollars scenario through the platform API and the real modules.

    cd app && PYTHONPATH=.:.. python scripts/integrated_demo.py

Real: discovery + detect_changes + analyze_impact + build_verification_plan (Kanishk),
scan_repository (Noirit), verify_regression + generate_migration (Sai), API/orchestration
(Ishan). Labeled stand-ins: the repository is Noirit's local team fixture
(examples/consumers/python-display) copied as a trusted snapshot, not fetched from GitHub;
invitations use the development inbox; publication is disabled (no PR is created).
"""
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from backend.demo import COMMIT
from backend.integrated import FIXTURE_LABEL, integrated_factory
from backend.main import CONSENT, create_app
from backend.store import stable
from fastapi.testclient import TestClient


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix='shockwave-integrated-demo-'))
    factory = integrated_factory(work / 'adapters')
    app = create_app(work / 'demo.db', demo=True, adapters_factory=factory)
    p, c = TestClient(app), TestClient(app)
    p.post('/api/v1/demo/session', json={'persona': 'provider'})
    c.post('/api/v1/demo/session', json={'persona': 'consumer'})

    def post(client, path, body=None):
        return client.post('/api/v1' + path, json=body, headers={'Idempotency-Key': str(uuid4())})

    def job(client, path, body=None):
        r = post(client, path, body)
        result = client.get('/api/v1/jobs/' + r.json()['id']).json()
        if result['status'] != 'succeeded':
            raise SystemExit(f'{path}: {result}')
        return result

    project = post(p, '/api-projects', {'name': 'Demo Payments', 'service_id': 'demo-payments',
                                        'base_urls': ['http://demo-payments.test']}).json()
    invite = post(p, f"/api-projects/{project['id']}/invitations", {'recipient_email': 'consumer@example.test'}).json()
    enrollment = post(c, '/invitations/accept', {'invitation_id': invite['id'], 'consumer_org_id': stable('consumer'),
                                                 'consent_version': CONSENT, 'share_summary': True}).json()
    c.get(post(c, '/github/connect', {'return_route': '/'}).json()['installation_url'])
    repository_id = c.get('/api/v1/github/repositories').json()['items'][0]['github_repository_id']
    repo = post(c, '/repository-connections', {'github_repository_id': repository_id, 'enrollment_id': enrollment['id'],
                                               'scan_consent': True, 'allow_pr_write': True}).json()

    print('Shockwave integrated demo — demo-payments (fictional), cents -> dollars')
    print(f'repository: {FIXTURE_LABEL}: examples/consumers/python-display')
    job(c, f"/repository-connections/{repo['id']}/scans", {'enrollment_id': enrollment['id'], 'ref': COMMIT})
    job(p, f"/api-projects/{project['id']}/assessments", {})
    for d in factory.state.dependencies.values():
        use = d.evidence[-1]
        print(f'[scan] dependency {d.field_path} {d.usage.operation} {d.usage.operand} ({d.confidence}) '
              f'at {use.file}:{use.line_start}: {use.excerpt}')
    change = p.get(f"/api/v1/api-projects/{project['id']}/changes").json()['items'][0]
    print(f"[detect_changes] {change['kind']} {change['field_path']} structural_breaking={change['structural_breaking']}")
    risk = c.get('/api/v1/consumer/risks').json()['items'][0]
    print(f"[analyze_impact] risk severity={risk['severity']} confidence={risk['confidence']}: {risk['explanation'][:160]}")
    job(c, f"/risks/{risk['id']}/verifications")
    v = next(x for x in c.get('/api/v1/consumer/risks').json()['items'] if x['id'] == risk['id'])['verifications'][-1]
    print(f"[verify_regression] {v['status']}: baseline exit {v['baseline']['exit_code']} "
          f"({v['baseline']['tests_run']} test), proposed exit {v['proposed']['exit_code']} ({v['proposed']['tests_run']} test)")
    for artifact_id in v['artifact_ids']:
        content = c.get('/api/v1/artifacts/' + artifact_id).json()['content']
        for line in content.splitlines():
            if 'expected' in line and '$' in line:
                print('   ', line.strip()[:200])
    m = c.get('/api/v1/migrations/' + job(c, f"/risks/{risk['id']}/migrations", {'mode': 'preview'})['result_id']).json()
    print(f"[generate_migration] {m['status']} ({m['generation_mode']}); changed {m['changed_files']}; "
          f"validation exit {m['validation']['exit_code']}, tests_run {m['validation']['tests_run']}")
    patch = c.get('/api/v1/artifacts/' + m['patch_artifact_id']).json()['content']
    print('\n'.join('    ' + line for line in patch.splitlines() if line[:1] in '+-' and not line.startswith(('+++', '---'))))
    print(f"[publication] not attempted (pr_url={m['pr_url']}); warnings: {m['warnings']}")
    summary = p.get(f"/api/v1/api-projects/{project['id']}/impact?change_id={change['id']}").json()['items'][0]
    print('[provider summary] ' + json.dumps({k: summary[k] for k in ('coverage', 'predicted_risk_count',
          'verified_regression_count', 'validated_fix_count', 'deployment')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
