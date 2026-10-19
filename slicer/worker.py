"""Slicing worker: runs on each slicing computer, takes jobs from the Pi and slices them with OrcaSlicer's command line.

    python3 worker.py enroll --server http://pi:8080 --code SETUP-CODE   (once: registers with the Pi)
    python3 worker.py run                                                 (the service: systemd starts it)
    python3 worker.py test-slice                                          (slices a test cube, no Pi needed)

Each job: download the meshes, place each one as it was placed in the Slicer tab (meshes.py), write the flattened
presets with the changed settings (profiles.py), run OrcaSlicer in its own process group with a time limit, check the
sliced 3MF it wrote, and send it back with its statistics. The temporary folder is removed afterwards, also on failure.
Nothing from a job is ever run: OrcaSlicer only gets file paths, in an argument list (no shell).

The command line used (OrcaSlicer 2.4.2's documented options, src/libslic3r/PrintConfig.cpp CLIActionsConfigDef):
    orca-slicer --arrange 0 --slice 0 --load-settings "machine.json;process.json" --load-filaments filament.json
                --outputdir OUT --export-3mf result.3mf object_1.stl object_2.stl ...
--arrange 0 keeps the objects where they were placed (STL coordinates are bed coordinates in millimetres).

Standard library only (plus meshes.py and profiles.py next to it), so it runs on a plain Ubuntu / Debian server.
"""
import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meshes  # noqa: E402
import profiles  # noqa: E402

VERSION = '1.0.0'
CONFIG = Path(os.environ.get('ORCA_WORKER_CONFIG', '/etc/orca-worker/config.json'))
DEFAULTS = dict(
    server='', worker_id='', token='',
    orca=['/opt/orcaslicer/AppRun'], orca_version='', profiles='/opt/orcaslicer/resources/profiles', vendors=['BBL'],
    xvfb=True, work_dir='/var/lib/orca-worker', timeout=1800, poll=5, heartbeat=15, max_download=200 * 1024 * 1024)


def load_config(path=CONFIG):
    config = dict(DEFAULTS)
    try:
        config.update(json.loads(Path(path).read_text()))
    except FileNotFoundError:
        pass
    return config


def save_config(config, path=CONFIG):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, indent=2))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class SliceError(Exception):
    """A job that can't be sliced. retry=True: the trouble was this worker's (a crash, a timeout), so another try may work."""
    def __init__(self, message, log='', retry=False):
        super().__init__(message)
        self.log, self.retry = log, retry


class Lost(Exception):
    """The Pi says the job is no longer ours (cancelled, or given to another worker)."""


# ---- talking to the Pi ----

class Server:
    def __init__(self, config):
        self.base = config['server'].rstrip('/')
        self.worker_id, self.token = config['worker_id'], config['token']

    def request(self, method, path, body=None, headers=None, timeout=60, raw=False):
        data, extra = None, dict(headers or {})
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            extra.setdefault('Content-Type', 'application/octet-stream')
        elif body is not None:
            data = json.dumps(body).encode()
            extra['Content-Type'] = 'application/json'
        extra.update({'Authorization': f'Bearer {self.worker_id}:{self.token}', 'User-Agent': f'orca-worker/{VERSION}'})
        req = urllib.request.Request(self.base + path, data=data, headers=extra, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode('utf-8', 'replace')
            if exc.code == 409:
                raise Lost(detail) from None
            raise RuntimeError(f'Pi answered {exc.code}: {detail}') from None
        return payload if raw else json.loads(payload or b'{}')


def system_info(config, running=()):
    info = dict(hostname=socket.gethostname(), os=platform.platform(), python=platform.python_version(),
                cores=os.cpu_count(), worker_version=VERSION, orca_version=config.get('orca_version', ''), running=list(running))
    try:
        info['os'] = next(line.split('=', 1)[1].strip().strip('"') for line in Path('/etc/os-release').read_text().splitlines()
                          if line.startswith('PRETTY_NAME='))
    except (OSError, StopIteration):
        pass
    try:
        info['cpu'] = next(line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                           if line.startswith('model name'))
    except (OSError, StopIteration):
        info['cpu'] = platform.processor()
    try:
        memory = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                  if line.split(':')[0] in ('MemTotal', 'MemAvailable')}
        info['ram_total'], info['ram_free'] = memory.get('MemTotal'), memory.get('MemAvailable')
    except (OSError, ValueError, IndexError):
        pass
    try:
        info['load'] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    try:
        Path(config['work_dir']).mkdir(parents=True, exist_ok=True)
        info['disk_free'] = shutil.disk_usage(config['work_dir']).free
    except OSError:
        pass
    return info


