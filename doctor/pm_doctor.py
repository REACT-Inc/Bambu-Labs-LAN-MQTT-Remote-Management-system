#!/usr/bin/env python3
"""pm-doctor: an independent watchdog for 3D Printer Management (#43).

It keeps working when the app doesn't start, crashes or freezes while systemd still says "running", so there is
always a way to see what went wrong and be told about it.

    sudo pm-doctor            check now and print the results
    sudo pm-doctor report     write an incident ZIP (secrets removed) and print its path
    pm-doctor run             the service: check every 30 s, keep incident records, alert, serve the status page

Independence: Python standard library only, never imports the app, installed in /usr/local/lib/pm-doctor (app
updates don't replace it; doctor/install-doctor.sh installs a new copy only when DOCTOR_VERSION changes), its own
systemd unit with CPU and memory limits. It never talks to the printers beyond a TCP reachability check.

Settings: /etc/pm-doctor/config.json (created on install)
    {"discord_webhook": "", "github": {"repository": "", "token": ""}, "restart_after_minutes": 0,
     "status_page": true, "port": 8081, "interval": 30}
"""
import argparse
import hashlib
import hmac
import html
import http.server
import io
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DOCTOR_VERSION = '1.0.0'
SERVICE = '3d-printer-management'
APP_CONFIG = Path('/etc/3d-printer-management/config.json')
DATA_DIR = Path('/var/lib/3d-printer-management')
CONFIG = Path('/etc/pm-doctor/config.json')
STATE = Path('/var/lib/pm-doctor')
UPDATER_STATUS = Path('/var/lib/pm-updater/status.json')
DEFAULTS = dict(discord_webhook='', github={'repository': '', 'token': ''}, restart_after_minutes=0, status_page=True,
                port=8081, interval=30)
FREEZE_CHECKS = 4          # failed /health checks in a row (2 min at 30 s) before "frozen"
RESTART_LOOP = (3, 600)    # this many restarts within this many seconds is a restart loop
KEEP_INCIDENTS = 30

# The same secret rules as the app's diagnostics.py (kept in sync by tests/test_doctor.py), copied so a broken app
# can't break the doctor.
SECRET_KEYS = re.compile(r'access_code|token|password|secret|hash|salt|api_key|credential|webhook', re.I)
SECRET_TEXT = [
    (re.compile(r'[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}'), '[discord-token]'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'), '[github-token]'),
    (re.compile(r'''((?:access_code|token|password|secret)["']?\s*[:=]\s*["']?)[^\s"',}]+''', re.I), r'\1[redacted]'),
    (re.compile(r'https://(?:discord(?:app)?\.com)/api/webhooks/\S+'), '[discord-webhook]'),
    (re.compile(r'rtsps?://[^@\s]+@'), 'rtsps://[redacted]@'),
]


def redact_text(text):
    for pattern, replacement in SECRET_TEXT:
        text = pattern.sub(replacement, text)
    return text


def redact(value, key=''):
    if key and SECRET_KEYS.search(key) and not isinstance(value, bool) and value not in (None, '', [], {}):
        return '[redacted]'
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key == 'serial' and isinstance(value, str) and len(value) > 6:
        return value[:3] + '…' + value[-3:]
    return redact_text(value) if isinstance(value, str) else value


def load_json(path, default):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def run(command, timeout=10):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, '', str(exc))


# ---- checks ------------------------------------------------------------------------------------------------------

