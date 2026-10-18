"""Printer errors that clear by themselves when the printer is fine (error_triage.py): what the error is about (from
Bambu's catalog), how far the print got and what the bed check sees decide whether it's kept or cleared. Errors
someone has to see to (filament, nozzle, bed, motion, hardware, door, the printer's AI, storage, anything unknown) are
always kept; a cancelled print, a file problem, a busy printer or the network are cleared when nothing was printed,
or after layer 1 only once the bed check says the bed is clear."""
import asyncio,importlib.util,json,os,re,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT))
import error_triage as et

NODE=shutil.which('node')
CANCELLED,FILAMENT_OUT,PARSE,CLOGGED=0x0300400C,0x07008011,0x05004003,0x03004006


class CategoryTests(unittest.TestCase):
    def check(self,code,expected,keep):
        found=et.lookup(f'{code:08X}',model='094')   # the H2D's catalog (some codes are worded differently per model)
        key,label,needs,about=et.category(found['code'],found['message'])
        self.assertEqual((key,needs),(expected,keep),(f'{code:08X}',found['message']))

    def test_the_printer_itself_is_fine(self):
        for code,key in ((0x0300400C,'cancelled'),(0x0500400E,'cancelled'),(0x05004003,'file'),(0x0500400A,'file'),
                         (0x05004038,'file'),   # "The nozzle diameter in sliced file ...": the file, not the nozzle
                         (0x0500400B,'file'),(0x05004004,'busy'),(0x0502400D,'busy'),(0x03004013,'busy'),(0x05004001,'network')):
            self.check(code,key,False)

    def test_someone_has_to_see_to_it(self):
        for code,key in ((0x03004006,'nozzle'),(0x07008011,'filament'),(0x18008011,'filament'),(0x03008015,'filament'),
                         (0x03004002,'bed'),(0x03008019,'bed'),(0x03004009,'motion'),(0x0300404B,'door'),
                         (0x03008003,'detection'),(0x03008017,'detection'),(0x0500400C,'storage'),
                         (0x0300800E,'storage'),   # "The print file is not available ... storage media has been removed"
                         (0x03008008,'nozzle'),(0x0300400F,'hardware'),(0x03008044,'other')):   # 03008044: fire in the chamber
            self.check(code,key,True)
        self.assertEqual(et.category('0300ABCD','Unrecognized error or model-specific wording; open Bambu support for details.')[0],'other')

    def test_every_catalog_error(self):
        catalogs=json.loads((ROOT/'bambu_error_catalog.json').read_text())['catalogs']
        clearable=set()
        for catalog in catalogs.values():
            for entry in catalog['data'].get('device_error',{}).get('en',[]):
                key,label,keep,about=et.category(str(entry['ecode']),entry.get('intro'))
                if not keep:clearable.add((key,entry.get('intro') or ''))
        self.assertTrue(clearable)
        for key,message in clearable:   # nothing that means the printer needs fixing is ever cleared
            self.assertIsNone(re.search(r'clog|ran out|run out|homing|foreign object|spaghetti|\bfire\b|malfunction|tangle|stuck|leveling',
                                        message.lower()),(key,message))
        self.assertEqual({k for k,_ in clearable},{'cancelled','file','busy','network'})


class DecideTests(unittest.TestCase):
    CLEAR,PARTS,UNKNOWN=dict(state='clear',detail='AI: looks empty'),dict(state='parts',detail='AI: parts'),dict(state='unknown',detail='no camera')

    def test_kept_whatever_the_bed(self):
        for bed in (self.CLEAR,None):
            decision,why=et.decide(True,'the filament or AMS',0,True,bed)
            self.assertEqual(decision,'keep');self.assertIn('someone has to see to it at the printer',why)

    def test_nothing_printed(self):
        self.assertEqual(et.decide(False,'a cancelled print',0,True,self.UNKNOWN),
                         ('clear',"It's about a cancelled print; the print stopped before its first layer."))
        self.assertEqual(et.decide(False,'the network',0,False,self.CLEAR)[1],
                         "It's about the network; no print was running, and the bed check says the bed is clear.")
        self.assertEqual(et.decide(False,'a cancelled print',0,True,self.PARTS)[0],'keep')

    def test_after_layer_one_the_bed_decides(self):
        self.assertEqual(et.decide(False,'a cancelled print',37,True,self.CLEAR)[0],'clear')
        decision,why=et.decide(False,'a cancelled print',37,True,self.PARTS)
        self.assertEqual(decision,'keep');self.assertIn('got to layer 37 and the bed check sees parts',why)
        decision,why=et.decide(False,'a cancelled print',37,True,self.UNKNOWN)
        self.assertEqual(decision,'keep');self.assertIn("probably still on the bed (the bed check can't tell: no camera)",why)
        self.assertEqual(et.decide(False,'a cancelled print',37,True,None)[0],'keep')


