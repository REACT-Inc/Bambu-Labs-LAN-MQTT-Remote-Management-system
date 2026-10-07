"""Clearing printer errors and health alerts (#34): Bambu Studio's exact messages, confirmation, honest results,
HMS alerts only hidden (never muted on the printer), logging, and the queue after clearing a FAILED printer."""
import asyncio,json,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from printer_alerts import Alerts

SPAGHETTI=0x0C008001   # any 8-hex print error; the description comes from the error catalog (or "Unrecognized")
HMS={'attr':0x07000200,'code':0x00020002}


class Printer:
    """A printer that drops its error when clean_print_error arrives (or doesn't, when the cause is still there)."""
    def __init__(self,state='FAILED',error=SPAGHETTI,hms=None,clears=True):
        self.data={'gcode_state':state,'print_error':error,'subtask_id':'1234567','hms':list(hms or [])}
        self.clears,self.sent=clears,[]
    def is_connected(self):return True
    def publish(self,topic,payload,qos=0):
        body=json.loads(payload);self.sent.append((topic,body))
        if self.clears and body.get('print',{}).get('command')=='clean_print_error':self.data['print_error']=0
        return SimpleNamespace(rc=0)


class Core:
    EXAMPLE_MODE=False
    def __init__(self,printer):
        self.printer,self.settings,self.clients=printer,{},{'H2D':printer}
    def names(self):return ['H2D']
    def printer_config(self,name):return {'serial':'0948AB','error_model':'094'}
    def state_data(self,name):
        d=self.printer.data;return d['gcode_state'],d['print_error'],dict(d),True
    def save_settings(self,updated):self.settings=dict(updated)


class Clock:
    def __init__(self):self.now=0.0
    def __call__(self):return self.now


