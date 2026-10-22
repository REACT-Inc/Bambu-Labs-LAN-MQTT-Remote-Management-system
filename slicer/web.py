"""The Slicer tab's API, the slicing workers' API and the worker installer, on the dashboard's web server.

Dashboard (signed in, like the rest of /api/):
    GET  /api/slicer/catalog                 presets the workers have, the settings that can be changed, worker count
    POST /api/slicer/models                  upload an STL, OBJ or 3MF (checked, kept as STL by content hash); GET .../models/{id}
    GET/POST /api/slicer/presets, POST /api/slicer/presets/delete   saved settings ("My settings")
    GET  /api/slicer/jobs/{id}/gcode         the sliced plate's G-code, for the layer preview
    POST /api/slicer/jobs                    queue a slicing job: objects with transforms, presets, changed settings
    GET  /api/slicer/jobs, /api/slicer/jobs/{id}, POST /api/slicer/jobs/{id}/cancel
    GET  /api/slicer/jobs/{id}/result        download the sliced 3MF
    POST /api/slicer/jobs/{id}/queue         put the sliced 3MF in a printer's print queue (it still needs Start)
    GET  /api/slicer/events                  Server-Sent Events: the jobs and workers whenever they change
    GET/POST /api/slicer/workers...          worker administration and setup codes
Workers (Authorization: Bearer <worker id>:<token>; no dashboard session):
    POST /api/slicer/worker/enroll (setup code), heartbeat, catalog, claim
    GET  /api/slicer/worker/jobs/{id}/models/{n}    POST .../progress, .../result, .../fail   (with the job's lease)
Worker setup (no secrets in them; registering needs a setup code from the dashboard):
    GET  /slicer/worker/install.sh, /slicer/worker/bundle.tar.gz
"""
import asyncio
import hashlib
import io
import json
import re
import secrets
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from aiohttp import web

from slicer import jobs as jobs_module
from slicer import imports, meshes, settings as slicing_settings
from slicer.installer import install_script

HERE = Path(__file__).resolve().parent
MAX_OBJECTS = 50
MAX_RESULT = 512 * 1024 * 1024
ID = re.compile(r'[0-9a-f]{16}')
MODEL = re.compile(r'[0-9a-f]{64}')


