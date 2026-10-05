"""Automatic AI failure-detection setup (#75), run by install.sh and Updater/update.sh as root. Safe to run again:
    sudo /usr/bin/python3 /opt/3d-printer-management/failureDetection/setup_ai.py
PM_AI=0 skips it. Only runs when a printer has a camera (camera_type rtsp or jpeg_tcp).

Does what was done by hand on the team Pi:
0. installs OpenCV, numpy and Pillow for the system Python (Debian / Raspberry Pi OS packages) so a .onnx model can
   run on the CPU; with a Raspberry Pi AI HAT (Hailo, PCIe vendor 0x1e60) also its software, and if /dev/hailo0 is
   missing (driver not built for the running kernel, seen on Raspberry Pi OS Trixie) the kernel headers, then
   rebuilds the driver. A driver that only loads after a reboot is reported; the CPU model is used until then;
1. fetches the failure model from the repository's `ai-model` GitHub release (print_failure.onnx, plus a Hailo .hef
   for the AI HAT's chip when the release has one) into /opt/3d-printer-management-models, checking its SHA-256;
2. checks the model really runs: one test picture through failureDetection/hailo_worker.py as the service user;
3. adds a "failure_detection" section to config.json, only when there isn't one (your own settings are never changed),
   starting in notify-only mode.

Standard library only. Prints one line per step; never fails the install (exit code 0 even when a step is skipped).
The service is restarted when config.json changed.
"""
import argparse
import base64
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = 'REACT-Inc/Bambu-Labs-LAN-MQTT-Remote-Management-system'
TAG = 'ai-model'
SERVICE_USER = 'printermanager'
# Used when the release has no print_failure.json: the 3D Print Failure Detection dataset (v4, CC BY 4.0).
DEFAULT_META = {'classes': ['spaghetti', 'stringing', 'warping'], 'labels': ['spaghetti', 'warping'],
                'threshold': 0.4, 'input_size': 640}
MAX_MODEL = 200 * 1024 * 1024


def say(text):
    print('AI setup: ' + text, flush=True)


