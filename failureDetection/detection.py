"""AI print-failure detection (#70): a .hef model on a Raspberry Pi 5 AI HAT, or a .onnx model on the CPU.
Off unless config.json enables it.

While a printer reports RUNNING, the monitor takes one still from its camera every `interval` seconds (the cheap
single-still path, reusing the dashboard's recent stills), has the model score it, and keeps the last `window`
scores. It only calls a print a failure when the evidence holds up over time:

- at least `needed` of the last `window` frames are failing: at or above `threshold`, or, once two frames reached
  the threshold, at or above the lower `hold` score (a real failure doesn't come and go, but the AI's score of it
  flickers around the threshold),
- the failing frames span at least `min_minutes` (a few bad frames in a row aren't enough),
- the newest frame is still failing (at or above `hold`), and
- the print has been running for at least `warm_up` minutes (heat-up, purge and first layer look odd).

Each printer's sensitivity (threshold, hold, minutes, share of frames, zoom) can be changed in its AI panel
(settings.json "ai_tuning"), from presets or one by one. The monitor also zooms in on the print by itself (Focus):
where the model saw something suspicious, or where a model with a "print"/"bed" class sees the print.

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
import math
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
DEFAULTS = dict(enabled=False, model='', classes=['spaghetti'], labels=[], threshold=0.6, interval='auto', window=10,
                needed=6, min_minutes=4, warm_up=3, action='notify', python='/usr/bin/python3', input_size=640,
                geometry={}, auto_reprint={}, crops='auto', collect={}, min_interval=None, max_interval=60,
                hold=None, focus=True)
START_INTERVAL = 30   # where the adaptive interval starts, and what old frame-count settings are measured in
GEOMETRY_DEFAULTS = dict(enabled=True, on_part_weight=0.5, margin_mm=5.0)


def settings_for(config):
    raw = (config or {}).get('failure_detection')
    s = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    s['threshold'] = min(0.99, max(0.05, float(s['threshold'])))
    # How often to look: "auto" adapts to how busy the Pi is (Throttle), or a fixed number of seconds.
    # The AI HAT (.hef) runs the model itself, so it may look more often than the CPU (.onnx).
    hat = str(s['model']).lower().endswith('.hef')
    floor = 5 if hat else 10
    s['min_interval'] = max(floor, int(s['min_interval'] if s['min_interval'] is not None else floor))
    s['max_interval'] = max(s['min_interval'], min(300, int(s['max_interval'])))
    s['fixed_interval'] = None if s['interval'] == 'auto' else min(300, max(floor, int(s['interval'])))
    s['interval'] = s['fixed_interval'] or START_INTERVAL
    s['window'] = min(60, max(3, int(s['window'])))
    s['needed'] = min(s['window'], max(2, int(s['needed'])))
    s['min_minutes'] = max(1.0, float(s['min_minutes']))
    # The decision is time-based, so it means the same however often the AI looks: the old "6 of the last 10
    # frames" (30 s apart) is "60% of the frames from the last 5 minutes". Both still have to span min_minutes.
    s['window_minutes'] = max(s['min_minutes'] + 1, s['window'] * (s['fixed_interval'] or START_INTERVAL) / 60)
    s['needed_share'] = s['needed'] / s['window']
    s['gap_seconds'] = max(120, 4 * s['max_interval'])   # a longer camera gap starts the evidence again
    s['warm_up'] = max(0.0, float(s['warm_up']))
    s['action'] = 'pause' if s['action'] == 'pause' else 'notify'
    s['crops'] = 'off' if s['crops'] in ('off', False, None) else 'auto'
    s['input_size'] = min(1280, max(160, int(s['input_size']) // 32 * 32))
    s['classes'] = [str(c) for c in s['classes']] or ['failure']
    # Classes that show where the print is (a model trained with a "print" or "bed" class) never count as failures.
    s['labels'] = [str(c) for c in s['labels']] or [c for c in s['classes'] if c.lower() not in AREA_CLASSES] or list(s['classes'])
    s['hold'] = hold_for(s['threshold'], s['hold'])
    s['focus'] = bool(s['focus'])
    g = {**GEOMETRY_DEFAULTS, **(s['geometry'] if isinstance(s['geometry'], dict) else {})}
    s['geometry'] = dict(enabled=bool(g['enabled']), on_part_weight=min(1.0, max(0.0, float(g['on_part_weight']))),
                         margin_mm=min(50.0, max(0.0, float(g['margin_mm']))))
    return s


def hold_for(threshold, hold=None):
    """The score that keeps counting once the threshold was reached: by default 0.15 under the threshold."""
    value = threshold - 0.15 if hold is None else float(hold)
    return round(min(threshold, max(0.05, value)), 3)


# Per-printer sensitivity, set in the dashboard. "normal" is config.json's own settings.
TUNING_LIMITS = dict(threshold=(0.05, 0.95), hold=(0.05, 0.95), min_minutes=(1.0, 30.0), needed_share=(0.1, 1.0))
PRESETS = {
    'cautious': dict(threshold=0.6, hold=0.5, min_minutes=6, needed_share=0.7),
    'normal': {},
    'sensitive': dict(threshold=0.35, hold=0.2, min_minutes=3, needed_share=0.4),
    'very_sensitive': dict(threshold=0.25, hold=0.12, min_minutes=2, needed_share=0.3),
}
PRESET_NAMES = {'cautious': 'Cautious (fewer false alarms)', 'normal': 'Normal (config.json)', 'sensitive': 'Sensitive',
                'very_sensitive': 'Very sensitive (catches more, more false alarms)', 'custom': 'Custom'}


def tuned(settings, saved):
    """settings with one printer's dashboard tuning applied (saved: {'preset': ..., 'threshold': ..., ...})."""
    saved = saved if isinstance(saved, dict) else {}
    preset = saved.get('preset') if saved.get('preset') in PRESETS else 'custom' if saved else 'normal'
    values = dict(PRESETS.get(preset, {}))
    if preset == 'custom':
        values = {k: saved[k] for k in TUNING_LIMITS if saved.get(k) is not None}
    result = dict(settings)
    for key, (low, high) in TUNING_LIMITS.items():
        if key in values:
            try:
                result[key] = round(min(high, max(low, float(values[key]))), 3)
            except (TypeError, ValueError):
                pass
    if 'hold' not in values:
        result['hold'] = hold_for(result['threshold'], None if result['threshold'] != settings['threshold'] else settings['hold'])
    result['hold'] = min(result['hold'], result['threshold'])
    result['window_minutes'] = max(settings['window_minutes'], result['min_minutes'] + 1)
    if saved.get('focus') is not None:
        result['focus'] = bool(saved['focus'])
    result['preset'] = preset
    return result