# ---- slicing ----

def placed_meshes(job, fetch, folder, max_download):
    """Each object's mesh, placed on the bed with its transform, as object_N.stl in `folder`. fetch(index) -> bytes."""
    paths = []
    for index, obj in enumerate(job['objects']):
        data = fetch(index)
        if len(data) > max_download:
            raise SliceError(f"{obj.get('name', 'An object')} is too large to slice here.")
        try:
            values = meshes.transform(meshes.triangles(data), obj['matrix'])
        except meshes.MeshError as exc:
            raise SliceError(f"{obj.get('name', 'An object')}: {exc}") from None
        path = Path(folder) / f'object_{index + 1}.stl'
        with open(path, 'wb') as stream:
            meshes.write_binary_stl(values, stream, obj.get('name', 'object'))
        paths.append(path)
    return paths


def write_presets(job, config, folder):
    """machine.json, process.json, filament.json: the flattened presets with the job's changed settings."""
    library = profiles.Profiles(config['profiles'], config.get('vendors') or ['BBL'])
    settings, changes = job['settings'], job.get('changes') or {}
    try:
        files = {}
        for kind, name, changed in (('machine', settings['machine'], {}), ('process', settings['process'], changes.get('process')),
                                    ('filament', settings['filament'], changes.get('filament'))):
            path = Path(folder) / f'{kind}.json'
            path.write_text(json.dumps(library.resolved(kind, name, changed), indent=1))
            files[kind] = path
    except profiles.ProfileError as exc:
        raise SliceError(str(exc)) from None
    return files


def orca_command(config, presets, objects, out):
    command = list(config['orca']) + [
        '--arrange', '0', '--slice', '0',
        '--load-settings', f"{presets['machine']};{presets['process']}",
        '--load-filaments', str(presets['filament']),
        '--outputdir', str(out), '--export-3mf', 'result.3mf'] + [str(p) for p in objects]
    if config.get('xvfb') and shutil.which('xvfb-run'):
        command = ['xvfb-run', '-a', '--server-args=-screen 0 640x480x24'] + command
    return command


def run_orca(command, timeout, alive=lambda: True, report=lambda: None, cwd=None):
    """Run OrcaSlicer in its own process group; kill the whole group on a timeout or when alive() turns false.
    Returns (exit code, output)."""
    log = tempfile.TemporaryFile()
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd,
                               start_new_session=True, env=dict(os.environ, LC_ALL='C'))
    started, reason = time.monotonic(), ''
    try:
        while process.poll() is None:
            if time.monotonic() - started > timeout:
                reason = f'OrcaSlicer took longer than {timeout // 60} minutes.'
            elif not alive():
                reason = 'cancelled'
            if reason:
                _kill(process)
                break
            report()
            time.sleep(1)
        process.wait()
    finally:
        log.seek(0)
        output = log.read()[-200000:].decode('utf-8', 'replace')
        log.close()
    if reason == 'cancelled':
        raise Lost('cancelled')
    if reason:
        raise SliceError(reason, output, retry=True)
    return process.returncode, output


