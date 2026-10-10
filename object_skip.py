"""Cancel single objects during a print, like Bambu Studio and Bambu Handy (#84).

The sliced .3mf lists each plate's objects in Metadata/slice_info.config:
    <plate> ... <object identify_id="75" name="Cube" skipped="false" /> ...
and the printer skips objects with the same MQTT command Bambu Studio sends:
    {"print": {"command": "skip_objects", "obj_list": [75, 92], "sequence_id": "..."}}
Already-skipped objects come back in the printer's report as "s_obj".

Objects are known for prints started from this app (queue or Print now), whose .3mf is on the Pi, and for prints
started elsewhere (Bambu Studio, Handy, the printer's screen) whose .3mf is on the printer's storage: it is copied to
the Pi once per print (fetch()). At least one object
must keep printing (skipping everything is what Stop is for). The firmware decides whether it can skip: a rejection
is reported in Activity and Discord like any other rejected control.
"""
import asyncio
import hashlib
import json
import re
import time
import zipfile
from pathlib import Path

import sliced_file


def plate_objects(path, plate):
    """[{'id': 75, 'name': 'Cube'}] for one plate of a sliced .3mf (names made unique with #2, #3...)."""
    try:
        with zipfile.ZipFile(path) as archive:
            data = sliced_file._read(archive, 'Metadata/slice_info.config')
    except (OSError, zipfile.BadZipFile):
        return []
    root = sliced_file._xml(data)
    if root is None:
        return []
    objects = []
    for element in root.findall('plate'):
        meta = sliced_file._metadata(element)
        if sliced_file._number(meta.get('index'), int) != int(plate):
            continue
        for item in element.findall('object'):
            identify = sliced_file._number(item.get('identify_id'), int)
            if identify is not None and str(item.get('skipped', 'false')).lower() != 'true':
                objects.append(dict(id=identify, name=str(item.get('name') or f'Object {identify}')[:80]))
    totals, numbered = {}, {}
    for item in objects:
        totals[item['name']] = totals.get(item['name'], 0) + 1
    for item in objects:   # several copies of one model share a name: number them
        if totals[item['name']] > 1:
            numbered[item['name']] = numbered.get(item['name'], 0) + 1
            item['name'] = f"{item['name']} #{numbered[item['name']]}"
    return objects


def skipped_ids(data):
    value = data.get('s_obj') or []
    return {int(v) for v in value if str(v).lstrip('-').isdigit()} if isinstance(value, list) else set()


def remote_candidates(data):
    """Where on the printer's storage the running print's .3mf may be, from its report."""
    names = []
    path = str(data.get('gcode_file') or '')
    if path.lower().endswith('.3mf'):
        names.append(re.sub(r'^/?(sdcard/)?', '', path))
    subtask = str(data.get('subtask_name') or '').strip()
    if subtask and '/' not in subtask and '..' not in subtask:
        stem = subtask[:-4] if subtask.lower().endswith('.3mf') else subtask
        for name in (f'{stem}.gcode.3mf', f'{stem}.3mf', f'cache/{stem}.gcode.3mf', f'cache/{stem}.3mf'):
            if name not in names:
                names.append(name)
    return [n for n in names if n and '..' not in n][:6]


def plate_of(data):
    match = re.search(r'plate_(\d+)', str(data.get('gcode_file') or ''))
    return int(match.group(1)) if match else 1


