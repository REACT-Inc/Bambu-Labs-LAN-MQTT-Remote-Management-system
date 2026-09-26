"""Persistent team tools: assignments, notes, reminders, channel archive, devices."""
import asyncio
import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import discord
from aiohttp import web
from dashboard import atomic_json


class Team:
    def __init__(self,core,store,dashboard):
        self.core,self.store,self.dashboard=core,store,dashboard
        self.db=store.db
        self.config_file=core.DATA_DIR/'team.json'
        self.config=json.loads(self.config_file.read_text()) if self.config_file.exists() else dict(
            ftc_url='',website_url='',management_url='',practice_enabled=False,
            timezone='America/New_York',practice_days=[0,2,4],practice_time='16:00',
            practice_channel_id='',practice_role_id='',roster=[])
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,guild TEXT,owner TEXT,title TEXT,body TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS reminders(id TEXT PRIMARY KEY,guild TEXT,owner TEXT,body TEXT,due REAL,status TEXT DEFAULT 'pending',error TEXT DEFAULT '');
          CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,occurrence TEXT UNIQUE,guild TEXT,channel TEXT,member TEXT,created REAL,status TEXT DEFAULT 'pending',error TEXT DEFAULT '');
          CREATE TABLE IF NOT EXISTS archives(channel TEXT PRIMARY KEY,guild TEXT,original TEXT,status TEXT DEFAULT 'pending');
          CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT,token_hash TEXT,last_seen REAL DEFAULT 0,report TEXT DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS device_tasks(id TEXT PRIMARY KEY,device TEXT,action TEXT,status TEXT DEFAULT 'pending',created REAL,result TEXT DEFAULT '');
        ''')
        # A lost acknowledgement must not cause an automatic second reboot/command.
        self.db.execute("UPDATE reminders SET status='needs_review',error='Service restarted during delivery; check before recreating.' WHERE status='sending'")
        self.db.execute("UPDATE assignments SET status='needs_review',error='Service restarted during delivery; check channel.' WHERE status='sending'")
        self.db.execute("UPDATE device_tasks SET status='unknown',result='Server restarted after delivery; command will not be resent.' WHERE status='delivered'")
        self.db.commit()
        from laptopManagement_Intergration.meshCentral.meshcentral_client import MeshCentral
        self.mesh = MeshCentral(core,store)
        dashboard.app.on_shutdown.append(self.mesh.close)
        self.inflight=set()
        dashboard.app.add_routes([
            web.get('/api/team',self.web_state),web.post('/api/team/{action}',self.web_action),
        ])

    def ids_allowed(self,guild):return int(guild) in self.core.ALLOWED_GUILD_IDS
    def setting_channel(self):
        cid=self.config.get('practice_channel_id')
        if not cid:raise ValueError('Configure a practice channel first.')
        return int(cid)

    def save_config(self,data):
        config=dict(self.config)
        for key in ('ftc_url','website_url','management_url'):
            value=str(data.get(key,''))
            if value and (urlparse(value).scheme not in ('http','https') or not urlparse(value).netloc):raise ValueError('Links must be complete HTTP or HTTPS URLs.')
            config[key]=value[:1000]
        ZoneInfo(str(data.get('timezone','America/New_York')))
        config['timezone']=str(data.get('timezone','America/New_York'))
        clock=str(data.get('practice_time','16:00'));datetime.strptime(clock,'%H:%M')
        config['practice_time']=clock
        days=data.get('practice_days',[])
        if not isinstance(days,list) or any(type(x)is not int or not 0<=x<=6 for x in days):raise ValueError('Practice days use 0=Monday through 6=Sunday.')
        config['practice_days']=sorted(set(days))
        roster=data.get('roster',[])
        if not isinstance(roster,list) or len(roster)>200:raise ValueError('Roster must be a list of up to 200 Discord user IDs.')
        config['roster']=list(dict.fromkeys(str(int(v)) for v in roster))
        if any(int(v)<=0 for v in config['roster']):raise ValueError('Invalid roster ID.')
        for key in ('practice_channel_id','practice_role_id'):
            v=data.get(key,'');config[key]=str(int(v)) if v else ''
        config['practice_enabled']=data.get('practice_enabled') is True
        if config['practice_enabled'] and (not config['roster'] or not config['practice_days'] or not config['practice_channel_id']):
            raise ValueError('Automatic assignments need roster IDs, a channel, and practice days.')
        atomic_json(self.config_file,config);self.config=config

    def notes(self,guild=None):
        rows=self.db.execute('SELECT * FROM notes'+(' WHERE guild=?' if guild else '')+' ORDER BY created DESC LIMIT 200',(str(guild),) if guild else ())
        return [dict(r) for r in rows]
    def remember(self,guild,owner,title,body):
        if not 1<=len(title)<=100 or not 1<=len(body)<=3000:raise ValueError('Title: 1–100 characters; note: 1–3000.')
        note_id=secrets.token_hex(5)
        with self.db:self.db.execute('INSERT INTO notes VALUES(?,?,?,?,?,?)',(note_id,str(guild),str(owner),title,body,time.time()))
        return note_id
    def reminder(self,guild,owner,body,minutes):
        minutes=int(minutes)
        if not 1<=minutes<=525600 or not 1<=len(body)<=1500:raise ValueError('Use 1–525600 minutes and a message up to 1500 characters.')
        reminder_id=secrets.token_hex(5)
        with self.db:self.db.execute('INSERT INTO reminders(id,guild,owner,body,due) VALUES(?,?,?,?,?)',(reminder_id,str(guild),str(owner),body,time.time()+minutes*60))
        return reminder_id

    async def channel(self,cid):
        if not self.core.bot.is_ready():raise ValueError('Discord is not connected.')
        ch=self.core.bot.get_channel(int(cid)) or await self.core.bot.fetch_channel(int(cid))
        if not isinstance(ch,discord.TextChannel) or ch.guild.id not in self.core.ALLOWED_GUILD_IDS:
            raise ValueError('Choose a text channel in an allowed server.')
        return ch

    async def assign(self,occurrence,channel_id=None,member_id=None,announce=True,guild_id=None):
        ch=await self.channel(channel_id or self.setting_channel())
        if guild_id is not None and ch.guild.id!=guild_id:raise ValueError('The practice channel must belong to this server.')
        roster=self.config.get('roster',[])
        if member_id is None and not roster:raise ValueError('Configure eligible report-writer user IDs in Team settings or select a person.')
        old=self.db.execute('SELECT * FROM assignments WHERE occurrence=?',(occurrence,)).fetchone()
        if old:return dict(old)
        # Least-used pool, then random choice: everyone gets a turn before repeats.
        if member_id is None:
            counts={uid:self.db.execute("SELECT COUNT(*) FROM assignments WHERE guild=? AND member=? AND status IN ('sent','sending','pending','needs_review','assigned_private')",(str(ch.guild.id),uid)).fetchone()[0] for uid in roster}
            member=secrets.choice([uid for uid,count in counts.items() if count==min(counts.values())])
        else:member=str(int(member_id))
        # Validate the chosen ID belongs to the selected server before storing/sending.
        selected=await ch.guild.fetch_member(int(member))
        if getattr(selected,'bot',False):raise ValueError('Choose a person, not a bot.')
        assignment_id=secrets.token_hex(6)
        with self.db:self.db.execute('INSERT INTO assignments(id,occurrence,guild,channel,member,created) VALUES(?,?,?,?,?,?)',
            (assignment_id,occurrence,str(ch.guild.id),str(ch.id),member,time.time()))
        if announce:
            await self.deliver_assignment(assignment_id)
        else:
            with self.db:self.db.execute("UPDATE assignments SET status='assigned_private' WHERE id=?",(assignment_id,))
            self.store.event('Team','Report assigned privately',f'Discord user {member} • {assignment_id}')
        return dict(self.db.execute('SELECT * FROM assignments WHERE id=?',(assignment_id,)).fetchone())

    async def deliver_assignment(self,assignment_id):
        row=self.db.execute('SELECT * FROM assignments WHERE id=?',(assignment_id,)).fetchone()
        if not row or row['status']!='pending':return
        with self.db:self.db.execute("UPDATE assignments SET status='sending' WHERE id=?",(assignment_id,))
        try:
            ch=await self.channel(row['channel']);role=self.config.get('practice_role_id')
            roles=[discord.Object(id=int(role))] if role else []
            content=(f'<@&{role}> ' if role else '')+f"<@{row['member']}> is today's meeting-report writer."
            await ch.send(content=content,embed=self.core.card('📝 Practice report assignment','Please record what the team worked on, progress made, problems, and next steps.',self.core.BLUE),
                allowed_mentions=discord.AllowedMentions(everyone=False,users=[discord.Object(id=int(row['member']))],roles=roles,replied_user=False))
            with self.db:self.db.execute("UPDATE assignments SET status='sent' WHERE id=?",(assignment_id,))
            self.store.event('Team','Report assigned',f"Discord user {row['member']} • {assignment_id}")
        except Exception as e:
            with self.db:self.db.execute("UPDATE assignments SET status='needs_review',error=? WHERE id=?",(type(e).__name__+': '+str(e)[:200],assignment_id))

    async def archive(self,channel_id,restore=False):
        try:
            return await self._archive(channel_id,restore)
        except discord.Forbidden as exc:
            raise ValueError('Discord denied archive access. Give the bot View Channel, Manage Channels, and Manage Roles in the source and destination categories/channels. Check channel permission overrides. Then retry; saved restore settings are retained.') from exc

    async def _archive(self,channel_id,restore=False):
        ch=await self.channel(channel_id)
        if not ch.guild.me:
            raise ValueError('Bot membership is unavailable. Wait for Discord to reconnect.')
        perms=ch.permissions_for(ch.guild.me)
        missing=[label for key,label in [('view_channel','View Channel'),('manage_channels','Manage Channels'),('manage_roles','Manage Roles')] if not getattr(perms,key,False)]
        if missing:raise ValueError('Bot is missing: '+', '.join(missing)+'. Grant these to the bot in this channel/category and retry.')
        existing=self.db.execute('SELECT * FROM archives WHERE channel=?',(str(ch.id),)).fetchone()
        if restore:
            if not existing:raise ValueError('This channel has no saved archive settings.')
            original=json.loads(existing['original']);overwrites={}
            for obj in original['overwrites']:
                target=ch.guild.get_role(int(obj['id'])) if obj['type']=='role' else ch.guild.get_member(int(obj['id']))
                if target is None and obj['type']=='member':
                    try:target=await ch.guild.fetch_member(int(obj['id']))
                    except discord.NotFound:continue
                if target:overwrites[target]=discord.PermissionOverwrite.from_pair(discord.Permissions(obj['allow']),discord.Permissions(obj['deny']))
            category=ch.guild.get_channel(original['category']) if original['category'] else None
            await ch.edit(category=category,overwrites=overwrites,sync_permissions=False,reason='3D Printer Management: restore archived channel')
            with self.db:self.db.execute('DELETE FROM archives WHERE channel=?',(str(ch.id),))
        else:
            if existing and existing['status']=='archived':raise ValueError('Channel is already archived. Use /unarchive to restore it.')
            protected=[self.core.settings.get('notification_channel_id'),self.core.settings.get('commands_channel_id'),self.config.get('practice_channel_id')]
            if str(ch.id) in [str(x) for x in protected if x]:raise ValueError('Choose a different channel; this one is configured for bot/practice messages.')
            original={'category':ch.category_id,'overwrites':[]}
            overwrites=ch.overwrites
            for target,overwrite in overwrites.items():
                allow,deny=overwrite.pair();original['overwrites'].append(dict(id=str(target.id),type='role' if isinstance(target,discord.Role) else 'member',allow=allow.value,deny=deny.value))
            # Snapshot first so a crash never loses the previous permission map.
            if not existing:
                with self.db:self.db.execute('INSERT INTO archives(channel,guild,original) VALUES(?,?,?)',(str(ch.id),str(ch.guild.id),json.dumps(original)))
            for target in set(overwrites)|{ch.guild.default_role}:
                ow=overwrites.get(target,discord.PermissionOverwrite())
                ow.send_messages=False;ow.send_messages_in_threads=False;ow.create_public_threads=False;ow.create_private_threads=False
                overwrites[target]=ow
            if ch.guild.me:
                ow=overwrites.get(ch.guild.me,discord.PermissionOverwrite());ow.view_channel=True;ow.send_messages=True;overwrites[ch.guild.me]=ow
            category=discord.utils.get(ch.guild.categories,name='Archive') or await ch.guild.create_category('Archive',reason='3D Printer Management archive')
            await ch.edit(category=category,overwrites=overwrites,sync_permissions=False,reason='3D Printer Management: archive channel')
            with self.db:self.db.execute("UPDATE archives SET status='archived' WHERE channel=?",(str(ch.id),))
        self.store.event('Team','Channel restored' if restore else 'Channel archived',str(ch.id))

    def server_status(self):
        total,used,free=shutil.disk_usage(self.core.DATA_DIR)
        temperature=None
        try:temperature=int(Path('/sys/class/thermal/thermal_zone0/temp').read_text())/1000
        except (OSError,ValueError):pass
        uptime=None
        try:uptime=float(Path('/proc/uptime').read_text().split()[0])
        except (OSError,ValueError):pass
        return dict(host=platform.node(),platform=platform.platform(),uptime=uptime,
            disk_total=total,disk_free=free,temperature=temperature,
            reboot_enabled=self.core.CONFIG.get('allow_host_reboot',False))

    async def reboot(self,confirmed):
        if confirmed is not True:raise ValueError('Confirm restarting the Pi.')
        if not self.core.CONFIG.get('allow_host_reboot'):raise ValueError('Pi reboot is disabled. Enable it locally with the installer --enable-reboot option.')
        if any(j['status'] in ('staging','awaiting_start','printing','paused','needs_review') for j in self.store.jobs()):
            raise ValueError('Resolve or finish active queue jobs before rebooting the management Pi.')
        self.store.event('Server','Pi reboot requested','Administrator confirmed reboot.')
        async def later():
            await asyncio.sleep(3)
            proc=await asyncio.create_subprocess_exec('/usr/bin/sudo','-n','/usr/local/sbin/pm-host-reboot',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            if await proc.wait():self.store.event('Server','Reboot failed','Check the local sudoers/reboot configuration.')
        task=asyncio.create_task(later());self.inflight.add(task);task.add_done_callback(self.inflight.discard)

    async def devices(self):
        return await self.mesh.devices()

    async def device_command(self,device,action):
        return await self.mesh.command(device,action)

    async def scheduler(self):
        while True:
            try:
                if self.core.bot.is_ready() and not getattr(self.core,'update_pending',lambda:False)():
                    for row in list(self.db.execute("SELECT * FROM reminders WHERE status='pending' AND due<=? ORDER BY due LIMIT 10",(time.time(),))):
                        with self.db:self.db.execute("UPDATE reminders SET status='sending' WHERE id=?",(row['id'],))
                        try:
                            if not self.ids_allowed(row['guild']):raise ValueError('Server no longer allowed.')
                            user=await self.core.bot.fetch_user(int(row['owner']))
                            await user.send(embed=self.core.card('⏰ Your reminder',row['body'],self.core.YELLOW),allowed_mentions=discord.AllowedMentions.none())
                            with self.db:self.db.execute("UPDATE reminders SET status='sent' WHERE id=?",(row['id'],))
                        except Exception as exc:
                            with self.db:self.db.execute("UPDATE reminders SET status='failed',error=? WHERE id=?",(type(exc).__name__+': '+str(exc)[:200],row['id']))
                    if self.config.get('practice_enabled'):
                        now=datetime.now(ZoneInfo(self.config['timezone']))
                        hour,minute=map(int,self.config['practice_time'].split(':'))
                        due=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
                        if now.weekday() in self.config['practice_days'] and 0<=(now-due).total_seconds()<1200:
                            occurrence=f"{self.config['practice_channel_id']}:{due.strftime('%Y-%m-%dT%H:%M')}"
                            await self.assign(occurrence)
            except Exception:self.core.log.exception('Team scheduler error')
            await asyncio.sleep(20)

    async def web_state(self,request):
        return web.json_response(dict(config=self.config,server=self.server_status(),notes=self.notes(),
            reminders=[dict(r) for r in self.db.execute('SELECT * FROM reminders ORDER BY due DESC LIMIT 100')],
            assignments=[dict(r) for r in self.db.execute('SELECT * FROM assignments ORDER BY created DESC LIMIT 100')],
            archives=[dict(channel=r['channel'],guild=r['guild'],status=r['status']) for r in self.db.execute('SELECT * FROM archives')],
            devices=await self.devices(),meshcentral=self.mesh.public(),tasks=[dict(r) for r in self.db.execute('SELECT * FROM device_tasks ORDER BY created DESC LIMIT 100')],
            guild_ids=[str(x) for x in sorted(self.core.ALLOWED_GUILD_IDS)]))

    async def web_action(self,request):
        action=request.match_info['action'];data=await request.json();result={'ok':True}
        if action=='settings':self.save_config(data)
        elif action=='assign':result=await self.assign('manual:'+secrets.token_hex(8))
        elif action=='remember':
            guild=str(data.get('guild',''))
            if not self.ids_allowed(guild):raise ValueError('Choose an allowed server.')
            result={'id':self.remember(guild,'web administrator',str(data.get('title','')),str(data.get('body','')))}
        elif action=='forget':
            with self.db:self.db.execute('DELETE FROM notes WHERE id=?',(str(data.get('id')),))
        elif action=='remind':
            guild=str(data.get('guild',''));owner=str(int(data.get('user_id','0')))
            if not self.ids_allowed(guild) or int(owner)<=0:raise ValueError('Choose an allowed server and user ID.')
            result={'id':self.reminder(guild,owner,str(data.get('body','')),data.get('minutes',1))}
        elif action=='cancelreminder':
            with self.db:self.db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'",(str(data.get('id')),))
        elif action in ('archive','unarchive'):
            if data.get('confirmed')is not True:raise ValueError('Confirm the channel change.')
            await self.archive(int(data['channel_id']),action=='unarchive')
        elif action=='meshsettings':
            self.mesh.save(data)
            await self.mesh.devices(force=True)
        elif action=='meshrefresh':
            await self.mesh.devices(force=True)
        elif action=='command':
            if data.get('confirmed')is not True:raise ValueError('Confirm the laptop command.')
            result={'id':await self.device_command(str(data['device']),str(data['command']))}
        elif action=='reboot':await self.reboot(data.get('confirmed'))
        else:raise ValueError('Unknown team action.')
        return web.json_response(result)
