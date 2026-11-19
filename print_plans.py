"""Print plans: change a printer's speed, temperatures or fans when its print reaches a percentage or a layer.

For example: at 10 % speed 277 %, at layer 60 back to normal speed. Made with /printplan in Discord or in the printer
panel of the dashboard.

- A plan made while the printer prints belongs to that print; made while it's idle, to the next print it starts. It
  ends with that print (finished, failed or cancelled), and Activity says what it did.
- Every few seconds each plan's waiting steps are checked against the printer's progress (mc_percent) and layer
  (layer_num). A step runs once, through the same printer controls as the dashboard and Discord (printer_controls).
  When several steps of the same kind are reached at once (the Pi was off for a while), only the last one runs.
- A step that's already passed when it's added (at 10 % when the print is at 40 %) doesn't run: use /speed now.
- Speed: silent, standard (normal), sport or ludicrous (Bambu's profiles: 50, 100, 124, 166 %), or any percentage
  from 10 to 300 (the G-code speed override, M220: how far a printer goes is up to its firmware).

Plans are kept in the database (print_plans), so a restart doesn't lose them.
"""
import asyncio
import json
import re
import secrets
import time

from printer_controls import SPEED_PERCENTS, prepare

ACTIONS = {'speed': 'Speed', 'nozzle': 'Nozzle temperature', 'bed': 'Bed temperature', 'fan': 'Part cooling fan',
           'chamber': 'Chamber temperature'}
PERMISSION = {'speed': 'speed', 'nozzle': 'temperature', 'bed': 'temperature', 'fan': 'fan', 'chamber': 'chamber'}   # Discord
PRINTING = ('RUNNING', 'PAUSE')
ENDED = ('FINISH', 'FAILED', 'IDLE')
MAX_STEPS = 20
EVERY = 5


def parse_at(text):
    """'10%', '10', 'layer 60', 'L60' → ('percent', 10) or ('layer', 60)."""
    t = str(text or '').strip().lower().replace(' ', '')
    m = re.fullmatch(r'(?:at)?(\d{1,3})(?:%|percent)?', t)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 99:
            raise ValueError('A percentage from 1 to 99.')
        return 'percent', n
    m = re.fullmatch(r'(?:at)?(?:layer|l)#?(\d{1,5})', t)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise ValueError('Layers start at 1.')
        return 'layer', n
    raise ValueError('When: a percentage like 10% or a layer like "layer 60".')


def parse_change(action, value):
    """(control kind, value) for printer_controls, from the action and what was typed."""
    if action not in ACTIONS:
        raise ValueError('Change: speed, nozzle, bed, fan or chamber.')
    text = str(value if value is not None else '').strip().lower()
    if action == 'speed':
        word = {'normal': 'standard', 'default': 'standard', 'reset': 'standard', 'quiet': 'silent', 'fast': 'sport'}.get(text, text)
        if word in SPEED_PERCENTS:
            return 'speed', word
        m = re.fullmatch(r'(\d{1,3})\s*%?', text)
        if not m:
            raise ValueError('Speed: silent, normal, sport, ludicrous, or a percentage like 277%.')
        n = int(m.group(1))
        named = {v: k for k, v in SPEED_PERCENTS.items()}
        return ('speed', named[n]) if n in named else ('speed_percent', n)   # 100 % is the normal profile
    m = re.fullmatch(r'(\d{1,3})\s*(?:%|°c|c)?', text)
    if not m:
        raise ValueError(f'{ACTIONS[action]}: a number.')
    return ('fan_part' if action == 'fan' else action), int(m.group(1))


def when(step):
    return f"{step['at']} %" if step['at_kind'] == 'percent' else f"layer {step['at']}"


