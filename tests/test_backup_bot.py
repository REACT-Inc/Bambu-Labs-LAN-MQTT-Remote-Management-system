import asyncio,importlib.util,json,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import discord
from backupDiscordBot import heartbeat
from backupDiscordBot.backup_bot import Supervisor,BackupCore,SharedStore,settings_for,make_bot


class HeartbeatTests(unittest.TestCase):
    def test_write_read_and_health(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(heartbeat.read(d));self.assertFalse(heartbeat.main_is_healthy(None))
            heartbeat.write(d,True,'rel1');beat=heartbeat.read(d)
            self.assertEqual((beat['discord_ready'],beat['release']),(True,'rel1'))
            self.assertTrue(heartbeat.main_is_healthy(beat))
            self.assertFalse(heartbeat.main_is_healthy(beat,now=beat['time']+60))            # stale: stopped or frozen
            heartbeat.write(d,False);self.assertFalse(heartbeat.main_is_healthy(heartbeat.read(d)))  # Discord disconnected
            (Path(d)/heartbeat.NAME).write_text('garbage');self.assertIsNone(heartbeat.read(d))

    def test_settings_defaults_and_minimums(self):
        self.assertEqual(settings_for({})['enabled'],False)
        s=settings_for({'backup_bot':{'enabled':True,'token':'t','failover_after':5,'failback_after':1}})
        self.assertEqual((s['failover_after'],s['failback_after']),(30,10))


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1000.0
        self.sup=Supervisor(SimpleNamespace(),{'failover_after':90,'failback_after':30,'token':'x'},self.tmp.name,clock=lambda:self.now)
        self.events=[]
        async def activate():self.sup.active=True;self.events.append(('on',self.now))
        async def deactivate():self.sup.active=False;self.events.append(('off',self.now))
        self.sup.activate,self.sup.deactivate=activate,deactivate
    async def asyncTearDown(self):self.tmp.cleanup()
    def beat(self,ready=True):
        with patch('backupDiscordBot.heartbeat.time.time',return_value=self.now):heartbeat.write(self.tmp.name,ready)
    async def advance(self,seconds,beat=None):
        for _ in range(int(seconds//5)):
            self.now+=5
            if beat is not None:self.beat(beat)
            await self.sup.step()

    async def test_stays_off_while_main_is_healthy(self):
        await self.advance(300,beat=True)
        self.assertEqual(self.events,[])

    async def test_takes_over_after_failover_delay_and_hands_back(self):
        self.beat();await self.advance(60,beat=True)
        await self.advance(85)                     # heartbeat stops (main stopped or frozen)
        self.assertEqual(self.events,[])           # still inside the 90 s grace period
        await self.advance(60)
        self.assertEqual([e[0] for e in self.events],['on'])
        await self.advance(25,beat=True)           # main is back, but not for 30 s yet
        self.assertEqual([e[0] for e in self.events],['on'])
        await self.advance(10,beat=True)
        self.assertEqual([e[0] for e in self.events],['on','off'])

    async def test_main_discord_disconnected_counts_as_down(self):
        await self.advance(120,beat=False)
        self.assertEqual([e[0] for e in self.events],['on'])

    async def test_brief_outage_does_not_flap(self):
        await self.advance(30,beat=True);await self.advance(40);await self.advance(200,beat=True)
        self.assertEqual(self.events,[])


class BackupBotCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'guild_ids':[123],'admin_user_ids':[42],'demo':True}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('backup_core',Path(__file__).parents[1]/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
        from queueing import Store
        Store(d/'management.sqlite3').db.close()   # the main service created the database
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()

    async def test_only_team_commands_and_backup_footer(self):
        from aiohttp import web
        from ftcTeamManagement.team import Team
        from ftcTeamManagement.team_discord import install
        bot=make_bot(self.core);store=SharedStore(Path(self.tmp.name)/'management.sqlite3')
        try:
            proxy=BackupCore(self.core,bot)
            team=Team(proxy,store,SimpleNamespace(app=web.Application()));install(proxy,team)
            self.assertEqual([c.name for c in bot.tree.get_commands()],['ftcteam'])   # no printer commands
            bot.tree.get_commands()[0].to_dict(bot.tree)
            self.assertIn('Backup bot',proxy.card('x').footer.text)
            self.assertIs(team.core.bot,bot)
            store.event(None,'Backup bot started','test')
            self.assertEqual(store.db.execute("SELECT title FROM events").fetchone()[0],'Backup bot started')
        finally:
            store.db.close();await bot.close()

    async def test_activate_and_deactivate_for_real(self):
        started=asyncio.Event();bots=[]
        def factory(core):
            bot=make_bot(core)
            async def start(token):
                self.assertEqual(token,'backup-token');started.set();await asyncio.Event().wait()
            bot.start=start;bots.append(bot);return bot
        sup=Supervisor(self.core,{'failover_after':90,'failback_after':30,'token':'backup-token'},self.tmp.name,bot_factory=factory)
        await sup.activate();await asyncio.wait_for(started.wait(),2)
        self.assertTrue(sup.active);self.assertEqual([c.name for c in bots[0].tree.get_commands()],['ftcteam'])
        self.assertFalse(sup.tasks[1].done())      # reminder/assignment scheduler running
        await sup.deactivate()
        self.assertFalse(sup.active);self.assertTrue(bots[0].is_closed())
        from queueing import Store
        store=Store(Path(self.tmp.name)/'management.sqlite3')
        self.assertEqual([e['title'] for e in store.events()][:2],['Backup bot stopped','Backup bot started'])
        store.db.close()

    async def test_failed_login_stands_down_and_retries_later(self):
        attempts=[]
        def factory(core):
            bot=make_bot(core)
            async def start(token):attempts.append(token);raise RuntimeError('bad token')
            bot.start=start;return bot
        now=[0.0]
        sup=Supervisor(self.core,{'failover_after':90,'failback_after':30,'token':'t'},self.tmp.name,clock=lambda:now[0],bot_factory=factory)
        now[0]=95;await sup.step();await asyncio.sleep(0.05)
        self.assertTrue(sup.active);self.assertEqual(len(attempts),1)
        now[0]=100;await sup.step()                   # login failed: stand down
        self.assertFalse(sup.active)
        now[0]=150;await sup.step();self.assertEqual(len(attempts),1)   # waits failover_after before retrying
        now[0]=195;await sup.step();await asyncio.sleep(0.05);self.assertEqual(len(attempts),2)
        await sup.deactivate()

    def test_shared_store_never_touches_queue_jobs(self):
        from queueing import Store
        path=Path(self.tmp.name)/'management.sqlite3';main=Store(path)
        main.db.execute("INSERT INTO jobs(id,printer,label,remote,options,status,position,author,created,updated) VALUES('j','A1','x','r','{}','printing',0,'a',0,0)");main.db.commit()
        SharedStore(path).db.close()
        self.assertEqual(main.db.execute("SELECT status FROM jobs WHERE id='j'").fetchone()[0],'printing')
        main.db.close()


if __name__=='__main__':
    unittest.main()
