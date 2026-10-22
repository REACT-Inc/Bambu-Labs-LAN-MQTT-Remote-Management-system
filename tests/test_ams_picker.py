"""The AMS slot picker in Queue a print and Print on another printer: every AMS slot of the printer (AMS A-D, AMS HT),
named and numbered like Bambu Studio and the printer's screen, with colour, material, brand, how much is left and the
nozzle it feeds; suggestions use the same numbers; the page draws pictures of the units instead of a number field."""
import json,re,shutil,subprocess,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace

import sliced_file
from queueing import Engine,Store,options
from tests.test_better_queue import make_3mf

ROOT=Path(__file__).parents[1]
NODE=shutil.which('node')

# An H2D-like printer: two nozzles, AMS A feeds the right nozzle, AMS B the left, and an AMS HT on the left.
DUAL={'device':{'extruder':{'state':0x02,'info':[{'id':0},{'id':1}]}},
      'ams':{'ams':[
          {'id':'0','info':'0000','tray':[{'id':'0','tray_type':'PLA','tray_color':'FE0101FF','tray_sub_brands':'PLA Basic','remain':80},
                                          {'id':'1','tray_type':'PLA','tray_color':'0000FFFF','remain':-1},{'id':'2'},
                                          {'id':'3','tray_type':'PETG','tray_color':'00EE00FF','tray_sub_brands':'PETG HF','remain':35}]},
          {'id':'1','info':'0100','tray':[{'id':'0','tray_type':'ABS','tray_color':'FFFFFFFF'},{'id':'1'},{'id':'2'},{'id':'3'}]},
          {'id':'128','info':'0100','tray':[{'id':'0','tray_type':'PA-CF','tray_color':'222222FF','remain':60}]}]}}


class SlotTests(unittest.TestCase):
    def test_every_slot_named_like_the_printer(self):
        slots=sliced_file.ams_slots(DUAL)
        self.assertEqual([(s['short'],s['tray'],s['loaded']) for s in slots],
                         [('A1',0,True),('A2',1,True),('A3',2,False),('A4',3,True),('B1',4,True),('B2',5,False),('B3',6,False),('B4',7,False),('HT-A',128,True)])
        a1,a2=slots[0],slots[1]
        self.assertEqual((a1['unit_name'],a1['type'],a1['colour'],a1['brand'],a1['remain'],a1['nozzle']),('AMS A','PLA','#FE0101','PLA Basic',80,'Right'))
        self.assertIsNone(a2['remain'])                                   # -1: the printer doesn't know
        self.assertEqual((slots[4]['nozzle'],slots[8]['nozzle'],slots[8]['unit_name'],slots[8]['label']),('Left','Left','AMS HT-A','AMS HT-A'))
        self.assertEqual(a1['label'],'AMS 1 slot 1')                      # unchanged for messages elsewhere
        single={'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA'}]}]}}
        self.assertIsNone(sliced_file.ams_slots(single)[0]['nozzle'])      # one nozzle: nothing to say
        self.assertEqual(sliced_file.ams_slots({}),[])
        self.assertEqual([s['short'] for s in sliced_file.loaded_trays(DUAL)],['A1','A2','A4','B1','HT-A'])

    def test_bad_reports_are_skipped(self):
        bad={'ams':{'ams':[None,{'id':'x'},{'id':'200','tray':[{'id':'0','tray_type':'PLA'}]},
                           {'id':'128','tray':[{'id':'1','tray_type':'PLA'}]},{'id':'0','tray':[{'id':'9','tray_type':'PLA'},None]}]}}
        self.assertEqual(sliced_file.ams_slots(bad),[])

    def test_suggestions_use_the_same_numbers_and_list_every_slot(self):
        plate={'filaments':[{'id':1,'type':'PA-CF','colour':'#222222','used_g':40.0},{'id':2,'type':'PETG','colour':'#00FF00','used_g':3.0}]}
        s=sliced_file.suggest_mapping(plate,DUAL)
        self.assertEqual((s['mapping'],s['complete']),('128,3',True))
        self.assertEqual([(r['tray_short'],r['used_g']) for r in s['rows']],[('HT-A',40.0),('A4',3.0)])
        self.assertEqual(len(s['slots']),9)
        self.assertEqual(options(1,True,s['mapping'])['ams_mapping'],[128,3])   # an AMS HT number is accepted
        empty=sliced_file.suggest_mapping(plate,{})
        self.assertEqual(([r['filament'] for r in empty['rows']],empty['slots']),([1,2],[]))   # rows still shown


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        configs={'H2D':{'model':'H2D'},'Mini':{'model':'A1 mini'}}
        async def notify(*a):pass
        data={'H2D':DUAL,'Mini':{}}
        self.core=SimpleNamespace(DATA_DIR=d,SETTINGS_FILE=str(d/'settings.json'),settings={},SETTINGS_USER_IDS=set(),EXAMPLE_MODE=True,
            names=lambda:list(configs),printer_config=configs.get,state_data=lambda n:('IDLE',0,json.loads(json.dumps(data[n])),True),last_seen={},
            bot=SimpleNamespace(is_ready=lambda:False),log=__import__('logging').getLogger('test'),notify=notify,BLUE=1,YELLOW=2,GREEN=3,RED=4)
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.headers={'X-PM':'1','X-CSRF':(await r.json())['csrf']}
    async def asyncTearDown(self):
        for t in list(self.engine.tasks):t.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()

    async def test_printer_slots(self):
        r=await (await self.client.get('/api/ams/H2D')).json()
        self.assertEqual([s['short'] for s in r['slots']][-1],'HT-A')
        self.assertEqual((await self.client.get('/api/ams/Nope')).status,400)

    async def test_job_suggestion_for_another_printer(self):
        path=make_3mf(Path(self.tmp.name)/'part.3mf',model='Bambu Lab H2D')
        job=self.store.add('Mini','Brackets',str(path),'',options(1),'Ana',demo=True)
        r=await (await self.client.get(f"/api/jobs/{job['id']}/suggest?printer=H2D")).json()
        self.assertEqual((r['mapping'],[x['tray_short'] for x in r['rows']]),('0,-1,3',['A1','A4']))
        self.assertEqual(len(r['slots']),9)
        remote=self.store.add('Mini','On the card',None,'cache/part.gcode.3mf',options(1),'Ana',demo=True)
        r=await (await self.client.get(f"/api/jobs/{remote['id']}/suggest?printer=H2D")).json()
        self.assertEqual((r['rows'],len(r['slots'])),([],9));self.assertIn("can't read its filaments",r['message'])
        self.assertEqual((await self.client.get(f"/api/jobs/{job['id']}/suggest?printer=Nope")).status,400)


