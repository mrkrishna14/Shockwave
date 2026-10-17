"""Explicit platform-owned local doubles, not teammates' analysis internals.
Only embedded trusted code executes. Never substitute user repository contents.
"""
import difflib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from shared.schemas import *
from backend.store import uid, stable, now

OLD = stable('demo-old')
NEW = stable('demo-new')
ENDPOINT = stable('demo-charge-endpoint')
COMMIT = 'd' * 40
SOURCE = 'def display(response):\n    return f"${response[\"amount\"] / 100:.2f}"\n'
PATCHED = SOURCE.replace(' / 100', '')
JS_SOURCE = "const display = response => '$' + (response.amount / 100).toFixed(2);\n"
JS_PATCHED = JS_SOURCE.replace(' / 100', '')
COVERAGE = Coverage(files_scanned=2, files_skipped=1, unsupported_patterns=['Fixture adapter: general repository scanning unavailable'])

def evidence(kind='source', private=True, file='display.py', excerpt=SOURCE):
    return Evidence(id=stable(str((kind,private,file,excerpt))),kind=kind,visibility='consumer_private' if private else 'provider_owned',source_uri='fixture://demo-payments',revision=COMMIT,file=file,line_start=1,line_end=2,excerpt=excerpt)

def endpoint():
    return Endpoint(id=ENDPOINT,service_id='demo-payments',method='GET',path='/v1/charges/{id}',snapshot_id=OLD,operation_id='get_charge',request_schema={},response_schema={'type':'object','properties':{'amount':{'type':'number'}}},evidence=[evidence('openapi',False,None,'Platform fixture; not a discovery scan')],reconciliation='not_compared',confidence='direct')

