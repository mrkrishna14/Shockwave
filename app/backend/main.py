import hashlib
import base64
import binascii
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
from backend.jobs import JobQueue
from backend.orchestration import Orchestrator
from backend.readiness import evidence_state
from backend.settings import Settings
from backend.outbox import Outbox
from backend.auth import hash_password, verify_password
from backend.mailer import SMTPMailer

CONSENT='2026-10-16.v1'
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def later(hours=24): return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()
def dump(model): return model.model_dump(mode='json')
def page(items,request=None):
    if request is None: return {'items':items,'next_cursor':None}
    try:
        limit=int(request.query_params.get('limit','50'))
        if not 1<=limit<=100: raise ValueError()
        token=request.query_params.get('cursor')
        offset=int(base64.urlsafe_b64decode(token+'===').decode()) if token else 0
        if offset<0 or offset>len(items): raise ValueError()
    except (ValueError,UnicodeDecodeError,binascii.Error):
        fail(422,'INVALID_PAGE','Use a valid cursor and limit from 1 to 100')
    end=offset+limit
    next_cursor=base64.urlsafe_b64encode(str(end).encode()).decode().rstrip('=') if end<len(items) else None
    return {'items':items[offset:end],'next_cursor':next_cursor}

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
class SignupInput(Input):
    email: str = Field(min_length=3,max_length=254); password: str = Field(min_length=8,max_length=200)
    account_type: Literal['provider','consumer']; organization_name: str = Field(min_length=1,max_length=120)
class PasswordLogin(Input): email: str = Field(min_length=3,max_length=254); password: str = Field(min_length=1,max_length=200)
class AcceptInput(Input): invitation_id: ID; consumer_org_id: ID; consent_version: str; share_summary: bool
class ConnectInput(Input): return_route: str = '/'
class RepoInput(Input): github_repository_id: str; enrollment_id: ID; scan_consent: bool; allow_model_processing: bool=False; allow_pr_write: bool=False
class ScanInput(Input): enrollment_id: ID; ref: str=COMMIT
class DiscoverInput(Input): spec_reference: Literal['demo-fixture']='demo-fixture'
class AssessInput(Input): old_snapshot_id: ID=OLD; new_snapshot_id: ID=NEW; declared_changes: list[APIChange]=Field(default_factory=list)
class MigrationInput(Input): mode: Literal['preview']='preview'
class PublishInput(Input): explicit_consent: bool; expected_commit: str


