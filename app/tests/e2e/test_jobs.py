"""Persistent queue behavior across processes and existing database upgrades."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from backend.jobs import JobQueue
from backend.outbox import Outbox
from backend.store import Store, now, uid


def queue_with_job(tmp_path):
    store=Store(tmp_path/'jobs.db')
    queue=JobQueue(store,lease_seconds=30)
    with store.tx() as db:
        db.execute("INSERT INTO organizations VALUES('org','Example','consumer')")
        job={'id':uid(),'owner_org_id':'org','kind':'scan','status':'queued','stage':'queued',
             'created_at':now(),'updated_at':now(),'result_id':None,'error':None}
        queue.enqueue(db,job)
    return store,queue,job


def test_second_store_keeps_queued_job(tmp_path):
    store,queue,job=queue_with_job(tmp_path)
    restarted=Store(store.path)
    with restarted.read() as db:
        assert restarted.get(db,job['id'],'job')['status']=='queued'
    assert JobQueue(restarted).claim(job['id']) is not None


def test_atomic_claim_and_stale_worker_cannot_finish(tmp_path):
    store,queue,job=queue_with_job(tmp_path)
    barrier=Barrier(2)
    def attempt():
        barrier.wait()
        return queue.claim(job['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda _:attempt(),range(2)))
    assert sum(x is not None for x in claims)==1
    first=next(x for x in claims if x)
    with store.tx() as db:
        db.execute("UPDATE job_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",(job['id'],))
    second=queue.claim(job['id'])
    assert second and second.generation>first.generation
    assert not queue.finish(first,lambda db:None,'old')
    assert queue.finish(second,lambda db:None,'new')
    with store.read() as db:
        assert store.get(db,job['id'],'job')['result_id']=='new'
        attempts=db.execute('SELECT status FROM job_attempts WHERE job_id=? ORDER BY generation',(job['id'],)).fetchall()
        assert [row['status'] for row in attempts]==['abandoned','succeeded']


def test_cancelled_job_cannot_commit(tmp_path):
    store,queue,job=queue_with_job(tmp_path)
    claim=queue.claim(job['id'])
    with store.tx() as db:
        queue.cancel(db,job['id'])
    assert not queue.finish(claim,lambda db:None,'forbidden')
    assert not queue.heartbeat(claim)
    with store.read() as db:
        assert db.execute('SELECT status FROM job_attempts WHERE job_id=?',(job['id'],)).fetchone()[0]=='cancelled'


def test_reads_do_not_reserve_writer(tmp_path):
    store,queue,job=queue_with_job(tmp_path)
    with store.read() as db:
        assert store.get(db,job['id'])
        with store.tx() as other:
            other.execute('UPDATE organizations SET name=? WHERE id=?',('Updated','org'))


def test_upgrade_keeps_old_records(tmp_path):
    path=tmp_path/'legacy.db'
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE organizations(id TEXT PRIMARY KEY,name TEXT,kind TEXT)')
    db.execute('CREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT,org_id TEXT,parent_id TEXT,payload TEXT)')
    db.execute("INSERT INTO organizations VALUES('org','Previous','consumer')")
    old={'id':'previous-job','owner_org_id':'org','kind':'scan','status':'queued','stage':'queued',
         'created_at':now(),'updated_at':now(),'result_id':None,'error':None}
    db.execute('INSERT INTO records VALUES(?,?,?,?,?)',(old['id'],'job','org',None,json.dumps(old)))
    db.commit();db.close()
    store=Store(path)
    with store.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==7
        assert store.get(db,old['id'],'job')==old
    assert JobQueue(store).claim(old['id'])


def test_worker_claims_jobs_after_completed_history(tmp_path):
    store=Store(tmp_path/'backlog.db');queue=JobQueue(store)
    with store.tx() as db:
        db.execute("INSERT INTO organizations VALUES('org','Example','consumer')")
        for index in range(101):
            job={'id':uid(),'owner_org_id':'org','kind':'scan',
                 'status':'succeeded' if index<100 else 'queued',
                 'stage':'completed' if index<100 else 'queued',
                 'created_at':now(),'updated_at':now(),'result_id':None,'error':None}
            queue.enqueue(db,job)
            if index==100: queued_id=job['id']
    assert queue.claim().job_id==queued_id


def test_ambiguous_delivery_is_not_retried(tmp_path):
    store=Store(tmp_path/'outbox.db');sent=[]
    class UncertainTransport:
        def send_invitation(self,*args):
            sent.append(args)
            raise RuntimeError('Connection lost after send')
    outbox=Outbox(store,UncertainTransport,True)
    with store.tx() as db:
        db.execute("INSERT INTO organizations VALUES('org','Provider','provider')")
        invitation={'id':'invite','api_project_id':'project','recipient_email':'person@example.test',
                    'status':'pending','delivery_status':'pending'}
        store.put(db,'invitation','org',invitation)
        op=outbox.queue_invitation(db,invitation,{'provider_name':'Provider','api_name':'API'},'invite-operation')
    assert not outbox.dispatch(op)
    assert not outbox.dispatch(op)
    assert len(sent)==1
    with store.read() as db:
        assert db.execute('SELECT status FROM outbox WHERE id=?',(op,)).fetchone()[0]=='needs_reconciliation'


def test_crashed_dispatch_is_reconciled_without_resend(tmp_path):
    store=Store(tmp_path/'outbox-crash.db');sent=[]
    class Transport:
        def send_invitation(self,*args):
            sent.append(args)
    outbox=Outbox(store,Transport,True)
    with store.tx() as db:
        db.execute("INSERT INTO organizations VALUES('org','Provider','provider')")
        invitation={'id':'invite','recipient_email':'person@example.test','status':'pending'}
        store.put(db,'invitation','org',invitation)
        op=outbox.queue_invitation(db,invitation,{'provider_name':'Provider','api_name':'API'},'stable-operation')
        db.execute("UPDATE outbox SET status='dispatching',updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",(op,))
    outbox.reconcile_abandoned()
    assert not outbox.dispatch(op)
    assert sent==[]
    with store.read() as db:
        assert db.execute('SELECT status FROM outbox WHERE id=?',(op,)).fetchone()[0]=='needs_reconciliation'
