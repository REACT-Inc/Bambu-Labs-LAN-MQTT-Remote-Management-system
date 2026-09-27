"""Administrative messaging, temporary public replies and printer file browsing."""
import io
import re
import time
import discord
from discord import app_commands
from printer_files import Browser, render, normalize


def permitted(core,i):
    return i.guild_id in core.ALLOWED_GUILD_IDS and (i.user.id in core.SETTINGS_USER_IDS or isinstance(i.user,discord.Member) and i.user.guild_permissions.administrator)


def install(core,store):
    bot=core.bot;browser=Browser(core);original=core.run_action
    async def admin(i):
        if permitted(core,i):return True
        text='Administrators and approved user IDs only.'
        if i.response.is_done():await i.followup.send(text,ephemeral=core.ephemeral(i))
        else:await i.response.send_message(text,ephemeral=core.ephemeral(i))
        return False

    @bot.tree.command(name='dm',description='Preview and send a bot DM (admins/approved users)')
    @app_commands.guild_only()
    @app_commands.describe(user='User ID or @mention',message='Message to send privately')
    async def dm(i:discord.Interaction,user:str,message:str):
        if not await admin(i):return
        await i.response.defer(ephemeral=True)
        match=re.fullmatch(r'(?:<@!?)?([0-9]{15,22})>?',user.strip())
        if not match or not 1<=len(message)<=3500:
            await i.followup.send('Provide a user ID/@mention and a message of 1–3500 characters.',ephemeral=True);return
        try:recipient=await bot.fetch_user(int(match[1]))
        except discord.HTTPException:
            await i.followup.send('Could not find that Discord user.',ephemeral=True);return
        if recipient.bot:
            await i.followup.send('Choose a person, not a bot account.',ephemeral=True);return
        class Send(core.OwnedView):
            def __init__(self):
                super().__init__(i.user.id);self.button('Send DM',self.send,discord.ButtonStyle.primary);self.button('Cancel',self.cancel)
            async def send(self,click):
                if not await admin(click):return
                if self.used:return
                self.used=True;self.stop()
                await click.response.defer()
                try:
                    # Sent in the server's name only; the recipient is not told which administrator sent it.
                    embed=core.card('Message from '+i.guild.name,message)
                    await recipient.send(embed=embed,allowed_mentions=discord.AllowedMentions.none())
                    store.event('Team','DM sent',f'From {i.user.id} to {recipient.id}; message body not logged.')
                    result=f'DM delivered to {recipient} ({recipient.id}).'
                except discord.Forbidden:result='Discord refused the DM. The recipient may block the bot or disallow DMs. Nothing was posted publicly.'
                except discord.HTTPException:result='Discord did not confirm delivery. Check with the recipient before retrying.'
                await click.edit_original_response(content=result,embed=None,view=None)
        view=Send()
        view.message=await i.followup.send(embed=core.card('Confirm private message',f'To: **{core.safe(recipient)}** (`{recipient.id}`)\n\n{message}\n\n*Sent as "Message from {core.safe(i.guild.name)}". Your name is not shown to the recipient.*'),view=view,ephemeral=True,wait=True)

    @bot.tree.command(name='rename',description='Rename a printer in management only (admins/approved users)')
    @app_commands.guild_only()
    @app_commands.describe(name='Current name or partial match',new_name='New management display name')
    async def rename(i:discord.Interaction,new_name:str,name:str=None):
        if not await admin(i):return
        await i.response.defer(ephemeral=core.ephemeral(i))
        async def confirm_for(click,printer):
            class Rename(core.OwnedView):
                def __init__(self):
                    super().__init__(i.user.id)
                    self.button('Rename in management',self.confirm,discord.ButtonStyle.primary)
                    self.button('Cancel',self.cancel)
                async def confirm(self,event):
                    if not await admin(event):return
                    if self.used:return
                    self.used=True;self.stop()
                    await event.response.defer()
                    try:
                        old=core.display_name(printer)
                        value=core.rename_printer(printer,new_name)
                        store.event(printer,'Management name changed',f'{old} → {value} • {event.user.id}')
                        message=f'Management name is now {value}. The printer device name was not changed.'
                    except ValueError as exc:message=str(exc)
                    await event.edit_original_response(content=message,embed=None,view=None)
            view=Rename()
            view.message=await click.followup.send(embed=core.card('Confirm printer rename',f'{core.safe(printer)} → {discord.utils.escape_markdown(new_name)}\nQueues, monitoring, and saved jobs stay attached to the same printer.'),view=view,ephemeral=core.ephemeral(click),wait=True)
        exact,suggestion=core.resolve_name(name)
        if exact:await confirm_for(i,exact)
        else:
            class Picker(core.PrinterPicker):
                def choose(self,printer):
                    async def picked(click):
                        if not await admin(click):return
                        if await self.finish(click,core.card('Printer selected',core.safe(printer))):
                            await confirm_for(click,printer)
                    return picked
            view=Picker(i.user.id,'rename',suggestion)
            view.message=await i.followup.send(embed=core.card('Choose printer','Select the printer to rename.'),view=view,ephemeral=core.ephemeral(i),wait=True)

    @bot.tree.command(name='publiccommands',description='Show the server-wide reply policy privately (admins/approved users)')
    @app_commands.guild_only()
    async def publiccommands(i:discord.Interaction,minutes:app_commands.Range[int,0,60]=2):
        if not await admin(i):return
        await i.response.send_message('Commands now reply publicly in every server channel. This command, report assignment and DM workflows remain private; a temporary override is no longer needed.',ephemeral=True)

    async def run_action(i,action,name):
        kind, _, folder = action.partition(':')
        if kind not in ('filelist','filesystem'):return await original(i,action,name)
        if not i.response.is_done():await i.response.defer(ephemeral=core.ephemeral(i))
        try:
            result=await browser.browse(name,folder or '/')
            text=render(result,kind=='filelist')
            preview='\n'.join(text.splitlines()[:16])
            embed=core.card('📁 '+core.display_name(name),core.safe(preview[:2600])+'\n\nFull listing attached. For a sliced .3mf use `/queueadd remote:<path>` (omit the leading /). Listing does not verify slicing or printer compatibility.')
            await i.followup.send(embed=embed,file=discord.File(io.BytesIO(text.encode()),filename='printer-files.txt'),ephemeral=core.ephemeral(i))
        except Exception as exc:
            core.log.warning('Printer listing failed for %s (%s)',name,type(exc).__name__)
            await core.respond(i,core.card('Could not list files','Printer storage could not be read. Check its connection, access code, and USB/SD storage. '+type(exc).__name__,core.RED))
    core.run_action=run_action
    files=app_commands.Group(name='file',description='Browse files stored on a printer')
    @files.command(name='list',description='List candidate .3mf and .gcode print files on the printer')
    @app_commands.guild_only()
    async def file_list(i:discord.Interaction,name:str=None,path:str='/'):await core.choose_printer(i,'filelist:'+normalize(path),name)
    @files.command(name='system',description='List printer folders and files recursively, with a downloadable listing')
    @app_commands.guild_only()
    async def file_system(i:discord.Interaction,name:str=None,path:str='/'):await core.choose_printer(i,'filesystem:'+normalize(path),name)
    bot.tree.add_command(files)