def create_app(db_path=None, demo=None, adapters_factory=DemoAdapters):
    settings=Settings.from_env()
    demo = settings.demo if demo is None else demo
    if adapters_factory is DemoAdapters and settings.adapters == 'integrated':
        from backend.integrated import integrated_factory
        adapters_factory = integrated_factory(settings.work_dir)
    integrated = bool(getattr(adapters_factory, 'accepts_consumer_org_id', False))
    store=Store(db_path or settings.database)
    api=FastAPI(title='Shockwave',version='0.1.0')
    api.state.store=store
    api.state.demo=demo
    api.state.settings=settings
    queue=JobQueue(store)
    orchestrator=Orchestrator(store,queue,adapters_factory,demo)
    outbox=Outbox(store,adapters_factory,demo,SMTPMailer(settings) if settings.smtp_configured else None)
    api.state.queue=queue
    api.state.orchestrator=orchestrator
    api.state.outbox=outbox
    if demo:
        with store.tx() as db:
            for kind,name in [('provider','Demo API team'),('consumer','Acme storefront'),('other','Other consumer')]:
                org=stable(kind); user=stable(kind+'-user')
                db.execute('INSERT OR IGNORE INTO organizations VALUES(?,?,?)',(org,name,'provider' if kind=='provider' else 'consumer'))
                db.execute('INSERT OR IGNORE INTO users(id,email,verified) VALUES(?,?,?)',(user,kind+'@example.test',1))
                db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?)',(user,org,'admin'))
            db.execute('INSERT OR IGNORE INTO users(id,email,verified) VALUES(?,?,?)',(stable('unverified-user'),'unverified@example.test',0))
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
        allowed=settings.origin
        if request.method not in ('GET','HEAD','OPTIONS') and (request.headers.get('sec-fetch-site')=='cross-site' or (origin and origin!=allowed)):
            return JSONResponse(status_code=403,content={'error':{'code':'BAD_ORIGIN','message':'Cross-origin mutation rejected','retryable':False},'request_id':uid()})
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    def identity(db,request):
        token=request.cookies.get('shockwave_session','')
        row=db.execute('SELECT users.id,users.email,users.verified FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires>?',(digest(token),now())).fetchone()
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
            if not db.execute('SELECT scan_consent_at FROM enrollment_repositories WHERE enrollment_id=? AND repository_id=?',(enrollment_id,repo_id)).fetchone(): fail(404,'NOT_FOUND','Repository is not selected for this enrollment')
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
    def delivery_attention(db,invitation_id):
        for row in db.execute("SELECT payload,status FROM outbox WHERE kind='invitation' ORDER BY rowid DESC"):
            if json.loads(row['payload'])['invitation_id']==invitation_id:
                return row['status']=='needs_reconciliation'
        return False
    def addressed(user,inv): return bool(user['verified']) and user['email'].lower()==inv['recipient_email']
    def incoming(db,user,id):
        found=store.get(db,id,'invitation')
        if not found: fail(404,'INVITATION_INVALID','Invitation not found')
        if not addressed(user,found): fail(403,'RECIPIENT_MISMATCH','Authenticate with the verified invited email')
        if found['status']!='pending' or found['expires_at']<=now(): fail(409,'INVITATION_UNAVAILABLE','Invitation expired, revoked, or already accepted')
        return found
    def normalized_email(value):
        email=value.strip().lower()
        if '@' not in email or any(c.isspace() for c in email): fail(422,'INVALID_EMAIL','Enter a valid email')
        return email
    def start_session(db,user_id,body,status=200):
        token=secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),user_id,later(8)))
        response=JSONResponse(body,status_code=status)
        response.set_cookie('shockwave_session',token,httponly=True,samesite='strict',max_age=8*3600,secure=settings.secure_cookie)
        return response
    def limited(db,bucket):
        row=db.execute('SELECT * FROM rate_limits WHERE bucket=?',(bucket,)).fetchone()
        if row and row['window']>now() and row['count']>=30: fail(429,'RATE_LIMIT','Too many attempts; try again later',True)
        count=row['count']+1 if row and row['window']>now() else 1
        expiry=row['window'] if row and row['window']>now() else later(1/60)
        db.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,?)',(bucket,count,expiry))

    @api.get('/api/v1/health')
    def health(): return {'status':'ok','integration_mode':('integrated' if integrated else 'fixture') if demo else 'unconfigured','production_auth':False,'password_auth':True,'demo_sessions':demo}
    @api.get('/api/v1/capabilities')
    def capabilities(request:Request):
        with store.read() as db: identity(db,request)
        return {'mode':('integrated' if integrated else 'fixture') if demo else 'unconfigured',
                # Integrated mode: these steps call the owners' real modules (backend.integrated);
                # repository access stays a local trusted fixture and publication stays disabled.
                'real_modules':['discovery','scan','change_detection','impact_analysis','verification_plan',
                                'verification','migration'] if integrated else [],
                'discovery':'fixture' if demo else 'unavailable',
                'repository_access':'fixture' if demo else 'unavailable',
                'scanner':'fixture' if demo else 'unavailable',
                'verification':'trusted_fixture' if demo else 'unavailable',
                'migration':'fixture' if demo else 'unavailable',
                'email':'smtp' if outbox.mailer else 'development_inbox' if demo else 'unavailable',
                'publication':'unavailable',
                'reasons':{'publication':'Live GitHub publication adapter and fresh remote revision checks are not configured.'}}
    @api.post('/api/v1/demo/session')
    def login(body:Login,request:Request):
        if not demo: fail(404,'NOT_FOUND','Demo sessions disabled')
        with store.tx() as db:
            return start_session(db,stable(body.persona+'-user'),{'mode':'local_demo','verified_email_simulated':True})
    @api.post('/api/v1/auth/signup',status_code=201)
    def signup(body:SignupInput,request:Request):
        email=normalized_email(body.email); name=body.organization_name.strip()
        if not name: fail(422,'INVALID_ORGANIZATION','Enter an organization name')
        with store.tx() as db: limited(db,'signup:'+request.client.host)
        password_hash=hash_password(body.password)
        with store.tx() as db:
            if db.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone(): fail(409,'EMAIL_TAKEN','An account with this email already exists; sign in instead')
            user,org=uid(),uid()
            # Email ownership is not proven by a verification message yet; see README.
            db.execute('INSERT INTO users(id,email,verified,password_hash,created_at) VALUES(?,?,?,?,?)',(user,email,1,password_hash,now()))
            db.execute('INSERT INTO organizations VALUES(?,?,?)',(org,name,body.account_type))
            db.execute('INSERT INTO memberships VALUES(?,?,?)',(user,org,'admin'))
            return start_session(db,user,{'user':{'id':user,'email':email,'verified':1},'organization_id':org},201)
    @api.post('/api/v1/auth/login')
    def password_login(body:PasswordLogin,request:Request):
        email=body.email.strip().lower()
        with store.tx() as db: limited(db,'login:'+request.client.host)
        with store.read() as db: row=db.execute('SELECT id,password_hash FROM users WHERE email=?',(email,)).fetchone()
        # verify_password runs even for unknown emails so timing does not reveal which accounts exist.
        if not verify_password(body.password,row['password_hash'] if row else None): fail(401,'INVALID_CREDENTIALS','Email or password is incorrect')
        with store.tx() as db: return start_session(db,row['id'],{'signed_in':True})
    @api.delete('/api/v1/session')
    def logout(request:Request):
        with store.tx() as db: db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(request.cookies.get('shockwave_session','')),))
        response=JSONResponse({'signed_out':True}); response.delete_cookie('shockwave_session'); return response
    @api.get('/api/v1/session')
    def session(request:Request):
        with store.read() as db:
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
        with store.read() as db:
            _,org=context(db,request,'provider'); return page(store.list(db,'project',org['id']),request)
    @api.post('/api/v1/api-projects/{id}/invitations',status_code=201)
    def invite(id:str,body:InviteInput,request:Request):
        email=normalized_email(body.recipient_email)
        operation_id=None
        with store.tx() as db:
            project,_,org=owned(db,request,id,'project','provider',True)
            scope='invite:'+id; key,fp,previous=idem(db,request,scope,{'email':email})
            if previous: return sanitized_invitation(previous)
            inv=next((x for x in store.list(db,'invitation',org['id'],id) if x['recipient_email']==email and x['status']=='pending' and x['expires_at']>now()),None)
            if not inv or inv['delivery_status']=='failed':
                if inv:
                    inv.update(expires_at=later(),delivery_status='pending')
                    store.update(db,inv)
                else:
                    inv=dump(Invitation(id=uid(),api_project_id=id,recipient_email=email,expires_at=later(),status='pending',delivery_status='pending'))
                    store.put(db,'invitation',org['id'],inv,id)
                # The transport runs after this transaction commits. This record is
                # durable if the process exits before dispatch. The email is a notice
                # only: the recipient accepts after signing in with this address.
                notice={'provider_name':org['name'],'api_name':project['name'],'recipient_email':email,'app_url':settings.app_url or settings.origin}
                if outbox.available: operation_id=outbox.queue_invitation(db,inv,notice,'invitation:'+inv['id']+':'+uid())
                else:
                    inv['delivery_status']='failed';store.update(db,inv)
            remember(db,scope,key,fp,inv['id'])
            invitation_id=inv['id']
        if operation_id: outbox.dispatch(operation_id)
        with store.read() as db:
            return sanitized_invitation(required(store.get(db,invitation_id,'invitation',org['id'])))
    @api.get('/api/v1/api-projects/{id}/invitations')
    def invitations(id:str,request:Request):
        with store.tx() as db:
            _,_,org=owned(db,request,id,'project','provider',True)
            items=store.list(db,'invitation',org['id'],id)
            for item in items:
                if item['status']=='pending' and item['expires_at']<=now(): item['status']='expired'; store.update(db,item)
            return page([{**sanitized_invitation(x),'delivery_attention':delivery_attention(db,x['id'])} for x in items],request)
    @api.get('/api/v1/api-projects/{id}/consumers')
    def consumers(id:str,request:Request):
        with store.read() as db:
            _,_,org=owned(db,request,id,'project','provider')
            items=[]
            for invitation in store.list(db,'invitation',org['id'],id):
                enrolled=next((e for e in store.list(db,'enrollment',parent=id)
                               if e['consumer_org_id']==invitation.get('consumer_org_id')),None)
                items.append({'invitation_id':invitation['id'],'recipient_email':invitation['recipient_email'],
                    'invitation_status':invitation['status'],'delivery_status':invitation['delivery_status'],
                    'enrollment_status':enrolled['status'] if enrolled else 'not_accepted',
                    'share_summary':bool(enrolled and enrolled['status']=='active' and enrolled['share_summary']),
                    'delivery_attention':delivery_attention(db,invitation['id'])})
            return page(items,request)
    @api.delete('/api/v1/invitations/{id}')
    def revoke_invitation(id:str,request:Request):
        with store.tx() as db:
            inv,_,_=owned(db,request,id,'invitation','provider',True)
            if inv['status']=='accepted': fail(409,'ALREADY_ACCEPTED','Revoke the enrollment instead')
            inv['status']='revoked';store.update(db,inv);return sanitized_invitation(inv)
    @api.get('/api/v1/demo/inbox')
    def inbox(request:Request):
        if not demo: fail(404,'NOT_FOUND','Development inbox disabled')
        with store.read() as db:
            user=identity(db,request)
            return page([x for x in store.list(db,'mail') if x['recipient_email']==user['email']],request)
    @api.get('/api/v1/invitations/incoming')
    def incoming_invitations(request:Request):
        # Pending invitations addressed to the signed-in verified email; emails carry no acceptance link.
        with store.read() as db:
            user,_=context(db,request,'consumer')
            items=[]
            for inv in store.list(db,'invitation'):
                if inv['status']!='pending' or inv['expires_at']<=now() or not addressed(user,inv): continue
                project=required(store.get(db,inv['api_project_id'],'project'))
                org=db.execute('SELECT name FROM organizations WHERE id=?',(project['provider_org_id'],)).fetchone()
                items.append({'invitation_id':inv['id'],'provider_name':org['name'],'api_name':project['name'],'consent_version':CONSENT,'expires_at':inv['expires_at'],'processing':'Shockwave centrally processes selected source. Providers receive opted-in summaries only. Model processing is optional and separately consented.'})
            return page(items,request)
    @api.post('/api/v1/invitations/accept')
    def accept(body:AcceptInput,request:Request):
        with store.tx() as db:
            user,org=context(db,request,'consumer',True)
            if org['id']!=body.consumer_org_id: fail(403,'FORBIDDEN','You must administer the selected consumer organization')
            inv=incoming(db,user,body.invitation_id)
            if body.consent_version!=CONSENT: fail(409,'CONSENT_VERSION','Review the current consent terms')
            existing=next((x for x in store.list(db,'enrollment',org['id']) if x['api_project_id']==inv['api_project_id'] and x['status']=='active'),None)
            if existing: fail(409,'ALREADY_ENROLLED','Organization is already enrolled')
            e=dump(Enrollment(id=uid(),api_project_id=inv['api_project_id'],consumer_org_id=org['id'],status='active',share_summary=body.share_summary,consent_version=CONSENT,accepted_at=now()))
            store.put(db,'enrollment',org['id'],e,inv['api_project_id'])
            inv.update(status='accepted',accepted_by_user_id=user['id'],consumer_org_id=org['id']);store.update(db,inv)
            return e
    @api.get('/api/v1/enrollments')
    def enrollments(request:Request):
        with store.read() as db:
            _,org=context(db,request,'consumer');return page(store.list(db,'enrollment',org['id']),request)
    def revoke(db,org,repo_id=None,enrollment_id=None):
        for job in store.list(db,'job',org):
            task=store.get(db,job['id']+'-task','task')
            if job['status'] in ('queued','running') and task and (repo_id is None or task.get('repository_id')==repo_id) and (enrollment_id is None or task.get('enrollment_id')==enrollment_id):
                job.update(status='cancelled',stage='consent_revoked',updated_at=now());store.update(db,job);queue.cancel(db,job['id'])
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
    options_for=getattr(adapters_factory,'repository_options',None)
    def repository_options(db,org_id):
        # Integrated adapters offer the repositories for the APIs this consumer is enrolled in.
        if options_for is None:
            return [{'github_repository_id':'fixture-'+org_id,'full_name':'acme/storefront','default_branch':'main','mode':'fixture'}]
        services={(store.get(db,e['api_project_id'],'project') or {}).get('service_id')
                  for e in store.list(db,'enrollment',org_id) if e['status']=='active'}
        return options_for(org_id,{s for s in services if s})
    @api.get('/api/v1/github/repositories')
    def github_repositories(request:Request):
        with store.read() as db:
            _,org=context(db,request,'consumer',True)
            installed=db.execute('SELECT 1 FROM installations WHERE org_id=?',(org['id'],)).fetchone()
            return page(repository_options(db,org['id']) if installed else [],request)
    @api.post('/api/v1/repository-connections',status_code=201)
    def repository(body:RepoInput,request:Request):
        with store.tx() as db:
            _,org=context(db,request,'consumer',True);active(db,body.enrollment_id,org['id'])
            if not body.scan_consent: fail(422,'CONSENT_REQUIRED','Explicit source processing consent is required')
            installed=db.execute('SELECT * FROM installations WHERE org_id=?',(org['id'],)).fetchone()
            option=next((o for o in repository_options(db,org['id']) if o['github_repository_id']==body.github_repository_id),None)
            if not demo or not installed or option is None: fail(403,'REPOSITORY_OWNERSHIP','Repository is not in the verified installation list')
            r=next((x for x in store.list(db,'repository',org['id']) if x['github_repository_id']==body.github_repository_id and x['status']=='connected'),None)
            if not r:
                r=dump(RepositoryConnection(id=uid(),consumer_org_id=org['id'],installation_id=installed['installation_id'],github_repository_id=body.github_repository_id,full_name=option['full_name'],default_branch=option['default_branch'],status='connected',scan_consent_at=now(),allow_model_processing=body.allow_model_processing,allow_pr_write=body.allow_pr_write))
                store.put(db,'repository',org['id'],r)
            db.execute('''INSERT INTO enrollment_repositories(enrollment_id,repository_id,allow_model_processing,allow_pr_write,scan_consent_at)
                VALUES(?,?,?,?,?) ON CONFLICT(enrollment_id,repository_id) DO UPDATE SET
                allow_model_processing=excluded.allow_model_processing,allow_pr_write=excluded.allow_pr_write,
                scan_consent_at=excluded.scan_consent_at''',
                (body.enrollment_id,r['id'],int(body.allow_model_processing),int(body.allow_pr_write),now()))
            return {**r,'enrollment_permissions':{'enrollment_id':body.enrollment_id,
                'allow_model_processing':body.allow_model_processing,'allow_pr_write':body.allow_pr_write}}
    @api.get('/api/v1/enrollments/{id}/repositories')
    def enrollment_repositories(id:str,request:Request):
        with store.read() as db:
            e,_,org=owned(db,request,id,'enrollment','consumer')
            rows=db.execute('''SELECT repository_id,allow_model_processing,allow_pr_write,scan_consent_at
                FROM enrollment_repositories WHERE enrollment_id=? ORDER BY repository_id''',(id,)).fetchall()
            return page([{**required(store.get(db,row['repository_id'],'repository',org['id'])),
                'enrollment_permissions':{'enrollment_id':id,'allow_model_processing':bool(row['allow_model_processing']),
                'allow_pr_write':bool(row['allow_pr_write']),'scan_consent_at':row['scan_consent_at']}} for row in rows],request)
    @api.get('/api/v1/repository-connections')
    def repositories(request:Request):
        with store.read() as db:
            _,org=context(db,request,'consumer');return page(store.list(db,'repository',org['id']),request)
    @api.delete('/api/v1/repository-connections/{id}')
    def disconnect(id:str,request:Request):
        with store.tx() as db:
            r,_,org=owned(db,request,id,'repository','consumer',True);r['status']='revoked';store.update(db,r);revoke(db,org['id'],repo_id=id);return r

    def enqueue(db,request,background,kind,org,task,body,scope):
        key,fp,previous=idem(db,request,scope,body)
        if previous: return previous
        job=dump(Job(id=uid(),owner_org_id=org,kind=kind,status='queued',stage='queued',created_at=now(),updated_at=now(),result_id=None,error=None))
        queue.enqueue(db,job)
        store.put(db,'task',org,{'id':job['id']+'-task',**task},job['id'])
        remember(db,scope,key,fp,job['id'])
        if settings.inline_jobs: background.add_task(run_job,job['id'])
        return job
    def artifact(db,org,content,risk,snapshot):
        data={'id':uid(),'content':content[:32000],'enrollment_id':risk['enrollment_id'],'repository_id':snapshot['repository_connection_id'],'purged':False}
        store.put(db,'artifact',org,data,risk['id']);return data['id']
    def run_job(id):
        claim=queue.claim(id)
        if claim: orchestrator.run(claim)

    api.state.process_job=run_job

    @api.post('/api/v1/api-projects/{id}/discoveries',status_code=202)
    def discover(id:str,body:DiscoverInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            _,_,org=owned(db,request,id,'project','provider')
            return enqueue(db,request,background,'discovery',org['id'],{'project_id':id},body.model_dump(),'discover:'+id)
    @api.get('/api/v1/api-projects/{id}/endpoints')
    def endpoints(id:str,request:Request,filter:str=''):
        with store.read() as db:
            _,_,org=owned(db,request,id,'project','provider');results=store.list(db,'discovery',org['id'],id)
            return page([e for e in (results[-1]['endpoints'] if results else []) if filter.lower() in (e['method']+' '+e['path']).lower()],request)
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
        with store.read() as db:
            _,org=context(db,request)
            return page([job_view(db,j) for j in store.list(db,'job',org['id'])],request)
    def job_view(db,j):
        lease=db.execute('SELECT attempts,cancel_requested FROM job_leases WHERE job_id=?',(j['id'],)).fetchone()
        history=[dict(row) for row in db.execute('''SELECT generation,started_at,ended_at,status,error_code
            FROM job_attempts WHERE job_id=? ORDER BY generation''',(j['id'],))]
        return {**j,'attempts':lease['attempts'] if lease else 0,
                'attempt_history':history,'cancel_requested':bool(lease and lease['cancel_requested'])}
    @api.get('/api/v1/jobs/{id}')
    def job(id:str,request:Request):
        with store.read() as db:
            j,_,_=owned(db,request,id,'job');return job_view(db,j)
    @api.delete('/api/v1/jobs/{id}')
    def cancel_job(id:str,request:Request):
        with store.tx() as db:
            j,_,_=owned(db,request,id,'job')
            if j['status'] in ('queued','running'):
                queue.cancel(db,id)
                j.update(status='cancelled',stage='cancel_requested',updated_at=now())
                store.update(db,j)
            return job_view(db,j)
    @api.post('/api/v1/jobs/{id}/retry',status_code=202)
    def retry_job(id:str,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            old,_,org=owned(db,request,id,'job')
            if old['status']!='failed' or not old['error'] or not old['error']['retryable']:
                fail(409,'RETRY_UNAVAILABLE','This job cannot be retried')
            task=required(store.get(db,id+'-task','task',org['id']))
            if task.get('enrollment_id'): active(db,task['enrollment_id'],org['id'])
            if task.get('repository_id'): repo_active(db,task['repository_id'],org['id'],task.get('enrollment_id'))
            return enqueue(db,request,background,old['kind'],org['id'],
                {k:v for k,v in task.items() if k!='id'}, {'retry_of':id},'retry:'+id)
    @api.get('/api/v1/consumer/risks')
    def risks(request:Request):
        with store.read() as db:
            _,org=context(db,request,'consumer');items=[]
            for r in store.list(db,'risk',org['id']):
                try: private_risk(db,r['id'],org['id'])
                except Problem: continue
                items.append({**r,'verifications':store.list(db,'verification',org['id'],r['id']),'migrations':store.list(db,'migration',org['id'],r['id']),'readiness':store.list(db,'readiness',org['id'],r['id']),'evidence_state':evidence_state(store,db,r)})
            return page(items,request)
    @api.get('/api/v1/risks/{id}/snapshot')
    def risk_snapshot(id:str,request:Request):
        with store.read() as db:
            _,org=context(db,request,'consumer');risk,snapshot=private_risk(db,id,org['id'])
            return {'repository_snapshot_id':snapshot['id'],'commit_sha':snapshot['commit_sha'],
                    'fetched_at':snapshot['fetched_at']}
    @api.post('/api/v1/risks/{id}/verifications',status_code=202)
    def verify(id:str,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            _,org=context(db,request,'consumer');r,s=private_risk(db,id,org['id'])
            return enqueue(db,request,background,'verification',org['id'],{'risk_id':id,'enrollment_id':r['enrollment_id'],'repository_id':s['repository_connection_id']},{},'verify:'+id)
    @api.post('/api/v1/risks/{id}/migrations',status_code=202)
    def migrate(id:str,body:MigrationInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            _,org=context(db,request,'consumer');r,s=private_risk(db,id,org['id']);vs=store.list(db,'verification',org['id'],id)
            if not vs or not evidence_state(store,db,r)['verified']: fail(409,'VERIFICATION_REQUIRED','A current verified regression is required before preview')
            return enqueue(db,request,background,'migration',org['id'],{'risk_id':id,'verification_id':vs[-1]['id'],'enrollment_id':r['enrollment_id'],'repository_id':s['repository_connection_id']},body.model_dump(),'migrate:'+id)
    @api.get('/api/v1/migrations/{id}')
    def migration(id:str,request:Request):
        with store.read() as db:
            m,_,org=owned(db,request,id,'migration','consumer');private_risk(db,m['risk_id'],org['id']);return m
    @api.post('/api/v1/migrations/{id}/publish',status_code=202)
    def publish(id:str,body:PublishInput,request:Request,background:BackgroundTasks):
        with store.tx() as db:
            m,user,org=owned(db,request,id,'migration','consumer',True);risk,snapshot=private_risk(db,m['risk_id'],org['id']);r=repo_active(db,snapshot['repository_connection_id'],org['id'],risk['enrollment_id'])
            grant=db.execute('SELECT allow_pr_write FROM enrollment_repositories WHERE enrollment_id=? AND repository_id=?',
                (risk['enrollment_id'],r['id'])).fetchone()
            if not body.explicit_consent or not grant or not grant['allow_pr_write']: fail(403,'WRITE_CONSENT_REQUIRED','Separate repository write authorization and explicit publication consent required')
            if body.expected_commit!=snapshot['commit_sha']: fail(409,'STALE_COMMIT','Repository revision changed; rescan and validate again')
            if not evidence_state(store,db,risk)['validated'] or m['status']!='patch_validated': fail(409,'PATCH_NOT_VALIDATED','Current passing patch validation required')
            vs=store.list(db,'verification',org['id'],risk['id'])
            if not vs or vs[-1]['status']!='verified_regression': fail(409,'VERIFICATION_REQUIRED','Latest verification does not establish a regression')
            return enqueue(db,request,background,'migration',org['id'],{'publish':True,'migration_id':id,'risk_id':risk['id'],'verification_id':vs[-1]['id'],'user_id':user['id'],'expected_commit':body.expected_commit,'enrollment_id':risk['enrollment_id'],'repository_id':r['id']},body.model_dump(),'publish:'+id)
    @api.get('/api/v1/artifacts/{id}')
    def get_artifact(id:str,request:Request):
        with store.read() as db:
            a,_,org=owned(db,request,id,'artifact','consumer');active(db,a['enrollment_id'],org['id']);repo_active(db,a['repository_id'],org['id'],a['enrollment_id'])
            if a['purged']: fail(404,'NOT_FOUND','Artifact was purged')
            return {'id':id,'content':a['content']}
    @api.get('/api/v1/api-projects/{id}/changes')
    def changes(id:str,request:Request):
        with store.read() as db:
            _,_,org=owned(db,request,id,'project','provider');return page(store.list(db,'change',org['id'],id),request)
    @api.get('/api/v1/api-projects/{id}/impact')
    def impact(id:str,request:Request,change_id:str):
        with store.read() as db:
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
                    if evidence_state(store,db,r)['current']: rs.append(r)
                verified=validated=pending=0
                for r in rs:
                    state=evidence_state(store,db,r)
                    verified+=state['verified']
                    validated+=state['validated'];pending+=state['validated']
                name=db.execute('SELECT name FROM organizations WHERE id=?',(consumer,)).fetchone()[0]
                scans=store.list(db,'scan',consumer,e['id'])
                active_scans=[s for s in scans if store.get(db,store.get(db,s['repository_snapshot_id'],'snapshot')['repository_connection_id'],'repository')['status']=='connected']
                items.append(dump(ProviderImpactSummary(api_project_id=id,change_id=change_id,enrollment_id=e['id'],consumer_display_name=name,coverage='partial' if active_scans else 'unscanned',predicted_risk_count=len(rs),verified_regression_count=verified,validated_fix_count=validated,pending_approval_count=pending,deployment='unknown')))
            return page(items,request)
    return api

class LazyApp:
    """Construct the server on first ASGI call, never during module import."""
    def __init__(self): self.instance=None
    async def __call__(self, scope, receive, send):
        if self.instance is None: self.instance=create_app()
        await self.instance(scope, receive, send)

app=LazyApp()
