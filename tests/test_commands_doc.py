import importlib,importlib.util,json,os,re,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import discord

ROOT=Path(__file__).parents[1]


def documented():
    """{command: 'Everyone'|'Admin'} from the command tables in docs/commands.md."""
    level,result=None,{}
    for line in (ROOT/'docs'/'commands.md').read_text().splitlines():
        if line.startswith('## '):level={'## Everyone':'Everyone','## Admin':'Admin'}.get(line.strip())
        match=re.match(r'\|\s*`/([a-z ]+)`\s*\|',line)
        if match and level:result[match[1]]=level
    return result


class CommandsDocTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'guild_ids':[123],'admin_user_ids':[42],'demo':True}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('test_core_commands_doc',ROOT/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()

    def registered(self):
        from queueing import Store,Engine
        from dashboard import Dashboard
        from team import Team
        from discord_Intergration.discord_queue import install as install_queue
        from discord_Intergration.extra_discord import install as install_extras
        from discord_Intergration.team_discord import install as install_team
        from discord_Intergration.controls_discord import install as install_controls
        from swapMod.plate_swap_discord import install as install_swap
        store=Store(Path(self.tmp.name)/'doc.sqlite');self.store=store
        engine=Engine(self.core,store);dashboard=Dashboard(self.core,store,engine)
        # Same installers as main.py.
        install_queue(self.core,store,engine,dashboard);install_team(self.core,Team(self.core,store,dashboard))
        install_extras(self.core,store);install_controls(self.core,dashboard.controls);install_swap(self.core,engine)
        for extra in ('diagnostics','issue_reports'):
            if (ROOT/(extra+'.py')).exists():
                module=importlib.import_module(extra)
                if extra=='diagnostics':module.install_discord(self.core,store)
                else:module.install_discord(self.core,module.IssueReports(self.core,store))
        result={}
        def walk(command,root=None):
            root=root or command.name
            if isinstance(command,discord.app_commands.Group):
                for child in command.commands:walk(child,root)
            else:result[command.qualified_name]='Admin' if root in self.core.ADMIN_COMMANDS else 'Everyone'
        for command in self.core.bot.tree.get_commands():walk(command)
        return result

    async def test_every_command_documented_with_its_permission_level(self):
        try:actual=self.registered()
        finally:getattr(self,'store',None) and self.store.db.close()
        docs=documented()
        self.assertGreater(len(actual),40)
        self.assertEqual(sorted(set(actual)-set(docs)),[],'Commands missing from docs/commands.md')
        self.assertEqual(sorted(set(docs)-set(actual)),[],'docs/commands.md lists commands that no longer exist')
        self.assertEqual({k:v for k,v in docs.items() if actual[k]!=v},{},'Permission level in docs/commands.md is wrong')


if __name__=='__main__':
    unittest.main()