AREA_CLASSES = {'print', 'object', 'part', 'model', 'bed', 'plate', 'build_plate', 'buildplate', 'printbed'}
FOCUS_MIN = 0.1      # a failure-class detection this sure is worth a closer look next time
FOCUS_KEEP = 600     # seconds a suspicious spot keeps getting a close-up after it was last seen
AREA_KEEP = 3600     # seconds the print's area (from an area class) is kept


def padded(box, factor, minimum):
    """box (0-1 of the picture) grown factor times around its centre, at least minimum wide and high, inside the picture."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = min(1.0, max(minimum, (x1 - x0) * factor)), min(1.0, max(minimum, (y1 - y0) * factor))
    left, top = min(max(0.0, cx - w / 2), 1.0 - w), min(max(0.0, cy - h / 2), 1.0 - h)
    return [round(left, 4), round(top, 4), round(left + w, 4), round(top + h, 4)]


def overlap(a, b):
    """Intersection over union of two boxes."""
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - w * h
    return w * h / union if union > 0 else 0.0


class Focus:
    """Finds the print in the picture by itself, so the model gets a close look where it matters.

    The failure model only knows failures, so it can't point at the bed. But even a weak detection (10% and up) is
    usually on the print: the next checks add a close-up around it (2.5x its size) for 10 minutes. A small spaghetti
    nest that scores 30% in the whole picture often scores much higher enlarged. A model trained with a "print" or
    "bed" class (AREA_CLASSES) also gives the print's area, which gets its own close-up."""

    def __init__(self, clock=time.time):
        self.clock, self.spots = clock, {}   # name -> {'job', 'focus', 'focus_at', 'focus_score', 'area', 'area_at'}

    def reset(self, name, job):
        if self.spots.get(name, {}).get('job') != job:
            self.spots[name] = {'job': job}

    def update(self, name, detections, labels):
        spot, now = self.spots.setdefault(name, {'job': None}), self.clock()
        boxed = [d for d in detections if d.get('box')]
        areas = [d['box'] for d in boxed if str(d.get('label', '')).lower() in AREA_CLASSES and d.get('label') not in labels]
        if areas:
            union = [min(b[0] for b in areas), min(b[1] for b in areas), max(b[2] for b in areas), max(b[3] for b in areas)]
            spot.update(area=padded(union, 1.3, 0.3), area_at=now)
        suspicious = [d for d in boxed if (not labels or d.get('label') in labels) and d.get('score', 0) >= FOCUS_MIN]
        if suspicious:
            best = max(suspicious, key=lambda d: d['score'])
            spot.update(focus=padded(best['box'], 2.5, 0.3), focus_at=now, focus_score=best['score'])

    def crops(self, name, base):
        """The extra close-ups for this printer, leaving out any that duplicate one of base."""
        spot, now, extra = self.spots.get(name, {}), self.clock(), []
        for key, keep in (('area', AREA_KEEP), ('focus', FOCUS_KEEP)):
            box = spot.get(key)
            if box and now - spot.get(key + '_at', 0) <= keep and all(overlap(box, other) < 0.8 for other in base + extra):
                extra.append(box)
        return extra

    def describe(self, name):
        spot, now = self.spots.get(name, {}), self.clock()
        if spot.get('area') and now - spot.get('area_at', 0) <= AREA_KEEP:
            return 'zooming in on the print the model found'
        if spot.get('focus') and now - spot.get('focus_at', 0) <= FOCUS_KEEP:
            return f"zooming in on a suspicious spot ({spot.get('focus_score', 0):.0%}, {int((now - spot['focus_at']) // 60)} min ago)"
        return ''


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


