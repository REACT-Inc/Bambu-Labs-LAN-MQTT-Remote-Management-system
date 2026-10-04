"""Multi-hotend support for dual-nozzle printers (H2D / H2D Pro / H2C / X2D)."""
import importlib.util,json,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
import filament_sides as fs
from printer_controls import Controls,prepare

# Shaped like an H2C report: left nozzle in use with a 0.6 hardened high-flow hotend, two hotends parked in the rack.
H2C={'device':{'extruder':{'state':2|1<<4,'info':[{'id':0,'snow':255<<8,'temp':28,'hnow':0},{'id':1,'snow':2,'temp':(220<<16)|214,'hnow':1}]},
               'nozzle':{'info':[{'id':0,'diameter':0.4,'type':'HS00'},{'id':1,'diameter':0.6,'type':'HH01'},
                                 {'id':0x10,'diameter':0.4,'type':'HH05','wear':3},{'id':0x11,'diameter':0.2,'type':'stainless_steel'}]}}}


class DecodeTests(unittest.TestCase):
    def test_nozzles(self):
        right,left=fs.nozzles(H2C)
        self.assertEqual((right['side'],right['current'],right['target'],right['active'],right['filament']),('Right',28,0,False,'Right external spool'))
        self.assertEqual((left['side'],left['current'],left['target'],left['active'],left['filament']),('Left',214,220,True,'AMS 1 slot 3'))
        self.assertEqual((right['hotend']['label'],left['hotend']['label']),('0.4 mm stainless steel','0.6 mm hardened steel, high flow'))
        self.assertEqual(fs.nozzles({'nozzle_temper':200}),[])                      # single-nozzle printers unchanged

    def test_rack_and_odd_values(self):
        rack=fs.hotends(H2C)[1]
        self.assertEqual([(h['slot'],h['label'],h['wear']) for h in rack],[(0,'0.4 mm tungsten carbide, high flow',3),(1,'0.2 mm stainless steel',None)])
        self.assertIsNone(fs.hotend({'id':'x'}));self.assertEqual(fs.hotend({'id':2,'type':'N/A'})['label'],'not reported')
        summary=json.loads(json.dumps(fs.summary(H2C)))
        self.assertEqual((len(summary['nozzles']),len(summary['rack'])),(2,2))


class ControlTests(unittest.TestCase):
    def setUp(self):
        configs={'H2D':{'model':'H2D','serial':'s'},'Mini':{'model':'A1 mini','serial':'m'}}
        self.core=SimpleNamespace(names=lambda:list(configs),printer_config=configs.get,EXAMPLE_MODE=False,last_seen={},
            state_data=lambda n:('IDLE',0,dict(H2C) if n=='H2D' else {},True),clients={n:MagicMock() for n in configs})
        for c in self.core.clients.values():c.publish.return_value.rc=0
        self.controls=Controls(self.core,SimpleNamespace(jobs=lambda:[],event=MagicMock()))

    def test_each_nozzle_on_its_own(self):
        self.assertEqual(prepare(self.core,'H2D','nozzle_left',220)[1],[{'command':'set_nozzle_temp','extruder_index':1,'target_temp':220}])
        self.assertEqual(prepare(self.core,'H2D','nozzle_right',0)[1],[{'command':'set_nozzle_temp','extruder_index':0,'target_temp':0}])
        with self.assertRaises(ValueError):prepare(self.core,'H2D','nozzle_left',351)          # H2D nozzle limit
        with self.assertRaisesRegex(ValueError,'single nozzle'):prepare(self.core,'Mini','nozzle_left',200)
        self.controls.apply('H2D','nozzle_left',230,confirmed=True)
        sent=json.loads(self.core.clients['H2D'].publish.call_args.args[1])['print']
        self.assertEqual((sent['command'],sent['extruder_index'],sent['target_temp']),('set_nozzle_temp',1,230))


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'guild_ids':[1],'demo':True}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('test_core_hotend',Path(__file__).parents[1]/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
    async def asyncTearDown(self):await self.core.bot.close();self.tmp.cleanup()

    def test_printer_card_and_demo_controls(self):
        fields={f.name:f.value for f in self.core.printer_embed('Demo H2D').fields}
        self.assertIn('Left nozzle • in use',fields);self.assertIn('214°C → 220°C',fields['Left nozzle • in use'])
        self.assertIn('0.4 mm hardened steel, high flow',fields['Left nozzle • in use'])
        self.assertIn('28°C → off',fields['Right nozzle']);self.assertNotIn('Nozzle',fields)
        self.assertIn('Nozzle',{f.name for f in self.core.printer_embed('Demo A1').fields})   # single nozzle unchanged
        controls=Controls(self.core,SimpleNamespace(jobs=lambda:[],event=MagicMock()))
        controls.apply('Demo H2D','nozzle_right',200,confirmed=True)
        self.assertEqual(fs.nozzles(self.core.EXAMPLE_DATA['Demo H2D'])[0]['target'],200)


if __name__=='__main__':
    unittest.main()
