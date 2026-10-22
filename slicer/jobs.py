"""The slicing queue and the slicing workers, kept in SQLite (slicer.sqlite3 in the data folder).

Job states: queued → assigned → preparing → slicing → processing → completed, or failed / cancelled.

- **Claiming:** a worker asks for work (claim()); the oldest queued job goes to it with a lease: a random token and an
  expiry LEASE seconds away. The worker's heartbeats and progress reports renew it.
- **A worker that disappears:** expire() puts a job whose lease ran out back in the queue (or fails it after
  `max_attempts` tries), so another worker picks it up. It runs on every claim and every few seconds, and after a
  restart of the Pi, whose database still has the leases.
- **Only once:** progress, results and failures are only accepted with the job's current lease token, so a worker that
  lost its lease (or a duplicate) can't complete a job another worker now has.
- **Workers:** each has a secret token (only its hash is stored), a concurrency limit, and can be turned off or
  drained (it finishes its jobs but takes no new ones).
"""
import hashlib
import json
import secrets
import sqlite3
import threading
import time

ACTIVE = ('assigned', 'preparing', 'slicing', 'processing')
DONE = ('completed', 'failed', 'cancelled')
STAGES = ('preparing', 'slicing', 'processing')
LEASE = 90           # seconds a job stays a worker's without a heartbeat or progress report
OFFLINE = 45         # seconds without a heartbeat before a worker shows as offline
ENROLL_VALID = 24 * 3600
MAX_ATTEMPTS = 3


def token_hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


