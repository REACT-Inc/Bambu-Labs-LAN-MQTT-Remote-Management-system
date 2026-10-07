"""Automatic reprint on another printer after an AI-paused failure (#67).

When the AI pauses a failing print, a countdown starts (12 hours by default). If nobody resumes or stops the print
in that time, the job is reprinted on an available printer; "Reprint now" in the dashboard does the same at once.

A printer is available for a job when all of these hold:
- it's the model the file was sliced for (the same check as Print on another printer),
- it's online, idle or finished, with no error and no active queue job,
- its loaded AMS filament matches the plate (or the job used the external spool),
- the camera shows its bed is empty: compared with the empty-bed reference taken the last time someone confirmed
  "the plate is clear" when starting a print there (failureDetection/bed_check.py).

The reprint: the paused print is stopped (so the printer doesn't sit paused and heated), the job is recorded as
failed, and a copy is started on the chosen printer, with no confirmation needed since the camera checked its bed.
With no available printer the countdown keeps waiting and tries again every minute, saying why in the dashboard.
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
DEFAULTS = dict(enabled=True, after_hours=12.0, stop_original=True, bed_threshold=0.005)
BED_FRESH = 600          # a bed check is reused for 10 minutes
AUTHOR = 'AI auto reprint'


def settings_for(raw):
    s = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    return dict(enabled=bool(s['enabled']), after_hours=min(168.0, max(0.0, float(s['after_hours']))),
                stop_original=bool(s['stop_original']), bed_threshold=min(0.5, max(0.001, float(s['bed_threshold']))))


def safe_name(name):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60] or 'printer'


class AutoReprint:
    def __init__(self, core, engine, settings, python='/usr/bin/python3', clock=time.time):
        self.core, self.engine, self.settings, self.python, self.clock = core, engine, settings_for(settings), python, clock
        data = Path(getattr(core, 'DATA_DIR', tempfile.gettempdir()))
        self.beds, self.path = data / 'beds', data / 'ai-reprints.json'
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

    def _plate_mapping(self, job, path, target):
        """(use_ams, mapping, reason) for the job on the target's loaded trays; reason is '' when it fits."""
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
        if not suggestion['complete'] or any(r['match'] != 'exact' for r in suggestion['rows']):
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
            row = dict(name=name, display=self.core.display_name(name) if hasattr(self.core, 'display_name') else name,
                       ok=False, reason='', bed='')
            check = job_transfer.check(self.core, job, name)
            if check['compatible'] is not True:
                row['reason'] = 'different printer model' if check['compatible'] is False else 'model unknown'
            elif self.engine.store.active(name):
                row['reason'] = 'busy with another queue job'
            else:
                try:
                    self.engine.ready(name)
                except ValueError as exc:
                    row['reason'] = str(exc).split('.')[0].lower()
            if not row['reason'] and job.get('asset'):
                row['use_ams'], row['mapping'], row['reason'] = self._plate_mapping(job, job['asset'], name)
            if not row['reason']:
                bed = await self.bed_state(name, refresh)
                row['bed'] = bed['state']
                if bed['state'] != 'clear':
                    row['reason'] = 'parts on the bed' if bed['state'] == 'parts' else f"bed not confirmed empty: {bed['detail']}"
            row['ok'] = not row['reason']
            results.append(row)
        results.sort(key=lambda r: (not r['ok'], r['name']))
        self.last[job['printer']] = (self.clock(), self.summary(results))
        return results

    @staticmethod
    def summary(rows):
        ok = [r['display'] for r in rows if r['ok']]
        if ok:
            return 'Available for a reprint: ' + ', '.join(ok) + ' (bed checked empty).'
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
        self.pending[job['id']] = dict(printer=name, since=self.clock(), label=job['label'], note='')
        self.save()

    def due(self, job_id):
        entry = self.pending.get(job_id)
        return entry['since'] + self.settings['after_hours'] * 3600 if entry else None

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
        """Every minute: drop dealt-with prints, reprint the ones whose countdown ran out."""
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
            asset = None
            if job_transfer.needs_fetch(self.core, job):   # the file is only on the failed printer: copy it first
                uploads = Path(getattr(self.core, 'DATA_DIR', tempfile.gettempdir())) / 'uploads'
                asset = await asyncio.to_thread(job_transfer.fetch, self.core, job, uploads)
                target['use_ams'], target['mapping'], reason = self._plate_mapping(job, asset, target['name'])
                if reason:
                    Path(asset).unlink(missing_ok=True)
                    raise ValueError(f"{target['display']}: {reason}.")
            if self.settings['stop_original'] and self.core.state_data(name)[0] in ('PAUSE', 'RUNNING'):
                await self.engine.control(name, 'stop', author)
            self.engine.store.set_status(job['id'], 'failed', f"AI: failed print, reprinted on {target['display']} • {author}")
            copy = job_transfer.send(self.core, self.engine.store, job['id'], target['name'], target.get('use_ams', False),
                                     target.get('mapping', ''), False, author, asset=asset)
            await self.engine.start(copy['id'], True, author)
            self.pending.pop(job['id'], None)
            self.save()
            await self.core.notify(target['name'], '🤖 AI reprint started', f"{job['label']} failed on {name} and is now printing here. "
                                   f"The camera checked this bed was empty. • {author}", getattr(self.core, 'BLUE', 0x3498DB), True)
            return copy

    def state(self, name):
        job_id, entry = self.for_printer(name)
        checked, summary = self.last.get(name, (None, ''))
        base = dict(enabled=self.settings['enabled'], after_hours=self.settings['after_hours'], available=summary, checked=checked)
        if not job_id:
            return dict(base, pending=False)
        return dict(base, pending=True, job=job_id, label=entry['label'], due=self.due(job_id), note=entry.get('note', ''))