class PageTests(unittest.TestCase):
    def test_dialogs_have_the_picker(self):
        page,app,css=((ROOT/'static'/f).read_text() for f in ('index.html','app.js','style.css'))
        for element in ('jobFilaments','sendToFilaments','sendToAmsHint','jobMapping','sendToMapping'):self.assertIn(f'id="{element}"',page)
        self.assertEqual(page.count('class="fil-manual"'),2)   # typing numbers is still possible, folded away
        for code in ("amsPicker($('jobFilaments'),$('jobMapping'),r)","amsPicker($('sendToFilaments'),$('sendToMapping'),r)",
                     "pickerError(jobPicker,","pickerError(sendPicker,","api('ams/'+"):self.assertIn(code,app)
        for rule in ('.tray-tile','.ams-unit','.spool','.fil-row'):self.assertIn(rule,css)
        self.assertNotIn(' style="',page)   # colours go through data-color (no inline styles under the CSP)

    @unittest.skipUnless(NODE,'node is not installed')
    def test_picker_writes_the_mapping(self):
        """amsPicker() in node, with just enough of a DOM: choosing a tile writes the mapping number and the missing list."""
        app=(ROOT/'static/app.js').read_text()
        source='\n'.join(re.search(rf'^{start}.*$',app,re.M).group(0) for start in ('const esc=','function applyStyles','function slotText'))
        start=app.index('function amsPicker(');end=app.index('\nfunction pickerError')
        script=source+'\n'+app[start:end]+r'''
const root={innerHTML:'',querySelectorAll:()=>[],onclick:null},input={value:'',oninput:null};
const slots=%s,rows=[{filament:1,type:'PA-CF',colour:'#222222',used_g:40},{filament:3,type:'PLA',colour:'#FF0000'}];
const p=amsPicker(root,input,{slots,rows});const before=p.missing();
const click=sel=>root.onclick({target:{closest:q=>q===sel.q?sel.el:null}});
click({q:'[data-pick]',el:{dataset:{pick:'3'}}});const open=root.innerHTML.includes('tray-picker');
click({q:'[data-tray]',el:{dataset:{tray:'1'},disabled:false}});
click({q:'[data-pick]',el:{dataset:{pick:'1'}}});click({q:'[data-tray]',el:{dataset:{tray:'128'},disabled:false}});
console.log(JSON.stringify({before,open,value:input.value,after:p.missing(),html:root.innerHTML.includes('HT-A')}));'''%json.dumps(sliced_file.ams_slots(DUAL))
        out=subprocess.run([NODE,'-e',script],capture_output=True,text=True,timeout=30)
        self.assertEqual(out.returncode,0,out.stderr)
        self.assertEqual(json.loads(out.stdout),{'before':[1,3],'open':True,'value':'128,-1,1','after':[],'html':True})


if __name__=='__main__':
    unittest.main()
