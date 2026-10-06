"""Commands show their result sooner (#60): a full report is requested shortly after each command (rate-limited per
printer), and the dashboard looks again soon after every control."""
import importlib.util,json,os,re,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).parents[1]


class Client:
    def __init__(self):self.sent=[];self.done=threading.Event()
    def is_connected(self):return True
    def publish(self,topic,payload,qos=0):
        self.sent.append((topic,json.loads(payload)))
        if 'pushing' in self.sent[-1][1]:self.done.set()
        return SimpleNamespace(rc=0)


class ReportRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'printers':[{'name':'A1','ip':'192.0.2.1','serial':'03919A','access_code':'1'}]}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('test_core_speed',ROOT/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
        self.client=self.core.clients['A1']=Client()
    def tearDown(self):self.tmp.cleanup()

    def pushes(self):return [b for t,b in self.client.sent if 'pushing' in b]

    def test_a_full_report_is_requested_after_a_command(self):
        self.assertTrue(self.core.request_report('A1',delay=0.01))
        self.assertTrue(self.client.done.wait(2))
        topic,body=self.client.sent[-1]
        self.assertEqual(topic,'device/03919A/request');self.assertEqual(body['pushing']['command'],'pushall')

    def test_rate_limited_per_printer(self):
        self.assertTrue(self.core.request_report('A1',delay=0.01))
        self.assertFalse(self.core.request_report('A1',delay=0.01))   # within 10 s: not again
        self.core.report_requested['A1']-=self.core.REPORT_GAP+1
        self.assertTrue(self.core.request_report('A1',delay=0.01))
        self.assertFalse(self.core.request_report('unknown printer'))

    def test_light_and_print_actions_request_it(self):
        with patch.object(self.core,'request_report') as requested:
            self.core.publish_light('A1',True);self.core.publish_action('A1','pause')
        self.assertEqual([c.args[0] for c in requested.call_args_list],['A1','A1'])

    def test_printer_controls_request_it(self):
        from printer_controls import Controls
        store=SimpleNamespace(event=lambda *a:None,jobs=lambda:[])
        core=self.core;core.state_data=lambda name:('IDLE',0,{},True)
        controls=Controls(core,store)
        with patch.object(core,'request_report') as requested:
            controls.apply('A1','speed','sport',confirmed=True,author='test')
        requested.assert_called_once_with('A1')


class DashboardPollingTests(unittest.TestCase):
    def test_dashboard_looks_again_soon_after_every_control(self):
        app=(ROOT/'static/app.js').read_text();controls=(ROOT/'static/controls.js').read_text()
        delays=[int(x) for x in re.search(r'function pollSoon\(\)\{for\(const ms of \[([0-9,]+)\]',app).group(1).split(',')]
        self.assertEqual(delays[0],600);self.assertGreaterEqual(delays[-1],15000)
        self.assertIn("notice(r.message||label||'Submitted.');pollSoon();",controls)   # temperatures, speed, filament…
        self.assertIn("/move',{value,axis,confirmed:true,homed:true});pollSoon();",controls)
        self.assertIn("/home',{confirmed:true});pollSoon();",controls)


if __name__=='__main__':
    unittest.main()
