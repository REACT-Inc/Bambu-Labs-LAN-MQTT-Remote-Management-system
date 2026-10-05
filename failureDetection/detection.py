"""AI print-failure detection (#70): a .hef model on a Raspberry Pi 5 AI HAT, or a .onnx model on the CPU.
Off unless config.json enables it.

While a printer reports RUNNING, the monitor takes one still from its camera every `interval` seconds (the cheap
single-still path, reusing the dashboard's recent stills), has the model score it, and keeps the last `window`
scores. It only calls a print a failure when the evidence holds up over time:

- at least `needed` of the last `window` frames score at or above `threshold`,
- the failing frames span at least `min_minutes` (a few bad frames in a row aren't enough),
- the newest frame is still failing, and
- the print has been running for at least `warm_up` minutes (heat-up, purge and first layer look odd).

A frame identical to the previous one (camera stuck, or a cached still) is skipped. One or two bad frames only
show "suspect" on the dashboard. A failure is recorded in Activity, sent to Discord with the camera picture and,
if "action" is "pause", the print is paused once (only while the printer still reports RUNNING). Each print is
judged once: after a failure it stays flagged until the printer stops running or starts a different file.

config.json:
    "failure_detection": {"enabled": true, "model": "/opt/3d-printer-management-models/print_failure.hef",
                          "classes": ["spaghetti"], "labels": ["spaghetti"], "threshold": 0.6,
                          "interval": 30, "window": 10, "needed": 6, "min_minutes": 4, "warm_up": 3,
                          "action": "notify", "python": "/usr/bin/python3"}
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from failureDetection import print_geometry
from failureDetection.auto_reprint import AutoReprint
from failureDetection.model_updates import ModelUpdates
from failureDetection.training import TrainingPictures

log = logging.getLogger('failure-detection')
WORKER = Path(__file__).with_name('hailo_worker.py')
DEFAULTS = dict(enabled=False, model='', classes=['spaghetti'], labels=[], threshold=0.6, interval=30, window=10,
                needed=6, min_minutes=4, warm_up=3, action='notify', python='/usr/bin/python3', input_size=640,
                geometry={}, auto_reprint={}, crops='auto', collect={})
GEOMETRY_DEFAULTS = dict(enabled=True, on_part_weight=0.5, margin_mm=5.0)


def settings_for(config):
    raw = (config or {}).get('failure_detection')
    s = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    s['threshold'] = min(0.99, max(0.05, float(s['threshold'])))
    s['interval'] = max(10, int(s['interval']))
    s['window'] = min(60, max(3, int(s['window'])))
    s['needed'] = min(s['window'], max(2, int(s['needed'])))
    s['min_minutes'] = max(1.0, float(s['min_minutes']))
    s['warm_up'] = max(0.0, float(s['warm_up']))
    s['action'] = 'pause' if s['action'] == 'pause' else 'notify'
    s['crops'] = 'off' if s['crops'] in ('off', False, None) else 'auto'
    s['input_size'] = min(1280, max(160, int(s['input_size']) // 32 * 32))
    s['classes'] = [str(c) for c in s['classes']] or ['failure']
    s['labels'] = [str(c) for c in s['labels']] or list(s['classes'])
    g = {**GEOMETRY_DEFAULTS, **(s['geometry'] if isinstance(s['geometry'], dict) else {})}
    s['geometry'] = dict(enabled=bool(g['enabled']), on_part_weight=min(1.0, max(0.0, float(g['on_part_weight']))),
                         margin_mm=min(50.0, max(0.0, float(g['margin_mm']))))
    return s


# Close-ups checked as well as the whole picture (fractions of the picture). The model shrinks every picture to its
# input size by the longer side, so a full-width strip wouldn't enlarge anything: these are nearer to square. Without
# a calibration they cover the upper part of the frame, where the bed and print sit on side-mounted cameras (A1 / A1
# mini look across the bed from low down; the bottom of the frame is the printer's base).
DEFAULT_CROPS = [[0.2, 0.05, 0.8, 0.62], [0.0, 0.0, 0.55, 0.65], [0.45, 0.0, 1.0, 0.65]]


def crops_for(corners):
    """Close-ups for one camera: around the calibrated bed outline (widened upwards for tall parts and split in two
    when the bed is wide), else the defaults."""
    if not corners or len(corners) != 4:
        return [list(c) for c in DEFAULT_CROPS]
    us, vs = [float(c[0]) for c in corners], [float(c[1]) for c in corners]
    x0, x1, y0, y1 = min(us), max(us), min(vs), max(vs)
    height = y1 - y0
    x0, x1 = max(0.0, x0 - 0.03), min(1.0, x1 + 0.03)
    y0, y1 = max(0.0, y0 - max(0.15, height * 0.4)), min(1.0, y1 + 0.03)   # parts grow upwards in the picture
    if x1 - x0 > 0.55:
        middle = (x0 + x1) / 2
        overlap = (x1 - x0) * 0.08
        return [[round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
                [round(x0, 4), round(y0, 4), round(middle + overlap, 4), round(y1, 4)],
                [round(middle - overlap, 4), round(y0, 4), round(x1, 4), round(y1, 4)]]
    return [[round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]]


class GeometryCheck:
    """Compares AI detections with the print file (#79): where the sliced G-code puts plastic, seen through each
    camera's bed calibration. Only for jobs whose .3mf is on the Pi (the queue and Print now) and calibrated cameras;
    otherwise scores pass through unchanged."""

    def __init__(self, core, engine, settings):
        self.core, self.engine, self.settings = core, engine, settings['geometry']
        self.labels, self.threshold = set(settings['labels']), settings['threshold']
        self.folder = Path(getattr(core, 'DATA_DIR', tempfile.gettempdir())) / 'geometry'
        self.loaded, self.tasks = {}, {}   # name -> (key, Geometry or None, message); name -> parsing task

    def corners(self, name):
        saved = (self.core.settings.get('ai_calibration') or {}).get(name) or {}
        return saved.get('corners')

    def set_corners(self, name, corners):
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        calibration = dict(self.core.settings.get('ai_calibration') or {})
        if corners is None:
            calibration.pop(name, None)
        else:
            corners = [[round(float(u), 4), round(float(v), 4)] for u, v in corners]
            print_geometry.Calibration(corners, (0, 0, 1, 1))   # validates: four corners, in order, not crossed
            calibration[name] = {'corners': corners}
        self.core.save_settings({**self.core.settings, 'ai_calibration': calibration})

    def source(self, name):
        """(key, .3mf path, plate) for the printer's active queue job when its file is on the Pi."""
        store = getattr(self.engine, 'store', None)
        job = store.active(name) if store else None
        if not job or not job.get('asset') or not Path(job['asset']).is_file():
            return None
        options = job.get('options') or {}
        try:
            options = json.loads(options) if isinstance(options, str) else options
            plate = int(options.get('plate', 1))
        except (ValueError, TypeError, AttributeError):
            plate = 1
        return f"{job['id']}-{plate}", job['asset'], plate

    async def _parse(self, name, key, path, plate):
        self.folder.mkdir(parents=True, exist_ok=True)
        out = self.folder / f'{key}.json'
        try:
            if not out.is_file():
                command = [sys.executable, str(Path(print_geometry.__file__)), str(path), str(plate), str(out)]
                if os.path.exists('/usr/bin/nice'):
                    command = ['/usr/bin/nice', '-n', '15'] + command   # never compete with MQTT and the dashboard
                process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(process.wait(), 900)
            data = json.loads(out.read_text())
            if 'error' in data:
                raise ValueError(data['error'])
            geometry = print_geometry.Geometry.from_json(data)
            self.loaded[name] = (key, geometry, f'Print file read: {geometry.layers} layers.')
        except Exception as exc:
            self.loaded[name] = (key, None, f'Print file not used: {str(exc)[:200] or type(exc).__name__}')
        finally:
            self.tasks.pop(name, None)
            for old in sorted(self.folder.glob('*.json'), key=lambda p: p.stat().st_mtime)[:-20]:   # keep the last 20
                old.unlink(missing_ok=True)

    def current(self, name):
        """The Geometry for the active job (starting the background read when needed), or None."""
        if not self.settings['enabled']:
            return None
        found = self.source(name)
        if not found:
            self.loaded.pop(name, None)
            return None
        key, path, plate = found
        loaded = self.loaded.get(name)
        if loaded and loaded[0] == key:
            return loaded[1]
        if name not in self.tasks:
            self.loaded[name] = (key, None, 'Reading the print file…')
            self.tasks[name] = asyncio.create_task(self._parse(name, key, path, plate))
        return None

    def adjust(self, name, score, detections, layer):
        """(score to judge, note). Failure detections outside the part keep their score; on the part they count for
        on_part_weight of it, since that's more likely the part's own geometry."""
        geometry, corners = self.current(name), self.corners(name)
        boxed = [d for d in detections if d.get('box') and (not self.labels or d.get('label') in self.labels)]
        if geometry is None or not corners or not boxed or not layer:
            return score, ''
        try:
            calibration = print_geometry.Calibration(corners, (geometry.origin[0], geometry.origin[1],
                                                              geometry.origin[0] + geometry.width, geometry.origin[1] + geometry.depth))
        except ValueError:
            return score, ''
        best, outside = 0.0, False
        for detection in boxed:
            on_part = print_geometry.inside_fraction(detection['box'], geometry, calibration, layer, self.settings['margin_mm'])
            detection['on_part'] = round(on_part, 2)
            weighted = detection['score'] * (self.settings['on_part_weight'] if on_part >= 0.5 else 1.0)
            best = max(best, weighted)
            outside = outside or (on_part < 0.5 and detection['score'] >= self.threshold)
        note = ('outside where the print file puts plastic' if outside else
                'on the part itself (counted less)' if any(d['score'] >= self.threshold for d in boxed) else '')
        return round(best, 4), note

    def state(self, name):
        loaded = self.loaded.get(name)
        return dict(enabled=self.settings['enabled'], calibrated=bool(self.corners(name)), corners=self.corners(name),
                    file=bool(loaded and loaded[1]), message=loaded[2] if loaded else
                    'Used for prints started from the queue or Print now.')


class Judge:
    """The per-print evidence for one printer, and the decision."""

    def __init__(self, settings, clock=time.time):
        self.settings, self.clock = settings, clock
        self.reset('')

    def reset(self, job):
        self.job, self.started, self.frames, self.last_hash, self.flagged, self.acted = job, self.clock(), deque(maxlen=self.settings['window']), None, False, False

    def add(self, score, frame_hash):
        """Record one scored frame; returns 'warming_up', 'duplicate', 'watching', 'suspect' or 'failure'."""
        now = self.clock()
        if frame_hash == self.last_hash:
            return 'duplicate'
        self.last_hash = frame_hash
        if now - self.started < self.settings['warm_up'] * 60:
            return 'warming_up'
        # A long camera gap breaks the run of evidence.
        if self.frames and now - self.frames[-1][0] > self.settings['interval'] * 4:
            self.frames.clear()
        self.frames.append((now, float(score)))
        failing = [t for t, s in self.frames if s >= self.settings['threshold']]
        if self.flagged:
            return 'failure'
        if (len(failing) >= self.settings['needed'] and failing[-1] - failing[0] >= self.settings['min_minutes'] * 60
                and self.frames[-1][1] >= self.settings['threshold']):
            self.flagged = True
            return 'failure'
        return 'suspect' if len(failing) >= 2 else 'watching'

    def failing(self):
        return sum(1 for _, s in self.frames if s >= self.settings['threshold'])


class HailoBackend:
    """Talks to hailo_worker.py running under the system Python: a .hef model runs on the AI HAT (hailo-all),
    a .onnx model on the CPU (python3-opencv). Both are Raspberry Pi OS packages, so the service venv needs neither."""

    def __init__(self, settings):
        self.settings, self.process, self.lock, self.error, self.retry_at = settings, None, asyncio.Lock(), '', 0
        self.stderr, self.drain = deque(maxlen=20), None

    async def _start(self):
        s = self.settings
        if not Path(s['model']).is_file():
            raise RuntimeError(f"Model file not found: {s['model'] or '(set failure_detection.model)'}")
        # HailoRT writes hailort.log into its working folder; the app folder is read-only for the service.
        logs = Path(tempfile.gettempdir())
        self.process = await asyncio.create_subprocess_exec(
            s['python'], str(WORKER), s['model'], json.dumps(s['classes']), json.dumps(s['labels']), str(s['input_size']),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1024 * 1024,
            cwd=str(logs), env={**os.environ, 'HAILORT_LOGGER_PATH': str(logs)})
        self.stderr = deque(maxlen=20)
        self.drain = asyncio.create_task(self._drain(self.process))   # never let the helper block on a full stderr pipe
        line = await asyncio.wait_for(self.process.stdout.readline(), 60)
        if not line or not json.loads(line).get('ready'):
            if not line:
                await asyncio.wait_for(asyncio.shield(self.drain), 5)
            error = self.stderr[-1] if self.stderr else line.decode(errors='replace')[:200] or 'it exited'
            raise RuntimeError('AI helper did not start: ' + error + self.hint(error))

    def hint(self, error):
        if "No module named 'cv2'" in error:
            return ' (for a .onnx model: sudo apt install python3-opencv)'
        if "No module named 'hailo_platform'" in error:
            return ' (for a .hef model: sudo apt install hailo-all)'
        return ''

    async def _drain(self, process):
        async for raw in process.stderr:
            text = raw.decode(errors='replace').strip()
            if text:
                self.stderr.append(text[:300])
                log.debug('AI HAT helper: %s', text)

    async def score(self, jpeg, crops=None):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                if time.monotonic() < self.retry_at:
                    raise RuntimeError(self.error or 'AI helper unavailable')
                try:
                    await self._start()
                    self.error = ''
                except Exception as exc:
                    await self.close()
                    self.error, self.retry_at = str(exc)[:300], time.monotonic() + 300
                    raise RuntimeError(self.error) from exc
            try:
                self.process.stdin.write((json.dumps({'jpeg': base64.b64encode(jpeg).decode(), 'crops': crops or []}) + '\n').encode())
                await self.process.stdin.drain()
                reply = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 30))
            except Exception as exc:
                await self.close()
                raise RuntimeError(f'AI helper stopped ({type(exc).__name__})') from exc
            if 'error' in reply:
                raise RuntimeError(reply['error'])
            return float(reply.get('score') or 0), reply.get('detections') or []

    async def close(self):
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            with contextlib.suppress(Exception):
                await self.process.wait()
        self.process = None
        if self.drain:
            self.drain.cancel()
            await asyncio.gather(self.drain, return_exceptions=True)
            self.drain = None