class Checks:
    """Everything the doctor looks at. Each probe is a method, so tests can replace them."""

    def __init__(self, app_config=APP_CONFIG, data_dir=DATA_DIR, runner=run, clock=time.time):
        self.app_config, self.data_dir, self.run, self.clock = Path(app_config), Path(data_dir), runner, clock

    def config(self):
        return load_json(self.app_config, {})

    def service(self):
        out = self.run(['systemctl', 'show', SERVICE, '-p', 'ActiveState,SubState,NRestarts,MainPID,ExecMainStartTimestampMonotonic']).stdout
        values = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        return dict(active=values.get('ActiveState', 'unknown'), sub=values.get('SubState', ''), restarts=int(values.get('NRestarts') or 0),
                    pid=int(values.get('MainPID') or 0))

    def health(self, port):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                data = json.loads(response.read())
            return data.get('application') == SERVICE, data.get('release', '')
        except Exception as exc:   # refused, timeout, bad JSON: not answering
            return False, type(exc).__name__

    def journal(self, unit=SERVICE, lines=300):
        return redact_text(self.run(['journalctl', '-u', unit, '-n', str(lines), '-o', 'short-iso', '--no-pager'], 20).stdout)

    def startup_error(self, journal):
        """The last traceback or start-up failure since the service last started, or ''."""
        text = journal.split('Started 3d-printer-management')[-1]
        for marker in ('Start-up failed', 'Traceback (most recent call last)', 'SyntaxError', 'ModuleNotFoundError', 'ImportError'):
            if marker in text:
                tail = text[text.rfind(marker):].splitlines()
                return '\n'.join(tail[:25])[:1800]
        return ''

    def disk(self):
        result = []
        for label, path in (('system', Path('/')), ('data', self.data_dir)):
            try:
                usage = shutil.disk_usage(path if path.exists() else '/')
            except OSError:
                continue
            result.append(dict(label=label, free_gb=round(usage.free / 1024 ** 3, 1), free_pct=round(100 * usage.free / max(1, usage.total))))
        return result

    def memory(self):
        info = {}
        try:
            for line in Path('/proc/meminfo').read_text().splitlines():
                key, _, value = line.partition(':')
                info[key] = int(value.split()[0]) // 1024
        except (OSError, ValueError, IndexError):
            return {}
        return dict(available_mb=info.get('MemAvailable', 0), total_mb=info.get('MemTotal', 0),
                    swap_used_mb=info.get('SwapTotal', 0) - info.get('SwapFree', 0))

    def temperature(self):
        try:
            return round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000, 1)
        except (OSError, ValueError):
            return None

    def throttled(self):
        """Raspberry Pi: under-voltage / throttling flags from vcgencmd, or ''."""
        if not shutil.which('vcgencmd'):
            return ''
        match = re.search(r'throttled=(0x[0-9a-fA-F]+)', self.run(['vcgencmd', 'get_throttled']).stdout)
        if not match:
            return ''
        bits = int(match.group(1), 16)
        now = [text for bit, text in ((0, 'under-voltage now'), (1, 'frequency capped now'), (2, 'throttled now'), (3, 'temperature limit now')) if bits & 1 << bit]
        past = [text for bit, text in ((16, 'under-voltage'), (18, 'throttling')) if bits & 1 << bit]
        return ', '.join(now) + (f" (since boot: {', '.join(past)})" if past and not now else '')

    def reachable(self, host, port, timeout=1.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def network(self, config):
        printers = []
        # Printers from config.json and the ones added in the dashboard (printers.json in the app's data folder, #8).
        added = [p for p in load_json(self.data_dir / 'printers.json', []) if isinstance(p, dict)]
        for printer in ((config.get('printers') or []) + added)[:20]:
            if isinstance(printer, dict) and printer.get('ip'):
                printers.append(dict(name=printer.get('name', '?'), mqtt=self.reachable(printer['ip'], 8883)))
        tailscale = ''
        if shutil.which('tailscale'):
            state = load_json_text(self.run(['tailscale', 'status', '--json']).stdout).get('BackendState', '')
            tailscale = state or 'unknown'
        return dict(printers=printers, discord=self.reachable('discord.com', 443), github=self.reachable('api.github.com', 443),
                    tailscale=tailscale)

    def snapshot(self):
        """One full look: dict with everything, plus 'problems' [(key, text)]."""
        config = self.config()
        service = self.service()
        port = int(config.get('port', 8080) or 8080)
        healthy, release = self.health(port) if service['active'] == 'active' else (False, '')
        journal = self.journal(lines=200)
        result = dict(time=int(self.clock()), service=service, healthy=healthy, release=release if healthy else '',
                      startup_error=self.startup_error(journal) if not healthy else '', disk=self.disk(), memory=self.memory(),
                      temperature=self.temperature(), throttled=self.throttled(), network=self.network(config),
                      updater=redact(load_json(UPDATER_STATUS, {})), doctor=DOCTOR_VERSION)
        result['problems'] = problems(result)
        return result


def load_json_text(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def problems(s):
    """What is wrong in one snapshot: [(key, text)]."""
    found = []
    active = s['service']['active']
    if active in ('failed', 'inactive'):
        found.append(('service_down', f"The app service is {active}" + (f": {s['startup_error'].splitlines()[-1][:200]}" if s['startup_error'] else '.')))
    elif active == 'active' and not s['healthy']:
        found.append(('not_responding', 'The app service is running but its dashboard does not answer (frozen or still starting).'))
    elif active == 'activating':
        found.append(('starting', 'The app service is starting or restarting.'))
    for disk in s['disk']:
        if disk['free_gb'] < 1 or disk['free_pct'] < 5:
            found.append(('disk_low', f"Low disk space on {disk['label']}: {disk['free_gb']} GB ({disk['free_pct']}%) free."))
    if s['memory'] and s['memory']['available_mb'] < 100:
        found.append(('memory_low', f"Low memory: {s['memory']['available_mb']} MB available."))
    if s['temperature'] is not None and s['temperature'] >= 80:
        found.append(('hot', f"CPU temperature {s['temperature']} °C."))
    if 'now' in s['throttled']:
        found.append(('power', f"Raspberry Pi reports {s['throttled']}: check the power supply."))
    return found


# ---- incidents and alerts ------------------------------------------------------------------------------------------

class Doctor:
    """Turns repeated checks into incidents: one record and one alert when something goes wrong, one when it clears."""

    SERIOUS = ('service_down', 'not_responding', 'restart_loop', 'disk_low', 'memory_low', 'hot', 'power')

    def __init__(self, checks=None, settings=None, state=STATE, clock=time.time, sleep=time.sleep, poster=None):
        self.checks, self.clock, self.sleep = checks or Checks(), clock, sleep
        self.settings = {**DEFAULTS, **(settings if settings is not None else load_json(CONFIG, {}))}
        self.state = Path(state)
        self.poster = poster or post_json
        self.last, self.open, self.not_responding, self.restarts_seen, self.restarted_at = None, None, 0, [], 0
        self.lock = threading.Lock()

    def tick(self):
        """One check. Returns the snapshot (with the open incident, if any)."""
        snap = self.checks.snapshot()
        keys = {k for k, _ in snap['problems']}
        self.not_responding = self.not_responding + 1 if 'not_responding' in keys else 0
        if 'not_responding' in keys and self.not_responding < FREEZE_CHECKS:   # give a slow start a chance
            snap['problems'] = [p for p in snap['problems'] if p[0] != 'not_responding']
        restarts = snap['service']['restarts']
        now = self.clock()
        if self.last and restarts > self.last['service']['restarts']:
            self.restarts_seen += [now] * (restarts - self.last['service']['restarts'])
        self.restarts_seen = [t for t in self.restarts_seen if now - t <= RESTART_LOOP[1]]
        if len(self.restarts_seen) >= RESTART_LOOP[0]:
            snap['problems'].append(('restart_loop', f"The app restarted {len(self.restarts_seen)} times in the last {RESTART_LOOP[1] // 60} minutes."))
        serious = [p for p in snap['problems'] if p[0] in self.SERIOUS]
        with self.lock:
            if serious and not self.open:
                self.open_incident(snap, serious)
            elif not serious and self.open:
                self.close_incident(snap)
            elif serious and self.open:
                self.open['problems'] = serious
            if self.open and self.not_responding >= FREEZE_CHECKS:
                self.maybe_restart(snap)
            snap['incident'] = self.open
            self.last = snap
        return snap

    def open_incident(self, snap, serious):
        incident_id = time.strftime('%Y%m%d-%H%M%S', time.localtime(self.clock())) + '-' + secrets.token_hex(2)
        stack = ''
        if any(k == 'not_responding' for k, _ in serious):
            stack = self.stack_dump()
        self.open = dict(id=incident_id, opened=int(self.clock()), problems=serious)
        self.save(incident_id, snap, stack)
        self.alert(f"🩺 Problem on the printer Pi: {serious[0][1]}", '\n'.join(text for _, text in serious), incident_id, 0xE74C3C, snap)

    def close_incident(self, snap):
        incident, self.open = self.open, None
        minutes = max(1, round((self.clock() - incident['opened']) / 60))
        self.alert('✅ Printer Pi back to normal', f"Incident {incident['id']} cleared after {minutes} min.", incident['id'], 0x2ECC71, snap, closing=True)

    def stack_dump(self):
        """Ask the frozen app to write every thread's stack to its log (faulthandler on SIGUSR1), then read it."""
        self.checks.run(['systemctl', 'kill', '-s', 'SIGUSR1', '--kill-whom=main', SERVICE])
        self.sleep(2)
        journal = self.checks.journal(lines=400)
        start = journal.rfind('Current thread')
        start = journal.rfind('Thread 0x', 0, start) if start > 0 else -1
        return journal[start:] if start >= 0 else ''

    def maybe_restart(self, snap):
        minutes = float(self.settings.get('restart_after_minutes') or 0)
        frozen_for = self.not_responding * float(self.settings.get('interval') or 30) / 60
        if minutes <= 0 or frozen_for < minutes or self.clock() - self.restarted_at < 600:
            return
        self.restarted_at = self.clock()
        self.checks.run(['systemctl', 'restart', SERVICE], 120)
        self.open.setdefault('actions', []).append(f"Restarted the app after {frozen_for:.0f} min without an answer.")
        self.alert('🔁 Restarted the app', f'It had not answered for {frozen_for:.0f} minutes (auto-restart is on).', self.open['id'], 0xF1C40F, snap)

    # records
    def save(self, incident_id, snap, stack=''):
        folder = self.state / 'incidents' / incident_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'checks.json').write_text(json.dumps(redact(snap), indent=2, default=str))
        (folder / 'app-journal.txt').write_text(self.checks.journal(lines=300))
        (folder / 'updater-journal.txt').write_text(self.checks.journal('pm-web-update', 100))
        log = self.checks.data_dir / 'logs' / 'management.log'
        try:
            (folder / 'management-log-tail.txt').write_text(redact_text('\n'.join(log.read_text(errors='replace').splitlines()[-300:])))
        except OSError:
            pass
        if stack:
            (folder / 'stack-dump.txt').write_text(redact_text(stack))
        for old in sorted((self.state / 'incidents').iterdir())[:-KEEP_INCIDENTS]:
            shutil.rmtree(old, ignore_errors=True)

    def incidents(self):
        folder = self.state / 'incidents'
        return sorted((p.name for p in folder.iterdir() if p.is_dir()), reverse=True) if folder.is_dir() else []

    def incident_zip(self, incident_id=None, snap=None):
        """ZIP of one incident (or a fresh report). Every file is redacted again on the way out."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            if incident_id:
                if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{4}', incident_id or '') or incident_id not in self.incidents():
                    raise ValueError('Unknown incident.')
                for file in sorted((self.state / 'incidents' / incident_id).iterdir()):
                    archive.writestr(f'{incident_id}/{file.name}', redact_text(file.read_text(errors='replace')))
            else:
                snap = snap or self.checks.snapshot()
                archive.writestr('report/checks.json', json.dumps(redact(snap), indent=2, default=str))
                archive.writestr('report/app-journal.txt', self.checks.journal(lines=300))
                archive.writestr('report/updater-journal.txt', self.checks.journal('pm-web-update', 100))
        return buffer.getvalue()

    # alerts
    def alert(self, title, text, incident_id, color, snap, closing=False):
        webhook = self.settings.get('discord_webhook')
        detail = f"{text}\n\nIncident `{incident_id}` • app {snap['service']['active']}" + (f" • release {snap['release']}" if snap.get('release') else '')
        if snap.get('startup_error') and not closing:
            detail += f"\n```\n{snap['startup_error'][-900:]}\n```"
        if webhook:
            self.poster(webhook, dict(embeds=[dict(title=title[:256], description=redact_text(detail)[:4000], color=color)],
                                      allowed_mentions={'parse': []}))
        github = self.settings.get('github') or {}
        if github.get('repository') and github.get('token') and not closing:
            body = f"{redact_text(detail)}\n\n<details><summary>Checks</summary>\n\n```json\n{json.dumps(redact(snap), indent=2, default=str)[:20000]}\n```\n</details>\n\n_Opened by pm-doctor {DOCTOR_VERSION}._"
            self.poster(f"https://api.github.com/repos/{github['repository']}/issues", dict(title=title[:200], body=body),
                        {'Authorization': 'Bearer ' + github['token'], 'Accept': 'application/vnd.github+json'})


def post_json(url, payload, headers=None):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'User-Agent': f'pm-doctor/{DOCTOR_VERSION}', **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except Exception as exc:
        print(f'pm-doctor: alert to {redact_text(url)} failed: {type(exc).__name__}', file=sys.stderr, flush=True)
        return None


# ---- status page ---------------------------------------------------------------------------------------------------

def check_password(password, auth_file=DATA_DIR / 'auth.json'):
    """The dashboard password, checked against the dashboard's own scrypt hash (dashboard.password_hash)."""
    auth = load_json(auth_file, {})
    if not auth.get('salt') or not auth.get('hash') or not 1 <= len(password) <= 1024:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(auth['salt']), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(digest, auth['hash'])


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Printer Pi doctor</title><style>
:root{{--bg:#0f1412;--panel:#18201c;--text:#e8efe9;--muted:#9fb0a5;--red:#ff6b61;--green:#8be28b;--line:#2a3530}}
@media (prefers-color-scheme: light){{:root{{--bg:#f5f7f5;--panel:#fff;--text:#16201a;--muted:#55645a;--line:#dbe3dd;--red:#c0392b;--green:#1e8449}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}}main{{max-width:860px;margin:0 auto;padding:16px}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}}h1{{font-size:22px}}h2{{font-size:16px;margin:0 0 8px}}
.bad{{color:var(--red)}}.good{{color:var(--green)}}.muted{{color:var(--muted)}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}
button,input{{font:inherit;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text)}}table{{width:100%;border-collapse:collapse}}td{{padding:4px 6px;border-bottom:1px solid var(--line)}}
</style></head><body><main><h1>🩺 Printer Pi doctor</h1>{body}</main></body></html>"""


class StatusServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def make_handler(doctor, auth_file=DATA_DIR / 'auth.json'):
    sessions, failures = {}, {}

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'pm-doctor'

        def log_message(self, *args):
            pass

        def session(self):
            for part in self.headers.get('Cookie', '').split(';'):
                name, _, value = part.strip().partition('=')
                if name == 'pmdoctor' and value in sessions and sessions[value]['expires'] > time.time():
                    return sessions[value]
            return None

        def send(self, status, body, kind='text/html; charset=utf-8', headers=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(status)
            for key, value in {'Content-Type': kind, 'Content-Length': str(len(data)), 'Cache-Control': 'no-store',
                               'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
                               'X-Frame-Options': 'DENY', **(headers or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def form(self):
            length = min(int(self.headers.get('Content-Length') or 0), 4096)
            return dict(urllib.parse.parse_qsl(self.rfile.read(length).decode(errors='replace')))

        def do_GET(self):
            session = self.session()
            path = urllib.parse.urlparse(self.path)
            if path.path == '/health':
                return self.send(200, json.dumps({'doctor': DOCTOR_VERSION}), 'application/json')
            if not session:
                return self.send(200, PAGE.format(body='<section><h2>Sign in</h2><p class="muted">Use the dashboard password.</p><form method="post" action="/login"><input type="password" name="password" autocomplete="current-password" required> <button>Sign in</button></form></section>'))
            if path.path == '/status.json':
                return self.send(200, json.dumps(redact(doctor.last or {}), default=str), 'application/json')
            if path.path == '/incident.zip':
                query = dict(urllib.parse.parse_qsl(path.query))
                try:
                    data = doctor.incident_zip(query.get('id') or None)
                except ValueError as exc:
                    return self.send(404, str(exc), 'text/plain')
                name = (query.get('id') or 'report-' + time.strftime('%Y%m%d-%H%M%S')) + '.zip'
                return self.send(200, data, 'application/zip', {'Content-Disposition': f'attachment; filename="pm-doctor-{name}"'})
            if path.path == '/':
                return self.send(200, PAGE.format(body=render(doctor, session['csrf'])))
            self.send(404, 'Not found', 'text/plain')

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            peer = self.client_address[0]
            if path == '/login':
                recent = [t for t in failures.get(peer, []) if t > time.time() - 300]
                if len(recent) >= 8:
                    return self.send(429, 'Too many attempts. Wait five minutes.', 'text/plain')
                if not check_password(self.form().get('password', ''), auth_file):
                    failures[peer] = recent + [time.time()]
                    return self.send(401, PAGE.format(body='<section><p class="bad">Incorrect password.</p><a href="/">Try again</a></section>'))
                token = secrets.token_urlsafe(32)
                sessions[token] = dict(csrf=secrets.token_urlsafe(24), expires=time.time() + 12 * 3600)
                return self.send(303, '', headers={'Location': '/', 'Set-Cookie': f'pmdoctor={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'})
            session = self.session()
            form = self.form()
            if not session or not hmac.compare_digest(form.get('csrf', ''), session['csrf']):
                return self.send(403, 'Sign in again.', 'text/plain')
            if path == '/restart':
                doctor.checks.run(['systemctl', 'restart', SERVICE], 120)
                return self.send(303, '', headers={'Location': '/?restarted=1'})
            if path == '/logout':
                sessions.clear()
                return self.send(303, '', headers={'Location': '/'})
            self.send(404, 'Not found', 'text/plain')

    return Handler



def render(doctor, csrf):
    s = doctor.last or {}
    if not s:
        return '<section><p class="muted">First check in progress… reload in a few seconds.</p></section>'
    e = html.escape
    service = s['service']
    state = 'good' if s['healthy'] else 'bad'
    rows = [f"<section><h2>App</h2><p class='{state}'><b>{e(service['active'])}</b> ({e(service['sub'])}) • "
            f"{'dashboard answers' if s['healthy'] else 'dashboard does not answer'}{' • release ' + e(s['release']) if s.get('release') else ''} • "
            f"{service['restarts']} restarts</p>"]
    if s.get('startup_error'):
        rows.append(f"<pre>{e(s['startup_error'])}</pre>")
    rows.append(f"<form method='post' action='/restart'><input type='hidden' name='csrf' value='{e(csrf)}'><button>Restart the app</button></form></section>")
    incident = s.get('incident')
    rows.append('<section><h2>Problems</h2>' + (''.join(f"<p class='bad'>{e(t)}</p>" for _, t in s['problems']) or "<p class='good'>None.</p>") +
                (f"<p class='muted'>Open incident {e(incident['id'])}</p>" if incident else '') + '</section>')
    health = [f"Disk {d['label']}: {d['free_gb']} GB free ({d['free_pct']}%)" for d in s['disk']]
    if s['memory']:
        health.append(f"Memory: {s['memory']['available_mb']} of {s['memory']['total_mb']} MB available")
    if s['temperature'] is not None:
        health.append(f"CPU {s['temperature']} °C")
    if s['throttled']:
        health.append('Power: ' + s['throttled'])
    rows.append('<section><h2>Pi</h2>' + ''.join(f'<p>{e(h)}</p>' for h in health) + '</section>')
    net = s['network']
    printers = ''.join(f"<tr><td>{e(p['name'])}</td><td class='{'good' if p['mqtt'] else 'bad'}'>{'reachable' if p['mqtt'] else 'not reachable'} (MQTT 8883)</td></tr>" for p in net['printers'])
    rows.append(f"<section><h2>Network</h2><table>{printers}</table><p>Discord {'✅' if net['discord'] else '❌'} • GitHub {'✅' if net['github'] else '❌'}"
                + (f" • Tailscale {e(net['tailscale'])}" if net['tailscale'] else '') + '</p></section>')
    links = ''.join(f"<li><a href='/incident.zip?id={e(i)}'>{e(i)}</a></li>" for i in doctor.incidents()[:15])
    rows.append(f"<section><h2>Incidents</h2><ul>{links or '<li class=muted>None recorded.</li>'}</ul><p><a href='/incident.zip'>Download a report now</a> • "
                f"<a href='/status.json'>status.json</a></p><p class='muted'>pm-doctor {DOCTOR_VERSION} • checked {time.strftime('%H:%M:%S', time.localtime(s['time']))}</p></section>")
    return ''.join(rows)


def serve(doctor, addresses, port):
    servers = []
    for address in addresses:
        try:
            server = StatusServer((address, port), make_handler(doctor))
        except OSError as exc:
            print(f'pm-doctor: status page not on {address}:{port} ({exc})', file=sys.stderr, flush=True)
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    return servers


# ---- command line --------------------------------------------------------------------------------------------------

def print_checks(snap):
    service = snap['service']
    print(f"App service: {service['active']} ({service['sub']}), {service['restarts']} restarts, "
          f"dashboard {'answers' if snap['healthy'] else 'does NOT answer'}{' (release ' + snap['release'] + ')' if snap['release'] else ''}")
    if snap['startup_error']:
        print('Last start-up error:\n  ' + snap['startup_error'].replace('\n', '\n  '))
    for disk in snap['disk']:
        print(f"Disk {disk['label']}: {disk['free_gb']} GB free ({disk['free_pct']}%)")
    if snap['memory']:
        print(f"Memory: {snap['memory']['available_mb']} MB available of {snap['memory']['total_mb']} MB")
    if snap['temperature'] is not None:
        print(f"CPU temperature: {snap['temperature']} °C")
    if snap['throttled']:
        print('Power: ' + snap['throttled'])
    net = snap['network']
    for printer in net['printers']:
        print(f"Printer {printer['name']}: {'reachable' if printer['mqtt'] else 'NOT reachable'} (MQTT 8883)")
    print(f"Internet: Discord {'ok' if net['discord'] else 'NOT reachable'}, GitHub {'ok' if net['github'] else 'NOT reachable'}"
          + (f", Tailscale {net['tailscale']}" if net['tailscale'] else ''))
    print('Problems: ' + ('; '.join(text for _, text in snap['problems']) if snap['problems'] else 'none'))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pm-doctor', description='Independent watchdog for 3D Printer Management.')
    parser.add_argument('command', nargs='?', default='check', choices=('check', 'report', 'run'))
    parser.add_argument('--version', action='version', version=DOCTOR_VERSION)
    args = parser.parse_args(argv)
    doctor = Doctor()
    if args.command == 'check':
        print_checks(doctor.checks.snapshot())
        return 0
    if args.command == 'report':
        doctor.state.mkdir(parents=True, exist_ok=True)
        target = doctor.state / f"report-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        target.write_bytes(doctor.incident_zip())
        os.chmod(target, 0o600)
        print(target)
        return 0
    doctor.state.mkdir(parents=True, exist_ok=True)
    if doctor.settings.get('status_page'):
        config = doctor.checks.config()
        addresses = list(dict.fromkeys(['127.0.0.1', *(config.get('listen') or [])]))
        serve(doctor, addresses, int(doctor.settings.get('port') or 8081))
    interval = max(10, int(doctor.settings.get('interval') or 30))
    while True:
        try:
            doctor.tick()
        except Exception as exc:   # the doctor itself must never stop
            print(f'pm-doctor: check failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(main())
