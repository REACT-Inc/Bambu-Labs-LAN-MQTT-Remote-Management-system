"""Dashboard first, Discord optional (#8): printers and the Discord bot are set up in the dashboard, a new installation
needs neither the old bot nor file editing, and Restart now applies the changes."""
import asyncio,importlib.util,io,json,os,signal,stat,sys,tempfile,unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT))
import printer_setup
from printer_setup import Setup
from queueing import Store,options

TOKEN='M'+'T'*23+'.'+'G'*6+'.'+'x'*38   # shaped like a Discord bot token (built here, not a real one)
MINI=dict(name='Mini 1',ip='192.168.1.41',serial='030ABC123456789',access_code='S3cr3tAC')


def load_main(data):
    """main.py as the service runs it (it imports core, which reads config.json)."""
    (data/'config.json').write_text('{}')
    with patch.dict(os.environ,{'PM_CONFIG':str(data/'config.json'),'PM_DATA':str(data)}):
        import main
    return main


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)
        self.core=SimpleNamespace(DATA_DIR=self.data,CONFIG={},bot=SimpleNamespace(is_ready=lambda:False),restart_requested=False)
        self.store=Store(self.data/'db');self.setup=Setup(self.core,self.store)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def saved(self,name):return json.loads((self.data/name).read_text())


class LoadingTests(Base):
    def test_dashboard_printers_join_config_printers_which_win(self):
        (self.data/'printers.json').write_text(json.dumps([dict(MINI),dict(MINI,name='andrew',ip='10.0.0.9')]))
        config={'printers':[{'name':'Andrew','ip':'192.168.1.40','serial':'094X','access_code':'x'}]}
        printers=printer_setup.merged_printers(config,self.data)
        self.assertEqual([(p['name'],p.get('managed',False)) for p in printers],[('Andrew',False),('Mini 1',True)])

    def test_discord_from_the_dashboard_unless_config_sets_it(self):
        (self.data/'discord.json').write_text(json.dumps({'token':TOKEN,'guild_ids':[111]}))
        self.assertEqual(printer_setup.discord_settings({},self.data),(TOKEN,{111}))
        self.assertEqual(printer_setup.discord_settings({'discord_token':'cfg','guild_ids':[222]},self.data),('cfg',{222}))
        self.assertEqual(printer_setup.discord_settings({},self.data/'nowhere'),('',set()))   # nothing set up: no Discord

    def test_the_app_uses_them(self):
        (self.data/'printers.json').write_text(json.dumps([MINI]));(self.data/'discord.json').write_text(json.dumps({'token':TOKEN,'guild_ids':[111]}))
        (self.data/'config.json').write_text(json.dumps({}))
        with patch.dict(os.environ,{'PM_CONFIG':str(self.data/'config.json'),'PM_DATA':str(self.data)}):
            spec=importlib.util.spec_from_file_location('core_setup_test',ROOT/'core.py')
            core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
        try:
            self.assertEqual(core.names(),['Mini 1']);self.assertFalse(core.EXAMPLE_MODE)   # live, no config.json printers needed
            self.assertEqual((core.DISCORD_BOT_TOKEN,core.ALLOWED_GUILD_IDS),(TOKEN,{111}))
        finally:asyncio.run(core.bot.close())


