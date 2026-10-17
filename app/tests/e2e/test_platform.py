import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app, CONSENT
from backend.store import stable, now
from backend.demo import OLD, NEW, COMMIT

@pytest.fixture
def env(tmp_path):
    app=create_app(tmp_path/'test.db',demo=True)
    clients={}
    for persona in ('provider','consumer','other','unverified'):
        c=TestClient(app); assert c.post('/api/v1/demo/session',json={'persona':persona}).status_code==200;clients[persona]=c
    return app,clients

def post(c,path,body=None,key=None):
    return c.post('/api/v1'+path,json=body,headers={'Idempotency-Key':key or str(uuid4())})
def get(c,path): return c.get('/api/v1'+path)
def project(c):
    r=post(c,'/api-projects',{'name':'Demo Payments','service_id':'demo-payments','base_urls':['http://demo-payments.test']});assert r.status_code==201,r.text;return r.json()
def invitation(p,c,project_id,email='consumer@example.test'):
    r=post(p,f'/api-projects/{project_id}/invitations',{'recipient_email':email});assert r.status_code==201,r.text
    mails=get(c,'/demo/inbox').json()['items']; assert mails[-1]['invitation_id']==r.json()['id']
    return r.json(),r.json()['id']

def onboard(env, share=True, write=True):
    app,cs=env;p=cs['provider'];c=cs['consumer'];pr=project(p);inv,token=invitation(p,c,pr['id'])
    r=post(c,'/invitations/accept',{'invitation_id':token,'consumer_org_id':stable('consumer'),'consent_version':CONSENT,'share_summary':share});assert r.status_code==200,r.text;e=r.json()
    connect=post(c,'/github/connect',{'return_route':'/'}).json();assert c.get(connect['installation_url']).status_code==200
    repos=get(c,'/github/repositories').json()['items'];r=post(c,'/repository-connections',{'github_repository_id':repos[0]['github_repository_id'],'enrollment_id':e['id'],'scan_consent':True,'allow_pr_write':write});assert r.status_code==201,r.text
    return pr,e,r.json(),token

def run(c,path,body=None):
    r=post(c,path,body);assert r.status_code==202,r.text
    job=get(c,'/jobs/'+r.json()['id']).json();assert job['status']=='succeeded',job
    return job

def assessed(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr,e,r,token=onboard(env)
    run(c,f"/repository-connections/{r['id']}/scans",{'enrollment_id':e['id'],'ref':COMMIT})
    run(p,f"/api-projects/{pr['id']}/assessments",{})
    risks=get(c,'/consumer/risks').json()['items'];assert len(risks)==2
    return pr,e,r,risks

def test_vertical_slice_and_private_artifacts(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr,e,r,risks=assessed(env)
    for risk in risks:
        job=run(c,f"/risks/{risk['id']}/verifications")
        verified=get(c,'/consumer/risks').json()['items'];v=next(x for x in verified if x['id']==risk['id'])['verifications'][-1]
        assert v['status']=='verified_regression'; assert v['baseline']['exit_code']==0; assert v['proposed']['exit_code']!=0
        job=run(c,f"/risks/{risk['id']}/migrations",{'mode':'preview'})
        migration=get(c,'/migrations/'+job['result_id']).json();assert migration['status']=='patch_validated';assert migration['validation']['tests_run']==2
        artifact=migration['patch_artifact_id'];assert '/ 100' in get(c,'/artifacts/'+artifact).json()['content']
        for foreign in (p,cs['other']):
            assert get(foreign,'/artifacts/'+artifact).status_code in (403,404)
            assert get(foreign,'/migrations/'+migration['id']).status_code in (403,404)
            assert get(foreign,'/jobs/'+job['id']).status_code==404
        # Actual publication is deliberately unavailable and cannot change readiness.
        published=post(c,f"/migrations/{migration['id']}/publish",{'explicit_consent':True,'expected_commit':COMMIT},'same-publish')
        assert published.status_code==202
        failure=get(c,'/jobs/'+published.json()['id']).json();assert failure['status']=='failed'
        replay=post(c,f"/migrations/{migration['id']}/publish",{'explicit_consent':True,'expected_commit':COMMIT},'same-publish')
        assert replay.json()['id']==published.json()['id']
        assert get(c,'/migrations/'+migration['id']).json()['pr_url'] is None
    change=get(p,f"/api-projects/{pr['id']}/changes").json()['items'][-1]
    summary=get(p,f"/api-projects/{pr['id']}/impact?change_id={change['id']}").json()['items'][0]
    assert summary['verified_regression_count']==2 and summary['validated_fix_count']==2 and summary['deployment']=='unknown'
    forbidden={'repository_id','repository_snapshot_id','evidence','excerpt','file','pr_url','full_name','explanation','artifact_ids','patch_artifact_id'}
    assert not forbidden.intersection(summary)
    assert 'display.py' not in json.dumps(summary)
    assert 'Acme storefront' in json.dumps(summary)

def test_invitation_security_and_idempotency(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr=project(p)
    first=post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'consumer@example.test'},'invite-once')
    again=post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'consumer@example.test'},'invite-once')
    assert first.json()==again.json();assert 'token_hash' not in first.json()
    duplicate=post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'consumer@example.test'},'new-key')
    assert first.json()['id']==duplicate.json()['id'];assert len(get(c,'/demo/inbox').json()['items'])==1
    assert post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'other@example.test'},'invite-once').status_code==409
    mail=get(c,'/demo/inbox').json()['items'][0]
    assert 'invite=' not in mail['body'] and 'Invitations' in mail['body'] and 'acceptance_url' not in mail
    payload={'invitation_id':first.json()['id'],'consumer_org_id':stable('consumer'),'consent_version':CONSENT,'share_summary':True}
    assert post(cs['other'],'/invitations/accept',payload).status_code==403
    assert post(cs['unverified'],'/invitations/accept',payload).status_code==403
    assert post(c,'/invitations/accept',{**payload,'consumer_org_id':stable('other')}).status_code==403
    with ThreadPoolExecutor(2) as pool:
        responses=list(pool.map(lambda _:post(c,'/invitations/accept',payload),range(2)))
    assert sorted(x.status_code for x in responses)==[200,409]

