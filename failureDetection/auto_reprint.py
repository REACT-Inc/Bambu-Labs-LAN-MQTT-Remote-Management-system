"""Automatic reprint on another printer after an AI-paused failure (#67).

When the AI pauses a failing print, a countdown starts. Its length is the job's **reprint priority**, chosen when the
print is queued: 1 = 5 minutes (urgent), 2 = 1 hour, 3 = 1.5 hours, 4 = 2 hours, 5 = 2.5 hours (the default). If nobody
resumes or stops the print in that time, the job is reprinted on an available printer; "Reprint now" in the dashboard
does the same at once. `after_hours` in config.json caps the wait (2.5 hours by default).

A printer is available for a job when all of these hold:
- it's the model the file was sliced for (the same check as Print on another printer),
- it's online, idle or finished, with no error and no active queue job,
- its loaded AMS filament matches the plate (or the job used the external spool),
- its bed is empty: the bed check AI (bed_model.py) when it knows that bed, otherwise a comparison with the empty-bed
  picture taken the last time a queue job started there (failureDetection/bed_check.py).

The reprint: the paused print is stopped (so the printer doesn't sit paused and heated), the job is recorded as
failed, and a copy is started on the chosen printer, with no confirmation needed since the camera checked its bed.
With no available printer the countdown keeps waiting and tries again every minute, saying why in the dashboard.

**Mark available** (an override, per printer): someone who has looked at a printer can make it available although the
app can't confirm it: its bed isn't confirmed empty, its model is unknown, or its filament isn't an exact match (the
closest loaded filaments are used). It never overrides what would make the print fail: a different printer model, an
offline, printing or erroring printer, or one busy with another queue job. The mark ends as soon as that printer is
seen printing (started here, from Bambu Studio or on its screen), or after OVERRIDE_HOURS.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

log = logging.getLogger('failure-detection')
HERE = Path(__file__).resolve().parent
DEFAULTS = dict(enabled=True, after_hours=2.5, stop_original=True, bed_threshold=0.005)
# Reprint priority (queueing.options) -> how long an AI-paused print waits before it's reprinted elsewhere.
WAIT_FOR_PRIORITY = {1: 5 * 60, 2: 60 * 60, 3: 90 * 60, 4: 120 * 60, 5: 150 * 60}
DEFAULT_PRIORITY = 5
OVERRIDE_HOURS = 12
BUSY_STATES = ('PREPARE', 'RUNNING', 'PAUSE', 'SLICING')   # a printer seen in one of these is printing
BED_FRESH = 600          # a bed check is reused for 10 minutes
AUTHOR = 'AI auto reprint'


def settings_for(raw):
    s = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    return dict(enabled=bool(s['enabled']), after_hours=min(168.0, max(0.0, float(s['after_hours']))),
                stop_original=bool(s['stop_original']), bed_threshold=min(0.5, max(0.001, float(s['bed_threshold']))))


def safe_name(name):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60] or 'printer'


def wait_text(seconds):
    return f'{round(seconds / 60)} min' if seconds < 3600 else f'{seconds / 3600:g} h'


class AutoReprint:
    def __init__(self, core, engine, settings, python='/usr/bin/python3', clock=time.time):
        self.core, self.engine, self.settings, self.python, self.clock = core, engine, settings_for(settings), python, clock
        data = Path(getattr(core, 'DATA_DIR', tempfile.gettempdir()))
        self.beds, self.path = data / 'beds', data / 'ai-reprints.json'
        self.overrides_path = data / 'ai-reprint-available.json'
        try:
            self.overrides = json.loads(self.overrides_path.read_text())   # printer -> {until, author}
        except (OSError, ValueError):
            self.overrides = {}
        self.bed_cache, self.lock, self.last = {}, asyncio.Lock(), {}   # last: printer -> (time, availability summary)
        self.bed_ai = None   # the bed check AI (bed_model.py); asked first when it's ready
        try:
            self.pending = json.loads(self.path.read_text())   # job id -> {printer, since, label, note}
        except (OSError, ValueError):
            self.pending = {}

    # ---- empty-bed references ----

    def reference(self, name):
        return self.beds / f'{safe_name(name)}.jpg'

    async def capture_reference(self, job, author=''):
        """Called when a print start is approved with "the plate is clear": take a fresh picture of the empty bed."""
        if author == AUTHOR or (job.get('options') or {}).get('swap') or getattr(self.core, 'EXAMPLE_MODE', False):
            return   # a Swapmod swap print starts with the finished part still on the bed
        name = job['printer']
        if (self.core.printer_config(name) or {}).get('camera_type') not in ('rtsp', 'jpeg_tcp'):
            return
        try:
            picture = await self.core.capture_still(name, 20)
        except Exception as exc:
            log.info('Empty-bed reference not taken for %s (%s)', name, type(exc).__name__)
            return
        if picture:
            self.beds.mkdir(parents=True, exist_ok=True)
            temporary = self.reference(name).with_suffix('.tmp')
            temporary.write_bytes(picture)
            os.replace(temporary, self.reference(name))
            self.bed_cache.pop(name, None)

    async def bed_state(self, name, refresh=False):
        """{'state': 'clear'|'parts'|'unknown', 'detail': text, 'checked': time}."""
        cached = self.bed_cache.get(name)
        if cached and not refresh and self.clock() - cached['checked'] < BED_FRESH:
            return cached
        result = await self._check_bed(name)
        result['checked'] = self.clock()
        self.bed_cache[name] = result
        return result

    async def _check_bed(self, name):
        if (self.core.printer_config(name) or {}).get('camera_type') not in ('rtsp', 'jpeg_tcp'):
            return dict(state='unknown', detail='no camera')
        note = ''
        if self.bed_ai and self.bed_ai.ready:   # the bed check AI first; when it's unsure, the picture comparison
            result = await self.bed_ai.check(name)
            if result['state'] != 'unknown':
                return dict(state=result['state'], detail='AI: ' + result['detail'], by='ai')
            note = f" (AI {result['detail']})"
        if not self.reference(name).is_file():
            return dict(state='unknown', detail='no empty-bed picture yet (taken at the next print start)' + note)
        try:
            picture = await self.core.capture_still(name, 20)
        except Exception as exc:
            return dict(state='unknown', detail=f'camera error ({type(exc).__name__})')
        if not picture:
            return dict(state='unknown', detail='no camera picture')
        corners = ((self.core.settings.get('ai_calibration') or {}).get(name) or {}).get('corners') or []
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as stream:
            stream.write(picture)
        try:
            process = await asyncio.create_subprocess_exec(
                self.python, str(HERE / 'bed_check.py'), str(self.reference(name)), stream.name, json.dumps(corners),
                str(self.settings['bed_threshold']), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await asyncio.wait_for(process.communicate(), 60)
            result = json.loads(output.decode().strip().splitlines()[-1])
        except Exception as exc:
            return dict(state='unknown', detail=f'bed check failed ({type(exc).__name__})')
        finally:
            os.unlink(stream.name)
        if 'error' in result:
            hint = ' (sudo apt install python3-pil python3-numpy)' if 'No module' in result['error'] else ''
            return dict(state='unknown', detail=result['error'][:200] + hint)
        percent = result['changed'] * 100
        return dict(state='clear' if result['clear'] else 'parts',
                    detail=f'{percent:.1f}% of the bed differs from the empty-bed picture' + note, by='picture')

    # ---- which printers can take the job ----

    def _plate_mapping(self, job, path, target, closest=False):
        """(use_ams, mapping, reason) for the job on the target's loaded trays; reason is '' when it fits. closest:
        (a printer marked available) the closest loaded filaments are good enough, as long as every one is loaded."""
        if not job['options'].get('use_ams'):
            return False, '', ''
        import sliced_file
        try:
            plates = {p['index']: p for p in sliced_file.inspect(path)['plates']} if path else {}
        except ValueError:
            plates = {}
        plate = plates.get(int(job['options'].get('plate', 1)))
        if not plate:
            return False, '', "can't read the file's filaments to match the AMS"
        suggestion = sliced_file.suggest_mapping(plate, self.core.state_data(target)[2])
        if not suggestion['complete']:
            return False, '', 'the plate needs filament that is not loaded'
        if not closest and any(r['match'] != 'exact' for r in suggestion['rows']):
            return False, '', 'the loaded filament does not match'
        return True, suggestion['mapping'], ''

    async def availability(self, job, refresh=False):
        """[{name, display, ok, reason, bed}] for every other printer, best first. Uses the cameras, so it runs only
        when the AI pauses a print, on "Check available printers" and when reprinting, never on dashboard refreshes."""
        import job_transfer
        results = []
        for name in self.core.names():
            if name == job['printer']:
                continue
            marked = self.marked(name)
            row = dict(name=name, display=self.core.display_name(name) if hasattr(self.core, 'display_name') else name,
                       ok=False, reason='', bed='', marked=bool(marked), can_mark=False)
            check = job_transfer.check(self.core, job, name)
            if check['compatible'] is False:
                row['reason'] = ("the job's file is no longer on the Pi" if 'no longer on the Pi' in check['reason']
                                 else 'different printer model')
            elif check['compatible'] is not True and not marked:
                row['reason'], row['can_mark'] = 'model unknown', True
            elif self.engine.store.active(name):
                row['reason'] = 'busy with another queue job'
            else:
                try:
                    self.engine.ready(name)
                except ValueError as exc:
                    row['reason'] = str(exc).split('.')[0].lower()
            if not row['reason'] and job.get('asset'):
                row['use_ams'], row['mapping'], row['reason'] = self._plate_mapping(job, job['asset'], name, closest=bool(marked))
                row['can_mark'] = row['reason'] == 'the loaded filament does not match'
            if not row['reason'] and marked:
                row['bed'] = 'marked'   # someone looked: no camera check
            elif not row['reason']:
                bed = await self.bed_state(name, refresh)
                row['bed'] = bed['state']
                if bed['state'] != 'clear':
                    row['reason'] = 'parts on the bed' if bed['state'] == 'parts' else f"bed not confirmed empty: {bed['detail']}"
                    row['can_mark'] = True
            row['ok'] = not row['reason']
            results.append(row)
        results.sort(key=lambda r: (not r['ok'], not r['marked'], r['name']))
        self.last[job['printer']] = (self.clock(), self.summary(results),
                                     [{k: r[k] for k in ('name', 'display', 'ok', 'reason', 'marked', 'can_mark')} for r in results])
        return results

    # ---- "Mark available" (an override someone sets after looking at the printer) ----

    def marked(self, name):
        """{until, author} while this printer is marked available, else None (a mark ends after OVERRIDE_HOURS)."""
        entry = self.overrides.get(name)
        if entry and self.clock() >= entry['until']:
            self.overrides.pop(name, None)
            self.save_overrides()
            self.update_rows(name, False, f'no longer marked available: {OVERRIDE_HOURS} h passed')
            return None
        return entry

    def save_overrides(self):
        temporary = self.overrides_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.overrides))
        os.replace(temporary, self.overrides_path)

    def mark(self, name, on, author):
        """Mark a printer available for reprints (or undo it), whatever the app can't confirm about it."""
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        if on:
            self.overrides[name] = dict(until=self.clock() + OVERRIDE_HOURS * 3600, author=str(author)[:80])
        elif not self.overrides.pop(name, None):
            return
        self.save_overrides()
        self.engine.store.event(name, 'Marked available for reprints' if on else 'No longer marked available', str(author)[:80])
        self.update_rows(name, on)

    def update_rows(self, name, on, why='no longer marked available: check again'):
        """The printer lists already shown (for each paused print) with this printer's mark changed, without looking
        at any camera again. The reprint itself always checks everything afresh."""
        for job_printer, (checked, summary, rows) in list(self.last.items()):
            changed = False
            for row in rows:
                if row['name'] == name and on and not row['ok'] and row['can_mark']:
                    row.update(ok=True, reason='', marked=True, can_mark=False)
                    changed = True
                elif row['name'] == name and not on and row['marked']:
                    row.update(ok=False, reason=why, marked=False, can_mark=True)
                    changed = True
            if changed:
                rows.sort(key=lambda r: (not r['ok'], not r['marked'], r['name']))
                self.last[job_printer] = (checked, self.summary(rows), rows)

    def started(self, name, why='it started a print'):
        """A print started on this printer: its bed won't be empty afterwards, so the mark ends."""
        if self.overrides.pop(name, None):
            self.save_overrides()
            if name in self.core.names():
                self.engine.store.event(name, 'No longer marked available', why)
            self.update_rows(name, False, f'no longer marked available: {why}')

    def forget_busy_marks(self):
        """A mark ends as soon as that printer is seen printing, wherever the print was started (this app, Bambu
        Studio, its own screen): its bed won't be empty afterwards."""
        for name in list(self.overrides):
            if name not in self.core.names():
                self.started(name, 'printer removed')
            elif self.core.state_data(name)[0] in BUSY_STATES:
                self.started(name)

    @staticmethod
    def summary(rows):
        ok = [r['display'] + (' (marked available)' if r['marked'] else ' (bed checked empty)') for r in rows if r['ok']]
        if ok:
            return 'Available for a reprint: ' + ', '.join(ok) + '.'
        if not rows:
            return 'No other printer to reprint on.'
        return 'No printer available for a reprint: ' + '; '.join(f"{r['display']}: {r['reason']}" for r in rows) + '.'

    # ---- countdown ----

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.pending))
        os.replace(temporary, self.path)

    def flag(self, name, job):
        """The AI paused this job's print: start the countdown."""
        if not self.settings['enabled'] or not job:
            return
        priority = (job.get('options') or {}).get('priority', DEFAULT_PRIORITY)
        self.pending[job['id']] = dict(printer=name, since=self.clock(), label=job['label'], note='',
                                       priority=priority if priority in WAIT_FOR_PRIORITY else DEFAULT_PRIORITY)
        self.save()

    def wait(self, entry):
        """Seconds an AI-paused print waits before it's reprinted: its priority's wait, at most after_hours."""
        return min(WAIT_FOR_PRIORITY.get(entry.get('priority'), WAIT_FOR_PRIORITY[DEFAULT_PRIORITY]),
                   self.settings['after_hours'] * 3600)

    def due(self, job_id):
        entry = self.pending.get(job_id)
        return entry['since'] + self.wait(entry) if entry else None

    def for_printer(self, name):
        return next(((job_id, entry) for job_id, entry in self.pending.items() if entry['printer'] == name), (None, None))

    def cancel(self, name, author):
        job_id, _ = self.for_printer(name)
        if not job_id:
            raise ValueError('No automatic reprint is waiting for this printer.')
        self.pending.pop(job_id, None)
        self.save()
        self.engine.store.event(name, 'Automatic reprint cancelled', f'{job_id} • {author}')

    def dealt_with(self, job_id, entry):
        """The paused print was resumed, stopped or recorded: no automatic reprint."""
        try:
            job = self.engine.store.get(job_id)
        except ValueError:
            return True
        return job['status'] != 'paused' or self.core.state_data(entry['printer'])[0] != 'PAUSE'

    async def tick(self):
        """Every round of the AI loop: end the marks of printers that started printing, drop dealt-with prints, and
        reprint the ones whose countdown ran out."""
        self.forget_busy_marks()
        for job_id, entry in list(self.pending.items()):
            if self.dealt_with(job_id, entry):
                self.pending.pop(job_id, None)
                self.save()
                self.engine.store.event(entry['printer'], 'Automatic reprint not needed', f"{entry['label']} • the paused print was dealt with")
            elif self.clock() >= self.due(job_id):
                try:
                    await self.reprint(entry['printer'], AUTHOR)
                except ValueError as exc:
                    if entry.get('note') != str(exc):
                        entry['note'] = str(exc)
                        self.save()
                        await self.core.notify(entry['printer'], '🤖 Automatic reprint waiting', f"{entry['label']}: {exc} Trying again every minute.",
                                               getattr(self.core, 'YELLOW', 0xF1C40F), False)

    async def reprint(self, name, author):
        """Reprint the AI-paused job from this printer on an available printer now. Returns the new job."""
        import job_transfer
        async with self.lock:
            job_id, entry = self.for_printer(name)
            job = self.engine.store.get(job_id) if job_id else self.engine.store.active(name)
            if not job or job['status'] not in ('paused', 'needs_review'):
                raise ValueError('There is no paused queue job on this printer to reprint.')
            rows = await self.availability(job, refresh=True)
            target = next((r for r in rows if r['ok']), None)
            if not target:
                raise ValueError(self.summary(rows))
            marked = self.marked(target['name']) if target['marked'] else None
            # Said in the notification; read now, because starting the reprint ends the mark (started()).
            checked = (f"{marked['author']} marked this printer available." if marked
                       else 'The camera checked this bed was empty.')
            asset = None
            if job_transfer.needs_fetch(self.core, job):   # the file is only on the failed printer: copy it first
                uploads = Path(getattr(self.core, 'DATA_DIR', tempfile.gettempdir())) / 'uploads'
                asset = await asyncio.to_thread(job_transfer.fetch, self.core, job, uploads)
                target['use_ams'], target['mapping'], reason = self._plate_mapping(job, asset, target['name'], closest=bool(marked))
                if reason:
                    Path(asset).unlink(missing_ok=True)
                    raise ValueError(f"{target['display']}: {reason}.")
            # Everything the copy needs is checked before the paused print is stopped, so a reprint that can't
            # happen never leaves a stopped print behind. An unknown model is accepted only for a marked printer.
            fits = job_transfer.check(self.core, job, target['name'], asset)
            if fits['compatible'] is False or (fits['compatible'] is None and not marked):
                if asset:
                    Path(asset).unlink(missing_ok=True)
                raise ValueError(f"{target['display']}: {fits['reason']}")
            if self.settings['stop_original'] and self.core.state_data(name)[0] in ('PAUSE', 'RUNNING'):
                await self.engine.control(name, 'stop', author)
            self.engine.store.set_status(job['id'], 'failed', f"AI: failed print, reprinted on {target['display']} • {author}")
            copy = job_transfer.send(self.core, self.engine.store, job['id'], target['name'], target.get('use_ams', False),
                                     target.get('mapping', ''), bool(marked), author, asset=asset)
            await self.engine.start(copy['id'], True, author)
            self.pending.pop(job['id'], None)
            self.save()
            await self.core.notify(target['name'], '🤖 AI reprint started', f"{job['label']} failed on {name} and is now printing here. "
                                   f"{checked} • {author}", getattr(self.core, 'BLUE', 0x3498DB), True)
            return copy

    def state(self, name):
        job_id, entry = self.for_printer(name)
        checked, summary, rows = self.last.get(name, (None, '', []))
        base = dict(enabled=self.settings['enabled'], after_hours=self.settings['after_hours'], available=summary, checked=checked,
                    printers=rows, marked=bool(self.overrides.get(name)))
        if not job_id:
            return dict(base, pending=False)
        return dict(base, pending=True, job=job_id, label=entry['label'], due=self.due(job_id), note=entry.get('note', ''),
                    priority=entry.get('priority', DEFAULT_PRIORITY), wait=self.wait(entry))
