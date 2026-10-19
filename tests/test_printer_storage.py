"""The printer's MicroSD card or USB drive, from its own reports (printer_storage.py): decoded the way Bambu Studio
does (aux bits 12-13 on the newer report, else home_flag bits 8-9, else sdcard), shown on the dashboard and in
Discord, announced when it goes wrong or comes right, and the queue holds the printer's jobs instead of failing the
upload. There's no command to format it over MQTT, so the messages say how to do it on the printer."""
import asyncio,importlib.util,json,os,re,shutil,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT))
import printer_models
import printer_storage as ps
from queueing import Engine,Store,options

NODE=shutil.which('node')
H2D,A1=printer_models.MODELS['h2d'],printer_models.MODELS['a1']
NEW=dict(cfg='0',fun='0',stat='0')   # the newer report's other fields, which come with "aux"


def aux(state,other=1<<26):
    """A newer report whose storage is `state` (0 none, 1 ready, 2 can't be read, 3 read-only), other bits set too."""
    return dict(NEW,aux=format(state<<12|other,'X'))


class DecodeTests(unittest.TestCase):
    def test_the_newer_report(self):
        self.assertEqual([ps.state(aux(n)) for n in range(4)],['none','ready','unreadable','read_only'])
        self.assertEqual(ps.state(dict(aux(2),home_flag=1<<8|7)),'unreadable')        # aux wins, as in Bambu Studio
        self.assertEqual(ps.state({'aux':'2000','home_flag':1<<8|7}),'ready')          # aux counts only with cfg, fun, stat
        self.assertEqual(ps.state(dict(NEW,aux='0',home_flag=3<<8|7)),'read_only')     # empty or 0: not filled in
        self.assertEqual(ps.state(dict(NEW,aux='',sdcard=True)),'ready')
        self.assertEqual(ps.state(dict(NEW,aux=' 0x00002000 ')),'unreadable')

    def test_home_flag(self):
        self.assertEqual([ps.state({'home_flag':n<<8|7}) for n in range(4)],['none','ready','unreadable','read_only'])
        self.assertEqual(ps.state({'home_flag':-(1<<31)|1<<8|7}),'ready')   # bit 31 set: a negative 32-bit number
        self.assertEqual(ps.state({'home_flag':str(2<<8|7)}),'unreadable')
        for flag in (0,True,'x',None):
            self.assertIsNone(ps.state({'home_flag':flag}),flag)

    def test_sdcard_and_nothing(self):
        self.assertEqual((ps.state({'sdcard':True}),ps.state({'sdcard':False})),('ready','none'))
        self.assertEqual(ps.state({'home_flag':2<<8|7,'sdcard':True}),'unreadable')   # home_flag wins
        for data in ({},{'sdcard':'yes'},None,[],dict(NEW,aux='zz')):
            self.assertIsNone(ps.state(data),data)

    def test_printing_without_storage(self):
        self.assertTrue(ps.prints_without({'fun2':'1'}));self.assertTrue(ps.prints_without({'fun2':'0x8000000000000000001'}))
        for data in ({'fun2':'2'},{'fun2':''},{'fun2':1},{},None):
            self.assertFalse(ps.prints_without(data),data)


