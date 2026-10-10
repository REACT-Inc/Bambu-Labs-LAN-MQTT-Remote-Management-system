"""What the desktop app remembers and decides, without any GUI code: its settings, which notifications pop up, what the
tray icon says, starting with Windows, and making sure only one copy runs. Tested on any computer
(tests/test_desktop_app.py).
"""
import copy
import hmac
import json
import os
import secrets
import socket
import sys
import threading
from pathlib import Path

APP_NAME = '3D Printer Management'
SLUG = '3d-printer-management-desktop'
NOTIFY = (('important', 'Prints, problems and AI alerts'), ('all', 'Everything, also progress every 10%'),
          ('problems', 'Only problems'), ('off', 'Off'))
DEFAULTS = dict(address='', notify='important', seen={}, told_tray=False)
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
BUSY = ('Printing', 'Preparing')


def folders(env=None, platform=None, home=None):
    """(settings folder, local data folder): %APPDATA% and %LOCALAPPDATA% on Windows. The window's sign-in and the log
    live in the local one; the Pi's address and choices in the settings one (it roams with a school profile)."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    if platform == 'win32':
        return (Path(env.get('APPDATA') or home / 'AppData' / 'Roaming') / APP_NAME,
                Path(env.get('LOCALAPPDATA') or home / 'AppData' / 'Local') / APP_NAME)
    if platform == 'darwin':
        folder = home / 'Library' / 'Application Support' / APP_NAME
        return folder, folder
    return (Path(env.get('XDG_CONFIG_HOME') or home / '.config') / SLUG,
            Path(env.get('XDG_DATA_HOME') or home / '.local' / 'share') / SLUG)


class Settings:
    """desktop.json: the Pi's address, the notification choice, and which notifications were already shown."""

    def __init__(self, path):
        self.path, self.lock = Path(path), threading.Lock()
        self.values = copy.deepcopy(DEFAULTS)
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict):
            self.values.update({key: value for key, value in saved.items()
                                if key in DEFAULTS and isinstance(value, type(DEFAULTS[key]))})
        if self.values['notify'] not in dict(NOTIFY):
            self.values['notify'] = DEFAULTS['notify']

    def __getitem__(self, key):
        return self.values[key]

    def update(self, **changes):
        with self.lock:
            self.values.update(changes)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.values, indent=2), encoding='utf-8')
            os.replace(temporary, self.path)


# ---- notifications ----

def new_notifications(summary, memory):
    """(the dashboard notifications that arrived since last time, oldest first; what to remember for next time).

    memory is what this returned last time for the same Pi. The first time ({}), nothing is shown: old notifications
    aren't replayed. The Pi app numbers its notifications from 1 again after a restart ('started' changes), so then
    the ones newer than the newest already seen count."""
    status = summary.get('status') or {}
    notes = [n for n in status.get('notifications') or [] if isinstance(n, dict)]
    started, seen_at = summary.get('started'), memory.get('seen_at', 0)
    if not memory:
        new = []
    elif memory.get('started') == started:
        new = [n for n in notes if n.get('id', 0) > memory.get('seen', 0)]
    else:
        new = [n for n in notes if n.get('at', 0) > seen_at]
    remember = dict(started=started, seen=status.get('latest', 0), seen_at=max([seen_at] + [n.get('at', 0) for n in notes]))
    return sorted(new, key=lambda n: n.get('id', 0)), remember


def wanted(note, choice):
    if choice == 'off':
        return False
    if choice == 'problems':
        return note.get('level') in ('error', 'warn')
    if choice == 'important':   # progress every 10 % is left out, as in the dashboard's own list
        return 'progress' not in str(note.get('title', '')).lower()
    return True


def fit(text, units):
    """text cut to fit a Windows text field of units UTF-16 code units, with room for the end marker: the tray's
    tooltip holds 128, a pop-up's title 64 and its message 256 (emoji take two)."""
    text = str(text)
    if len(text.encode('utf-16-le')) // 2 < units:
        return text
    while text and len((text + '…').encode('utf-16-le')) // 2 >= units:
        text = text[:-1]
    return text + '…'


def toast(notes, choice, names=None):
    """The pop-up to show for these new notifications as (title, message), or None. Several at once become one pop-up
    listing the newest three, so the screen isn't covered after the laptop has been asleep."""
    chosen = [n for n in notes if wanted(n, choice)]
    if not chosen:
        return None
    names = names or {}

    def printer(note):
        return names.get(note.get('printer')) or note.get('printer') or ''

    if len(chosen) == 1:
        note = chosen[0]
        message = ' · '.join(part for part in (printer(note), note.get('detail')) if part)
        return fit(note.get('title') or 'Notification', 64), fit(message or 'Open the app for details.', 256)
    newest = list(reversed(chosen))[:3]
    lines = ['• ' + (f"{printer(n)}: {n.get('title', '')}" if printer(n) else n.get('title', '')) for n in newest]
    if len(chosen) > len(newest):
        lines.append(f'…and {len(chosen) - len(newest)} more in the app')
    return fit(f'{len(chosen)} new notifications', 64), fit('\n'.join(lines), 256)


# ---- the tray icon ----

def minutes(value):
    try:
        total = max(0, round(float(value)))
    except (TypeError, ValueError):
        return ''
    return f'{total // 60} h {total % 60:02d} min' if total >= 60 else f'{total} min'


def percent(value):
    try:
        return f'{max(0, min(100, round(float(value))))}%'
    except (TypeError, ValueError):
        return ''


def short(text, limit=70):
    first = str(text or '').strip().splitlines()[0] if str(text or '').strip() else ''
    return first if len(first) <= limit else first[:limit - 1].rstrip() + '…'