def test_expired_and_revoked_invitations(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr=project(p);inv,token=invitation(p,c,pr['id'])
    with app.state.store.tx() as db:
        value=app.state.store.get(db,inv['id']);value['expires_at']='2000-01-01T00:00:00+00:00';app.state.store.update(db,value)
    accept={'consumer_org_id':stable('consumer'),'consent_version':CONSENT,'share_summary':True}
    assert get(c,'/invitations/incoming').json()['items']==[]
    assert post(c,'/invitations/accept',{**accept,'invitation_id':token}).status_code==409
    inv,token=invitation(p,c,pr['id']);assert p.delete('/api/v1/invitations/'+inv['id']).status_code==200
    assert get(c,'/invitations/incoming').json()['items']==[]
    assert post(c,'/invitations/accept',{**accept,'invitation_id':token}).status_code==409

def test_installation_forgery_and_selected_repo(env):
    app,cs=env;c=cs['consumer'];pr,e,r,_=onboard(env)
    url=post(c,'/github/connect',{'return_route':'/'}).json()['installation_url']
    assert c.get(url.replace('demo-'+stable('consumer'),'demo-'+stable('other'))).status_code==403
    assert cs['other'].get(url).status_code==403
    assert c.get(url).status_code==200;assert c.get(url).status_code==403
    assert post(c,'/repository-connections',{'github_repository_id':'forged','enrollment_id':e['id'],'scan_consent':True}).status_code==403
    assert post(cs['other'],f"/repository-connections/{r['id']}/scans",{'enrollment_id':e['id']}).status_code==404
    assert get(c,'/repository-connections').status_code==200
    assert c.get('/api/v1/repository-connections',headers={'x-organization-id':stable('other')}).status_code==403

def test_revocation_blocks_private_results_and_publication(env):
    app,cs=env;c=cs['consumer'];pr,e,r,risks=assessed(env);risk=risks[0]
    run(c,f"/risks/{risk['id']}/verifications")
    job=run(c,f"/risks/{risk['id']}/migrations",{'mode':'preview'});m=get(c,'/migrations/'+job['result_id']).json()
    assert c.delete('/api/v1/enrollments/'+e['id']).status_code==200
    assert post(c,f"/repository-connections/{r['id']}/scans",{'enrollment_id':e['id']}).status_code==409
    assert post(c,f"/migrations/{m['id']}/publish",{'explicit_consent':True,'expected_commit':COMMIT}).status_code==409
    assert get(c,'/artifacts/'+m['patch_artifact_id']).status_code==409
    assert get(c,'/consumer/risks').json()['items']==[]
    with app.state.store.tx() as db: assert app.state.store.get(db,m['patch_artifact_id'])['purged']

def test_nonsharing_enrollment_not_assessed(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr,e,r,_=onboard(env,share=False)
    run(c,f"/repository-connections/{r['id']}/scans",{'enrollment_id':e['id']})
    run(p,f"/api-projects/{pr['id']}/assessments",{})
    assert get(c,'/consumer/risks').json()['items']==[]

def test_restart_and_no_production_demo_login(env,tmp_path):
    app,cs=env;pr,e,r,_=onboard(env);c=cs['consumer'];job=run(c,f"/repository-connections/{r['id']}/scans",{'enrollment_id':e['id']})
    with app.state.store.tx() as db:
        j=app.state.store.get(db,job['id']);j['status']='running';app.state.store.update(db,j)
    restarted=create_app(app.state.store.path,demo=True)
    c2=TestClient(restarted);c2.cookies.update(c.cookies)
    assert get(c2,'/jobs/'+job['id']).json()['status']=='running'
    claim=restarted.state.queue.claim(job['id'])
    assert claim is not None
    restarted.state.orchestrator.run(claim)
    assert get(c2,'/jobs/'+job['id']).json()['status']=='succeeded'
    assert len(get(c2,'/repository-connections').json()['items'])==1
    prod=TestClient(create_app(tmp_path/'prod.db',demo=False));assert post(prod,'/demo/session',{'persona':'provider'}).status_code==404
    assert get(prod,'/session').status_code==401

def test_origins_invalid_input_and_no_arbitrary_fetch(env):
    app,cs=env;p=cs['provider'];pr=project(p)
    assert p.post('/api/v1/api-projects',json={},headers={'Origin':'https://evil.test'}).status_code==403
    assert post(p,f"/api-projects/{pr['id']}/discoveries",{'spec_reference':'http://127.0.0.1/secret'}).status_code==422
    r=post(p,f"/api-projects/{pr['id']}/discoveries",{});assert r.status_code==202
    assert len(get(p,f"/api-projects/{pr['id']}/endpoints").json()['items'])==1
    assert post(p,f"/api-projects/{pr['id']}/assessments",{'old_snapshot_id':stable('forged')}).status_code==422

def test_queued_work_cancelled_on_disconnect(env):
    from shared.schemas import Job
    from backend.store import uid
    app,cs=env;pr,e,r,_=onboard(env);s=app.state.store
    with s.tx() as db:
        j=Job(id=uid(),owner_org_id=stable('consumer'),kind='scan',status='queued',stage='queued',created_at=now(),updated_at=now(),result_id=None,error=None).model_dump(mode='json')
        s.put(db,'job',stable('consumer'),j)
        s.put(db,'task',stable('consumer'),{'id':j['id']+'-task','repository_id':r['id'],'enrollment_id':e['id']},j['id'])
    assert cs['consumer'].delete('/api/v1/repository-connections/'+r['id']).status_code==200
    assert get(cs['consumer'],'/jobs/'+j['id']).json()['status']=='cancelled'

@pytest.mark.parametrize('baseline_code,tests,timed_out,infra,expected',[(1,2,False,None,'inconclusive'),(0,0,False,None,'inconclusive'),(0,2,True,None,'inconclusive'),(0,2,False,'runner unavailable','inconclusive')])
def test_bad_baselines_never_verified(monkeypatch,baseline_code,tests,timed_out,infra,expected):
    from backend import demo
    from shared.schemas import ExecutionResult, VerificationPlan
    baseline=ExecutionResult(exit_code=baseline_code,tests_run=tests,timed_out=timed_out,infrastructure_error=infra,artifact_ids=[],duration_ms=0)
    proposed=ExecutionResult(exit_code=1,tests_run=2,timed_out=False,infrastructure_error=None,artifact_ids=[],duration_ms=0)
    calls=iter([(baseline,''),(proposed,"AssertionError '$0.10' != '$10.00'")]);monkeypatch.setattr(demo,'execute',lambda *a,**k:next(calls))
    p=VerificationPlan(id=stable('plan'),risk_id=stable('risk'),repository_snapshot_id=stable('snapshot'),scenario_id='demo',runner_profile='trusted_python',test_files=['display.py'],old_fixture_id=OLD,new_fixture_id=NEW,expected_behavior='$10.00',timeout_seconds=45)
    assert demo.DemoAdapters().verify_regression(p,None).status==expected

def test_failed_patch_cannot_publish_or_turn_readiness_green(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr,e,r,risks=assessed(env);risk=risks[0]
    run(c,f"/risks/{risk['id']}/verifications");job=run(c,f"/risks/{risk['id']}/migrations",{'mode':'preview'})
    with app.state.store.tx() as db:
        m=app.state.store.get(db,job['result_id']);m['status']='validation_failed';m['validation']['exit_code']=1;app.state.store.update(db,m)
    assert post(c,f"/migrations/{m['id']}/publish",{'explicit_consent':True,'expected_commit':COMMIT}).status_code==409
    change=get(p,f"/api-projects/{pr['id']}/changes").json()['items'][-1]
    summary=get(p,f"/api-projects/{pr['id']}/impact?change_id={change['id']}").json()['items'][0]
    assert summary['validated_fix_count']==0 and summary['deployment']=='unknown'

def test_no_scan_duplicates_after_rescan(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr,e,r,_=onboard(env)
    for _ in range(2):run(c,f"/repository-connections/{r['id']}/scans",{'enrollment_id':e['id']})
    run(p,f"/api-projects/{pr['id']}/assessments",{})
    assert len(get(c,'/consumer/risks').json()['items'])==2

def test_delivery_retry_does_not_duplicate_invitation(env,monkeypatch):
    from backend.demo import DemoAdapters
    from shared.schemas import DeliveryResult
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr=project(p)
    original=DemoAdapters.send_invitation
    monkeypatch.setattr(DemoAdapters,'send_invitation',lambda *args:DeliveryResult(status='failed',message_id=None,error='simulated transport failure'))
    a=post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'consumer@example.test'},'failed')
    assert a.json()['delivery_status']=='failed'
    monkeypatch.setattr(DemoAdapters,'send_invitation',original)
    b=post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'consumer@example.test'},'retry')
    assert b.json()['delivery_status']=='development_inbox' and a.json()['id']==b.json()['id']
    assert len(get(c,'/demo/inbox').json()['items'])==1