def api(url, token=''):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'pm-ai-setup',
                                                   **({'Authorization': 'Bearer ' + token} if token else {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def download(asset, target, token=''):
    """Download one release asset to target, checking the size and (when GitHub reports it) the SHA-256."""
    if asset.get('size', 0) > MAX_MODEL:
        raise ValueError(f"{asset['name']} is larger than {MAX_MODEL // 1024 // 1024} MiB")
    headers = {'Accept': 'application/octet-stream', 'User-Agent': 'pm-ai-setup', **({'Authorization': 'Bearer ' + token} if token else {})}
    request = urllib.request.Request(asset['url'], headers=headers)
    digest, size = hashlib.sha256(), 0
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.download-')
    try:
        with urllib.request.urlopen(request, timeout=120) as response, os.fdopen(fd, 'wb') as out:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MODEL:
                    raise ValueError(f"{asset['name']} is too large")
                digest.update(chunk)
                out.write(chunk)
        expected = str(asset.get('digest') or '')
        if expected.startswith('sha256:') and expected[7:] != digest.hexdigest():
            raise ValueError(f"{asset['name']} checksum mismatch")
        if asset.get('size') and size != asset['size']:
            raise ValueError(f"{asset['name']} size mismatch")
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        return digest.hexdigest()
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_models(repo, models, chip, token=''):
    """Bring the ai-model release's files up to date in models/. Returns (metadata, {'onnx': path, 'hef': path})."""
    found = {}
    try:
        release = api(f'https://api.github.com/repos/{repo}/releases/tags/{TAG}', token)
    except Exception as exc:
        say(f'no "{TAG}" release reachable in {repo} ({type(exc).__name__}); keeping any model already installed.')
        release = {'assets': []}
    assets = {a.get('name'): a for a in release.get('assets') or []}
    meta = dict(DEFAULT_META)
    if 'print_failure.json' in assets:
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'meta.json'
                download(assets['print_failure.json'], path, token)
                loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                meta.update({k: loaded[k] for k in DEFAULT_META if k in loaded})
        except Exception as exc:
            say(f'could not read print_failure.json ({exc}); using the default classes.')
    wanted = {'onnx': 'print_failure.onnx'}
    if chip:
        wanted['hef'] = f'print_failure_{chip}.hef'
    record_path = models / '.downloaded.json'   # what this setup put there, so your own models are never replaced
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        record = {}
    for kind, name in wanted.items():
        target = models / name
        asset = assets.get(name)
        if asset:
            remote = str(asset.get('digest') or '')
            local = sha256(target) if target.is_file() else None
            if local and remote.startswith('sha256:') and local == remote[7:]:
                record[name] = local
                say(f'{name} is up to date.')
            elif local and local != record.get(name):
                say(f'{name} was put there by hand; keeping it (delete it to get the release model).')
            else:
                try:
                    record[name] = download(asset, target, token)
                    say(f'downloaded {name} ({asset.get("size", 0) // 1024} KiB).')
                except Exception as exc:
                    say(f'could not download {name}: {exc}')
        if target.is_file():
            found[kind] = target
    record_path.write_text(json.dumps(record))
    return meta, found


def runs(python, model, meta):
    """Push one test picture through the real helper as the service user. Returns (ok, message)."""
    picture = Path(tempfile.gettempdir()) / 'pm-ai-test.jpg'
    try:
        from PIL import Image  # system Pillow (python3-pil)
        Image.new('RGB', (1280, 720), (60, 60, 60)).save(picture, 'JPEG')
    except Exception as exc:
        return False, f'Pillow missing ({exc})'
    os.chmod(picture, 0o644)
    command = [python, str(HERE / 'hailo_worker.py'), str(model), json.dumps(meta['classes']), json.dumps(meta['labels']),
               str(meta.get('input_size', 640))]
    if os.geteuid() == 0:
        try:
            pwd.getpwnam(SERVICE_USER)
            command = ['/usr/sbin/runuser', '-u', SERVICE_USER, '--'] + command
        except KeyError:
            pass
    request = json.dumps({'jpeg': base64.b64encode(picture.read_bytes()).decode()}) + '\n'
    try:
        result = subprocess.run(command, input=request, capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'
    finally:
        picture.unlink(missing_ok=True)
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith('{')]
    if len(lines) < 2 or not json.loads(lines[0]).get('ready'):
        error = (result.stderr.strip().splitlines() or ['no output'])[-1]
        return False, error[:300]
    reply = json.loads(lines[1])
    if 'error' in reply:
        return False, reply['error']
    return True, f"ready, test picture scored {reply.get('score', 0)}"


def configure(config_path, model, meta):
    """Add failure_detection when config.json has none. Returns True when the file changed."""
    config = json.loads(config_path.read_text())
    if 'failure_detection' in config:
        say('config.json already has failure_detection; leaving your settings as they are.')
        return False
    config['failure_detection'] = {'enabled': True, 'model': str(model), 'classes': meta['classes'],
                                   'labels': meta['labels'], 'threshold': meta['threshold'],
                                   'input_size': meta.get('input_size', 640), 'action': 'notify'}
    info = config_path.stat()
    fd, temporary = tempfile.mkstemp(dir=config_path.parent, prefix='.config-')
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream, indent=2)
    os.chmod(temporary, info.st_mode & 0o777)
    os.chown(temporary, info.st_uid, info.st_gid)
    os.replace(temporary, config_path)
    say(f'enabled AI failure detection in config.json ({model.name}, notify only; set "action": "pause" when you trust it).')
    return True


def run(command, timeout=1800):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, '', str(exc))


def apt_install(*packages):
    result = run(['apt-get', 'install', '-y', '-q', *packages])
    if result.returncode:
        say(f"could not install {' '.join(packages)}: {(result.stderr.strip().splitlines() or ['?'])[-1][:200]}")
    return result.returncode == 0


def has_camera(config_path):
    try:
        printers = json.loads(config_path.read_text()).get('printers') or []
    except (OSError, ValueError):
        return False
    return any(p.get('camera_type') in ('rtsp', 'jpeg_tcp') for p in printers if isinstance(p, dict))


def hailo_device(root=Path('/sys/bus/pci/devices')):
    """PCI address of a Hailo device (vendor 0x1e60), or ''."""
    for device in sorted(root.glob('*')):
        try:
            if (device / 'vendor').read_text().strip() == '0x1e60':
                return device.name
        except OSError:
            continue
    return ''


def chip_from(identify):
    text = identify.upper()
    return 'hailo10h' if 'HAILO10H' in text else 'hailo8l' if 'HAILO8L' in text else 'hailo8' if 'HAILO8' in text else ''


