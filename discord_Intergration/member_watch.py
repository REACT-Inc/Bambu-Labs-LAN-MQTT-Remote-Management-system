"""Member updates by DM: when someone in the server changes their Discord status or profile, chosen people get a DM.

What can be watched (Settings & help → Member updates):
- status: online / idle / do not disturb / offline;
- custom_status: the custom status text (and emoji);
- activity: playing, streaming or watching (not Spotify songs: those change every few minutes);
- nickname: the server nickname;
- username: the Discord username and display name;
- avatar: the profile picture (server or global);
- roles: roles added or removed;
- timeout: timed out, or the time-out ended.

Changes are collected for a minute and sent together, one DM per person (a status flickering between online and idle
in that minute isn't sent at all). Bots and the people receiving the DMs are left out unless chosen.

Discord only tells a bot about these with two privileged intents, turned on for the bot in the Developer Portal
(Bot → Privileged Gateway Intents): Server Members (nickname, username, avatar, roles, timeout) and Presence (status,
custom status, activity). The bot asks for them only while this is on; if Discord refuses, the bot connects without
them and the status icon says what to turn on. See docs/discord.md.

The settings are in settings.json, "member_watch":
{"enabled": true, "recipients": [user IDs], "watch": [kinds above], "members": [user IDs, empty: everyone],
 "include_bots": false}
"""
import asyncio
import contextlib
import time

import discord

KINDS = ('status', 'custom_status', 'activity', 'nickname', 'username', 'avatar', 'roles', 'timeout')
DEFAULT_WATCH = ['custom_status', 'activity', 'nickname', 'username', 'avatar', 'roles', 'timeout']   # status: noisy, so opt-in
PRESENCE_KINDS = {'status', 'custom_status', 'activity'}
LABELS = dict(status='Status', custom_status='Custom status', activity='Activity', nickname='Nickname', username='Username',
              avatar='Profile picture', roles='Roles', timeout='Time-out')
STATUS = {'online': '🟢 online', 'idle': '🌙 idle', 'dnd': '⛔ do not disturb', 'offline': '⚫ offline', 'invisible': '⚫ offline'}
BATCH_SECONDS = 60
MAX_RECIPIENTS = 10


def clean(data):
    """The settings from the dashboard, checked (ValueError says what's wrong)."""
    if not isinstance(data, dict):
        raise ValueError('Send the member update settings.')
    def ids(value, what, most):
        items = [str(v).strip().strip('<@!>') for v in (str(value or '').replace('\n', ',').split(',') if not isinstance(value, list) else value) if str(v).strip()]
        if len(items) > most or not all(i.isdigit() and 0 < int(i) < 2 ** 63 for i in items):
            raise ValueError(f'{what}: up to {most} Discord user IDs (numbers), one per line (right-click a person → Copy User ID).')
        return list(dict.fromkeys(int(i) for i in items))
    recipients = ids(data.get('recipients'), 'Send the DMs to', MAX_RECIPIENTS)
    members = ids(data.get('members'), 'Only these members', 500)
    watch = [k for k in KINDS if k in (data.get('watch') or [])]
    enabled = data.get('enabled') is True
    if enabled and not recipients:
        raise ValueError('Add at least one Discord user ID to send the updates to.')
    if enabled and not watch:
        raise ValueError('Choose at least one thing to watch.')
    return dict(enabled=enabled, recipients=recipients, watch=watch, members=members, include_bots=data.get('include_bots') is True)


def settings_of(saved):
    saved = saved if isinstance(saved, dict) else {}
    return dict(enabled=saved.get('enabled') is True, recipients=[int(r) for r in saved.get('recipients') or [] if str(r).isdigit()],
                watch=[k for k in saved.get('watch', DEFAULT_WATCH) if k in KINDS], members=[int(m) for m in saved.get('members') or [] if str(m).isdigit()],
                include_bots=saved.get('include_bots') is True)


def needs_intents(settings):
    """(members, presences): the privileged intents these settings need."""
    if not settings['enabled']:
        return False, False
    watch = set(settings['watch'])
    return bool(watch - PRESENCE_KINDS), bool(watch & PRESENCE_KINDS)


# ---- what changed (pure functions of the before/after objects discord.py hands over) ----