class TextTests(unittest.TestCase):
    def test_what_the_h2d_says(self):
        found=ps.problem(H2D,aux(2))
        self.assertEqual((found['state'],found['title'],found['short'],found['sentence']),
                         ('unreadable',"Can't read the USB drive","can't read the USB drive","The H2D can't read its USB drive."))
        self.assertIn('Format it on the printer: Settings → USB Storage → Format External Storage (this erases it).',found['detail'])
        self.assertIn('try another USB drive',found['detail'])
        found=ps.problem(H2D,aux(0))
        self.assertEqual((found['title'],found['sentence']),('No USB drive','The H2D has no USB drive.'));self.assertIn('FAT32 or exFAT',found['detail'])
        found=ps.problem(H2D,aux(3))
        self.assertEqual((found['title'],found['sentence']),('USB drive is read-only',"The H2D's USB drive is read-only."))
        self.assertIn('timelapse',found['detail']);self.assertIn('Format External Storage',found['detail'])

    def test_other_printers(self):
        found=ps.problem(A1,{'home_flag':2<<8|7})
        self.assertEqual((found['title'],found['sentence']),("Can't read the MicroSD card","The A1 can't read its MicroSD card."))
        self.assertIn('on a computer as FAT32 or exFAT',found['detail']);self.assertNotIn('USB Storage',found['detail'])
        self.assertEqual(ps.problem(printer_models.UNKNOWN,{'home_flag':7})['sentence'],'The printer has no MicroSD card or USB drive.')
        self.assertIn('USB Storage',ps.problem(printer_models.MODELS['h2c'],aux(2))['detail'])   # the whole H2 series

    def test_nothing_to_say(self):
        for data in (aux(1),{},{'home_flag':1<<8|7},{'sdcard':True}):
            self.assertIsNone(ps.problem(H2D,data),data)
        self.assertIsNone(ps.problem(H2D,dict(aux(0),fun2='1')))       # no drive, but it can print without one
        self.assertIsNotNone(ps.problem(H2D,dict(aux(2),fun2='1')))    # though not from a drive it can't read
        self.assertIsNone(ps.problem(H2D,aux(2),{'storage_check':False}))

    def test_short_enough_for_a_job_note(self):
        # A job keeps the first 250 characters of the error that stopped it (Engine.dispatch).
        for model in list(printer_models.MODELS.values())+[printer_models.UNKNOWN]:
            for n in (0,2,3):
                self.assertLessEqual(len(str(ps.StorageProblem(ps.problem(model,aux(n))))),250,(model['key'],n))


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.now=[100.0];self.watch=ps.Watch(clock=lambda:self.now[0])
    def report(self,data,after=1,name='H2D',config=None):
        self.now[0]+=after;return self.watch.update(name,H2D,data,config)

    def test_going_wrong_and_coming_right(self):
        self.assertIsNone(self.report(aux(1)))           # the first report is only remembered
        self.assertIsNone(self.report(aux(2)))           # it has to last 15 seconds
        self.assertIsNone(self.report(aux(2),10))
        title,detail,level=self.report(aux(2),6)
        self.assertEqual((title,level),("💾 Can't read the USB drive",'warn'));self.assertIn('Format External Storage',detail)
        self.assertIsNone(self.report(aux(2),60))        # said once
        # Formatted on the printer: unmounted for a moment, then ready
        self.assertIsNone(self.report(aux(0)));self.assertIsNone(self.report(aux(1),5));self.assertIsNone(self.report(aux(1),10))
        self.assertEqual(self.report(aux(1),6),('💾 USB drive ready','The printer can use its USB drive again: queued jobs can start.','ok'))

    def test_a_drive_mounting_after_a_restart_says_nothing(self):
        self.report(aux(1));self.report(aux(0));self.report(aux(0),10)
        self.assertIsNone(self.report(aux(1),3));self.assertIsNone(self.report(aux(1),30))

    def test_wrong_from_the_start_is_shown_not_announced(self):
        self.assertIsNone(self.report(aux(3)));self.assertIsNone(self.report(aux(3),60))
        self.report(aux(1));self.assertEqual(self.report(aux(1),20)[2],'ok')

    def test_reports_without_it_and_turning_it_off(self):
        self.assertIsNone(self.report({}));self.report(aux(1));self.assertIsNone(self.report({},30))
        self.report(aux(2));self.assertEqual(self.report(aux(2),20)[2],'warn')
        self.report(aux(1),name='Off',config={'storage_check':False})
        self.assertIsNone(self.report(aux(2),30,'Off',{'storage_check':False}))
        self.report(dict(aux(1),fun2='1'),name='Emmc');self.report(dict(aux(0),fun2='1'),name='Emmc')
        self.assertIsNone(self.report(dict(aux(0),fun2='1'),30,'Emmc'))   # no drive, but it doesn't need one


