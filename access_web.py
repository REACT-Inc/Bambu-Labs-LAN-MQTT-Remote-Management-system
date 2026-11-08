"""The dashboard's side of signing in with Google through Cloudflare Access (cloudflare_access.py checks the tokens).

- Each API request: who it is (through Cloudflare), or what the Pi's own addresses may do ("lan"); blocked accounts.
- Who may do what: admins manage, operators print, everyone else Access lets in looks (can_manage / can_print).
- Settings & help → Google sign-in: settings (saved in DATA_DIR/cloudflare_access.json, applied at once), Test, each
  person's level, Recent sign-ins and requests to print (the access_seen table).
- The status icon: Cloudflare's keys unreadable, or the cloudflared service stopped (checked every 5 minutes).

See docs/cloudflare-access.md.
"""
import asyncio
import json
import shutil
import time

from aiohttp import web

import cloudflare_access
from cloudflare_access import Access, actor, clean

ASK_PATH = '/api/ask-access'   # (not under /api/access, which is management: view-only people may ask)


class SignIn:
    def __init__(self, core, store, dashboard):
        self.core, self.store, self.dashboard = core, store, dashboard
        self.file = core.DATA_DIR / 'cloudflare_access.json'
        self.access = Access(self.settings())
        self.tunnel = None   # is the cloudflared service running (None: no systemd to ask)
        self.seen_at = {}    # email -> when its Recent sign-ins row was last written (once a minute at most)
        self.task = None
        db = store.db
        db.executescript('CREATE TABLE IF NOT EXISTS access_seen (email TEXT PRIMARY KEY, first REAL NOT NULL, last REAL NOT NULL, '
                         'admin INTEGER NOT NULL, print INTEGER NOT NULL, address TEXT NOT NULL, visits INTEGER NOT NULL DEFAULT 1)')
        columns = {r[1] for r in db.execute('PRAGMA table_info(access_seen)')}
        for column in ('wants TEXT', 'wants_at REAL', 'note TEXT'):   # Ask to print: the level someone asked for
            if column.split()[0] not in columns:
                db.execute(f'ALTER TABLE access_seen ADD COLUMN {column}')
        db.commit()

    def routes(self):
        return [web.get('/api/access', self.get), web.post('/api/access', self.save), web.post('/api/access/test', self.test),
                web.post('/api/access/person', self.person), web.post(ASK_PATH, self.ask)]

    def settings(self):
        """Saved from Settings (or by cloudflare-tunnel.sh) in DATA_DIR; else "cloudflare_access" in config.json."""
        try:
            return json.loads(self.file.read_text())
        except FileNotFoundError:
            return (getattr(self.core, 'CONFIG', None) or {}).get('cloudflare_access')
        except ValueError:
            self.core.log.error('%s is not valid JSON: Google sign-in is off until it is fixed.', self.file)
            return None

    def apply(self, settings, keep_keys=False):
        """Save and use new settings at once (no restart). The same team keeps the keys already read."""
        old = self.access
        cloudflare_access.atomic_write(self.file, settings)
        self.access = Access(settings)
        if keep_keys and old.team == self.access.team:
            self.access.fetch, self.access.keys, self.access.keys_at = old.fetch, old.keys, old.keys_at
        elif self.access.enabled:
            asyncio.get_running_loop().create_task(self.access.warm())

    # ---- each request ----

    def public_origin(self, request):
        """Through Cloudflare Tunnel the Pi sees plain http, but the browser's page is https://<hostname>."""
        return f'https://{self.access.hostname}' if self.access.enabled and self.access.through_cloudflare(request) else None

    def allows_host(self, name):
        return self.access.enabled and name == self.access.hostname   # (its requests must carry Access's token)

    async def check(self, request):
        """Before an API request: who it is. A response when it may not go on (no or a bad sign-in, blocked, "lan": "off")."""
        if not self.access.enabled:
            return None
        if not self.access.through_cloudflare(request):
            if self.access.lan == 'off':
                raise web.HTTPForbidden(text=f'This dashboard is open at https://{self.access.hostname} only.')
            return None
        # Cloudflare Access let this through: its signed token says who (no token, no access)
        identity = request['identity'] = await self.access.identity(request)
        if not identity:
            return web.json_response({'error': 'Sign in with Google again: reload the page.', 'signin': True}, status=403)
        self.seen(request)
        if identity.get('blocked'):   # an admin blocked this account (Recent sign-ins): at once
            return web.json_response({'error': f"This Google account ({identity['email']}) has been blocked from this dashboard by an admin.",
                                      'blocked': True}, status=403)
        return None

    def can_manage(self, request):
        """With Cloudflare Access set up: its admins (or the Pi's own addresses when "lan" is "full"). Else everyone."""
        if not self.access.enabled:
            return True
        identity = request.get('identity')
        return identity['admin'] if identity else self.access.lan == 'full'

    def can_print(self, request):
        """Queue, slice and control printers (any change at all): operators and admins, and the Pi's own addresses
        unless "lan" is "view". Without Cloudflare Access, everyone."""
        if not self.access.enabled:
            return True
        identity = request.get('identity')
        return identity['print'] if identity else self.access.lan in ('printing', 'full')

    def may_ask(self, request):
        return request.path == ASK_PATH

    def locked_message(self, request):
        identity = request.get('identity')
        if identity:
            return (f"Your Google account ({identity['email']}) isn't a dashboard admin: ask an admin (Settings & help → Google sign-in).")
        return f'Management is open at https://{self.access.hostname} (sign in with Google there).'

    def view_only_message(self, request):
        identity = request.get('identity')
        if identity:
            return f"{identity['email']} can look but not print or change anything: press Ask to print, or ask an admin."
        return f'This address can only look: sign in with Google at https://{self.access.hostname} to print.'

    def state(self, request, manage):
        """For /api/state."""
        enabled = self.access.enabled
        return dict(identity=request.get('identity'), access=self.access.public(),
                    access_requests=self.requests_waiting() if manage and enabled else 0,
                    my_request=self.my_request(request) if enabled else None)

    # ---- Recent sign-ins (the access_seen table) ----

    def seen(self, request):
        """Each Google account's first and last visit, and from where (written once a minute at most)."""
        identity, now = request['identity'], time.time()
        email = identity['email'].strip().lower()[:200]
        if not email or now - self.seen_at.get(email, 0) < 60:
            return
        if len(self.seen_at) > 5000:
            self.seen_at.clear()
        self.seen_at[email] = now
        address = request.headers.get('Cf-Connecting-Ip', '')[:64]
        with self.store.db:
            self.store.db.execute('INSERT INTO access_seen(email,first,last,admin,print,address,visits) VALUES(?,?,?,?,?,?,1) '
                                  'ON CONFLICT(email) DO UPDATE SET last=excluded.last,admin=excluded.admin,print=excluded.print,'
                                  'address=excluded.address,visits=visits+1', (email, now, now, int(identity['admin']), int(identity['print']), address))

    def recent(self, limit=50):
        """Recent sign-ins, those asking to print first: online in the last 5 minutes, new in the last day."""
        rows = self.store.db.execute('SELECT * FROM access_seen ORDER BY wants IS NULL, last DESC LIMIT ?', (limit,)).fetchall()
        now = time.time()
        return [dict(email=r['email'], first=r['first'], last=r['last'], address=r['address'], visits=r['visits'],
                     level=self.access.level(r['email']), own=r['email'] in self.access.people,
                     online=now - r['last'] < 300, new=now - r['first'] < 86400,
                     wants=r['wants'], wants_at=r['wants_at'], note=r['note'] or '') for r in rows]

    def requests_waiting(self):
        return self.store.db.execute('SELECT COUNT(*) FROM access_seen WHERE wants IS NOT NULL').fetchone()[0]

    def my_request(self, request):
        identity = request.get('identity')
        if not identity:
            return None
        row = self.store.db.execute('SELECT wants FROM access_seen WHERE email=?', (identity['email'].strip().lower()[:200],)).fetchone()
        return row['wants'] if row else None

    def clear_request(self, email):
        row = self.store.db.execute('SELECT wants FROM access_seen WHERE email=?', (email,)).fetchone()
        with self.store.db:
            self.store.db.execute('UPDATE access_seen SET wants=NULL,wants_at=NULL,note=NULL WHERE email=?', (email,))
        return row['wants'] if row else None

    # ---- checked every 5 minutes, shown in the status icon ----

    async def start(self, app):
        self.task = asyncio.create_task(self.watch())

    async def stop(self, app):
        if self.task:
            self.task.cancel()

    async def watch(self):
        while True:
            try:
                if self.access.enabled:
                    await self.access.warm()
                    self.tunnel = await self.tunnel_running()
            except Exception as exc:
                self.core.log.warning('Checking Google sign-in failed: %s', exc)
            await asyncio.sleep(300)

    async def tunnel_running(self):
        if not shutil.which('systemctl'):
            return None
        proc = await asyncio.create_subprocess_exec('systemctl', 'is-active', '--quiet', 'cloudflared',
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await proc.wait() == 0

    def status(self):
        if not self.access.enabled:
            return []
        items = []
        if self.access.error:
            items.append(dict(key='access-keys', title="Google sign-in can't check sign-ins", level='error',
                              detail=self.access.error + ' Nobody can use https://' + self.access.hostname + ' until it can.'))
        if self.tunnel is False:
            items.append(dict(key='access-tunnel', title='Cloudflare Tunnel is not running', level='warn',
                              detail=f'https://{self.access.hostname} is down. On the Pi: sudo systemctl status cloudflared'))
        return items

    # ---- Settings & help → Google sign-in ----

    def panel(self, request):
        return dict(settings=self.access.config if self.access.enabled else None, status=self.access.status(), tunnel=self.tunnel,
                    through_cloudflare=self.access.enabled and self.access.through_cloudflare(request),
                    identity=request.get('identity'), file=str(self.file), seen=self.recent())

    async def get(self, request):
        return web.json_response(self.panel(request))

    async def test(self, request):
        """Before saving: can the Pi read the team's keys, and does https://<hostname> ask for a Google sign-in?"""
        trial = Access(clean(await request.json()))
        try:
            await trial.key(None)
        except Exception:
            pass   # (trial.error says why; an unknown key id is expected here)
        keys = dict(ok=bool(trial.keys), text=f'{len(trial.keys)} signing keys read from https://{trial.team}.' if trial.keys else
                    (trial.error or f'https://{trial.team} has no signing keys: check the team domain.'))
        state, text = await cloudflare_access.probe(trial.team, trial.hostname)
        return web.json_response(dict(keys=keys, address=dict(ok=state == 'protected', state=state, text=text)))

    async def save(self, request):
        data = await request.json()
        who = actor(request)
        identity = request.get('identity')
        if data.get('remove') is True:
            if identity and data.get('confirmed') is not True:
                return web.json_response({'error': f'https://{self.access.hostname} stops working (and anyone who can reach the Pi can manage): '
                                          "use the Pi's own address afterwards.", 'confirm': True}, status=409)
            self.apply({})   # (an empty file, so "cloudflare_access" in config.json isn't used either)
            self.store.event('Settings', 'Google sign-in turned off', f'Cloudflare Access • {who}')
            return web.json_response(self.panel(request))
        if 'people' not in data:   # (the panel's form doesn't edit them: Recent sign-ins does)
            data = {**data, 'people': self.access.people}
        settings = clean(data)
        new = Access(settings)
        # Don't lock out the person saving: say what they would lose, and save only once they've confirmed it.
        if identity and new.hostname == self.access.hostname and not new.is_admin(identity['email']):
            lose = f"{identity['email']} would no longer be an admin, so you couldn't change these settings again from here."
        elif not identity and new.lan != 'full':
            lose = (f"This is one of the Pi's own addresses: it would {'only print' if new.lan == 'printing' else 'only look' if new.lan == 'view' else 'stop working'}, "
                    f'and management would be at https://{new.hostname} only. Use Test first, and make sure you can sign in there as an admin.')
        else:
            lose = ''
        if lose and data.get('confirmed') is not True:
            return web.json_response({'error': lose, 'confirm': True}, status=409)
        self.apply(settings)
        admins = ', '.join(settings['admins']) or 'everyone Access lets in'
        self.store.event('Settings', 'Google sign-in saved',
                         f"https://{settings['hostname']} • admins: {admins} • the Pi's own addresses: {settings['lan']} • {who}"[:1800])
        return web.json_response(self.panel(request))

    async def person(self, request):
        """One person's level from Recent sign-ins: admin / print / view / block, or null for "from the lists";
        or {"dismiss": true} to turn down their request to print."""
        if not self.access.enabled:
            raise ValueError('Turn the Google sign-in on first.')
        data = await request.json()
        email, level = str(data.get('email') or '').strip().lower(), data.get('level')
        if data.get('dismiss') is True:
            wanted = self.clear_request(email)
            self.store.event('Settings', f'Google sign-in: request from {email} dismissed', f'asked for: {wanted or "nothing"} • {actor(request)}')
            return web.json_response(self.panel(request))
        identity = request.get('identity')
        if identity and email == identity['email'].lower():
            raise ValueError("You can't change your own level: ask another admin.")
        people = dict(self.access.people)
        if level is None:
            people.pop(email, None)
        else:
            people[email] = level
        self.apply(clean({**self.access.config, 'people': people}), keep_keys=True)
        if self.access.level(email) in ('admin', 'print', 'block'):
            self.clear_request(email)   # answered
        text = {'admin': 'is an admin', 'print': 'can print', 'view': 'can only look', 'block': 'is blocked', None: 'follows the lists again'}[level]
        self.store.event('Settings', f'Google sign-in: {email} {text}', f'{email} is now: {self.access.level(email)} • {actor(request)}')
        return web.json_response(self.panel(request))

    async def ask(self, request):
        """Ask to print: someone who can only look asks the admins for more (shown first in their Recent sign-ins)."""
        identity = request.get('identity')
        if not identity:
            raise ValueError(f'Sign in with Google at https://{self.access.hostname} to ask for access.' if self.access.enabled else 'Nothing to ask for.')
        data = await request.json()
        wants = data.get('level', 'print')
        if wants not in ('print', 'admin'):
            raise ValueError('Ask to print, or to be an admin.')
        level = self.access.level(identity['email'])
        if level == wants or (level == 'admin' and wants == 'print'):
            raise ValueError('You already can.')
        email = identity['email'].strip().lower()[:200]
        self.seen_at.pop(email, None)
        self.seen(request)   # (make sure there's a row)
        with self.store.db:
            self.store.db.execute('UPDATE access_seen SET wants=?,wants_at=?,note=? WHERE email=?',
                                  (wants, time.time(), str(data.get('note') or '').strip()[:300], email))
        return web.json_response({'ok': True, 'wants': wants})
