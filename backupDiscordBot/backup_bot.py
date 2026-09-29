"""Backup Discord bot for the /ftcteam team commands.

Runs as its own service (pm-backup-bot) with its own Discord bot token. It watches the main service's heartbeat
(backupDiscordBot/heartbeat.py):

- main healthy (fresh heartbeat and its Discord bot connected)  -> backup stays logged out;
- main unhealthy for `failover_after` seconds                   -> backup logs in and serves /ftcteam;
- main healthy again for `failback_after` seconds              -> backup removes its commands and logs out.

It only loads the team code: no printer connections, queue, cameras or updates. It opens the shared database
directly (never through queueing.Store, whose start-up recovery would mark the main service's active jobs).

config.json:
    "backup_bot": {"enabled": true, "token": "<second bot's token>", "failover_after": 90, "failback_after": 30}
"""
import asyncio
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from backupDiscordBot import heartbeat  # noqa: E402

log = logging.getLogger('backup-bot')
DEFAULTS = {'enabled': False, 'token': '', 'failover_after': 90, 'failback_after': 30, 'check_every': 5}


def settings_for(config):
    raw = config.get('backup_bot') or {}
    merged = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    merged['failover_after'] = max(30, int(merged['failover_after']))
    merged['failback_after'] = max(10, int(merged['failback_after']))
    return merged