def custom_status(activities):
    for a in activities or ():
        if isinstance(a, discord.CustomActivity) or getattr(a, 'type', None) == discord.ActivityType.custom:
            text = ' '.join(str(x) for x in (getattr(a, 'emoji', None) or '', getattr(a, 'name', None) or getattr(a, 'state', None) or '') if str(x))
            return text.strip() or None
    return None


def activity(activities):
    """Playing, streaming, watching or competing (not Spotify or other listening: a new song every few minutes)."""
    for a in activities or ():
        kind = getattr(a, 'type', None)
        if kind in (discord.ActivityType.playing, discord.ActivityType.streaming, discord.ActivityType.watching, discord.ActivityType.competing):
            verb = {discord.ActivityType.playing: 'Playing', discord.ActivityType.streaming: 'Streaming',
                    discord.ActivityType.watching: 'Watching', discord.ActivityType.competing: 'Competing in'}[kind]
            return f'{verb} {getattr(a, "name", "") or "something"}'
    return None


def presence_changes(before, after, watch):
    out = []
    if 'status' in watch and str(before.status) != str(after.status):
        out.append(('status', STATUS.get(str(before.status), str(before.status)), STATUS.get(str(after.status), str(after.status))))
    if 'custom_status' in watch and custom_status(before.activities) != custom_status(after.activities):
        out.append(('custom_status', custom_status(before.activities), custom_status(after.activities)))
    if 'activity' in watch and activity(before.activities) != activity(after.activities):
        out.append(('activity', activity(before.activities), activity(after.activities)))
    return out


def member_changes(before, after, watch):
    out = []
    if 'nickname' in watch and before.nick != after.nick:
        out.append(('nickname', before.nick, after.nick))
    if 'avatar' in watch and getattr(before, 'guild_avatar', None) != getattr(after, 'guild_avatar', None):
        out.append(('avatar', 'server picture', 'new server picture' if after.guild_avatar else 'server picture removed'))
    if 'roles' in watch and {r.id for r in before.roles} != {r.id for r in after.roles}:
        added = [r.name for r in after.roles if r not in before.roles]
        removed = [r.name for r in before.roles if r not in after.roles]
        text = ', '.join([f'+{n}' for n in added] + [f'−{n}' for n in removed])
        out.append(('roles', None, text))
    if 'timeout' in watch:
        was, now = getattr(before, 'timed_out_until', None), getattr(after, 'timed_out_until', None)
        if was != now:
            out.append(('timeout', None, f'timed out until {discord.utils.format_dt(now)}' if now else 'time-out ended'))
    return out


def user_changes(before, after, watch):
    out = []
    if 'username' in watch:
        if before.name != after.name:
            out.append(('username', before.name, after.name))
        if getattr(before, 'global_name', None) != getattr(after, 'global_name', None):
            out.append(('username', f'display name {before.global_name or "(none)"}', after.global_name or '(no display name)'))
    if 'avatar' in watch and getattr(before, 'avatar', None) != getattr(after, 'avatar', None):
        out.append(('avatar', 'profile picture', 'new profile picture' if after.avatar else 'profile picture removed'))
    return out