class AlertTests(unittest.IsolatedAsyncioTestCase):
    def make(self,**kw):
        self.printer=Printer(**kw);self.core=Core(self.printer);self.store=SimpleNamespace(event=MagicMock());self.clock=Clock()
        async def sleep(s):self.clock.now+=s
        return Alerts(self.core,self.store,clock=self.clock,sleep=sleep)

    def test_lists_errors_and_health_alerts_with_descriptions(self):
        alerts=self.make(hms=[HMS])
        items=alerts.current('H2D')
        self.assertEqual([a['kind'] for a in items],['error','hms'])
        self.assertEqual(items[0]['id'],'error:0C008001');self.assertEqual(items[1]['code'],'0700020000020002')
        self.assertTrue(all(a['message'] and a['url'].startswith('https://e.bambulab.com/') for a in items))

    async def test_sends_bambu_studios_two_messages_and_says_cleared(self):
        alerts=self.make()
        result=await alerts.clear('H2D','all',True,author='Ana')
        topics={t for t,_ in self.printer.sent};self.assertEqual(topics,{'device/0948AB/request'})
        clean,close=[b for _,b in self.printer.sent]
        self.assertEqual({k:v for k,v in clean['print'].items() if k!='sequence_id'},
                         {'command':'clean_print_error','subtask_id':'1234567','print_error':SPAGHETTI})
        self.assertEqual({k:v for k,v in close['system'].items() if k!='sequence_id'},
                         {'command':'uiop','name':'print_error','action':'close','source':1,'type':'dialog','err':'0C008001'})
        self.assertEqual(result['cleared'],['error:0C008001']);self.assertEqual(result['still'],[])
        self.assertIn('Cleared',result['message'])
        events={c.args[1]:c.args[2] for c in self.store.event.call_args_list}
        self.assertIn('0C008001',events['Printer error cleared']);self.assertIn('Ana',events['Printer error cleared'])

    async def test_an_error_whose_cause_remains_is_reported_honestly(self):
        alerts=self.make(clears=False)
        result=await alerts.clear('H2D',['error:0C008001'],True,author='Ana',wait=5)
        self.assertEqual(result['still'],['error:0C008001']);self.assertNotIn('Cleared',result['message'])
        self.assertIn('still reports 0C008001',result['message']);self.assertGreaterEqual(self.clock.now,5)   # waited for reports
        self.assertEqual(self.store.event.call_args.args[1],'Printer still reports error')

    async def test_failed_without_an_error_is_simply_ready(self):
        from queueing import Engine
        alerts=self.make(error=0)                               # FAILED after a cancelled print: idle and ready
        self.assertEqual(alerts.current('H2D'),[])              # nothing to clear
        engine=Engine.__new__(Engine);engine.core=SimpleNamespace(update_pending=lambda:False,state_data=self.core.state_data,
                                                                    EXAMPLE_MODE=True,last_seen={});engine.alerts=alerts
        engine.ready('H2D')

    async def test_confirmation_is_required(self):
        alerts=self.make()
        with self.assertRaisesRegex(ValueError,'only dismisses the message'):await alerts.clear('H2D','all',False)
        with self.assertRaisesRegex(ValueError,'no longer reports'):await alerts.clear('H2D',['error:FFFFFFFF'],True)
        self.assertEqual(self.printer.sent,[])

    async def test_health_alerts_are_hidden_not_muted_on_the_printer(self):
        alerts=self.make(error=0,state='IDLE',hms=[HMS])
        result=await alerts.clear('H2D','all',True,author='Ana')
        self.assertEqual(self.printer.sent,[])                         # no idle_ignore: that would mute it for good
        self.assertIn('Dismissed 1 health alert',result['message'])
        self.assertEqual(alerts.current('H2D'),[]);self.assertTrue(alerts.current('H2D',include_dismissed=True)[0]['dismissed'])
        self.printer.data['hms']=[];alerts.current('H2D')               # the printer stops reporting it…
        self.printer.data['hms']=[HMS]
        self.assertEqual(len(alerts.current('H2D')),1)                  # …so it shows again when it comes back
        self.assertEqual(self.core.settings['dismissed_hms'],{})

    async def test_failed_printer_can_start_the_next_job_after_its_error_is_cleared(self):
        alerts=self.make()
        state,error,data,_=self.core.state_data('H2D')
        self.assertFalse(alerts.ready_after_clear('H2D',state,error,data))
        await alerts.clear('H2D','all',True)
        state,error,data,_=self.core.state_data('H2D')
        self.assertEqual((state,error),('FAILED',0))
        self.assertTrue(alerts.ready_after_clear('H2D',state,error,data))
        self.assertFalse(alerts.ready_after_clear('H2D',state,error,dict(data,subtask_id='999')))   # a later failed print

    async def test_queue_uses_it(self):
        from queueing import Engine
        alerts=self.make()
        engine=Engine.__new__(Engine);engine.core=SimpleNamespace(update_pending=lambda:False,state_data=self.core.state_data,
                                                                    EXAMPLE_MODE=True,last_seen={});engine.alerts=alerts
        with self.assertRaisesRegex(ValueError,'reports error'):engine.ready('H2D')   # FAILED with an error code
        await alerts.clear('H2D','all',True)
        engine.ready('H2D')   # no "Start ignoring error" needed any more


class DiscordTests(unittest.TestCase):
    def test_command_and_notification_button(self):
        import importlib.util,os,tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1],'demo':True}))
            with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
                spec=importlib.util.spec_from_file_location('test_core_clear',Path(__file__).parents[1]/'core.py')
                core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
            from discord_Intergration.clear_errors_discord import install
            listed=[]
            fake=SimpleNamespace(current=lambda name:listed)
            install(core,fake)
            self.assertIsNotNone(core.bot.tree.get_command('clearerror'))
            self.assertEqual(core.command_level('clearerror'),'everyone')   # like /pause and /stop
            async def views():
                listed.append({'id':'error:0C008001'})
                return (core.notification_view('H2D','🛑 Printer error'),core.notification_view('H2D','✅ Print complete'))
            button,none=asyncio.run(views())
            self.assertEqual(button.children[0].label,'Clear error…');self.assertIsNone(none)
            asyncio.run(core.bot.close())


if __name__=='__main__':
    unittest.main()