class SharedStore:
    """The main service's database, opened for the team tables only (no queue start-up recovery)."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=10000')

    def event(self, printer, title, detail):
        try:
            with self.db:
                self.db.execute('INSERT INTO events(time,printer,title,detail) VALUES(?,?,?,?)', (time.time(), printer, title, str(detail)))
        except sqlite3.Error:
            log.warning('Could not record event %r', title)


class BackupCore:
    """Everything team_discord and Team need from core, but with the backup bot instead of the main one."""

    def __init__(self, core, bot):
        self._core, self.bot = core, bot

    def __getattr__(self, name):
        return getattr(self._core, name)

    def card(self, title, description='', color=None):
        embed = self._core.card(title, description, self._core.BLUE if color is None else color)
        embed.set_footer(text='3D Printer Management • Backup bot (team commands only)')
        return embed

    def update_pending(self):
        return False


def make_bot(core):
    import discord

    # A plain client with slash commands only: no prefix commands, so no message-content intent is needed.
    class BackupBot(discord.Client):
        def __init__(self):
            super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
            self.tree = core.PrinterTree(self)

        async def setup_hook(self):
            # Guild commands appear immediately and are removed again on hand-back; no global commands are left behind.
            for guild_id in core.ALLOWED_GUILD_IDS:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()

        async def on_ready(self):
            await self.change_presence(activity=discord.Game('Backup: /ftcteam only'))
            log.info('Backup bot connected as %s', self.user)

    return BackupBot()


class Supervisor:
    def __init__(self, core, settings, data_dir, clock=time.time, bot_factory=make_bot):
        self.core, self.settings, self.data_dir, self.clock, self.bot_factory = core, settings, Path(data_dir), clock, bot_factory
        now = clock()
        self.bad_since, self.good_since = now, None   # at start, wait failover_after before taking over
        self.active = False
        self.bot = self.team = self.store = None
        self.tasks = []

    def main_ok(self):
        return heartbeat.main_is_healthy(heartbeat.read(self.data_dir), self.clock())

    async def step(self):
        if self.active and self.tasks and self.tasks[0].done():
            # The backup bot couldn't log in or lost Discord (bad token, network): stand down, retry after failover_after.
            await self.deactivate('failed')
            self.bad_since = self.clock()
            return
        now, ok = self.clock(), self.main_ok()
        if ok:
            self.bad_since = None
            if self.good_since is None:
                self.good_since = now
        else:
            self.good_since = None
            if self.bad_since is None:
                self.bad_since = now
        if not self.active and self.bad_since is not None and now - self.bad_since >= self.settings['failover_after']:
            await self.activate()
        elif self.active and self.good_since is not None and now - self.good_since >= self.settings['failback_after']:
            await self.deactivate()

    def reload_settings_file(self):
        try:
            fresh = json.loads(Path(self.core.SETTINGS_FILE).read_text())
            self.core.settings.clear()
            self.core.settings.update(fresh)
            self.core.SETTINGS_USER_IDS = set(fresh.get('admin_user_ids', self.core.SETTINGS_USER_IDS))
        except (OSError, ValueError):
            pass

    async def activate(self):
        from aiohttp import web
        from ftcTeamManagement.team import Team
        from ftcTeamManagement.team_discord import install as install_team
        log.warning('Main Discord bot is offline or not responding: starting the backup bot')
        self.reload_settings_file()   # permission changes made in the dashboard since this service started
        self.bot = self.bot_factory(self.core)
        backup_core = BackupCore(self.core, self.bot)
        self.store = SharedStore(self.data_dir / 'management.sqlite3')
        self.team = Team(backup_core, self.store, SimpleNamespace(app=web.Application()))
        install_team(backup_core, self.team)
        self.store.event(None, 'Backup bot started', 'The main Discord bot was offline; /ftcteam is served by the backup bot.')
        bot_task = asyncio.create_task(self.bot.start(self.settings['token']))
        bot_task.add_done_callback(lambda t: t.cancelled() or not t.exception() or log.error('Backup bot stopped: %r (check backup_bot.token)', t.exception()))
        self.tasks = [bot_task, asyncio.create_task(self.team.scheduler())]
        self.active = True

    async def deactivate(self, reason='main'):
        import discord
        if reason == 'main':
            log.warning('Main Discord bot is back: handing /ftcteam back and stopping the backup bot')
        elif reason == 'failed':
            log.warning('Backup bot could not stay connected to Discord; retrying after failover_after')
        else:
            log.info('Backup bot service stopping')
        # Stop the reminder/assignment scheduler first so nothing is sent twice.
        for task in self.tasks[1:]:
            task.cancel()
        await asyncio.gather(*self.tasks[1:], return_exceptions=True)
        try:
            if self.bot and self.bot.is_ready():
                for guild_id in self.core.ALLOWED_GUILD_IDS:
                    guild = discord.Object(id=int(guild_id))
                    self.bot.tree.clear_commands(guild=guild)
                    await self.bot.tree.sync(guild=guild)
        except Exception:
            log.exception('Could not remove the backup commands; they stay listed until the backup next starts')
        if self.bot:
            await self.bot.close()
        # close() normally ends bot.start(); never wait on it forever.
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks[:1], timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks[:1], return_exceptions=True)
        if self.team:
            try:
                await self.team.mesh.close(None)
            except Exception:
                pass
        if self.store:
            self.store.event(None, 'Backup bot stopped', {'main': 'The main Discord bot is back online.', 'failed': 'The backup bot could not connect to Discord; it will retry.'}.get(reason, 'The backup bot service stopped.'))
            self.store.db.close()
        self.bot = self.team = self.store = None
        self.tasks = []
        self.active = False


def release_id():
    try:
        return json.loads((APP_DIR / 'release.json').read_text()).get('id', '')
    except (OSError, ValueError):
        return ''


async def main():
    import core
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    settings = settings_for(core.CONFIG)
    if not settings['enabled'] or not settings['token']:
        log.info('Backup bot is not enabled (config.json "backup_bot"): idling')
        while True:
            await asyncio.sleep(3600)
    if settings['token'] == core.DISCORD_BOT_TOKEN:
        log.error('backup_bot.token must be a different Discord bot from the main one; idling')
        while True:
            await asyncio.sleep(3600)
    supervisor = Supervisor(core, settings, core.DATA_DIR)
    started_release = release_id()
    try:
        while True:
            await supervisor.step()
            if release_id() != started_release:
                # A new release was installed: exit so systemd restarts this service with the new code.
                log.info('New release installed: restarting the backup bot service')
                return
            await asyncio.sleep(settings['check_every'])
    finally:
        if supervisor.active:
            await supervisor.deactivate('shutdown')


if __name__ == '__main__':
    asyncio.run(main())
