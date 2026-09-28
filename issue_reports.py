"""Send a user's problem report, with redacted diagnostics, to the maintainers.

Destinations are pluggable: add a coroutine to SENDERS (for example a Discord webhook or
email) and it becomes selectable. GitHub issues is the default.
"""
import json
import os
import platform
import re
import sys
import time

import aiohttp
from aiohttp import web

import diagnostics

DEFAULT_REPOSITORY = 'REACT-Inc/Bambu-Labs-LAN-MQTT-Remote-Management-system'
REPOSITORY = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}')
IPV4 = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
MAX_BODY = 60000
MIN_INTERVAL = 120


def scrub(text):
    """Issues may be public: remove secrets and local network addresses."""
    return IPV4.sub('[ip]', diagnostics.redact_text(str(text)))


def fence(text):
    return '```text\n' + text.replace('```', "'''") + '\n```'


def compose(core, title, description, reporter):
    """Return (title, markdown body). Contains no IPs, serials, tokens or access codes."""
    try:
        from Updater.version import VERSION
    except Exception:
        VERSION = 'unknown'
    lines = [
        '## What happened', scrub(description).strip() or '_No description given._', '',
        '## System',
        f'- Version: `{VERSION}`',
        f'- Reported by: {scrub(reporter)}',
        f'- Python {sys.version.split()[0]} on {platform.platform()}',
        f'- Uptime: {round((time.monotonic() - getattr(core, "STARTED", time.monotonic())) / 3600, 1)} h'
        f' · Demo mode: {getattr(core, "EXAMPLE_MODE", None)}',
        '',
        '## Printers',
        '| Printer | Model | Connected | State | Print error | Last report | HMS alerts |',
        '|---|---|---|---|---|---|---|',
    ]
    for p in diagnostics.printer_summary(core):
        age = p.get('seconds_since_report')
        lines.append('| {} | {} | {} | {} | {} | {} | {} |'.format(
            scrub(p['name']).replace('|', '/'), scrub(p.get('model') or '?'), p.get('connected', '?'), p.get('state', '?'),
            p.get('print_error', '?'), f'{age:g} s ago' if age is not None else 'never', len(p.get('hms') or [])))
    errors = diagnostics.snapshot_errors()[-10:]
    lines += ['', f'## Recent errors ({len(diagnostics.snapshot_errors())} since start, newest last)']
    if errors:
        for e in errors:
            when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['time']))
            label = f"{when} · {e['level']}" + (f" · ID `{e['error_id']}`" if e.get('error_id') else '')
            lines += [f'<details><summary>{label}</summary>', '', fence(scrub(e['text'])[-1500:]), '', '</details>']
    else:
        lines.append('_None._')
    log = diagnostics.log_dir(core.DATA_DIR) / diagnostics.LOG_NAME
    try:
        recent = diagnostics.tail(log, 24000).decode('utf-8', 'replace').splitlines()[-80:]
        lines += ['', '<details><summary>Last log lines</summary>', '', fence(scrub('\n'.join(recent))), '', '</details>']
    except OSError:
        lines += ['', '_No log file available._']
    lines += ['', '_Sent from the 3D Printer Management dashboard/Discord. Secrets, IP addresses and serial numbers were removed. '
              'Ask the reporter for the full diagnostic ZIP if needed._']
    body = '\n'.join(lines)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 40] + '\n\n_[report truncated]_'
    return '[Report] ' + scrub(title).strip()[:200], body


async def send_github(config, title, body):
    repository, token = config.get('repository') or DEFAULT_REPOSITORY, config.get('token')
    if not token:
        raise ValueError('Issue reports need a GitHub token with "Issues: read and write" on ' + repository + '. Add it under Settings.')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': '3d-printer-management'}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f'https://api.github.com/repos/{repository}/issues', headers=headers,
                                json={'title': title, 'body': body}) as response:
            if response.status == 201:
                return (await response.json())['html_url']
            hints = {401: 'the token is invalid or expired', 403: 'the token lacks Issues write access or is rate limited',
                     404: 'the repository was not found or the token cannot see it', 410: 'issues are disabled on the repository',
                     422: 'GitHub rejected the report content'}
            raise ValueError(f'GitHub returned HTTP {response.status}: {hints.get(response.status, "unexpected response")}.')


