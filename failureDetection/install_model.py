"""Install a new failure model (for example a Hailo-8 .hef from the Ultralytics Platform) safely, run as root on the Pi:

    sudo /usr/bin/python3 /opt/3d-printer-management/failureDetection/install_model.py best.zip
    sudo /usr/bin/python3 /opt/3d-printer-management/failureDetection/install_model.py best.zip \\
        --failure ~/pictures/failed --healthy ~/pictures/good          # also measure it on your own pictures
    sudo /usr/bin/python3 /opt/3d-printer-management/failureDetection/install_model.py best.zip --check-only
    sudo /usr/bin/python3 /opt/3d-printer-management/failureDetection/install_model.py --rollback

Accepts the Ultralytics Platform's Hailo export (best.zip with best_hailo_model/best.hef, metadata.yaml and
nms_config.json), or a plain .hef or .onnx (class names then come from --classes or config.json, in training order).

1. reads the class names in training order from metadata.yaml, and the on-chip NMS thresholds from nms_config.json;
2. for a .hef: checks with hailortcli that it was compiled for this Pi's chip (fw-control identify), takes one UINT8
   NHWC square picture, ends in Hailo NMS, and has as many classes as the names; records the HailoRT and firmware
   versions and the CPU architecture;
3. runs it through failureDetection/hailo_worker.py as the service user, with a time limit, before touching anything:
   a generated picture, plus your --failure / --healthy pictures (how many it catches / falsely flags);
4. backs up the current model (models/backups/) and config.json (config.json.backup-<time>), installs the new model
   as print_failure.<hef|onnx> (root, mode 644) with a record of where it came from (print_failure.<ext>.json), and
   changes only failure_detection.model (plus classes / input_size when the model's differ), keeping config.json's
   owner and permissions;
5. restarts the service and waits for the app's own "AI model ready: <model> on <AI HAT or CPU>" log line. When the
   model doesn't load, or the app had to fall back to the CPU model, everything is put back as it was.

Standard library only (Pillow, which the AI helper needs anyway, makes the test picture). Exit code 0 when installed.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import platform
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = '3d-printer-management'
SERVICE_USER = 'printermanager'
HISTORY = 'installs.json'   # in the models folder: what each install replaced, for --rollback
MAX_MODEL = 500 * 1024 * 1024
PICTURES = ('.jpg', '.jpeg', '.png', '.bmp', '.webp')


class Refused(Exception):
    """The model can't be used here; nothing was changed."""


def say(text):
    print('Model install: ' + text, flush=True)


def run(command, timeout=60, **extra):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, **extra)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, '', f'{type(exc).__name__}: {exc}')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


# ---- 1. the export and its metadata ---------------------------------------------------------------------------------

def scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '"\'':
        return text[1:-1]
    if text.lower() in ('true', 'false'):
        return text.lower() == 'true'
    if text.lower() in ('null', '~', ''):
        return None
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    if text.startswith('[') and text.endswith(']'):
        return [scalar(part) for part in text[1:-1].split(',') if part.strip()]
    return text


def parse_yaml(text):
    """The small YAML subset Ultralytics writes in metadata.yaml: nested maps, "- item" lists (also at the key's own
    indent, as in "imgsz:\n- 640") and scalars."""
    root = {}
    stack = [(0, root)]   # (indent of its items, container)
    pending = None        # (indent, parent, key): a "key:" with nothing after it, waiting to see a map or a list
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        line = raw.strip()
        item = line == '-' or line.startswith('- ')
        if pending and (indent > pending[0] or (indent == pending[0] and item)):
            container = [] if item else {}
            pending[1][pending[2]] = container
            stack.append((indent, container))
        pending = None
        while len(stack) > 1 and (indent < stack[-1][0] or (isinstance(stack[-1][1], list) and not item)):
            stack.pop()
        container = stack[-1][1]
        if item:
            if isinstance(container, list):
                container.append(scalar(line[1:]))
            continue
        if ':' not in line or not isinstance(container, dict):
            continue
        key, _, value = line.partition(':')
        key = scalar(key)
        if value.strip():
            container[key] = scalar(value.split(' #', 1)[0])
        else:
            container[key] = None
            pending = (indent, container, key)
    return root