def execute(amount, patched=False, language='python'):
    """Real native assertion on a bounded, trusted, embedded fixture only."""
    start=time.monotonic()
    if language == 'python':
        content=(PATCHED if patched else SOURCE) + f'\nimport unittest\nclass DisplayTest(unittest.TestCase):\n    def test_currency(self):\n        self.assertEqual(display({{"amount": {amount}}}), "$10.00")\n    def test_unrelated_conversion(self):\n        self.assertEqual(500 / 100, 5)\nunittest.main()\n'
        argv=[sys.executable, '-I', 'fixture.py']; filename='fixture.py'
    else:
        content="const {test} = require('node:test'); const assert = require('node:assert/strict');\n" + (JS_PATCHED if patched else JS_SOURCE) + f"test('currency', () => assert.equal(display({{amount:{amount}}}), '$10.00'));\ntest('unrelated', () => assert.equal(500 / 100, 5));\n"
        argv=['node','--test','fixture.cjs']; filename='fixture.cjs'
    with tempfile.TemporaryDirectory(prefix='shockwave-trusted-') as directory:
        Path(directory,filename).write_text(content)
        try:
            proc=subprocess.run(argv,cwd=directory,env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':directory},capture_output=True,text=True,timeout=45)
            log=(proc.stdout+proc.stderr)[:16000]
            tests=2 if ('Ran 2 tests' in log or '# tests 2' in log) else 0
            return ExecutionResult(exit_code=proc.returncode,timed_out=False,infrastructure_error=None,artifact_ids=[],duration_ms=int((time.monotonic()-start)*1000),tests_run=tests), log
        except (OSError,subprocess.TimeoutExpired) as exc:
            return ExecutionResult(exit_code=None,timed_out=isinstance(exc,subprocess.TimeoutExpired),infrastructure_error=type(exc).__name__,artifact_ids=[],duration_ms=int((time.monotonic()-start)*1000),tests_run=0), str(exc)

class DemoAdapters:
    mode='fixture'
    def discover_endpoints(self, request, fetched_inputs, source_root):
        if request.service_id != 'demo-payments': raise ValueError('Live discovery adapter not connected; Stripe inventory unavailable')
        return DiscoveryResult(service_id=request.service_id,snapshot_id=OLD,endpoints=[endpoint()],coverage=COVERAGE,warnings=['Fixture inventory; not real Stripe or server-source discovery'])
    def fetch_repository(self, connection, commit_sha):
        if commit_sha != COMMIT: raise ValueError('Demo adapter supports only the pinned fixture revision')
        import hashlib
        return RepositorySnapshot(id=uid(),repository_connection_id=connection.id,commit_sha=COMMIT,fetched_at=now(),content_hash=hashlib.sha256(SOURCE.encode()).hexdigest())
    def send_invitation(self, recipient_email, acceptance_url, idempotency_key):
        return DeliveryResult(status='development_inbox',message_id=idempotency_key,error=None)
    def scan_repository(self, request, snapshot, repo_root):
        deps=[]
        for language,file in [('python','display.py'),('javascript','display.cjs')]:
            deps.append(Dependency(id=uid(),consumer_org_id=self.consumer_org_id,enrollment_id=request.enrollment_id,repository_snapshot_id=snapshot.id,service_id='demo-payments',endpoint_id=ENDPOINT,language=language,field_path='response.amount',origin=Origin(kind='http_response',library='demo-fixture',call='get_charge'),usage=Usage(operation='divide',operand=100,destination='display'),confidence='direct',evidence=[evidence(file=file,excerpt=SOURCE if language=='python' else JS_SOURCE)]))
        return ScanResult(repository_snapshot_id=snapshot.id,dependencies=deps,coverage=COVERAGE,warnings=['Fixture dependencies; no real repository scanned'])
    def detect_changes(self, old, new, declared_changes): return declared_changes
    def analyze_impact(self, change, dependencies):
        risks=[Risk(id=uid(),change_id=change.id,consumer_org_id=d.consumer_org_id,enrollment_id=d.enrollment_id,repository_snapshot_id=d.repository_snapshot_id,dependency_ids=[d.id],severity='high',confidence='direct',explanation='Fixture prediction: dividing a dollar amount by 100 displays $0.10 instead of $10.00.',suggested_action='Remove the conversion only for the new contract; coordinate deployment.',evidence=d.evidence) for d in dependencies if d.service_id==change.service_id and d.endpoint_id==change.endpoint_id and d.usage.operation=='divide']
        return AnalysisResult(change_id=change.id,risks=risks,unresolved_dependency_ids=[],warnings=['Fixture analysis, not live AI'],analysis_mode='fixture')
    def build_verification_plan(self, risk, snapshot):
        js=risk.evidence[0].file.endswith('.cjs')
        return VerificationPlan(id=uid(),risk_id=risk.id,repository_snapshot_id=snapshot.id,scenario_id='demo-usd-display',runner_profile='trusted_node' if js else 'trusted_python',test_files=[risk.evidence[0].file],old_fixture_id=OLD,new_fixture_id=NEW,expected_behavior='$10.00',timeout_seconds=45)
    def verify_regression(self, plan, repo_root):
        language='javascript' if plan.runner_profile=='trusted_node' else 'python'
        baseline,old_log=execute(1000,language=language); proposed,new_log=execute(10,language=language)
        valid=baseline.exit_code==0 and not baseline.timed_out and not baseline.infrastructure_error and baseline.tests_run>0 and proposed.tests_run>0 and not proposed.infrastructure_error and not proposed.timed_out
        relevant='$0.10' in new_log and '$10.00' in new_log and ('AssertionError' in new_log or 'ERR_ASSERTION' in new_log)
        status='verified_regression' if valid and proposed.exit_code!=0 and relevant else 'compatible_within_tested_scope' if valid and proposed.exit_code==0 else 'inconclusive'
        self.logs=old_log+'\nPROPOSED\n'+new_log
        return VerificationResult(id=uid(),risk_id=plan.risk_id,repository_snapshot_id=plan.repository_snapshot_id,status=status,baseline=baseline,proposed=proposed,scenario_id=plan.scenario_id,summary='Trusted fixture only: expected $10.00; proposed response produces $0.10.' if status=='verified_regression' else 'See execution evidence; no general compatibility claim.',artifact_ids=[])
    def generate_migration(self, request, risk, verification, repo_root):
        js=risk.evidence[0].file.endswith('.cjs')
        validation,log=execute(10,patched=True,language='javascript' if js else 'python')
        self.logs=log
        self.patch=''.join(difflib.unified_diff((SOURCE if not js else JS_SOURCE).splitlines(True),(PATCHED if not js else JS_PATCHED).splitlines(True),fromfile='a/'+risk.evidence[0].file,tofile='b/'+risk.evidence[0].file))
        return MigrationResult(id=uid(),risk_id=risk.id,repository_snapshot_id=request.repository_snapshot_id,target_contract_snapshot_id=request.target_contract_snapshot_id,status='patch_validated' if validation.exit_code==0 and not validation.timed_out and not validation.infrastructure_error and validation.tests_run>0 and verification.status=='verified_regression' else 'validation_failed',generation_mode='fixture',patch_artifact_id=None,changed_files=[risk.evidence[0].file],validation=validation,pr_url=None,branch=None,warnings=['Trusted local fixture patch; not applied to a GitHub repository.','New contract only; incompatible with old responses.'])
    def publish_migration(self, migration, connection, authorization):
        raise ValueError('Live PR publication unavailable: connect Sai’s GitHub adapter. No PR has been created.')