class ObjectSkip:
    RETRY = 300   # seconds before trying to fetch a print's file from the printer again

    def __init__(self, core, store, controls, download=None):
        self.core, self.store, self.controls = core, store, controls
        self.download = download
        self.folder = Path(getattr(core, 'DATA_DIR', '.')) / 'objects'
        self.fetched = {}   # name -> {'key', 'path', 'plate', 'label'} or {'key', 'failed_at'}

    def job(self, name):
        """The running print's file: the queue job started from here, else the file fetched from the printer."""
        job = self.store.active(name)
        if job and job.get('asset'):
            return job
        _, _, data, _ = self.core.state_data(name)
        got = self.fetched.get(name)
        if got and got.get('path') and got['key'] == self.key(data):
            return dict(asset=got['path'], label=got['label'], options={'plate': got['plate']})
        return None

    @staticmethod
    def key(data):
        return f"{data.get('subtask_id') or ''}|{data.get('gcode_file') or ''}|{data.get('subtask_name') or ''}"

    async def fetch(self, name):
        """For a print not started from this app, copy its .3mf from the printer once (in a worker thread)."""
        if getattr(self.core, 'EXAMPLE_MODE', False):
            return
        job = self.store.active(name)
        if job and job.get('asset'):
            return
        state, _, data, connected = self.core.state_data(name)
        if state not in ('RUNNING', 'PAUSE') or not connected:
            return
        key, got = self.key(data), self.fetched.get(name)
        if got and got['key'] == key and (got.get('path') or time.time() - got.get('failed_at', 0) < self.RETRY):
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        dest = self.folder / (hashlib.sha1(name.encode()).hexdigest()[:12] + '.3mf')
        download = self.download
        if download is None:
            import queueing
            download = queueing.download

        def copy():
            for remote in remote_candidates(data):
                dest.unlink(missing_ok=True)
                try:
                    download(self.core.printer_config(name), remote, dest)
                    return remote
                except Exception:
                    continue
            return None
        remote = await asyncio.to_thread(copy)
        if remote and plate_objects(dest, plate_of(data)):
            self.fetched[name] = dict(key=key, path=str(dest), plate=plate_of(data), label=str(data.get('subtask_name') or Path(remote).name))
        else:
            self.fetched[name] = dict(key=key, failed_at=time.time())

    def objects(self, name):
        """{'job': label or '', 'objects': [{'id', 'name', 'skipped'}], 'reason': why there's nothing to skip}."""
        state, _, data, _ = self.core.state_data(name)
        job = self.job(name)
        if not job:
            busy = state in ('RUNNING', 'PAUSE')
            return dict(job=str(data.get('subtask_name') or '') if busy else '', objects=[],
                        reason="The print file couldn't be read from the printer's storage (prints sent from the cloud keep it "
                               'internally), so its objects are unknown.' if busy else 'Objects can be cancelled while a print is running or paused.')
        objects = plate_objects(job['asset'], job['options'].get('plate', 1))
        if not objects:
            return dict(job=job['label'], objects=[], reason="The print file doesn't list its objects (slice it with a recent Bambu Studio).")
        done = skipped_ids(data)
        rows = [dict(item, skipped=item['id'] in done) for item in objects]
        reason = '' if state in ('RUNNING', 'PAUSE') else 'Objects can be cancelled while the print is running or paused.'
        return dict(job=job['label'], objects=rows, reason=reason)

    def skip(self, name, ids, confirmed, author):
        if confirmed is not True:
            raise ValueError('Confirm which objects to cancel.')
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise ValueError('Choose the objects to cancel.')
        state, _, data, connected = self.core.state_data(name)
        if not connected:
            raise ValueError('Printer is offline.')
        info = self.objects(name)
        if info['reason']:
            raise ValueError(info['reason'])
        known = {o['id']: o for o in info['objects']}
        if any(i not in known for i in ids):
            raise ValueError('Those objects are not on the plate being printed.')
        remaining = [o for o in info['objects'] if not o['skipped'] and o['id'] not in ids]
        if not remaining:
            raise ValueError('At least one object has to keep printing. To cancel everything, use Stop.')
        ids = sorted(set(ids) | skipped_ids(data))   # the printer expects the full list of skipped objects
        names = ', '.join(known[i]['name'] for i in sorted(set(ids)) if i in known and not known[i]['skipped'])
        label = f'Cancel objects: {names}'
        if self.core.EXAMPLE_MODE:
            self.core.EXAMPLE_DATA[name]['s_obj'] = ids
        else:
            client = self.core.clients.get(name)
            if not client or not client.is_connected():
                raise ValueError('Printer disconnected.')
            sequence = str(time.time_ns() % 1000000000)
            # Registered with the controls, so a firmware rejection is reported like any other control.
            self.controls.pending[sequence] = (name, 'skip_objects', label, time.monotonic())
            payload = {'print': {'command': 'skip_objects', 'obj_list': ids, 'sequence_id': sequence}}
            result = client.publish(f"device/{self.core.printer_config(name)['serial']}/request", json.dumps(payload), qos=1)
            if result.rc != 0:
                raise ValueError('Could not send the command to the printer.')
            if hasattr(self.core, 'request_report'):
                self.core.request_report(name)   # show the skipped objects sooner (#60)
        self.store.event(name, 'Demo control' if self.core.EXAMPLE_MODE else 'Objects cancelled', f'{label} • {author}')
        return label + (' • Demo only.' if self.core.EXAMPLE_MODE else ' • Sent; the printer skips them from the next layer.')
