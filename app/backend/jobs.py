"""SQLite job claims with leases and fencing for independent worker processes."""
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.store import now


def after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@dataclass(frozen=True)
class Claim:
    job_id: str
    owner: str
    generation: int


class JobQueue:
    def __init__(self, store, lease_seconds=120, max_attempts=3):
        self.store = store
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def enqueue(self, db, job):
        self.store.put(db, 'job', job['owner_org_id'], job)
        db.execute('INSERT INTO job_leases(job_id) VALUES(?)', (job['id'],))

    def claim(self, job_id=None):
        with self.store.tx() as db:
            q = """SELECT l.*, r.payload FROM job_leases l JOIN records r ON r.id=l.job_id
                WHERE r.kind='job' AND l.cancel_requested=0
                AND json_extract(r.payload,'$.status') IN ('queued','running')
                AND (json_extract(r.payload,'$.status')='queued' OR l.expires_at IS NULL OR l.expires_at<=?)"""
            args = [now()]
            if job_id:
                q += ' AND l.job_id=?'; args.append(job_id)
            q += ' ORDER BY r.rowid LIMIT 100'
            for row in db.execute(q, args):
                job = json.loads(row['payload'])
                if job['status'] not in ('queued', 'running') or row['cancel_requested']:
                    continue
                if job['status'] == 'running' and row['expires_at'] and row['expires_at'] > now():
                    continue
                if row['attempts'] >= self.max_attempts:
                    if job['status']=='running':
                        db.execute("UPDATE job_attempts SET ended_at=?,status='abandoned',error_code='LEASE_EXPIRED' WHERE job_id=? AND generation=? AND ended_at IS NULL",
                                   (now(),job['id'],row['generation']))
                    job.update(status='failed', stage='attempts_exhausted', updated_at=now(),
                               error={'code':'ATTEMPTS_EXHAUSTED','message':'Worker attempts exhausted; review the operation before retrying.','retryable':False})
                    self.store.update(db, job)
                    continue
                owner = str(uuid4())
                generation = row['generation'] + 1
                if job['status']=='running':
                    db.execute("UPDATE job_attempts SET ended_at=?,status='abandoned',error_code='LEASE_EXPIRED' WHERE job_id=? AND generation=? AND ended_at IS NULL",
                               (now(),job['id'],row['generation']))
                db.execute('UPDATE job_leases SET owner=?,generation=?,expires_at=?,attempts=attempts+1 WHERE job_id=?',
                           (owner, generation, after(self.lease_seconds), job['id']))
                db.execute('INSERT INTO job_attempts VALUES(?,?,?,?,?,?)',
                           (job['id'],generation,now(),None,'running',None))
                job.update(status='running', stage='preparing', updated_at=now(), error=None)
                self.store.update(db, job)
                return Claim(job['id'], owner, generation)
        return None

    def owns(self, db, claim):
        row = db.execute('SELECT owner,generation,expires_at,cancel_requested FROM job_leases WHERE job_id=?', (claim.job_id,)).fetchone()
        job = self.store.get(db, claim.job_id, 'job')
        return bool(row and job and job['status']=='running' and row['owner']==claim.owner
                    and row['generation']==claim.generation and not row['cancel_requested']
                    and row['expires_at'] and row['expires_at']>now())

    def heartbeat(self, claim):
        with self.store.tx() as db:
            if not self.owns(db, claim): return False
            db.execute('UPDATE job_leases SET expires_at=? WHERE job_id=?', (after(self.lease_seconds), claim.job_id))
            return True

    def stage(self, claim, value):
        with self.store.tx() as db:
            if not self.owns(db, claim): return False
            job = self.store.get(db, claim.job_id, 'job')
            job.update(stage=value, updated_at=now())
            self.store.update(db, job)
            return True

    def finish(self, claim, persist, result_id):
        with self.store.tx() as db:
            if not self.owns(db, claim): return False
            persist(db)
            job = self.store.get(db, claim.job_id, 'job')
            job.update(status='succeeded',stage='completed',result_id=result_id,updated_at=now())
            self.store.update(db, job)
            db.execute("UPDATE job_attempts SET ended_at=?,status='succeeded' WHERE job_id=? AND generation=? AND ended_at IS NULL",
                       (now(),claim.job_id,claim.generation))
            db.execute('UPDATE job_leases SET owner=NULL,expires_at=NULL WHERE job_id=?', (claim.job_id,))
            return True

    def fail(self, claim, code='ADAPTER_FAILED', retryable=True):
        with self.store.tx() as db:
            if not self.owns(db, claim): return False
            job = self.store.get(db, claim.job_id, 'job')
            message=('Stripe checkout dependencies are missing. Run `npm ci --ignore-scripts --prefix '
                     'demo/templates/checkout` from the repository root, then retry.'
                     if code=='NODE_DEPENDENCIES_MISSING' else
                     'These results were already recorded differently, usually by an older version of Shockwave. '
                     'Stop the website, delete app/shockwave.db and app/.shockwave-integrated, then start it again.'
                     if code=='RESULT_ID_CONFLICT' else
                     'Operation failed. Check its prerequisites and retry if appropriate.')
            job.update(status='failed',stage='failed',updated_at=now(),error={
                'code':code,'message':message,
                'retryable':retryable})
            self.store.update(db, job)
            db.execute("UPDATE job_attempts SET ended_at=?,status='failed',error_code=? WHERE job_id=? AND generation=? AND ended_at IS NULL",
                       (now(),code,claim.job_id,claim.generation))
            db.execute('UPDATE job_leases SET owner=NULL,expires_at=NULL,last_error_code=? WHERE job_id=?', (code,claim.job_id))
            return True

    def cancel(self, db, job_id):
        db.execute('UPDATE job_leases SET cancel_requested=1 WHERE job_id=?', (job_id,))
        db.execute("UPDATE job_attempts SET ended_at=?,status='cancelled' WHERE job_id=? AND ended_at IS NULL",(now(),job_id))


def run_forever(queue, process, poll_seconds=1):
    while True:
        claim = queue.claim()
        if claim:
            process(claim)
        else:
            time.sleep(poll_seconds)
