"""Set the app up from the dashboard (#8): printers, and the optional Discord bot. No file editing, no Discord needed.

The dashboard is the primary way to use the app; Discord is an optional extra. So a new installation is set up here:
- **Printers:** add, edit and remove printers (name, IP address, serial number, access code, model, camera). Saved
  in the data folder (printers.json, readable only by the service), next to the printers in config.json. Printers
  from config.json are shown but can only be changed there.
- **Discord (optional):** the bot token and the servers it may be used in (discord.json in the data folder). Values
  in config.json take precedence.
- **Restart now:** printers and Discord are connected when the app starts, so changes apply after a restart. The
  dashboard asks systemd to start the app again (it exits with RESTART_EXIT, which systemd restarts after a failure).

Secrets never go back to the browser: access codes and the bot token are write-only (blank keeps the saved one).
A printer can't be renamed (queue history uses its name; the display name can be changed with /rename) and can't be
removed while it has queue jobs waiting or running.
"""
import asyncio
import json
import os
import re
import signal

from aiohttp import web

import printer_models

PRINTERS_FILE = 'printers.json'
DISCORD_FILE = 'discord.json'
RESTART_EXIT = 75   # EX_TEMPFAIL: a non-zero exit, so systemd's Restart=on-failure starts the app again
CAMERAS = ('auto', 'none', 'jpeg_tcp', 'rtsp')
WAITING = ('queued', 'staging', 'awaiting_start', 'printing', 'paused', 'needs_review')
HOST = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$')


def read(data_dir, name, default):
    """A dashboard-managed file from the data folder, or default when it's missing or unreadable."""
    try:
        value = json.loads((data_dir / name).read_text())
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def dashboard_printers(data_dir):
    return [p for p in read(data_dir, PRINTERS_FILE, []) if isinstance(p, dict) and p.get('name')]


def merged_printers(config, data_dir):
    """config.json's printers, then the dashboard's (a dashboard printer with a config.json printer's name is skipped)."""
    printers = list(config.get('printers') or [])
    taken = {str(p.get('name', '')).casefold() for p in printers}
    for printer in dashboard_printers(data_dir):
        if printer['name'].casefold() not in taken:
            printers.append(dict(printer, managed=True))
            taken.add(printer['name'].casefold())
    return printers


def discord_settings(config, data_dir):
    """(token, server IDs): from config.json when set there, else from the dashboard."""
    saved = read(data_dir, DISCORD_FILE, {})
    token = config.get('discord_token') or saved.get('token') or ''
    guilds = config.get('guild_ids') or saved.get('guild_ids') or []
    return token, {int(g) for g in guilds if str(g).isdigit()}