def class_names(names):
    """metadata.yaml's names ({0: spaghetti, ...} or a list) in training order."""
    if isinstance(names, dict):
        try:
            ordered = sorted(names.items(), key=lambda item: int(item[0]))
        except (TypeError, ValueError):
            raise Refused(f'metadata.yaml class names are not numbered: {names}')
        if [int(k) for k, _ in ordered] != list(range(len(ordered))):
            raise Refused(f'metadata.yaml class numbers have gaps: {names}')
        return [str(v) for _, v in ordered]
    if isinstance(names, list):
        return [str(v) for v in names]
    return []


def find_value(data, *words):
    """The first number under a key containing all the words, anywhere in nested JSON (nms_config.json layouts vary)."""
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and all(w in str(key).lower() for w in words):
                return float(value)
        for value in data.values():
            found = find_value(value, *words)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_value(value, *words)
            if found is not None:
                return found
    return None


def unpack(source, folder):
    """The model file and what its export says about it. source: a .zip, .hef or .onnx."""
    source = Path(source).expanduser()
    if not source.is_file():
        raise Refused(f'{source} not found')
    suffix = source.suffix.lower()
    if suffix in ('.pt', '.pth'):
        raise Refused('a .pt checkpoint must be exported first: to Hailo (.hef) on the Ultralytics Platform, '
                      'or to ONNX (yolo export model=best.pt format=onnx); see failureDetection/FAILURE_DETECTION.md')
    meta = {'source': source.name}
    if suffix == '.zip':
        with zipfile.ZipFile(source) as archive:
            names = [n for n in archive.namelist() if not n.endswith('/') and '__MACOSX' not in n]
            if sum(archive.getinfo(n).file_size for n in names) > MAX_MODEL:
                raise Refused(f'{source.name} is too large')
            for name in names:
                target = (Path(folder) / name).resolve()
                if Path(folder).resolve() not in target.parents:
                    raise Refused(f'{source.name} has an unsafe path: {name}')
            archive.extractall(folder, names)
        files = sorted(Path(folder).rglob('*'))
        model = next((p for p in files if p.suffix.lower() == '.hef'), None) or next((p for p in files if p.suffix.lower() == '.onnx'), None)
        if not model:
            raise Refused(f'{source.name} has no .hef or .onnx model in it')
        metadata = next((p for p in files if p.name == 'metadata.yaml'), None)
        nms = next((p for p in files if p.name == 'nms_config.json'), None)
        if metadata:
            data = parse_yaml(metadata.read_text(errors='replace'))
            meta['classes'] = class_names(data.get('names'))
            size = data.get('imgsz')
            meta['input_size'] = int(size[0] if isinstance(size, list) and size else size) if size else None
            for key, name in (('hailo_arch', 'hailo_arch'), ('version', 'ultralytics'), ('date', 'exported'), ('task', 'task'),
                              ('description', 'description')):
                if data.get(key) is not None:
                    meta[name] = data[key]
            arguments = data.get('args') if isinstance(data.get('args'), dict) else {}
            calibration = data.get('calib_data') or data.get('calibration') or arguments.get('data')
            if calibration:
                meta['calibration'] = str(calibration)
            for key in ('dfc_version', 'compiler', 'compiler_version', 'hailo_dfc_version'):
                if data.get(key) is not None:
                    meta['compiler'] = str(data[key])
        if nms:
            try:
                config = json.loads(nms.read_text())
            except ValueError as exc:
                raise Refused(f'nms_config.json is not valid JSON ({exc})')
            meta['nms_score_threshold'] = find_value(config, 'score') if find_value(config, 'score') is not None else find_value(config, 'conf')
            meta['nms_iou_threshold'] = find_value(config, 'iou')
            meta['nms_max_boxes'] = find_value(config, 'max', 'class') or find_value(config, 'max', 'proposals')
    elif suffix in ('.hef', '.onnx'):
        model = Path(folder) / source.name
        shutil.copyfile(source, model)
    else:
        raise Refused(f'unsupported file type {source.suffix}: give the Ultralytics Hailo export (.zip), a .hef or a .onnx')
    if model.stat().st_size > MAX_MODEL:
        raise Refused(f'{model.name} is too large')
    meta = {k: v for k, v in meta.items() if v is not None}
    return model, meta