MIN_FAILING = 4   # never decide on fewer failing frames than this, however slowly the AI is looking
STRONG_NEEDED = 2   # frames at or above the threshold before frames at or above hold count too


class Throttle:
    """Adaptive check interval: look more often while the Pi is quiet, back off while it's busy.

    After each round of checks it looks at the Pi's load (per core), how late the event loop woke up (a sign the
    dashboard and printer connections are being starved) and how long the round took, then:
    - busy (load over 0.75 per core, or the loop woke over half a second late): 1.5x longer, up to max_interval;
    - quiet (load under 0.5 per core): 20% shorter, down to min_interval (10 s on the CPU, 5 s with the AI HAT);
    - never more than half the time spent checking (the interval is at least twice the round's length).
    A fixed "interval" in config.json turns this off."""

    def __init__(self, settings, load=None):
        self.settings = settings
        self.current = settings['fixed_interval'] or START_INTERVAL
        self.load = load or self.system_load
        self.reason = 'starting'

    @staticmethod
    def system_load():
        try:
            return os.getloadavg()[0] / max(1, os.cpu_count() or 1)
        except OSError:
            return 0.0

    def next(self, cost, lateness=0.0):
        """cost: seconds the last round of checks took; lateness: how late the last sleep woke up."""
        if self.settings['fixed_interval']:
            self.current, self.reason = self.settings['fixed_interval'], 'fixed in config.json'
            return self.current
        load = self.load()
        if load > 0.75 or lateness > 0.5:
            self.current *= 1.5
            self.reason = f'Pi busy (load {load:.2f} per core), looking less often'
        elif load < 0.5:
            self.current *= 0.8
            self.reason = f'Pi quiet (load {load:.2f} per core)'
        else:
            self.reason = f'Pi moderately busy (load {load:.2f} per core)'
        self.current = max(self.settings['min_interval'], 2 * cost, min(self.settings['max_interval'], self.current))
        self.current = min(self.current, max(self.settings['max_interval'], 2 * cost))
        self.settings['interval'] = self.current   # the snapshot freshness and the docs read this
        return self.current


