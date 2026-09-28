import io,json,logging,tempfile,time,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)
        self.log=logging.getLogger('test-diagnostics');self.log.propagate=True
        self.root=logging.getLogger();self.before=list(self.root.handlers)
        diagnostics._installed=False;diagnostics.recent_errors.clear()
    def tearDown(self):
        for handler in self.root.handlers[:]:
            if handler not in self.before:self.root.removeHandler(handler);handler.close()
        diagnostics._installed=False;diagnostics.recent_errors.clear();self.tmp.cleanup()

    def test_redacts_secrets_but_keeps_useful_fields(self):
        config={'discord_token':'abc','printers':[{'name':'H2D','ip':'10.0.0.5','serial':'0948ABCDEF123','access_code':'12345678'}],
                'github':{'token':'ghp_'+'a'*36,'automatic':True,'has_token':True},'password':{'hash':'x','salt':'y'},'port':8080}
        r=diagnostics.redact(config)
        self.assertEqual(r['discord_token'],'[redacted]');self.assertEqual(r['printers'][0]['access_code'],'[redacted]')
        self.assertEqual(r['github']['token'],'[redacted]');self.assertEqual(r['password'],'[redacted]')
        self.assertEqual(r['printers'][0]['ip'],'10.0.0.5');self.assertEqual(r['printers'][0]['serial'],'094…123')
        self.assertIs(r['github']['has_token'],True);self.assertEqual(r['port'],8080)
        text=diagnostics.redact_text('access_code=12345678 using ghp_'+'b'*36+' and "password": "hunter2"')
        self.assertNotIn('12345678',text);self.assertNotIn('ghp_',text);self.assertNotIn('hunter2',text)

    def test_log_file_error_ids_and_recent_errors(self):
        diagnostics.setup(self.data,self.log);diagnostics.setup(self.data,self.log)
        try:raise RuntimeError('printer exploded with access_code=87654321')
        except RuntimeError as exc:error_id=diagnostics.log_error(self.log,'Command failed',exc)
        self.assertRegex(error_id,r'^[0-9a-f]{8}$')
        for h in self.root.handlers:h.flush()
        text=(self.data/'logs'/diagnostics.LOG_NAME).read_text()
        self.assertIn(error_id,text);self.assertIn('RuntimeError',text);self.assertNotIn('87654321',text)
        self.assertEqual(len(diagnostics.recent_errors),1)
        self.assertEqual(diagnostics.recent_errors[0]['error_id'],error_id)
        self.assertEqual(sum(isinstance(h,diagnostics.RecentErrors) for h in self.root.handlers),1)

    def test_report_contents(self):
        diagnostics.setup(self.data,self.log);self.log.error('Printer offline')
        for h in self.root.handlers:h.flush()
        core=SimpleNamespace(DATA_DIR=self.data,CONFIG={'discord_token':'secret-value','printers':[{'name':'A1','serial':'0300AAAAAA999','access_code':'1'}]},
            settings={},EXAMPLE_MODE=False,STARTED=time.monotonic(),last_seen={'A1':time.time()},names=lambda:['A1'],
            printer_config=lambda n:{'name':'A1','serial':'0300AAAAAA999','ip':'10.0.0.9'},
            state_data=lambda n:('IDLE',0,{'gcode_state':'IDLE','hms':[]},True))
        store=SimpleNamespace(events=lambda:[{'title':'Control submitted','detail':'Move X'}])
        name,data=diagnostics.build_report(core,store)
        self.assertTrue(name.startswith('diagnostics-') and name.endswith('.zip'))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files=set(z.namelist())
            self.assertTrue({'summary.json','printers.json','config.json','recent_errors.json','events.json','logs/management.log'}<=files)
            self.assertNotIn('secret-value',z.read('config.json').decode())
            printer=json.loads(z.read('printers.json'))[0]
            self.assertEqual((printer['state'],printer['connected'],printer['serial']),('IDLE',True,'030…999'))
            self.assertIn('Printer offline',z.read('logs/management.log').decode())
            self.assertEqual(len(json.loads(z.read('recent_errors.json'))),1)

    def test_report_without_logs(self):
        core=SimpleNamespace(DATA_DIR=self.data,CONFIG={},settings={},names=lambda:[])
        with zipfile.ZipFile(io.BytesIO(diagnostics.build_report(core)[1])) as z:
            self.assertIn('logs/README.txt',z.namelist())


if __name__=='__main__':
    unittest.main()
