"""Printer errors: a real problem someone has to see to, or one that can be cleared by itself?

When a printer reports an error (its `print_error`), the app looks at it the way a person at the printer would:

1. **What it's about** (category(), from the code's message in Bambu's error catalog, printer_errors.py):
   - **someone has to see to it:** the filament or AMS, the nozzle or hotend, the bed or build plate, the printer's
     motion (homing, motors, lost steps), its hardware (temperatures, fans, sensors, power), a door or cover,
     something the printer's own AI or camera found, or its storage. An error that isn't recognised counts as one too.
   - **the printer itself is fine:** a cancelled print, a problem with the print file (it couldn't be read, it was
     sliced for something else, it didn't download), the printer was busy (updating, drying, loading filament), or
     the network.
2. **How far the print got** (observe()): the highest layer it reached. Before layer 1 nothing has been printed, so the
   bed is as it was when the print was started.
3. **The bed:** the bed check AI, else the comparison with the empty-bed picture taken when a print was last started
   (failureDetection/auto_reprint.py bed_state): clear, parts on it, or can't tell.

Then (decide()):
- an error someone has to see to is kept, whatever the bed looks like;
- otherwise, once the print had reached layer 1 it's cleared only when the bed check says the bed is clear: the failed
  print is probably still there, and the error stays until someone removes it;
- before layer 1, or with no print, it's cleared unless the bed check sees something on the bed.

It looks once per error, SETTLE seconds after the printer stopped (the toolhead parks, the camera sees the plate), and
clears only errors that appear while the app is watching: one that was already there when the app started is only
marked safe to clear. Clearing sends what the dashboard's Clear error sends (printer_alerts.py), and never starts
anything: the next queue job still needs someone to confirm the plate is clear. Each decision shows on the printer's
card and panel and in Discord's /printer with the reason, and goes in Activity; a cleared error sends an alert, and so
does one kept because the bed check sees parts. Settings → Printers → "Clear safe printer errors by themselves"
(settings.json "auto_clear_errors", on unless turned off) stops the clearing; the decisions still show.
"""
import asyncio
import logging
import re
import time

from printer_alerts import model_of
from printer_errors import lookup, number

log = logging.getLogger('printer-bot')

PRINTING = ('PREPARE', 'RUNNING', 'PAUSE', 'SLICING')
SETTLE = 20          # seconds after the printer stops before its error and bed are looked at
CONFIRM = 20         # seconds to wait for the printer to stop reporting a cleared error
SHOW_CLEARED = 3600  # how long "Cleared by itself" stays on the printer's card (or until its next print)
TITLES = {'checking': 'Looking into it', 'kept': 'Kept', 'safe': 'Safe to clear', 'cleared': 'Cleared by itself'}

# What an error is about, from its catalog message: (category, label, someone has to see to it, what it's about,
# pattern). The first match wins, so the order matters: "The print file is not available ... the storage media has
# been removed" is about storage, and "The nozzle diameter in sliced file ..." about the file.
RULES = [
    ('cancelled', 'Print cancelled', False, 'a cancelled print', r'\b(task|printing) was cancell?ed\b'),
    ('busy', 'Printer busy', False, 'the printer being busy',
     r'failed to start a new task|cannot start new task|not allowed to be sent|cannot be initiated while|'
     r'cannot be upgraded at the moment|currently upgrading'),
    ('storage', 'Storage', True, 'its storage', r'micro ?sd|sd card|usb flash drive|storage|file system'),
    ('file', 'Print file', False, 'the print file',
     r'print file|the file|sliced file|slicing|re-?slic|3mf|g-?code|file name|download(ing)? (a )?(file|print job)|'
     r'failed to download'),
    ('detection', "Printer's AI or camera", True, "something the printer's own AI or camera found",
     r'ai print monitoring|spaghetti|pile-up|clumping|foreign object|first layer|camera is (blocked|obstructed)'),
    ('door', 'Door or cover', True, 'a door or cover', r'\bdoor\b|\bcover\b'),
    ('nozzle', 'Nozzle or hotend', True, 'the nozzle or hotend', r'nozzle|hot ?end|extru|clog|heat ?break'),
    ('filament', 'Filament or AMS', True, 'the filament or AMS',
     r'filament|\bams\b|spool|ptfe|feeder|cutter|run out|ran out|tangle|purg'),
    ('bed', 'Bed or build plate', True, 'the bed or build plate',
     r'heat ?bed|heated bed|hot ?bed|build plate|print plate|platform|leveling|\bbed\b'),
    ('motion', 'Motion', True, "the printer's motion", r'homing|axis|motor|step|resonance|layer shift|belt|obstruct|motion'),
    ('hardware', 'Hardware', True, "the printer's hardware",
     r'temperature|\bfans?\b|sensor|power|voltage|board|communication|camera|laser|cutting|module|accessory|firmware|'
     r'self-(check|test)|uncertified|exception|malfunction'),
    ('network', 'Network', False, 'the network', r'cloud|router|network|binding|bound|authori[sz]ation|ip address|internet'),
]
FILAMENT = next(rule for rule in RULES if rule[0] == 'filament')
OTHER = ('other', 'Printer error', True, "something the app doesn't recognise", '')


