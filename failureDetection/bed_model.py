"""Bed check AI: is the build plate empty? A second AI model on the AI HAT, next to the failure model.

**Where the model comes from.** Hailo's model compiler only runs on x86 PCs, so no Pi can build a .hef. Instead the
service downloads Hailo's own precompiled CLIP image model (`clip_resnet_50x4`) from the Hailo Model Zoo, built for
this Pi's chip (Hailo-8 or Hailo-8L) and HailoRT version. That happens by itself in the background the first time
the service runs with an AI HAT (65-125 MB); the status icon at the top left shows the progress. When HailoRT is
updated (apt), the matching model is downloaded again (looked at once a day).

**How it decides.** CLIP turns a picture into a "fingerprint" (embedding) that captures what's in it, and two pictures
of the same scene give similar fingerprints even in different light. Each printer keeps fingerprints of its own bed:
- **empty:** a picture when a queue job is started (the plate was just cleared), and after a Swapmod swap print
  finished (a fresh plate);
- **parts on the bed:** a picture shortly after a normal print finished;
- **your answers:** "It's clear" / "Not clear" in the printer panel teach it the picture it just checked.
A new picture is compared with both sets (the closest few of each). Until a printer has at least MIN_EACH of each,
or when the two are too close to call, it says "not sure" and the picture comparison (bed_check.py) decides instead.
It's never the only safety: an unsure or "parts" answer never starts anything by itself.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

log = logging.getLogger('failure-detection')

MODEL = 'clip_resnet_50x4'
ZOO = 'https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/{version}/{chip}/' + MODEL + '.hef'
# Each Model Zoo release is compiled for one HailoRT version (its changelog: "Update to use HailoRT 4.20.0").
ZOO_FOR_HAILORT = {(4, 19): 'v2.13.0', (4, 20): 'v2.14.0', (4, 21): 'v2.15.0', (4, 22): 'v2.16.0', (4, 23): 'v2.17.0'}
CHIPS = ('hailo8', 'hailo8l')
CHECK_EVERY = 24 * 3600
RETRY_AFTER = 3600       # after a failed download
MIN_EACH = 3             # examples of each kind before the AI gives an answer
KEEP = 30                # examples of each kind kept per printer (the newest)
NEAREST = 3              # how many of the closest examples are averaged
MARGIN = 0.02            # how much closer to one kind than the other before it decides
LEARN_DELAY = 20         # seconds after a print ends before its "parts on the bed" picture is taken
STATUS_KEY = 'bed-ai'


def zoo_version(hailort):
    """The Model Zoo release for a HailoRT version ('4.20.0'), or None. A newer HailoRT than we know gets the newest."""
    match = re.match(r'(\d+)\.(\d+)', str(hailort or ''))
    if not match:
        return None
    key = (int(match.group(1)), int(match.group(2)))
    if key in ZOO_FOR_HAILORT:
        return ZOO_FOR_HAILORT[key]
    newest = max(ZOO_FOR_HAILORT)
    return ZOO_FOR_HAILORT[newest] if key > newest else None


def download(url, target, progress):
    """Download url to target (atomically), calling progress(fraction or None). Returns the size."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.part')
    request = urllib.request.Request(url, headers={'User-Agent': '3d-printer-management'})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get('Content-Length') or 0)
        if total and shutil.disk_usage(target.parent).free < total * 2 + 256 * 1024 * 1024:
            raise RuntimeError(f'Not enough free space for the bed check model ({total / 1e6:.0f} MB).')
        done, last = 0, 0.0
        with open(temporary, 'wb') as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
                done += len(chunk)
                if total and done / total - last >= 0.01:
                    last = done / total
                    progress(last)
    if total and done != total:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f'The download was cut off ({done} of {total} bytes).')
    os.replace(temporary, target)
    return done


def similarity(a, b):
    return sum(x * y for x, y in zip(a, b))   # both unit length: the cosine similarity


def safe_name(name):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60] or 'printer'


