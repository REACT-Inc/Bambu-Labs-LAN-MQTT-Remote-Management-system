"""Clear printer errors and health (HMS) alerts from the dashboard and Discord (#34).

What a clear does, matching Bambu Studio (DeviceErrorDialog.cpp / DeviceManager.cpp):
- **Print error** (`print_error` in the report): the dialog's OK button sends
      {"print": {"command": "clean_print_error", "subtask_id": <report subtask_id>, "print_error": <code>}}
  and closing the dialog sends
      {"system": {"command": "uiop", "name": "print_error", "action": "close", "source": 1, "type": "dialog",
                  "err": "<code as 8 hex digits>"}}
  Both are sent. The printer drops the error if its cause is gone; otherwise it keeps reporting it.
- **HMS alerts** (`hms` list): Studio has no plain "clear" for these. Its only printer command, `idle_ignore`
  ("don't remind me next time"), mutes that alert on the printer for good, so it is never sent here. Dismissing an
  HMS alert hides it in the dashboard until the printer stops reporting it (it shows again if it comes back).

Every clear is confirmed first and logged (Activity and the service log) with who did it, the printer and the
error. The result is honest: it waits for the printer's next reports and only says "Cleared" once the error is gone.
Clearing never starts a job. After clearing, a printer left in FAILED by a finished or failed job can start the next
queued job without "Start ignoring error" (the plate-clear confirmation is still required).
"""
import asyncio
import json
import time

from printer_errors import errors as decode_errors

WAIT = 20   # seconds to wait for the printer to stop reporting a cleared error


def model_of(core, name):
    printer = core.printer_config(name) or {} if hasattr(core, 'printer_config') else {}
    return printer.get('error_model') or str(printer.get('serial', ''))[:3]