# ---- 2. the AI HAT and the .hef ------------------------------------------------------------------------------------

def chip_from(text):
    text = text.upper()
    return 'hailo10h' if 'HAILO10H' in text else 'hailo8l' if 'HAILO8L' in text else 'hailo8' if 'HAILO8' in text else ''


def system_info():
    """HailoRT, firmware, chip and CPU architecture of this Pi."""
    info = {'cpu_arch': platform.machine(), 'kernel': platform.release()}
    version = run(['hailortcli', '--version'])
    match = re.search(r'(\d+\.\d+\.\d+)', version.stdout)
    info['hailort'] = match.group(1) if match else ''
    identify = run(['hailortcli', 'fw-control', 'identify'], 30)
    info['chip'] = chip_from(identify.stdout)
    match = re.search(r'Firmware Version:\s*([^\n]+)', identify.stdout)
    info['firmware'] = match.group(1).strip() if match else ''
    if identify.returncode and not info['chip']:
        info['identify_error'] = ((identify.stderr or identify.stdout).strip().splitlines() or ['no output'])[-1][:200]
    return info


def parse_hef(text):
    """What `hailortcli parse-hef` says about a model."""
    found = {}
    match = re.search(r'compiled for:\s*(\w+)', text, re.I)
    found['arch'] = chip_from(match.group(1)) if match else ''
    inputs = re.findall(r'^\s*Input\s+\S+\s+(\w+),\s*(\w+)\((\d+)x(\d+)x(\d+)\)', text, re.M)
    found['inputs'] = [dict(type=t, order=o, height=int(h), width=int(w), channels=int(c)) for t, o, h, w, c in inputs]
    outputs = re.findall(r'^\s*Output\s+\S+\s+(\w+),\s*(.+)$', text, re.M)
    found['nms'] = any('NMS' in shape.upper() for _, shape in outputs)
    found['outputs'] = len(outputs)
    match = re.search(r'number of classes:\s*(\d+)', text, re.I)
    found['classes'] = int(match.group(1)) if match else None
    match = re.search(r'bounding boxes per class:\s*(\d+)', text, re.I)
    found['max_boxes'] = int(match.group(1)) if match else None
    return found


def check_hef(model, classes, system):
    """Refuses a .hef that can't work on this Pi; returns what was found."""
    result = run(['hailortcli', 'parse-hef', str(model)], 60)
    if result.returncode:
        raise Refused('hailortcli parse-hef failed: ' + ((result.stderr or result.stdout).strip().splitlines() or ['no output'])[-1][:200]
                      + ('' if shutil.which('hailortcli') else ' (hailortcli not installed: sudo apt install hailo-all)'))
    hef = parse_hef(result.stdout)
    chip = system.get('chip')
    if not chip:
        raise Refused('no working AI HAT found (hailortcli fw-control identify: '
                      f"{system.get('identify_error', 'no chip reported')}); install the .onnx instead, or fix the AI HAT first")
    if hef['arch'] and hef['arch'] != chip and not (hef['arch'] == 'hailo8l' and chip == 'hailo8'):
        raise Refused(f"the model was compiled for {hef['arch'].upper()}, this AI HAT is {chip.upper()}; export it again for {chip.upper()}")
    if len(hef['inputs']) != 1:
        raise Refused(f"expected one picture input, the model has {len(hef['inputs'])}")
    picture = hef['inputs'][0]
    if picture['type'] != 'UINT8' or picture['order'] != 'NHWC' or picture['channels'] != 3 or picture['width'] != picture['height']:
        raise Refused(f"unexpected input {picture['type']} {picture['order']}({picture['height']}x{picture['width']}x{picture['channels']}); "
                      'expected UINT8 NHWC(SxSx3)')
    if not hef['nms']:
        raise Refused('the model has no Hailo NMS output; export it with NMS (the Ultralytics Platform does this)')
    if hef['classes'] is not None and classes and hef['classes'] != len(classes):
        raise Refused(f"the model has {hef['classes']} classes but {len(classes)} names were given ({', '.join(classes)})")
    if hef['arch'] == 'hailo8l' and chip == 'hailo8':
        say('note: a Hailo-8L model on a Hailo-8 runs, but slower than a model compiled for Hailo-8.')
    return hef