def test_rescan_invalidates_previous_readiness(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];pr,e,r,risks=assessed(env)
    risk=risks[0]
    run(c,f"/risks/{risk['id']}/verifications")
    run(c,f"/risks/{risk['id']}/migrations",{'mode':'preview'})
    before=get(c,'/consumer/risks').json()['items']
    assert next(x for x in before if x['id']==risk['id'])['evidence_state']['validated']
    run(c,f"/repository-connections/{r['id']}/scans",{'enrollment_id':e['id']})
    after=get(c,'/consumer/risks').json()['items']
    assert not next(x for x in after if x['id']==risk['id'])['evidence_state']['current']
    change=get(p,f"/api-projects/{pr['id']}/changes").json()['items'][-1]
    summary=get(p,f"/api-projects/{pr['id']}/impact?change_id={change['id']}").json()['items'][0]
    assert summary['verified_regression_count']==0 and summary['validated_fix_count']==0

def test_repository_permissions_are_per_enrollment(env):
    app,cs=env;p,c=cs['provider'],cs['consumer'];first,e,repo,_=onboard(env,write=True)
    second=project(p);inv,token=invitation(p,c,second['id'])
    accepted=post(c,'/invitations/accept',{'invitation_id':token,'consumer_org_id':stable('consumer'),
        'consent_version':CONSENT,'share_summary':True}).json()
    selected=post(c,'/repository-connections',{'github_repository_id':'fixture-'+stable('consumer'),
        'enrollment_id':accepted['id'],'scan_consent':True,'allow_pr_write':False}).json()
    assert selected['id']==repo['id']
    assert not selected['enrollment_permissions']['allow_pr_write']
    first_selection=get(c,f"/enrollments/{e['id']}/repositories").json()['items'][0]
    second_selection=get(c,f"/enrollments/{accepted['id']}/repositories").json()['items'][0]
    assert first_selection['enrollment_permissions']['allow_pr_write']
    assert not second_selection['enrollment_permissions']['allow_pr_write']

