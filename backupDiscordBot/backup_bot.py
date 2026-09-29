"""Backup Discord bot for the /ftcteam team commands, running on a separate Pi.

Runs as its own service (pm-backup-bot) on the backup Pi, with its own Discord bot token. Every few seconds it asks
the main Pi's dashboard for GET /backup-sync/state (backupDiscordBot/sync_api.py):

- main answers and its Discord bot is connected       -> backup stays logged out and keeps a copy of the team data;
- no answer, or main's bot disconnected, for `failover_after` seconds -> backup logs in and serves /ftcteam from its copy;
- main healthy again for `failback_after` seconds    -> backup logs out and sends its changes back (POST /backup-sync/merge).

It only loads the team code: no printer connections, queue, cameras or updates, and its own local database.

config.json on the backup Pi:
    "backup_bot": {"enabled": true, "token": "<second bot's token>", "main_url": "http://100.x.y.z:8080",
                   "sync_key": "<same as backup_sync.key on the main Pi>", "failover_after": 90, "failback_after": 30}
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from backupDiscordBot.sync_api import MIN_KEY_LENGTH, SETTINGS_KEYS, TABLES, dump  # noqa: E402

log = logging.getLogger('backup-bot')
DEFAULTS = {'enabled': False, 'token': '', 'main_url': '', 'sync_key': '', 'failover_after': 90, 'failback_after': 30,
            'check_every': 5, 'timeout': 10}


def settings_for(config):
    raw = config.get('backup_bot') or {}
    merged = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    merged['failover_after'] = max(30, int(merged['failover_after']))
    merged['failback_after'] = max(10, int(merged['failback_after']))
    merged['main_url'] = str(merged['main_url'] or '').rstrip('/')
    return merged


def problems(settings, main_token=''):
    """Reasons the backup can't run, or [] when it's ready."""
    found = []
    if not settings['enabled'] or not settings['token']:
        found.append('backup_bot is not enabled or has no token')
    elif main_token and settings['token'] == main_token:
        found.append('backup_bot.token must be a different Discord bot from the main one')
    if not settings['main_url'].startswith(('http://', 'https://')):
        found.append('backup_bot.main_url must be the main dashboard address, like http://100.x.y.z:8080')
    if len(str(settings['sync_key'])) < MIN_KEY_LENGTH:
        found.append(f'backup_bot.sync_key must be at least {MIN_KEY_LENGTH} characters (the same as backup_sync.key on the main Pi)')
    return found


class MainLink:
    """HTTP client for the main Pi's /backup-sync endpoints."""

    def __init__(self, url, key, timeout=10):
        self.url, self.key, self.timeout = url, key, timeout
        self.session = None

    def headers(self, active):
        return {'X-Backup-Key': self.key, 'X-Backup-Active': '1' if active else '0', 'X-PM': '1'}

    async def _session(self):
        import aiohttp
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self.session

    async def state(self, active):
        """The main service's state, or None when it doesn't answer (stopped, crashed, frozen or unreachable)."""
        try:
            session = await self._session()
            async with session.get(self.url + '/backup-sync/state', headers=self.headers(active)) as response:
                if response.status != 200:
                    log.warning('Main Pi answered %s to /backup-sync/state (check backup_bot.sync_key and backup_sync.key)', response.status)
                    return None
                return await response.json()
        except Exception as exc:  # timeouts, refused connections, bad JSON
            log.debug('Main Pi not reachable: %r', exc)
            return None

    async def merge(self, changes, active):
        """True when merged, False to retry later, 'rejected' when the main Pi can never accept them."""
        try:
            session = await self._session()
            async with session.post(self.url + '/backup-sync/merge', json=changes, headers=self.headers(active)) as response:
                if response.status == 200:
                    return True
                log.warning('Main Pi refused the backup changes (%s): %s', response.status, (await response.text())[:200])
                return 'rejected' if response.status == 400 else False
        except Exception as exc:
            log.warning('Could not send the backup changes to the main Pi yet: %r', exc)
        return False

    async def close(self):
        if self.session:
            await self.session.close()