class FailureMonitor:
    def __init__(self, core, engine, backend=None, clock=time.time):
        self.core, self.engine, self.clock = core, engine, clock
        self.settings = settings_for(getattr(core, 'CONFIG', {}))
        self.backend = backend or (HailoBackend(self.settings) if self.settings['enabled'] else None)
        self.judges, self.status, self.task, self.pause_poll = {}, {}, None, 1
        self.geometry = GeometryCheck(core, engine, self.settings)
        self.reprints = AutoReprint(core, engine, self.settings['auto_reprint'], python=self.settings['python'], clock=clock)
        self.models = ModelUpdates(core, self, clock=clock)
        self.training = TrainingPictures(core, self.settings['collect'], clock=clock)
        if self.settings['enabled']:
            self.models.apply_saved()   # a model an automatic update switched to last time

    @property
    def enabled(self):
        return bool(self.settings['enabled'] and self.backend)

    def watching(self, name):
        """Per-printer opt-out from the dashboard (settings.json "ai_watch_off")."""
        return name not in set(self.core.settings.get('ai_watch_off') or [])

    def set_watching(self, name, on):
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        off = set(self.core.settings.get('ai_watch_off') or [])
        off.discard(name) if on else off.add(name)
        self.core.save_settings({**self.core.settings, 'ai_watch_off': sorted(off)})
        self.store_event(name, 'AI failure watch ' + ('on' if on else 'off'), 'Changed in the dashboard.')

    def store_event(self, name, title, detail):
        listener = getattr(self.core, 'event_listener', None)
        if listener:
            listener(name, title, detail)

    def state(self, name):
        if not self.enabled:
            return dict(enabled=False, status='off')
        base = dict(enabled=True, watching=self.watching(name), action=self.settings['action'], status='idle', message='')
        return {**base, **self.status.get(name, {}), 'geometry': self.geometry.state(name), 'reprint': self.reprints.state(name),
                'model': Path(self.settings['model']).name, 'model_update': self.models.message}

    def _set(self, name, **values):
        self.status[name] = {**self.status.get(name, {}), **values, 'checked': self.clock()}

    async def check(self, name):
        """One look at one printer."""
        state, _, data, connected = self.core.state_data(name)
        config = self.core.printer_config(name) or {}
        if not self.watching(name) or config.get('camera_type') not in ('rtsp', 'jpeg_tcp'):
            self.judges.pop(name, None);self._set(name, status='idle', message='');return
        job = str(data.get('subtask_name') or data.get('gcode_file') or '')
        judge = self.judges.get(name)
        if state == 'PAUSE' and judge and judge.job == job:
            # Keep this print's evidence (and a flag) through a pause, so a resumed print isn't judged from scratch.
            if self.status.get(name, {}).get('status') not in ('failure', 'paused'):
                self._set(name, status='idle', message='Printer paused; watching resumes with the print.')
            return
        if state != 'RUNNING' or not connected:
            self.judges.pop(name, None)
            self._set(name, status='idle', message='Watches while the printer is printing.', score=None, failing=0, frames=0, job='');return
        if judge is None or judge.job != job:
            judge = self.judges[name] = Judge(self.settings, self.clock);judge.reset(job)
        picture = await self.core.snapshot(name, timeout=20)
        if not picture:
            self._set(name, status='watching', message='No camera picture this time.');return
        crops = crops_for(self.geometry.corners(name)) if self.settings['crops'] == 'auto' else []
        try:
            score, detections = await self.backend.score(picture, crops)
        except Exception as exc:
            self._set(name, status='unavailable', message=str(exc)[:300]);return
        try:
            layer = int(data.get('layer_num') or 0)
        except (TypeError, ValueError):
            layer = 0
        raw = score
        score, where = self.geometry.adjust(name, score, detections, layer)
        verdict = judge.add(score, hashlib.sha256(picture).hexdigest())
        if verdict == 'duplicate':
            return
        status = {'warming_up': 'watching', 'watching': 'watching', 'suspect': 'suspect', 'failure': 'failure'}[verdict]
        active = self.engine.store.active(name) if getattr(self.engine, 'store', None) else None
        try:   # training pictures from this camera (never lets a disk problem stop the watch)
            self.training.save(name, active['id'] if active else job, picture, raw, status)
        except OSError:
            log.warning('Could not save a training picture for %s', name)
        labels = ', '.join(sorted({d.get('label', '?') for d in detections if d.get('score', 0) >= self.settings['threshold']})) or ''
        message = ('Warming up: the first minutes of a print are not judged.' if verdict == 'warming_up' else
                   f"{judge.failing()} of the last {len(judge.frames)} frames look like a failure" + (f' ({labels})' if labels else '')
                   + (f', {where}' if where else '') + '.')
        if where:
            labels = f'{labels}, {where}' if labels else where
        if judge.acted:   # already reported (and maybe paused) for this print: keep that status, don't act again
            self._set(name, score=round(score, 3), failing=judge.failing(), frames=len(judge.frames));return
        self._set(name, status=status, score=round(score, 3), failing=judge.failing(), frames=len(judge.frames), message=message, job=job)
        if verdict == 'failure':
            judge.acted = True
            await self.act(name, job, judge, labels)

    async def test(self, name):
        """"Test AI now": one check on a fresh camera picture at any time, printing or not. It doesn't count towards
        the failure rules and never pauses anything; the picture is kept as a training picture."""
        if not self.enabled:
            raise ValueError('AI failure detection is not enabled in config.json.')
        if (self.core.printer_config(name) or {}).get('camera_type') not in ('rtsp', 'jpeg_tcp'):
            raise ValueError('This printer has no camera set up (camera_type in config.json).')
        picture = None
        capture = getattr(self.core, 'capture_still', None)
        if capture:
            try:
                picture = await capture(name, 20)
            except Exception:
                picture = None
        picture = picture or await self.core.snapshot(name, timeout=20)
        if not picture:
            raise ValueError('No camera picture right now. Check the camera, then try again.')
        crops = crops_for(self.geometry.corners(name)) if self.settings['crops'] == 'auto' else []
        try:
            score, detections = await self.backend.score(picture, crops)
        except Exception as exc:
            raise ValueError(f'The AI could not check the picture: {str(exc)[:300]}') from exc
        state, _, data, _ = self.core.state_data(name)
        where = ''
        if state in ('RUNNING', 'PAUSE'):
            try:
                score_on_file, where = self.geometry.adjust(name, score, detections, int(data.get('layer_num') or 0))
            except (TypeError, ValueError):
                score_on_file = score
        else:
            score_on_file = score
        threshold = self.settings['threshold']
        try:
            self.training.save(name, 'manual-test', picture, score, 'suspect' if score >= threshold else 'watching', force=True)
        except OSError:
            pass
        self.store_event(name, 'AI test', f'Score {score:.2f} (threshold {threshold:g})' + (f' • {where}' if where else ''))
        return dict(score=round(score, 3), judged=round(score_on_file, 3), threshold=threshold, failing=score_on_file >= threshold,
                    labels=sorted(self.settings['labels']), detections=detections, crops=crops, where=where,
                    picture=base64.b64encode(picture).decode(), checked=self.clock())

    async def act(self, name, job, judge, labels):
        minutes = (judge.frames[-1][0] - min(t for t, s in judge.frames if s >= self.settings['threshold'])) / 60
        detail = (f"{judge.failing()} of the last {len(judge.frames)} camera frames over {minutes:.0f} min look like a failed print"
                  + (f' ({labels})' if labels else '') + f" • {job or 'current print'}")
        paused = False
        if self.settings['action'] == 'pause':
            state = self.core.state_data(name)[0]
            if state != 'RUNNING':
                detail += f' • Not paused: the printer reports {state or "unknown"}'
            else:
                try:
                    await self.engine.control(name, 'pause', 'AI failure detection')
                    paused = await self.confirm_pause(name)
                    if not paused:
                        detail += ' • Pause sent, but the printer has not reported PAUSE yet'
                except Exception as exc:
                    detail += f' • Pause failed: {exc}'
        self._set(name, status='paused' if paused else 'failure', message=detail)
        title = '🤖 AI paused a failing print' if paused else '🤖 AI: print may be failing'
        hint = '\nCheck the printer. Resume from the dashboard or /resume if it is fine.' if paused else '\nCheck the printer and stop or pause it if needed.'
        job = self.engine.store.active(name) if getattr(self.engine, 'store', None) else None
        if paused and job and self.reprints.settings['enabled']:
            # Automatic reprint (#67): unless someone resumes or stops it, the job reprints elsewhere after the countdown.
            self.reprints.flag(name, job)
            try:
                available = self.reprints.summary(await self.reprints.availability(job))
            except Exception as exc:
                available = f'Could not check the other printers ({type(exc).__name__}).'
            hours = self.reprints.settings['after_hours']
            hint += (f"\n{available}\nIf it isn't resumed or stopped within {hours:g} h, it will be reprinted on an available printer."
                     " To do it now, use Reprint now in the dashboard.")
        await self.core.notify(name, title, detail + hint, getattr(self.core, 'RED', 0xE74C3C), True)

    async def confirm_pause(self, name, wait=30):
        """Only call it paused once the printer itself reports PAUSE (a sent command isn't an applied one)."""
        deadline = time.monotonic() + wait
        while True:
            if self.core.state_data(name)[0] == 'PAUSE':
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.pause_poll)

    async def run(self):
        while True:
            try:
                for name in self.core.names():
                    await self.check(name)
                await self.reprints.tick()
                await self.models.check()   # once a day: a newer model from the ai-model release
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Failure detection error')
            await asyncio.sleep(self.settings['interval'])

    async def start(self, app=None):
        if self.enabled and not getattr(self.core, 'EXAMPLE_MODE', False):
            if self.reprints.settings['enabled']:
                self.engine.on_start_approved = self.reprints.capture_reference
            self.task = asyncio.create_task(self.run())

    async def stop(self, app=None):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.backend and hasattr(self.backend, 'close'):
            await self.backend.close()