# ---- 3. a test run, as the service user ----------------------------------------------------------------------------

def pictures_in(folder):
    if not folder:
        return []
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise Refused(f'{folder} is not a folder')
    return sorted(p for p in folder.rglob('*') if p.suffix.lower() in PICTURES and p.is_file())


def blank_picture():
    from PIL import Image  # system Pillow (python3-pil), which the AI helper needs too
    stream = io.BytesIO()
    Image.new('RGB', (1280, 720), (60, 60, 60)).save(stream, 'JPEG')
    return stream.getvalue()


def test_run(python, model, classes, labels, size, pictures, timeout):
    """Every picture through the real helper in one process, as the service user. Returns (backend, [score, ...])."""
    command = [python, str(HERE / 'hailo_worker.py'), str(model), json.dumps(classes), json.dumps(labels), str(size)]
    if os.geteuid() == 0:
        try:
            pwd.getpwnam(SERVICE_USER)
            command = ['/usr/sbin/runuser', '-u', SERVICE_USER, '--'] + command
        except KeyError:
            pass
    requests = ''.join(json.dumps({'jpeg': base64.b64encode(p).decode()}) + '\n' for p in pictures)
    result = run(command, timeout + 2 * len(pictures), input=requests, cwd=tempfile.gettempdir(),
                 env={**os.environ, 'HAILORT_LOGGER_PATH': tempfile.gettempdir()})
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith('{')]
    if not lines or not lines[0].get('ready'):
        error = ((result.stderr or '').strip().splitlines() or [result.stdout.strip()[:200] or 'no output'])[-1]
        raise Refused(f'the model does not run: {error[:300]}')
    replies = lines[1:]
    errors = [r['error'] for r in replies if 'error' in r]
    if errors:
        raise Refused(f'the model failed on a test picture: {errors[0][:300]}')
    if len(replies) != len(pictures):
        error = ((result.stderr or '').strip().splitlines() or ['it stopped early'])[-1]
        raise Refused(f'the helper answered {len(replies)} of {len(pictures)} pictures within the time limit ({error[:200]})')
    return lines[0].get('backend', 'hailo' if str(model).lower().endswith('.hef') else 'cpu'), [float(r.get('score', 0)) for r in replies]


def rate(scores, threshold):
    return round(sum(s >= threshold for s in scores) / len(scores), 3) if scores else None


# ---- 4. install, config, history -----------------------------------------------------------------------------------

def write_like(path, text, like=None):
    """Write text to path atomically with like's (or path's) owner and permissions."""
    path = Path(path)
    reference = Path(like) if like else path
    info = reference.stat() if reference.exists() else None
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.chmod(temporary, info.st_mode & 0o777 if info else 0o644)
        if info and os.geteuid() == 0:
            os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def copy_like(source, target, mode=0o644):
    fd, temporary = tempfile.mkstemp(dir=Path(target).parent, prefix='.' + Path(target).name + '-')
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def backup_file(path, folder, stamp):
    """Copy path into folder as <name>.<stamp>[.N], keeping mode and owner. Returns the copy."""
    folder.mkdir(parents=True, exist_ok=True)
    target, number = folder / f'{path.name}.{stamp}', 2
    while target.exists():
        target, number = folder / f'{path.name}.{stamp}.{number}', number + 1
    shutil.copy2(path, target)
    if os.geteuid() == 0:
        info = path.stat()
        os.chown(target, info.st_uid, info.st_gid)
    return target