class PrintPlans:
    def __init__(self, core, store, controls, every=EVERY, clock=time.time):
        self.core, self.store, self.controls, self.every, self.clock = core, store, controls, every, clock
        self.task = None
        store.db.executescript('CREATE TABLE IF NOT EXISTS print_plans (printer TEXT PRIMARY KEY, plan TEXT NOT NULL)')
        store.db.commit()

    # ---- storage ----

    def get(self, printer):
        row = self.store.db.execute('SELECT plan FROM print_plans WHERE printer=?', (printer,)).fetchone()
        return json.loads(row['plan']) if row else None

    def save(self, printer, plan):
        with self.store.db:
            if plan is None:
                self.store.db.execute('DELETE FROM print_plans WHERE printer=?', (printer,))
            else:
                self.store.db.execute('INSERT OR REPLACE INTO print_plans(printer,plan) VALUES(?,?)', (printer, json.dumps(plan)))

    def printers(self):
        return [r['printer'] for r in self.store.db.execute('SELECT printer FROM print_plans')]

    # ---- the print a plan belongs to ----

    @staticmethod
    def print_of(data):
        """Which print this is: its file. (The same file printed again is told apart by the printer finishing in
        between: a plan ends when its print does.) Other fields fill in at different moments of a print's start."""
        return str(data.get('subtask_name') or '')

    @staticmethod
    def progress(data):
        def num(key):
            try:
                return int(float(data.get(key)))
            except (TypeError, ValueError):
                return None
        return {'percent': num('mc_percent'), 'layer': num('layer_num')}

    # ---- changing a plan ----

    def add(self, printer, at, action, value, author):
        if printer not in self.core.names():
            raise ValueError('Unknown printer.')
        at_kind, at_value = parse_at(at)
        kind, control_value = parse_change(action, value)
        _, _, label = prepare(self.core, printer, kind, control_value)   # the same checks as the controls (limits, printer model)
        state, _, data, _ = self.core.state_data(printer)
        plan = self.get(printer) or dict(print=None, steps=[], created=self.clock(), author=author)
        if state in PRINTING and not plan['print']:
            plan['print'] = self.print_of(data)   # this print
        waiting = [s for s in plan['steps'] if s['state'] == 'waiting']
        if len(waiting) >= MAX_STEPS:
            raise ValueError(f'A plan has at most {MAX_STEPS} waiting steps.')
        if at_kind == 'layer' and str(data.get('total_layer_num') or '').isdigit() and state in PRINTING and at_value > int(data['total_layer_num']):
            raise ValueError(f"This print has {data['total_layer_num']} layers.")
        step = dict(id=secrets.token_hex(3), at_kind=at_kind, at=at_value, action=action, kind=kind, value=control_value,
                    label=label, state='waiting', note='', author=author)
        now = self.progress(data).get(at_kind)
        if state in PRINTING and now is not None and now >= at_value:
            step.update(state='skipped', note=f"the print was already at {when(dict(at_kind=at_kind, at=now))}")
        plan['steps'].append(step)
        plan['steps'].sort(key=lambda s: (s['at_kind'] != 'percent', s['at']))
        self.save(printer, plan)
        self.store.event(printer, 'Print plan', f"At {when(step)}: {label}{' (already passed: not run)' if step['state'] == 'skipped' else ''} • {author}")
        return plan, step

    def remove(self, printer, step_id, author):
        plan = self.get(printer)
        if not plan or not any(s['id'] == step_id for s in plan['steps']):
            raise ValueError('No such step in this printer\'s plan.')
        step = next(s for s in plan['steps'] if s['id'] == step_id)
        plan['steps'] = [s for s in plan['steps'] if s['id'] != step_id]
        self.save(printer, plan if plan['steps'] else None)
        self.store.event(printer, 'Print plan', f"Removed: at {when(step)} {step['label']} • {author}")
        return plan if plan['steps'] else None

    def clear(self, printer, author):
        had = self.get(printer)
        self.save(printer, None)
        if had:
            self.store.event(printer, 'Print plan cleared', author)
        return bool(had)

    def public(self, printer):
        """For the dashboard and Discord: the plan, and whether it's for this print or the next one."""
        plan = self.get(printer)
        if not plan:
            return None
        state, _, data, _ = self.core.state_data(printer)
        current = state in PRINTING and plan['print'] == self.print_of(data)
        return dict(steps=plan['steps'], current=current, waiting_for_print=not plan['print'], progress=self.progress(data) if current else None)

    # ---- running ----

    async def check(self, printer):
        plan = self.get(printer)
        if not plan:
            return []
        state, _, data, connected = self.core.state_data(printer)
        if not connected:
            return []
        this = self.print_of(data)
        if not plan['print']:
            if state not in PRINTING:
                return []   # waiting for the next print
            plan['print'] = this
            self.save(printer, plan)
        elif state in ENDED or (state in PRINTING + ('PREPARE',) and this and this != plan['print']):
            await self.finish(printer, plan, state)
            return []
        if state not in PRINTING:
            return []
        now = self.progress(data)
        reached = [s for s in plan['steps'] if s['state'] == 'waiting' and now.get(s['at_kind']) is not None and now[s['at_kind']] >= s['at']]
        if not reached:
            return []
        # Several of one kind reached together (the Pi was off, or a jump in progress): only the last one counts.
        last = {}
        for s in reached:
            last[s['kind'].split('_')[0]] = s
        ran = []
        for s in reached:
            if last[s['kind'].split('_')[0]] is not s:
                s.update(state='skipped', note='a later step of the same kind was reached at the same time')
                continue
            try:
                message = self.controls.apply(printer, s['kind'], s['value'], confirmed=True, author=f"print plan ({s['author']})")
                s.update(state='done', note=str(message)[:200], at_time=self.clock())
                ran.append(s)
            except ValueError as exc:
                s.update(state='failed', note=str(exc)[:200])
        self.save(printer, plan)
        name = self.core.display_name(printer) if hasattr(self.core, 'display_name') else printer
        for s in ran:
            await self.core.notify(printer, f'⏩ {name}: print plan', f"At {when(s)}: {s['label']}", self.core.BLUE)
        for s in reached:
            if s['state'] == 'failed':
                await self.core.notify(printer, f'⚠️ {name}: print plan step failed', f"At {when(s)}: {s['label']}: {s['note']}", self.core.YELLOW)
        return ran

    async def finish(self, printer, plan, state):
        done = sum(s['state'] == 'done' for s in plan['steps'])
        self.save(printer, None)
        how = {'FINISH': 'finished', 'FAILED': 'failed or was cancelled'}.get(state, 'ended')
        self.store.event(printer, 'Print plan ended', f"The print {how}: {done} of {len(plan['steps'])} changes made.")

    async def run(self):
        while True:
            for printer in self.printers():
                try:
                    await self.check(printer)
                except Exception as exc:
                    self.core.log.warning('Print plan for %s: %s', printer, exc)
            await asyncio.sleep(self.every)
