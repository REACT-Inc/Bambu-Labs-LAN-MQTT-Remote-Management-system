"""Swapmod: swap the build plate by running a swap print file, then carry on with the queue.

Per printer there are only two settings: Swapmod on/off, and the **swap print file** (the sliced .3mf you print to swap
the plate). Nothing counts plates or approves batches. It works off the normal start-print machinery:

1. You start a job from the queue as usual.
2. When it **finishes**, the swap file is started as a normal queue job (uploaded and started the same way).
3. When the swap print finishes, the **next waiting job** for that printer starts on its own, if there is one.
4. After a **failed** print it doesn't swap on its own: the cause (a clog, a run-out) would just repeat. You're told,
   and **Swap plate now** runs the swap whenever you want.

Offered for the A1 family (A1 / A1 mini), the printers Swapmod kits fit (#17).
"""
import asyncio
import hashlib
import shutil
from pathlib import Path

import printer_models

SWAPMOD_MODELS = ('a1mini', 'a1')
NOT_A_SERIES = ('Swapmod is only available for Bambu Lab A-series printers (A1 / A1 mini). If this is one, set its "model" '
                'in config.json (for example "A1 mini").')
SETTLE = 5   # seconds after a print ends before the next start, so the printer has reported its new state


def a_series_model(core, name):
    """'A1 mini' or 'A1' for A1-family printers, otherwise None (config "model", else the serial, else the name)."""
    key = printer_models.printer(core, name)['key']
    return printer_models.label(key) if key in SWAPMOD_MODELS else None