class LocalStore:
    """The backup Pi's own database for the team tables while it's active (a fresh copy each time)."""

    def __init__(self, path):
        path = Path(path)
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(path) + suffix).unlink(missing_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,time REAL NOT NULL,printer TEXT,title TEXT NOT NULL,detail TEXT NOT NULL)')
        self.db.commit()

    def event(self, printer, title, detail):
        with self.db:
            self.db.execute('INSERT INTO events(time,printer,title,detail) VALUES(?,?,?,?)', (time.time(), printer, title, str(detail)))

    def events(self):
        return [dict(r) for r in self.db.execute('SELECT time,printer,title,detail FROM events ORDER BY id')]


def key_of(table, row):
    return tuple(row[k] for k in TABLES[table][0])


def changes_between(before, after):
    """What changed in the team tables: {'upserts': {table: [rows]}, 'deletes': {table: [keys]}}."""
    upserts, deletes = {}, {}
    for table in TABLES:
        old = {key_of(table, r): r for r in before.get(table, [])}
        new = {key_of(table, r): r for r in after.get(table, [])}
        changed = [row for key, row in new.items() if old.get(key) != row]
        removed = [list(key) for key in old if key not in new]
        if changed:
            upserts[table] = changed
        if removed:
            deletes[table] = removed
    return {'upserts': upserts, 'deletes': deletes}


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


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data))
    os.replace(temp, path)


class Supervisor:
    def __init__(self, core, settings, data_dir, link, clock=time.time, bot_factory=make_bot):
        self.core, self.settings, self.data_dir, self.link, self.clock, self.bot_factory = core, settings, Path(data_dir), link, clock, bot_factory
        self.bad_since, self.good_since = clock(), None   # at start, wait failover_after before taking over
        self.active = False
        self.bot = self.team = self.store = None
        self.tasks = []
        self.baseline = None
        # The last copy of the main Pi's team data, and changes not yet sent back; both survive a restart of this Pi.
        self.snapshot_file, self.unmerged_file = self.data_dir / 'backup-snapshot.json', self.data_dir / 'backup-unmerged.json'
        self.snapshot = read_json(self.snapshot_file)
        self.unmerged = read_json(self.unmerged_file)
        if self.snapshot:
            self.apply_settings(self.snapshot)

    def apply_settings(self, state):
        """Use the main Pi's servers, admins and command permissions, so /ftcteam behaves the same."""
        settings = state.get('settings') or {}
        for key in SETTINGS_KEYS:
            if key in settings:
                self.core.settings[key] = settings[key]
            else:
                self.core.settings.pop(key, None)
        if state.get('guild_ids'):
            self.core.ALLOWED_GUILD_IDS.clear()
            self.core.ALLOWED_GUILD_IDS.update(int(g) for g in state['guild_ids'])
        if 'admin_user_ids' in state:
            self.core.SETTINGS_USER_IDS = {int(u) for u in state['admin_user_ids'] or []}

    def keep(self, state):
        """Store a fresh copy of the main Pi's data while on standby (written only when it changed)."""
        snapshot = {k: state.get(k) for k in ('guild_ids', 'admin_user_ids', 'settings', 'team_config', 'tables')}
        self.apply_settings(snapshot)
        if snapshot != self.snapshot:
            self.snapshot = snapshot
            write_json(self.snapshot_file, snapshot)

    async def flush(self, active=False):
        """Send the changes made while active back to the main Pi; kept (and retried) until it accepts them."""
        if not self.unmerged:
            return
        result = await self.link.merge(self.unmerged, active)
        if result == 'rejected':
            # Retrying can't help, and waiting changes keep the main Pi's reminders on hold: set them aside.
            kept = self.data_dir / f'backup-rejected-{int(time.time())}.json'
            write_json(kept, self.unmerged)
            log.error('The main Pi rejected the backup changes; they were saved in %s', kept)
        elif not result:
            return
        else:
            log.info('Sent the backup changes to the main Pi')
        self.unmerged = None
        self.unmerged_file.unlink(missing_ok=True)

    async def step(self):
        if self.active and self.tasks and self.tasks[0].done():
            # The backup bot couldn't log in or lost Discord (bad token, network): stand down, retry after failover_after.
            await self.deactivate('failed')
            self.bad_since = self.clock()
            return
        state = await self.link.state(self.active or bool(self.unmerged))
        now = self.clock()
        ok = bool(state) and state.get('discord_ready') is True
        if state and not self.active:
            await self.flush()
            if not self.unmerged:
                self.keep(state)   # never overwrite the copy while changes are waiting to go back
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
            await self.flush()

    async def activate(self):
        from aiohttp import web
        from ftcTeamManagement.team import Team
        from ftcTeamManagement.team_discord import install as install_team
        log.warning('Main Discord bot is offline or not responding: starting the backup bot')
        snapshot = self.snapshot or {}
        if not snapshot:
            log.warning('No copy of the main Pi\'s team data yet: starting with empty notes, reminders and attendance')
        if self.unmerged:
            log.warning('Changes from the last take-over were never sent to the main Pi; they are sent after this one')
        if snapshot.get('team_config'):
            write_json(Path(self.core.DATA_DIR) / 'team.json', snapshot['team_config'])   # Team reads it from there
        self.bot = self.bot_factory(self.core)
        backup_core = BackupCore(self.core, self.bot)
        self.store = LocalStore(self.data_dir / 'backup.sqlite3')
        self.team = Team(backup_core, self.store, SimpleNamespace(app=web.Application()))   # creates the team tables
        tables = snapshot.get('tables') or {}
        with self.store.db:
            for table, (_, columns) in TABLES.items():
                for row in tables.get(table, []):
                    cols = [c for c in columns if c in row]
                    self.store.db.execute(f'INSERT OR REPLACE INTO {table}({",".join(cols)}) VALUES({",".join("?" * len(cols))})', [row[c] for c in cols])
            # The main Pi may have stopped mid-send: never send those again (as the main service does on start-up).
            self.store.db.execute("UPDATE reminders SET status='needs_review',error='Main Pi went offline during delivery; check before recreating.' WHERE status='sending'")
            self.store.db.execute("UPDATE assignments SET status='needs_review',error='Main Pi went offline during delivery; check channel.' WHERE status='sending'")
        self.baseline = {t: tables.get(t, []) for t in TABLES}
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
            # Everything the backup changed, queued for the main Pi (added to any changes still waiting from before).
            changes = changes_between(self.baseline or {}, dump(self.store.db))
            changes['events'] = self.store.events()
            self.unmerged = combine(self.unmerged, changes)
            write_json(self.unmerged_file, self.unmerged)
            self.store.db.close()
        self.bot = self.team = self.store = None
        self.tasks = []
        self.active = False


