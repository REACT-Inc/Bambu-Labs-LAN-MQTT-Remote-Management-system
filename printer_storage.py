"""The printer's own storage, its MicroSD card or USB flash drive: whether it's in and usable.

A Bambu printer prints a file sent over the network from its storage (printer_models "storage"). Its reports say what
state that storage is in, read here the way Bambu Studio reads them (DeviceManager.cpp parse_home_flag and
parse_new_info, DeviceCore/DevStorage):
    0 none inserted, 1 ready, 2 can't be read (Studio: "abnormal", it usually needs formatting), 3 read-only
- from bits 12-13 of the hex "aux" flags when the printer sends the newer report ("cfg", "fun", "aux" and "stat",
  e.g. the H2D),
- else bits 8-9 of "home_flag",
- else the "sdcard" true / false.
A printer that reports none of them is never held, and neither is one with "storage_check": false in its config.json
entry.

Like Bambu Studio in LAN mode (SelectMachineDialog), the queue doesn't send a print to a printer whose storage can't be
read or is read-only, nor to one without storage unless it reports it can print without (bit 0 of "fun2").

There is no MQTT command to format the storage: Bambu Studio has none (it only reads the state), and its file browser
also says to format it on the printer. So the app says what's wrong and how to fix it on the printer's screen.
"""
import time

import printer_models

NONE, READY, UNREADABLE, READ_ONLY = 'none', 'ready', 'unreadable', 'read_only'
STATES = (NONE, READY, UNREADABLE, READ_ONLY)   # index: the 2-bit value the printer reports
SETTLE = 15   # seconds a new state has to last before it's announced (a drive mounting at boot or while formatting)


def flags(value):
    """A report's hex flags string ("aux", "fun2") as a number, like Bambu Studio's DevUtil; None if it isn't one."""
    text = str('' if value is None else value).strip()
    if text[:2].lower() == '0x':
        text = text[2:]
    digits = ''.join(c for c in text if c in '0123456789abcdefABCDEF')
    return int(digits, 16) if digits else None


def state(data):
    """'ready', 'none', 'unreadable' or 'read_only' as the printer reports its storage, or None when it doesn't say."""
    if not isinstance(data, dict):
        return None
    if all(key in data for key in ('cfg', 'fun', 'aux', 'stat')):
        aux = flags(data.get('aux'))
        if aux:   # empty or 0: not filled in, so the older fields decide
            return STATES[aux >> 12 & 3]
    flag = data.get('home_flag')
    if not isinstance(flag, bool):
        try:
            flag = int(flag or 0)
        except (TypeError, ValueError):
            flag = 0
        if flag:   # 0: not filled in (a real printer always sets some of its bits)
            return STATES[flag >> 8 & 3]
    if isinstance(data.get('sdcard'), bool):
        return READY if data['sdcard'] else NONE
    return None


def prints_without(data):
    """Whether the printer says it can print a file sent over the LAN without storage (bit 0 of "fun2", Bambu Studio's
    is_support_print_with_emmc)."""
    fun2 = flags(data.get('fun2')) if isinstance(data, dict) and isinstance(data.get('fun2'), str) else None
    return bool(fun2 and fun2 & 1)


def thing(model):
    return {'usb': 'USB drive', 'sd': 'MicroSD card'}.get(model['storage'], 'MicroSD card or USB drive')


def format_steps(model):
    """How to format this model's storage. The H2 series has a button for it (Bambu wiki, HMS 0500-0100-0003-0006)."""
    if model['series'] == 'H':
        return 'Format it on the printer: Settings → USB Storage → Format External Storage (this erases it).'
    return "Format it on the printer's screen, or on a computer as FAT32 or exFAT (this erases it)."


def texts(model, current):
    """(title, short, sentence, detail) for a storage state that stops prints, e.g. ("Can't read the USB drive",
    "can't read the USB drive", "The H2D can't read its USB drive.", what to do)."""
    kind, name = thing(model), model['name'] if model['key'] else 'printer'
    if current == NONE:
        return (f'No {kind}', f'no {kind}', f'The {name} has no {kind}.',
                f"It needs one to print files sent from here: put one in{' (FAT32 or exFAT)' if model['storage'] == 'usb' else ''}.")
    if current == UNREADABLE:
        return (f"Can't read the {kind}", f"can't read the {kind}", f"The {name} can't read its {kind}.",
                f"{format_steps(model)} If it still can't be read, try another {kind}.")
    return (f'{kind[0].upper()}{kind[1:]} is read-only', f'{kind} is read-only', f"The {name}'s {kind} is read-only.",
            f'That happens when a save fails, often during a timelapse. {format_steps(model)} '
            'If it happens again, try a faster one.')


def problem(model, data, config=None):
    """What stops this printer printing from its storage, or None:
    {'state', 'title', 'short', 'sentence', 'detail'}. `config` is the printer's config.json entry."""
    if (config or {}).get('storage_check') is False:
        return None
    current = state(data)
    if current in (None, READY) or current == NONE and prints_without(data):
        return None
    title, short, sentence, detail = texts(model, current)
    return dict(state=current, title=title, short=short, sentence=sentence, detail=detail)


def for_printer(core, name, data=None):
    """problem() for a printer the app knows, from its latest report unless `data` is given."""
    config = getattr(core, 'printer_config', lambda n: None)(name) or {}
    if data is None:
        data = core.state_data(name)[2]
    return problem(printer_models.info(config, name), data, config)


class StorageProblem(ValueError):
    """Raised by the queue (Engine.ready) when a printer's storage stops the print; `short` is the reason in a few
    words, for lists such as the AI reprint's available printers."""
    def __init__(self, found):
        super().__init__(f"{found['sentence']} {found['detail']}")
        self.short = found['short']


class Watch:
    """Announces a printer's storage going wrong, and coming right again, once the new state has lasted SETTLE seconds,
    so a drive mounting after a restart or while it's formatted doesn't send a burst of alerts. The first report is
    only remembered: what's already wrong shows on the dashboard instead."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.told, self.seen = clock, {}, {}

    def update(self, name, model, data, config=None):
        """(title, description, level) to announce, level 'warn' or 'ok', or None."""
        if (config or {}).get('storage_check') is False:
            return None
        current, now = state(data), self.clock()
        if current is None:
            return None
        if current == NONE and prints_without(data):
            current = READY   # no storage, but it doesn't need any
        if name not in self.told:
            self.told[name], self.seen[name] = current, (current, now)
            return None
        if current != self.seen[name][0]:
            self.seen[name] = (current, now)
        before = self.told[name]
        if current == before or now - self.seen[name][1] < SETTLE:
            return None
        self.told[name] = current
        if current != READY:
            title, _, _, detail = texts(model, current)
            return f'💾 {title}', detail, 'warn'
        kind = thing(model)
        return f'💾 {kind[0].upper()}{kind[1:]} ready', f"The printer can use its {kind} again: queued jobs can start.", 'ok'
