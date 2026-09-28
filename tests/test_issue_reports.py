import json,logging,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
import diagnostics,issue_reports
from issue_reports import IssueReports,compose


def fake_core(data_dir,config=None):
    return SimpleNamespace(DATA_DIR=data_dir,CONFIG=config or {},EXAMPLE_MODE=False,STARTED=time.monotonic(),log=logging.getLogger('test-reports'),
        last_seen={'H2D':time.time()},names=lambda:['H2D'],
        printer_config=lambda n:{'name':'H2D','model':'H2D','ip':'192.168.1.50','serial':'0948ABCDEF123','access_code':'12345678'},
        state_data=lambda n:('IDLE',0,{'hms':[{'attr':1}],'ip':'192.168.1.50'},True))


class Response:
    def __init__(self,status,data):self.status,self.data=status,data
    async def json(self):return self.data
    async def __aenter__(self):return self
    async def __aexit__(self,*a):return False


class Session:
    calls=[];status=201
    def __init__(self,**kw):pass
    async def __aenter__(self):return self
    async def __aexit__(self,*a):return False
    def post(self,url,headers,json):
        Session.calls.append((url,headers,json))
        return Response(Session.status,{'html_url':'https://github.com/o/r/issues/7'})


class IssueReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)
        diagnostics.recent_errors.clear();Session.calls=[];Session.status=201
        (self.data/'logs').mkdir()
        (self.data/'logs'/diagnostics.LOG_NAME).write_text('2026 INFO connected to 192.168.1.50 with access_code=12345678\n')
        self.core=fake_core(self.data);self.store=SimpleNamespace(event=MagicMock())
        self.reports=IssueReports(self.core,self.store)
    def tearDown(self):diagnostics.recent_errors.clear();self.tmp.cleanup()

    def test_compose_has_context_but_no_private_details(self):
        diagnostics.recent_errors.append({'time':time.time(),'level':'ERROR','logger':'x','error_id':'abcd1234','text':'boom at 10.0.0.2 token=secretvalue'})
        title,body=compose(self.core,'AMS 0 empty','Printer at 192.168.1.50 shows nothing','dashboard administrator')
        self.assertEqual(title,'[Report] AMS 0 empty')
        for present in ('| H2D | H2D | True | IDLE | 0 |','abcd1234','Last log lines','What happened'):self.assertIn(present,body)
        for absent in ('192.168.1.50','10.0.0.2','12345678','secretvalue','0948ABCDEF123'):self.assertNotIn(absent,body)

    async def test_send_creates_github_issue(self):
        self.reports.configure({'repository':'o/r','token':'ghp_test'})
        with patch('issue_reports.aiohttp.ClientSession',Session):
            url=await self.reports.send('Movement broken','Jog X does nothing on the H2D','dashboard administrator')
        self.assertEqual(url,'https://github.com/o/r/issues/7')
        (endpoint,headers,payload),=Session.calls
        self.assertEqual(endpoint,'https://api.github.com/repos/o/r/issues');self.assertEqual(headers['Authorization'],'Bearer ghp_test')
        self.assertEqual(payload['title'],'[Report] Movement broken');self.store.event.assert_called_once()
        with self.assertRaisesRegex(ValueError,'Wait'):await self.reports.send('Another one','Second report too soon','x')

    async def test_errors_are_clear_and_do_not_block_retry(self):
        with self.assertRaisesRegex(ValueError,'token'):await self.reports.send('No token set','Nothing configured yet','x')
        self.reports.configure({'repository':'o/r','token':'bad'});Session.status=401
        with patch('issue_reports.aiohttp.ClientSession',Session):
            with self.assertRaisesRegex(ValueError,'401'):await self.reports.send('Bad token','Token is expired now','x')
            Session.status=201
            self.assertTrue(await self.reports.send('Retry works','Retry after fixing token','x'))
        for title,description in [('Hi','long enough description'),('Valid title','short')]:
            self.reports.last_sent=0
            with self.assertRaises(ValueError):await self.reports.send(title,description,'x')

    def test_settings_validation_and_token_privacy(self):
        self.assertEqual(self.reports.public()['repository'],issue_reports.DEFAULT_REPOSITORY)
        for bad in [{'repository':'not a repo'},{'repository':'o/r','destination':'fax'},{'repository':'o/r','token':'has space'}]:
            with self.assertRaises(ValueError):self.reports.configure(bad)
        public=self.reports.configure({'repository':'o/r','token':'ghp_x'})
        self.assertTrue(public['has_token']);self.assertNotIn('ghp_x',json.dumps(public))
        self.assertEqual(IssueReports(self.core,self.store).config['token'],'ghp_x')
        self.assertEqual((self.data/'issue-reports.json').stat().st_mode&0o777,0o600)
        self.reports.configure({'repository':'o/r'});self.assertEqual(self.reports.config['token'],'ghp_x')
        self.reports.configure({'repository':'o/r','clear_token':True});self.assertFalse(self.reports.public()['has_token'])

    def test_config_json_defaults(self):
        core=fake_core(self.data,{'issue_reports':{'repository':'team/printers','token':'preset'}})
        self.assertEqual(IssueReports(core,self.store).config['repository'],'team/printers')


if __name__=='__main__':
    unittest.main()
