import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from printer_errors import errors, lookup
from printer_files import normalize, render, scan


class ErrorTests(unittest.TestCase):
    def test_official_print_error(self):
        entry=lookup('0502C014',model='093')
        self.assertIn('Remaining Filament',entry['message'])
    def test_hms_full_64_bit_code(self):
        result=errors(0,{'hms':[{'attr':int('03001800',16),'code':int('0001000C',16)}]},'093')
        self.assertEqual(result[0]['code'],'030018000001000C')
        self.assertIn('extrusion force',result[0]['message'])
    def test_unknown_not_guessed(self):
        self.assertIn('Unrecognized',lookup('FFFFFFFF')['message'])
    def test_zero_and_invalid(self):
        self.assertEqual(errors(0,{'hms':[None,{'attr':'bad!'}]}),[])


class FileTests(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(normalize('cache'),'/cache')
        for path in ('../etc','/cache/../','abc\r\nDELE x'):
            with self.assertRaises(ValueError):normalize(path)
    def test_recursive_listing(self):
        class FTP:
            def __init__(self,**kw):pass
            def connect(self,*a):pass
            def login(self,*a):pass
            def prot_p(self):pass
            def cwd(self,path):self.path=path
            def close(self):pass
            def retrlines(self,cmd,callback):
                rows={'/':['drwxr-xr-x 1 u g 0 Sep 24 12:00 cache','lrwxr-xr-x 1 u g 0 Sep 24 12:00 loop -> /'],
                      '/cache':['-rw-r--r-- 1 u g 123 Sep 24 12:00 my part.gcode.3mf','-rw-r--r-- 1 u g 9 Sep 24 12:00 video.mp4']}
                for row in rows[self.path]:callback(row)
        with patch.dict(sys.modules,{'bambulabs_api.ftp_client':SimpleNamespace(ImplicitFTP_TLS=FTP)}):
            data=scan({'ip':'test','access_code':'test'})
        text=render(data,True)
        self.assertIn('my part.gcode.3mf',text);self.assertNotIn('video.mp4',text)
        self.assertEqual(len(data['entries']),4) # links are listed but never traversed


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'guild_ids':[123],'admin_user_ids':[42],'demo':True}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('test_core_features',Path(__file__).parents[1]/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
        self.store=SimpleNamespace(event=MagicMock())
        from discord_Intergration.extra_discord import install
        install(self.core,self.store)
        self.i=MagicMock(spec=discord.Interaction)
        self.i.guild_id=123;self.i.channel_id=789;self.i.guild=SimpleNamespace(name='Team')
        self.i.user=SimpleNamespace(id=42)
        self.i.message=None;self.i.command=None;self.i.data={}
        self.i.response=SimpleNamespace(is_done=lambda:False,defer=AsyncMock(),send_message=AsyncMock())
        self.i.followup=SimpleNamespace(send=AsyncMock())
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()
    async def test_help_single_reply_with_all_features(self):
        from queueing import Store, Engine
        from dashboard import Dashboard
        from team import Team
        from discord_Intergration.discord_queue import install as install_queue
        from discord_Intergration.team_discord import install as install_team
        from discord_Intergration.controls_discord import install as install_controls
        from swapMod.plate_swap_discord import install as install_swap
        store=Store(Path(self.tmp.name)/'help.sqlite')
        try:
            engine=Engine(self.core,store)
            dashboard=Dashboard(self.core,store,engine)
            install_queue(self.core,store,engine,dashboard)
            install_team(self.core,Team(self.core,store,dashboard))
            install_controls(self.core,dashboard.controls)
            install_swap(self.core,engine)
            self.core.settings['commands_channel_id']=456
            await self.core.bot.tree.get_command('help').callback(self.i)
            self.i.response.send_message.assert_awaited_once()
            self.i.followup.send.assert_not_awaited()
            result=self.i.response.send_message.call_args.kwargs
            self.assertFalse(result['ephemeral'])
            embed=result['embed']
            self.assertLessEqual(len(embed),6000)
            self.assertLessEqual(len(embed.fields),25)
            text=' '.join(f.value for f in embed.fields)
            for f in embed.fields:self.assertLessEqual(len(f.value),1024)
            def check(command):
                if isinstance(command,discord.app_commands.Group):
                    for child in command.commands:check(child)
                else:
                    name='`/'+command.qualified_name+'`'
                    if command.qualified_name.split()[0] in self.core.ADMIN_COMMANDS:self.assertNotIn(name,text)
                    else:self.assertIn(name,text)
            for command in self.core.bot.tree.get_commands():check(command)
            self.i.response.send_message.reset_mock()
            await self.core.bot.tree.get_command('adminhelp').callback(self.i)
            self.i.response.send_message.assert_awaited_once()
            result=self.i.response.send_message.call_args.kwargs
            self.assertFalse(result['ephemeral']);self.assertLess(len(result['embed']),6000)
            admintext=' '.join(f.value for f in result['embed'].fields)
            for name in ('temperature','move','rename','dm','plateswap approve','laptop cmd'):
                self.assertIn('`/'+name+'`',admintext)
            self.i.user.id=7
            for name in ('temperature','chamber','fan','fanall','move','rename','dm','plateswap','laptop','pause'):
                cmd=self.core.bot.tree.get_command(name)
                self.i.command=cmd
                self.assertFalse(await self.core.bot.tree.interaction_check(self.i))
            self.i.command=self.core.bot.tree.get_command('temperature')
            self.i.user.id=42
            self.assertTrue(await self.core.bot.tree.interaction_check(self.i))
            self.i.user=MagicMock(spec=discord.Member);self.i.user.id=7;self.i.user.guild_permissions.administrator=True
            self.assertTrue(await self.core.bot.tree.interaction_check(self.i))
        finally:store.db.close()

    async def test_plate_swap_permission_and_registration(self):
        from swapMod.plate_swap_discord import install
        engine=SimpleNamespace(plate_swap=MagicMock())
        install(self.core,engine)
        for command in self.core.bot.tree.get_commands():command.to_dict(self.core.bot.tree)
        self.i.user.id=7
        await self.core.bot.tree.get_command('plateswap').get_command('configure').callback(self.i,'A1',True,'A1',2)
        engine.plate_swap.configure.assert_not_called()
        self.assertFalse(self.i.response.send_message.call_args.kwargs['ephemeral'])
        self.i.user.id=42
        await self.core.bot.tree.get_command('plateswap').get_command('configure').callback(self.i,'A1',True,'A1',2)
        engine.plate_swap.configure.assert_not_called()
        self.assertFalse(self.i.followup.send.call_args.kwargs['ephemeral'])

    async def test_controls_permission_and_registration(self):
        from discord_Intergration.controls_discord import install
        controls=SimpleNamespace(apply=MagicMock())
        install(self.core,controls)
        for command in self.core.bot.tree.get_commands():command.to_dict(self.core.bot.tree)
        self.i.user.id=7
        await self.core.bot.tree.get_command('move').callback(self.i,'X',1,'A1')
        controls.apply.assert_not_called()
        self.assertFalse(self.i.response.send_message.call_args.kwargs['ephemeral'])
        self.i.user.id=42
        await self.core.bot.tree.get_command('temperature').callback(self.i,'bed',60,'A1')
        self.assertFalse(self.i.response.defer.call_args.kwargs['ephemeral'])
        self.assertFalse(self.i.followup.send.call_args.kwargs['ephemeral'])
        controls.apply.assert_not_called()  # preview is not execution

    async def test_public_replies_and_private_exceptions(self):
        self.core.settings['commands_channel_id']=456
        for name in ('help','rename','status','adminhelp'):
            self.i.command=self.core.bot.tree.get_command(name)
            self.assertFalse(self.core.ephemeral(self.i))
        for name in ('dm','publiccommands','assign report','meeting report assign'):
            self.i.command=SimpleNamespace(qualified_name=name)
            self.assertTrue(self.core.ephemeral(self.i))
        self.i.command=self.core.bot.tree.get_command('publiccommands')
        await self.i.command.callback(self.i,2)
        self.assertTrue(self.i.response.send_message.call_args.kwargs['ephemeral'])
        self.i.command=self.core.bot.tree.get_command('status')
        self.assertFalse(self.core.ephemeral(self.i))
        self.i.command=None;self.i.message=SimpleNamespace(flags=SimpleNamespace(ephemeral=True))
        self.assertTrue(self.core.ephemeral(self.i))

    async def test_dm_denies_unapproved(self):
        self.i.command=self.core.bot.tree.get_command('dm')
        self.i.user.id=7
        self.core.bot.fetch_user=AsyncMock()
        await self.core.bot.tree.get_command('dm').callback(self.i,'123456789012345','hi')
        self.core.bot.fetch_user.assert_not_awaited()
        self.assertTrue(self.i.response.send_message.call_args.kwargs['ephemeral'])
    async def test_dm_preview_then_send(self):
        recipient=SimpleNamespace(id=123456789012345,bot=False,send=AsyncMock())
        self.core.bot.fetch_user=AsyncMock(return_value=recipient)
        await self.core.bot.tree.get_command('dm').callback(self.i,str(recipient.id),'hello')
        recipient.send.assert_not_awaited()
        kw=self.i.followup.send.call_args.kwargs
        self.assertTrue(kw['ephemeral'])
        click=self.i;click.edit_original_response=AsyncMock()
        await kw['view'].children[0].callback(click)
        recipient.send.assert_awaited_once()
        await kw['view'].children[0].callback(click)
        recipient.send.assert_awaited_once()
    async def test_dm_rechecks_permission(self):
        recipient=SimpleNamespace(id=123456789012345,bot=False,send=AsyncMock())
        self.core.bot.fetch_user=AsyncMock(return_value=recipient)
        await self.core.bot.tree.get_command('dm').callback(self.i,str(recipient.id),'hello')
        view=self.i.followup.send.call_args.kwargs['view'];self.core.SETTINGS_USER_IDS.clear()
        await view.children[0].callback(self.i)
        recipient.send.assert_not_awaited()
    async def test_file_groups_registered(self):
        group=self.core.bot.tree.get_command('file')
        self.assertEqual({x.name for x in group.commands},{'list','system'})

    async def test_public_override_denies_unapproved(self):
        self.i.user.id=7
        await self.core.bot.tree.get_command('publiccommands').callback(self.i,2)
        self.assertEqual(self.core.public_channels,{})

    async def test_dm_blocked_stays_private(self):
        recipient=SimpleNamespace(id=123456789012345,bot=False,send=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),{'code':50007,'message':'Cannot send messages'})))
        self.core.bot.fetch_user=AsyncMock(return_value=recipient)
        await self.core.bot.tree.get_command('dm').callback(self.i,str(recipient.id),'private text')
        view=self.i.followup.send.call_args.kwargs['view']
        self.i.edit_original_response=AsyncMock()
        await view.children[0].callback(self.i)
        self.assertIn('refused',self.i.edit_original_response.call_args.kwargs['content'])
        self.store.event.assert_not_called()

    async def test_rename_preserves_identity_and_persists(self):
        name=self.core.names()[0]
        before=list(self.core.names())
        self.core.rename_printer(name,'Workshop One')
        self.assertEqual(self.core.names(),before)
        self.assertEqual(self.core.display_name(name),'Workshop One')
        self.assertEqual(self.core.resolve_name('workshop one'),(name,None))
        self.assertEqual(self.core.resolve_name(name),(name,None))
        stored=json.loads(Path(self.core.SETTINGS_FILE).read_text())
        self.assertEqual(stored['printer_names'][name],'Workshop One')

    async def test_rename_duplicates_and_invalid_names(self):
        first,second=self.core.names()[:2]
        for value in (second,'','a'*81,'abc\ndef'):
            with self.assertRaises(ValueError):self.core.rename_printer(first,value)
        self.core.rename_printer(second,'Other')
        with self.assertRaises(ValueError):self.core.rename_printer(first,'other')

    async def test_rename_preview_private_and_denial(self):
        name=self.core.names()[0]
        self.core.public_channels[(123,789)]=float('inf')
        await self.core.bot.tree.get_command('rename').callback(self.i,'New Name',name)
        self.assertFalse(self.i.response.defer.call_args.kwargs['ephemeral'])
        kw=self.i.followup.send.call_args.kwargs
        self.assertFalse(kw['ephemeral'])
        self.i.edit_original_response=AsyncMock()
        await kw['view'].children[0].callback(self.i)
        self.assertEqual(self.core.display_name(name),'New Name')
        self.i.user.id=7
        await self.core.bot.tree.get_command('rename').callback(self.i,'Unauthorized',name)
        self.assertEqual(self.core.display_name(name),'New Name')

    async def test_meeting_specific_assignment_private_during_override(self):
        from discord_Intergration.team_discord import install
        team=SimpleNamespace(assign=AsyncMock(return_value={'id':'abc','member':'88','status':'assigned_private'}))
        install(self.core,team)
        self.core.public_channels[(123,789)]=float('inf')
        command=self.core.bot.tree.get_command('meeting').get_command('report').get_command('assign')
        await command.callback(self.i,SimpleNamespace(id=88),False)
        self.assertEqual(team.assign.call_args.kwargs['member_id'],88)
        self.assertFalse(team.assign.call_args.kwargs['announce'])
        self.assertEqual(team.assign.call_args.kwargs['guild_id'],123)
        self.assertTrue(self.i.response.defer.call_args.kwargs['ephemeral'])
        self.assertTrue(self.i.followup.send.call_args.kwargs['ephemeral'])
        self.i.user.id=7
        team.assign.reset_mock()
        await command.callback(self.i,None,False)
        team.assign.assert_not_awaited()