class Judge:
    """The per-print evidence for one printer, and the decision."""

    def __init__(self, settings, clock=time.time):
        self.settings, self.clock = settings, clock
        self.reset('')

    def reset(self, job):
        self.job, self.started, self.frames, self.last_hash, self.flagged, self.acted = job, self.clock(), deque(maxlen=600), None, False, False

    def add(self, score, frame_hash):
        """Record one scored frame; returns 'warming_up', 'duplicate', 'watching', 'suspect' or 'failure'."""
        now = self.clock()
        if frame_hash == self.last_hash:
            return 'duplicate'
        self.last_hash = frame_hash
        if now - self.started < self.settings['warm_up'] * 60:
            return 'warming_up'
        # A long camera gap breaks the run of evidence.
        if self.frames and now - self.frames[-1][0] > self.settings['gap_seconds']:
            self.frames.clear()
        self.frames.append((now, float(score)))
        while self.frames and now - self.frames[0][0] > self.settings['window_minutes'] * 60:
            self.frames.popleft()   # only the last window_minutes count
        failing = self.failing_times()
        if self.flagged:
            return 'failure'
        if (len(failing) >= self.needed() and failing[-1] - failing[0] >= self.settings['min_minutes'] * 60
                and self.frames[-1][1] >= self.settings['hold'] and self.strong() >= STRONG_NEEDED):
            self.flagged = True
            return 'failure'
        return 'suspect' if len(failing) >= 2 else 'watching'

    def strong(self):
        return sum(1 for _, s in self.frames if s >= self.settings['threshold'])

    def failing_times(self):
        """Times of the failing frames in the window: those at or above the threshold, and once STRONG_NEEDED frames
        reached it, every frame from the first of them on that is at or above the lower hold score."""
        th, hold = self.settings['threshold'], self.settings['hold']
        if self.strong() < STRONG_NEEDED:
            return [t for t, s in self.frames if s >= th]
        first = next(t for t, s in self.frames if s >= th)
        return [t for t, s in self.frames if t >= first and s >= hold]

    def needed(self):
        """Failing frames needed now: needed_share of the frames in the window, never fewer than MIN_FAILING."""
        return max(MIN_FAILING, math.ceil(self.settings['needed_share'] * len(self.frames)))

    def failing(self):
        return len(self.failing_times())