class Slicer:
    def __init__(self, core, dashboard):
        self.core, self.dashboard = core, dashboard
        self.folder = Path(core.DATA_DIR) / 'slicer'
        self.models, self.results = self.folder / 'models', self.folder / 'results'
        for folder in (self.models, self.results):
            folder.mkdir(parents=True, exist_ok=True)
        self.jobs = jobs_module.Jobs(self.folder / 'slicer.sqlite3')
        self.task = None

    def routes(self):
        return [
            web.get('/api/slicer/catalog', self.catalog), web.post('/api/slicer/models', self.upload),
            web.get('/api/slicer/models/{id}', self.model),
            web.get('/api/slicer/presets', self.presets), web.post('/api/slicer/presets', self.save_preset),
            web.post('/api/slicer/presets/delete', self.delete_preset),
            web.get('/api/slicer/jobs/{id}/gcode', self.gcode),
            web.get('/api/slicer/jobs', self.list_jobs), web.post('/api/slicer/jobs', self.add_job),
            web.get('/api/slicer/jobs/{id}', self.job), web.post('/api/slicer/jobs/{id}/cancel', self.cancel),
            web.get('/api/slicer/jobs/{id}/result', self.result), web.post('/api/slicer/jobs/{id}/queue', self.to_queue),
            web.get('/api/slicer/events', self.events),
            web.get('/api/slicer/workers', self.workers), web.post('/api/slicer/workers/enroll', self.enrollment),
            web.post('/api/slicer/workers/{id}', self.configure), web.post('/api/slicer/workers/{id}/remove', self.remove),
            web.post('/api/slicer/worker/enroll', self.w_enroll), web.post('/api/slicer/worker/heartbeat', self.w_heartbeat),
            web.post('/api/slicer/worker/catalog', self.w_catalog), web.post('/api/slicer/worker/claim', self.w_claim),
            web.get('/api/slicer/worker/jobs/{id}/models/{index}', self.w_model),
            web.post('/api/slicer/worker/jobs/{id}/progress', self.w_progress),
            web.post('/api/slicer/worker/jobs/{id}/result', self.w_result), web.post('/api/slicer/worker/jobs/{id}/fail', self.w_fail),
            web.get('/slicer/worker/install.sh', self.installer), web.get('/slicer/worker/bundle.tar.gz', self.bundle),
        ]

    async def start(self, app=None):
        self.task = asyncio.create_task(self.expire_loop())

    async def stop(self, app=None):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def expire_loop(self):
        while True:   # jobs whose worker went quiet go back in the queue, also when no worker is asking for work
            try:
                for job_id in self.jobs.expire():
                    self.event('Slicing job back in the queue', job_id)
            except Exception:
                self.core.log.exception('Slicing queue check failed') if hasattr(self.core, 'log') else None
            await asyncio.sleep(10)

    def event(self, title, detail):
        store = getattr(self.dashboard, 'store', None)
        if store:
            store.event('', title, detail)

    # ---- the Slicer tab ----

    async def catalog(self, request):
        version, catalog = self.jobs.catalog()
        workers = self.jobs.workers()
        return web.json_response(dict(version=version, catalog=catalog, schema=slicing_settings.SCHEMA,
                                      workers=sum(1 for w in workers if w['online'] and w['enabled']),
                                      printers=[dict(name=n, model=self.printer_model(n)) for n in self.core.names()]))

    def printer_model(self, name):
        import printer_models
        model = printer_models.printer(self.core, name)
        return model['name'] if model['key'] else ''

    async def upload(self, request):
        """An STL, OBJ or 3MF: checked, and kept as one binary STL per object (by content hash). A 3MF can hold several."""
        reader = await request.multipart()
        part = await reader.next()
        name = Path((part.filename or '').replace('\\', '/')).name[:100] if part else ''
        if not part or part.name != 'file' or not name.lower().endswith(('.stl', '.obj', '.3mf')):
            raise ValueError('Choose an .stl, .obj or .3mf file.')
        if shutil.disk_usage(self.models).free < 1024 ** 3:
            raise ValueError('Less than 1 GB of free disk space on the Pi: free some space first.')
        data = bytearray()
        while chunk := await part.read_chunk(1 << 16):
            data += chunk
            if len(data) > meshes.MAX_BYTES:
                raise ValueError(f'The file is larger than {meshes.MAX_BYTES // 1048576} MB.')
        if name.lower().endswith('.stl'):   # kept as uploaded, read as a stream (no copy of every triangle in memory)
            info = await asyncio.to_thread(meshes.summary, bytes(data))
            return web.json_response(dict(models=[dict(info, id=self.keep(bytes(data)), name=name)]))
        objects = await asyncio.to_thread(imports.read, name, bytes(data))
        del data
        models = []
        for object_name, values in objects:
            stream = io.BytesIO()
            meshes.write_binary_stl(values, stream, object_name)
            models.append(dict(imports.summary(values), id=self.keep(stream.getvalue()), name=object_name if len(objects) > 1 else name))
        return web.json_response(dict(models=models))

    def keep(self, stl):
        model_id = hashlib.sha256(stl).hexdigest()
        path = self.models / f'{model_id}.stl'
        if not path.exists():
            temporary = path.with_suffix(f'.{secrets.token_hex(4)}.tmp')
            temporary.write_bytes(stl)
            temporary.replace(path)
        return model_id

    async def model(self, request):
        """A stored model as a binary STL, so the 3D view shows exactly what will be sliced."""
        model_id = request.match_info['id']
        if not MODEL.fullmatch(model_id) or not (self.models / f'{model_id}.stl').is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(self.models / f'{model_id}.stl', headers={'Content-Type': 'model/stl', 'Cache-Control': 'private, max-age=86400'})

    # ---- saved settings ("My settings") ----

    async def presets(self, request):
        return web.json_response(dict(presets=self.jobs.presets()))

    async def save_preset(self, request):
        data = await request.json()
        name = str(data.get('name') or '').strip()
        if not 1 <= len(name) <= 60:
            raise ValueError('Give the settings a name (up to 60 characters).')
        chosen = data.get('settings') if isinstance(data.get('settings'), dict) else {}
        changes = data.get('changes') if isinstance(data.get('changes'), dict) else {}
        slicing_settings.check(changes)   # only known settings, within their limits
        preset = dict(settings={k: str(chosen.get(k) or '')[:120] for k in ('machine', 'process', 'filament')}, changes=changes,
                      options=self.check_options(data.get('options')))
        if len(self.jobs.presets()) >= 100 and name not in {p['name'] for p in self.jobs.presets()}:
            raise ValueError('There are 100 saved settings already: delete some first.')
        self.jobs.save_preset(name, preset)
        self.event('Slicer settings saved', name)
        return web.json_response(dict(presets=self.jobs.presets()))

    async def delete_preset(self, request):
        data = await request.json()
        self.jobs.delete_preset(str(data.get('name') or ''))
        return web.json_response(dict(presets=self.jobs.presets()))

    @staticmethod
    def check_options(options):
        """How OrcaSlicer treats the plate: arrange the objects itself, and turn them to print better (both off by default)."""
        options = options if isinstance(options, dict) else {}
        for key in ('arrange', 'orient'):
            if key in options and type(options[key]) is not bool:
                raise ValueError(f'{key} must be true or false.')
        return dict(arrange=options.get('arrange') is True, orient=options.get('orient') is True)

    def check_request(self, data):
        """A job request from the Slicer tab, checked against the presets the workers have and the settings schema."""
        if not isinstance(data, dict):
            raise ValueError('Invalid request.')
        objects = data.get('objects')
        if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_OBJECTS:
            raise ValueError(f'Add 1 to {MAX_OBJECTS} objects to the plate.')
        clean = []
        for obj in objects:
            if not isinstance(obj, dict) or not MODEL.fullmatch(str(obj.get('model', ''))):
                raise ValueError('An object refers to an unknown model.')
            if not (self.models / f"{obj['model']}.stl").is_file():
                raise ValueError('A model is no longer on the Pi: upload it again.')
            clean.append(dict(model=obj['model'], name=str(obj.get('name') or 'object')[:100], matrix=meshes.check_matrix(obj.get('matrix'))))
        chosen = data.get('settings') or {}
        version, catalog = self.jobs.catalog()
        if not catalog:
            raise ValueError('No slicing worker has reported its presets yet: set up a worker first (Slicer → Workers).')
        machines = {m['name'] for m in catalog['machines']}
        machine = chosen.get('machine')
        if machine not in machines:
            raise ValueError('Choose a printer preset.')
        for kind, key in (('process', 'processes'), ('filament', 'filaments')):
            entry = next((p for p in catalog[key] if p['name'] == chosen.get(kind)), None)
            if not entry:
                raise ValueError(f'Choose a {kind} preset.')
            if machine not in entry['printers']:
                raise ValueError(f'The {kind} preset "{entry["name"]}" is not for the {machine}.')
        return dict(objects=clean, settings=dict(machine=machine, process=chosen['process'], filament=chosen['filament']),
                    changes=slicing_settings.check(data.get('changes')), options=self.check_options(data.get('options')), catalog_version=version)

    async def add_job(self, request):
        data = await request.json()
        clean = self.check_request(data)
        label = str(data.get('label') or clean['objects'][0]['name']).strip()[:120] or 'Plate'
        job = self.jobs.add(self.owner(request), label, clean)
        self.event('Slicing job queued', f"{label} • {job['id']} • {clean['settings']['machine']}")
        return web.json_response(self.jobs.public(job))

    def owner(self, request):
        return str((request.get('session') or {}).get('name') or 'web administrator')[:60]

    def job_id(self, request):
        job_id = request.match_info['id']
        if not ID.fullmatch(job_id):
            raise web.HTTPNotFound()
        return job_id

    async def list_jobs(self, request):
        return web.json_response(dict(jobs=[self.jobs.public(j) for j in self.jobs.list()]))

    async def job(self, request):
        job = self.jobs.get(self.job_id(request))
        return web.json_response(dict(self.jobs.public(job), log=job['log'][-6000:] if job['status'] == 'failed' else ''))

    async def cancel(self, request):
        job = self.jobs.cancel(self.job_id(request))
        self.event('Slicing job cancelled', f"{job['label']} • {job['id']}")
        return web.json_response(self.jobs.public(job))

    def result_path(self, job):
        if job['status'] != 'completed' or not job.get('result') or not Path(job['result']).is_file():
            raise ValueError('This job has no sliced file.')
        return Path(job['result'])

    async def result(self, request):
        job = self.jobs.get(self.job_id(request))
        path = self.result_path(job)
        name = re.sub(r'[^A-Za-z0-9_.-]+', '_', job['label']).strip('._') or 'plate'
        return web.FileResponse(path, headers={'Content-Disposition': f'attachment; filename="{name}.gcode.3mf"',
                                               'Content-Type': 'application/vnd.ms-package.3dmanufacturing-3dmodel+xml'})

    async def gcode(self, request):
        """The sliced plate's G-code, for the layer preview (compressed on the way: G-code shrinks about tenfold)."""
        job = self.jobs.get(self.job_id(request))
        path = self.result_path(job)
        response = web.StreamResponse(headers={'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'private, max-age=3600'})
        response.enable_compression()
        with zipfile.ZipFile(path) as archive:
            names = sorted(n for n in archive.namelist() if re.fullmatch(r'Metadata/plate_\d+\.gcode', n))
            if not names:
                raise ValueError('This sliced file has no G-code.')
            await response.prepare(request)
            with archive.open(names[0]) as stream:
                while chunk := await asyncio.to_thread(stream.read, 1 << 20):
                    await response.write(chunk)
        await response.write_eof()
        return response

    async def to_queue(self, request):
        """The sliced 3MF goes in a printer's print queue, as if it was uploaded there. It still needs Start."""
        job = self.jobs.get(self.job_id(request))
        path = self.result_path(job)
        data = await request.json()
        asset = secrets.token_hex(16)
        target = self.dashboard.uploads / f'{asset}.3mf'
        await asyncio.to_thread(shutil.copyfile, path, target)
        name = re.sub(r'[^A-Za-z0-9_. -]+', '_', job['label']).strip() or 'plate'
        target.with_suffix('.name').write_text(f'{name}.gcode.3mf')
        try:
            queued = self.dashboard.add(dict(printer=data.get('printer'), asset=asset, label=job['label'], plate=1,
                                             use_ams=data.get('use_ams', False), mapping=data.get('mapping', '')),
                                        self.owner(request))
        except BaseException:
            target.unlink(missing_ok=True)
            target.with_suffix('.name').unlink(missing_ok=True)
            raise
        self.event('Sliced plate sent to a print queue', f"{job['label']} • {job['id']} → {queued['printer']}")
        return web.json_response(dict(ok=True, job=queued['id'], printer=queued['printer']))

    def snapshot(self):
        return dict(jobs=[self.jobs.public(j) for j in self.jobs.list()], workers=self.jobs.workers())

    async def events(self, request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        await response.prepare(request)
        last, idle = None, 0
        try:
            for _ in range(1800):   # an hour; the browser reconnects by itself
                body = json.dumps(self.snapshot(), sort_keys=True)
                if body != last or idle >= 15:
                    await response.write(f'data: {body}\n\n'.encode())
                    last, idle = body, 0
                idle += 2
                await asyncio.sleep(2)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    # ---- worker administration ----

    async def workers(self, request):
        return web.json_response(dict(workers=self.jobs.workers()))

    async def enrollment(self, request):
        code, expires = self.jobs.new_enrollment()
        base = f'{request.scheme}://{request.host}'
        command = f'curl -fsSL {base}/slicer/worker/install.sh | sudo bash -s -- --server {base} --code {code}'
        self.event('Slicing worker setup code made', 'valid for 24 hours, once')
        return web.json_response(dict(code=code, expires=expires, command=command))

    async def configure(self, request):
        data = await request.json()
        worker = self.jobs.configure(request.match_info['id'], data.get('enabled'), data.get('draining'), data.get('concurrency'))
        self.event('Slicing worker changed', f"{worker['name']}: {json.dumps({k: data[k] for k in ('enabled', 'draining', 'concurrency') if k in data})}")
        return web.json_response(worker)

    async def remove(self, request):
        self.jobs.remove(request.match_info['id'])
        self.event('Slicing worker removed', request.match_info['id'])
        return web.json_response(dict(ok=True))

    # ---- the workers' API ----

    def worker_of(self, request):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer ') or ':' not in header:
            raise web.HTTPUnauthorized(text='Worker token missing.')
        worker_id, secret = header[7:].split(':', 1)
        try:
            return self.jobs.authenticate(worker_id, secret)
        except PermissionError:
            raise web.HTTPUnauthorized(text='Unknown worker or wrong token.') from None

    async def w_enroll(self, request):
        data = await request.json()
        try:
            worker_id, token = self.jobs.enroll(data.get('code'), data.get('name'), data.get('info'))
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from None
        self.event('Slicing worker registered', f"{data.get('name')} • {worker_id}")
        return web.json_response(dict(worker_id=worker_id, token=token))

    async def w_heartbeat(self, request):
        worker = self.worker_of(request)
        info = await request.json()
        current = self.jobs.heartbeat(worker['id'], info if isinstance(info, dict) else {})
        info = info if isinstance(info, dict) else {}
        return web.json_response(dict(enabled=bool(current['enabled']), draining=bool(current['draining']), concurrency=current['concurrency'],
                                      stop=self.jobs.cancelled(worker['id'], info.get('running')),
                                      catalog_wanted=not self.jobs.has_catalog(info.get('orca_version') or 'unknown')))

    async def w_catalog(self, request):
        worker = self.worker_of(request)
        data = await request.json()
        catalog = data.get('catalog') if isinstance(data, dict) else None
        if not isinstance(catalog, dict) or not all(isinstance(catalog.get(k), list) for k in ('machines', 'processes', 'filaments')):
            raise ValueError('Invalid preset catalog.')
        self.jobs.save_catalog(data.get('version') or 'unknown', catalog)
        self.event('Slicing presets updated', f"{worker['name']}: OrcaSlicer {data.get('version')}, {len(catalog['machines'])} printers")
        return web.json_response(dict(ok=True))

    async def w_claim(self, request):
        worker = self.worker_of(request)
        job = self.jobs.claim(worker['id'])
        if not job:
            return web.json_response(dict(job=None))
        self.event('Slicing job started', f"{job['label']} • {job['id']} on {worker['name']}")
        request_data = job['request']
        return web.json_response(dict(job=dict(id=job['id'], lease=job['lease'], label=job['label'], settings=request_data['settings'],
                                               changes=request_data['changes'], options=request_data.get('options') or {},
                                               objects=[dict(name=o['name'], matrix=o['matrix']) for o in request_data['objects']])))

    def held(self, request, worker, lease):
        try:
            return self.jobs.held(self.job_id(request), worker['id'], lease)
        except PermissionError as exc:
            raise web.HTTPConflict(text=str(exc)) from None

    async def w_model(self, request):
        worker = self.worker_of(request)
        job = self.held(request, worker, request.headers.get('X-Lease'))
        try:
            obj = job['request']['objects'][int(request.match_info['index'])]
        except (ValueError, IndexError):
            raise web.HTTPNotFound() from None
        return web.FileResponse(self.models / f"{obj['model']}.stl")

    async def w_progress(self, request):
        worker = self.worker_of(request)
        data = await request.json()
        self.held(request, worker, data.get('lease'))
        try:
            self.jobs.progress(self.job_id(request), worker['id'], data.get('lease'), data.get('stage'), data.get('note', ''), data.get('percent'))
        except PermissionError as exc:
            raise web.HTTPConflict(text=str(exc)) from None
        return web.json_response(dict(ok=True))

    async def w_result(self, request):
        worker = self.worker_of(request)
        lease = request.query.get('lease')
        job = self.held(request, worker, lease)
        try:
            stats = json.loads(request.query.get('stats') or '{}')
        except ValueError:
            stats = {}
        target = self.results / f"{job['id']}.3mf"
        temporary = target.with_suffix('.part')
        total = 0
        with temporary.open('wb') as stream:
            while chunk := await request.content.read(1 << 16):
                total += len(chunk)
                if total > MAX_RESULT:
                    temporary.unlink(missing_ok=True)
                    raise ValueError('The sliced file is too large.')
                stream.write(chunk)
        try:
            from queueing import validate_archive
            await asyncio.to_thread(validate_archive, temporary, 1)   # a real sliced plate: Metadata/plate_1.gcode
        except ValueError as exc:
            temporary.unlink(missing_ok=True)
            self.jobs.fail(job['id'], worker['id'], lease, f'The worker sent an invalid sliced file: {exc}', retry=True)
            raise
        temporary.replace(target)
        try:
            done = self.jobs.complete(job['id'], worker['id'], lease, target, stats if isinstance(stats, dict) else {})
        except PermissionError as exc:
            target.unlink(missing_ok=True)
            raise web.HTTPConflict(text=str(exc)) from None
        self.event('Slicing job finished', f"{done['label']} • {done['id']} on {worker['name']}")
        return web.json_response(dict(ok=True))

    async def w_fail(self, request):
        worker = self.worker_of(request)
        data = await request.json()
        self.held(request, worker, data.get('lease'))
        job = self.jobs.fail(self.job_id(request), worker['id'], data.get('lease'), str(data.get('error') or 'Slicing failed.'),
                             str(data.get('log') or ''), data.get('retry') is True)
        self.event('Slicing job back in the queue' if job['status'] == 'queued' else 'Slicing job failed',
                   f"{job['label']} • {job['id']} on {worker['name']}: {str(data.get('error'))[:200]}")
        return web.json_response(dict(ok=True, status=job['status']))

    # ---- setting up a worker ----

    async def installer(self, request):
        return web.Response(text=install_script(), content_type='text/x-shellscript')

    async def bundle(self, request):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for name in ('worker.py', 'meshes.py', 'profiles.py'):
                info = tarfile.TarInfo(name)
                data = (HERE / name).read_bytes()
                info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
                archive.addfile(info, io.BytesIO(data))
        return web.Response(body=buffer.getvalue(), content_type='application/gzip')