class PrinterTests(Base):
    def test_add_a_printer_with_its_model_and_camera_worked_out(self):
        state=self.setup.save_printer(dict(MINI,model='',camera='auto'),'web')
        self.assertEqual(self.saved('printers.json'),[dict(MINI,serial='030ABC123456789',camera_type='jpeg_tcp')])   # A1 mini from 030
        row=state['printers'][0]
        self.assertEqual((row['model_label'],row['camera'],row['source'],row['has_access_code']),('A1 mini','jpeg_tcp','dashboard',True))
        self.assertNotIn('S3cr3tAC',json.dumps(state))   # the access code never goes back to the browser
        self.assertEqual(stat.S_IMODE((self.data/'printers.json').stat().st_mode),0o600)
        self.assertTrue(state['pending_restart'])        # connected after a restart
        self.assertFalse(state['pending_discord'])       # (the Discord section has nothing to apply)
        self.setup.save_printer(dict(MINI,name='Lab H2D',serial='094ABC1234567',model='h2d',camera='auto'),'web')
        self.assertEqual(self.saved('printers.json')[1]['model'],'H2D');self.assertEqual(self.saved('printers.json')[1]['camera_type'],'rtsp')
        self.setup.save_printer(dict(MINI,name='Garage',serial='ZZZ12345678',camera='auto'),'web')
        self.assertNotIn('camera_type',self.saved('printers.json')[2])   # unknown model: no camera unless chosen

    def test_what_is_refused(self):
        save=lambda **changes:self.setup.save_printer(dict(MINI,**changes),'web')
        for changes,message in (({'name':''},'name'),({'ip':'not an ip!'},'IP address'),({'serial':'12'},'serial'),
                                ({'access_code':''},'access code'),({'model':'x9'},'model'),({'camera':'webcam'},'camera')):
            with self.assertRaisesRegex(ValueError,message):save(**changes)
        save()
        with self.assertRaisesRegex(ValueError,'already a printer called Mini 1'):save(name='MINI 1')   # case-insensitive
        self.core.CONFIG={'printers':[{'name':'Andrew'}]}
        with self.assertRaisesRegex(ValueError,'config.json'):save(name='andrew')
        with self.assertRaisesRegex(ValueError,'Unknown printer'):save(name='Nope',edit=True)   # no renaming
        self.assertEqual(len(self.saved('printers.json')),1)

    def test_edit_keeps_the_access_code_when_left_blank(self):
        self.setup.save_printer(dict(MINI),'web')
        self.setup.save_printer(dict(MINI,ip='192.168.1.99',access_code='',edit=True),'web')
        saved=self.saved('printers.json')[0]
        self.assertEqual((saved['ip'],saved['access_code']),('192.168.1.99','S3cr3tAC'))
        self.assertEqual([e['title'] for e in self.store.events()][:2],['Printer changed','Printer added'])

    def test_config_printers_are_listed_but_not_changed_here(self):
        self.core.CONFIG={'printers':[{'name':'Andrew','ip':'192.168.1.40','serial':'094X','access_code':'secret','camera_type':'rtsp'}]}
        row=self.setup.state()['printers'][0]
        self.assertEqual((row['source'],row['model_label']),('config','H2D'));self.assertNotIn('secret',json.dumps(self.setup.state()))
        with self.assertRaisesRegex(ValueError,'config.json'):self.setup.remove_printer('Andrew','web')

    def test_remove_only_without_queue_jobs(self):
        self.setup.save_printer(dict(MINI),'web')
        job=self.store.add('Mini 1','Bracket',None,'x.3mf',options(),'t')
        with self.assertRaisesRegex(ValueError,'1 queue job'):self.setup.remove_printer('Mini 1','web')
        self.store.set_status(job['id'],'finished')
        self.assertEqual(self.setup.remove_printer('Mini 1','web')['printers'],[])
        self.assertEqual(self.saved('printers.json'),[])


class DiscordTests(Base):
    def test_connect_keep_and_remove_the_bot(self):
        with self.assertRaisesRegex(ValueError,'bot token'):self.setup.save_discord({'token':'nope','guild_ids':'1'},'web')
        with self.assertRaisesRegex(ValueError,'server'):self.setup.save_discord({'token':TOKEN},'web')
        with self.assertRaisesRegex(ValueError,'server IDs'):self.setup.save_discord({'token':TOKEN,'guild_ids':'abc'},'web')
        state=self.setup.save_discord({'token':TOKEN,'guild_ids':'111\n222'},'web')
        self.assertEqual(self.saved('discord.json'),{'token':TOKEN,'guild_ids':[111,222]})
        self.assertNotIn(TOKEN,json.dumps(state))   # write-only
        self.assertTrue(state['pending_discord'])
        self.assertEqual((state['discord']['has_token'],state['discord']['guild_ids']),(True,['111','222']))
        self.setup.save_discord({'token':'','guild_ids':'333'},'web')   # blank keeps the token
        self.assertEqual(self.saved('discord.json'),{'token':TOKEN,'guild_ids':[333]})
        self.setup.save_discord({'clear_token':True,'guild_ids':''},'web')
        self.assertEqual(self.saved('discord.json')['token'],'')
        self.assertEqual(stat.S_IMODE((self.data/'discord.json').stat().st_mode),0o600)


