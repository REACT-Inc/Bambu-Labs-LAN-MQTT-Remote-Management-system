"""Send a finished print to another printer's queue (#57, dashboard only).

G-code is sliced for one printer model, so a job only goes to a printer of the same model:
- the file's model comes from the .3mf (Metadata/project_settings.config "printer_model", or
  Metadata/slice_info.config "printer_model_id");
- the printer's model comes from its config.json "model", or its serial number.
A known mismatch is refused; if either model is unknown, the user has to confirm they checked it.

Jobs whose file is only on the original printer's storage (remote: jobs) are copied to the Pi over FTPS first
(fetch), then queued like an upload: the new printer gets the file uploaded when the job starts.
"""
import json
import re
import secrets
import zipfile
from pathlib import Path

import printer_models
import queueing
from queueing import TERMINAL, options, validate_archive


def model_key(text):
    """'Bambu Lab A1 mini' / 'A1 mini' / 'A1M' -> 'a1mini'; '' for nothing. Models the app doesn't know yet
    keep their normalized name, so two printers of the same new model still match."""
    return printer_models.key_from_text(text) or printer_models.normalize(text)


label = printer_models.label


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
                    return printer_models.key_from_studio_id(match.group(1).decode(errors='replace'))
    except (OSError, zipfile.BadZipFile):
        pass
    return ''


def printer_model(core, name):
    config = core.printer_config(name) or {}
    if config.get('model'):
        return model_key(config['model'])
    return printer_models.key_for(config, name)


def file_model(core, job, path=None):
    """(model_key, how we know) for the job's file: read from the .3mf, or for a file still on the original printer,
    that printer's model (it was printed there, so it was sliced for it)."""
    path = path or job.get('asset')
    if path:
        return sliced_for(path), 'Sliced for'
    return printer_model(core, job['printer']), 'Printed on'


def check(core, job, target, path=None):
    """{'compatible': True/False/None, 'reason': text} for sending this job to the target printer."""
    if (path or job.get('asset')) and not Path(path or job['asset']).is_file():
        return dict(compatible=False, reason='The uploaded file for this job is no longer on the Pi.')
    if target == job['printer']:
        return dict(compatible=False, reason='That is the printer it already printed on; use Queue again.')
    (sliced, how), model = file_model(core, job, path), printer_model(core, target)
    if sliced and model:
        if sliced == model:
            return dict(compatible=True, reason=f'{how} the {label(sliced)}.')
        return dict(compatible=False, reason=f'{how} the {label(sliced)}, not the {label(model)}. Re-slice it for the {label(model)} in Bambu Studio.')
    missing = 'which printer the file was sliced for' if not sliced else f"this printer's model (set \"model\" in config.json)"
    return dict(compatible=None, reason=f"Couldn't tell {missing}; check the file was sliced for this printer.")


def targets(core, job):
    return [dict(name=name, display=core.display_name(name) if hasattr(core, 'display_name') else name,
                 model=label(printer_model(core, name)) if printer_model(core, name) else '', **check(core, job, name))
            for name in core.names() if name != job['printer']]


def validate(core, store, job_id, target, checked, path=None):
    """The job, if it may be sent to the target printer; raises with the reason otherwise."""
    job = store.get(job_id)
    if job['status'] not in TERMINAL:
        raise ValueError('Only finished, failed or cancelled jobs can be sent to another printer.')
    if target not in core.names():
        raise ValueError('Choose a printer.')
    result = check(core, job, target, path)
    if result['compatible'] is False:
        raise ValueError(result['reason'])
    if result['compatible'] is None and checked is not True:
        raise ValueError(result['reason'] + ' Tick the confirmation to send it anyway.')
    return job


def needs_fetch(core, job):
    return not job.get('asset') and not getattr(core, 'EXAMPLE_MODE', False)


def fetch(core, job, uploads, download=None):
    """Copy a remote: job's file from the original printer to the Pi's uploads folder. Runs in a worker thread
    (no database access). Returns the local path."""
    dest = Path(uploads) / (secrets.token_hex(16) + '.3mf')
    try:
        (download or queueing.download)(core.printer_config(job['printer']), job['remote'], dest)
        validate_archive(dest, job['options'].get('plate', 1))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return str(dest)


def send(core, store, job_id, target, use_ams, mapping, checked, author, asset=None):
    """Add a copy of a finished job to another printer's queue. Returns the new job.

    asset: for a remote: job, the file fetch() copied from the original printer. It's checked again here, now
    that the real file can be read; a refused send leaves it for the caller to delete.
    """
    job = validate(core, store, job_id, target, checked, asset)
    # Plate and bed type carry over; the AMS mapping is chosen again for the new printer's trays.
    # Swapmod approvals don't carry over: a batch is approved again on the printer it runs on.
    opts = dict(options(job['options'].get('plate', 1), use_ams, mapping, job['options'].get('bed', 'textured_plate'),
                        job['options'].get('priority')))
    copy = store.add(target, job['label'], asset or job['asset'], job['remote'], opts, author, bool(job['demo']))
    copied = ' • file copied from that printer' if asset else ''
    store.event(target, 'Job sent from another printer', f"{job['label']} • from {job['printer']} ({job_id}){copied} • new job {copy['id']} • {author}")
    return copy
