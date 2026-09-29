"""Main-service side of the backup Discord bot, which runs on a separate Pi.

The backup Pi calls two endpoints on the main dashboard, authenticated with a shared key (config.json
"backup_sync": {"key": "..."}, the same value as the backup Pi's "backup_bot.sync_key"):

- GET  /backup-sync/state  health check plus a copy of the team data and Discord permission settings. A stopped,
                           crashed or frozen main service doesn't answer, so this doubles as the heartbeat (#40).
- POST /backup-sync/merge  the changes the backup made while it was serving /ftcteam, sent once when it hands back.

While the backup bot reports it is active (or still has changes to send), the main team scheduler holds reminders
and report assignments so nothing is sent twice.
"""
import hmac
import time

from aiohttp import web

# Team tables the backup may change, with their key columns and columns (ftcTeamManagement/team.py).
TABLES = {
    'notes': (('id',), ('id', 'guild', 'owner', 'title', 'body', 'created')),
    'reminders': (('id',), ('id', 'guild', 'owner', 'body', 'due', 'status', 'error')),
    'assignments': (('id',), ('id', 'occurrence', 'guild', 'channel', 'member', 'created', 'status', 'error')),
    'attendance': (('guild', 'meeting', 'member'), ('guild', 'meeting', 'member', 'name', 'status', 'reason', 'updated')),
}
SETTINGS_KEYS = ('command_permissions', 'member_role_ids', 'commands_channel_id', 'admin_user_ids')
MIN_KEY_LENGTH = 24
HOLD_FOR = 60          # seconds the scheduler stays held after the backup last said it was active
STARTUP_HOLD = 20      # after a restart, give the backup a moment to say whether it's active


def configured_key(config):
    key = str(((config or {}).get('backup_sync') or {}).get('key') or '')
    return key if len(key) >= MIN_KEY_LENGTH else ''


def dump(db):
    return {table: [dict(row) for row in db.execute(f'SELECT {",".join(columns)} FROM {table}')]
            for table, (_, columns) in TABLES.items()}


class BackupSync:
    def __init__(self, core, team, clock=time.monotonic):
        self.core, self.team, self.clock = core, team, clock
        self.started = clock()
        self.backup_active_at = None   # last time the backup said it was active or had unsent changes
        self.reported = False

    def install(self, app):
        app.router.add_get('/backup-sync/state', self.state)
        app.router.add_post('/backup-sync/merge', self.merge)
        self.team.hold = self.holding

    def holding(self):
        """True while the team scheduler must not send reminders or assignments (the backup may be sending them)."""
        if not configured_key(self.core.CONFIG):
            return False
        now = self.clock()
        if not self.reported and now - self.started < STARTUP_HOLD:
            return True
        return self.backup_active_at is not None and now - self.backup_active_at < HOLD_FOR

    def check(self, request):
        key = configured_key(self.core.CONFIG)
        if not key:
            raise web.HTTPNotFound()
        if not hmac.compare_digest(request.headers.get('X-Backup-Key', '').encode(), key.encode()):
            raise web.HTTPForbidden(text='Invalid backup key.')
        self.reported = True
        if request.headers.get('X-Backup-Active') == '1':
            self.backup_active_at = self.clock()
        elif request.method == 'GET':
            self.backup_active_at = None

    async def state(self, request):
        self.check(request)
        settings = {k: self.core.settings[k] for k in SETTINGS_KEYS if k in self.core.settings}
        return web.json_response({
            'time': time.time(),
            'discord_ready': bool(self.core.bot.is_ready()),
            'guild_ids': sorted(int(g) for g in self.core.ALLOWED_GUILD_IDS),
            'admin_user_ids': sorted(int(u) for u in self.core.SETTINGS_USER_IDS),
            'settings': settings,
            'team_config': self.team.config,
            'tables': dump(self.team.db),
        })

    async def merge(self, request):
        self.check(request)
        try:
            data = await request.json()
            counts = apply_changes(self.team.db, data)
            with self.team.store.db:
                for event in (data.get('events') or [])[:500]:
                    self.team.store.db.execute('INSERT INTO events(time,printer,title,detail) VALUES(?,?,?,?)',
                        (float(event.get('time') or time.time()), event.get('printer') or 'Team', str(event.get('title') or 'Backup bot'), str(event.get('detail') or '')))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # 400 tells the backup that retrying can't help (it sets the changes aside).
            return web.json_response({'error': f'Invalid backup changes: {exc}'}, status=400)
        self.backup_active_at = None
        self.core.log.info('Merged changes from the backup Discord bot: %s', counts)
        return web.json_response({'ok': True, 'merged': counts})


def apply_changes(db, data):
    """Apply {'upserts': {table: [rows]}, 'deletes': {table: [keys]}} to the team tables in one transaction."""
    counts = {}
    with db:
        for table, rows in (data.get('upserts') or {}).items():
            if table not in TABLES:
                raise ValueError(f'Unknown table {table!r}.')
            keys, columns = TABLES[table]
            for row in rows:
                cols = [c for c in columns if c in row]
                if not all(k in cols for k in keys):
                    raise ValueError(f'{table} row is missing its key.')
                db.execute(f'INSERT OR REPLACE INTO {table}({",".join(cols)}) VALUES({",".join("?" * len(cols))})', [row[c] for c in cols])
            counts[table] = counts.get(table, 0) + len(rows)
        for table, key_values in (data.get('deletes') or {}).items():
            if table not in TABLES:
                raise ValueError(f'Unknown table {table!r}.')
            keys, _ = TABLES[table]
            for key in key_values:
                if len(key) != len(keys):
                    raise ValueError(f'{table} key has the wrong length.')
                db.execute(f'DELETE FROM {table} WHERE ' + ' AND '.join(f'{k}=?' for k in keys), list(key))
            counts[table] = counts.get(table, 0) + len(key_values)
    return counts
