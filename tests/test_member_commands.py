"""Printer actions and queue commands open to every member: confirmation first, and who ran it is logged."""
import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
import discord


def interaction(user_id=7):
    i=MagicMock(spec=discord.Interaction)
    i.guild_id=123;i.channel_id=789;i.guild=SimpleNamespace(name='Team')
    i.user=SimpleNamespace(id=user_id,display_name='Alex')  # a regular member, not an administrator
    i.command=None;i.message=None;i.data={}
    i.response=SimpleNamespace(is_done=lambda:False,defer=AsyncMock(),send_message=AsyncMock(),edit_message=AsyncMock())
    i.followup=SimpleNamespace(send=AsyncMock())
    i.original_response=AsyncMock();i.edit_original_response=AsyncMock()
    return i


class MemberCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'guild_ids':[123],'admin_user_ids':[42],'demo':True}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('test_core_members',Path(__file__).parents[1]/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
        self.printers=self.core.names()
        self.engine=SimpleNamespace(control=AsyncMock(),start=AsyncMock(),resolve=MagicMock())
        self.store=SimpleNamespace(jobs=lambda name=None:[],get=MagicMock(return_value={'id':'job1','label':'Bracket','printer':self.printers[0]}),edit=MagicMock())
        from discord_Intergration.discord_queue import install
        install(self.core,self.store,self.engine,SimpleNamespace(copy_job=MagicMock(),add=MagicMock()))
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()

    def sent(self,i):
        call=i.response.send_message.call_args or i.followup.send.call_args
        return call.kwargs

    async def test_member_passes_permission_check_for_open_commands(self):
        for name in ('pause','resume','stop','lighton','lightoff','reprint','queuestart','queueforce','queuemanage'):
            i=interaction();i.command=self.core.bot.tree.get_command(name)
            self.assertTrue(await self.core.bot.tree.interaction_check(i),name)
        for name in ('temperature','move','dm','reboot'):
            i=interaction();i.command=self.core.bot.tree.get_command(name) or SimpleNamespace(qualified_name=name)
            self.assertFalse(await self.core.bot.tree.interaction_check(i),name)

    async def test_pause_resume_stop_and_lights_confirm_then_log_who(self):
        printer=self.printers[0]
        for action in ('pause','resume','stop','lighton','lightoff'):
            self.engine.control.reset_mock()
            i=interaction()
            await self.core.bot.tree.get_command(action).callback(i,printer)
            self.engine.control.assert_not_awaited()  # nothing happens before confirming
            kw=self.sent(i);self.assertIn('Confirm',kw['embed'].title)
            await kw['view'].children[1].callback(interaction())  # Cancel does nothing
            self.engine.control.assert_not_awaited()
            i=interaction();await self.core.bot.tree.get_command(action).callback(i,printer)
            await self.sent(i)['view'].children[0].callback(interaction())
            self.engine.control.assert_awaited_once_with(printer,action,'Discord Alex (7)')

    async def test_queuemanage_by_member_confirms_and_records_author(self):
        i=interaction()
        await self.core.bot.tree.get_command('queuemanage').callback(i,'job1','remove')
        self.store.edit.assert_not_called()
        await self.sent(i)['view'].children[0].callback(interaction())
        self.store.edit.assert_called_once_with('job1','remove','Discord Alex (7)')
        self.store.get.side_effect=ValueError('Job not found.')
        i=interaction();await self.core.bot.tree.get_command('queuemanage').callback(i,'missing','remove')
        self.assertIn('not found',self.sent(i)['embed'].title)

    async def test_fanall_sets_every_printer_and_continues_past_failures(self):
        from discord_Intergration.controls_discord import install
        def apply(printer,kind,value,axis=None,**kw):
            if printer==self.printers[0]:raise ValueError('Printer is offline.')
            return f'All fans → {value}% • Submitted'
        controls=SimpleNamespace(apply=MagicMock(side_effect=apply));install(self.core,controls)
        i=interaction()
        await self.core.bot.tree.get_command('fanall').callback(i,60)
        controls.apply.assert_not_called()
        kw=self.sent(i);self.assertIn(str(len(self.printers)),kw['embed'].description)
        click=interaction();await kw['view'].children[0].callback(click)
        self.assertEqual([c.args[0] for c in controls.apply.call_args_list],self.printers)
        for c in controls.apply.call_args_list:
            self.assertEqual((c.args[1],c.args[2],c.kwargs['confirmed'],c.kwargs['author']),('fanall',60,True,'Discord Alex (7)'))
        result=click.edit_original_response.call_args.kwargs['embed'].description
        self.assertIn('Printer is offline',result);self.assertIn('✅',result)

    async def test_fan_and_speed_open_but_temperature_stays_admin(self):
        from discord_Intergration.controls_discord import install
        controls=SimpleNamespace(apply=MagicMock(return_value='Speed → sport • Submitted'));install(self.core,controls)
        i=interaction();await self.core.bot.tree.get_command('speed').callback(i,'sport',self.printers[0])
        view=i.followup.send.call_args.kwargs['view'];controls.apply.assert_not_called()
        await view.children[0].callback(interaction())
        self.assertEqual(controls.apply.call_args.kwargs['author'],'Discord Alex (7)')
        controls.apply.reset_mock();i=interaction()
        await self.core.bot.tree.get_command('temperature').callback(i,'bed',60,self.printers[0])
        self.assertIn('Administrators',i.response.send_message.call_args.args[0]);controls.apply.assert_not_called()

    async def test_every_command_is_logged_without_free_text(self):
        i=interaction();i.command=self.core.bot.tree.get_command('queuemanage')
        i.data={'name':'queuemanage','options':[{'name':'job_id','type':3,'value':'job1'},{'name':'action','type':3,'value':'remove'}]}
        with self.assertLogs('printer-bot','INFO') as logs:await self.core.bot.tree.interaction_check(i)
        self.assertIn('Discord Alex (7) ran /queuemanage job_id=job1 action=remove',logs.output[0])
        i=interaction();i.command=SimpleNamespace(qualified_name='dm')
        i.data={'name':'dm','options':[{'name':'user','type':3,'value':'555'},{'name':'message','type':3,'value':'top secret'}]}
        with self.assertLogs('printer-bot','WARNING') as logs:await self.core.bot.tree.interaction_check(i)
        self.assertIn('Denied /dm user=555 message=[text]',logs.output[0]);self.assertNotIn('top secret',logs.output[0])

    async def test_dashboard_overrides_change_who_can_run_commands(self):
        tree=self.core.bot.tree
        async def allowed(name,i=None):
            i=i or interaction();i.command=tree.get_command(name);return await tree.interaction_check(i)
        self.core.settings['command_permissions']={'pause':'admin','temperature':'everyone','stop':'disabled','queuestart':'role'}
        self.assertFalse(await allowed('pause'));self.assertTrue(await allowed('temperature'))
        admin=interaction(42);admin.command=tree.get_command('stop')
        self.assertFalse(await tree.interaction_check(admin))  # "off" applies to admins too
        self.assertIn('turned off',admin.response.send_message.call_args.args[0])
        self.assertFalse(await allowed('queuestart'))
        self.core.settings['member_role_ids']=[555]
        member=interaction();member.user.roles=[SimpleNamespace(id=555)]
        self.assertTrue(await allowed('queuestart',member));self.assertTrue(await allowed('queuestart',interaction(42)))
        # Buttons and inner checks follow the same setting: an opened /temperature reaches its confirmation.
        from discord_Intergration.controls_discord import install
        controls=SimpleNamespace(apply=MagicMock(return_value='Bed → 60 °C • Submitted'));install(self.core,controls)
        i=interaction();await tree.get_command('temperature').callback(i,'bed',60,self.printers[0])
        await i.followup.send.call_args.kwargs['view'].children[0].callback(interaction())
        self.assertEqual(controls.apply.call_args.kwargs['author'],'Discord Alex (7)')
        i=interaction();await tree.get_command('temperature').callback(i,'chamber',50,self.printers[0])
        self.assertIn('Administrators',i.response.send_message.call_args.args[0])  # /temperature chamber follows /chamber

    async def test_locked_commands_never_open_and_help_hides_off_commands(self):
        self.core.settings['command_permissions']={'reboot':'everyone','dm':'role','laptop':'disabled','ftc':'disabled'}
        self.assertEqual((self.core.command_level('reboot'),self.core.command_level('dm'),self.core.command_level('laptop')),('admin','admin','disabled'))
        self.assertFalse(self.core.command_allowed(interaction(),'reboot'))
        everyday=' '.join(f.value for f in self.core.help_embed().fields)
        self.assertNotIn('/ftc',everyday);self.assertIn('/pause',everyday)

    async def test_dashboard_permission_api(self):
        from dashboard import Dashboard
        from queueing import Store,Engine
        store=Store(Path(self.tmp.name)/'perm.sqlite')
        try:
            dash=Dashboard(self.core,store,Engine(self.core,store))
            from discord_Intergration.controls_discord import install
            from swapMod.plate_swap_discord import install as install_swap
            from discord_Intergration.team_discord import install as install_team
            from team import Team
            install(self.core,dash.controls);install_swap(self.core,SimpleNamespace(plate_swap=MagicMock()));install_team(self.core,Team(self.core,store,dash))
            class Request:
                def __init__(self,data):self.data=data
                async def json(self):return self.data
            state=dash.permission_state();rows={r['command']:r for r in state['commands']}
            self.assertEqual((rows['pause']['level'],rows['move']['level'],rows['reboot']['locked']),('everyone','admin',True))
            self.assertIn('plateswap now',rows['plateswap']['subcommands'])
            for bad in [{'levels':{'nope':'admin'}},{'levels':{'pause':'sometimes'}},{'levels':{'reboot':'everyone'}},{'levels':{},'member_role_ids':'-5'}]:
                with self.assertRaises(ValueError):await dash.save_permissions(Request(bad))
            response=await dash.save_permissions(Request({'levels':{'move':'role','pause':'everyone','fan':'admin'},'member_role_ids':'555\n777'}))
            saved=json.loads(Path(self.core.SETTINGS_FILE).read_text())
            self.assertEqual(saved['command_permissions'],{'move':'role','fan':'admin'})  # defaults are not stored
            self.assertEqual(saved['member_role_ids'],[555,777])
            events=store.events();self.assertEqual(events[0]['title'],'Discord permissions changed')
            self.assertIn('/move: Admins only → Allowed roles + admins',events[0]['detail']);self.assertIn('web administrator',events[0]['detail'])
            await dash.save_permissions(Request({'levels':{'move':'admin','fan':'everyone'},'member_role_ids':'555\n777'}))
            self.assertEqual(json.loads(Path(self.core.SETTINGS_FILE).read_text())['command_permissions'],{})
        finally:store.db.close()


if __name__=='__main__':
    unittest.main()
