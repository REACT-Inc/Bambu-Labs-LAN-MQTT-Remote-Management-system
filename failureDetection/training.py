"""Training pictures from your own cameras, for a better failure model.

A model trained on other people's photos struggles with your camera angle, lighting and plates (for example the A1
mini's low side view of a holographic plate). The fix is training on pictures from your own printers.

**Off unless turned on** (the dashboard's switch, settings.json "ai_collect", or config.json's collect.enabled). These
pictures never change the AI by themselves: they're only for a person to label and train a new model with. While it's
on and a printer prints, the monitor keeps:
- one camera still every `every_minutes` (1), and
- every frame the AI found suspicious or failing (each check, every 30 s), which are the most useful to label.
It also keeps a picture of each printer's **empty bed** whenever a queue print is started on a cleared plate, and when
someone answers "It's clear" to the bed check. A model trained without pictures of your empty plate can take its
texture (a textured or holographic plate) for spaghetti.

Pictures are kept per printer and print in /var/lib/3d-printer-management/training/, capped at `max_pictures`
(oldest removed first) and never filling the disk. "Download training pictures" in the dashboard gives a ZIP sorted
by what each picture shows (empty bed / false alarm / how the print ended): upload it to your Roboflow project, draw
boxes on the failures, mark everything else Null, and retrain (failureDetection/FAILURE_DETECTION.md, "Better accuracy
for your printers").
"""
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

DEFAULTS = dict(enabled=False, every_minutes=1.0, max_pictures=5000, min_free_mb=2048)
FLAGGED_GAP = 25   # seconds between kept suspicious frames per printer
HEADROOM = 20   # a tidy-up removes this many extra old pictures, so it lists the folder (slow on an SD card) rarely
EMPTY_BED = '@empty-bed'   # the folder for empty-bed pictures (safe() never makes an '@', so no print is named this)
README = """Training pictures from your printers' cameras
=============================================

empty-bed/     each printer's empty plate: taken when a queue print was started on a cleared plate,
               or when you answered "It's clear" to the bed check
false-alarms/  pictures the AI scored as failing, from prints that went on to finish: most are false alarms
failed/        prints that failed or were stopped
finished/      prints that finished normally
other/         prints still running, cancelled before starting, or not started from the queue

File names: <time>_score<AI score>.jpg; the score is how sure the model was that the print was failing.

To improve the model:
1. In Roboflow, open your project and Upload these pictures (drag the folders in).
2. Draw a box around every spaghetti, warping or stringing area and label it with that name.
3. Mark every picture that has nothing to box as Null (the Mark Null tool, key N, in Roboflow Annotate):
   all of empty-bed/, and the good pictures in false-alarms/, finished/ and the rest.
   This matters: Roboflow leaves pictures that have no boxes and no Null mark out of the dataset.
   Without them the model never sees your empty plate or a good print, and can take the plate's
   texture (for example a textured or holographic plate) for spaghetti.
4. Generate a new dataset version and run the Colab training again (FAILURE_DETECTION.md).
5. Before installing the new model, check it on your empty beds:
   install_model.py ... --healthy empty-bed --max-false 0.05 refuses a model that flags them.
6. Replace /opt/3d-printer-management-models/print_failure.onnx with the new best.onnx,
   or publish it in the repository's ai-model release so every Pi updates itself.
"""


def settings_for(raw):
    s = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    return dict(enabled=bool(s['enabled']), every_minutes=min(240.0, max(1.0, float(s['every_minutes']))),
                max_pictures=min(20000, max(50, int(s['max_pictures']))), min_free_mb=max(256, int(s['min_free_mb'])))


def safe(text):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', str(text))[:60].strip('._') or 'unknown'