class Alerts:
    def __init__(self, core, store, clock=time.time, sleep=asyncio.sleep):
        self.core, self.store, self.clock, self.sleep = core, store, clock, sleep
        self.failed_cleared = {}   # name -> subtask_id of the FAILED print whose error was cleared (ready_after_clear)

    # ---- what the printer reports --------------------------------------------------------------------------------
    def dismissed(self, name):
        return set((self.core.settings.get('dismissed_hms') or {}).get(name) or [])

    def current(self, name, include_dismissed=False):
        """The printer's error and HMS alerts: [{'id', 'kind', 'code', 'message', 'url', 'dismissed'}]."""
        _, error, data, _ = self.core.state_data(name)
        dismissed = self.dismissed(name)
        alerts = []
        for entry in decode_errors(error, data, model_of(self.core, name)):
            kind = 'error' if len(entry['code']) == 8 else 'hms'
            alerts.append(dict(id=f"{kind}:{entry['code']}", kind=kind, code=entry['code'], message=entry['message'],
                               url=entry['url'], dismissed=kind == 'hms' and entry['code'] in dismissed))
        self.forget_gone(name, {a['code'] for a in alerts if a['kind'] == 'hms'})
        return alerts if include_dismissed else [a for a in alerts if not a['dismissed']]

    def forget_gone(self, name, reported):
        """An HMS alert the printer no longer reports is forgotten, so it shows again if it comes back."""
        dismissed = self.dismissed(name)
        if dismissed and not dismissed <= reported:
            self.save_dismissed(name, dismissed & reported)

    def save_dismissed(self, name, codes):
        everything = dict(self.core.settings.get('dismissed_hms') or {})
        if codes:
            everything[name] = sorted(codes)
        else:
            everything.pop(name, None)
        self.core.save_settings({**self.core.settings, 'dismissed_hms': everything})

    # ---- clearing ------------------------------------------------------------------------------------------------
    def payloads(self, data, code):
        """Bambu Studio's two messages for clearing print error `code` (8 hex digits)."""
        number = int(code, 16)
        return [
            {'print': {'command': 'clean_print_error', 'subtask_id': str(data.get('subtask_id') or ''), 'print_error': number}},
            {'system': {'command': 'uiop', 'name': 'print_error', 'action': 'close', 'source': 1, 'type': 'dialog', 'err': f'{number:08X}'}},
        ]

    def send(self, name, payloads):
        if getattr(self.core, 'EXAMPLE_MODE', False):
            data = self.core.EXAMPLE_DATA[name]   # demo: the printer accepts the clear
            for key in ('print_error', 'error'):
                if key in data:
                    data[key] = 0
            if data.get('state') == 'FAILED':
                data['state'] = 'IDLE'
            return
        client = self.core.clients.get(name)
        if not client or not client.is_connected():
            raise ValueError('Printer disconnected.')
        topic = f"device/{self.core.printer_config(name)['serial']}/request"
        for payload in payloads:
            root = next(iter(payload))
            body = {root: dict(payload[root], sequence_id=str(time.time_ns() % 1000000000))}
            if client.publish(topic, json.dumps(body), qos=1).rc != 0:
                raise ValueError('Sending the clear to the printer failed. Try again.')

    async def clear(self, name, ids, confirmed, author='unknown', wait=WAIT):
        """Clear the alerts with these ids ('all' for every one). Returns {'cleared', 'still', 'message'}."""
        if confirmed is not True:
            raise ValueError('Confirm that you checked the printer: clearing only dismisses the message, it does not fix the cause.')
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        state, _, data, connected = self.core.state_data(name)
        alerts = self.current(name)
        chosen = alerts if ids == 'all' else [a for a in alerts if a['id'] in set(ids or [])]
        if not chosen:
            raise ValueError('The printer no longer reports that alert.' if ids != 'all' else 'The printer reports no error or alert to clear.')
        errors = [a for a in chosen if a['kind'] == 'error']
        hms = [a for a in chosen if a['kind'] == 'hms']
        if errors and not connected:
            raise ValueError('Printer is offline.')
        for alert in errors:
            self.send(name, self.payloads(data, alert['code']))
        if hms:
            self.save_dismissed(name, self.dismissed(name) | {a['code'] for a in hms})
        still = []
        if errors:
            deadline = self.clock() + wait
            while True:
                reported = {a['code'] for a in self.current(name) if a['kind'] == 'error'}
                still = [a for a in errors if a['code'] in reported]
                if not still or self.clock() >= deadline:
                    break
                await self.sleep(1)
        cleared = [a for a in chosen if a not in still]
        if state == 'FAILED' and errors and not still:
            self.failed_cleared[name] = str(data.get('subtask_id') or '')   # this failed print, not a later one
        for alert in cleared:
            self.store.event(name, 'Printer error cleared' if alert['kind'] == 'error' else 'Health alert dismissed',
                             f"{alert['code']} {alert['message']} • {author}")
        for alert in still:
            self.store.event(name, 'Printer still reports error', f"{alert['code']} {alert['message']} • {author}")
        parts = []
        if [a for a in cleared if a['kind'] == 'error']:
            parts.append('Cleared: the printer no longer reports ' + ('these errors.' if len(errors) - len(still) > 1 else 'the error.'))
        if hms:
            parts.append(f"Dismissed {len(hms)} health alert{'s' if len(hms) > 1 else ''} in the dashboard (it shows again if the printer reports it again).")
        if still:
            parts.append('The printer still reports ' + ', '.join(a['code'] for a in still) + ': fix the cause on the printer, then clear it again.')
        return dict(cleared=[a['id'] for a in cleared], still=[a['id'] for a in still], message=' '.join(parts))

    def ready_after_clear(self, name, state, error, data):
        """Whether a FAILED printer may start the next queued job normally: only when its error was cleared here for
        this same print (a later failed print needs clearing again) and it reports no error any more."""
        if state != 'FAILED' or error or name not in self.failed_cleared:
            return False
        return self.failed_cleared[name] == str(data.get('subtask_id') or '')