def trigger_labels(classes, old_labels):
    """Which classes pause/notify: the old choice where it still fits, else spaghetti and warping, else all."""
    kept = [label for label in (old_labels or []) if label in classes]
    return kept or [c for c in classes if c in ('spaghetti', 'warping')] or list(classes)


def new_section(section, model, classes, size):
    """failure_detection with the new model. Classes / labels / input_size change only when the model's differ."""
    updated = dict(section or {}, model=str(model))
    if not section:
        updated.update(enabled=True, threshold=0.6, action='notify')
    if classes and section.get('classes') != classes:
        updated['classes'] = classes
        updated['labels'] = trigger_labels(classes, section.get('labels'))
    if size and section.get('input_size', 640) != size:
        updated['input_size'] = size
    return updated


def load_history(models):
    try:
        history = json.loads((models / HISTORY).read_text())
        return history if isinstance(history, list) else []
    except (OSError, ValueError):
        return []


def save_history(models, history):
    write_like(models / HISTORY, json.dumps(history[-20:], indent=2))


# ---- 5. restart and verify -----------------------------------------------------------------------------------------

def restart_and_verify(model_name, timeout):
    """Restart the service and wait for its "AI model ready" line. Returns (ok, message)."""
    since = int(time.time()) - 1
    result = run(['systemctl', 'restart', SERVICE], 120)
    if result.returncode:
        return False, 'systemctl restart failed: ' + ((result.stderr or '').strip().splitlines() or ['?'])[-1][:200]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(3)
        journal = run(['journalctl', '-u', SERVICE, '--since', f'@{since}', '-o', 'cat', '--no-pager'], 30).stdout
        for line in journal.splitlines():
            if 'AI model ready:' in line:
                if f'AI model ready: {model_name} on ' not in line or '(fallback)' in line:
                    return False, 'the app fell back instead of using the new model: ' + line.strip()[-200:]
                return True, line.strip()[line.find('AI model ready:'):]
            if 'AI model did not start' in line:
                return False, line.strip()[-300:]
        if run(['systemctl', 'is-failed', '--quiet', SERVICE]).returncode == 0:
            return False, 'the service stopped (journalctl -u 3d-printer-management for details)'
    return False, f'no "AI model ready" line within {timeout} s (is failure_detection enabled, with a camera printer?)'


def restore(entry, config_path):
    """Put back what one install replaced."""
    target = Path(entry['installed'])
    if entry.get('model_backup') and Path(entry['model_backup']).is_file():
        copy_like(entry['model_backup'], target)
    elif not entry.get('model_backup') and target.is_file():
        target.unlink()
    if entry.get('config_backup') and Path(entry['config_backup']).is_file():
        write_like(config_path, Path(entry['config_backup']).read_text(), like=config_path)


def rollback(args, models, config_path):
    history = load_history(models)
    if not history:
        say('nothing to roll back (no install recorded in ' + str(models / HISTORY) + ').')
        return 1
    entry = history.pop()
    restore(entry, config_path)
    save_history(models, history)
    say(f"restored the model and config.json from before {entry.get('date', 'the last install')}.")
    if not args.no_restart:
        ok, message = restart_and_verify(Path(entry.get('previous_model') or entry['installed']).name, args.wait)
        say(('service restarted: ' if ok else 'after restart: ') + message)
    return 0