class Printer:
    """A printer's reports, as Engine.telemetry passes them on."""
    def __init__(self):self.state,self.error,self.layer,self.subtask,self.connected='IDLE',0,0,'1',True
    def data(self):return dict(gcode_state=self.state,print_error=self.error,layer_num=self.layer,subtask_id=self.subtask)


class Alerts:
    def __init__(self,printer,works=True):self.printer,self.works,self.sent,self.failed_cleared=printer,works,[],{}
    def payloads(self,data,code):return [('clean_print_error',code,data.get('subtask_id'))]
    def send(self,name,payloads):
        self.sent.append((name,payloads))
        if self.works:self.printer.error=0


class TriageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.printer=Printer();self.events=[];self.now=[1000.0]
        self.core=SimpleNamespace(settings={},EXAMPLE_MODE=False,printer_config=lambda n:{'model':'H2D'},notify=AsyncMock(),GREEN=3,YELLOW=2,
                                  state_data=lambda n:(self.printer.state,self.printer.error,self.printer.data(),self.printer.connected))
        self.core.save_settings=lambda updated:setattr(self.core,'settings',dict(updated))
        self.store=SimpleNamespace(event=lambda *a:self.events.append(a))
        self.alerts=Alerts(self.printer)
        self.bed_result=dict(state='unknown',detail='no camera');self.looked=0
        async def bed(name):
            self.looked+=1;return self.bed_result
        async def sleep(seconds):self.now[0]+=seconds
        self.triage=et.Triage(self.core,self.store,self.alerts,bed=bed,clock=lambda:self.now[0],sleep=sleep)

    async def report(self,**changes):
        for key,value in changes.items():setattr(self.printer,key,value)
        self.triage.observe('H2D',self.printer.data())
        while self.triage.tasks:
            await asyncio.gather(*list(self.triage.tasks.values()))

    async def start_print(self,layers=()):
        await self.report()
        await self.report(state='PREPARE',subtask=str(int(self.printer.subtask)+1),layer=0,error=0)   # a new print has no error
        for layer in layers:await self.report(state='RUNNING',layer=layer)

    def condition(self):return self.triage.condition('H2D',self.printer.error,self.printer.data())

    async def test_cancelled_before_the_first_layer_is_cleared(self):
        await self.start_print()
        await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual(self.alerts.sent,[('H2D',[('clean_print_error','0300400C','2')])])
        self.assertEqual(self.alerts.failed_cleared,{'H2D':'2'})   # its next queued job starts normally
        self.assertEqual(self.events[-1][1],'Printer error cleared by itself')
        self.assertIn('the print stopped before its first layer',self.events[-1][2])
        self.core.notify.assert_awaited_once();self.assertEqual(self.core.notify.await_args.args[1],'✅ Error cleared by itself')
        self.assertEqual(self.looked,1)
        self.assertEqual((self.condition()['decision'],self.condition()['title']),('cleared','Cleared by itself'))
        self.now[0]+=et.SHOW_CLEARED+1;self.assertIsNone(self.condition())

    async def test_a_print_that_left_parts_keeps_its_error(self):
        await self.start_print([0,1,5,37])
        self.bed_result=dict(state='parts',detail='AI: looks like a bed with parts on it')
        await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual(self.alerts.sent,[])
        check=self.condition()
        self.assertEqual((check['decision'],check['title'],check['bed']['state']),('kept','Kept','parts'))
        self.assertIn('got to layer 37',check['why'])
        self.assertEqual(self.core.notify.await_args.args[1],'🧹 Parts on the bed')
        self.assertEqual(self.events[-1][1],'Printer error kept')

    async def test_after_layer_one_a_clear_bed_is_needed(self):
        await self.start_print([3])
        await self.report(state='FAILED',error=CANCELLED)   # the bed check can't tell
        self.assertEqual((self.alerts.sent,self.condition()['decision']),([],'kept'))
        self.core.notify.assert_not_awaited()                 # the error's own alert already went out
        await self.start_print([3]);self.bed_result=dict(state='clear',detail='AI: looks like its empty bed')
        await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual(len(self.alerts.sent),1);self.assertEqual(self.condition()['decision'],'cleared')

    async def test_filament_and_nozzle_errors_are_kept(self):
        for error in (FILAMENT_OUT,CLOGGED):
            await self.start_print([0])
            self.bed_result=dict(state='clear',detail='AI: empty')
            await self.report(state='FAILED',error=error)
            self.assertEqual(self.alerts.sent,[]);self.assertEqual(self.condition()['decision'],'kept')
            self.assertIn('someone has to see to it',self.condition()['why'])
            await self.report(error=0,state='IDLE')            # cleared at the printer: the note goes
            self.assertIsNone(self.condition())
        self.assertEqual(self.looked,0)                        # no camera needed to keep it

    async def test_waits_while_the_print_is_paused(self):
        await self.start_print([12])
        await self.report(state='PAUSE',error=FILAMENT_OUT)
        self.assertEqual(self.events,[]);self.assertEqual(self.condition()['decision'],'checking')
        await self.report(state='RUNNING',error=0)             # loaded and resumed
        self.assertIsNone(self.condition())
        await self.report(state='FAILED',error=CANCELLED)       # then stopped: the bed has parts
        self.assertEqual(self.condition()['decision'],'kept')

    async def test_an_error_already_there_is_only_marked_safe(self):
        self.printer.state,self.printer.error='FAILED',PARSE
        await self.report()
        self.assertEqual(self.alerts.sent,[]);self.assertEqual(self.condition()['decision'],'safe')
        self.assertEqual(self.events[-1][1],'Printer error safe to clear')

    async def test_turned_off(self):
        self.triage.set_enabled(False)
        self.assertEqual(self.core.settings,{'auto_clear_errors':False});self.assertIn('no longer',self.events[-1][2])
        await self.start_print();await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual(self.alerts.sent,[]);self.assertEqual(self.condition()['title'],'Safe to clear')
        self.assertIn('Clearing by itself is turned off',self.condition()['why'])
        with self.assertRaisesRegex(ValueError,'on or off'):self.triage.set_enabled('yes')

    async def test_cleared_or_restarted_meanwhile(self):
        await self.start_print()
        async def someone_clears(seconds):self.printer.error=0
        self.triage.sleep=someone_clears
        await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual((self.alerts.sent,self.events,self.triage.pending),([],[],{}))

    async def test_offline_meanwhile_looks_again_later(self):
        await self.start_print()
        sleep=self.triage.sleep
        async def drops_out(seconds):self.printer.connected=False
        self.triage.sleep=drops_out
        await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual(self.events,[]);self.assertEqual(self.condition()['decision'],'checking')
        self.triage.sleep,self.printer.connected=sleep,True
        await self.report()                                     # back online: its next report
        self.assertEqual(self.condition()['decision'],'cleared')

    async def test_the_printer_keeps_reporting_it(self):
        self.alerts.works=False
        await self.start_print();await self.report(state='FAILED',error=PARSE)
        self.assertEqual(len(self.alerts.sent),1)
        self.assertEqual((self.events[-1][1],self.condition()['decision']),('Printer error not cleared','kept'))
        self.assertIn("Clearing it didn't work (the printer still reports it)",self.condition()['why'])

    async def test_a_stale_layer_while_preparing_doesnt_count(self):
        self.printer.state,self.printer.layer='FINISH',120
        await self.report()                                     # the last print reached layer 120
        await self.report(state='PREPARE',subtask='9')          # the new print still reports 120 while it prepares
        await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual(self.condition()['decision'],'cleared')

    async def test_a_new_print_forgets_the_old_decision(self):
        await self.start_print();await self.report(state='FAILED',error=CANCELLED)
        await self.report(state='PREPARE',subtask='3',error=0)
        self.assertIsNone(self.condition())

    async def test_demo_mode_and_no_bed_check(self):
        self.core.EXAMPLE_MODE=True
        await self.start_print();await self.report(state='FAILED',error=CANCELLED)
        self.assertEqual((self.alerts.sent,self.triage.prints),([],{}))
        self.core.EXAMPLE_MODE=False
        triage=et.Triage(self.core,self.store,self.alerts,sleep=self.triage.sleep,clock=lambda:self.now[0])
        self.assertEqual(await triage.look('H2D'),{'state':'unknown','detail':'no bed check'})
        async def broken(name):raise OSError('camera')
        triage.bed=broken;self.assertEqual((await triage.look('H2D'))['state'],'unknown')


