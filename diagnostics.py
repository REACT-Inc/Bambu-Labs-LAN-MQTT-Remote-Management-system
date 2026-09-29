"""Persistent error logging and redacted diagnostic reports for troubleshooting."""
import asyncio
import collections
import io
import json
import logging
import logging.handlers
import platform
import re
import secrets
import sys
import threading
import time
import zipfile
from pathlib import Path

LOG_NAME = 'management.log'
LOG_BYTES, LOG_BACKUPS = 2 * 1024 * 1024, 5
FORMAT = '%(asctime)s %(levelname)s [%(name)s] %(message)s'
SECRET_KEYS = re.compile(r'access_code|token|password|secret|hash|salt|api_key|credential', re.I)
SECRET_TEXT = [
    (re.compile(r'[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}'), '[discord-token]'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'), '[github-token]'),
    (re.compile(r'''((?:access_code|token|password|secret)["']?\s*[:=]\s*["']?)[^\s"',}]+''', re.I), r'\1[redacted]'),
]
recent_errors = collections.deque(maxlen=100)
_errors_lock = threading.Lock()
_installed = False


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


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_text(super().format(record))


class RecentErrors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.setFormatter(RedactingFormatter(FORMAT))

    def emit(self, record):
        try:
            entry = {'time': record.created, 'level': record.levelname, 'logger': record.name,
                                  'error_id': getattr(record, 'error_id', None), 'text': self.format(record)[-4000:]}
            with _errors_lock:
                recent_errors.append(entry)
        except Exception:
            self.handleError(record)


def snapshot_errors():
    with _errors_lock:
        return list(recent_errors)


def log_dir(data_dir):
    return Path(data_dir) / 'logs'


def setup(data_dir, log=None):
    """Write all logs to a rotating file in the data directory and capture crashes. Safe to call twice."""
    global _installed
    log = log or logging.getLogger('printer-bot')
    if _installed:
        return log
    root = logging.getLogger()
    try:
        folder = log_dir(data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(folder / LOG_NAME, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding='utf-8')
        handler.setFormatter(RedactingFormatter(FORMAT))
        root.addHandler(handler)
    except OSError:
        log.exception('Log file unavailable; logging to the service journal only')
    root.addHandler(RecentErrors())
    logging.captureWarnings(True)

    def excepthook(kind, error, traceback):
        if not issubclass(kind, KeyboardInterrupt):
            log.critical('Unhandled exception', exc_info=(kind, error, traceback))
        sys.__excepthook__(kind, error, traceback)

    def thread_hook(args):
        if not issubclass(args.exc_type, SystemExit):
            log.critical('Unhandled exception in thread %s', getattr(args.thread, 'name', '?'),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook, threading.excepthook = excepthook, thread_hook
    _installed = True
    return log


def install_loop_handler(loop, log):
    def handler(loop, context):
        error = context.get('exception')
        log.error('Unhandled asyncio error: %s', context.get('message', 'no message'),
                  exc_info=(type(error), error, error.__traceback__) if error else None)
    loop.set_exception_handler(handler)


def log_error(log, message, error=None):
    """Log an exception with a short reference ID that can be shown to the user."""
    error_id = secrets.token_hex(4)
    log.error('%s [error ID %s]', message, error_id, extra={'error_id': error_id},
              exc_info=(type(error), error, error.__traceback__) if error else True)
    return error_id


def tail(path, limit):
    with open(path, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read()
    return data if size <= limit else b'[... earlier lines omitted ...]\n' + data.split(b'\n', 1)[-1]


def printer_summary(core):
    printers = []
    for name in core.names():
        try:
            state, error, data, connected = core.state_data(name)
        except Exception as exc:
            printers.append({'name': name, 'error': f'state unavailable: {exc!r}'})
            continue
        seen = getattr(core, 'last_seen', {}).get(name)
        config = core.printer_config(name) or {}
        printers.append({
            'name': name, 'model': config.get('model'), 'ip': config.get('ip'), 'serial': config.get('serial'),
            'connected': connected, 'state': state, 'print_error': error,
            'seconds_since_report': round(time.time() - seen, 1) if seen else None,
            'hms': data.get('hms'), 'latest_report': data,
        })
    return printers


def build_report(core, store=None, log_bytes=6 * 1024 * 1024):
    """Return (filename, zip bytes) with system info, printer state, recent errors/events and logs. Secrets are redacted."""
    try:
        from Updater.version import VERSION
    except Exception:
        VERSION = 'unknown'
    bot = getattr(core, 'bot', None)
    summary = {
        'generated': time.strftime('%Y-%m-%d %H:%M:%S %z'),
        'version': VERSION,
        'python': sys.version.split()[0], 'platform': platform.platform(),
        'uptime_seconds': round(time.monotonic() - getattr(core, 'STARTED', time.monotonic())),
        'demo_mode': getattr(core, 'EXAMPLE_MODE', None),
        'discord_connected': bool(bot and bot.is_ready()) if bot else None,
    }
    files = {
        'summary.json': summary,
        'printers.json': printer_summary(core),
        'config.json': getattr(core, 'CONFIG', {}),
        'settings.json': getattr(core, 'settings', {}),
        'recent_errors.json': snapshot_errors(),
    }
    if store is not None:
        try:
            files['events.json'] = store.events()
        except Exception as exc:
            files['events.json'] = {'error': repr(exc)}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, json.dumps(redact(value), indent=2, default=str))
        folder = log_dir(core.DATA_DIR)
        logs = sorted(folder.glob(LOG_NAME + '*'), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
        # Stack dumps from event-loop stalls (loop_watchdog.py): small, and the best evidence for a freeze.
        for path in (sorted(folder.glob('stalls.log*')) if folder.is_dir() else []):
            archive.writestr('logs/' + path.name, redact_text(tail(path, 512 * 1024).decode('utf-8', 'replace')))
        remaining = log_bytes
        for path in logs:
            if remaining <= 0:
                break
            data = tail(path, remaining)
            remaining -= len(data)
            archive.writestr('logs/' + path.name, redact_text(data.decode('utf-8', 'replace')))
        if not logs:
            archive.writestr('logs/README.txt', 'No log file yet. Logs are written to ' + str(folder / LOG_NAME) + ' after the service starts.')
    return time.strftime('diagnostics-%Y%m%d-%H%M%S.zip'), buffer.getvalue()


def install_discord(core, store):
    import discord
    from discord import app_commands

    @core.bot.tree.command(name='diagnostics', description='Download a diagnostic report with logs and recent errors (admins/approved users)')
    @app_commands.guild_only()
    async def diagnostics(i: discord.Interaction):
        # Admin-only via core.ADMIN_COMMANDS. Always private: the report contains printer IPs and logs.
        await i.response.defer(ephemeral=True, thinking=True)
        filename, data = await asyncio.to_thread(build_report, core, store, 7 * 1024 * 1024)
        errors = len(snapshot_errors())
        await i.followup.send(
            embed=core.card('🩺 Diagnostic report', f'{errors} error(s) since the service started. Secrets are redacted; '
                            'printer IPs and names are included. Attach it to a GitHub issue or send it to whoever is fixing the problem.'),
            file=discord.File(io.BytesIO(data), filename=filename), ephemeral=True)