# ---- main ----------------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description='Check and install a failure model (Ultralytics Hailo export .zip, .hef or .onnx).')
    parser.add_argument('model', nargs='?', help='best.zip from the Ultralytics Platform, a .hef or a .onnx')
    parser.add_argument('--config', default='/etc/3d-printer-management/config.json')
    parser.add_argument('--models', default='/opt/3d-printer-management-models')
    parser.add_argument('--python', default='/usr/bin/python3')
    parser.add_argument('--classes', help='class names in training order, comma separated, when the export has no metadata.yaml')
    parser.add_argument('--failure', help='folder of pictures of failing prints: reports how many the model catches')
    parser.add_argument('--healthy', help='folder of pictures of good prints: reports how many the model falsely flags')
    parser.add_argument('--threshold', type=float, help='score that counts as caught (default: failure_detection.threshold)')
    parser.add_argument('--min-catch', type=float, help='refuse the model when it catches fewer of --failure (0-1)')
    parser.add_argument('--max-false', type=float, help='refuse the model when it flags more of --healthy (0-1)')
    parser.add_argument('--timeout', type=int, default=120, help='seconds the test run may take to load the model')
    parser.add_argument('--wait', type=int, default=120, help='seconds to wait for the app to load the model after restarting')
    parser.add_argument('--check-only', action='store_true', help='check and test, change nothing')
    parser.add_argument('--no-restart', action='store_true', help='install without restarting (no automatic rollback)')
    parser.add_argument('--rollback', action='store_true', help='undo the last install')
    args = parser.parse_args(argv)
    config_path, models = Path(args.config), Path(args.models)
    if not args.check_only and os.geteuid() != 0:
        say('run with sudo (it installs into ' + str(models) + ' and restarts the service), or use --check-only.')
        return 2
    if args.rollback:
        return rollback(args, models, config_path)
    if not args.model:
        parser.error('give the model to install, or --rollback')
    try:
        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    except ValueError as exc:
        say(f'{config_path} is not valid JSON ({exc}); fix it first.')
        return 2
    section = config.get('failure_detection') if isinstance(config.get('failure_detection'), dict) else {}
    with tempfile.TemporaryDirectory() as folder:
        try:
            model, meta = unpack(args.model, folder)
            for path in [Path(folder), *Path(folder).rglob('*')]:   # the test run reads it as the service user
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
            classes = [c.strip() for c in args.classes.split(',') if c.strip()] if args.classes else meta.get('classes') or section.get('classes') or []
            if not classes:
                raise Refused('no class names: the export has no metadata.yaml; give --classes spaghetti,stringing,warping (training order)')
            say(f"{model.name} from {meta['source']}: classes {', '.join(f'{i}={c}' for i, c in enumerate(classes))}.")
            hef = model.suffix.lower() == '.hef'
            system = system_info()
            say(f"this Pi: {system['cpu_arch']}, kernel {system['kernel']}, HailoRT {system['hailort'] or 'not installed'}, "
                f"AI HAT {system['chip'].upper() or 'not found'}{', firmware ' + system['firmware'] if system['firmware'] else ''}.")
            details = {}
            size = meta.get('input_size') or section.get('input_size') or 640
            if hef:
                details = check_hef(model, classes, system)
                size = details['inputs'][0]['width']
                say(f"model: {details['arch'].upper() or 'arch not stated'}, input UINT8 NHWC {size}x{size}x3, Hailo NMS, "
                    f"{details['classes'] or len(classes)} classes, up to {details['max_boxes'] or '?'} boxes per class.")
                if meta.get('nms_score_threshold') is not None:
                    say(f"on-chip NMS drops detections below {meta['nms_score_threshold']:.2f} (IoU {meta.get('nms_iou_threshold', '?')}); "
                        'those score 0 here.')
                stated = chip_from(str(meta.get('hailo_arch', '')))
                if stated and details['arch'] and stated != details['arch']:
                    say(f"note: metadata.yaml says {meta['hailo_arch']}, the model itself says {details['arch']}.")
            labels = trigger_labels(classes, section.get('labels'))
            threshold = args.threshold if args.threshold is not None else float(section.get('threshold', 0.6))
            failing, healthy = pictures_in(args.failure), pictures_in(args.healthy)
            batch = [blank_picture()] + [p.read_bytes() for p in failing + healthy]
            say(f'test run as {SERVICE_USER} on {len(batch)} picture(s)...')
            started = time.monotonic()
            backend, scores = test_run(args.python, model, classes, labels, size, batch, args.timeout)
            seconds = time.monotonic() - started
            if hef and backend != 'hailo':
                raise Refused(f'the test ran on {backend}, not the AI HAT')
            tests = dict(backend=backend, seconds=round(seconds, 1), blank_score=scores[0], threshold=threshold,
                         catch=rate(scores[1:1 + len(failing)], threshold), false_alarms=rate(scores[1 + len(failing):], threshold),
                         failure_pictures=len(failing), healthy_pictures=len(healthy))
            say(f"runs on {'the AI HAT' if backend == 'hailo' else 'the CPU'} ({seconds:.1f} s for {len(batch)} picture(s) including loading); "
                f'blank picture scored {scores[0]:.2f}.')
            if failing:
                say(f"failure pictures: caught {sum(s >= threshold for s in scores[1:1 + len(failing)])} of {len(failing)} "
                    f"at threshold {threshold:.2f} (scores {', '.join(f'{s:.2f}' for s in scores[1:1 + len(failing)][:20])}).")
            if healthy:
                say(f"healthy pictures: falsely flagged {sum(s >= threshold for s in scores[1 + len(failing):])} of {len(healthy)}.")
            if args.min_catch is not None and tests['catch'] is not None and tests['catch'] < args.min_catch:
                raise Refused(f"caught {tests['catch']:.0%} of the failure pictures, below --min-catch {args.min_catch:.0%}")
            if args.max_false is not None and tests['false_alarms'] is not None and tests['false_alarms'] > args.max_false:
                raise Refused(f"flagged {tests['false_alarms']:.0%} of the healthy pictures, above --max-false {args.max_false:.0%}")
        except Refused as exc:
            say(f'NOT installed: {exc}')
            return 1
        if args.check_only:
            say('check only: nothing changed.')
            return 0

        # Install: back up, replace, record.
        stamp = time.strftime('%Y%m%d-%H%M%S')
        models.mkdir(parents=True, exist_ok=True)
        target = models / ('print_failure' + model.suffix.lower())
        model_backup = backup_file(target, models / 'backups', stamp) if target.is_file() else None
        config_backup = None
        if config_path.is_file():
            config_backup = config_path.with_name(f'{config_path.name}.backup-{stamp}')
            shutil.copy2(config_path, config_backup)
            info = config_path.stat()
            if os.geteuid() == 0:
                os.chown(config_backup, info.st_uid, info.st_gid)
        entry = dict(date=time.strftime('%Y-%m-%d %H:%M:%S'), installed=str(target), model_backup=str(model_backup) if model_backup else None,
                     config_backup=str(config_backup) if config_backup else None, previous_model=section.get('model'))
        copy_like(model, target)
        record = dict(installed=entry['date'], file=target.name, sha256=sha256(target), size=target.stat().st_size, classes=classes,
                      labels=labels, input_size=size, export=meta, hef=details, system=system, test=tests)
        write_like(target.with_name(target.name + '.json'), json.dumps(record, indent=2))
        history = load_history(models)
        save_history(models, history + [entry])
        say(f"installed {target} (sha256 {record['sha256'][:12]}…){'; previous model kept in ' + str(model_backup) if model_backup else ''}.")
        if config_path.is_file():
            config['failure_detection'] = new_section(section, target, classes, size)
            write_like(config_path, json.dumps(config, indent=2), like=config_path)
            say(f'config.json now uses {target.name} (backup {config_backup.name}).')
        else:
            say(f'no {config_path}; set failure_detection.model to {target} yourself.')
        if args.no_restart:
            say('not restarted (--no-restart): sudo systemctl restart 3d-printer-management')
            return 0
        if not config.get('failure_detection', {}).get('enabled', True):
            say('failure_detection is disabled in config.json; restart the service after enabling it.')
            return 0
        say('restarting the service and waiting for the app to load the model...')
        ok, message = restart_and_verify(target.name, args.wait)
        if ok:
            say('DONE: ' + message)
            return 0
        say(f'FAILED: {message}. Rolling back...')
        restore(entry, config_path)
        save_history(models, history)
        ok, message = restart_and_verify(Path(section.get('model') or target).name, args.wait) if section.get('model') else (True, 'restored')
        say('rolled back: ' + message)
        return 1


if __name__ == '__main__':
    sys.exit(main())