class MemberWatch:
    def __init__(self, core, clock=time.monotonic, sleep=asyncio.sleep, batch=BATCH_SECONDS):
        self.core, self.clock, self.sleep, self.batch = core, clock, sleep, batch
        self.pending = {}     # (user id, kind) -> dict(who, before, after); several changes to one thing merge
        self.order = []       # the order they happened in
        self.task = None
        self.problem = ''     # for the status icon: intents refused, or DMs that couldn't be sent
        self.sent = 0

    @property
    def settings(self):
        return settings_of(self.core.settings.get('member_watch'))

    # ---- the bot's intents ----

    def apply_intents(self, bot):
        """Before connecting: ask Discord for the privileged intents these settings need (and only those)."""
        members, presences = needs_intents(self.settings)
        bot.intents.members, bot.intents.presences = members, presences
        return members or presences

    def drop_intents(self, bot):
        """Discord refused them (not turned on in the Developer Portal): connect without them rather than not at all."""
        if not (bot.intents.members or bot.intents.presences):
            return False
        wanted = ' and '.join(n for n, on in (('Server Members', bot.intents.members), ('Presence', bot.intents.presences)) if on)
        self.problem = (f'Member updates are off: turn on the {wanted} intent for the bot in the Discord Developer Portal '
                        '(Bot → Privileged Gateway Intents), then restart.')
        self.core.log.error(self.problem)
        bot.intents.members = bot.intents.presences = False
        return True

    # ---- events ----

    def watching(self, member, guild_id=None):
        s = self.settings
        if not s['enabled'] or member is None:
            return False
        if guild_id is not None and guild_id not in self.core.ALLOWED_GUILD_IDS:
            return False
        if member.bot and not s['include_bots']:
            return False
        if s['members']:
            return member.id in s['members']
        return member.id not in s['recipients']   # (their own changes aren't sent to themselves)

    def add(self, member, changes):
        who = getattr(member, 'display_name', None) or getattr(member, 'name', '') or str(member.id)
        for kind, before, after in changes:
            key = (member.id, kind) if kind != 'roles' else (member.id, kind, len(self.order))   # each role change on its own
            if key in self.pending:
                self.pending[key].update(after=after, who=who)   # the first "before", the last "after"
            else:
                self.pending[key] = dict(id=member.id, who=who, kind=kind, before=before, after=after)
                self.order.append(key)
        if changes and (self.task is None or self.task.done()):
            self.task = asyncio.get_running_loop().create_task(self.flush_later())

    async def on_presence_update(self, before, after):
        if self.watching(after, getattr(after.guild, 'id', None)):
            self.add(after, presence_changes(before, after, set(self.settings['watch'])))

    async def on_member_update(self, before, after):
        if self.watching(after, getattr(after.guild, 'id', None)):
            self.add(after, member_changes(before, after, set(self.settings['watch'])))

    async def on_user_update(self, before, after):
        if self.watching(after):
            self.add(after, user_changes(before, after, set(self.settings['watch'])))

    # ---- sending ----

    def lines(self):
        """The waiting changes as DM lines, oldest first; a change that went back where it started is left out."""
        out = []
        for key in self.order:
            c = self.pending[key]
            if c['before'] == c['after'] and c['kind'] not in ('roles', 'timeout'):
                continue
            change = f"{c['before'] or '(none)'} → {c['after'] or '(none)'}" if c['kind'] not in ('roles', 'timeout') else str(c['after'])
            out.append(f"**{discord.utils.escape_markdown(c['who'])}** (<@{c['id']}>) · {LABELS[c['kind']]}: {discord.utils.escape_markdown(change)}")
        self.pending.clear()
        self.order.clear()
        return out

    async def flush_later(self):
        await self.sleep(self.batch)
        await self.flush()

    async def flush(self):
        lines = self.lines()
        if not lines:
            return 0
        pages, page = [], ''
        for line in lines:
            line = line[:1000]
            if len(page) + len(line) + 1 > 3900:
                pages.append(page)
                page = ''
            page += line + '\n'
        pages.append(page)
        delivered = 0
        for user_id in self.settings['recipients']:
            try:
                user = self.core.bot.get_user(user_id) or await self.core.bot.fetch_user(user_id)
                for n, text in enumerate(pages, 1):
                    title = '👤 Member updates' + (f' ({n}/{len(pages)})' if len(pages) > 1 else '')
                    await user.send(embed=self.core.card(title, text.strip()))
                delivered += 1
            except discord.Forbidden:
                self.problem = f"Member updates couldn't be sent to user {user_id}: they don't accept DMs from this server's members."
                self.core.log.warning(self.problem)
            except (discord.HTTPException, discord.NotFound) as exc:
                self.problem = f"Member updates couldn't be sent to user {user_id}: {exc}"
                self.core.log.warning(self.problem)
        if delivered:
            self.sent += len(lines)
            if self.problem.startswith("Member updates couldn't be sent") and delivered == len(self.settings['recipients']):
                self.problem = ''
        return delivered

    def status(self):
        """The status icon (status_center.py)."""
        if not self.settings['enabled'] or not self.problem:
            return []
        return [dict(key='member-watch', title='Member updates by DM', detail=self.problem, level='warn')]

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


def install(core, watch):
    bot = core.bot
    bot.add_listener(watch.on_presence_update, 'on_presence_update')
    bot.add_listener(watch.on_member_update, 'on_member_update')
    bot.add_listener(watch.on_user_update, 'on_user_update')