def combine(older, newer):
    """Two sets of unsent changes as one; the newer wins for the same row."""
    if not older:
        return newer
    result = {'upserts': {}, 'deletes': {}, 'events': (older.get('events') or []) + (newer.get('events') or [])}
    for table in TABLES:
        rows = {key_of(table, r): r for r in older.get('upserts', {}).get(table, [])}
        deleted = {tuple(k) for k in older.get('deletes', {}).get(table, [])}
        for key in newer.get('deletes', {}).get(table, []):
            rows.pop(tuple(key), None)
            deleted.add(tuple(key))
        for row in newer.get('upserts', {}).get(table, []):
            rows[key_of(table, row)] = row
            deleted.discard(key_of(table, row))
        if rows:
            result['upserts'][table] = list(rows.values())
        if deleted:
            result['deletes'][table] = [list(k) for k in deleted]
    return result


def release_id():
    try:
        return json.loads((APP_DIR / 'release.json').read_text()).get('id', '')
    except (OSError, ValueError):
        return ''


async def main():
    import core
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')
    settings = settings_for(core.CONFIG)
    found = problems(settings, core.DISCORD_BOT_TOKEN)
    if found:
        log.error('Backup bot is not set up (config.json "backup_bot"): %s. Idling.', '; '.join(found))
        while True:
            await asyncio.sleep(3600)
    link = MainLink(settings['main_url'], settings['sync_key'], settings['timeout'])
    supervisor = Supervisor(core, settings, core.DATA_DIR, link)
    started_release = release_id()
    log.info('Backup bot watching the main Pi at %s', settings['main_url'])
    try:
        while True:
            await supervisor.step()
            if release_id() != started_release and not supervisor.active:
                # A new release was installed: exit so systemd restarts this service with the new code.
                log.info('New release installed: restarting the backup bot service')
                return
            await asyncio.sleep(settings['check_every'])
    finally:
        if supervisor.active:
            await supervisor.deactivate('shutdown')   # its changes are kept in backup-unmerged.json and sent later
        await link.close()


if __name__ == '__main__':
    asyncio.run(main())
