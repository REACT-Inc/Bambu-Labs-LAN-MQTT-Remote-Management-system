import asyncio,importlib.util,json,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from backupDiscordBot.backup_bot import Supervisor,BackupCore,LocalStore,MainLink,settings_for,problems,make_bot,changes_between,combine
from backupDiscordBot.sync_api import BackupSync,apply_changes

KEY='k'*32


def load_core(d,**config):
    (d/'config.json').write_text(json.dumps({'guild_ids':[123],'admin_user_ids':[42],'demo':True,**config}))
    with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
        spec=importlib.util.spec_from_file_location('backup_core',Path(__file__).parents[1]/'core.py')
        core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
    return core


class SettingsTests(unittest.TestCase):
    def test_settings_defaults_minimums_and_problems(self):
        self.assertEqual(settings_for({})['enabled'],False)
        s=settings_for({'backup_bot':{'enabled':True,'token':'t','failover_after':5,'failback_after':1,'main_url':'http://100.1.2.3:8080/','sync_key':KEY}})
        self.assertEqual((s['failover_after'],s['failback_after'],s['main_url']),(30,10,'http://100.1.2.3:8080'))
        self.assertEqual(problems(s,'main-token'),[])
        self.assertEqual(len(problems(s,'t')),1)                                   # same bot as the main one
        self.assertEqual(len(problems(settings_for({'backup_bot':{'enabled':True,'token':'t','sync_key':'short'}}))),2)

    def test_changes_and_combine(self):
        before={'notes':[{'id':'a','title':'x'},{'id':'b','title':'y'}],'attendance':[{'guild':'1','meeting':'m','member':'5','status':'attending'}]}
        after={'notes':[{'id':'a','title':'x2'},{'id':'c','title':'z'}],'attendance':[{'guild':'1','meeting':'m','member':'5','status':'attending'}]}
        changes=changes_between(before,after)
        self.assertEqual(changes,{'upserts':{'notes':[{'id':'a','title':'x2'},{'id':'c','title':'z'}]},'deletes':{'notes':[['b']]}})
        later={'upserts':{'notes':[{'id':'b','title':'again'}]},'deletes':{'notes':[['c']]},'events':[{'title':'e'}]}
        merged=combine(dict(changes,events=[]),later)
        self.assertEqual(sorted(r['id'] for r in merged['upserts']['notes']),['a','b'])
        self.assertEqual(merged['deletes'],{'notes':[['c']]});self.assertEqual(merged['events'],[{'title':'e'}])