def prepare_system():
    """Packages, and the AI HAT driver. Returns (chip or '', reboot_needed)."""
    if not shutil.which('apt-get'):
        say('automatic package setup needs Debian / Raspberry Pi OS; install OpenCV for the system Python yourself.')
        return '', False
    if run(['apt-get', 'update', '-q']).returncode:
        say('apt-get update failed; trying with the current package lists.')
    say('installing OpenCV, numpy and Pillow for the system Python (CPU models)...')
    apt_install('python3-opencv', 'python3-numpy', 'python3-pil')
    address = hailo_device()
    if not address:
        say('no AI HAT found; models run on the CPU.')
        return '', False
    name = run(['lspci', '-s', address]).stdout.split(': ', 1)[-1].strip()
    say(f'AI HAT found: {name or "Hailo device at " + address}.')
    package = 'hailo-h10-all' if ('10H' in name or 'Hailo-10' in name) else 'hailo-all'
    headers = next((h for h in (f'linux-headers-{os.uname().release}', 'linux-headers-rpi-2712', 'linux-headers-rpi-v8')
                    if run(['apt-cache', 'show', h]).returncode == 0), '')
    say(f"installing {package}, dkms{' and ' + headers if headers else ''}...")
    apt_install('dkms', *([headers] if headers else []), package)
    device = Path('/dev/hailo0')
    if not device.exists():
        run(['modprobe', 'hailo_pci'])
    if not device.exists():
        say('driver not loaded; rebuilding it for this kernel...')
        if run(['dpkg', '-s', 'hailort-pcie-driver']).returncode == 0:
            apt_install('--reinstall', 'hailort-pcie-driver')
        run(['modprobe', 'hailo_pci'])
    if not device.exists():
        say('the AI HAT driver needs a reboot to load. The CPU model is used until then; this setup runs again on the next update.')
        return '', True
    identify = run(['hailortcli', 'fw-control', 'identify'], 30).stdout
    chip = chip_from(identify)
    say(f"driver OK ({chip or 'chip not identified'}).")
    if device.stat().st_mode & 0o006 != 0o006:
        say(f'note: /dev/hailo0 is mode {device.stat().st_mode & 0o777:o}; the service user may need access to it.')
    return chip, False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/3d-printer-management/config.json')
    parser.add_argument('--models', default='/opt/3d-printer-management-models')
    parser.add_argument('--data', default='/var/lib/3d-printer-management')
    parser.add_argument('--repo', default=os.environ.get('PM_AI_REPO', ''))
    parser.add_argument('--chip', default='', help='hailo8, hailo8l or hailo10h when an AI HAT works')
    parser.add_argument('--python', default='/usr/bin/python3')
    parser.add_argument('--no-packages', action='store_true', help='skip apt and the AI HAT driver')
    parser.add_argument('--restart', action='store_true', help='restart the service when config.json changed')
    args = parser.parse_args(argv)
    config_path, models = Path(args.config), Path(args.models)
    if not config_path.is_file():
        say('no config.json yet; skipped.')
        return 0
    if os.environ.get('PM_AI') == '0':
        say('skipped (PM_AI=0).')
        return 0
    if not has_camera(config_path):
        say('no printer has a camera (camera_type); skipped. Run this setup again after adding one.')
        return 0
    chip, reboot = (args.chip, False) if args.chip or args.no_packages else prepare_system()
    repo, token = args.repo, ''
    try:   # follow the repository (and private token) chosen under Settings → GitHub releases
        saved = json.loads((Path(args.data) / 'github-updates.json').read_text())
        repo = repo or saved.get('repository', '')
        token = saved.get('token', '') if repo == saved.get('repository') else ''
    except (OSError, ValueError):
        pass
    repo = repo or DEFAULT_REPO
    models.mkdir(parents=True, exist_ok=True)
    os.chmod(models, 0o755)
    meta, found = fetch_models(repo, models, args.chip, token)
    choice = None
    for kind in ('hef', 'onnx'):   # the AI HAT first, when there's a model for its chip and it runs
        if kind in found:
            ok, message = runs(args.python, found[kind], meta)
            say(f'{found[kind].name}: {"works" if ok else "does not run"} ({message}).')
            if ok:
                choice = found[kind]
                break
    if reboot:
        say('REBOOT RECOMMENDED for the AI HAT: sudo reboot')
    if not choice:
        say('no working model; AI failure detection stays off. Put a .onnx or .hef in '
            f'{models} (see failureDetection/FAILURE_DETECTION.md) and run this setup again.')
        return 0
    if configure(config_path, choice, meta) and args.restart:
        if run(['systemctl', 'is-active', '--quiet', '3d-printer-management']).returncode == 0:
            run(['systemctl', 'restart', '3d-printer-management'], 120)
            say('restarted the service to start watching.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:   # never break an install over the optional AI feature
        say(f'skipped after an unexpected error: {type(exc).__name__}: {exc}')
        sys.exit(0)
