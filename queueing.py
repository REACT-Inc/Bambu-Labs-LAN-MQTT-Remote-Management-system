"""One durable queue shared by Discord and HTTP. Starts require explicit approval; plate-swap completion requires a physical check."""
import asyncio
import json
import re
import sqlite3
import ssl
import time
import uuid
import zipfile
from pathlib import Path

ACTIVE = ('staging', 'awaiting_start', 'printing', 'paused', 'needs_review')
TERMINAL = ('finished', 'failed', 'cancelled')
MAX_UPLOAD = 256 * 1024 * 1024


def validate_archive(path, plate):
    if Path(path).stat().st_size > MAX_UPLOAD:
        raise ValueError('Maximum upload is 256 MiB.')
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(f'Metadata/plate_{plate}.gcode')
            if not 0 < info.file_size <= 2 * 1024**3:
                raise ValueError('Invalid or oversized plate G-code.')
    except (zipfile.BadZipFile, KeyError):
        raise ValueError('Upload a sliced Bambu/Orca .3mf containing the selected plate, not an STL or unsliced project.')


def options(plate=1, use_ams=False, mapping='', bed='textured_plate'):
    plate = int(plate)
    if not 1 <= plate <= 100:
        raise ValueError('Plate must be between 1 and 100.')
    if type(use_ams) is not bool:
        raise ValueError('Use AMS must be a boolean.')
    trays = [int(x.strip()) for x in str(mapping).split(',') if x.strip()]
    if len(trays) > 32 or any(x < 0 or x > 255 for x in trays):
        raise ValueError('Invalid AMS mapping.')
    if use_ams and not trays:
        raise ValueError('Provide an AMS mapping, for example 0 or 0,1.')
    if bed not in ('textured_plate', 'hot_plate', 'cool_plate', 'engineering_plate'):
        raise ValueError('Choose a supported bed type.')
    return dict(plate=plate, use_ams=use_ams, ams_mapping=trays, bed=bed)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, printer TEXT NOT NULL, label TEXT NOT NULL,
              asset TEXT, remote TEXT NOT NULL, options TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', position INTEGER NOT NULL,
              author TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
              note TEXT NOT NULL DEFAULT '', started REAL, seen_running INTEGER DEFAULT 0, demo INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active ON jobs(printer)
              WHERE status IN ('staging','awaiting_start','printing','paused','needs_review');
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
              printer TEXT, title TEXT NOT NULL, detail TEXT NOT NULL);
        ''')
        if 'demo' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN demo INTEGER NOT NULL DEFAULT 0')
        # A restart cannot prove whether a publish/print succeeded. Never retry it.
        self.db.execute("UPDATE jobs SET status='needs_review', note='Service restarted. Verify the physical printer and resolve this job before continuing.' WHERE status IN ('staging','awaiting_start','printing','paused')")
        self.db.commit()

    def event(self, printer, title, detail):
        with self.db:
            self.db.execute('INSERT INTO events(time,printer,title,detail) VALUES(?,?,?,?)', (time.time(), printer, title, str(detail)))
            self.db.execute('DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-2000 FROM events)')

    def events(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]

    def get(self, job_id):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Job not found.')
        data = dict(row)
        data['options'] = json.loads(data['options'])
        return data

    def jobs(self, printer=None):
        query = 'SELECT id FROM jobs'
        args = ()
        if printer:
            query += ' WHERE printer=?'
            args = (printer,)
        query += ' ORDER BY position,created'
        return [self.get(r['id']) for r in self.db.execute(query, args)]

    def active(self, printer):
        return next((j for j in self.jobs(printer) if j['status'] in ACTIVE), None)

    def add(self, printer, label, asset, remote, opts, author, demo=False):
        if not label.strip() or len(label) > 120:
            raise ValueError('Job name must be 1–120 characters.')
        if not asset and (not re.fullmatch(r'[A-Za-z0-9_./ -]+\.3mf', remote) or '..' in remote or remote.startswith('/')):
            raise ValueError('Use a relative printer file path ending in .3mf, such as cache/model.gcode.3mf.')
        job_id = uuid.uuid4().hex[:12]
        if asset:
            remote = f'pm_{job_id}.gcode.3mf'
        now = time.time()
        with self.db:
            position = self.db.execute('SELECT COALESCE(MAX(position),0)+1 FROM jobs').fetchone()[0]
            self.db.execute('INSERT INTO jobs(id,printer,label,asset,remote,options,position,author,created,updated,demo) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (job_id, printer, label.strip(), asset, remote, json.dumps(opts), position, author, now, now, int(demo)))
        self.event(printer, 'Job queued', f'{label} • {job_id} • {author}')
        return self.get(job_id)

    def set_status(self, job_id, status, note='', running=None):
        if status not in ACTIVE + TERMINAL + ('queued',):
            raise ValueError('Invalid job status.')
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,note=?,updated=? WHERE id=?', (status, note, time.time(), job_id))
            if running is not None:
                self.db.execute('UPDATE jobs SET seen_running=? WHERE id=?', (int(running), job_id))

    def claim(self, job_id, reserve=None):
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                job = self.get(job_id)
                if job['status'] != 'queued' or self.active(job['printer']):
                    raise ValueError('Printer already has an active or unresolved job, or this job was already used.')
                head = next(j for j in self.jobs(job['printer']) if j['status'] == 'queued')
                if head['id'] != job_id:
                    raise ValueError('Only the first queued job can start. Move this job first if needed.')
                self.db.execute("UPDATE jobs SET status='staging',started=?,updated=? WHERE id=?", (time.time(), time.time(), job_id))
                if reserve:reserve(job)
        except sqlite3.IntegrityError:
            raise ValueError('Another job is already active on this printer.')
        return self.get(job_id)

    def edit(self, job_id, action, author):
        job = self.get(job_id)
        if job['status'] != 'queued':
            raise ValueError('Only waiting jobs can be removed or reordered.')
        if action == 'remove':
            self.set_status(job_id, 'cancelled', f'Removed by {author}')
        elif action in ('up', 'down'):
            waiting = [j for j in self.jobs(job['printer']) if j['status'] == 'queued']
            index = next(i for i,j in enumerate(waiting) if j['id']==job_id)
            other = index + (-1 if action == 'up' else 1)
            if 0 <= other < len(waiting):
                with self.db:
                    self.db.execute('UPDATE jobs SET position=? WHERE id=?', (waiting[other]['position'], job_id))
                    self.db.execute('UPDATE jobs SET position=? WHERE id=?', (job['position'], waiting[other]['id']))
        else:
            raise ValueError('Unknown queue action.')
        self.event(job['printer'], 'Queue changed', f'{action} • {job_id} • {author}')


def upload(printer, source, remote):
    # BambuTools supplies implicit FTPS with TLS session reuse for port 990.
    from bambulabs_api.ftp_client import ImplicitFTP_TLS
    context = ssl._create_unverified_context()
    model=str(printer.get('model') or printer.get('name','')).lower().replace(' ','')
    unwrap=printer.get('ftp_tls_unwrap','h2d' in model)
    if type(unwrap) is not bool:raise ValueError('ftp_tls_unwrap must be true or false.')
    ftp = ImplicitFTP_TLS(timeout=60, context=context, unwrap=unwrap)
    sent=0;expected=Path(source).stat().st_size;deadline=time.monotonic()+240
    phase='connect'
    def progress(chunk):
        nonlocal sent
        sent+=len(chunk)
        if time.monotonic()>deadline:raise TimeoutError('Upload exceeded four minutes.')
    try:
        ftp.connect(printer['ip'], 990)
        ftp.login('bblp', printer['access_code'])
        ftp.prot_p()
        phase='transfer / TLS close'
        with open(source, 'rb') as stream:
            result = ftp.storbinary('STOR ' + remote, stream, blocksize=32768,callback=progress)
        if not result.startswith('226'):
            raise RuntimeError('Printer did not confirm file upload.')
        phase='verify remote size'
        ftp.voidcmd('TYPE I')
        if ftp.size(remote) != Path(source).stat().st_size:
            raise RuntimeError('Uploaded file size did not match.')
    except Exception as exc:
        detail=str(exc).replace(str(printer['access_code']),'[hidden]')
        raise RuntimeError(f'FTPS {phase}: {detail[:120]} • {sent}/{expected} bytes sent; file unverified, print not started.') from exc
    finally:
        ftp.close()


class Engine:
    def __init__(self, core, store):
        self.core, self.store = core, store
        self.locks = {}
        self.tasks = set()
        from plate_swap import PlateSwap
        self.plate_swap = PlateSwap(core,store)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def ready(self, printer, override_error=False):
        if getattr(self.core,'update_pending',lambda:False)():raise ValueError('Management update in progress.')
        state, error, data, connected = self.core.state_data(printer)
        if not connected or (not self.core.EXAMPLE_MODE and time.time() - self.core.last_seen.get(printer, 0) > 90):
            raise ValueError('Printer is offline or telemetry is stale. Wait for a fresh report.')
        allowed = ('IDLE', 'FINISH', 'FAILED') if override_error else ('IDLE', 'FINISH')
        if state not in allowed:
            raise ValueError(f'Printer reports {state}. Stop any current print and wait for an idle/finished/failed report.')
        if error and not override_error:
            raise ValueError(f'Printer reports error {error}. Inspect the printer, then use Start ignoring error if appropriate.')

    async def start(self, job_id, confirmed, author, override_error=False):
        if type(override_error) is not bool:
            raise ValueError("Error override must be a boolean.")
        if confirmed is not True:
            raise ValueError('Confirm the plate is clear and the file is sliced for this printer.')
        job = self.store.get(job_id)
        if bool(job['demo']) != bool(self.core.EXAMPLE_MODE):
            raise ValueError('This job belongs to a different live/demo mode. Add a new job in the current mode.')
        async with self.locks.setdefault(job['printer'], asyncio.Lock()):
            self.ready(job['printer'], override_error)
            self.plate_swap.check(job)
            job = self.store.claim(job_id, self.plate_swap.reserve)
            self.store.event(job['printer'], 'Start approved', f'{job_id} • {author}')
            if override_error:
                self.store.event(job['printer'], 'Error override approved', f'{job_id} • {author} • reported error/state bypassed for this attempt')
            self.spawn(self.dispatch(job, override_error))
        return self.store.get(job_id)

    async def dispatch(self, job, override_error=False):
        published = False
        try:
            if job['asset'] and not self.core.EXAMPLE_MODE:
                # Never start from inside the worker: a timed-out upload cannot later print.
                await asyncio.wait_for(asyncio.to_thread(upload, self.core.printer_config(job['printer']), job['asset'], job['remote']), 300)
            if self.store.get(job['id'])['status'] != 'staging':
                return  # Cancelled during upload; never publish afterward.
            self.ready(job['printer'], override_error)
            self.plate_swap.check(job,dispatch=True)
            self.store.set_status(job['id'], 'awaiting_start', 'Waiting for matching printer telemetry; do not resend.')
            if self.core.EXAMPLE_MODE:
                self.spawn(self.demo(job))
                return
            opts = job['options']
            payload = {'print': dict(command='project_file', sequence_id=str(time.time_ns()%1000000000),
                param=f"Metadata/plate_{opts['plate']}.gcode", file=job['remote'], url='ftp:///'+job['remote'],
                subtask_name=job['remote'], bed_leveling=True, bed_type=opts['bed'], flow_cali=True,
                vibration_cali=True, layer_inspect=False, use_ams=opts['use_ams'], ams_mapping=opts['ams_mapping'])}
            published = True
            result = self.core.clients[job['printer']].publish(f"device/{self.core.printer_config(job['printer'])['serial']}/request", json.dumps(payload), qos=1)
            if result.rc != 0:
                raise RuntimeError('MQTT send failed; verify printer state before resolving this job.')
            await self.core.notify(job['printer'], '📡 Print submitted', job['label']+' • waiting for printer confirmation', self.core.BLUE)
            await asyncio.sleep(120)
            if self.store.get(job['id'])['status'] == 'awaiting_start':
                self.store.set_status(job['id'], 'needs_review', 'No matching start confirmation. Inspect the printer; do not assume it did not start.')
                await self.core.notify(job['printer'], '⚠️ Queue needs review', job['label']+' • start was not confirmed', self.core.YELLOW)
        except Exception as error:
            if self.store.get(job['id'])['status'] == 'cancelled':
                return
            self.store.set_status(job['id'], 'needs_review' if published else 'failed', type(error).__name__ + ': ' + str(error)[:250])
            await self.core.notify(job['printer'], '⚠️ Queue job needs attention', job['label']+' • check queue details', self.core.RED)

    async def telemetry(self, printer, data):
        job = self.store.active(printer)
        if not job or job['status'] in ('staging','needs_review'):
            return
        state = data.get('gcode_state')
        identifiers = [str(data.get(k,'')) for k in ('gcode_file','subtask_name')]
        matched = any(Path(s).name in (Path(job['remote']).name, Path(job['remote']).stem) for s in identifiers if s)
        contradictory = any(s.lower().endswith('.3mf') and Path(s).name != Path(job['remote']).name for s in identifiers if s)
        if contradictory:
            matched = False
        if not matched:
            # Never assign another print's terminal state to this queue job.
            if job['seen_running'] and any(identifiers):
                self.store.set_status(job['id'], 'needs_review', 'Printer reported a different job. Verify manually.')
            return
        if state == 'RUNNING':
            self.store.set_status(job['id'], 'printing', running=True)
        elif state == 'PAUSE' and job['seen_running']:
            self.store.set_status(job['id'], 'paused', 'Printer paused; queue is held.')
        elif state in ('FINISH','FAILED') and job['seen_running']:
            self.store.set_status(job['id'], 'finished' if state=='FINISH' else 'failed')
            self.plate_swap.invalidate(printer)
            message = '. Swaplist batch ended. Check the magazine and starting setup with /plateswap check before starting another batch.' if self.plate_swap.state(printer)['enabled'] else '. Clear the plate before starting the next job.'
            await self.core.notify(printer, '📋 Queue updated', job['label']+' • '+state.lower()+message, self.core.GREEN if state=='FINISH' else self.core.RED)
        elif state == 'IDLE' and job['seen_running']:
            self.store.set_status(job['id'], 'needs_review', 'Print became idle without a confirmed outcome.')

    async def demo(self, job):
        name = job['printer']
        data = self.core.EXAMPLE_DATA[name]
        if hasattr(self.core, 'report_progress'):
            self.core.report_progress(name, dict(data, state='PREPARE'))
        data.update(state='RUNNING', subtask_name=job['remote'], mc_percent=0, total_layer_num=100)
        await self.core.notify(name, '🧪 Demo print started', job['label'], self.core.BLUE)
        for percent in range(0,101,10):
            while data['state']=='PAUSE':
                await asyncio.sleep(1)
            if data['state'] not in ('RUNNING','PAUSE'):
                return
            data.update(mc_percent=percent, layer_num=percent, mc_remaining_time=round((100-percent)/10))
            if hasattr(self.core, 'report_progress'):
                self.core.report_progress(name, data)
            await self.telemetry(name, dict(data, gcode_state='RUNNING'))
            await asyncio.sleep(2)
        data['state']='FINISH'
        if hasattr(self.core, 'progress_description'):
            await self.core.notify(name, '🧪 Print complete • 100%', self.core.progress_description(data), self.core.GREEN, True)
        await self.telemetry(name, dict(data, gcode_state='FINISH'))

    async def control(self, printer, action):
        if action in ('lighton', 'lightoff'):
            self.core.publish_light(printer, action == 'lighton')
            self.store.event(printer, 'Light command submitted', action)
            return
        if action not in ('pause','resume','stop'):
            raise ValueError('Unknown control.')
        active = self.store.active(printer)
        if action == 'stop' and active:
            if active['status'] == 'staging':
                self.store.set_status(active['id'], 'cancelled', 'Cancelled before submission.')
            elif active['status'] in ('awaiting_start', 'printing', 'paused'):
                self.store.set_status(active['id'], 'needs_review', 'Stop requested. Verify the printer before recording the outcome.')
        self.core.publish_action(printer, action)
        if self.core.EXAMPLE_MODE:
            data = self.core.EXAMPLE_DATA[printer]
            data['state'] = {'pause':'PAUSE','resume':'RUNNING','stop':'IDLE'}[action]
            await self.telemetry(printer, dict(data,gcode_state=data['state']))
        self.store.event(printer, 'Control submitted', action)

    def resolve(self, job_id, outcome, confirmed, author):
        job = self.store.get(job_id)
        if job['status'] != 'needs_review' or outcome not in TERMINAL or confirmed is not True:
            raise ValueError('Only review jobs can be resolved, after inspecting the printer.')
        self.store.set_status(job_id, outcome, f'Manually verified by {author}')
        self.store.event(job['printer'], 'Job resolved', f'{job_id} • {outcome} • {author}')