class PlateSwap:
    def __init__(self, core, store, settle=SETTLE):
        self.core, self.store, self.settle = core, store, settle
        db = store.db
        db.execute('''CREATE TABLE IF NOT EXISTS plate_swap (
            printer TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0)''')
        columns = {r[1] for r in db.execute('PRAGMA table_info(plate_swap)')}
        for column, kind in (('swap_file', "TEXT NOT NULL DEFAULT ''"), ('swap_name', "TEXT NOT NULL DEFAULT ''"),
                             ('swap_plate', 'INTEGER NOT NULL DEFAULT 1')):
            if column not in columns:
                db.execute(f'ALTER TABLE plate_swap ADD COLUMN {column} {kind}')
        db.commit()
        self.folder = Path(getattr(core, 'DATA_DIR', '.')) / 'swapmod'

    def available(self, name):
        return a_series_model(self.core, name) is not None

    def state(self, name):
        row = self.store.db.execute('SELECT enabled, swap_file, swap_name, swap_plate FROM plate_swap WHERE printer=?', (name,)).fetchone()
        enabled, path, label, plate = (bool(row[0]), row[1], row[2], row[3]) if row else (False, '', '', 1)
        has_file = bool(path) and Path(path).is_file()
        return dict(available=self.available(name), enabled=enabled, file=label if has_file else '', plate=plate,
                    ready=enabled and has_file, printer_model=a_series_model(self.core, name))

    def _save(self, name, **values):
        current = self.store.db.execute('SELECT enabled, swap_file, swap_name, swap_plate FROM plate_swap WHERE printer=?', (name,)).fetchone()
        row = dict(zip(('enabled', 'swap_file', 'swap_name', 'swap_plate'), current or (0, '', '', 1)), **values)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO plate_swap(printer, enabled, swap_file, swap_name, swap_plate) VALUES(?,?,?,?,?)',
                                  (name, int(row['enabled']), row['swap_file'], row['swap_name'], int(row['swap_plate'])))

    def configure(self, name, enabled, author):
        """Swapmod on or off for a printer."""
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        if type(enabled) is not bool:
            raise ValueError('Choose on or off.')
        if enabled and not self.available(name):
            raise ValueError(NOT_A_SERIES)
        self._save(name, enabled=enabled)
        self.store.event(name, 'Swapmod', f"{'On' if enabled else 'Off'} • {author}")
        return self.state(name)

    def set_file(self, name, source, label, plate, author):
        """Keep the swap print file (an uploaded, sliced .3mf) for this printer."""
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / (hashlib.sha1(name.encode()).hexdigest()[:12] + '.3mf')
        shutil.copyfile(source, target)
        self._save(name, swap_file=str(target), swap_name=label[:120], swap_plate=int(plate))
        self.store.event(name, 'Swapmod', f'Swap print file: {label} (plate {plate}) • {author}')
        return self.state(name)

    # The queue calls these; Swapmod no longer restricts or reserves anything.
    def check(self, job, dispatch=False):
        return None

    def reserve(self, job):
        return None

    def invalidate(self, name):
        return None

    # ---- the swap --------------------------------------------------------------------------------------------------
    def swap_job(self, name, author):
        """Queue the swap print at the front of this printer's queue."""
        from queueing import options
        row = self.store.db.execute('SELECT swap_file, swap_name, swap_plate FROM plate_swap WHERE printer=?', (name,)).fetchone()
        if not row or not row[0] or not Path(row[0]).is_file():
            raise ValueError('Choose the swap print file first (Swapmod in the printer panel).')
        opts = dict(options(row[2] or 1, False, '', 'textured_plate'), swap=True)
        job = self.store.add(name, 'Plate swap', row[0], row[1] or 'plate-swap.gcode.3mf', opts, author, bool(getattr(self.core, 'EXAMPLE_MODE', False)))
        self.store.move_to_front(job['id'])
        return job

    async def swap_now(self, engine, name, author):
        if not self.state(name)['ready']:
            raise ValueError('Turn Swapmod on and choose the swap print file first.')
        job = self.swap_job(name, author)
        try:
            return await engine.start(job['id'], True, author)
        except Exception:
            if self.store.get(job['id'])['status'] == 'queued':
                self.store.set_status(job['id'], 'cancelled', 'Plate swap could not start.')
            raise

    async def after(self, engine, name, job, state):
        """Called when a queue job on this printer ended (FINISH or FAILED)."""
        if not self.state(name)['ready']:
            return
        notify = getattr(self.core, 'notify', None)
        try:
            if job['options'].get('swap'):
                if state != 'FINISH':
                    if notify:
                        await notify(name, '♻️ Plate swap failed', 'The swap print did not finish. Check the printer; the queue waits.', self.core.RED)
                    return
                following = next((j for j in self.store.jobs(name) if j['status'] == 'queued' and j['id'] != job['id']
                                  and not j['options'].get('swap')), None)
                if not following:
                    if notify:
                        await notify(name, '♻️ Plate swapped', 'Fresh plate ready. Nothing else is waiting in the queue.', self.core.GREEN)
                    return
                await asyncio.sleep(self.settle)
                # The bed check AI (failureDetection/bed_model.py), when it's set up: hold the queue if it's sure the
                # swap left parts on the plate. When it's unsure or not set up, the queue carries on as before.
                gate = getattr(engine, 'bed_check', None)
                bed = await gate(name) if gate else {'state': 'unknown'}
                if bed.get('state') == 'parts':
                    if notify:
                        await notify(name, '♻️ Plate swapped, but the bed does not look clear',
                                     f"{following['label']} was not started: the bed check AI {bed.get('detail', 'sees parts on the bed')}. "
                                     'Check the printer, then start it from the queue (and tell the AI in the printer panel '
                                     'whether the bed was clear).', self.core.YELLOW)
                    return
                await engine.start(following['id'], True, 'Swapmod')
                if notify:
                    await notify(name, '♻️ Plate swapped, next print started', following['label'], self.core.GREEN)
            elif state == 'FINISH':
                await asyncio.sleep(self.settle)
                await self.swap_now(engine, name, 'Swapmod')
            elif notify:
                await notify(name, '♻️ Not swapping', f"{job['label']} failed, so the plate wasn't swapped automatically "
                             '(the cause would just repeat). Check the printer, then use Swap plate now.', self.core.YELLOW)
        except Exception as exc:
            if notify:
                await notify(name, '♻️ Swapmod stopped', f'Could not continue: {str(exc)[:300]}', self.core.RED)