def _kill(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(10)
            return
        except subprocess.TimeoutExpired:
            continue


def orca_error(out, code, output):
    """What went wrong, from OrcaSlicer's result.json (its return_code and error_string) or its output."""
    try:
        result = json.loads((Path(out) / 'result.json').read_text())
        if result.get('return_code') not in (0, None) or result.get('error_string'):
            return f"OrcaSlicer: {result.get('error_string') or 'error'} (code {result.get('return_code')})"
    except (OSError, ValueError):
        pass
    lines = [line for line in output.splitlines() if 'error' in line.lower()][-3:]
    return f'OrcaSlicer stopped with exit code {code}' + (': ' + ' / '.join(lines)[:400] if lines else '.')


def sliced_result(out):
    """The sliced 3MF OrcaSlicer wrote, checked: a zip with plate 1's G-code. Raises SliceError otherwise."""
    candidates = sorted(Path(out).glob('*.3mf'))
    if not candidates:
        raise SliceError('OrcaSlicer finished but wrote no 3MF.')
    path = candidates[0]
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not any(n.startswith('Metadata/plate_') and n.endswith('.gcode') for n in names):
                raise SliceError('OrcaSlicer wrote a 3MF without G-code (the plate was not sliced).')
    except zipfile.BadZipFile:
        raise SliceError('OrcaSlicer wrote a damaged 3MF.') from None
    return path


def statistics(path):
    """Print time, filament and weight from the sliced 3MF's Metadata/slice_info.config."""
    import xml.etree.ElementTree as ElementTree
    stats = {}
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.read('Metadata/slice_info.config')
        root = ElementTree.fromstring(info)
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return stats
    plate = root.find('plate')
    if plate is None:
        return stats
    meta = {m.get('key'): m.get('value') for m in plate.findall('metadata')}
    for key, name, kind in (('prediction', 'seconds', int), ('weight', 'grams', float)):
        try:
            stats[name] = kind(float(meta[key]))
        except (KeyError, TypeError, ValueError):
            pass
    stats['filaments'] = []
    for filament in plate.findall('filament'):
        try:
            stats['filaments'].append(dict(type=filament.get('type', ''), color=filament.get('color', ''),
                                           meters=float(filament.get('used_m') or 0), grams=float(filament.get('used_g') or 0)))
        except ValueError:
            continue
    stats['objects'] = len(plate.findall('object'))
    warnings = [w.get('msg') for w in plate.findall('warning') if w.get('msg')]
    if warnings:
        stats['warnings'] = warnings[:10]
    return stats


def slice_job(job, config, fetch, progress, alive=lambda: True):
    """Slice a job in a temporary folder; returns (sliced 3MF bytes, statistics, log). The folder is always removed."""
    root = Path(config['work_dir'])
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='job-', dir=root))
    try:
        progress('preparing', 'Downloading and placing the models')
        objects = placed_meshes(job, fetch, folder, config['max_download'])
        presets = write_presets(job, config, folder)
        out = folder / 'out'
        out.mkdir()
        progress('slicing', 'OrcaSlicer is slicing')
        last = [time.monotonic()]

        def report():   # keeps the lease while OrcaSlicer runs
            if time.monotonic() - last[0] > 20:
                last[0] = time.monotonic()
                progress('slicing', 'OrcaSlicer is slicing')
        code, output = run_orca(orca_command(config, presets, objects, out), config['timeout'], alive, report, cwd=folder)
        if code != 0:
            raise SliceError(orca_error(out, code, output), output, retry=code < 0)
        progress('processing', 'Checking the sliced file')
        path = sliced_result(out)
        return path.read_bytes(), statistics(path), output[-20000:]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ---- the service ----

class Worker:
    def __init__(self, config):
        self.config, self.server = config, Server(config)
        self.running, self.stop, self.lock = {}, threading.Event(), threading.Lock()
        self.limit = 1

    def heartbeat(self):
        with self.lock:
            running = list(self.running)
        answer = self.server.request('POST', '/api/slicer/worker/heartbeat', system_info(self.config, running))
        self.limit = int(answer.get('concurrency') or 1) if answer.get('enabled', True) and not answer.get('draining') else 0
        for job_id in answer.get('stop') or []:
            with self.lock:
                if job_id in self.running:
                    self.running[job_id] = False   # the job's thread kills OrcaSlicer
        if answer.get('catalog_wanted'):
            self.send_catalog()

    def send_catalog(self):
        library = profiles.Profiles(self.config['profiles'], self.config.get('vendors') or ['BBL'])
        version = self.config.get('orca_version') or 'unknown'
        self.server.request('POST', '/api/slicer/worker/catalog', dict(version=version, catalog=library.catalog()), timeout=120)

    def work(self, job):
        job_id, lease = job['id'], job['lease']
        headers = {'X-Lease': lease}

        def fetch(index):
            return self.server.request('GET', f'/api/slicer/worker/jobs/{job_id}/models/{index}', headers=headers, raw=True, timeout=300)

        def progress(stage, note):
            self.server.request('POST', f'/api/slicer/worker/jobs/{job_id}/progress', dict(lease=lease, stage=stage, note=note))

        try:
            data, stats, log = slice_job(job, self.config, fetch, progress, alive=lambda: self.running.get(job_id, False))
            query = urllib.parse.urlencode(dict(lease=lease, stats=json.dumps(stats)))
            self.server.request('POST', f'/api/slicer/worker/jobs/{job_id}/result?{query}', data, timeout=600)
        except Lost:
            pass   # cancelled or reassigned: nothing to report
        except SliceError as exc:
            self._fail(job_id, lease, str(exc), exc.log, exc.retry)
        except Exception as exc:   # anything else is this worker's trouble: another worker may manage
            self._fail(job_id, lease, f'{type(exc).__name__}: {exc}', '', True)
        finally:
            with self.lock:
                self.running.pop(job_id, None)

    def _fail(self, job_id, lease, error, log, retry):
        try:
            self.server.request('POST', f'/api/slicer/worker/jobs/{job_id}/fail', dict(lease=lease, error=error[:1000], log=log[-20000:], retry=retry))
        except (Lost, RuntimeError, OSError):
            pass

    def loop(self):
        signal.signal(signal.SIGTERM, lambda *a: self.stop.set())
        next_beat = 0
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_beat:
                    self.heartbeat()
                    next_beat = time.monotonic() + self.config['heartbeat']
                with self.lock:
                    free = self.limit - len(self.running)
                if free > 0:
                    answer = self.server.request('POST', '/api/slicer/worker/claim', {})
                    job = answer.get('job')
                    if job:
                        with self.lock:
                            self.running[job['id']] = True
                        threading.Thread(target=self.work, args=(job,), daemon=True).start()
                        continue
            except (RuntimeError, OSError, ValueError) as exc:
                print(f'orca-worker: {exc}', file=sys.stderr, flush=True)
            self.stop.wait(self.config['poll'])
        with self.lock:   # stopping: OrcaSlicer is stopped; the Pi gives the jobs to another worker when their leases run out
            for job_id in self.running:
                self.running[job_id] = False


