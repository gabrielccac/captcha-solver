"""Encrypted job/session storage and a single browser claim across local processes."""
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet


class ServiceError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


class Store:
    def __init__(self, path, key, *, queue_limit=16, session_ttl=21600):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = Fernet(key)
        self.queue_limit, self.session_ttl = queue_limit, session_ttl
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT,
                    state TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
                    payload BLOB, session_id TEXT, code TEXT, timing TEXT,
                    UNIQUE(owner, idem));
                CREATE UNIQUE INDEX IF NOT EXISTS one_browser ON jobs((1)) WHERE state='running';
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL,
                    deadline REAL NOT NULL, version INTEGER NOT NULL, payload BLOB NOT NULL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode())

    def open(self, value):
        return json.loads(self.cipher.decrypt(value))

    @staticmethod
    def prune(db, now):
        db.execute("UPDATE jobs SET state='failed',code='job_expired',payload=NULL WHERE state='queued' AND deadline<?", (now,))
        db.execute("UPDATE jobs SET state='failed',code='worker_lost',payload=NULL WHERE state='running' AND deadline<?", (now,))
        db.execute('DELETE FROM sessions WHERE deadline<?', (now,))
        db.execute("DELETE FROM jobs WHERE state IN ('succeeded','failed') AND created<?", (now-86400,))

    def enqueue(self, owner, credentials, idem=None):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db, now)
            row = db.execute('SELECT id FROM jobs WHERE owner=? AND idem=?', (owner, idem)).fetchone() if idem else None
            if row:
                job_id = row['id']
            else:
                if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= self.queue_limit:
                    raise ServiceError('login_queue_full', 429)
                if db.execute("SELECT count(*) FROM jobs WHERE owner=? AND state IN ('queued','running')", (owner,)).fetchone()[0] >= 4:
                    raise ServiceError('client_queue_full', 429)
                job_id = secrets.token_urlsafe(24)
                db.execute("INSERT INTO jobs(id,owner,idem,state,created,deadline,payload) VALUES(?,?,?,'queued',?,?,?)",
                           (job_id, owner, idem, now, now+600, self.seal(credentials)))
        return self.job(owner, job_id)

    def job(self, owner, job_id):
        with self.db() as db:
            self.prune(db, time.time())
            row = db.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (job_id, owner)).fetchone()
            if not row:
                raise ServiceError('job_not_found', 404)
            position = db.execute("SELECT count(*) FROM jobs WHERE state='queued' AND (created<? OR (created=? AND id<=?))",
                                  (row['created'], row['created'], row['id'])).fetchone()[0] if row['state']=='queued' else None
            return {'job_id': row['id'], 'status': row['state'], 'created_at': row['created'],
                    'queue_position': position, 'session_id': row['session_id'], 'code': row['code'],
                    'timing': json.loads(row['timing']) if row['timing'] else None}

    def claim(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db, time.time())
            if db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            # A lost worker fails the job; a password is never retained for automatic replay.
            db.execute("UPDATE jobs SET state='running',deadline=?,payload=NULL WHERE id=?", (time.time()+300, row['id']))
            return row['id'], row['owner'], self.open(row['payload'])

    def finish(self, job_id, owner, *, bundle=None, code='ok', timing=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT 1 FROM jobs WHERE id=? AND owner=? AND state='running'", (job_id, owner)).fetchone()
            if not row:
                return
            session_id = None
            if bundle is not None:
                session_id = secrets.token_urlsafe(32)
                now = time.time()
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)',
                           (session_id, owner, now, now+self.session_ttl, 1, self.seal(bundle)))
            db.execute('UPDATE jobs SET state=?,session_id=?,code=?,timing=?,payload=NULL WHERE id=?',
                       ('succeeded' if bundle is not None else 'failed', session_id, code, json.dumps(timing), job_id))

    def session(self, owner, session_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=? AND owner=? AND deadline>?',
                             (session_id, owner, time.time())).fetchone()
            if not row:
                raise ServiceError('session_not_found', 404)
            return self.open(row['payload']), row['version'], row['deadline']

    def replace(self, owner, session_id, bundle, version):
        with self.db() as db:
            changed = db.execute('UPDATE sessions SET payload=?,version=version+1 WHERE id=? AND owner=? AND version=? AND deadline>?',
                                 (self.seal(bundle), session_id, owner, version, time.time())).rowcount
            if not changed:
                raise ServiceError('session_changed', 409)

    def close(self, owner, session_id):
        with self.db() as db:
            deleted = db.execute('DELETE FROM sessions WHERE id=? AND owner=?', (session_id, owner)).rowcount
            if not deleted:
                raise ServiceError('session_not_found', 404)
