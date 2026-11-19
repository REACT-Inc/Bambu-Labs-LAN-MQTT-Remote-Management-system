"""Print plans (print_plans.py, /printplan): change speed, temperatures or the fan at a percentage or layer."""
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import print_plans as pp
from printer_controls import prepare
from queueing import Store
from tests.test_slicer import Core

ROOT=Path(__file__).parents[1]


class ParseTests(unittest.TestCase):
    def test_when(self):
        for text,want in (('10%',('percent',10)),('10',('percent',10)),(' 45 % ',('percent',45)),('layer 60',('percent',60) if False else ('layer',60)),
                          ('L6',('layer',6)),('Layer #120',('layer',120)),('at 25%',('percent',25))):
            self.assertEqual(pp.parse_at(text),want,text)
        for bad in ('0%','100%','150%','layer 0','soon','',None,'-5%'):
            with self.assertRaises(ValueError,msg=bad):pp.parse_at(bad)

    def test_changes(self):
        self.assertEqual(pp.parse_change('speed','277%'),('speed_percent',277))
        self.assertEqual(pp.parse_change('speed','normal'),('speed','standard'))
        self.assertEqual(pp.parse_change('speed','100%'),('speed','standard'))   # a profile's own percentage is that profile
        self.assertEqual(pp.parse_change('speed','166'),('speed','ludicrous'))
        self.assertEqual(pp.parse_change('speed','Sport'),('speed','sport'))
        self.assertEqual(pp.parse_change('fan','80%'),('fan_part',80))
        self.assertEqual(pp.parse_change('nozzle','220°C'),('nozzle',220))
        for action,value in (('speed','warp'),('nozzle','hot'),('flow','100'),('fan','')):
            with self.assertRaises(ValueError,msg=(action,value)):pp.parse_change(action,value)

    def test_custom_speed_is_the_gcode_override(self):
        core=Core(Path('.'),{'Mini':{'model':'A1 mini'}})
        self.assertEqual(prepare(core,'Mini','speed_percent',277),('gcode_line','M220 S277\n','Print speed → 277 % (M220)'))
        for bad in (5,301,'fast',True):
            with self.assertRaises(ValueError,msg=bad):prepare(core,'Mini','speed_percent',bad)


class Printer(Core):
    def __init__(self,d):
        super().__init__(d,{'Mini':{'model':'A1 mini'}})
        self.now=dict(state='IDLE',data={},connected=True)
    def state_data(self,name):return self.now['state'],0,dict(self.now['data']),self.now['connected']


class PlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Printer(d);self.store=Store(d/'db')
        self.applied=[]
        def apply(name,kind,value,axis=None,confirmed=False,homed=False,author=''):
            if value==999:raise ValueError('Printer is offline.')
            self.applied.append((kind,value,author));return f'{kind} → {value}'
        self.controls=SimpleNamespace(apply=apply)
        self.plans=pp.PrintPlans(self.core,self.store,self.controls)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def printing(self,percent,layer=None,file='bracket.3mf',state='RUNNING'):
        self.core.now.update(state=state,data=dict(subtask_name=file,mc_percent=percent,layer_num=layer if layer is not None else percent,total_layer_num=200))

    async def test_a_plan_for_the_next_print(self):
        self.plans.add('Mini','10%','speed','277%','Sam');self.plans.add('Mini','layer 60','speed','normal','Sam')
        self.assertTrue(self.plans.public('Mini')['waiting_for_print'])
        await self.plans.check('Mini');self.assertEqual(self.applied,[])   # idle: waiting
        self.printing(2,3);await self.plans.check('Mini')
        self.assertTrue(self.plans.public('Mini')['current']);self.assertEqual(self.applied,[])
        self.printing(10,20);await self.plans.check('Mini')
        self.assertEqual(self.applied,[('speed_percent',277,'print plan (Sam)')])
        self.core.notify.assert_awaited();self.assertIn('At 10 %',self.core.notify.await_args.args[2])
        self.printing(31,60);await self.plans.check('Mini')
        self.assertEqual(self.applied[-1][:2],('speed','standard'))
        self.assertEqual([s['state'] for s in self.plans.get('Mini')['steps']],['done','done'])
        self.printing(100,200,state='FINISH');await self.plans.check('Mini')
        self.assertIsNone(self.plans.get('Mini'))   # it ended with its print
        self.assertIn('2 of 2 changes',self.store.events()[0]['detail'])

    async def test_a_step_already_passed_doesnt_run(self):
        self.printing(40,80)
        plan,step=self.plans.add('Mini','10%','speed','sport','Sam')
        self.assertEqual(step['state'],'skipped');self.assertIn('already at 40 %',step['note'])
        await self.plans.check('Mini');self.assertEqual(self.applied,[])
        with self.assertRaises(ValueError):self.plans.add('Mini','layer 500','speed','sport','Sam')   # this print has 200 layers

    async def test_only_the_last_of_a_kind_reached_together_runs(self):
        self.printing(1,1)
        for at,value in (('10%','sport'),('20%','ludicrous'),('30%','silent')):self.plans.add('Mini',at,'speed',value,'Sam')
        self.plans.add('Mini','25%','fan','80','Sam')
        self.printing(35,70);await self.plans.check('Mini')   # (the Pi was off between 1 % and 35 %)
        self.assertEqual(sorted(self.applied),sorted([('speed','silent','print plan (Sam)'),('fan_part',80,'print plan (Sam)')]))
        self.assertEqual([s['state'] for s in self.plans.get('Mini')['steps']],['skipped','skipped','done','done'])

    async def test_failures_and_a_new_print(self):
        self.printing(1)
        self.plans.add('Mini','5%','nozzle','220','Sam')
        plan=self.plans.get('Mini');plan['steps'][0]['value']=999;self.plans.save('Mini',plan)   # (the printer refuses it)
        self.printing(6);await self.plans.check('Mini')
        self.assertEqual(self.plans.get('Mini')['steps'][0]['state'],'failed')
        self.assertEqual(self.core.notify.await_args.args[3],self.core.YELLOW)
        self.printing(1,file='other.3mf');await self.plans.check('Mini')   # another print: that plan is over
        self.assertIsNone(self.plans.get('Mini'))

    async def test_kept_and_checked(self):
        with self.assertRaises(ValueError):self.plans.add('Mini','10%','nozzle','999','Sam')   # above the A1 mini's limit
        with self.assertRaises(ValueError):self.plans.add('Nope','10%','speed','sport','Sam')
        self.plans.add('Mini','10%','speed','sport','Sam')
        again=pp.PrintPlans(self.core,self.store,self.controls)   # after a restart
        self.assertEqual(len(again.get('Mini')['steps']),1)
        step=again.get('Mini')['steps'][0]
        again.remove('Mini',step['id'],'Sam');self.assertIsNone(again.get('Mini'))
        with self.assertRaises(ValueError):again.remove('Mini','nope','Sam')
        self.plans.add('Mini','10%','speed','sport','Sam');self.assertTrue(self.plans.clear('Mini','Sam'));self.assertFalse(self.plans.clear('Mini','Sam'))
        self.core.now['connected']=False;self.plans.add('Mini','10%','speed','sport','Sam')
        self.printing(50);self.core.now['connected']=False;await self.plans.check('Mini');self.assertEqual(self.applied,[])


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard
        from queueing import Engine
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'Mini':{'model':'A1 mini'}});self.core.CONFIG={'protect_management':False}
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        self.csrf=(await (await self.client.get('/api/state')).json())['csrf']
    async def asyncTearDown(self):
        for t in list(self.engine.tasks):t.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()
    async def post(self,path,body):
        r=await self.client.post(path,json=body,headers={'X-PM':'1','X-CSRF':self.csrf});return r.status,await r.json()

    async def test_routes(self):
        status,body=await self.post('/api/printplan/Mini',{'at':'10%','action':'speed','value':'277%'})
        self.assertEqual(status,200);self.assertEqual(body['step']['kind'],'speed_percent')
        state=await (await self.client.get('/api/state')).json()
        plan=state['printers'][0]['plan'];self.assertEqual(plan['steps'][0]['at'],10);self.assertTrue(plan['waiting_for_print'])
        status,body=await self.post('/api/printplan/Mini',{'at':'tomorrow','action':'speed','value':'sport'});self.assertEqual(status,400)
        status,body=await self.post('/api/printplan/Mini/remove',{'id':plan['steps'][0]['id']});self.assertIsNone(body['plan'])
        await self.post('/api/printplan/Mini',{'at':'layer 6','action':'fan','value':'50'})
        status,body=await self.post('/api/printplan/Mini/clear',{});self.assertEqual(status,200)
        self.assertIsNone(self.dashboard.print_plans.get('Mini'))

    def test_the_page_and_discord(self):
        page,app=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text()
        for id in ('planSection','planSteps','planForm','planAt','planAction','planValue','planClear'):self.assertIn(f'id="{id}"',page)
        self.assertIn("api('printplan/'+encodeURIComponent(selectedPrinter)",app)
        from discord_Intergration.print_plan_discord import describe
        core=SimpleNamespace(safe=lambda t:str(t),display_name=lambda n:n)
        text=describe(core,'Mini',dict(current=True,progress={'percent':12,'layer':30},waiting_for_print=False,
            steps=[dict(id='a1b2c3',state='done',at_kind='percent',at=10,label='Print speed → 277 % (M220)',note=''),
                   dict(id='d4e5f6',state='waiting',at_kind='layer',at=60,label='Print speed → standard',note='')]))
        self.assertIn('at 12 %, layer 30',text);self.assertIn('✅ `a1b2c3` · at **10 %**',text);self.assertIn('⏳ `d4e5f6` · at **layer 60**',text)
        self.assertIn('no print plan',describe(core,'Mini',None))


if __name__=='__main__':
    unittest.main()