# Add new destinations here, e.g. 'discord_webhook': send_discord_webhook. Each takes (config, title, body) and returns a link or text.
SENDERS = {'github': send_github}
LABELS = {'github': 'GitHub issue'}


class IssueReports:
    def __init__(self, core, store, app=None):
        self.core, self.store = core, store
        self.path = core.DATA_DIR / 'issue-reports.json'
        try:
            self.saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.saved = {}
        self.last_sent = 0
        if app is not None:
            app.add_routes([web.get('/api/report/settings', self.status_route), web.post('/api/report/settings', self.settings_route),
                            web.post('/api/report', self.send_route)])

    @property
    def config(self):
        # Saved dashboard settings override config.json "issue_reports", which overrides the defaults.
        defaults = {'destination': 'github', 'repository': DEFAULT_REPOSITORY, 'token': ''}
        return {**defaults, **(getattr(self.core, 'CONFIG', {}).get('issue_reports') or {}), **self.saved}

    def public(self):
        c = self.config
        return {'destination': c['destination'], 'destinations': LABELS, 'repository': c['repository'],
                'has_token': bool(c.get('token')), 'last_sent': self.last_sent}

    def configure(self, data):
        destination = str(data.get('destination') or 'github')
        if destination not in SENDERS:
            raise ValueError('Unknown report destination.')
        repository = str(data.get('repository') or '').strip()
        if not REPOSITORY.fullmatch(repository):
            raise ValueError('Repository must look like owner/name.')
        token = str(data.get('token', '')).strip()
        if len(token) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError('Invalid token.')
        saved_token = '' if data.get('clear_token') else token or self.saved.get('token', '')
        self.saved = {'destination': destination, 'repository': repository, 'token': saved_token}
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as f:
            os.chmod(temp, 0o600)
            json.dump(self.saved, f)
        os.replace(temp, self.path)
        return self.public()

    async def send(self, title, description, reporter):
        title, description = str(title or '').strip(), str(description or '').strip()
        if not 5 <= len(title) <= 200:
            raise ValueError('Give the report a short title (5–200 characters).')
        if not 10 <= len(description) <= 8000:
            raise ValueError('Describe what happened (10–8000 characters).')
        wait = MIN_INTERVAL - (time.time() - self.last_sent)
        if wait > 0:
            raise ValueError(f'A report was just sent. Wait {int(wait) + 1} seconds before sending another.')
        config = self.config
        issue_title, body = compose(self.core, title, description, reporter)
        self.last_sent = time.time()
        try:
            link = await SENDERS[config['destination']](config, issue_title, body)
        except ValueError:
            self.last_sent = 0
            raise
        except Exception as exc:
            self.last_sent = 0
            error_id = diagnostics.log_error(self.core.log, 'Sending issue report failed', exc)
            raise ValueError(f'Could not send the report (error ID {error_id}). Check the Pi has internet access.') from None
        self.core.log.info('Issue report sent: %s', link)
        self.store.event(None, 'Issue report sent', f'{title} → {link}')
        return link

    async def status_route(self, request):
        return web.json_response(self.public())

    async def settings_route(self, request):
        return web.json_response(self.configure(await request.json()))

    async def send_route(self, request):
        data = await request.json()
        link = await self.send(data.get('title'), data.get('description'), 'dashboard administrator')
        return web.json_response({'url': link})


def install_discord(core, reports):
    import discord
    from discord import app_commands

    @core.bot.tree.command(name='reportissue', description='Send a problem report with diagnostics to the developers (admins/approved users)')
    @app_commands.guild_only()
    @app_commands.describe(title='Short summary, e.g. "H2D AMS 0 shows empty"', description='What happened, what you expected, and any error ID')
    async def reportissue(i: discord.Interaction, title: str, description: str):
        # Admin-only via core.ADMIN_COMMANDS, which also limits spam.
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            link = await reports.send(title, description, f'Discord user {i.user.id}')
        except ValueError as exc:
            await i.followup.send(embed=core.card('Report not sent', str(exc), core.RED), ephemeral=True)
            return
        await i.followup.send(embed=core.card('✅ Report sent', f'Thanks. The developers can follow up here:\n{link}', core.GREEN), ephemeral=True)
