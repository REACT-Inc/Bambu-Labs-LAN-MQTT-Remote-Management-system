import json,time,unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
import filament_sides as fs
from printer_controls import Controls,prepare

# Shaped like an H2D report: AMS 0 feeds the left nozzle, AMS 1 the right, AMS 2 goes through a filament switch.
H2D={'device':{'extruder':{'state':2|1<<4,'info':[{'id':0,'snow':1<<8|2},{'id':1,'snow':254<<8}]}},
     'ams':{'ams':[{'id':'0','info':'1101'},{'id':'1','info':'1001'},{'id':'2','info':'E01'}],'tray_now':'6'},
     'vir_slot':[{'id':'254','tray_type':'PETG'},{'id':'255','tray_type':'PLA'}]}


class DecodeTests(unittest.TestCase):
    def test_h2d_sides(self):
        self.assertTrue(fs.is_dual(H2D));self.assertEqual(fs.active_nozzle(H2D),fs.LEFT)
        self.assertEqual([fs.ams_nozzle(u) for u in H2D['ams']['ams']],[fs.LEFT,fs.RIGHT,'both'])
        self.assertEqual([(a,t['tray_type'],n) for a,t,n in fs.external_spools(H2D)],[(255,'PLA',fs.RIGHT),(254,'PETG',fs.LEFT)])
        self.assertEqual(fs.loaded_slots(H2D),{(1,2):fs.RIGHT,(254,0):fs.LEFT})
        self.assertEqual((fs.side_label(0),fs.side_label(1),fs.side_label('both'),fs.side_label(None)),('Right nozzle','Left nozzle','Both nozzles',''))

    def test_single_nozzle_printers_are_unchanged(self):
        a1={'ams':{'ams':[{'id':'0','info':'1101'}]},'vt_tray':{'tray_type':'PLA'}}
        self.assertFalse(fs.is_dual(a1));self.assertIsNone(fs.active_nozzle(a1))
        self.assertEqual(fs.external_spools(a1),[(255,{'tray_type':'PLA'},None)])
        self.assertEqual(fs.loaded_slots(a1),{});self.assertEqual(fs.summary(a1)['ams'],{})
        self.assertEqual(fs.external_spools({}),[])

    def test_odd_values(self):
        self.assertIsNone(fs.ams_nozzle({}));self.assertIsNone(fs.ams_nozzle({'info':'zz'}))
        self.assertEqual(fs.external_id('65024'),254)           # encoded (254 << 8) | 0 ... as Bambu Studio decodes it
        nothing={'device':{'extruder':{'state':2,'info':[{'id':0,'snow':0xFFFF},{'id':1,'snow':'bad'}]}}}
        self.assertEqual(fs.loaded_slots(nothing),{});self.assertEqual(fs.active_nozzle(nothing),fs.RIGHT)

    def test_summary_is_json(self):
        summary=json.loads(json.dumps(fs.summary(H2D)))
        self.assertEqual(summary['ams'],{'0':1,'1':0,'2':'both'})
        self.assertIn({'ams':254,'slot':0,'nozzle':1},summary['loaded'])


class LeftExternalSpoolTests(unittest.TestCase):
    def setUp(self):
        self.core=SimpleNamespace(names=lambda:['H2D'],printer_config=lambda n:{'serial':'s','name':n},EXAMPLE_MODE=False,
            last_seen={'H2D':time.time()},state_data=lambda n:('IDLE',0,H2D,True),clients={'H2D':MagicMock()})
        self.client=self.core.clients['H2D'];self.client.publish.return_value.rc=0
        self.controls=Controls(self.core,SimpleNamespace(jobs=lambda:[],event=MagicMock()))

    def test_left_external_spool_uses_ams_254(self):
        command,param,label=prepare(self.core,'H2D','filament',{'ams':'external_left','type':'PETG','color':'#112233'})
        self.assertEqual({k:param[0][k] for k in ('ams_id','slot_id','tray_id')},{'ams_id':254,'slot_id':0,'tray_id':254})
        self.assertIn('Left external spool',label)
        right=prepare(self.core,'H2D','filament',{'ams':'external','type':'PLA','color':'#112233'})[1][0]
        self.assertEqual((right['ams_id'],right['tray_id']),(255,254))

    def test_slot_loaded_in_either_nozzle_is_protected_while_printing(self):
        self.core.state_data=lambda n:('RUNNING',0,H2D,True)
        with self.assertRaisesRegex(ValueError,'feeding the current print'):
            self.controls.apply('H2D','filament',{'ams':1,'slot':2,'type':'PLA','color':'#FFFFFF'},confirmed=True)
        with self.assertRaisesRegex(ValueError,'feeding the current print'):
            self.controls.apply('H2D','filament',{'ams':'external_left','type':'PLA','color':'#FFFFFF'},confirmed=True)
        self.controls.apply('H2D','filament',{'ams':0,'slot':3,'type':'PLA','color':'#FFFFFF'},confirmed=True)
        self.client.publish.assert_called_once()


if __name__=='__main__':
    unittest.main()