def category(code, message):
    """(category, label, someone has to see to it, what it's about) for an error code and its catalog message."""
    if str(code).upper()[:2] in ('07', '12', '18'):   # the AMS's own errors (AMS, AMS lite, AMS HT, external spools)
        return FILAMENT[:4]
    text = str(message or '').lower()
    return next((rule[:4] for rule in RULES if re.search(rule[4], text)), OTHER[:4])


def decide(keep, about, layers, started, bed):
    """('keep' or 'clear', why) for an error about `about`, `layers` the highest layer the print reached, `started`
    whether the app saw the print start, `bed` the bed check {'state': 'clear'|'parts'|'unknown', 'detail'}."""
    if keep:
        return 'keep', f"It's about {about}, so someone has to see to it at the printer."
    state, detail = (bed or {}).get('state', 'unknown'), (bed or {}).get('detail') or 'no bed check'
    if layers >= 1:
        if state == 'clear':
            return 'clear', f"It's about {about}, and although the print got to layer {layers}, the bed check says the bed is clear."
        if state == 'parts':
            return 'keep', f'The print got to layer {layers} and the bed check sees parts on the bed. Remove them, then clear the error.'
        return 'keep', (f"The print got to layer {layers}, so it's probably still on the bed (the bed check can't tell: "
                        f'{detail}). Clear the bed, then clear the error.')
    if state == 'parts':
        return 'keep', 'Nothing was printed, but the bed check sees something on the bed. Check it, then clear the error.'
    how = 'the print stopped before its first layer' if started else 'no print was running'
    return 'clear', f"It's about {about}; {how}" + (', and the bed check says the bed is clear.' if state == 'clear' else '.')


def code_of(error):
    """A print_error as a number (0 when there is none or it can't be read)."""
    try:
        value = number(error) if error not in (None, '') else 0
    except (TypeError, ValueError):
        return 0
    return value if 0 < value <= 0xffffffff else 0


