import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from shared.schemas import *
from backend.store import Store, now, uid, stable
from backend.demo import DemoAdapters, OLD, NEW, ENDPOINT, COMMIT, endpoint, evidence

CONSENT='2026-10-16.v1'
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def later(hours=24): return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()
def dump(model): return model.model_dump(mode='json')
def page(items): return {'items':items,'next_cursor':None}

class Problem(Exception):
    def __init__(self, status, code, message, retryable=False):
        self.status,self.code,self.message,self.retryable=status,code,message,retryable

def fail(status, code, message, retryable=False): raise Problem(status,code,message,retryable)
def required(value):
    if value is None: fail(404,'NOT_FOUND','Resource not found')
    return value

class Input(BaseModel): model_config=ConfigDict(extra='forbid')
class Login(Input): persona: Literal['provider','consumer','other','unverified']
class ProjectInput(Input): name: str = Field(min_length=1,max_length=120); service_id: str; base_urls: list[str]
class InviteInput(Input): recipient_email: str = Field(min_length=3,max_length=254)
class TokenInput(Input): token: str = Field(min_length=20,max_length=200)
class AcceptInput(TokenInput): consumer_org_id: ID; consent_version: str; share_summary: bool
class ConnectInput(Input): return_route: str = '/'
class RepoInput(Input): github_repository_id: str; enrollment_id: ID; scan_consent: bool; allow_model_processing: bool=False; allow_pr_write: bool=False
class ScanInput(Input): enrollment_id: ID; ref: str=COMMIT
class DiscoverInput(Input): spec_reference: Literal['demo-fixture']='demo-fixture'
class AssessInput(Input): old_snapshot_id: ID=OLD; new_snapshot_id: ID=NEW; declared_changes: list[APIChange]=Field(default_factory=list)
class MigrationInput(Input): mode: Literal['preview']='preview'
class PublishInput(Input): explicit_consent: bool; expected_commit: str