class RestartTests(Base):
    def test_restart_only_under_systemd_and_then_it_exits_to_be_started_again(self):
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('INVOCATION_ID',None)
            self.assertFalse(self.setup.state()['can_restart'])
            with self.assertRaisesRegex(ValueError,'not run by systemd'):self.setup.restart('web')
        kill=MagicMock();self.setup.kill=kill
        async def restart():
            with patch.dict(os.environ,{'INVOCATION_ID':'abc'}):self.setup.restart('web')
            await asyncio.sleep(0.6)
        asyncio.run(restart())
        self.assertTrue(self.core.restart_requested);kill.assert_called_once_with(os.getpid(),signal.SIGTERM)
        main=load_main(self.data)
        with patch.object(main.core,'restart_requested',True):self.assertEqual(main.exit_code(),printer_setup.RESTART_EXIT)
        with patch.object(main.core,'restart_requested',False):self.assertEqual(main.exit_code(),0)


class FreshInstallTests(unittest.TestCase):
    def test_a_new_installation_needs_no_old_bot_and_no_demo(self):
        import configure
        with tempfile.TemporaryDirectory() as d:
            out=io.StringIO()
            with redirect_stdout(out):configure.migrate(Path(d)/'missing-old-bot.py',Path(d)/'etc'/'config.json',Path(d)/'data','100.1.2.3')
            config=json.loads((Path(d)/'etc'/'config.json').read_text())
            self.assertEqual((config['printers'],config['discord_token'],config['demo']),([],'',False))
            self.assertEqual(config['listen'],['127.0.0.1','100.1.2.3'])
            self.assertIn('add your printers under Settings & help → Printers',out.getvalue())
            self.assertIn('INITIAL DASHBOARD PASSWORD',out.getvalue())
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):configure.migrate(Path(d)/'missing.py',Path(d)/'config.json',Path(d)/'data','127.0.0.1',demo=True)
            self.assertTrue(json.loads((Path(d)/'config.json').read_text())['demo'])

    def test_importing_the_old_bot_still_works(self):
        import configure
        with tempfile.TemporaryDirectory() as d:
            old=Path(d)/'bot.py'
            old.write_text(f"DISCORD_BOT_TOKEN='{TOKEN}'\nALLOWED_GUILD_IDS=[111]\nPRINTERS=[{{'name':'A','ip':'1.2.3.4','serial':'030X','access_code':'12345678'}}]\n")
            with redirect_stdout(io.StringIO()):configure.migrate(old,Path(d)/'config.json',Path(d)/'data','127.0.0.1')
            config=json.loads((Path(d)/'config.json').read_text())
            self.assertEqual((config['discord_token'],config['guild_ids'],config['demo']),(TOKEN,[111],False))


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_routes_need_a_signed_in_dashboard(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        from queueing import Engine
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            core=SimpleNamespace(DATA_DIR=d,SETTINGS_FILE=str(d/'settings.json'),settings={},SETTINGS_USER_IDS=set(),EXAMPLE_MODE=True,CONFIG={},
                names=lambda:[],state_data=lambda n:('IDLE',0,{},True),last_seen={},bot=SimpleNamespace(is_ready=lambda:False),log=__import__('logging').getLogger('test'))
            store=Store(d/'db');dashboard=Dashboard(core,store,Engine(core,store));atomic_json(d/'auth.json',password_hash('test-password-123'))
            async with TestClient(TestServer(dashboard.app)) as client:
                self.assertEqual((await client.get('/api/setup')).status,401)
                self.assertEqual((await client.post('/api/setup/printer',json=MINI,headers={'X-PM':'1'})).status,401)
                r=await client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'});csrf=(await r.json())['csrf']
                headers={'X-PM':'1','X-CSRF':csrf}
                self.assertEqual((await client.post('/api/setup/printer',json=MINI,headers={'X-PM':'1'})).status,403)   # no CSRF token
                r=await client.post('/api/setup/printer',json=MINI,headers=headers);self.assertEqual(r.status,200)
                self.assertEqual([p['name'] for p in (await r.json())['printers']],['Mini 1'])
                state=await (await client.get('/api/state')).json()
                self.assertTrue(state['setup_needed']);self.assertFalse(state['discord_configured'])   # until the restart
            store.db.close()


class DoctorTests(unittest.TestCase):
    def test_pm_doctor_checks_dashboard_printers_too(self):
        from doctor import pm_doctor
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'printers.json').write_text(json.dumps([MINI]))
            checks=pm_doctor.Checks(Path(d)/'config.json',Path(d),runner=lambda *a,**k:SimpleNamespace(stdout='',returncode=0))
            with patch.object(checks,'reachable',return_value=True):
                net=checks.network({'printers':[{'name':'Andrew','ip':'192.168.1.40'}]})
            self.assertEqual([p['name'] for p in net['printers']],['Andrew','Mini 1'])


if __name__=='__main__':
    unittest.main()
