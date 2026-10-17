from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app, CONSENT

def post(c,path,body=None):
    return c.post('/api/v1'+path,json=body,headers={'Idempotency-Key':str(uuid4())})
def get(c,path): return c.get('/api/v1'+path)
def account(app,email,kind,org_name,password='correct horse battery'):
    c=TestClient(app)
    r=post(c,'/auth/signup',{'email':email,'password':password,'account_type':kind,'organization_name':org_name})
    assert r.status_code==201,r.text
    return c,r.json()['organization_id']
def project(c):
    r=post(c,'/api-projects',{'name':'Payments API','service_id':'demo-payments','base_urls':['http://demo-payments.test']})
    assert r.status_code==201,r.text;return r.json()

@pytest.fixture
def app(tmp_path): return create_app(tmp_path/'auth.db',demo=True)

def test_signup_creates_unique_accounts_and_organizations(app):
    p1,p1_org=account(app,'Owner@Provider-One.test','provider','Provider One')
    p2,p2_org=account(app,'owner@provider-two.test','provider','Provider Two')
    c1,c1_org=account(app,'dev@consumer-one.test','consumer','Consumer One')
    assert len({p1_org,p2_org,c1_org})==3
    session=get(p1,'/session').json()
    assert session['user']['email']=='owner@provider-one.test' and 'password_hash' not in session['user']
    assert [(o['name'],o['kind'],o['role']) for o in session['organizations']]==[('Provider One','provider','admin')]
    duplicate=post(TestClient(app),'/auth/signup',{'email':'OWNER@provider-one.test','password':'another password','account_type':'consumer','organization_name':'X'})
    assert duplicate.status_code==409 and duplicate.json()['error']['code']=='EMAIL_TAKEN'
    # Providers cannot see each other's projects; consumers cannot act as providers.
    mine=project(p1)
    assert get(p2,'/api-projects').json()['items']==[]
    assert get(p2,f"/api-projects/{mine['id']}/invitations").status_code==404
    assert post(c1,'/api-projects',{'name':'x','service_id':'s','base_urls':[]}).status_code==403

def test_password_login(app):
    account(app,'dev@consumer.test','consumer','Consumer')
    c=TestClient(app)
    for body in ({'email':'dev@consumer.test','password':'wrong password'},{'email':'nobody@consumer.test','password':'correct horse battery'}):
        r=post(c,'/auth/login',body);assert r.status_code==401 and r.json()['error']['code']=='INVALID_CREDENTIALS'
    assert get(c,'/session').status_code==401
    assert post(c,'/auth/login',{'email':' DEV@consumer.test ','password':'correct horse battery'}).status_code==200
    assert get(c,'/session').json()['user']['email']=='dev@consumer.test'
    assert c.delete('/api/v1/session').status_code==200 and get(c,'/session').status_code==401
    # Seeded demo personas have no password and cannot be signed into.
    assert post(c,'/auth/login',{'email':'consumer@example.test','password':''}).status_code==422
    assert post(c,'/auth/login',{'email':'consumer@example.test','password':'anything'}).status_code==401
    assert post(c,'/auth/signup',{'email':'short@consumer.test','password':'short','account_type':'consumer','organization_name':'S'}).status_code==422

def test_invitation_is_accepted_in_app_only_by_the_invited_consumer(app):
    p,_=account(app,'owner@provider.test','provider','Payments Co')
    a,a_org=account(app,'alice@consumer-a.test','consumer','Consumer A')
    b,b_org=account(app,'bob@consumer-b.test','consumer','Consumer B')
    pr=project(p)
    inv=post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'Alice@consumer-a.test'}).json()
    mail=get(a,'/demo/inbox').json()['items']
    assert len(mail)==1 and mail[0]['subject']=='Payments Co invited you to Payments API on Shockwave'
    assert inv['id'] not in mail[0]['body'] and '?invite=' not in mail[0]['body'] and 'notification only' in mail[0]['body']
    assert get(b,'/demo/inbox').json()['items']==[]
    incoming=get(a,'/invitations/incoming').json()['items']
    assert [(x['invitation_id'],x['provider_name'],x['api_name']) for x in incoming]==[(inv['id'],'Payments Co','Payments API')]
    assert get(b,'/invitations/incoming').json()['items']==[]
    payload={'invitation_id':inv['id'],'consent_version':CONSENT,'share_summary':True}
    r=post(b,'/invitations/accept',{**payload,'consumer_org_id':b_org});assert r.status_code==403 and r.json()['error']['code']=='RECIPIENT_MISMATCH'
    assert post(p,'/invitations/accept',{**payload,'consumer_org_id':a_org}).status_code==403
    r=post(a,'/invitations/accept',{**payload,'consumer_org_id':a_org});assert r.status_code==200,r.text
    assert get(a,'/invitations/incoming').json()['items']==[]
    consumers=get(p,f"/api-projects/{pr['id']}/consumers").json()['items']
    assert [(x['recipient_email'],x['enrollment_status']) for x in consumers]==[('alice@consumer-a.test','active')]

def test_smtp_notification_has_no_acceptance_link(tmp_path,monkeypatch):
    sent=[]
    class FakeSMTP:
        def __init__(self,host,port,timeout): self.host,self.port=host,port
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def starttls(self,context): self.tls=True
        def login(self,username,password): self.credentials=(username,password)
        def send_message(self,message): sent.append((self,message))
    monkeypatch.setattr('smtplib.SMTP',FakeSMTP)
    for key,value in {'HOST':'smtp.example.test','PORT':'587','USERNAME':'mailer','PASSWORD':'secret','FROM':'Shockwave <noreply@example.test>'}.items():
        monkeypatch.setenv('SHOCKWAVE_SMTP_'+key,value)
    monkeypatch.setenv('SHOCKWAVE_APP_URL','https://shockwave.example.test')
    app=create_app(tmp_path/'smtp.db',demo=True)
    p,_=account(app,'owner@provider.test','provider','Payments Co')
    c,_=account(app,'alice@consumer.test','consumer','Consumer A')
    pr=project(p)
    inv=post(p,f"/api-projects/{pr['id']}/invitations",{'recipient_email':'alice@consumer.test'}).json()
    assert inv['delivery_status']=='sent' and get(p,'/capabilities').json()['email']=='smtp'
    (server,message),=sent
    assert server.tls and server.credentials==('mailer','secret') and message['To']=='alice@consumer.test'
    body=message.get_content()
    assert 'Payments Co invited alice@consumer.test to enroll in Payments API' in body
    assert 'https://shockwave.example.test' in body and '?invite=' not in body and inv['id'] not in body
    assert get(c,'/demo/inbox').json()['items']==[] and len(get(c,'/invitations/incoming').json()['items'])==1

def test_accounts_work_without_demo_mode(tmp_path):
    app=create_app(tmp_path/'prod.db',demo=False)
    assert get(TestClient(app),'/health').json()['demo_sessions'] is False
    p,_=account(app,'owner@provider.test','provider','Payments Co')
    assert get(p,'/session').json()['organizations'][0]['name']=='Payments Co'
    assert post(TestClient(app),'/demo/session',{'persona':'provider'}).status_code==404
