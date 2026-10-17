"""Run an isolated, credential-free platform demonstration; never sends externally."""
import json
import tempfile
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from backend.main import create_app, CONSENT
from backend.store import stable

def post(c,path,body=None):
    r=c.post('/api/v1'+path,json=body,headers={'Idempotency-Key':str(uuid4())})
    r.raise_for_status();return r.json()
def get(c,path):
    r=c.get('/api/v1'+path);r.raise_for_status();return r.json()
def run(c,path,body=None):
    j=post(c,path,body);j=get(c,'/jobs/'+j['id']);assert j['status']=='succeeded',j;return j

def main():
    with tempfile.TemporaryDirectory(prefix='shockwave-demo-') as directory:
        app=create_app(Path(directory)/'demo.db',demo=True)
        p=TestClient(app);c=TestClient(app)
        post(p,'/demo/session',{'persona':'provider'});post(c,'/demo/session',{'persona':'consumer'})
        project=post(p,'/api-projects',{'name':'Demo Payments','service_id':'demo-payments','base_urls':['http://demo-payments.test']});pid=project['id']
        run(p,f'/api-projects/{pid}/discoveries',{})
        invite=post(p,f'/api-projects/{pid}/invitations',{'recipient_email':'consumer@example.test'})
        e=post(c,'/invitations/accept',{'invitation_id':invite['id'],'consumer_org_id':stable('consumer'),'consent_version':CONSENT,'share_summary':True})
        url=post(c,'/github/connect',{'return_route':'/'})['installation_url'];c.get(url).raise_for_status()
        selected=get(c,'/github/repositories')['items'][0]
        repo=post(c,'/repository-connections',{'github_repository_id':selected['github_repository_id'],'enrollment_id':e['id'],'scan_consent':True})
        run(c,f"/repository-connections/{repo['id']}/scans",{'enrollment_id':e['id']})
        run(p,f'/api-projects/{pid}/assessments',{})
        results=[]
        for risk in get(c,'/consumer/risks')['items']:
            run(c,f"/risks/{risk['id']}/verifications")
            j=run(c,f"/risks/{risk['id']}/migrations",{'mode':'preview'})
            m=get(c,'/migrations/'+j['result_id'])
            current=next(r for r in get(c,'/consumer/risks')['items'] if r['id']==risk['id']);v=current['verifications'][-1]
            results.append({'language_fixture':risk['evidence'][0]['file'],'verification':v['status'],'baseline_exit':v['baseline']['exit_code'],'proposed_exit':v['proposed']['exit_code'],'migration':m['status'],'validation_exit':m['validation']['exit_code'],'tests_run':m['validation']['tests_run'],'pr_url':m['pr_url']})
        change=get(p,f'/api-projects/{pid}/changes')['items'][-1]
        summary=get(p,f"/api-projects/{pid}/impact?change_id={change['id']}")['items']
        output={'mode':'fixture','source':'trusted embedded platform fixture','results':results,'provider_summary':summary,'live_integrations':'not connected'}
        target=Path(__file__).resolve().parents[1]/'docs/sample-demo-output.json';target.write_text(json.dumps(output,indent=2)+'\n');print(json.dumps(output,indent=2))
if __name__=='__main__': main()