def test_pagination_and_capabilities_are_scoped(env):
    app,cs=env;p,c=cs['provider'],cs['consumer']
    for _ in range(3):project(p)
    first=p.get('/api/v1/api-projects?limit=2').json()
    second=p.get('/api/v1/api-projects?limit=2&cursor='+first['next_cursor']).json()
    assert len(first['items'])==2 and len(second['items'])==1
    assert len({x['id'] for x in first['items']+second['items']})==3
    assert p.get('/api/v1/api-projects?limit=999').status_code==422
    assert get(c,'/capabilities').json()['publication']=='unavailable'
    assert get(p,'/consumer/risks').status_code==403

def test_slow_adapter_does_not_block_api(tmp_path,monkeypatch):
    from threading import Event
    from backend.demo import DemoAdapters
    entered=Event();release=Event()
    class SlowAdapters(DemoAdapters):
        def discover_endpoints(self,*args):
            entered.set();assert release.wait(5)
            return super().discover_endpoints(*args)
    monkeypatch.setenv('SHOCKWAVE_INLINE_JOBS','0')
    app=create_app(tmp_path/'slow.db',demo=True,adapters_factory=SlowAdapters)
    p=TestClient(app);post(p,'/demo/session',{'persona':'provider'})
    pr=project(p)
    response=post(p,f"/api-projects/{pr['id']}/discoveries",{})
    assert response.status_code==202
    job_id=response.json()['id'];claim=app.state.queue.claim(job_id)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(app.state.orchestrator.run,claim)
        assert entered.wait(5)
        assert get(p,'/jobs/'+job_id).json()['status']=='running'
        assert get(p,'/api-projects').status_code==200
        release.set();future.result(timeout=5)
    assert get(p,'/jobs/'+job_id).json()['status']=='succeeded'
