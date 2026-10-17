"""SQLite unit-of-work; each request/worker owns a separate transaction."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL


def uid(): return str(uuid4())
def stable(value): return str(uuid5(NAMESPACE_URL, 'shockwave:' + value))
def now(): return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.tx() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS organizations(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, verified INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS memberships(user_id TEXT REFERENCES users(id), org_id TEXT REFERENCES organizations(id), role TEXT NOT NULL, PRIMARY KEY(user_id,org_id));
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), expires TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, org_id TEXT NOT NULL REFERENCES organizations(id), parent_id TEXT REFERENCES records(id), payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS records_scope ON records(kind,org_id,parent_id);
            CREATE TABLE IF NOT EXISTS idempotency(scope TEXT, key TEXT, fingerprint TEXT NOT NULL, result_id TEXT REFERENCES records(id), PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS installation_states(token_hash TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), org_id TEXT REFERENCES organizations(id), expires TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS installations(org_id TEXT REFERENCES organizations(id), installation_id TEXT, user_id TEXT REFERENCES users(id), PRIMARY KEY(org_id,installation_id));
            CREATE TABLE IF NOT EXISTS enrollment_repositories(enrollment_id TEXT REFERENCES records(id), repository_id TEXT REFERENCES records(id), PRIMARY KEY(enrollment_id,repository_id));
            CREATE TABLE IF NOT EXISTS rate_limits(bucket TEXT PRIMARY KEY, count INTEGER, window TEXT);
            ''')
            for row in self.list(db, 'job'):
                if row['status'] in ('running','queued'):
                    row.update(status='failed', stage='interrupted', updated_at=now(), error={'code':'WORKER_INTERRUPTED','message':'Server restarted; retry the operation with a new idempotency key.','retryable':True})
                    self.update(db, row)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, db, kind, org, data, parent=None):
        db.execute('INSERT INTO records VALUES(?,?,?,?,?)', (data['id'],kind,org,parent,json.dumps(data)))
        return data

    def update(self, db, data):
        db.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(data),data['id']))
        return data

    def get(self, db, id, kind=None, org=None):
        q='SELECT payload FROM records WHERE id=?'; args=[id]
        if kind: q+=' AND kind=?'; args.append(kind)
        if org: q+=' AND org_id=?'; args.append(org)
        row=db.execute(q,args).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, db, kind, org=None, parent=None):
        q='SELECT payload FROM records WHERE kind=?'; args=[kind]
        if org: q+=' AND org_id=?'; args.append(org)
        if parent: q+=' AND parent_id=?'; args.append(parent)
        return [json.loads(row[0]) for row in db.execute(q,args)]