class HailoBackend:
    """Talks to hailo_worker.py running under the system Python: a .hef model runs on the AI HAT (hailo-all),
    a .onnx model on the CPU (python3-opencv). Both are Raspberry Pi OS packages, so the service venv needs neither."""

    def __init__(self, settings):
        self.settings, self.process, self.lock, self.error, self.retry_at = settings, None, asyncio.Lock(), '', 0
        self.stderr, self.drain = deque(maxlen=20), None
        self.active, self.backend_name, self.note = '', '', ''   # model in use, where it runs, why (fallback)

    def fallback(self):
        """The CPU model to use when the AI HAT model won't start: failure_detection.fallback_model, else a .onnx
        next to the .hef (print_failure.hef -> print_failure.onnx)."""
        primary = Path(self.settings['model'])
        if primary.suffix.lower() != '.hef':
            return None
        candidate = Path(self.settings.get('fallback_model') or primary.with_suffix('.onnx'))
        return candidate if candidate.is_file() and candidate != primary else None

    async def _start(self):
        s = self.settings
        primary = s['model']
        try:
            ready = await self._spawn(primary)
            self.note = ''
        except Exception as exc:
            backup = self.fallback()
            if not backup:
                raise
            await self.close()
            log.warning('AI HAT model %s did not start (%s); falling back to the CPU model %s', Path(primary).name, exc, backup.name)
            ready = await self._spawn(str(backup))
            self.note = f'AI HAT model failed to start ({str(exc)[:150]}); using the CPU model instead.'
            s['min_interval'] = max(10, s['min_interval'])   # the CPU can't keep the HAT's pace
        self.active = ready['model']
        self.backend_name = 'AI HAT (Hailo-8)' if ready.get('backend') == 'hailo' else 'CPU'
        log.info('AI model ready: %s on %s%s', Path(self.active).name, self.backend_name, ' (fallback)' if self.note else '')

    async def _spawn(self, model):
        s = self.settings
        if not Path(model).is_file():
            raise RuntimeError(f"Model file not found: {model or '(set failure_detection.model)'}")
        # HailoRT writes hailort.log into its working folder; the app folder is read-only for the service.
        logs = Path(tempfile.gettempdir())
        self.process = await asyncio.create_subprocess_exec(
            s['python'], str(WORKER), model, json.dumps(s['classes']), json.dumps(s['labels']), str(s['input_size']),
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
        return dict(json.loads(line), model=model)

    async def warm(self):
        """Start the helper now (at service start) instead of on the first check, so the log says straight away
        which model loaded where, and the first check isn't slowed down by loading it."""
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                try:
                    await self._start()
                    self.error = ''
                except Exception as exc:
                    await self.close()
                    self.error, self.retry_at = str(exc)[:300], time.monotonic() + 300
                    log.error('AI model did not start: %s', self.error)

    def describe(self):
        return dict(backend=self.backend_name or ('not started' if not self.error else 'unavailable'),
                    active_model=Path(self.active).name if self.active else '', backend_note=self.note or self.error)

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
        self.checking = asyncio.Lock()   # the background loop and "Check AI now" never judge at the same moment
        self.throttle = Throttle(self.settings)
        self.geometry = GeometryCheck(core, engine, self.settings)
        self.reprints = AutoReprint(core, engine, self.settings['auto_reprint'], python=self.settings['python'], clock=clock)
        self.models = ModelUpdates(core, self, clock=clock)
        self.training = TrainingPictures(core, self.settings['collect'], clock=clock)
        self.focus = Focus(clock)
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

    def tuning(self, name):
        """This printer's settings with its dashboard sensitivity applied."""
        return tuned(self.settings, (self.core.settings.get('ai_tuning') or {}).get(name))

    def set_tuning(self, name, data):
        """Dashboard sensitivity: {'preset': 'sensitive'}, or custom values (threshold, hold, min_minutes,
        needed_share, focus), or {'preset': 'normal'} for config.json's settings."""
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        preset = data.get('preset') or 'custom'
        if preset not in PRESETS and preset != 'custom':
            raise ValueError('Unknown sensitivity preset.')
        saved = {'preset': preset} if preset != 'custom' else {}
        if preset == 'custom':
            for key, (low, high) in TUNING_LIMITS.items():
                if data.get(key) is not None:
                    try:
                        saved[key] = float(data[key])
                    except (TypeError, ValueError):
                        raise ValueError(f'{key} must be a number.') from None
                    if not low <= saved[key] <= high:
                        raise ValueError(f'{key} must be between {low:g} and {high:g}.')
            if saved.get('hold') is not None and saved.get('threshold') is not None and saved['hold'] > saved['threshold']:
                raise ValueError('The keep-counting score must not be above the failure score.')
        if data.get('focus') is not None:
            saved['focus'] = bool(data['focus'])
        all_tuning = dict(self.core.settings.get('ai_tuning') or {})
        if saved in ({'preset': 'normal'}, {'preset': 'normal', 'focus': self.settings['focus']}):
            all_tuning.pop(name, None)
        else:
            all_tuning[name] = saved
        self.core.save_settings({**self.core.settings, 'ai_tuning': all_tuning})
        t = self.tuning(name)
        self.store_event(name, 'AI sensitivity changed', f"{PRESET_NAMES[t['preset']]}: failure at {t['threshold']:.0%}, keeps counting "
                         f"from {t['hold']:.0%}, {t['needed_share']:.0%} of frames over {t['min_minutes']:g} min"
                         f"{', zoom on suspicious spots' if t['focus'] else ', no zoom'}.")
        return t

    def tuning_state(self, name):
        t = self.tuning(name)
        values = lambda v: {k: v[k] for k in ('threshold', 'hold', 'min_minutes', 'needed_share')}
        return dict(preset=t['preset'], preset_name=PRESET_NAMES[t['preset']], focus=t['focus'], window_minutes=t['window_minutes'],
                    **values(t), presets={k: values(tuned(self.settings, {'preset': k})) for k in PRESETS},
                    nms_floor=0.25 if str(self.settings['model']).lower().endswith('.hef') else 0)

    def store_event(self, name, title, detail):
        listener = getattr(self.core, 'event_listener', None)
        if listener:
            listener(name, title, detail)

    def state(self, name):
        if not self.enabled:
            return dict(enabled=False, status='off')
        base = dict(enabled=True, watching=self.watching(name), action=self.settings['action'], status='idle', message='')
        return {**base, **self.status.get(name, {}), 'geometry': self.geometry.state(name), 'reprint': self.reprints.state(name),
                'model': Path(self.settings['model']).name, 'model_update': self.models.message,
                'interval': round(self.throttle.current, 1), 'interval_reason': self.throttle.reason,
                'tuning': self.tuning_state(name), 'focus': self.focus.describe(name),
                **(self.backend.describe() if hasattr(self.backend, 'describe') else {})}

    def _set(self, name, **values):
        self.status[name] = {**self.status.get(name, {}), **values, 'checked': self.clock()}

    async def check_now(self, name):
        """"Check AI now": a real check of a printing printer on request. It counts like any check, and because a
        person asked for a decision, a frame at or above the threshold acts straight away (pause or notify, as set)
        instead of waiting for several failing frames over minutes. A print already reported isn't acted on again."""
        if not self.enabled:
            raise ValueError('AI failure detection is not enabled in config.json.')
        if (self.core.printer_config(name) or {}).get('camera_type') not in ('rtsp', 'jpeg_tcp'):
            raise ValueError('This printer has no camera set up (camera_type in config.json).')
        state, _, _, connected = self.core.state_data(name)
        if state != 'RUNNING' or not connected:
            raise ValueError(f"Check AI now works while a print is running (the printer reports {state or 'unknown'}). "
                             'Use Test AI now to see what the AI makes of the camera any time.')
        async with self.checking:
            result = await self.check(name, on_request=True)
        if not result:
            raise ValueError(self.status.get(name, {}).get('message') or 'The check could not run.')
        return result

    async def check(self, name, on_request=False):
        """One look at one printer. Returns what it found (or None when it couldn't look)."""
        state, _, data, connected = self.core.state_data(name)
        config = self.core.printer_config(name) or {}
        if (not self.watching(name) and not on_request) or config.get('camera_type') not in ('rtsp', 'jpeg_tcp'):
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
        tuning = self.tuning(name)
        if judge is None or judge.job != job:
            judge = self.judges[name] = Judge(tuning, self.clock);judge.reset(job)
        judge.settings = tuning   # a sensitivity change applies to the evidence already collected
        self.focus.reset(name, job)
        picture = None
        if on_request and getattr(self.core, 'capture_still', None):   # a fresh picture, not a reused one
            try:
                picture = await self.core.capture_still(name, 20)
            except Exception:
                picture = None
        # A reused picture must be newer than the check interval: an older one would be the frame the last check
        # already judged (skipped as a duplicate), halving how many frames count.
        picture = picture or await self.core.snapshot(name, timeout=20, max_age=max(4, self.throttle.current - 5))
        if not picture:
            self._set(name, status='watching', message='No camera picture this time.');return
        crops = self.crops(name, tuning)
        try:
            score, detections = await self.backend.score(picture, crops)
        except Exception as exc:
            self._set(name, status='unavailable', message=str(exc)[:300]);return
        self.focus.update(name, detections, tuning['labels'])
        try:
            layer = int(data.get('layer_num') or 0)
        except (TypeError, ValueError):
            layer = 0
        raw = score
        score, where = self.geometry.adjust(name, score, detections, layer)
        frame_id = hashlib.sha256(picture).hexdigest() + (f'@{self.clock()}' if on_request else '')
        verdict = judge.add(score, frame_id)
        if verdict == 'duplicate':
            return None
        status = {'warming_up': 'watching', 'watching': 'watching', 'suspect': 'suspect', 'failure': 'failure'}[verdict]
        active = self.engine.store.active(name) if getattr(self.engine, 'store', None) else None
        try:   # training pictures from this camera (never lets a disk problem stop the watch)
            self.training.save(name, active['id'] if active else job, picture, raw, status)
        except OSError:
            log.warning('Could not save a training picture for %s', name)
        labels = ', '.join(sorted({d.get('label', '?') for d in detections if d.get('score', 0) >= tuning['threshold']})) or ''
        message = ('Warming up: the first minutes of a print are not judged.' if verdict == 'warming_up' else
                   f"{judge.failing()} of the last {len(judge.frames)} frames look like a failure" + (f' ({labels})' if labels else '')
                   + (f', {where}' if where else '') + '.')
        if where:
            labels = f'{labels}, {where}' if labels else where
        threshold = tuning['threshold']
        result = dict(score=round(score, 3), raw=round(raw, 3), threshold=threshold, hold=tuning['hold'], verdict=verdict, where=where,
                      detections=detections, failing=judge.failing(), frames=len(judge.frames), acted=False, already=judge.acted)
        if judge.acted:   # already reported (and maybe paused) for this print: keep that status, don't act again
            self._set(name, score=round(score, 3), failing=judge.failing(), frames=len(judge.frames));return result
        if on_request and verdict != 'failure' and score >= threshold:
            verdict = result['verdict'] = 'failure'   # a person asked for a decision: this frame decides
            status = 'failure'
        self._set(name, status=status, score=round(score, 3), failing=judge.failing(), frames=len(judge.frames), message=message, job=job)
        if verdict == 'failure':
            judge.acted = result['acted'] = True
            await self.act(name, job, judge, labels, on_request=on_request, score=score)
        return result

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
        tuning = self.tuning(name)
        crops = self.crops(name, tuning)
        try:
            score, detections = await self.backend.score(picture, crops)
        except Exception as exc:
            raise ValueError(f'The AI could not check the picture: {str(exc)[:300]}') from exc
        self.focus.update(name, detections, tuning['labels'])
        state, _, data, _ = self.core.state_data(name)
        where = ''
        if state in ('RUNNING', 'PAUSE'):
            try:
                score_on_file, where = self.geometry.adjust(name, score, detections, int(data.get('layer_num') or 0))
            except (TypeError, ValueError):
                score_on_file = score
        else:
            score_on_file = score
        threshold = tuning['threshold']
        try:
            self.training.save(name, 'manual-test', picture, score, 'suspect' if score >= threshold else 'watching', force=True)
        except OSError:
            pass
        self.store_event(name, 'AI test', f'Score {score:.2f} (threshold {threshold:g})' + (f' • {where}' if where else ''))
        return dict(score=round(score, 3), judged=round(score_on_file, 3), threshold=threshold, failing=score_on_file >= threshold,
                    labels=sorted(self.settings['labels']), detections=detections, crops=crops, where=where,
                    picture=base64.b64encode(picture).decode(), checked=self.clock())

    def crops(self, name, tuning):
        """Close-ups for this check: around the calibrated bed (or the defaults), plus where Focus found the print
        or a suspicious spot."""
        if self.settings['crops'] != 'auto':
            return []
        base = crops_for(self.geometry.corners(name))
        return base + (self.focus.crops(name, base) if tuning['focus'] else [])

    async def act(self, name, job, judge, labels, on_request=False, score=None):
        failing_times = judge.failing_times()
        if on_request and not judge.flagged:
            detail = (f"Checked on request: this camera frame scored {score:.0%} (threshold {judge.settings['threshold']:.0%})"
                      + (f' ({labels})' if labels else '') + f" • {job or 'current print'}")
        else:
            minutes = (judge.frames[-1][0] - min(failing_times)) / 60 if failing_times else 0
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
        lateness = 0.0
        while True:
            started = time.monotonic()
            try:
                for name in self.core.names():
                    async with self.checking:
                        await self.check(name)
                await self.reprints.tick()
                await self.models.check()   # once a day: a newer model from the ai-model release
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Failure detection error')
            interval = self.throttle.next(time.monotonic() - started, lateness)
            woke = time.monotonic() + interval
            await asyncio.sleep(interval)
            lateness = max(0.0, time.monotonic() - woke)   # a late wake-up means the Pi is struggling

    async def start(self, app=None):
        if self.enabled and not getattr(self.core, 'EXAMPLE_MODE', False):
            if hasattr(self.backend, 'warm'):
                asyncio.create_task(self.backend.warm())   # load the model now; logs where it runs
            if self.reprints.settings['enabled']:
                self.engine.on_start_approved = self.reprints.capture_reference
            self.task = asyncio.create_task(self.run())

    async def stop(self, app=None):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.backend and hasattr(self.backend, 'close'):
            await self.backend.close()
