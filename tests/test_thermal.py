import json,time,unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from printer_controls import Controls,prepare
from thermal_controls import fans

class ThermalTests(unittest.TestCase):
    def setUp(self):
        self.data={'device':{'airduct':{'modeCur':0,'modeList':[{'modeId':0,'ctrl':[16,32,160],'off':[]}],
            'parts':[{'id':i<<4,'func':i,'range':100<<16,'state':40} for i in [0,1,2,3,4,5,6,10]]}}}
        self.client=MagicMock();self.client.publish.return_value.rc=0
        self.core=SimpleNamespace(names=lambda:['BOB'],printer_config=lambda n:{'model':'H2D','serial':'serial'},
            state_data=lambda n:('IDLE',0,self.data,True),EXAMPLE_MODE=False,clients={'BOB':self.client},last_seen={'BOB':time.time()})
        self.controls=Controls(self.core,SimpleNamespace(event=MagicMock(),jobs=lambda:[]))
    def sent(self):return [json.loads(c.args[1])['print'] for c in self.client.publish.call_args_list]
    def test_new_fan_protocol_uses_percent_and_reported_id(self):
        self.controls.apply('BOB','fan_auxiliary2',55,confirmed=True)
        data=self.sent()[0];self.assertEqual(data['command'],'set_fan');self.assertEqual(data['fan_index'],10);self.assertEqual(data['speed'],55)
        self.assertNotIn('param',data)
    def test_automatic_fans_blocked_and_all_skips_them(self):
        with self.assertRaises(ValueError):self.controls.apply('BOB','fan_chamber',80,confirmed=True)
        self.client.publish.assert_not_called()
        label=self.controls.apply('BOB','fanall',80,confirmed=True)
        self.assertEqual([p['fan_index'] for p in self.sent()],[1,2,10]);self.assertIn('unchanged',label)
    def test_legacy_a1_has_only_part_fan(self):
        self.data={};self.core.printer_config=lambda n:{'model':'A1 Mini','serial':'serial'}
        self.controls.apply('BOB','fanall',100,confirmed=True)
        self.assertEqual(self.sent()[0]['param'],'M106 P1 S255\n');self.assertEqual(len(self.sent()),1)
        with self.assertRaises(ValueError):prepare(self.core,'BOB','fan_auxiliary',50)
        with self.assertRaises(ValueError):prepare(self.core,'BOB','chamber',50)
    def test_legacy_fans_report_their_current_percent(self):
        # Older reports give 0-15; the dashboard shows the percentage so the slider doesn't snap back to 0.
        self.data={'cooling_fan_speed':'15','big_fan1_speed':'7','big_fan2_speed':'0'}
        self.core.printer_config=lambda n:{'model':'X1C','serial':'serial'}
        self.assertEqual({f['key']:f['percent'] for f in fans(self.core,'BOB')},{'part':100,'auxiliary':40,'chamber':0})
        self.data={'cooling_fan_speed':'junk'}
        self.assertIsNone(fans(self.core,'BOB')[0]['percent'])
    def test_demo_fan_targets_are_kept_per_fan(self):
        self.data={};self.core.EXAMPLE_MODE=True;self.core.EXAMPLE_DATA={'BOB':self.data}
        self.core.printer_config=lambda n:{'model':'X1C','serial':'serial'}
        self.controls.apply('BOB','fan_part',40,confirmed=True);self.controls.apply('BOB','fan_auxiliary',70,confirmed=True)
        self.assertEqual(self.data['demo_fan_targets'],{'1':40,'2':70})
        self.assertEqual({f['key']:f['percent'] for f in fans(self.core,'BOB')},{'part':40,'auxiliary':70,'chamber':None})
    def test_chamber_command_and_limits(self):
        self.controls.apply('BOB','chamber',60,confirmed=True)
        self.assertEqual(self.sent()[0]['command'],'set_ctt');self.assertEqual(self.sent()[0]['ctt_val'],60)
        for invalid in [-1,1,39,66,True,float('nan')]:
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):prepare(self.core,'BOB','chamber',invalid)
        self.assertEqual(prepare(self.core,'BOB','chamber',0)[1][0]['ctt_val'],0)
    def test_range_and_partial_submission(self):
        self.data['device']['airduct']['parts'][1]['range']=(100<<16)|20
        with self.assertRaises(ValueError):self.controls.apply('BOB','fanall',10,confirmed=True)
        self.client.publish.assert_not_called()
        self.client.publish.side_effect=[SimpleNamespace(rc=0),SimpleNamespace(rc=1)]
        with self.assertRaisesRegex(ValueError,'after 1/3'):self.controls.apply('BOB','fanall',50,confirmed=True)
        self.assertEqual(self.client.publish.call_count,2)
    def test_confirmation_and_maintenance(self):
        with self.assertRaises(ValueError):self.controls.apply('BOB','fanall',50)
        self.core.update_pending=lambda:True
        with self.assertRaises(ValueError):self.controls.apply('BOB','chamber',60,confirmed=True)
        self.client.publish.assert_not_called()
    def test_correlated_chamber_rejection_is_explained(self):
        self.controls.apply('BOB','chamber',60,confirmed=True)
        command=self.sent()[0]
        self.controls.handle_response('BOB',{'sequence_id':command['sequence_id'],'command':'set_ctt','errno':-2})
        detail=self.controls.store.event.call_args.args
        self.assertEqual(detail[1],'Control rejected by printer');self.assertIn('PLA/PETG/TPU',detail[2])
        count=self.controls.store.event.call_count
        self.controls.handle_response('BOB',{'sequence_id':'unrelated','command':'set_ctt','errno':-2})
        self.assertEqual(self.controls.store.event.call_count,count)
