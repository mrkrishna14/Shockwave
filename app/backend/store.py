"""SQLite persistence with short writes, concurrent reads, and additive migrations."""
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
        self.migrate()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=20000')
        return db

    def migrate(self):
        # user_version=0 includes the original platform database. These statements
        # preserve its rows and add worker coordination without resetting state.
        with self.tx() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 4:
                raise RuntimeError(f'Unsupported Shockwave database version {version}')
            if version < 1:
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
                db.execute('PRAGMA user_version=1')
            if version < 2:
                db.executescript('''
                CREATE TABLE IF NOT EXISTS job_leases(
                    job_id TEXT PRIMARY KEY REFERENCES records(id), owner TEXT, generation INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS job_leases_expiry ON job_leases(expires_at);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_org_id TEXT NOT NULL,
                    operation_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT, updated_at TEXT NOT NULL
                );
                ''')
                db.execute('PRAGMA user_version=2')
            if version < 3:
                columns={r['name'] for r in db.execute('PRAGMA table_info(enrollment_repositories)')}
                if 'allow_model_processing' not in columns:
                    db.execute('ALTER TABLE enrollment_repositories ADD COLUMN allow_model_processing INTEGER NOT NULL DEFAULT 0')
                    db.execute('ALTER TABLE enrollment_repositories ADD COLUMN allow_pr_write INTEGER NOT NULL DEFAULT 0')
                    db.execute('ALTER TABLE enrollment_repositories ADD COLUMN scan_consent_at TEXT')
                # Earlier versions stored the consent on the connection. Preserve
                # previously granted access only for mappings already selected.
                for row in db.execute('SELECT enrollment_id,repository_id FROM enrollment_repositories'):
                    repo=self.get(db,row['repository_id'],'repository')
                    if repo:
                        db.execute('''UPDATE enrollment_repositories SET allow_model_processing=?,
                            allow_pr_write=?,scan_consent_at=? WHERE enrollment_id=? AND repository_id=?''',
                            (int(repo['allow_model_processing']),int(repo['allow_pr_write']),repo['scan_consent_at'],
                             row['enrollment_id'],row['repository_id']))
                db.execute('PRAGMA user_version=3')
            if version < 4:
                db.executescript('''
                CREATE TABLE IF NOT EXISTS job_attempts(
                    job_id TEXT NOT NULL REFERENCES records(id), generation INTEGER NOT NULL,
                    started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
                    error_code TEXT, PRIMARY KEY(job_id,generation)
                );
                ''')
                db.execute('PRAGMA user_version=4')
            # Legacy jobs predate the lease table. They remain claimable after an
            # upgrade; an active lease from another worker is never discarded.
            for row in db.execute("SELECT id,payload FROM records WHERE kind='job'"):
                db.execute('INSERT OR IGNORE INTO job_leases(job_id) VALUES(?)',(row['id'],))

    @contextmanager
    def tx(self):
        """Write transaction. Never hold this while calling an adapter."""
        db = self.connect()
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

    @contextmanager
    def read(self):
        """Snapshot read that does not reserve SQLite's single writer slot."""
        db = self.connect()
        db.execute('BEGIN')
        try:
            yield db
            db.rollback()
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
        q+=' ORDER BY rowid ASC'
        return [json.loads(row[0]) for row in db.execute(q,args)]
