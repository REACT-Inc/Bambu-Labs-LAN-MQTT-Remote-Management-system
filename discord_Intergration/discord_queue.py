"""Discord is one interface to the same queue used by the dashboard."""
import asyncio
import secrets
from pathlib import Path
import discord
from discord import app_commands
from queueing import MAX_UPLOAD, ACTIVE, TERMINAL


def install(core, store, engine, dashboard):
    bot = core.bot
    original_action = core.run_action

    async def show_queue(interaction, name):
        jobs=store.jobs(name)
        waiting=[j for j in jobs if j['status'] not in TERMINAL]
        lines=[f"`{j['id']}` • **{core.safe(j['label'])}** — {j['status']}" for j in waiting]
        # Paginate text rather than silently dropping queued jobs.
        for start in range(0,max(1,len(lines)),15):
            await core.respond(interaction,core.card('📋 '+core.display_name(name)+' • Queue','\n'.join(lines[start:start+15]) or 'No waiting jobs. Use /queueadd to add a sliced file.'))

    class StartView(core.OwnedView):
        def __init__(self,owner,job,override_error=False):
            super().__init__(owner)
            self.job=job
            self.override_error=override_error
            self.button('Plate clear • Override error' if override_error else 'Plate clear • Start job',self.start,discord.ButtonStyle.danger)
            self.button('Cancel',self.cancel)
        async def start(self,interaction):
            if self.used:
                await interaction.response.send_message('Already used.',ephemeral=core.ephemeral(interaction));return
            self.used=True;self.stop()
            await interaction.response.defer(ephemeral=core.ephemeral(interaction))
            try:
                await engine.start(self.job['id'],True,core.who(interaction),self.override_error)
                await interaction.edit_original_response(embed=core.card('📡 Job accepted',self.job['label']+' • check the queue for printer confirmation.'),view=None)
            except (ValueError,RuntimeError) as exc:
                await interaction.edit_original_response(embed=core.card('Could not start',str(exc),core.RED),view=None)

    async def run_action(interaction,action,name):
        # Queue starts, changes and printer actions are open to every member; each asks for confirmation and is logged.
        if action=='queue':
            await show_queue(interaction,name)
        elif action in ('queuestart','queueforce'):
            job=next((j for j in store.jobs(name) if j['status']=='queued'),None)
            if not job:
                await core.respond(interaction,core.card('Queue empty','Add a job with /queueadd.'));return
            opts=job['options']
            await core.respond(interaction,core.card('⚠️ Start next print',
                f"**{core.safe(name)}**\n{core.safe(job['label'])}\nPlate {opts['plate']} • {'AMS '+str(opts['ams_mapping']) if opts['use_ams'] else 'External spool'}\n"
                'Confirm the build plate is clear, the correct material is loaded, and this file was sliced for this printer.'
                + ('\n**Override:** ignore the reported error and allow FAILED state for this attempt. This does not clear printer errors or bypass firmware protections.' if action=='queueforce' else ''),core.YELLOW),StartView(interaction.user.id,job,action=='queueforce'))
        elif action=='reprint':
            previous=next((j for j in reversed(store.jobs(name)) if j['status']=='finished'),None)
            if not previous:
                await core.respond(interaction,core.card('No managed print history','Reprint uses previously completed queue jobs. Add the exact sliced file with /queueadd.',core.YELLOW));return
            class Requeue(core.OwnedView):
                def __init__(self):
                    super().__init__(interaction.user.id)
                    self.button('Add reprint to queue',self.confirm,discord.ButtonStyle.primary)
                    self.button('Cancel',self.cancel)
                async def confirm(self,click):
                    if await self.finish(click,core.card('Reprint queued',core.safe(previous['label']),core.GREEN)):
                        dashboard.copy_job(previous['id'],core.who(click))
            await core.respond(interaction,core.card('Reprint this job?',core.safe(previous['label'])+'\nThis adds a new queue entry. Use /queuestart after clearing the plate.'),Requeue())
        else:
            await original_action(interaction,action,name)

    core.run_action=run_action
    # Existing pause/resume/stop confirmations now call the shared engine.
    async def send_action(interaction,name,action,filename=None):
        try:
            await engine.control(name,action,core.who(interaction))
            await core.respond(interaction,core.card('📡 '+action.title(),core.safe(name)+' • command submitted; wait for printer status.',core.GREEN))
        except (ValueError,RuntimeError) as exc:
            await core.respond(interaction,core.card('Command not sent',str(exc),core.RED))
    core.send_action=send_action
    core.register_printer_command('queue','View the shared queue for a printer')
    core.register_printer_command('queuestart','Confirm the plate is clear and start the first queued job')
    core.register_printer_command('queueforce','Start the next job even though the printer reports an error, with confirmation')
    core.register_printer_command('lighton','Turn the printer light on')
    core.register_printer_command('lightoff','Turn the printer light off')

    @bot.tree.command(name='queueadd',description='Queue a sliced .3mf upload or a file already on the printer')
    @app_commands.guild_only()
    @app_commands.describe(name='Printer name, partial name, or leave blank to choose',
        file='A sliced Bambu/Orca .3mf file',remote='Alternative: path on printer, e.g. cache/model.gcode.3mf',
        mapping='AMS slot mapping, e.g. 0 or 0,1; required if use_ams is true')
    async def queueadd(interaction:discord.Interaction,name:str=None,file:discord.Attachment=None,
                       remote:str=None,label:str=None,plate:int=1,use_ams:bool=False,mapping:str='',bed:str='textured_plate'):
        if bool(file)==bool(remote):
            await interaction.response.send_message('Provide either a sliced file attachment OR its existing path on the printer.',ephemeral=core.ephemeral(interaction));return
        await interaction.response.defer(ephemeral=core.ephemeral(interaction))
        asset=None
        if file:
            if file.size>MAX_UPLOAD or not file.filename.lower().endswith('.3mf'):
                await interaction.followup.send('Use a sliced .3mf file up to 256 MiB (Discord may impose a lower upload limit).',ephemeral=core.ephemeral(interaction));return
            if __import__('shutil').disk_usage(dashboard.uploads).free<MAX_UPLOAD*2:
                await interaction.followup.send('Not enough free disk space.',ephemeral=core.ephemeral(interaction));return
            asset=secrets.token_hex(16);path=dashboard.uploads/(asset+'.3mf')
            try:
                await asyncio.wait_for(file.save(path),timeout=120)
            except BaseException:
                path.unlink(missing_ok=True);raise
        data=dict(asset=asset,remote=remote or '',label=label or (file.filename if file else Path(remote).name),
                  plate=plate,use_ams=use_ams,mapping=mapping,bed=bed)
        async def add(click,printer):
            try:
                job=dashboard.add(dict(data,printer=printer),core.who(interaction))
                await core.respond(click,core.card('📋 Job queued',f"**{core.safe(printer)}**\n{core.safe(job['label'])}\nID: `{job['id']}`",core.GREEN))
                await core.notify(printer,'📋 Job queued',job['label'],core.BLUE)
            except ValueError as exc:
                await core.respond(click,core.card('Cannot queue file',str(exc),core.RED))
        exact,suggestion=core.resolve_name(name)
        if exact:
            await add(interaction,exact)
        else:
            class AddPicker(core.PrinterPicker):
                def choose(self,printer):
                    async def callback(click):
                        if await self.finish(click,core.card('Printer selected',core.safe(printer))):
                            await add(click,printer)
                    return callback
            await core.respond(interaction,core.card('Choose printer',f'Did you mean **{core.safe(suggestion)}**?' if suggestion else 'Select the printer this file was sliced for.'),
                               AddPicker(interaction.user.id,'queueadd',suggestion))

    @bot.tree.command(name='queuemanage',description='Remove, reorder or resolve a queued job, with confirmation')
    @app_commands.guild_only()
    @app_commands.choices(action=[app_commands.Choice(name=x,value=x) for x in ('up','down','remove','resolve_finished','resolve_failed','resolve_cancelled')])
    async def queuemanage(interaction:discord.Interaction,job_id:str,action:str):
        try:job=store.get(job_id)
        except ValueError:
            await core.respond(interaction,core.card('Job not found',f'No job with ID `{core.safe(job_id)}`. Use /queue to see job IDs.',core.RED));return
        class Confirm(core.OwnedView):
            def __init__(self):
                super().__init__(interaction.user.id)
                self.button('Confirm',self.confirm,discord.ButtonStyle.danger)
                self.button('Cancel',self.cancel)
            async def confirm(self,click):
                if not await self.finish(click,core.card('Processing queue change',job_id)):return
                try:
                    if action.startswith('resolve_'):
                        engine.resolve(job_id,action[8:],True,core.who(click))
                    else: store.edit(job_id,action,core.who(click))
                    await core.respond(click,core.card('Queue updated',job_id,core.GREEN))
                except ValueError as exc:
                    await core.respond(click,core.card('Queue unchanged',str(exc),core.RED))
        await core.respond(interaction,core.card('Confirm queue change',
            f"{action} • **{core.safe(job['label'])}**\n"+('Inspect the physical printer first. This records an outcome; it does not stop or control the printer.' if action.startswith('resolve_') else 'Apply this change?'),core.YELLOW),Confirm())

    async def printer_autocomplete(interaction,current):
        return [app_commands.Choice(name=core.display_name(n)[:100],value=core.display_name(n)) for n in core.names() if current.casefold() in (n+' '+core.display_name(n)).casefold()][:25]
    queueadd.autocomplete('name')(printer_autocomplete)
