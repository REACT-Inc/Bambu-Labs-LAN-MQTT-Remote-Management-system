import discord
from discord import app_commands
from discord_Intergration.extra_discord import permitted
from printer_controls import prepare
from thermal_controls import fans


def install(core,controls):
    async def check(i):
        if permitted(core,i):return True
        if i.response.is_done():await i.followup.send('Administrators and approved user IDs only.',ephemeral=core.ephemeral(i))
        else:await i.response.send_message('Administrators and approved user IDs only.',ephemeral=core.ephemeral(i))
        return False

    async def choose(i,name,kind,value,axis=None):
        if not await check(i):return
        await i.response.defer(ephemeral=core.ephemeral(i))
        async def preview(click,printer):
            try:_,_,label=prepare(core,printer,kind,value,axis)
            except ValueError as exc:
                await click.followup.send(str(exc),ephemeral=core.ephemeral(click));return
            class Confirm(core.OwnedView):
                def __init__(self):
                    super().__init__(i.user.id)
                    self.button('Confirm move' if kind=='move' else 'Apply setting',self.apply,discord.ButtonStyle.danger)
                    self.button('Cancel',self.cancel)
                async def apply(self,event):
                    if not await check(event):return
                    if self.used:return
                    self.used=True;self.stop();await event.response.defer()
                    try:message=controls.apply(printer,kind,value,axis,confirmed=True,homed=kind=='move')
                    except ValueError as exc:message=str(exc)
                    await event.edit_original_response(content=message,embed=None,view=None)
            detail=f'{core.safe(printer)}\n{label}'
            if kind=='move':detail+='\nConfirm only after homing on the printer and checking the nozzle, bed and travel path are clear. Direction follows printer coordinates, not necessarily bed travel. Never jog during a print.'
            elif kind=='nozzle':detail+='\nTargets the active nozzle; does not switch H2D tools.'
            view=Confirm();view.message=await click.followup.send(embed=core.card('Confirm printer control',detail),view=view,ephemeral=core.ephemeral(click),wait=True)
        exact,suggestion=core.resolve_name(name)
        if exact:await preview(i,exact)
        else:
            class Picker(core.PrinterPicker):
                def choose(self,printer):
                    async def picked(click):
                        if not await check(click):return
                        if await self.finish(click,core.card('Printer selected',core.safe(printer))):await preview(click,printer)
                    return picked
            view=Picker(i.user.id,kind,suggestion)
            view.message=await i.followup.send(embed=core.card('Choose printer','Select the printer to control.'),view=view,ephemeral=core.ephemeral(i),wait=True)

    @core.bot.tree.command(name='temperature',description='Set bed or active nozzle temperature (admins/approved IDs)')
    @app_commands.guild_only()
    @app_commands.choices(target=[app_commands.Choice(name=x,value=x) for x in ('nozzle','bed','chamber')])
    async def temperature(i:discord.Interaction,target:str,degrees:int,name:str=None):await choose(i,name,target,degrees)

    @core.bot.tree.command(name='speed',description='Set print speed profile (admins/approved IDs)')
    @app_commands.guild_only()
    @app_commands.choices(mode=[app_commands.Choice(name=x.title(),value=x) for x in ('silent','standard','sport','ludicrous')])
    async def speed(i:discord.Interaction,mode:str,name:str=None):await choose(i,name,'speed',mode)

    async def fan_options(i:discord.Interaction,current:str):
        if not permitted(core,i):return []
        name=getattr(i.namespace,'name',None)
        exact,_=core.resolve_name(name)
        if exact:options=[(f['label']+(' (automatic)' if not f['manual'] else ''),f['key']) for f in fans(core,exact)]
        else:options=[('Part cooling','part'),('Auxiliary cooling','auxiliary'),('Chamber / exhaust','chamber'),('Internal circulation','circulation'),('Auxiliary cooling 2','auxiliary2')]
        return [app_commands.Choice(name=label[:100],value=key) for label,key in options if current.lower() in (label+' '+key).lower()][:25]

    @core.bot.tree.command(name='fan',description='Set a selected fan percentage (admins/approved IDs)')
    @app_commands.guild_only()
    @app_commands.autocomplete(target=fan_options)
    async def fan(i:discord.Interaction,percent:app_commands.Range[int,0,100],name:str=None,target:str='part'):
        await choose(i,name,'fan_'+target,percent)

    @core.bot.tree.command(name='fanall',description='Set all manually controllable fans on one printer (admins/approved IDs)')
    @app_commands.guild_only()
    async def fanall(i:discord.Interaction,percent:app_commands.Range[int,0,100],name:str=None):await choose(i,name,'fanall',percent)

    @core.bot.tree.command(name='chamber',description='Set H2D chamber target: 0 off or 40–65 °C (admins/approved IDs)')
    @app_commands.guild_only()
    async def chamber(i:discord.Interaction,degrees:app_commands.Range[int,0,65],name:str=None):await choose(i,name,'chamber',degrees)

    @core.bot.tree.command(name='move',description='Jog an idle, homed printer axis (admins/approved IDs)')
    @app_commands.guild_only()
    @app_commands.choices(axis=[app_commands.Choice(name=x,value=x) for x in ('X','Y','Z')])
    async def move(i:discord.Interaction,axis:str,millimeters:float,name:str=None):await choose(i,name,'move',millimeters,axis)