class Core:
    """The parts of core.py the queue and the dashboard use."""
    def __init__(self,d,configs,data):
        self.DATA_DIR,self.SETTINGS_FILE,self.settings,self.SETTINGS_USER_IDS=d,str(d/'settings.json'),{},set()
        self.EXAMPLE_MODE,self.configs,self.data,self.last_seen=True,configs,data,{}
        self.EXAMPLE_DATA={name:{} for name in configs}   # a demo print, once a job starts
        self.bot=SimpleNamespace(is_ready=lambda:False);self.log=__import__('logging').getLogger('test')
        self.notify=AsyncMock();self.BLUE,self.YELLOW,self.GREEN,self.RED=1,2,3,4
    def names(self):return list(self.configs)
    def printer_config(self,name):return self.configs.get(name)
    def display_name(self,name):return name
    def state_data(self,name):return 'IDLE',0,dict(self.data[name]),True


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'H2D':{'model':'H2D'},'Mini':{'model':'A1 mini'}},{'H2D':aux(2),'Mini':{}})
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store)
    async def asyncTearDown(self):
        for task in list(self.engine.tasks):task.cancel()
        self.store.db.close();self.tmp.cleanup()

    def test_ready_says_what_to_do(self):
        with self.assertRaises(ps.StorageProblem) as caught:self.engine.ready('H2D')
        self.assertTrue(str(caught.exception).startswith("The H2D can't read its USB drive. Format it on the printer: Settings"))
        self.assertEqual(caught.exception.short,"can't read the USB drive")
        self.assertIsInstance(caught.exception,ValueError)   # shown like every other reason a start is refused
        self.engine.ready('H2D',override_error=True)          # Start anyway / /queueforce
        self.engine.ready('Mini')                             # reports nothing about its storage: never held
        self.core.configs['H2D']['storage_check']=False;self.engine.ready('H2D')
        del self.core.configs['H2D']['storage_check'];self.core.data['H2D']=aux(1);self.engine.ready('H2D')

    async def test_the_job_waits_instead_of_failing(self):
        job=self.store.add('H2D','Bracket',None,'cache/bracket.gcode.3mf',options(),'Ana',demo=True)
        with self.assertRaisesRegex(ValueError,"can't read its USB drive"):
            await self.engine.start(job['id'],True,'Ana')
        self.assertEqual(self.store.get(job['id'])['status'],'queued')   # not used up: start it again once it's fixed
        self.core.data['H2D']=aux(1)
        await self.engine.start(job['id'],True,'Ana')
        self.assertEqual(self.store.get(job['id'])['status'],'staging')

    async def test_the_ai_reprint_picks_another_printer(self):
        from failureDetection.auto_reprint import AutoReprint
        from tests.test_better_queue import make_3mf
        self.core.configs={'Mini 1':{'model':'A1 mini'},'Mini 2':{'model':'A1 mini'},'Mini 3':{'model':'A1 mini'}}
        self.core.data={'Mini 1':{},'Mini 2':{'home_flag':2<<8|7},'Mini 3':{'home_flag':1<<8|7}}
        (Path(self.tmp.name)/'uploads').mkdir()
        job=self.store.add('Mini 1','Bracket',str(make_3mf(Path(self.tmp.name)/'uploads'/'part.3mf')),'',options(1),'t',demo=True)
        reprints=AutoReprint(self.core,self.engine,{})
        async def bed_state(name,refresh=False):return dict(state='clear',detail='',checked=0)
        reprints.bed_state=bed_state
        rows={r['name']:r for r in await reprints.availability(self.store.get(job['id']))}
        self.assertEqual((rows['Mini 2']['ok'],rows['Mini 2']['reason']),(False,"can't read the MicroSD card"))
        self.assertTrue(rows['Mini 3']['ok'])


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'H2D':{'model':'H2D'},'Mini':{'model':'A1 mini'}},{'H2D':aux(3),'Mini':{'home_flag':1<<8|7}})
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.headers={'X-PM':'1','X-CSRF':(await r.json())['csrf']}
    async def asyncTearDown(self):
        for task in list(self.engine.tasks):task.cancel()
        await self.client.close();self.store.db.close();self.tmp.cleanup()

    async def test_card_status_icon_and_start(self):
        state=await (await self.client.get('/api/state')).json()
        printers={p['name']:p for p in state['printers']}
        self.assertEqual(printers['H2D']['storage']['title'],'USB drive is read-only');self.assertIsNone(printers['Mini']['storage'])
        item=next(i for i in state['status']['items'] if i['key']=='storage:H2D')
        self.assertEqual((item['title'],item['level'],item['printer']),('H2D: USB drive is read-only','warn','H2D'))
        job=self.store.add('H2D','Bracket',None,'cache/bracket.gcode.3mf',options(),'Ana',demo=True)
        r=await self.client.post(f"/api/jobs/{job['id']}/start",json={'confirmed':True},headers=self.headers)
        self.assertEqual(r.status,400);self.assertIn("The H2D's USB drive is read-only.",(await r.json())['error'])
        self.core.state_data=lambda name:('IDLE',0,aux(3),False)   # offline: its last report is stale, so nothing shows
        state=await (await self.client.get('/api/state')).json()
        self.assertIsNone(state['printers'][0]['storage']);self.assertFalse([i for i in state['status']['items'] if i['key'].startswith('storage:')])

    def test_the_page(self):
        app,controls,page=((ROOT/'static'/f).read_text() for f in ('app.js','controls.js','index.html'))
        self.assertIn('${driveLine(x)}',app);self.assertIn('id="detailDrive"',page);self.assertIn("$('detailDrive')",controls)

    @unittest.skipUnless(NODE,'node is not installed')
    def test_the_card_line(self):
        app=(ROOT/'static/app.js').read_text()
        source='\n'.join(re.search(rf'^{start}.*$',app,re.M).group(0) for start in ('const esc=','function driveLine\\('))
        cards=[{'storage':{'title':"Can't read the USB drive",'detail':'Format it <now>'}},{'storage':None},{}]
        script=source+f"\nconsole.log(JSON.stringify({json.dumps(cards)}.map(driveLine)));"
        lines=json.loads(subprocess.run([NODE,'-e',script],capture_output=True,text=True,check=True,timeout=30).stdout)
        self.assertEqual(lines,['<p class="drive-line" title="Format it &lt;now&gt;">💾 Can&#39;t read the USB drive</p>','',''])