def create_app(db_path=None, demo=None, adapters_factory=DemoAdapters):
    demo = os.getenv('SHOCKWAVE_DEMO')=='1' if demo is None else demo
    store=Store(db_path or os.getenv('SHOCKWAVE_DB','shockwave.db'))
    api=FastAPI(title='Shockwave',version='0.1.0')
    api.state.store=store
    api.state.demo=demo
    if demo:
        with store.tx() as db:
            for kind,name in [('provider','Demo API team'),('consumer','Acme storefront'),('other','Other consumer')]:
                org=stable(kind); user=stable(kind+'-user')
                db.execute('INSERT OR IGNORE INTO organizations VALUES(?,?,?)',(org,name,'provider' if kind=='provider' else 'consumer'))
                db.execute('INSERT OR IGNORE INTO users VALUES(?,?,?)',(user,kind+'@example.test',1))
                db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?)',(user,org,'admin'))
            db.execute('INSERT OR IGNORE INTO users VALUES(?,?,?)',(stable('unverified-user'),'unverified@example.test',0))
            db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?)',(stable('unverified-user'),stable('consumer'),'admin'))

    @api.exception_handler(Problem)
    async def error_handler(request, exc):
        return JSONResponse(status_code=exc.status,content={'error':{'code':exc.code,'message':exc.message,'retryable':exc.retryable},'request_id':uid()})
    @api.exception_handler(RequestValidationError)
    async def validation_handler(request,exc):
        return JSONResponse(status_code=422,content={'error':{'code':'VALIDATION_ERROR','message':'Invalid request fields or values','retryable':False},'request_id':uid()})

    @api.middleware('http')
    async def origin_guard(request, call_next):
        # Same-origin frontend proxy. Reject browser cross-site mutations.
        origin=request.headers.get('origin')
        allowed=os.getenv('SHOCKWAVE_ORIGIN','http://127.0.0.1:3000')
        if request.method not in ('GET','HEAD','OPTIONS') and (request.headers.get('sec-fetch-site')=='cross-site' or (origin and origin!=allowed)):
            return JSONResponse(status_code=403,content={'error':{'code':'BAD_ORIGIN','message':'Cross-origin mutation rejected','retryable':False},'request_id':uid()})
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    def identity(db,request):
        token=request.cookies.get('shockwave_session','')
        row=db.execute('SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires>?',(digest(token),now())).fetchone()
        if not row: fail(401,'UNAUTHENTICATED','Sign in first')
        return dict(row)
    def context(db,request,kind=None,admin=False):
        user=identity(db,request)
        org=request.headers.get('x-organization-id')
        rows=db.execute('SELECT organizations.*,memberships.role FROM memberships JOIN organizations ON organizations.id=org_id WHERE user_id=?',(user['id'],)).fetchall()
        chosen=next((dict(r) for r in rows if (not org or r['id']==org) and (not kind or r['kind']==kind)),None)
        if not chosen or (admin and chosen['role']!='admin'): fail(403,'FORBIDDEN','Authorized organization membership required')
        return user,chosen
    def owned(db,request,id,kind,orgkind=None,admin=False):
        user,org=context(db,request,orgkind,admin)
        return required(store.get(db,id,kind,org['id'])),user,org
    def active(db,enrollment_id,org):
        e=required(store.get(db,enrollment_id,'enrollment',org))
        if e['status']!='active': fail(409,'CONSENT_REVOKED','Enrollment consent was revoked')
        return e
    def repo_active(db,repo_id,org,enrollment_id=None):
        r=required(store.get(db,repo_id,'repository',org))
        if r['status']!='connected': fail(409,'CONSENT_REVOKED','Repository access was revoked')
        if enrollment_id:
            active(db,enrollment_id,org)
            if not db.execute('SELECT 1 FROM enrollment_repositories WHERE enrollment_id=? AND repository_id=?',(enrollment_id,repo_id)).fetchone(): fail(404,'NOT_FOUND','Repository is not selected for this enrollment')
        return r
    def private_risk(db,id,org):
        risk=required(store.get(db,id,'risk',org)); active(db,risk['enrollment_id'],org)
        snapshot=required(store.get(db,risk['repository_snapshot_id'],'snapshot',org))
        repo_active(db,snapshot['repository_connection_id'],org,risk['enrollment_id'])
        return risk,snapshot
    def idem(db,request,scope,body):
        key=request.headers.get('idempotency-key')
        if not key or len(key)>200: fail(422,'IDEMPOTENCY_REQUIRED','Supply an Idempotency-Key of at most 200 characters')
        fingerprint=digest(json.dumps(body,sort_keys=True))
        row=db.execute('SELECT * FROM idempotency WHERE scope=? AND key=?',(scope,key)).fetchone()
        if row:
            if row['fingerprint']!=fingerprint: fail(409,'IDEMPOTENCY_CONFLICT','Key already used for a different request')
            return key,fingerprint,required(store.get(db,row['result_id']))
        return key,fingerprint,None
    def remember(db,scope,key,fingerprint,id): db.execute('INSERT INTO idempotency VALUES(?,?,?,?)',(scope,key,fingerprint,id))
    def sanitized_invitation(inv): return {k:v for k,v in inv.items() if k!='token_hash'}
    def find_token(db,token):
        h=digest(token)
        found=next((x for x in store.list(db,'invitation') if secrets.compare_digest(x['token_hash'],h)),None)
        if not found: fail(404,'INVITATION_INVALID','Invitation not found')
        if found['status']!='pending' or found['expires_at']<=now(): fail(409,'INVITATION_UNAVAILABLE','Invitation expired, revoked, or already accepted')
        return found
    def limited(db,bucket):
        row=db.execute('SELECT * FROM rate_limits WHERE bucket=?',(bucket,)).fetchone()
        if row and row['window']>now() and row['count']>=30: fail(429,'RATE_LIMIT','Too many attempts; try again later',True)
        count=row['count']+1 if row and row['window']>now() else 1
        expiry=row['window'] if row and row['window']>now() else later(1/60)
        db.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,?)',(bucket,count,expiry))

    @api.get('/api/v1/health')
    def health(): return {'status':'ok','integration_mode':'fixture' if demo else 'unconfigured','production_auth':False}
    @api.post('/api/v1/demo/session')
    def login(body:Login,request:Request):
        if not demo: fail(404,'NOT_FOUND','Demo sessions disabled')
        with store.tx() as db:
            token=secrets.token_urlsafe(32)
            db.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),stable(body.persona+'-user'),later(8)))
        response=JSONResponse({'mode':'local_demo','verified_email_simulated':True})
        response.set_cookie('shockwave_session',token,httponly=True,samesite='strict',max_age=8*3600,secure=os.getenv('SHOCKWAVE_SECURE_COOKIE')=='1')
        return response
    @api.delete('/api/v1/session')
    def logout(request:Request):
        with store.tx() as db: db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(request.cookies.get('shockwave_session','')),))
        response=JSONResponse({'signed_out':True}); response.delete_cookie('shockwave_session'); return response
    @api.get('/api/v1/session')
    def session(request:Request):
        with store.tx() as db:
            user=identity(db,request)
            orgs=[dict(r) for r in db.execute('SELECT organizations.*,role FROM organizations JOIN memberships ON org_id=organizations.id WHERE user_id=?',(user['id'],))]
            return {'user':user,'organizations':orgs,'mode':'local_demo' if demo else 'unconfigured','consent_version':CONSENT}
    @api.post('/api/v1/api-projects',status_code=201)
    def project(body:ProjectInput,request:Request):
        with store.tx() as db:
            _,org=context(db,request,'provider',True)
            return store.put(db,'project',org['id'],dump(APIProject(id=uid(),provider_org_id=org['id'],created_at=now(),**body.model_dump())))
    @api.get('/api/v1/api-projects')
    def projects(request:Request):
        with store.tx() as db:
            _,org=context(db,request,'provider'); return page(store.list(db,'project',org['id']))
    @api.post('/api/v1/api-projects/{id}/invitations',status_code=201)
    def invite(id:str,body:InviteInput,request:Request):
        email=body.recipient_email.strip().lower()
        if '@' not in email or any(c.isspace() for c in email): fail(422,'INVALID_EMAIL','Enter a valid email')
        with store.tx() as db:
            project,_,org=owned(db,request,id,'project','provider',True)
            scope='invite:'+id; key,fp,previous=idem(db,request,scope,{'email':email})
            if previous: return sanitized_invitation(previous)
            inv=next((x for x in store.list(db,'invitation',org['id'],id) if x['recipient_email']==email and x['status']=='pending' and x['expires_at']>now()),None)
            if not inv or inv['delivery_status']=='failed':
                token=secrets.token_urlsafe(32)
                if inv:
                    inv.update(token_hash=digest(token),expires_at=later(),delivery_status='pending')
                    store.update(db,inv)
                else:
                    inv=dump(Invitation(id=uid(),api_project_id=id,recipient_email=email,token_hash=digest(token),expires_at=later(),status='pending',delivery_status='pending'))
                    store.put(db,'invitation',org['id'],inv,id)
                # This inbox is the transport, recipient-only; raw URL is never in provider APIs.
                delivery=adapters_factory().send_invitation(email,'/?invite='+token,key) if demo else DeliveryResult(status='failed',message_id=None,error='Email adapter unavailable')
                inv['delivery_status']=delivery.status
                store.update(db,inv)
                if delivery.status=='development_inbox': store.put(db,'mail',org['id'],{'id':uid(),'recipient_email':email,'acceptance_url':'/?invite='+token,'invitation_id':inv['id']},inv['id'])
            remember(db,scope,key,fp,inv['id']); return sanitized_invitation(inv)
    @api.get('/api/v1/api-projects/{id}/invitations')
    def invitations(id:str,request:Request):
        with store.tx() as db:
            _,_,org=owned(db,request,id,'project','provider',True)
            items=store.list(db,'invitation',org['id'],id)
            for item in items:
                if item['status']=='pending' and item['expires_at']<=now(): item['status']='expired'; store.update(db,item)
            return page([sanitized_invitation(x) for x in items])
    @api.delete('/api/v1/invitations/{id}')
    def revoke_invitation(id:str,request:Request):
        with store.tx() as db:
            inv,_,_=owned(db,request,id,'invitation','provider',True)
            if inv['status']=='accepted': fail(409,'ALREADY_ACCEPTED','Revoke the enrollment instead')
            inv['status']='revoked';store.update(db,inv);return sanitized_invitation(inv)
    @api.get('/api/v1/demo/inbox')
    def inbox(request:Request):
        if not demo: fail(404,'NOT_FOUND','Development inbox disabled')
        with store.tx() as db:
            user=identity(db,request)
            return page([x for x in store.list(db,'mail') if x['recipient_email']==user['email']])
    @api.post('/api/v1/invitations/preview')
    def preview(body:TokenInput,request:Request):
        # Rate-limit commits even when the token validation fails.
        with store.tx() as db: limited(db,'preview:'+request.client.host)
        with store.tx() as db:
            inv=find_token(db,body.token); project=required(store.get(db,inv['api_project_id'],'project'))
            org=db.execute('SELECT name FROM organizations WHERE id=?',(project['provider_org_id'],)).fetchone()
            return {'provider_name':org['name'],'api_name':project['name'],'consent_version':CONSENT,'expires_at':inv['expires_at'],'processing':'Shockwave centrally processes selected source. Providers receive opted-in summaries only. Model processing is optional and separately consented.'}
    @api.post('/api/v1/invitations/accept')
    def accept(body:AcceptInput,request:Request):
        with store.tx() as db:
            user,org=context(db,request,'consumer',True)
            if org['id']!=body.consumer_org_id: fail(403,'FORBIDDEN','You must administer the selected consumer organization')
            inv=find_token(db,body.token)
            if not user['verified'] or user['email'].lower()!=inv['recipient_email']: fail(403,'RECIPIENT_MISMATCH','Authenticate with the verified invited email')
            if body.consent_version!=CONSENT: fail(409,'CONSENT_VERSION','Review the current consent terms')
            existing=next((x for x in store.list(db,'enrollment',org['id']) if x['api_project_id']==inv['api_project_id'] and x['status']=='active'),None)
            if existing: fail(409,'ALREADY_ENROLLED','Organization is already enrolled')
            e=dump(Enrollment(id=uid(),api_project_id=inv['api_project_id'],consumer_org_id=org['id'],status='active',share_summary=body.share_summary,consent_version=CONSENT,accepted_at=now()))
            store.put(db,'enrollment',org['id'],e,inv['api_project_id'])
            inv.update(status='accepted',accepted_by_user_id=user['id'],consumer_org_id=org['id']);store.update(db,inv)
            return e
    @api.get('/api/v1/enrollments')
    def enrollments(request:Request):
        with store.tx() as db:
            _,org=context(db,request,'consumer');return page(store.list(db,'enrollment',org['id']))
    def revoke(db,org,repo_id=None,enrollment_id=None):
        for job in store.list(db,'job',org):
            task=store.get(db,job['id']+'-task','task')
            if job['status'] in ('queued','running') and task and (repo_id is None or task.get('repository_id')==repo_id) and (enrollment_id is None or task.get('enrollment_id')==enrollment_id):
                job.update(status='cancelled',stage='consent_revoked',updated_at=now());store.update(db,job)
        # Purge private artifacts in this scope immediately (no cached checkout in demo).
        for artifact in store.list(db,'artifact',org):
            if (repo_id is None or artifact.get('repository_id')==repo_id) and (enrollment_id is None or artifact.get('enrollment_id')==enrollment_id):
                artifact.update(content='',purged=True);store.update(db,artifact)
    @api.delete('/api/v1/enrollments/{id}')
    def revoke_enrollment(id:str,request:Request):
        with store.tx() as db:
            e,_,org=owned(db,request,id,'enrollment','consumer',True);e.update(status='revoked',revoked_at=now());store.update(db,e);revoke(db,org['id'],enrollment_id=id);return e
    @api.post('/api/v1/github/connect')
    def connect(body:ConnectInput,request:Request):
        if not demo: fail(503,'ADAPTER_UNAVAILABLE','GitHub adapter is not connected')
        if body.return_route!='/': fail(422,'RETURN_ROUTE','Only the dashboard return route is supported')
        with store.tx() as db:
            user,org=context(db,request,'consumer',True);state=secrets.token_urlsafe(32)
            db.execute('INSERT INTO installation_states VALUES(?,?,?,?,0)',(digest(state),user['id'],org['id'],later(1/6)))
            return {'mode':'fixture','installation_url':'/api/v1/github/callback?state='+state+'&installation_id=demo-'+org['id']}
    @api.get('/api/v1/github/callback')
    def callback(state:str,installation_id:str,request:Request):
        if not demo: fail(503,'ADAPTER_UNAVAILABLE','Live installation verification unavailable')
        with store.tx() as db:
            user,org=context(db,request,'consumer',True)
            row=db.execute('SELECT * FROM installation_states WHERE token_hash=?',(digest(state),)).fetchone()
            if not row or row['used'] or row['expires']<=now() or row['user_id']!=user['id'] or row['org_id']!=org['id']: fail(403,'INVALID_STATE','Installation state invalid or expired')
            if installation_id!='demo-'+org['id']: fail(403,'INSTALLATION_OWNERSHIP','Installation is not accessible to this demo account')
            db.execute('UPDATE installation_states SET used=1 WHERE token_hash=?',(digest(state),))
            db.execute('INSERT OR REPLACE INTO installations VALUES(?,?,?)',(org['id'],installation_id,user['id']))
            return {'connected':True,'mode':'fixture','installation_id':installation_id}
    @api.post('/api/v1/github/webhook')
    def webhook(): fail(503,'ADAPTER_UNAVAILABLE','Live signed webhook adapter unavailable; no event accepted')
    @api.get('/api/v1/github/repositories')
    def github_repositories(request:Request):
        with store.tx() as db:
            _,org=context(db,request,'consumer',True)
            installed=db.execute('SELECT 1 FROM installations WHERE org_id=?',(org['id'],)).fetchone()
            return page([{'github_repository_id':'fixture-'+org['id'],'full_name':'local-demo/storefront','default_branch':'main','mode':'fixture'}] if installed else [])
    @api.post('/api/v1/repository-connections',status_code=201)
    def repository(body:RepoInput,request:Request):
        with store.tx() as db:
            _,org=context(db,request,'consumer',True);active(db,body.enrollment_id,org['id'])
            if not body.scan_consent: fail(422,'CONSENT_REQUIRED','Explicit source processing consent is required')
            installed=db.execute('SELECT * FROM installations WHERE org_id=?',(org['id'],)).fetchone()
            if not demo or not installed or body.github_repository_id!='fixture-'+org['id']: fail(403,'REPOSITORY_OWNERSHIP','Repository is not in the verified installation list')
            r=next((x for x in store.list(db,'repository',org['id']) if x['github_repository_id']==body.github_repository_id and x['status']=='connected'),None)
            if not r:
                r=dump(RepositoryConnection(id=uid(),consumer_org_id=org['id'],installation_id=installed['installation_id'],github_repository_id=body.github_repository_id,full_name='local-demo/storefront',default_branch='main',status='connected',scan_consent_at=now(),allow_model_processing=body.allow_model_processing,allow_pr_write=body.allow_pr_write))
                store.put(db,'repository',org['id'],r)
            db.execute('INSERT OR IGNORE INTO enrollment_repositories VALUES(?,?)',(body.enrollment_id,r['id']))
            return r
    @api.get('/api/v1/repository-connections')
    def repositories(request:Request):
        with store.tx() as db:
            _,org=context(db,request,'consumer');return page(store.list(db,'repository',org['id']))
    @api.delete('/api/v1/repository-connections/{id}')
    def disconnect(id:str,request:Request):
        with store.tx() as db:
            r,_,org=owned(db,request,id,'repository','consumer',True);r['status']='revoked';store.update(db,r);revoke(db,org['id'],repo_id=id);return r

    def enqueue(db,request,background,kind,org,task,body,scope):
        key,fp,previous=idem(db,request,scope,body)
        if previous: return previous
        job=dump(Job(id=uid(),owner_org_id=org,kind=kind,status='queued',stage='queued',created_at=now(),updated_at=now(),result_id=None,error=None))
        store.put(db,'job',org,job)
        store.put(db,'task',org,{'id':job['id']+'-task',**task},job['id'])
        remember(db,scope,key,fp,job['id']);background.add_task(run_job,job['id']);return job
    def artifact(db,org,content,risk,snapshot):
        data={'id':uid(),'content':content[:32000],'enrollment_id':risk['enrollment_id'],'repository_id':snapshot['repository_connection_id'],'purged':False}
        store.put(db,'artifact',org,data,risk['id']);return data['id']
    def run_job(id):
        # Serial SQLite worker transaction prevents revocation racing an external boundary.
        # Demo-only adapters are local and bounded; live workers need durable outbox leases.
        try:
            with store.tx() as db:
                job=required(store.get(db,id,'job'))
                if job['status']!='queued': return
                job.update(status='running',stage='executing_fixture',updated_at=now());store.update(db,job)
            with store.tx() as db:
                job=required(store.get(db,id,'job'))
                if job['status']!='running': return
                task=required(store.get(db,id+'-task','task')); org=job['owner_org_id']
                if not demo: raise ValueError('Live adapters have not been configured')
                adapters=adapters_factory()
                if job['kind']=='discovery':
                    project=required(store.get(db,task['project_id'],'project',org))
                    result=adapters.discover_endpoints(DiscoveryInput(service_id=project['service_id'],snapshot_id=OLD,openapi_documents=['fixture://demo'],frameworks=[]),{},None)
                    data={'id':uid(),**dump(result)};store.put(db,'discovery',org,data,project['id'])
                elif job['kind']=='scan':
                    e=active(db,task['enrollment_id'],org); r=repo_active(db,task['repository_id'],org,e['id'])
                    project=required(store.get(db,e['api_project_id'],'project'))
                    if project['service_id']!='demo-payments': raise ValueError('Fixture scanner only supports Demo Payments')
                    snapshot=adapters.fetch_repository(RepositoryConnection.model_validate(r),task['ref']);store.put(db,'snapshot',org,dump(snapshot),r['id'])
                    adapters.consumer_org_id=org
                    result=adapters.scan_repository(ScanRequest(repository_snapshot_id=snapshot.id,enrollment_id=e['id'],service_id='demo-payments',endpoint_catalog=[endpoint()],supported_languages=['python','javascript']),snapshot,Path('.'))
                    data={'id':uid(),**dump(result)};store.put(db,'scan',org,data,e['id'])
                    for d in result.dependencies: store.put(db,'dependency',org,dump(d),e['id'])
                elif job['kind']=='analysis':
                    project=required(store.get(db,task['project_id'],'project',org))
                    if project['service_id']!='demo-payments': raise ValueError('Fixture units assessment only supports Demo Payments')
                    changes=[APIChange.model_validate(c) for c in task['declared_changes']] or [APIChange(id=uid(),api_project_id=project['id'],service_id='demo-payments',endpoint_id=ENDPOINT,old_snapshot_id=OLD,new_snapshot_id=NEW,field_path='response.amount',kind='units_changed',old_semantics='USD cents; 1000 = $10',new_semantics='USD dollars; 10 = $10',structural_breaking=False,evidence=[evidence('declared_change',False,None,'Fictional units change; not Stripe')])]
                    for change in changes:
                        store.put(db,'change',org,dump(change),project['id'])
                        deps=[]
                        for e in store.list(db,'enrollment',parent=project['id']):
                            if e['status']!='active' or not e['share_summary']: continue
                            # Only most recent scan per selected repository; no accumulating stale dependencies.
                            scans=store.list(db,'scan',e['consumer_org_id'],e['id']); latest={}
                            for scan in scans:
                                snap=store.get(db,scan['repository_snapshot_id'],'snapshot');r=store.get(db,snap['repository_connection_id'],'repository')
                                if r and r['status']=='connected': latest[r['id']]=scan
                            for scan in latest.values(): deps.extend(Dependency.model_validate(d) for d in scan['dependencies'])
                        result=adapters.analyze_impact(change,deps)
                        for risk in result.risks: store.put(db,'risk',risk.consumer_org_id,dump(risk),risk.enrollment_id)
                        # Provider-owned job result stores no private analysis payload.
                        data={'id':uid(),'change_id':change.id,'analysis_mode':result.analysis_mode};store.put(db,'assessment',org,data,project['id'])
                elif job['kind'] in ('verification','migration'):
                    risk,snapshot=private_risk(db,task['risk_id'],org)
                    if job['kind']=='verification':
                        plan=adapters.build_verification_plan(Risk.model_validate(risk),RepositorySnapshot.model_validate(snapshot))
                        result=adapters.verify_regression(plan,Path('.'))
                        result.artifact_ids=[artifact(db,org,adapters.logs,risk,snapshot)]
                        data=dump(result);store.put(db,'verification',org,data,risk['id'])
                    else:
                        v=required(store.get(db,task['verification_id'],'verification',org))
                        if task.get('publish'):
                            m=required(store.get(db,task['migration_id'],'migration',org));r=repo_active(db,snapshot['repository_connection_id'],org,risk['enrollment_id'])
                            if not r['allow_pr_write'] or m['status']!='patch_validated' or task['expected_commit']!=snapshot['commit_sha']: raise ValueError('Publication authorization or revision is invalid')
                            result=adapters.publish_migration(MigrationResult.model_validate(m),RepositoryConnection.model_validate(r),PublicationAuthorization(user_id=task['user_id'],consumer_org_id=org,repository_connection_id=r['id'],expected_commit=task['expected_commit'],consent_timestamp=r['scan_consent_at']))
                            data=dump(result);store.update(db,data)
                        else:
                            change=required(store.get(db,risk['change_id'],'change'))
                            result=adapters.generate_migration(MigrationRequest(risk_id=risk['id'],verification_id=v['id'],repository_snapshot_id=snapshot['id'],target_contract_snapshot_id=change['new_snapshot_id'],mode='preview'),Risk.model_validate(risk),VerificationResult.model_validate(v),Path('.'))
                            result.patch_artifact_id=artifact(db,org,adapters.patch,risk,snapshot)
                            result.validation.artifact_ids=[artifact(db,org,adapters.logs,risk,snapshot)]
                            data=dump(result);store.put(db,'migration',org,data,risk['id'])
                            store.put(db,'readiness',org,{'id':uid(),**dump(Readiness(risk_id=risk['id'],migration_id=data['id'],approval='pending',merge='not_merged',deployment='unknown',deployment_evidence=None))},risk['id'])
                job.update(status='succeeded',stage='completed_fixture',updated_at=now(),result_id=data['id']);store.update(db,job)
        except Exception as exc:
            with store.tx() as db:
                job=store.get(db,id,'job')
                if job and job['status']!='cancelled':
                    # Do not expose adapter exception text (may contain private paths or credentials).
                    job.update(status='failed',stage='failed',updated_at=now(),error={'code':'ADAPTER_FAILED','message':'Operation could not complete. Check configured adapter, active consent, revision, and supported fixture. Live email/GitHub/discovery adapters are not connected.','retryable':True});store.update(db,job)

    @api.post('/api/v1/api-projects/{id}/discoveries',status_code=202)
    def discover(id:str,body:DiscoverInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            _,_,org=owned(db,request,id,'project','provider')
            return enqueue(db,request,background,'discovery',org['id'],{'project_id':id},body.model_dump(),'discover:'+id)
    @api.get('/api/v1/api-projects/{id}/endpoints')
    def endpoints(id:str,request:Request,filter:str=''):
        with store.tx() as db:
            _,_,org=owned(db,request,id,'project','provider');results=store.list(db,'discovery',org['id'],id)
            return page([e for e in (results[-1]['endpoints'] if results else []) if filter.lower() in (e['method']+' '+e['path']).lower()])
    @api.post('/api/v1/repository-connections/{id}/scans',status_code=202)
    def scan(id:str,body:ScanInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            _,org=context(db,request,'consumer');repo_active(db,id,org['id'],body.enrollment_id)
            return enqueue(db,request,background,'scan',org['id'],{'repository_id':id,'enrollment_id':body.enrollment_id,'ref':body.ref},body.model_dump(),'scan:'+id)
    @api.post('/api/v1/api-projects/{id}/assessments',status_code=202)
    def assess(id:str,body:AssessInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            p,_,org=owned(db,request,id,'project','provider')
            if body.old_snapshot_id!=OLD or body.new_snapshot_id!=NEW: fail(422,'SNAPSHOT_INVALID','Demo assessment requires the canonical old/new snapshots')
            for c in body.declared_changes:
                if c.api_project_id!=id or c.service_id!=p['service_id'] or c.endpoint_id!=ENDPOINT or c.old_snapshot_id!=OLD or c.new_snapshot_id!=NEW: fail(422,'CHANGE_SCOPE','Declared change does not belong to this project or snapshot')
            return enqueue(db,request,background,'analysis',org['id'],{'project_id':id,**body.model_dump(mode='json')},body.model_dump(mode='json'),'analysis:'+id)
    @api.get('/api/v1/jobs')
    def jobs(request:Request):
        with store.tx() as db:
            _,org=context(db,request);return page(store.list(db,'job',org['id']))
    @api.get('/api/v1/jobs/{id}')
    def job(id:str,request:Request):
        with store.tx() as db:
            j,_,_=owned(db,request,id,'job');return j
    @api.get('/api/v1/consumer/risks')
    def risks(request:Request):
        with store.tx() as db:
            _,org=context(db,request,'consumer');items=[]
            for r in store.list(db,'risk',org['id']):
                try: private_risk(db,r['id'],org['id'])
                except Problem: continue
                items.append({**r,'verifications':store.list(db,'verification',org['id'],r['id']),'migrations':store.list(db,'migration',org['id'],r['id']),'readiness':store.list(db,'readiness',org['id'],r['id'])})
            return page(items)
    @api.post('/api/v1/risks/{id}/verifications',status_code=202)
    def verify(id:str,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            _,org=context(db,request,'consumer');r,s=private_risk(db,id,org['id'])
            return enqueue(db,request,background,'verification',org['id'],{'risk_id':id,'enrollment_id':r['enrollment_id'],'repository_id':s['repository_connection_id']},{},'verify:'+id)
    @api.post('/api/v1/risks/{id}/migrations',status_code=202)
    def migrate(id:str,body:MigrationInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            _,org=context(db,request,'consumer');r,s=private_risk(db,id,org['id']);vs=store.list(db,'verification',org['id'],id)
            if not vs or vs[-1]['status']!='verified_regression': fail(409,'VERIFICATION_REQUIRED','A verified regression is required before preview')
            return enqueue(db,request,background,'migration',org['id'],{'risk_id':id,'verification_id':vs[-1]['id'],'enrollment_id':r['enrollment_id'],'repository_id':s['repository_connection_id']},body.model_dump(),'migrate:'+id)
    @api.get('/api/v1/migrations/{id}')
    def migration(id:str,request:Request):
        with store.tx() as db:
            m,_,org=owned(db,request,id,'migration','consumer');private_risk(db,m['risk_id'],org['id']);return m
    @api.post('/api/v1/migrations/{id}/publish',status_code=202)
    def publish(id:str,body:PublishInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            m,user,org=owned(db,request,id,'migration','consumer',True);risk,snapshot=private_risk(db,m['risk_id'],org['id']);r=repo_active(db,snapshot['repository_connection_id'],org['id'],risk['enrollment_id'])
            if not body.explicit_consent or not r['allow_pr_write']: fail(403,'WRITE_CONSENT_REQUIRED','Separate repository write authorization and explicit publication consent required')
            if body.expected_commit!=snapshot['commit_sha']: fail(409,'STALE_COMMIT','Repository revision changed; rescan and validate again')
            if m['status']!='patch_validated' or not m['validation'] or m['validation']['exit_code']!=0 or m['validation']['tests_run']==0: fail(409,'PATCH_NOT_VALIDATED','Passing patch validation required')
            vs=store.list(db,'verification',org['id'],risk['id'])
            if not vs or vs[-1]['status']!='verified_regression': fail(409,'VERIFICATION_REQUIRED','Latest verification does not establish a regression')
            return enqueue(db,request,background,'migration',org['id'],{'publish':True,'migration_id':id,'risk_id':risk['id'],'verification_id':vs[-1]['id'],'user_id':user['id'],'expected_commit':body.expected_commit,'enrollment_id':risk['enrollment_id'],'repository_id':r['id']},body.model_dump(),'publish:'+id)
    @api.get('/api/v1/artifacts/{id}')
    def get_artifact(id:str,request:Request):
        with store.tx() as db:
            a,_,org=owned(db,request,id,'artifact','consumer');active(db,a['enrollment_id'],org['id']);repo_active(db,a['repository_id'],org['id'],a['enrollment_id'])
            if a['purged']: fail(404,'NOT_FOUND','Artifact was purged')
            return {'id':id,'content':a['content']}
    @api.get('/api/v1/api-projects/{id}/changes')
    def changes(id:str,request:Request):
        with store.tx() as db:
            _,_,org=owned(db,request,id,'project','provider');return page(store.list(db,'change',org['id'],id))
    @api.get('/api/v1/api-projects/{id}/impact')
    def impact(id:str,request:Request,change_id:str):
        with store.tx() as db:
            _,_,org=owned(db,request,id,'project','provider');change=required(store.get(db,change_id,'change',org['id']))
            if change['api_project_id']!=id: fail(404,'NOT_FOUND','Change not found')
            items=[]
            for e in store.list(db,'enrollment',parent=id):
                if e['status']!='active' or not e['share_summary']: continue
                consumer=e['consumer_org_id'];rs=[]
                for r in store.list(db,'risk',consumer,e['id']):
                    if r['change_id']!=change_id: continue
                    try: private_risk(db,r['id'],consumer)
                    except Problem: continue
                    rs.append(r)
                verified=validated=pending=0
                for r in rs:
                    vs=store.list(db,'verification',consumer,r['id']);ms=store.list(db,'migration',consumer,r['id'])
                    verified+=bool(vs and vs[-1]['status']=='verified_regression')
                    good=bool(vs and vs[-1]['status']=='verified_regression' and ms and ms[-1]['status'] in ('patch_validated','pr_opened') and ms[-1]['validation'] and ms[-1]['validation']['exit_code']==0 and not ms[-1]['validation']['timed_out'] and not ms[-1]['validation']['infrastructure_error'] and ms[-1]['validation']['tests_run']>0)
                    validated+=good;pending+=good
                name=db.execute('SELECT name FROM organizations WHERE id=?',(consumer,)).fetchone()[0]
                scans=store.list(db,'scan',consumer,e['id'])
                active_scans=[s for s in scans if store.get(db,store.get(db,s['repository_snapshot_id'],'snapshot')['repository_connection_id'],'repository')['status']=='connected']
                items.append(dump(ProviderImpactSummary(api_project_id=id,change_id=change_id,enrollment_id=e['id'],consumer_display_name=name,coverage='partial' if active_scans else 'unscanned',predicted_risk_count=len(rs),verified_regression_count=verified,validated_fix_count=validated,pending_approval_count=pending,deployment='unknown')))
            return page(items)
    return api

app=create_app()