class TrainingPictures:
    def __init__(self, core, settings, clock=time.time):
        self.core, self.settings, self.clock = core, settings_for(settings), clock
        data = getattr(core, 'DATA_DIR', None)
        self.folder = Path(data) / 'training' if data else None   # no data folder (tests): nothing is kept
        self.last, self.last_flagged, self.problem = {}, {}, ''   # problem: why the last picture wasn't kept
        self.count = None   # pictures kept, counted once and then kept up to date
        self.lock = threading.Lock()   # saves run in worker threads (one per printer at a time)

    @property
    def collecting(self):
        """Whether pictures are kept: the dashboard's choice (settings.json "ai_collect"), else config.json's
        collect.enabled, which is off by default."""
        chosen = (getattr(self.core, 'settings', None) or {}).get('ai_collect')
        return chosen if isinstance(chosen, bool) else self.settings['enabled']

    def pictures(self):
        return sorted(self.folder.glob('*/*/*.jpg'), key=lambda p: p.name) if self.folder and self.folder.is_dir() else []

    def save(self, name, job, picture, score, status, force=False):
        """Keep this frame when it's time for the periodic still, or when the AI found it suspicious (or force:
        a manual "Check AI now" picture)."""
        if not self.collecting or not picture or not self.folder:
            return None
        now = self.clock()
        flagged = status in ('suspect', 'failure', 'paused')
        periodic = now - self.last.get(name, 0) >= self.settings['every_minutes'] * 60
        if not force and not periodic and not (flagged and now - self.last_flagged.get(name, 0) >= FLAGGED_GAP):
            return None
        if not self.room():
            return None
        if periodic and not force:   # suspicious extras and tests don't shift the steady once-a-minute still
            self.last[name] = now
        if flagged:
            self.last_flagged[name] = now
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
        return self.write(self.folder / safe(name) / safe(job or 'no-job') / f'{stamp}_score{score:.2f}.jpg', picture)

    def save_empty(self, name, picture):
        """Keep a picture of this printer's empty bed (a print was just started on a cleared plate, or someone said
        it's clear). They're the pictures to mark Null: without them a model doesn't know what the plate looks like."""
        if not self.collecting or not picture or not self.folder or not self.room():
            return None
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(self.clock()))
        return self.write(self.folder / safe(name) / EMPTY_BED / f'{stamp}_empty.jpg', picture)

    def room(self):
        """True when there's room on the Pi for another picture; otherwise self.problem says why not."""
        try:
            free = shutil.disk_usage(self.folder.parent).free
        except OSError as exc:
            self.problem = f'Not saving: cannot check the free space ({exc.strerror or type(exc).__name__}).'
            return False
        if free < self.settings['min_free_mb'] * 1024 * 1024:
            self.problem = (f"Not saving: only {free / 1024 ** 3:.1f} GB free on the Pi, below the {self.settings['min_free_mb'] / 1024:.1f} GB "
                            'safety limit. Free some space, or lower "min_free_mb" under failure_detection.collect.')
            return False
        return True

    def write(self, target, picture):
        number = 2
        while target.exists():   # two pictures in the same second (e.g. repeated "Check AI now")
            target = target.with_name(f'{target.stem.rsplit("~", 1)[0]}~{number}.jpg')
            number += 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(picture)
            os.replace(temporary, target)
        except OSError as exc:
            self.problem = f'Not saving: {exc.strerror or type(exc).__name__} writing {target.parent}.'
            raise
        self.problem = ''
        with self.lock:
            self.count = len(self.pictures()) if self.count is None else self.count + 1
            if self.count > self.settings['max_pictures']:
                self.prune()
        return target

    def prune(self):
        """Remove the oldest pictures down to the cap, minus some headroom, so this runs once every HEADROOM saves
        instead of after every one."""
        pictures = self.pictures()
        keep = self.settings['max_pictures'] - min(HEADROOM, self.settings['max_pictures'] // 10)
        for old in pictures[:max(0, len(pictures) - keep)]:
            old.unlink(missing_ok=True)
        self.count = min(len(pictures), keep)
        for folder in (self.folder.glob('*/*') if self.folder else []):
            try:
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            except OSError:   # a picture was just saved there
                pass

    def summary(self):
        pictures = self.pictures()
        size = sum(p.stat().st_size for p in pictures)
        return dict(enabled=self.collecting, count=len(pictures), mb=round(size / 1024 / 1024, 1),
                    every_minutes=self.settings['every_minutes'], problem=self.problem)

    def export(self, outcomes, target, thresholds=None):
        """Write a ZIP of all pictures to target, sorted by what they show. outcomes: job id -> 'failed'/'finished'/...
        thresholds: printer -> its failure score; a picture from a print that finished, which the AI scored at least
        that, goes in false-alarms/."""
        thresholds = {safe(printer): score for printer, score in (thresholds or {}).items()}
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_STORED) as archive:   # JPEGs don't compress further
            archive.writestr('README.txt', README)
            for picture in self.pictures():
                printer, job = picture.parent.parent.name, picture.parent.name
                if job == EMPTY_BED:
                    archive.write(picture, f'empty-bed/{printer}/{picture.name}')
                    continue
                outcome = outcomes.get(job, '')
                group = 'failed' if outcome in ('failed', 'needs_review') else 'finished' if outcome == 'finished' else 'other'
                scored = re.search(r'_score(\d+(?:\.\d+)?)', picture.name)
                if group == 'finished' and scored and printer in thresholds and float(scored.group(1)) >= thresholds[printer]:
                    group = 'false-alarms'
                archive.write(picture, f'{group}/{printer}/{job}_{picture.name}')
        return target

    def clear(self):
        if self.folder and self.folder.is_dir():
            shutil.rmtree(self.folder)
        self.count = None
        self.last.clear()
        self.last_flagged.clear()