def load_core(d,example_data):
    (Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1],'demo':True,'example_data':example_data}))
    with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
        spec=importlib.util.spec_from_file_location('test_core_printer_storage',ROOT/'core.py')
        core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
    return core


class FakeClient:
    """paho's client, enough for connect_printer: the test calls its on_message itself."""
    def __init__(self,*args,**kwargs):pass
    def __getattr__(self,name):return lambda *args,**kwargs:None
    def is_connected(self):return True


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.core=load_core(cls.tmp.name,{'H2D':dict(aux(2)),'A1':{'home_flag':1<<8|7}})
    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.core.bot.close());cls.tmp.cleanup()

    def test_discord_printer_card(self):
        fields={f.name:f.value for f in self.core.printer_embed('H2D').fields}
        self.assertIn('Format External Storage',fields["💾 Can't read the USB drive"])
        self.assertFalse([n for n in (f.name for f in self.core.printer_embed('A1').fields) if n.startswith('💾')])

    def test_alerts_from_the_printers_reports(self):
        core,now=self.core,[1000.0]
        notes=[]
        with patch.object(core.mqtt,'Client',FakeClient),patch.object(core,'queue_notification',lambda *a:notes.append(a)),\
             patch.object(core,'storage_watch',ps.Watch(clock=lambda:now[0])):
            core.connect_printer({'name':'Lab H2D','ip':'192.0.2.5','serial':'0948AB123456','access_code':'12345678','model':'H2D'})
            client=core.clients.pop('Lab H2D')
            def report(fields,after=1):
                now[0]+=after;client.on_message(client,None,SimpleNamespace(payload=json.dumps({'print':fields}).encode()))
            report(aux(1));report({'aux':aux(2)['aux']});report({'nozzle_temper':25},20)   # an update without it still counts
            self.assertEqual([(n[0],n[1],n[3]) for n in notes],[('Lab H2D',"💾 Can't read the USB drive",core.YELLOW)])
            report({'aux':aux(1)['aux']});report({'nozzle_temper':26},20)
            self.assertEqual((notes[-1][1],notes[-1][3]),('💾 USB drive ready',core.GREEN));self.assertEqual(len(notes),2)
        core.reports.pop('Lab H2D',None)


class DiagnosticsTests(unittest.TestCase):
    def test_the_report_says_it(self):
        import diagnostics
        core=SimpleNamespace(names=lambda:['H2D'],state_data=lambda n:('IDLE',0,aux(2),True),printer_config=lambda n:{},last_seen={})
        self.assertEqual(diagnostics.printer_summary(core)[0]['storage'],'unreadable')


if __name__=='__main__':
    unittest.main()