# ---- setup ----

def test_cube(size=20.0):
    """A closed cube's triangles (12), for the installer's test slice."""
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    values = []
    for face in faces:
        for i in face:
            values.extend(v[i])
    return values


def test_slice(config, machine='Bambu Lab A1 mini 0.4 nozzle'):
    """Slice a 20 mm cube with the printer's default process and filament: proves OrcaSlicer runs here."""
    import io
    library = profiles.Profiles(config['profiles'], config.get('vendors') or ['BBL'])
    flat = library.flat('machine', machine)
    stl = io.BytesIO()
    meshes.write_binary_stl(test_cube(), stl, 'test cube')
    middle = [sum(p[i] for p in profiles._area(flat.get('printable_area'))) / 4 - 10 for i in (0, 1)]
    job = dict(objects=[dict(name='test cube', matrix=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, middle[0], middle[1], 0, 1])],
               settings=dict(machine=machine, process=flat['default_print_profile'], filament=profiles._first(flat['default_filament_profile'])))
    data, stats, _ = slice_job(job, config, lambda i: stl.getvalue(), lambda stage, note: print(f'  {stage}: {note}'))
    return len(data), stats


def main(argv=None):
    parser = argparse.ArgumentParser(description='OrcaSlicer slicing worker for 3D Printer Management.')
    parser.add_argument('--config', default=str(CONFIG))
    sub = parser.add_subparsers(dest='action', required=True)
    enroll = sub.add_parser('enroll', help='register this computer with the Pi')
    enroll.add_argument('--server', required=True)
    enroll.add_argument('--code', required=True)
    enroll.add_argument('--name', default=socket.gethostname())
    enroll.add_argument('--orca-version', default='')
    sub.add_parser('run', help='take and slice jobs (the service)')
    sub.add_parser('test-slice', help='slice a test cube with OrcaSlicer')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.action == 'enroll':
        config.update(server=args.server.rstrip('/'), orca_version=args.orca_version or config.get('orca_version', ''))
        request = urllib.request.Request(config['server'] + '/api/slicer/worker/enroll', method='POST',
                                         data=json.dumps(dict(code=args.code, name=args.name, info=system_info(config))).encode(),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            sys.exit(f'Registering failed ({exc.code}): {exc.read()[:300].decode("utf-8", "replace")}')
        config.update(worker_id=answer['worker_id'], token=answer['token'])
        save_config(config, args.config)
        print(f"Registered as {answer['worker_id']}.")
    elif args.action == 'test-slice':
        size, stats = test_slice(config)
        print(f'Test slice OK: {size} bytes, {stats}')
    else:
        if not config.get('token'):
            sys.exit('Not registered yet: run "worker.py enroll" first.')
        Worker(config).loop()


if __name__ == '__main__':
    main()
