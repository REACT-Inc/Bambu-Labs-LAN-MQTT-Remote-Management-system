"""Cancelling single objects mid-print (#84): objects from the .3mf, the skip_objects command, safety checks."""
import json,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from object_skip import ObjectSkip,plate_objects,skipped_ids
from queueing import Store,options

SLICE_INFO='''<?xml version="1.0" encoding="UTF-8"?><config>
<plate><metadata key="index" value="1"/>
<object identify_id="75" name="Cube" skipped="false"/><object identify_id="92" name="Cube" skipped="false"/>
<object identify_id="110" name="Bracket" skipped="false"/><object identify_id="130" name="Helper" skipped="true"/></plate>
<plate><metadata key="index" value="2"/><object identify_id="7" name="Other plate" skipped="false"/></plate>
</config>'''


def make(path,info=SLICE_INFO):
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('Metadata/slice_info.config',info);z.writestr('Metadata/plate_1.gcode','G1 X1');z.writestr('Metadata/plate_1.png',b'\x89PNG')
    return str(path)


class ParseTests(unittest.TestCase):
    def test_objects_of_one_plate_with_numbered_copies(self):
        with tempfile.TemporaryDirectory() as d:
            path=make(Path(d)/'a.3mf')
            self.assertEqual(plate_objects(path,1),[{'id':75,'name':'Cube #1'},{'id':92,'name':'Cube #2'},{'id':110,'name':'Bracket'}])
            self.assertEqual(plate_objects(path,2),[{'id':7,'name':'Other plate'}])
            self.assertEqual(plate_objects(Path(d)/'missing.3mf',1),[])
            self.assertEqual(plate_objects(make(Path(d)/'old.3mf','<config><plate><metadata key="index" value="1"/></plate></config>'),1),[])

    def test_skipped_from_report(self):
        self.assertEqual(skipped_ids({'s_obj':[75,'92','x']}),{75,92});self.assertEqual(skipped_ids({}),set());self.assertEqual(skipped_ids({'s_obj':'75'}),set())


class SkipTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.state='RUNNING';self.data={}
        self.client=MagicMock();self.client.is_connected.return_value=True;self.client.publish.return_value=SimpleNamespace(rc=0)
        self.core=SimpleNamespace(state_data=lambda n:(self.state,0,self.data,True),EXAMPLE_MODE=False,clients={'P':self.client},
                                  printer_config=lambda n:{'serial':'01S00'},EXAMPLE_DATA={'P':{}})
        self.store=Store(d/'db');self.controls=SimpleNamespace(pending={})
        self.job=self.store.add('P','Plate',make(d/'job.3mf'),'',options(1),'t');self.store.set_status(self.job['id'],'printing')
        self.skip=ObjectSkip(self.core,self.store,self.controls)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def sent(self):
        topic,payload=self.client.publish.call_args.args[:2];return topic,json.loads(payload)['print']

    def test_lists_objects_with_their_state(self):
        self.data={'s_obj':[92]}
        info=self.skip.objects('P')
        self.assertEqual((info['job'],info['reason']),('Plate',''))
        self.assertEqual([(o['name'],o['skipped']) for o in info['objects']],[('Cube #1',False),('Cube #2',True),('Bracket',False)])

    def test_sends_skip_objects_with_the_full_list(self):
        self.data={'s_obj':[92]}
        message=self.skip.skip('P',[75],True,'web')
        topic,command=self.sent()
        self.assertEqual(topic,'device/01S00/request')
        self.assertEqual((command['command'],command['obj_list']),('skip_objects',[75,92]))   # already-skipped kept
        self.assertIn(command['sequence_id'],self.controls.pending)                        # rejections get reported
        self.assertIn('Cube #1',message);self.assertEqual(self.store.events()[0]['title'],'Objects cancelled')

    def test_safety_checks(self):
        with self.assertRaisesRegex(ValueError,'Confirm'):self.skip.skip('P',[75],False,'web')
        with self.assertRaisesRegex(ValueError,'Choose'):self.skip.skip('P',[],True,'web')
        with self.assertRaisesRegex(ValueError,'Choose'):self.skip.skip('P',['75'],True,'web')
        with self.assertRaisesRegex(ValueError,'not on the plate'):self.skip.skip('P',[7],True,'web')       # other plate
        with self.assertRaisesRegex(ValueError,'At least one'):self.skip.skip('P',[75,92,110],True,'web')
        self.data={'s_obj':[75,92]}
        with self.assertRaisesRegex(ValueError,'At least one'):self.skip.skip('P',[110],True,'web')
        self.data={};self.state='IDLE'
        with self.assertRaisesRegex(ValueError,'running or paused'):self.skip.skip('P',[75],True,'web')
        self.client.publish.assert_not_called()

    def test_paused_print_allowed_and_demo(self):
        self.state='PAUSE';self.skip.skip('P',[110],True,'web')
        self.core.EXAMPLE_MODE=True;self.client.publish.reset_mock()
        self.assertIn('Demo only',self.skip.skip('P',[75],True,'web'))
        self.assertEqual(self.core.EXAMPLE_DATA['P']['s_obj'],[75]);self.client.publish.assert_not_called()

    def test_prints_not_started_by_the_app(self):
        self.store.set_status(self.job['id'],'finished')
        info=self.skip.objects('P');self.assertEqual(info['objects'],[]);self.assertIn("couldn't be read from the printer",info['reason'])
        with self.assertRaisesRegex(ValueError,"couldn't be read"):self.skip.skip('P',[75],True,'web')

    def test_print_started_elsewhere_reads_its_file_from_the_printer(self):
        import asyncio,shutil
        self.store.set_status(self.job['id'],'finished')       # no queue job: started from Bambu Studio / Handy
        self.data={'subtask_name':'Plate','gcode_file':'Plate.gcode.3mf','subtask_id':'77'}
        tried=[]
        def download(printer,remote,dest):
            tried.append(remote)
            if remote!='Plate.gcode.3mf':raise ValueError('missing')
            shutil.copy(self.job['asset'],dest)
        self.core.DATA_DIR=Path(self.tmp.name);self.skip=ObjectSkip(self.core,self.store,self.controls,download=download)
        asyncio.run(self.skip.fetch('P'))
        info=self.skip.objects('P')
        self.assertEqual((info['job'],info['reason']),('Plate',''));self.assertEqual(len(info['objects']),3)
        self.assertEqual(tried,['Plate.gcode.3mf'])
        asyncio.run(self.skip.fetch('P'));self.assertEqual(tried,['Plate.gcode.3mf'])   # copied once per print
        self.skip.skip('P',[75],True,'web');self.assertEqual(self.sent()[1]['obj_list'],[75])
        self.data={'subtask_name':'Other','gcode_file':'/data/Metadata/plate_1.gcode','subtask_id':'78'}   # a cloud print
        asyncio.run(self.skip.fetch('P'))
        self.assertIn("couldn't be read",self.skip.objects('P')['reason'])


class DashboardRouteTests(unittest.TestCase):
    def test_routes(self):
        import inspect,dashboard
        source=inspect.getsource(dashboard.Dashboard.__init__)
        for route in ("'/api/objects/{name}'","'/api/objects/{name}/plate.png'"):self.assertIn(route,source)
        # Registered before the generic /api/printers/{name}/{action} route can't swallow them (different prefix).
        self.assertNotIn("'/api/printers/{name}/objects'",source)


if __name__=='__main__':
    unittest.main()
