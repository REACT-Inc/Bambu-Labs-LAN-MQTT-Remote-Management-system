"""Server and admin Discord commands: channel archiving, MeshCentral laptops, Pi status and reboot."""
import discord
from discord import app_commands


def install(core,team):
    # team is the Team backend (ftcTeamManagement.team): it also holds archiving, MeshCentral and server status.
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

    for cmd,restore in [('archive',False),('unarchive',True)]:
        def make(restore):
            async def callback(i:discord.Interaction,channel:discord.TextChannel):
                if not await core.require(i,'unarchive' if restore else 'archive'):return
                async def apply():
                    await team.archive(channel.id,restore);return f'{channel.mention} '+('restored.' if restore else 'archived. Messages are retained.')
                await core.respond(i,core.card('Confirm channel change',f"{'Restore' if restore else 'Archive'} {channel.mention}?\nArchiving makes it read-only for non-administrators and moves it to Archive. No messages are deleted.",core.YELLOW),Confirm(i.user.id,apply,command='unarchive' if restore else 'archive'))
            return callback
        bot.tree.command(name=cmd,description='Restore an archived channel' if restore else 'Archive a text channel without deleting messages')(app_commands.guild_only()(make(restore)))

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