class FakeLink:
    def __init__(self):self.up=True;self.ready=True;self.merged=[];self.accept=True;self.active_flags=[]
    async def state(self,active):
        self.active_flags.append(active)
        return {'discord_ready':self.ready,'guild_ids':[123],'admin_user_ids':[42],'settings':{},'team_config':{},'tables':{}} if self.up else None
    async def merge(self,changes,active):
        if self.accept:self.merged.append(changes)
        return self.accept


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1000.0;self.link=FakeLink()
        core=SimpleNamespace(settings={},ALLOWED_GUILD_IDS={123},SETTINGS_USER_IDS=set())
        self.sup=Supervisor(core,{'failover_after':90,'failback_after':30,'token':'x'},self.tmp.name,self.link,clock=lambda:self.now)
        self.events=[]
        async def activate():self.sup.active=True;self.events.append(('on',self.now))
        async def deactivate():self.sup.active=False;self.events.append(('off',self.now))
        self.sup.activate,self.sup.deactivate=activate,deactivate
    async def asyncTearDown(self):self.tmp.cleanup()
    async def advance(self,seconds,up=True,ready=True):
        self.link.up,self.link.ready=up,ready
        for _ in range(int(seconds//5)):
            self.now+=5;await self.sup.step()

    async def test_stays_off_while_main_is_healthy(self):
        await self.advance(300)
        self.assertEqual(self.events,[])
        self.assertTrue((Path(self.tmp.name)/'backup-snapshot.json').exists())      # keeps a copy for a take-over

    async def test_takes_over_after_failover_delay_and_hands_back(self):
        await self.advance(60)
        await self.advance(85,up=False)            # main Pi stops answering (stopped, frozen or unreachable)
        self.assertEqual(self.events,[])           # still inside the 90 s grace period
        await self.advance(60,up=False)
        self.assertEqual([e[0] for e in self.events],['on'])
        await self.advance(25)                     # main is back, but not for 30 s yet
        self.assertEqual([e[0] for e in self.events],['on'])
        self.assertTrue(self.link.active_flags[-1])   # tells the main Pi to hold its reminders meanwhile
        await self.advance(10)
        self.assertEqual([e[0] for e in self.events],['on','off'])

    async def test_main_discord_disconnected_counts_as_down(self):
        await self.advance(120,ready=False)
        self.assertEqual([e[0] for e in self.events],['on'])

    async def test_brief_outage_does_not_flap(self):
        await self.advance(30);await self.advance(40,up=False);await self.advance(200)
        self.assertEqual(self.events,[])

    async def test_unsent_changes_are_retried_and_protect_the_copy(self):
        self.sup.unmerged={'upserts':{'notes':[{'id':'n'}]},'deletes':{},'events':[]};self.link.accept=False
        await self.advance(10)
        self.assertTrue(all(self.link.active_flags))         # main keeps holding its scheduler
        self.assertIsNone(self.sup.snapshot)                  # the copy isn't replaced while changes wait
        self.link.accept=True;await self.advance(5)
        self.assertEqual(len(self.link.merged),1);self.assertIsNone(self.sup.unmerged);self.assertIsNotNone(self.sup.snapshot)


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """A real main-Pi app with the sync API, and a backup Supervisor talking to it over HTTP."""
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'main').mkdir();(d/'backup').mkdir()
        self.core=load_core(d/'main',backup_sync={'key':KEY})
        from queueing import Store
        from ftcTeamManagement.team import Team
        self.store=Store(d/'main'/'management.sqlite3')
        app=web.Application()
        self.team=Team(self.core,self.store,SimpleNamespace(app=app))
        self.sync=BackupSync(self.core,self.team);self.sync.install(app)
        self.team.remember(123,42,'Robot','Main note')
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.link=MainLink(str(self.client.make_url('')),KEY,timeout=5)
    async def asyncTearDown(self):
        await self.link.close();await self.client.close();await self.core.bot.close()
        self.store.db.close();self.tmp.cleanup()

    async def test_key_is_required(self):
        self.assertEqual((await self.client.get('/backup-sync/state')).status,403)
        self.assertEqual((await self.client.get('/backup-sync/state',headers={'X-Backup-Key':'wrong'})).status,403)
        self.core.CONFIG['backup_sync']={'key':'too-short'}
        self.assertEqual((await self.client.get('/backup-sync/state',headers={'X-Backup-Key':'too-short'})).status,404)

    async def test_bad_merge_is_rejected(self):
        with self.assertRaises(ValueError):apply_changes(self.team.db,{'upserts':{'jobs':[{'id':'x'}]}})
        with self.assertRaises(ValueError):apply_changes(self.team.db,{'upserts':{'notes':[{'title':'no id'}]}})

    async def test_state_holds_the_main_scheduler(self):
        self.assertTrue(self.team.hold())                     # just started: waits to hear from the backup
        state=await self.link.state(False)
        self.assertEqual(state['tables']['notes'][0]['title'],'Robot');self.assertEqual(state['guild_ids'],[123])
        self.assertFalse(self.team.hold())
        await self.link.state(True);self.assertTrue(self.team.hold())
        await self.link.state(False);self.assertFalse(self.team.hold())

    async def test_take_over_changes_are_merged_back(self):
        started=asyncio.Event()
        def factory(core):
            bot=make_bot(core)
            async def start(token):
                self.assertEqual(token,'backup-token');started.set()
                while not bot.is_closed():await asyncio.sleep(0.02)
            bot.start=start;return bot
        now=[0.0];backup_dir=Path(self.tmp.name)/'backup'
        sup=Supervisor(self.core,{'failover_after':90,'failback_after':30,'token':'backup-token'},backup_dir,self.link,clock=lambda:now[0],bot_factory=factory)
        self.core.bot.is_ready=lambda:False                   # the main bot is disconnected
        await sup.step();now[0]=95;await sup.step();await asyncio.wait_for(started.wait(),2)
        self.assertTrue(sup.active)
        now[0]=96;await sup.step();self.assertTrue(self.team.hold())   # main holds its reminders while the backup is active
        self.assertEqual([c.name for c in sup.bot.tree.get_commands()],['ftcteam'])   # no printer commands
        self.assertIn('Backup bot',BackupCore(self.core,sup.bot).card('x').footer.text)
        self.assertFalse(sup.tasks[1].done())                 # reminder/assignment scheduler running
        # Changes made through /ftcteam while the main Pi is down go to the backup's own copy.
        self.assertEqual([n['title'] for n in sup.team.notes()],['Robot'])
        sup.team.remember(123,7,'Backup','From the backup')
        meeting=sup.team.set_attendance(123,7,'not_attending')
        note_id=[n['id'] for n in sup.team.notes() if n['title']=='Robot'][0]
        with sup.team.db:sup.team.db.execute('DELETE FROM notes WHERE id=?',(note_id,))
        self.assertEqual(len(self.team.notes()),1)            # the main Pi hasn't seen them yet
        self.core.bot.is_ready=lambda:True
        now[0]=100;await sup.step();now[0]=131;await sup.step()
        self.assertFalse(sup.active);self.assertIsNone(sup.unmerged)
        self.assertEqual([n['title'] for n in self.team.notes()],['Backup'])
        self.assertEqual(self.team.absent('123',meeting),{'7'})
        titles=[e['title'] for e in self.store.events()]
        self.assertIn('Backup bot started',titles);self.assertIn('Backup bot stopped',titles)
        self.assertFalse(self.team.hold())
        now[0]=136;await sup.step()                           # standby again: the copy follows the main Pi
        self.assertEqual([n['title'] for n in sup.snapshot['tables']['notes']],['Backup'])

    async def test_changes_survive_when_the_main_pi_refuses_them(self):
        backup_dir=Path(self.tmp.name)/'backup'
        (backup_dir/'backup-unmerged.json').write_text(json.dumps({'upserts':{'notes':[{'id':'z','guild':'123','owner':'1','title':'Late','body':'b','created':1.0}]},'deletes':{},'events':[]}))
        self.link.key='x'*32
        sup=Supervisor(self.core,{'failover_after':90,'failback_after':30,'token':'t'},backup_dir,self.link,clock=lambda:0)
        await sup.step();self.assertIsNotNone(sup.unmerged)
        self.link.key=KEY;await sup.step()
        self.assertIsNone(sup.unmerged);self.assertFalse((backup_dir/'backup-unmerged.json').exists())
        self.assertIn('Late',[n['title'] for n in self.team.notes()])
        sup.unmerged={'upserts':{'jobs':[{'id':'x'}]},'deletes':{},'events':[]}   # can never be merged
        await sup.step()
        self.assertIsNone(sup.unmerged);self.assertEqual(len(list(backup_dir.glob('backup-rejected-*.json'))),1)

    async def test_failed_login_stands_down_and_retries_later(self):
        attempts=[]
        def factory(core):
            bot=make_bot(core)
            async def start(token):attempts.append(token);raise RuntimeError('bad token')
            bot.start=start;return bot
        now=[0.0];self.link.url='http://127.0.0.1:1'           # main Pi unreachable
        sup=Supervisor(self.core,{'failover_after':90,'failback_after':30,'token':'t'},Path(self.tmp.name)/'backup',self.link,clock=lambda:now[0],bot_factory=factory)
        now[0]=95;await sup.step();await asyncio.sleep(0.05)
        self.assertTrue(sup.active);self.assertEqual(len(attempts),1)
        now[0]=100;await sup.step()                   # login failed: stand down
        self.assertFalse(sup.active)
        now[0]=150;await sup.step();self.assertEqual(len(attempts),1)   # waits failover_after before retrying
        now[0]=195;await sup.step();await asyncio.sleep(0.05);self.assertEqual(len(attempts),2)
        await sup.deactivate()

    def test_local_store_starts_fresh(self):
        path=Path(self.tmp.name)/'backup'/'backup.sqlite3'
        s=LocalStore(path);s.event(None,'x','y');s.db.close()
        s=LocalStore(path);self.assertEqual(s.events(),[]);s.db.close()


if __name__=='__main__':
    unittest.main()