def printer_line(printer):
    """'H2D — Printing 45% · 1 h 20 min left' for the tray menu."""
    name = printer.get('display_name') or printer.get('name') or '?'
    state = printer.get('display_state') or printer.get('state') or 'Unknown'
    if state == 'Printing':
        left = minutes(printer.get('remaining'))
        text = ' '.join(part for part in ('Printing', percent(printer.get('progress'))) if part) + (f' · {left} left' if left else '')
    elif state == 'Paused':
        text = f"Paused at {percent(printer.get('progress'))}" if percent(printer.get('progress')) else 'Paused'
    elif state == 'Error' and printer.get('error_text'):
        text = 'Error: ' + short(printer['error_text'])
    else:
        text = state
    return f'{name} — {text}'


def tray_lines(summary, problem=None):
    """The status lines at the top of the tray menu: one per printer, or what's wrong."""
    if problem:
        return [problem]
    lines = [printer_line(p) for p in summary.get('printers') or []] or ['No printers set up yet']
    return (['Demo printers (add your own in the dashboard)'] if summary.get('demo') else []) + lines


def tooltip(summary, problem=None):
    if problem:
        return fit(f'{APP_NAME}\n{problem}', 128)
    states = [p.get('display_state') or p.get('state') for p in summary.get('printers') or []]
    counts = [(sum(s in BUSY for s in states), 'printing'), (states.count('Paused'), 'paused'),
              (states.count('Error'), 'with an error'), (states.count('Offline'), 'offline'),
              (sum(s not in BUSY + ('Paused', 'Error', 'Offline') for s in states), 'ready')]
    words = ' · '.join(f'{count} {word}' for count, word in counts if count) or 'No printers yet'
    return fit(f'{APP_NAME}\n{words}', 128)


def icon_level(summary, problem=None):
    """The tray icon's dot, like the dashboard's status icon: error, warn, busy, offline, or ok (no dot)."""
    if problem:
        return 'offline'
    level = (summary.get('status') or {}).get('level')
    return level if level in ('error', 'warn', 'busy') else 'ok'


def menu_text(text, windows=sys.platform == 'win32'):
    """Windows menus treat & as a keyboard shortcut marker: 'Tom & Jerry' would show as 'Tom  Jerry'."""
    return text.replace('&', '&&') if windows else text


# ---- starting with Windows ----

class Startup:
    """Start with Windows, in the tray: a value in this user's Run key that points at this .exe."""

    def __init__(self, exe, registry=None):
        self.exe = str(exe) if exe else ''
        if registry is None and self.exe:
            try:
                import winreg as registry
            except ImportError:
                registry = None
        self.registry = registry

    @property
    def command(self):
        return f'"{self.exe}" --tray'

    def available(self):
        return bool(self.exe and self.registry)

    def read(self):
        if not self.available():
            return None
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                return self.registry.QueryValueEx(key, APP_NAME)[0]
        except OSError:
            return None

    def enabled(self):
        return self.read() is not None

    def set(self, on):
        with self.registry.CreateKeyEx(self.registry.HKEY_CURRENT_USER, RUN_KEY, 0, self.registry.KEY_SET_VALUE) as key:
            if on:
                self.registry.SetValueEx(key, APP_NAME, 0, self.registry.REG_SZ, self.command)
            else:
                try:
                    self.registry.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass

    def repair(self):
        """After the .exe was moved, or a newer download is run from elsewhere: point the entry at this copy."""
        value = self.read()
        if value is not None and value != self.command:
            self.set(True)
            return True
        return False


# ---- one copy at a time ----

class SingleInstance:
    """One copy per Windows user. The running copy listens on a random local port and writes the port, with a secret,
    to a file in this user's own folder. Starting the app again asks that copy to show its window, then exits."""
    COMMANDS = ('show', 'quit')

    def __init__(self, folder):
        self.file, self.server, self.token = Path(folder) / 'running.json', None, ''

    def running(self):
        try:
            info = json.loads(self.file.read_text(encoding='utf-8'))
            return info if isinstance(info, dict) and {'port', 'token'} <= set(info) else None
        except (OSError, ValueError):
            return None

    def signal(self, command='show', timeout=3.0):
        """True when a running copy got the command."""
        info = self.running()
        if not info:
            return False
        try:
            with socket.create_connection(('127.0.0.1', int(info['port'])), timeout=timeout) as connection:
                connection.sendall(json.dumps(dict(token=info['token'], command=command)).encode() + b'\n')
                return connection.makefile('rb').readline(16).strip() == b'ok'
        except (OSError, ValueError, TypeError):
            return False

    def listen(self, handle):
        """Become the running copy: handle(command) is called on a background thread for 'show' and 'quit'."""
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):   # Windows: no other program can take over the port
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(4)
        self.token = secrets.token_urlsafe(24)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(port=self.server.getsockname()[1], token=self.token, pid=os.getpid())),
                             encoding='utf-8')
        os.replace(temporary, self.file)
        threading.Thread(target=self._serve, args=(self.server, handle), daemon=True, name='single-instance').start()

    def _serve(self, server, handle):
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return   # closed
            with connection:
                try:
                    connection.settimeout(3)
                    message = json.loads(connection.makefile('rb').readline(512) or b'{}')
                    command = message.get('command')
                    ok = (hmac.compare_digest(str(message.get('token', '')), self.token) and command in self.COMMANDS)
                    connection.sendall(b'ok\n' if ok else b'no\n')
                except (OSError, ValueError, AttributeError):
                    continue
            if ok:
                handle(command)

    def close(self):
        if self.server:
            self.server.close()
            if (self.running() or {}).get('token') == self.token:
                try:
                    self.file.unlink()
                except OSError:
                    pass
