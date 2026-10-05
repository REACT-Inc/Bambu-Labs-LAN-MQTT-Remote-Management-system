"""Training pictures from your own cameras, for a better failure model.

A model trained on other people's photos struggles with your camera angle, lighting and plates (for example the A1
mini's low side view of a holographic plate). The fix is training on pictures from your own printers, so while a
printer prints the monitor keeps:
- one camera still every `every_minutes` (5), and
- every frame the AI found suspicious or failing (at most one a minute), which are the most useful to label.

Pictures are kept per printer and print in /var/lib/3d-printer-management/training/, capped at `max_pictures`
(oldest removed first) and never filling the disk. "Download training pictures" in the dashboard gives a ZIP sorted
by how each print ended (failed / finished / other): upload it to your Roboflow project, draw boxes on the failures,
and retrain (failureDetection/FAILURE_DETECTION.md, "Better accuracy for your printers").
"""
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

DEFAULTS = dict(enabled=True, every_minutes=5.0, max_pictures=1500, min_free_mb=2048)
README = """Training pictures from your printers' cameras
=============================================

failed/    prints that failed or were stopped
finished/  prints that finished normally
other/     prints still running, cancelled before starting, or not started from the queue

File names: <time>_score<AI score>.jpg; the score is how sure the current model was that the print was failing.

To improve the model:
1. In Roboflow, open your project and Upload these pictures (drag the folders in).
2. Draw a box around every spaghetti, warping or stringing area and label it with that name.
   Leave pictures of good prints without boxes: they teach the model what normal looks like
   (for example a shiny holographic plate is NOT spaghetti).
3. Generate a new dataset version and run the Colab training again (FAILURE_DETECTION.md).
4. Replace /opt/3d-printer-management-models/print_failure.onnx with the new best.onnx,
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
        self.last, self.last_flagged = {}, {}

    def pictures(self):
        return sorted(self.folder.glob('*/*/*.jpg'), key=lambda p: p.name) if self.folder and self.folder.is_dir() else []

    def save(self, name, job, picture, score, status):
        """Keep this frame when it's time for the periodic still, or when the AI found it suspicious."""
        if not self.settings['enabled'] or not picture or not self.folder:
            return None
        now = self.clock()
        flagged = status in ('suspect', 'failure', 'paused')
        periodic = now - self.last.get(name, 0) >= self.settings['every_minutes'] * 60
        if not periodic and not (flagged and now - self.last_flagged.get(name, 0) >= 60):
            return None
        try:
            if shutil.disk_usage(self.folder.parent).free < self.settings['min_free_mb'] * 1024 * 1024:
                return None
        except OSError:
            return None
        self.last[name] = now
        if flagged:
            self.last_flagged[name] = now
        target = self.folder / safe(name) / safe(job or 'no-job') / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}_score{score:.2f}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(picture)
        os.replace(temporary, target)
        self.prune()
        return target

    def prune(self):
        pictures = self.pictures()
        for old in pictures[:max(0, len(pictures) - self.settings['max_pictures'])]:
            old.unlink(missing_ok=True)
        for folder in (self.folder.glob('*/*') if self.folder else []):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()

    def summary(self):
        pictures = self.pictures()
        size = sum(p.stat().st_size for p in pictures)
        return dict(enabled=self.settings['enabled'], count=len(pictures), mb=round(size / 1024 / 1024, 1),
                    every_minutes=self.settings['every_minutes'])

    def export(self, outcomes, target):
        """Write a ZIP of all pictures to target, sorted by outcome. outcomes: job id -> 'failed'/'finished'/..."""
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_STORED) as archive:   # JPEGs don't compress further
            archive.writestr('README.txt', README)
            for picture in self.pictures():
                printer, job = picture.parent.parent.name, picture.parent.name
                outcome = outcomes.get(job, '')
                group = 'failed' if outcome in ('failed', 'needs_review') else 'finished' if outcome == 'finished' else 'other'
                archive.write(picture, f'{group}/{printer}/{job}_{picture.name}')
        return target

    def clear(self):
        if self.folder and self.folder.is_dir():
            shutil.rmtree(self.folder)
        self.last.clear()
        self.last_flagged.clear()