class BedAI:
    def __init__(self, core, monitor, clock=time.time, fetch=download):
        self.core, self.monitor, self.clock, self.fetch = core, monitor, clock, fetch
        data = Path(getattr(core, 'DATA_DIR', tempfile.gettempdir()))
        self.folder, self.memory = data / 'models' / 'bed', data / 'beds'
        raw = monitor.settings.get('bed_ai')
        self.enabled = bool((raw if isinstance(raw, dict) else {}).get('enabled', True))
        self.task, self.checked, self.failed_at = None, 0, 0
        self.message = ''
        self.last = {}   # printer -> the last check: {'vector', 'state', 'detail', 'checked'}

    # ---- the model file ----

    @property
    def backend(self):
        return self.monitor.backend

    def wanted(self):
        """(url, path) of the model this Pi's AI HAT needs, or None (no AI HAT, or an unknown HailoRT)."""
        device = getattr(self.backend, 'device', None) or {}
        chip, version = device.get('chip'), zoo_version(device.get('hailort'))
        if chip not in CHIPS or not version:
            return None
        return ZOO.format(version=version, chip=chip), self.folder / f'{MODEL}-{version}-{chip}.hef'

    def status(self, title, detail='', level='busy', progress=None):
        self.message = title + (f': {detail}' if detail else '')
        center = getattr(self.core, 'status_center', None)
        if center:
            center.set(STATUS_KEY, title, detail, level, progress)

    async def tick(self):
        """From the AI loop: set up or update the model in the background when needed. Never waits for a download."""
        if not self.enabled or (self.task and not self.task.done()):
            return
        if self.clock() - self.checked < (RETRY_AFTER if self.failed_at else CHECK_EVERY) and self.checked:
            return
        if not str(self.monitor.settings.get('model', '')).lower().endswith('.hef'):
            return   # the bed model runs on the AI HAT only
        if not getattr(self.backend, 'device', None):
            return   # the helper hasn't said which chip and HailoRT it has yet
        self.checked = self.clock()
        self.task = asyncio.create_task(self.setup())

    async def setup(self):
        wanted = self.wanted()
        if not wanted:
            device = getattr(self.backend, 'device', {})
            self.status('Bed check AI not available', f"No precompiled model for this AI HAT ({device.get('chip') or 'unknown chip'}, "
                        f"HailoRT {device.get('hailort') or 'unknown'}). The picture comparison checks beds instead.", 'info')
            return
        url, path = wanted
        try:
            if not path.is_file():
                self.status('Downloading the bed check AI', f'{MODEL} from the Hailo Model Zoo, for {path.stem.rsplit("-", 1)[-1]}', 'busy', 0)
                loop = asyncio.get_running_loop()
                progress = lambda fraction: loop.call_soon_threadsafe(
                    self.status, 'Downloading the bed check AI', 'From the Hailo Model Zoo', 'busy', fraction)
                await asyncio.to_thread(self.fetch, url, path, progress)
                for old in self.folder.glob(f'{MODEL}-*.hef'):   # a model for an older HailoRT
                    if old != path:
                        old.unlink(missing_ok=True)
            if self.monitor.settings.get('bed_model') != str(path) or not getattr(self.backend, 'bed_ready', False):
                self.status('Loading the bed check AI', 'Starting it on the AI HAT next to the failure model')
                self.monitor.settings['bed_model'] = str(path)
                await self.backend.restart()
            if getattr(self.backend, 'bed_ready', False):
                self.failed_at = 0
                self.status('Bed check AI ready', self.learning(), 'ok')
            else:
                raise RuntimeError(getattr(self.backend, 'bed_error', '') or 'It did not load on the AI HAT.')
        except Exception as exc:
            self.failed_at = self.clock()
            log.warning('Bed check AI setup failed: %s', exc)
            self.status('Bed check AI setup failed', f'{str(exc)[:200]} Trying again in an hour; the picture comparison checks beds meanwhile.', 'warn')

    def learning(self):
        counts = [self.counts(name) for name in self.core.names()]
        ready = sum(1 for c in counts if c['empty'] >= MIN_EACH and c['parts'] >= MIN_EACH)
        return f'{ready} of {len(counts)} printers have learned their bed. The rest learn from their next prints.'

    @property
    def ready(self):
        return self.enabled and bool(getattr(self.backend, 'bed_ready', False))

    # ---- what each printer's bed looks like ----

    def path(self, name):
        return self.memory / f'{safe_name(name)}.examples.json'

    def examples(self, name):
        try:
            data = json.loads(self.path(name).read_text())
            return {kind: list(data.get(kind) or []) for kind in ('empty', 'parts')}
        except (OSError, ValueError):
            return {'empty': [], 'parts': []}

    def counts(self, name):
        return {kind: len(items) for kind, items in self.examples(name).items()}

    def remember(self, name, kind, vector, source):
        examples = self.examples(name)
        examples[kind] = (examples[kind] + [dict(vector=vector, at=self.clock(), source=source)])[-KEEP:]
        self.memory.mkdir(parents=True, exist_ok=True)
        temporary = self.path(name).with_suffix('.tmp')
        temporary.write_text(json.dumps(examples, separators=(',', ':')))
        os.replace(temporary, self.path(name))

    def bed_crop(self, name):
        """The calibrated bed outline's bounding box (with a margin for tall parts), or None for the whole picture."""
        corners = ((self.core.settings.get('ai_calibration') or {}).get(name) or {}).get('corners') or []
        if len(corners) != 4:
            return None
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        return [max(0.0, min(xs) - 0.03), max(0.0, min(ys) - 0.15), min(1.0, max(xs) + 0.03), min(1.0, max(ys) + 0.03)]

    async def fingerprint(self, name, picture=None):
        """The bed's fingerprint from a fresh camera picture (or the one given)."""
        if picture is None:
            picture = await self.core.capture_still(name, 20)
        if not picture:
            raise RuntimeError('No camera picture.')
        crop = self.bed_crop(name)
        vectors = await self.backend.embed(picture, [crop] if crop else [])
        return vectors[-1]   # the bed crop when calibrated, else the whole picture

    def judge(self, name, vector):
        examples = self.examples(name)
        if min(len(examples['empty']), len(examples['parts'])) < MIN_EACH:
            c = self.counts(name)
            return dict(state='unknown', detail=f"still learning this bed ({c['empty']} empty and {c['parts']} with parts "
                                                f'seen; needs {MIN_EACH} of each)')
        close = {kind: sorted((similarity(vector, e['vector']) for e in items), reverse=True)[:NEAREST]
                 for kind, items in examples.items()}
        empty, parts = (sum(close[k]) / len(close[k]) for k in ('empty', 'parts'))
        if abs(empty - parts) < MARGIN:
            return dict(state='unknown', detail=f'not sure: looks as much like an empty bed as one with parts ({empty:.3f} vs {parts:.3f})')
        state = 'clear' if empty > parts else 'parts'
        return dict(state=state, detail=('looks like its empty bed' if state == 'clear' else 'looks like a bed with parts on it')
                    + f' ({empty:.3f} vs {parts:.3f})')

    async def check(self, name, picture=None):
        """{'state': 'clear'|'parts'|'unknown', 'detail', 'checked'} from the bed check AI."""
        if not self.ready:
            return dict(state='unknown', detail='the bed check AI is not ready', checked=self.clock())
        try:
            vector = await self.fingerprint(name, picture)
        except Exception as exc:
            return dict(state='unknown', detail=f'bed check AI: {str(exc)[:150]}', checked=self.clock())
        result = dict(self.judge(name, vector), checked=self.clock())
        self.last[name] = dict(result, vector=vector)
        return result

    async def learn(self, name, kind, source, delay=0, still=None):
        """Remember a picture of this printer's bed as empty or with parts. still(): is it still worth taking (the
        printer hasn't started something else meanwhile)?"""
        if not self.ready or getattr(self.core, 'EXAMPLE_MODE', False):
            return
        try:
            if delay:
                await asyncio.sleep(delay)
            if still and not still():
                return
            self.remember(name, kind, await self.fingerprint(name), source)
            self.status('Bed check AI ready', self.learning(), 'ok')
        except Exception as exc:
            log.info('Bed check AI: no %s picture for %s (%s)', kind, name, exc)

    def answer(self, name, clear, author=''):
        """Someone looked: teach the AI the picture it checked last on this printer."""
        last = self.last.get(name)
        if not last or self.clock() - last['checked'] > 3600:
            raise ValueError('Check the bed first, then say whether it was clear.')
        self.remember(name, 'empty' if clear else 'parts', last['vector'], f'answer by {author}'[:80])
        self.last.pop(name, None)
        return self.counts(name)

    def state(self, name):
        last = self.last.get(name) or {}
        return dict(ready=self.ready, message=self.message, counts=self.counts(name), needed=MIN_EACH,
                    last={k: last[k] for k in ('state', 'detail', 'checked') if k in last})