class Triage:
    def __init__(self, core, store, alerts, bed=None, clock=time.time, sleep=asyncio.sleep, settle=SETTLE, confirm=CONFIRM):
        """bed: async (printer) -> {'state', 'detail'}, the bed check; None without one (it can't tell)."""
        self.core, self.store, self.alerts, self.bed = core, store, alerts, bed
        self.clock, self.sleep, self.settle, self.confirm = clock, sleep, settle, confirm
        self.prints = {}     # printer -> {'id', 'layers', 'started', 'printing', 'running'}: the current or last print
        self.seen = {}       # printer -> the error in its last report
        self.pending = {}    # printer -> (code, clear it if safe): an error waiting for the printer to stop
        self.verdicts = {}   # printer -> what was decided about its error
        self.tasks = {}

    @property
    def enabled(self):
        value = self.core.settings.get('auto_clear_errors')
        return value if isinstance(value, bool) else True

    # ---- following the printers ----

    def observe(self, name, data):
        """Every report (Engine.telemetry): follows the print, and looks at a new error once the printer stops."""
        if getattr(self.core, 'EXAMPLE_MODE', False) or not isinstance(data, dict):
            return
        state, error = data.get('gcode_state'), code_of(data.get('print_error'))
        ident = str(data.get('subtask_id') or data.get('task_id') or '')
        try:
            layer = max(0, int(data.get('layer_num') or 0))
        except (TypeError, ValueError):
            layer = 0
        info = self.prints.setdefault(name, dict(id=ident, layers=0, started=False, printing=False, running=False))
        if state in PRINTING:
            if not info['printing'] or (ident and ident != info['id']):   # a new print: nothing on it printed yet
                info.update(id=ident, layers=0, started=True, running=False)
                self.forget(name)
            info['printing'] = True
            info['running'] = info['running'] or state in ('RUNNING', 'PAUSE')
        elif state is not None:
            info['printing'] = False
        # The layer is only trusted once the print runs (while it's preparing, the printer can still report the last
        # print's), or for a print the app didn't see start, whose reports are all it knows.
        if (info['running'] or not info['started']) and (not ident or ident == info['id']):
            info['layers'] = max(info['layers'], layer)
        if 'print_error' in data:
            previous = self.seen.get(name)
            self.seen[name] = error
            if not error:
                self.pending.pop(name, None)
                verdict = self.verdicts.get(name)
                if verdict and verdict['decision'] != 'cleared':
                    self.verdicts.pop(name, None)
            elif error != previous:
                # The first report only shows what's already there: it's looked at, but not cleared by itself.
                self.pending[name] = (error, previous is not None)
                self.verdicts[name] = self.checking(error)
        if name in self.pending and not info['printing'] and name not in self.tasks:
            code, act = self.pending.pop(name)
            self.tasks[name] = asyncio.create_task(self.examine(name, code, act))

    def forget(self, name):
        self.verdicts.pop(name, None)
        self.pending.pop(name, None)

    def checking(self, error):
        return dict(code=f'{error:08X}', message='', decision='checking', label='', bed={}, at=self.clock(),
                    why='The app looks at it once the printer has stopped.')

    # ---- looking at an error ----

    async def look(self, name):
        if not self.bed:
            return dict(state='unknown', detail='no bed check')
        try:
            return dict(await self.bed(name))
        except Exception as exc:
            return dict(state='unknown', detail=f'bed check failed ({type(exc).__name__})')

    def retry(self, name, code, act):
        """Look again at the next report if the printer still reports this error and isn't printing (it was offline)."""
        state, error = self.core.state_data(name)[:2]
        if code_of(error) == code and state not in PRINTING:
            self.pending.setdefault(name, (code, act))

    def still(self, name, code):
        """(state, data) if the printer still reports this error and hasn't started anything, else None."""
        state, error, data, connected = self.core.state_data(name)
        if not connected or state in PRINTING or code_of(error) != code:
            return None
        return state, data

    async def examine(self, name, code, act):
        try:
            await self.sleep(self.settle)
            if not self.still(name, code):
                self.retry(name, code, act)   # cleared or a print started: nothing to decide; offline: later
                return
            found = lookup(f'{code:08X}', model=model_of(self.core, name))
            key, label, keep, about = category(found['code'], found['message'])
            info = self.prints.get(name) or {}
            bed = None if keep else await self.look(name)
            decision, why = decide(keep, about, info.get('layers', 0), info.get('started', False), bed)
            now = self.still(name, code)
            if not now:
                self.retry(name, code, act)
                return
            verdict = dict(code=found['code'], message=found['message'], category=key, label=label, layers=info.get('layers', 0),
                           bed=bed or {}, why=why, at=self.clock())
            if decision == 'keep':
                verdict['decision'] = 'kept'
            elif act and self.enabled:
                verdict.update(await self.clear(name, code, now))
            else:
                verdict['decision'] = 'safe'
                if act:
                    verdict['why'] = why + ' Clearing by itself is turned off (Settings → Printers).'
            self.verdicts[name] = verdict
            await self.report(name, verdict)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Looking at the error on %s failed', name)
        finally:
            self.tasks.pop(name, None)

    async def clear(self, name, code, now):
        """Clear the error the way the dashboard's Clear error does, and wait for the printer to stop reporting it."""
        state, data = now
        text = f'{code:08X}'
        try:
            self.alerts.send(name, self.alerts.payloads(data, text))
        except ValueError as exc:
            return dict(decision='safe', failed=str(exc))
        deadline = self.clock() + self.confirm
        while self.clock() < deadline:
            await self.sleep(1)
            reported = self.core.state_data(name)
            if code_of(reported[1]) != code:
                if state == 'FAILED':   # like a clear from the dashboard: the next queued job starts normally
                    self.alerts.failed_cleared[name] = str(data.get('subtask_id') or '')
                return dict(decision='cleared')
        return dict(decision='kept', failed='the printer still reports it')

    async def report(self, name, verdict):
        """Activity, and an alert when it was cleared or the bed has parts on it."""
        detail = f"{verdict['code']} {verdict['message']} • {verdict['why']}"
        if verdict.get('failed'):
            title = 'Printer error not cleared'
            verdict['why'] += f" Clearing it didn't work ({verdict['failed']}): use Clear error, or clear it on the printer."
            detail += f" • not cleared: {verdict['failed']}"
        else:
            title = {'kept': 'Printer error kept', 'safe': 'Printer error safe to clear', 'cleared': 'Printer error cleared by itself'}[verdict['decision']]
        self.store.event(name, title, detail)
        notify = getattr(self.core, 'notify', None)
        if not notify:
            return
        if verdict['decision'] == 'cleared':
            await notify(name, '✅ Error cleared by itself', f"{verdict['message']} [{verdict['code']}]\n{verdict['why']}", getattr(self.core, 'GREEN', None))
        elif verdict['decision'] == 'kept' and verdict['bed'].get('state') == 'parts':
            await notify(name, '🧹 Parts on the bed', f"{verdict['message']} [{verdict['code']}]\n{verdict['why']}", getattr(self.core, 'YELLOW', None))

    # ---- what the dashboard and Discord show ----

    def condition(self, name, error=None, data=None):
        """What was decided about the printer's error, for its card, panel and Discord's /printer:
        {'decision', 'title', 'why', 'code', 'message', 'label', 'bed'}, or None."""
        verdict, code = self.verdicts.get(name), code_of(error)
        if not verdict:
            return None
        if not code:
            if verdict['decision'] != 'cleared' or self.clock() - verdict['at'] > SHOW_CLEARED:
                return None
        elif verdict['code'] != f'{code:08X}':
            return None
        return dict({k: verdict.get(k) for k in ('decision', 'why', 'code', 'message', 'label', 'bed')}, title=TITLES[verdict['decision']])

    def set_enabled(self, on):
        if type(on) is not bool:
            raise ValueError('Choose on or off.')
        self.core.save_settings({**self.core.settings, 'auto_clear_errors': on})
        self.store.event('', 'Printer errors', 'Safe printer errors now clear by themselves.' if on
                         else 'Printer errors no longer clear by themselves; the dashboard still says which are safe to clear.')

    async def stop(self, app=None):
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
