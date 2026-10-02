"""Send a finished print to another printer's queue (#57, dashboard only).

G-code is sliced for one printer model, so a job only goes to a printer of the same model:
- the file's model comes from the .3mf (Metadata/project_settings.config "printer_model", or
  Metadata/slice_info.config "printer_model_id");
- the printer's model comes from its config.json "model", or its serial number.
A known mismatch is refused; if either model is unknown, the user has to confirm they checked it.
"""
import json
import re
import zipfile
from pathlib import Path

from queueing import TERMINAL, options

# Bambu Studio printer_model_id values (resources/profiles/BBL/machine/*.json "model_id").
MODEL_IDS = {'N1': 'a1mini', 'N2S': 'a1', 'C11': 'p1p', 'C12': 'p1s', 'BL-P001': 'x1c', 'BL-P002': 'x1',
             'C13': 'x1e', 'O1D': 'h2d'}
# First 3 characters of the serial number.
SERIALS = {'030': 'a1mini', '039': 'a1', '01S': 'p1p', '01P': 'p1s', '00M': 'x1c', '03W': 'x1e', '094': 'h2d'}
ALIASES = {'x1carbon': 'x1c', 'a1m': 'a1mini'}
NAMES = {'a1mini': 'A1 mini', 'a1': 'A1', 'p1p': 'P1P', 'p1s': 'P1S', 'x1c': 'X1 Carbon', 'x1': 'X1', 'x1e': 'X1E', 'h2d': 'H2D'}


def model_key(text):
    """'Bambu Lab A1 mini' / 'A1 mini' / 'A1M' -> 'a1mini'; '' for nothing."""
    key = re.sub(r'[^a-z0-9]', '', str(text or '').lower())
    key = key.removeprefix('bambulab')
    return ALIASES.get(key, key)


def label(key):
    return NAMES.get(key, key.upper() if key else 'unknown model')


def sliced_for(path):
    """The printer model a sliced .3mf was made for, as a model_key, or '' if it doesn't say."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if 'Metadata/project_settings.config' in names:
                try:
                    settings = json.loads(archive.read('Metadata/project_settings.config'))
                    if isinstance(settings, dict) and settings.get('printer_model'):
                        return model_key(settings['printer_model'])
                except ValueError:
                    pass
            if 'Metadata/slice_info.config' in names:
                match = re.search(rb'key="printer_model_id"\s+value="([^"]+)"', archive.read('Metadata/slice_info.config'))
                if match:
                    return MODEL_IDS.get(match.group(1).decode(errors='replace'), '')
    except (OSError, zipfile.BadZipFile):
        pass
    return ''


def printer_model(core, name):
    config = core.printer_config(name) or {}
    if config.get('model'):
        return model_key(config['model'])
    return SERIALS.get(str(config.get('serial') or '')[:3], '')


def check(core, job, target):
    """{'compatible': True/False/None, 'reason': text} for sending this job to the target printer."""
    if not job.get('asset'):
        return dict(compatible=False, reason="This job's file is stored on the printer, not the Pi, so it can't be sent elsewhere. Queue the file again for the other printer.")
    if not Path(job['asset']).is_file():
        return dict(compatible=False, reason='The uploaded file for this job is no longer on the Pi.')
    if target == job['printer']:
        return dict(compatible=False, reason='That is the printer it already printed on; use Queue again.')
    sliced, model = sliced_for(job['asset']), printer_model(core, target)
    if sliced and model:
        if sliced == model:
            return dict(compatible=True, reason=f'Sliced for the {label(sliced)}.')
        return dict(compatible=False, reason=f'Sliced for the {label(sliced)}, not the {label(model)}. Re-slice it for the {label(model)} in Bambu Studio.')
    missing = 'which printer the file was sliced for' if not sliced else f"this printer's model (set \"model\" in config.json)"
    return dict(compatible=None, reason=f"Couldn't tell {missing}; check the file was sliced for this printer.")


def targets(core, job):
    return [dict(name=name, display=core.display_name(name) if hasattr(core, 'display_name') else name,
                 model=label(printer_model(core, name)) if printer_model(core, name) else '', **check(core, job, name))
            for name in core.names() if name != job['printer']]


def send(core, store, job_id, target, use_ams, mapping, checked, author):
    """Add a copy of a finished job to another printer's queue. Returns the new job."""
    job = store.get(job_id)
    if job['status'] not in TERMINAL:
        raise ValueError('Only finished, failed or cancelled jobs can be sent to another printer.')
    if target not in core.names():
        raise ValueError('Choose a printer.')
    result = check(core, job, target)
    if result['compatible'] is False:
        raise ValueError(result['reason'])
    if result['compatible'] is None and checked is not True:
        raise ValueError(result['reason'] + ' Tick the confirmation to send it anyway.')
    # Plate and bed type carry over; the AMS mapping is chosen again for the new printer's trays.
    # Swapmod approvals don't carry over: a batch is approved again on the printer it runs on.
    opts = dict(options(job['options'].get('plate', 1), use_ams, mapping, job['options'].get('bed', 'textured_plate')))
    copy = store.add(target, job['label'], job['asset'], job['remote'], opts, author, bool(job['demo']))
    store.event(target, 'Job sent from another printer', f"{job['label']} • from {job['printer']} ({job_id}) • new job {copy['id']} • {author}")
    return copy
