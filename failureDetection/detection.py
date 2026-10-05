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
import tempfile
import time
from collections import deque
from pathlib import Path

log = logging.getLogger('failure-detection')
WORKER = Path(__file__).with_name('hailo_worker.py')
DEFAULTS = dict(enabled=False, model='', classes=['spaghetti'], labels=[], threshold=0.6, interval=30, window=10,
                needed=6, min_minutes=4, warm_up=3, action='notify', python='/usr/bin/python3', input_size=640)


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
    s['input_size'] = min(1280, max(160, int(s['input_size']) // 32 * 32))
    s['classes'] = [str(c) for c in s['classes']] or ['failure']
    s['labels'] = [str(c) for c in s['labels']] or list(s['classes'])
    return s


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

    async def score(self, jpeg):
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
                self.process.stdin.write((json.dumps({'jpeg': base64.b64encode(jpeg).decode()}) + '\n').encode())
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
        return {**base, **self.status.get(name, {})}

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
        try:
            score, detections = await self.backend.score(picture)
        except Exception as exc:
            self._set(name, status='unavailable', message=str(exc)[:300]);return
        verdict = judge.add(score, hashlib.sha256(picture).hexdigest())
        if verdict == 'duplicate':
            return
        status = {'warming_up': 'watching', 'watching': 'watching', 'suspect': 'suspect', 'failure': 'failure'}[verdict]
        labels = ', '.join(sorted({d.get('label', '?') for d in detections if d.get('score', 0) >= self.settings['threshold']})) or ''
        message = ('Warming up: the first minutes of a print are not judged.' if verdict == 'warming_up' else
                   f"{judge.failing()} of the last {len(judge.frames)} frames look like a failure" + (f' ({labels})' if labels else '') + '.')
        if judge.acted:   # already reported (and maybe paused) for this print: keep that status, don't act again
            self._set(name, score=round(score, 3), failing=judge.failing(), frames=len(judge.frames));return
        self._set(name, status=status, score=round(score, 3), failing=judge.failing(), frames=len(judge.frames), message=message, job=job)
        if verdict == 'failure':
            judge.acted = True
            await self.act(name, job, judge, labels)

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
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Failure detection error')
            await asyncio.sleep(self.settings['interval'])

    async def start(self, app=None):
        if self.enabled and not getattr(self.core, 'EXAMPLE_MODE', False):
            self.task = asyncio.create_task(self.run())

    async def stop(self, app=None):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.backend and hasattr(self.backend, 'close'):
            await self.backend.close()