class Jobs:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS slice_jobs (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL,
              request TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
              worker TEXT, lease TEXT, lease_until REAL, stage_note TEXT NOT NULL DEFAULT '', progress REAL,
              error TEXT NOT NULL DEFAULT '', log TEXT NOT NULL DEFAULT '', result TEXT, stats TEXT NOT NULL DEFAULT '{}',
              finished REAL);
            CREATE INDEX IF NOT EXISTS slice_jobs_status ON slice_jobs(status, created);
            CREATE TABLE IF NOT EXISTS slice_workers (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL, info TEXT NOT NULL DEFAULT '{}',
              enabled INTEGER NOT NULL DEFAULT 1, draining INTEGER NOT NULL DEFAULT 0, concurrency INTEGER NOT NULL DEFAULT 1,
              created REAL NOT NULL, last_seen REAL, catalog_version TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS slice_enroll (
              token TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL, used REAL);
            CREATE TABLE IF NOT EXISTS slice_presets (
              name TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS slice_catalog (
              version TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);
        ''')
        self.db.commit()

    # ---- jobs ----

    def add(self, owner, label, request, max_attempts=MAX_ATTEMPTS):
        job_id, now = secrets.token_hex(8), self.clock()
        with self.lock, self.db:
            self.db.execute('INSERT INTO slice_jobs(id,owner,label,status,request,created,updated,max_attempts) VALUES(?,?,?,?,?,?,?,?)',
                            (job_id, owner, label[:120], 'queued', json.dumps(request), now, now, max_attempts))
        return self.get(job_id)

    def get(self, job_id):
        row = self.db.execute('SELECT * FROM slice_jobs WHERE id=?', (str(job_id),)).fetchone()
        if not row:
            raise ValueError('Slicing job not found.')
        job = dict(row)
        job['request'], job['stats'] = json.loads(job['request']), json.loads(job['stats'] or '{}')
        return job

    def list(self, limit=50):
        rows = self.db.execute('SELECT id FROM slice_jobs ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        return [self.get(r['id']) for r in rows]

    def public(self, job):
        """A job as the dashboard shows it (no lease token, no worker secrets)."""
        return {k: v for k, v in job.items() if k not in ('lease', 'request', 'log', 'result')} | dict(
            settings=job['request'].get('settings', {}), objects=[o.get('name') for o in job['request'].get('objects', [])],
            has_result=bool(job.get('result')), queue_position=self.position(job))

    def position(self, job):
        if job['status'] != 'queued':
            return None
        return self.db.execute("SELECT COUNT(*) FROM slice_jobs WHERE status='queued' AND created<?", (job['created'],)).fetchone()[0] + 1

    def cancel(self, job_id):
        job = self.get(job_id)
        if job['status'] in DONE:
            raise ValueError(f"This job is already {job['status']}.")
        with self.lock, self.db:
            self.db.execute("UPDATE slice_jobs SET status='cancelled',lease=NULL,lease_until=NULL,finished=?,updated=? WHERE id=?",
                            (self.clock(), self.clock(), job['id']))
        return self.get(job_id)

    def expire(self):
        """Jobs whose worker stopped answering go back in the queue, or fail after their last try. Returns their ids."""
        now, changed = self.clock(), []
        with self.lock, self.db:
            rows = self.db.execute(f"SELECT id,attempts,max_attempts,worker FROM slice_jobs WHERE status IN ({','.join('?' * len(ACTIVE))}) AND lease_until<?",
                                   (*ACTIVE, now)).fetchall()
            for row in rows:
                if row['attempts'] >= row['max_attempts']:
                    self.db.execute("UPDATE slice_jobs SET status='failed',lease=NULL,lease_until=NULL,finished=?,updated=?,"
                                    "error=? WHERE id=?", (now, now, f"The worker stopped answering {row['attempts']} times; giving up.", row['id']))
                else:
                    self.db.execute("UPDATE slice_jobs SET status='queued',worker=NULL,lease=NULL,lease_until=NULL,progress=NULL,"
                                    "stage_note=?,updated=? WHERE id=?", (f"Back in the queue: worker {row['worker']} stopped answering.", now, row['id']))
                changed.append(row['id'])
        return changed

    def claim(self, worker_id):
        """The next queued job for this worker, with a new lease, or None (nothing queued, worker off, draining or full)."""
        self.expire()
        now = self.clock()
        with self.lock, self.db:
            worker = self.db.execute('SELECT * FROM slice_workers WHERE id=?', (worker_id,)).fetchone()
            if not worker or not worker['enabled'] or worker['draining']:
                return None
            running = self.db.execute(f"SELECT COUNT(*) FROM slice_jobs WHERE worker=? AND status IN ({','.join('?' * len(ACTIVE))})",
                                      (worker_id, *ACTIVE)).fetchone()[0]
            if running >= worker['concurrency']:
                return None
            row = self.db.execute("SELECT id FROM slice_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            lease = secrets.token_urlsafe(24)
            self.db.execute("UPDATE slice_jobs SET status='assigned',worker=?,lease=?,lease_until=?,attempts=attempts+1,progress=NULL,"
                            "stage_note='',error='',updated=? WHERE id=? AND status='queued'", (worker_id, lease, now + LEASE, now, row['id']))
        return self.get(row['id'])

    def held(self, job_id, worker_id, lease):
        """The job, if this worker still holds its lease; raises otherwise."""
        job = self.get(job_id)
        if job['status'] not in ACTIVE or job['worker'] != worker_id or not job['lease'] or not secrets.compare_digest(job['lease'], str(lease or '')):
            raise PermissionError('This job is no longer yours (it was cancelled, timed out or given to another worker).')
        return job

    def progress(self, job_id, worker_id, lease, stage, note='', percent=None):
        if stage not in STAGES:
            raise ValueError('Unknown stage.')
        if percent is not None and (type(percent) is bool or not isinstance(percent, (int, float)) or not 0 <= percent <= 100):
            raise ValueError('Progress must be 0-100.')
        self.held(job_id, worker_id, lease)
        now = self.clock()
        with self.lock, self.db:
            self.db.execute('UPDATE slice_jobs SET status=?,stage_note=?,progress=?,lease_until=?,updated=? WHERE id=? AND lease=?',
                            (stage, str(note)[:300], percent, now + LEASE, now, job_id, lease))

    def complete(self, job_id, worker_id, lease, result_path, stats, log=''):
        self.held(job_id, worker_id, lease)
        now = self.clock()
        with self.lock, self.db:
            done = self.db.execute("UPDATE slice_jobs SET status='completed',result=?,stats=?,log=?,progress=100,stage_note='',"
                                   "lease=NULL,lease_until=NULL,finished=?,updated=? WHERE id=? AND lease=?",
                                   (str(result_path), json.dumps(stats or {}), str(log)[-20000:], now, now, job_id, lease)).rowcount
        if not done:
            raise PermissionError('This job is no longer yours.')
        return self.get(job_id)

    def fail(self, job_id, worker_id, lease, error, log='', retry=False):
        """A worker couldn't slice it: back in the queue if `retry` (the worker's own trouble) and tries are left."""
        job = self.held(job_id, worker_id, lease)
        now = self.clock()
        again = retry and job['attempts'] < job['max_attempts']
        with self.lock, self.db:
            if again:
                self.db.execute("UPDATE slice_jobs SET status='queued',worker=NULL,lease=NULL,lease_until=NULL,progress=NULL,"
                                "stage_note=?,log=?,updated=? WHERE id=?", (f'Back in the queue: {str(error)[:200]}', str(log)[-20000:], now, job_id))
            else:
                self.db.execute("UPDATE slice_jobs SET status='failed',error=?,log=?,lease=NULL,lease_until=NULL,finished=?,updated=? WHERE id=?",
                                (str(error)[:1000], str(log)[-20000:], now, now, job_id))
        return self.get(job_id)

    def cancelled(self, worker_id, job_ids):
        """Of the jobs a worker says it's running, the ones it should stop (cancelled, or no longer its)."""
        out = []
        for job_id in (job_ids or [])[:64]:
            try:
                job = self.get(job_id)
            except ValueError:
                out.append(job_id)
                continue
            if job['status'] not in ACTIVE or job['worker'] != worker_id:
                out.append(job_id)
        return out

    # ---- workers ----

    def new_enrollment(self):
        token, now = secrets.token_urlsafe(24), self.clock()
        with self.lock, self.db:
            self.db.execute('DELETE FROM slice_enroll WHERE expires<?', (now,))
            self.db.execute('INSERT INTO slice_enroll(token,created,expires) VALUES(?,?,?)', (token_hash(token), now, now + ENROLL_VALID))
        return token, now + ENROLL_VALID

    def enroll(self, enrollment, name, info):
        """A new worker, with a one-time enrollment token from the dashboard. Returns (worker id, its secret token)."""
        now = self.clock()
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM slice_enroll WHERE token=?', (token_hash(enrollment),)).fetchone()
            if not row or row['used'] or row['expires'] < now:
                raise PermissionError('This worker setup code is wrong, used or expired. Make a new one in the Slicer tab.')
            self.db.execute('UPDATE slice_enroll SET used=? WHERE token=?', (now, row['token']))
            worker_id, secret = 'w-' + secrets.token_hex(4), secrets.token_urlsafe(32)
            label = str(name or worker_id).strip()[:60] or worker_id
            self.db.execute('INSERT INTO slice_workers(id,name,token,info,created,last_seen) VALUES(?,?,?,?,?,?)',
                            (worker_id, label, token_hash(secret), json.dumps(info or {})[:20000], now, now))
        return worker_id, secret

    def authenticate(self, worker_id, secret):
        row = self.db.execute('SELECT * FROM slice_workers WHERE id=?', (str(worker_id),)).fetchone()
        if not row or not secrets.compare_digest(row['token'], token_hash(secret)):
            raise PermissionError('Unknown worker or wrong token.')
        return dict(row)

    def heartbeat(self, worker_id, info):
        """Renews the leases of the jobs the worker says it's running; one it forgot (it restarted) expires as usual."""
        now, running = self.clock(), [str(j) for j in (info or {}).get('running') or [] if isinstance(j, str)][:64]
        with self.lock, self.db:
            self.db.execute('UPDATE slice_workers SET info=?,last_seen=? WHERE id=?', (json.dumps(info or {})[:20000], now, worker_id))
            if running:
                self.db.execute(f"UPDATE slice_jobs SET lease_until=? WHERE worker=? AND status IN ({','.join('?' * len(ACTIVE))}) "
                                f"AND id IN ({','.join('?' * len(running))})", (now + LEASE, worker_id, *ACTIVE, *running))
        return self.worker(worker_id)

    def worker(self, worker_id):
        row = self.db.execute('SELECT * FROM slice_workers WHERE id=?', (str(worker_id),)).fetchone()
        if not row:
            raise ValueError('Worker not found.')
        worker = {k: v for k, v in dict(row).items() if k != 'token'}
        worker['info'] = json.loads(worker['info'] or '{}')
        worker['online'] = bool(worker['last_seen'] and self.clock() - worker['last_seen'] < OFFLINE)
        worker['jobs'] = [r['id'] for r in self.db.execute(
            f"SELECT id FROM slice_jobs WHERE worker=? AND status IN ({','.join('?' * len(ACTIVE))})", (worker_id, *ACTIVE))]
        return worker

    def workers(self):
        return [self.worker(r['id']) for r in self.db.execute('SELECT id FROM slice_workers ORDER BY name')]

    def configure(self, worker_id, enabled=None, draining=None, concurrency=None):
        self.worker(worker_id)
        changes = {}
        for key, value in (('enabled', enabled), ('draining', draining)):
            if value is not None:
                if type(value) is not bool:
                    raise ValueError(f'{key} must be true or false.')
                changes[key] = int(value)
        if concurrency is not None:
            if type(concurrency) is not int or not 1 <= concurrency <= 8:
                raise ValueError('Jobs at once must be 1 to 8.')
            changes['concurrency'] = concurrency
        if changes:
            with self.lock, self.db:
                self.db.execute(f"UPDATE slice_workers SET {','.join(k + '=?' for k in changes)} WHERE id=?", (*changes.values(), worker_id))
        return self.worker(worker_id)

    def remove(self, worker_id):
        worker = self.worker(worker_id)
        if worker['jobs']:
            raise ValueError('This worker is slicing; drain it first, or wait.')
        with self.lock, self.db:
            self.db.execute('DELETE FROM slice_workers WHERE id=?', (worker_id,))

    # ---- saved settings ----

    def presets(self):
        return [dict(json.loads(r['data']), name=r['name']) for r in self.db.execute('SELECT name,data FROM slice_presets ORDER BY name COLLATE NOCASE')]

    def save_preset(self, name, data):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO slice_presets(name,data,updated) VALUES(?,?,?)', (name, json.dumps(data), self.clock()))

    def delete_preset(self, name):
        with self.lock, self.db:
            self.db.execute('DELETE FROM slice_presets WHERE name=?', (name,))

    # ---- presets ----

    def save_catalog(self, version, catalog):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO slice_catalog(version,data,updated) VALUES(?,?,?)', (str(version)[:40], json.dumps(catalog), self.clock()))

    def has_catalog(self, version):
        return self.db.execute('SELECT 1 FROM slice_catalog WHERE version=?', (str(version)[:40],)).fetchone() is not None

    def catalog(self):
        """The presets the newest worker reported, or None before any worker did."""
        row = self.db.execute('SELECT version,data FROM slice_catalog ORDER BY updated DESC LIMIT 1').fetchone()
        return (row['version'], json.loads(row['data'])) if row else (None, None)
