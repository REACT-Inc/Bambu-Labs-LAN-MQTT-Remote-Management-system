"""Alerts beyond Discord (#9): Home Assistant, ntfy, generic webhooks and Discord webhooks.

Every notification the app sends to Discord (print finished or failed, printer errors and health alerts, AI
failure alerts, progress…) also goes to each target set up under the dashboard's Settings → Other alerts, either all
of them or only the important ones (red: errors, failures, AI alerts). Targets work without the Discord bot.

Target types (settings.json "alert_targets"; tokens are never sent to the browser):
- ntfy: POST the text to https://ntfy.sh/<topic> (or your own server). Title and priority as headers; optional token.
- home_assistant:
    * a webhook URL (http://homeassistant.local:8123/api/webhook/<id>): POST JSON {printer, title, message, level}
      to trigger an automation, or
    * a notify service URL (http://homeassistant.local:8123/api/services/notify/mobile_app_phone) with a long-lived
      access token: POST {title, message}.
- webhook: POST JSON {printer, title, message, level, color, time} to any URL.
- discord_webhook: a channel webhook URL; posts an embed, independent of the bot.

Sending never blocks the app: each alert is posted in the background with a 10 s limit, and a target that fails is
logged and shown in Settings (it is retried with the next alert, not queued).
"""
import asyncio
import re
import secrets
import time
from urllib.parse import urlparse

import aiohttp

TYPES = {'ntfy': 'ntfy', 'home_assistant': 'Home Assistant', 'webhook': 'Webhook (JSON)', 'discord_webhook': 'Discord webhook'}
EVENTS = ('all', 'important')
RED = 0xE74C3C
MAX_TARGETS = 10


def plain(text):
    """Discord markdown to plain text for ntfy / Home Assistant."""
    return re.sub(r'\*\*|__|`', '', str(text))


def important(title, color):
    return color == RED or any(mark in title for mark in ('🛑', '❌', '🤖', 'error', 'failed', 'Failed'))


class AlertTargets:
    def __init__(self, core, log, session=None, clock=time.time):
        self.core, self.log, self.clock = core, log, clock
        self.session = session   # an aiohttp.ClientSession; created on first use
        self.status = {}          # target id -> last result text, shown in Settings
        self.tasks = set()

    def targets(self):
        return [t for t in self.core.settings.get('alert_targets') or [] if isinstance(t, dict)]

    def public(self):
        """For the dashboard: everything except secrets."""
        return [dict({k: v for k, v in t.items() if k != 'token'}, has_token=bool(t.get('token')), status=self.status.get(t.get('id'), ''),
                     type_name=TYPES.get(t.get('type'), t.get('type'))) for t in self.targets()]

    def save(self, items):
        """Validate and store targets from the dashboard. A blank token keeps the saved one."""
        if not isinstance(items, list) or len(items) > MAX_TARGETS:
            raise ValueError(f'Send a list of at most {MAX_TARGETS} targets.')
        old = {t.get('id'): t for t in self.targets()}
        saved = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Each target must be an object.')
            kind = item.get('type')
            if kind not in TYPES:
                raise ValueError('Choose a target type: ' + ', '.join(TYPES.values()) + '.')
            url = str(item.get('url') or '').strip()
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or len(url) > 500:
                raise ValueError('Enter a full http(s) URL for each target.')
            events = item.get('events', 'all')
            if events not in EVENTS:
                raise ValueError('Send "all" or "important" alerts.')
            target_id = item.get('id') if item.get('id') in old else secrets.token_hex(4)
            token = str(item.get('token') or '').strip() or old.get(target_id, {}).get('token', '')
            if item.get('clear_token'):
                token = ''
            name = str(item.get('name') or TYPES[kind]).strip()[:60]
            saved.append(dict(id=target_id, type=kind, name=name, url=url, events=events, enabled=item.get('enabled', True) is not False,
                              **({'token': token[:500]} if token else {})))
        self.core.save_settings({**self.core.settings, 'alert_targets': saved})
        return self.public()

    # ---- sending ---------------------------------------------------------------------------------------------------
    def request(self, target, printer, title, message, color):
        """(url, keyword arguments for session.post) for one alert."""
        level = 'important' if important(title, color) else 'info'
        token = target.get('token')
        auth = {'Authorization': 'Bearer ' + token} if token else {}
        kind, url = target['type'], target['url']
        if kind == 'ntfy':
            # Title and priority as query parameters: unlike headers they can carry UTF-8 (emoji, accents).
            params = dict(title=plain(f'{printer}: {title}'), priority='high' if level == 'important' else 'default', tags='printer')
            return url, dict(data=plain(message).encode(), params=params, headers=auth)
        if kind == 'home_assistant':
            if '/api/webhook/' in url:
                return url, dict(json=dict(printer=printer, title=plain(title), message=plain(message), level=level), headers=auth)
            return url, dict(json=dict(title=plain(f'{printer}: {title}'), message=plain(message)), headers=auth)
        if kind == 'discord_webhook':
            embed = dict(title=f'{title}'[:256], description=f'**{printer}**\n{message}'[:4000], color=int(color or 0))
            return url, dict(json=dict(embeds=[embed], allowed_mentions={'parse': []}))
        return url, dict(json=dict(printer=printer, title=plain(title), message=plain(message), level=level, color=color,
                                   time=int(self.clock())), headers=auth)

    async def post(self, target, printer, title, message, color):
        """Send one alert to one target. Returns a short result text."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        url, kwargs = self.request(target, printer, title, message, color)
        try:
            async with self.session.post(url, timeout=aiohttp.ClientTimeout(total=10), **kwargs) as response:
                if response.status >= 400:
                    result = f'Failed: HTTP {response.status} at {time.strftime("%H:%M", time.localtime(self.clock()))}'
                else:
                    result = f'Sent at {time.strftime("%H:%M", time.localtime(self.clock()))}'
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            result = f'Failed: {type(exc).__name__} at {time.strftime("%H:%M", time.localtime(self.clock()))}'
        self.status[target['id']] = result
        if result.startswith('Failed'):
            self.log.warning('Alert to %s (%s) failed: %s', target.get('name'), TYPES.get(target['type']), result)
        return result

    def dispatch(self, printer, title, message, color):
        """Called for every notification: post it to each matching target in the background."""
        for target in self.targets():
            if not target.get('enabled', True) or (target.get('events') == 'important' and not important(title, color)):
                continue
            task = asyncio.create_task(self.post(target, printer, title, message, color))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def test(self, target_id):
        target = next((t for t in self.targets() if t.get('id') == target_id), None)
        if not target:
            raise ValueError('Save the target first, then send a test.')
        return await self.post(target, 'Test printer', '🧪 Test alert', 'If you can read this, alerts from 3D Printer Management reach you here.', 0x3498DB)

    async def close(self, app=None):
        if self.session and not self.session.closed:
            await self.session.close()
