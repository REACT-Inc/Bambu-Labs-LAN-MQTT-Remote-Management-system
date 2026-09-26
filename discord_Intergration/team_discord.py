import secrets
import time
import discord
from discord import app_commands


def install(core,team):
    bot=core.bot
    def permitted(i):
        # Administrator or approved user ID (e.g. may delete anyone's note). Command access uses core.require.
        return i.guild_id in core.ALLOWED_GUILD_IDS and (i.user.id in core.SETTINGS_USER_IDS or isinstance(i.user,discord.Member) and i.user.guild_permissions.administrator)
    async def private_respond(i,embed,view=None):
        if not i.response.is_done():await i.response.defer(ephemeral=core.ephemeral(i))
        message=await i.followup.send(embed=embed,view=view,ephemeral=core.ephemeral(i),wait=True)
        if view:view.message=message

    class Confirm(core.OwnedView):
        def __init__(self,owner,callback,label='Confirm',command=None):
            super().__init__(owner);self.callback=callback;self.command=command
            self.button(label,self.confirm,discord.ButtonStyle.danger);self.button('Cancel',self.cancel)
        async def confirm(self,i):
            if self.command and not await core.require(i,self.command):return
            if not await self.finish(i,core.card('Request confirmed','Processing…')):return
            try:
                result=await self.callback()
                await private_respond(i,core.card('Request complete',str(result or 'Done.'),core.GREEN))
            except Exception as exc:
                await private_respond(i,core.card('Request failed',str(exc)[:1800],core.RED))

    for name,key,description in [('ftc','ftc_url','FTC Discord invite'),('website','website_url','Team website'),('management','management_url','3D Printer Management dashboard')]:
        def make(key,description):
            async def callback(i:discord.Interaction):
                value=team.config.get(key)
                await core.respond(i,core.card(description,value or 'An administrator needs to set this link in Team settings.'))
            return callback
        bot.tree.command(name=name,description=description)(app_commands.guild_only()(make(key,description)))

    @bot.tree.command(name='rememberthis',description='Save a shared team note')
    @app_commands.guild_only()
    async def rememberthis(i:discord.Interaction,title:str,text:str):
        note=team.remember(i.guild_id,i.user.id,title,text)
        await core.respond(i,core.card('📝 Remembered',f'{core.safe(title)}\nNote ID: `{note}`\nUse /remember to find it.'))

    @bot.tree.command(name='remember',description='Find shared team notes')
    @app_commands.guild_only()
    async def remember(i:discord.Interaction,query:str=''):
        rows=[r for r in team.notes(i.guild_id) if query.casefold() in (r['title']+' '+r['body']).casefold()]
        if not rows:await core.respond(i,core.card('Team notes','No matching notes.'));return
        for row in rows[:5]:await core.respond(i,core.card('📝 '+row['title'],row['body']+f"\n\nID: `{row['id']}`"))
        if len(rows)>5:await core.respond(i,core.card('More results','Refine your search or use the dashboard to view more notes.'))

    @bot.tree.command(name='forget',description='Delete one of your notes (admins may delete any team note)')
    @app_commands.guild_only()
    async def forget(i:discord.Interaction,note_id:str):
        row=team.db.execute('SELECT * FROM notes WHERE id=? AND guild=?',(note_id,str(i.guild_id))).fetchone()
        if not row or (row['owner']!=str(i.user.id) and not permitted(i)):
            await i.response.send_message('Note not found or you do not own it.',ephemeral=core.ephemeral(i));return
        with team.db:team.db.execute('DELETE FROM notes WHERE id=?',(note_id,))
        await i.response.send_message('Note deleted.',ephemeral=core.ephemeral(i))

    @bot.tree.command(name='remindme',description='Send yourself a Discord DM reminder after a number of minutes')
    @app_commands.guild_only()
    async def remindme(i:discord.Interaction,minutes:int,text:str):
        rid=team.reminder(i.guild_id,i.user.id,text,minutes)
        await i.response.send_message(embed=core.card('⏰ Reminder saved',f"I will DM you <t:{int(time.time()+minutes*60)}:R>. Enable DMs from this server.\nID: `{rid}`"),ephemeral=core.ephemeral(i))

    @bot.tree.command(name='reminders',description='View your reminders and delivery status')
    @app_commands.guild_only()
    async def reminders(i:discord.Interaction):
        rows=team.db.execute('SELECT * FROM reminders WHERE owner=? AND guild=? ORDER BY due DESC LIMIT 15',(str(i.user.id),str(i.guild_id)))
        text='\n'.join(f"`{r['id']}` • {r['status']} • <t:{int(r['due'])}:R> • {core.safe(r['body'][:80])}" for r in rows)
        await i.response.send_message(embed=core.card('Your reminders',text or 'No reminders.'),ephemeral=core.ephemeral(i))

    @bot.tree.command(name='cancelreminder',description='Cancel your pending reminder')
    @app_commands.guild_only()
    async def cancelreminder(i:discord.Interaction,reminder_id:str):
        with team.db:cursor=team.db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND owner=? AND guild=? AND status='pending'",(reminder_id,str(i.user.id),str(i.guild_id)))
        await i.response.send_message('Cancelled.' if cursor.rowcount else 'No pending reminder with that ID belongs to you.',ephemeral=core.ephemeral(i))

    for cmd,restore in [('archive',False),('unarchive',True)]:
        def make(restore):
            async def callback(i:discord.Interaction,channel:discord.TextChannel):
                if not await core.require(i,'unarchive' if restore else 'archive'):return
                async def apply():
                    await team.archive(channel.id,restore);return f'{channel.mention} '+('restored.' if restore else 'archived. Messages are retained.')
                await core.respond(i,core.card('Confirm channel change',f"{'Restore' if restore else 'Archive'} {channel.mention}?\nArchiving makes it read-only for non-administrators and moves it to Archive. No messages are deleted.",core.YELLOW),Confirm(i.user.id,apply,command='unarchive' if restore else 'archive'))
            return callback
        bot.tree.command(name=cmd,description='Restore an archived channel' if restore else 'Archive a text channel without deleting messages')(app_commands.guild_only()(make(restore)))

    async def assign_report(i,member=None,announce=False,command='assign'):
        if not await core.require(i,command):return
        await i.response.defer(ephemeral=True)
        try:
            result=await team.assign('manual:'+secrets.token_hex(8),channel_id=None if announce else i.channel_id,member_id=member.id if member else None,announce=announce,guild_id=i.guild_id)
            detail='An announcement was requested in the practice channel.' if announce else 'Saved privately. No channel announcement or DM was sent.'
            await i.followup.send(f"Assignment `{result['id']}`: <@{result['member']}> • {result['status']}\n{detail}",ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            core.log.warning('Manual report assignment failed (%s)',type(exc).__name__)
            await i.followup.send('Could not assign report: '+str(exc)[:1500],ephemeral=True)

    assign=app_commands.Group(name='assign',description='Assign team responsibilities')
    @assign.command(name='report',description='Assign a chosen person or select from the roster (admins/approved IDs)')
    @app_commands.guild_only()
    @app_commands.describe(member='Choose a person, or omit for a random roster selection',announce='Post in the practice channel; your command reply stays private')
    async def report(i:discord.Interaction,member:discord.Member=None,announce:bool=False):
        await assign_report(i,member,announce)
    bot.tree.add_command(assign)

    meeting=app_commands.Group(name='meeting',description='Meeting tools')
    reports=app_commands.Group(name='report',description='Meeting report responsibilities',parent=meeting)
    @reports.command(name='assign',description='Assign a report writer (admins/approved IDs; private reply)')
    @app_commands.guild_only()
    @app_commands.describe(member='Choose a person, or omit for a random roster selection',announce='Post in the practice channel; your command reply stays private')
    async def meeting_assign(i:discord.Interaction,member:discord.Member=None,announce:bool=False):
        await assign_report(i,member,announce,'meeting')
    bot.tree.add_command(meeting)

    @bot.tree.command(name='laptops',description='View laptops from MeshCentral')
    @app_commands.guild_only()
    async def laptops(i:discord.Interaction):
        if not await core.require(i,'laptops'):return
        await i.response.defer(ephemeral=core.ephemeral(i))
        rows=await team.devices();lines=[f"{'🟢' if d['online'] else '🔴'} **{core.safe(d['name'])}** • `{d['id']}`\nCommands: {', '.join(d['report'].get('commands',[])) or 'No configured commands'}" for d in rows]
        for start in range(0,max(1,len(lines)),8):await i.followup.send(embed=core.card('💻 MeshCentral laptops','\n\n'.join(lines[start:start+8]) or 'Connect MeshCentral in the dashboard Laptops tab. ' + (team.mesh.error or '') + ''),ephemeral=core.ephemeral(i))

    laptop=app_commands.Group(name='laptop',description='Manage MeshCentral laptops')
    @laptop.command(name='cmd',description='Run a configured command through MeshCentral')
    @app_commands.guild_only()
    async def cmd(i:discord.Interaction,device_id:str,command:str):
        if not await core.require(i,'laptop'):return
        async def apply():return 'MeshCentral task: '+await team.device_command(device_id,command)+'. Results appear in the dashboard or /laptop result. An unknown result is never retried automatically.'
        await private_respond(i,core.card('Confirm laptop command',f'`{device_id}` • **{core.safe(command)}**\nOnly server-configured commands can execute through MeshCentral.',core.YELLOW),Confirm(i.user.id,apply,command='laptop'))
    @cmd.autocomplete('device_id')
    async def devices_complete(i,current):
        if not core.command_allowed(i,'laptop'):return []
        return [app_commands.Choice(name=d['name'][:100],value=d['id']) for d in team.mesh.cached if current.casefold() in (d['name']+d['id']).casefold()][:25]
    @cmd.autocomplete('command')
    async def commands_complete(i,current):
        if not core.command_allowed(i,'laptop'):return []
        device=next((d for d in team.mesh.cached if d['id']==getattr(i.namespace,'device_id',None)),None)
        return [app_commands.Choice(name=c,value=c) for c in (device['report'].get('commands',[]) if device else []) if current.casefold() in c.casefold()][:25]
    @laptop.command(name='result',description='View the result of a laptop command')
    @app_commands.guild_only()
    async def result(i:discord.Interaction,task_id:str):
        if not await core.require(i,'laptop'):return
        row=team.db.execute('SELECT * FROM device_tasks WHERE id=?',(task_id,)).fetchone()
        await i.response.send_message(embed=core.card('Laptop task',f"{row['status']}\n{row['result']}" if row else 'Task not found.'),ephemeral=core.ephemeral(i))
    bot.tree.add_command(laptop)

    @bot.tree.command(name='server',description='Pi server health and disk status')
    @app_commands.guild_only()
    async def server(i:discord.Interaction):
        if not await core.require(i,'server'):return
        s=team.server_status()
        await core.respond(i,core.card('🖥️ Management server',f"**{core.safe(s['host'])}**\nUptime: {int((s['uptime'] or 0)/3600)} hours\nFree disk: {s['disk_free']/1024**3:.1f} GiB\nTemperature: {s['temperature'] if s['temperature'] is not None else 'unavailable'}\nPi reboot enabled: {s['reboot_enabled']}"))

    @bot.tree.command(name='reboot',description='Reboot the management Pi (requires local enablement)')
    @app_commands.guild_only()
    async def reboot(i:discord.Interaction):
        if not await core.require(i,'reboot'):return
        async def apply():await team.reboot(True);return 'Pi reboot requested. The dashboard and Discord connection will briefly go offline.'
        await core.respond(i,core.card('Reboot the Pi?', 'This restarts the management server, not a printer. Active queued prints must be resolved or finished first.',core.YELLOW),Confirm(i.user.id,apply,'Reboot Pi',command='reboot'))