class Core:
    """The parts of core.py the dashboard uses."""
    def __init__(self,d,configs,data):
        self.DATA_DIR,self.SETTINGS_FILE,self.settings,self.SETTINGS_USER_IDS=d,str(d/'settings.json'),{},set()
        self.EXAMPLE_MODE,self.configs,self.data,self.last_seen=False,configs,data,{}
        self.bot=SimpleNamespace(is_ready=lambda:False);self.log=__import__('logging').getLogger('test')
        self.notify=AsyncMock();self.BLUE,self.YELLOW,self.GREEN,self.RED=1,2,3,4
        self.clients={}
    def names(self):return list(self.configs)
    def printer_config(self,name):return self.configs.get(name)
    def display_name(self,name):return name
    def state_data(self,name):return self.data[name]['gcode_state'],self.data[name]['print_error'],dict(self.data[name]),True
    def save_settings(self,updated):self.settings=dict(updated)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        from queueing import Engine,Store
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'H2D':{'model':'H2D'}},{'H2D':dict(gcode_state='FAILED',print_error=CLOGGED,layer_num=4,subtask_id='5')})
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.headers={'X-PM':'1','X-CSRF':(await r.json())['csrf']}
    async def asyncTearDown(self):
        await self.dashboard.triage.stop()
        await self.client.close();self.store.db.close();self.tmp.cleanup()

    async def test_reports_reach_it_and_the_card_shows_it(self):
        self.assertIs(self.engine.triage,self.dashboard.triage);self.assertIs(self.core.error_triage,self.dashboard.triage)
        self.dashboard.triage.settle=0
        await self.engine.telemetry('H2D',self.core.data['H2D'])   # the first report: looked at, not cleared
        await asyncio.gather(*self.dashboard.triage.tasks.values())
        state=await (await self.client.get('/api/state')).json()
        check=state['printers'][0]['condition']
        self.assertEqual((check['decision'],check['title'],check['code']),('kept','Kept','03004006'))
        self.assertEqual(state['settings'].get('auto_clear_errors'),None)   # on unless turned off

    async def test_the_switch(self):
        r=await self.client.post('/api/printer-errors/auto',json={'on':False},headers=self.headers)
        self.assertEqual(await r.json(),{'ok':True,'on':False});self.assertIs(self.core.settings['auto_clear_errors'],False)
        r=await self.client.post('/api/printer-errors/auto',json={'on':'no'},headers=self.headers)
        self.assertEqual(r.status,400)

    def test_the_page(self):
        app,controls,page=((ROOT/'static'/f).read_text() for f in ('app.js','controls.js','index.html'))
        self.assertIn('${conditionLine(x)}',app);self.assertIn("api('printer-errors/auto'",app)
        self.assertIn('id="autoClearErrors"',page);self.assertIn('id="detailCondition"',page);self.assertIn("$('detailCondition')",controls)

    @unittest.skipUnless(NODE,'node is not installed')
    def test_the_card_line(self):
        app=(ROOT/'static/app.js').read_text()
        source='\n'.join(re.search(rf'^{start}.*$',app,re.M).group(0) for start in ('const esc=','function conditionLine\\('))
        cards=[{'condition':{'decision':'kept','title':'Kept','why':'Remove <it>','message':'The task was canceled.'}},{'condition':None}]
        script=source+f"\nconsole.log(JSON.stringify({json.dumps(cards)}.map(conditionLine)));"
        lines=json.loads(subprocess.run([NODE,'-e',script],capture_output=True,text=True,check=True,timeout=30).stdout)
        self.assertEqual(lines,['<p class="condition-line condition-kept" title="The task was canceled.">🩺 Kept: Remove &lt;it&gt;</p>',''])


def load_core(d):
    (Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1],'demo':True,'example_data':{'H2D':{'error':CANCELLED,'state':'FAILED'}}}))
    with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
        spec=importlib.util.spec_from_file_location('test_core_error_triage',ROOT/'core.py')
        core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
    return core


class DiscordTests(unittest.TestCase):
    def test_printer_card(self):
        with tempfile.TemporaryDirectory() as d:
            core=load_core(d)
            try:
                self.assertFalse([f for f in core.printer_embed('H2D').fields if f.name.startswith('🩺')])   # nothing decided
                core.error_triage=SimpleNamespace(condition=lambda n,e,data:{'title':'Kept','why':'The print stopped at layer 37.'})
                fields={f.name:f.value for f in core.printer_embed('H2D').fields}
                self.assertEqual(fields['🩺 Kept'],'The print stopped at layer 37.')
            finally:
                asyncio.run(core.bot.close())


if __name__=='__main__':
    unittest.main()