def write(path, value):
    """Written atomically and readable only by the service (it holds access codes or the bot token)."""
    temporary = path.with_suffix('.tmp')
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Setup:
    def __init__(self, core, store, app=None, kill=os.kill):
        self.core, self.store, self.kill = core, store, kill
        self.data = core.DATA_DIR
        # What this run of the app started with: saved changes differ from it until the next restart.
        self.started_with = {name: self.fingerprint(name) for name in (PRINTERS_FILE, DISCORD_FILE)}
        if app is not None:
            app.add_routes([web.get('/api/setup', self.state_route), web.post('/api/setup/printer', self.printer_route),
                            web.post('/api/setup/printer/remove', self.remove_route),
                            web.post('/api/setup/discord', self.discord_route), web.post('/api/setup/restart', self.restart_route)])

    # ---- what's set up ----

    def fingerprint(self, name):
        return json.dumps(read(self.data, name, {} if name == DISCORD_FILE else []), sort_keys=True)

    def pending(self, name):
        """Saved since the app started, so not applied yet (printers and Discord connect when the app starts)."""
        return self.fingerprint(name) != self.started_with[name]

    @property
    def config(self):
        return getattr(self.core, 'CONFIG', {}) or {}

    def config_names(self):
        return {str(p.get('name', '')).casefold() for p in self.config.get('printers') or []}

    @staticmethod
    def can_restart():
        """Only when systemd runs the app (it sets INVOCATION_ID), so something starts it again."""
        return bool(os.environ.get('INVOCATION_ID'))

    def state(self):
        def row(p, source):
            key = printer_models.key_for(p)
            return dict(name=p['name'], ip=p.get('ip', ''), serial=p.get('serial', ''), model=p.get('model', ''),
                        model_label=printer_models.label(key) if key else 'unknown model', camera=p.get('camera_type') or 'none',
                        has_access_code=bool(p.get('access_code')), source=source)
        managed = dashboard_printers(self.data)
        token, guilds = discord_settings(self.config, self.data)
        saved = read(self.data, DISCORD_FILE, {})
        return dict(
            printers=[row(p, 'config') for p in self.config.get('printers') or []] +
                     [row(p, 'dashboard') for p in managed if p['name'].casefold() not in self.config_names()],
            discord=dict(has_token=bool(token), token_in_config=bool(self.config.get('discord_token')),
                         guild_ids=[str(g) for g in sorted(guilds)], guilds_in_config=bool(self.config.get('guild_ids')),
                         saved_token=bool(saved.get('token')), connected=bool(getattr(self.core.bot, 'is_ready', lambda: False)())),
            models=[dict(key=key, name=m['name'], camera=m['camera']) for key, m in printer_models.MODELS.items()],
            pending_restart=self.pending(PRINTERS_FILE) or self.pending(DISCORD_FILE), pending_discord=self.pending(DISCORD_FILE),
            can_restart=self.can_restart())

    # ---- printers ----

    def save_printer(self, data, author):
        """Add a printer, or (edit=True) change a dashboard printer's details. Returns the new state."""
        if not isinstance(data, dict):
            raise ValueError('Send the printer details.')
        edit = data.get('edit') is True
        name = ' '.join(str(data.get('name', '')).split())
        if not 1 <= len(name) <= 40 or any(not c.isprintable() for c in name):
            raise ValueError('Give the printer a name (up to 40 characters).')
        managed = dashboard_printers(self.data)
        existing = next((p for p in managed if p['name'].casefold() == name.casefold()), None)
        if name.casefold() in self.config_names():
            raise ValueError(f'{name} is set in config.json; change it there.')
        if edit and not existing:
            raise ValueError('Unknown printer.')
        if not edit and existing:
            raise ValueError(f'There is already a printer called {existing["name"]}.')
        ip = str(data.get('ip', '')).strip()
        if not HOST.match(ip):
            raise ValueError("Enter the printer's IP address (shown on the printer under Settings → Network), for example 192.168.1.40.")
        serial = str(data.get('serial', '')).strip().upper()
        if not re.fullmatch(r'[A-Z0-9]{8,20}', serial):
            raise ValueError("Enter the printer's serial number (letters and numbers, shown on the printer under Settings → Device).")
        code = str(data.get('access_code', '')).strip()
        if not code and existing:
            code = existing.get('access_code', '')   # blank keeps the saved access code
        if not re.fullmatch(r'[A-Za-z0-9]{4,32}', code):
            raise ValueError("Enter the printer's LAN access code (shown on the printer under Settings → Network / LAN only).")
        model = str(data.get('model', '') or '').strip()
        if model and model not in printer_models.MODELS:
            raise ValueError('Choose a printer model from the list, or Automatic.')
        camera = data.get('camera', 'auto') or 'auto'
        if camera not in CAMERAS:
            raise ValueError('Choose the camera: Automatic, None, JPEG (A1 / P1 family) or RTSP (X1 / H2 family).')
        entry = dict(name=existing['name'] if existing else name, ip=ip, serial=serial, access_code=code)
        if model:
            entry['model'] = printer_models.MODELS[model]['name']
        if camera == 'auto':   # from the model (chosen, or recognised from the serial number)
            key = printer_models.key_for(entry)
            camera = printer_models.MODELS[key]['camera'] if key in printer_models.MODELS else 'none'
        if camera != 'none':
            entry['camera_type'] = camera
        managed = [entry if p is existing else p for p in managed] if existing else managed + [entry]
        write(self.data / PRINTERS_FILE, managed)
        self.store.event(entry['name'], 'Printer changed' if existing else 'Printer added',
                         f"{entry['ip']} • {entry.get('model') or printer_models.label(printer_models.key_for(entry))} • {author} "
                         '• applies after a restart')
        return self.state()

    def remove_printer(self, name, author):
        managed = dashboard_printers(self.data)
        existing = next((p for p in managed if p['name'] == name), None)
        if not existing:
            raise ValueError('Only printers added in the dashboard can be removed here; others are in config.json.')
        waiting = [j for j in self.store.jobs(name) if j['status'] in WAITING]
        if waiting:
            raise ValueError(f'{name} has {len(waiting)} queue job(s) waiting or running. Finish or remove them first.')
        write(self.data / PRINTERS_FILE, [p for p in managed if p is not existing])
        self.store.event(name, 'Printer removed', f'{author} • applies after a restart')
        return self.state()

    # ---- Discord (optional) ----

    def save_discord(self, data, author):
        if not isinstance(data, dict):
            raise ValueError('Send the Discord settings.')
        saved = read(self.data, DISCORD_FILE, {})
        token = str(data.get('token', '')).strip()
        if data.get('clear_token') is True:
            token = ''
        elif not token:
            token = saved.get('token', '')   # blank keeps the saved token
        elif not re.fullmatch(r'[A-Za-z0-9._-]{50,100}', token):
            raise ValueError('That does not look like a Discord bot token (Developer Portal → your app → Bot → Reset Token).')
        guilds = [g.strip() for g in str(data.get('guild_ids', '')).replace('\n', ',').split(',') if g.strip()]
        if any(not g.isdigit() or not 0 < int(g) < 2 ** 63 for g in guilds) or len(guilds) > 10:
            raise ValueError('Servers: up to 10 Discord server IDs (numbers), one per line.')
        if token and not guilds and not self.config.get('guild_ids'):
            raise ValueError('Add the ID of the Discord server the bot may be used in (right-click the server → Copy Server ID).')
        write(self.data / DISCORD_FILE, dict(token=token, guild_ids=list(dict.fromkeys(int(g) for g in guilds))))
        self.store.event('', 'Discord settings changed', f"{'bot token set' if token else 'no bot token'} • {author} • applies after a restart")
        return self.state()

    # ---- restart ----

    def restart(self, author):
        """Stop the app gracefully; it exits with RESTART_EXIT and systemd starts it again (main.py)."""
        if not self.can_restart():
            raise ValueError('This copy of the app is not run by systemd, so it would not start again. Restart it yourself.')
        self.core.restart_requested = True
        self.store.event('', 'Restarting', f'{author} • to apply printer and Discord settings')
        asyncio.get_running_loop().call_later(0.5, self.kill, os.getpid(), signal.SIGTERM)   # after this reply is sent

    # ---- routes ----

    async def state_route(self, request):
        return web.json_response(self.state())

    async def printer_route(self, request):
        return web.json_response(self.save_printer(await request.json(), 'web administrator'))

    async def remove_route(self, request):
        return web.json_response(self.remove_printer(str((await request.json()).get('name', '')), 'web administrator'))

    async def discord_route(self, request):
        return web.json_response(self.save_discord(await request.json(), 'web administrator'))

    async def restart_route(self, request):
        self.restart('web administrator')
        return web.json_response({'ok': True, 'message': 'Restarting. The dashboard is back in about 15 seconds.'})
