"""pm-doctor (#43): independent of the app, the same secret rules as diagnostics.py, one alert per incident,
frozen-app detection with a stack dump, opt-in auto-restart, restart loops, redacted reports, and a status page that
uses the dashboard password."""
import ast,io,json,re,sys,tempfile,threading,unittest,urllib.error,urllib.parse,urllib.request,zipfile
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).parents[1]
# A made-up bot-token-shaped string, assembled here so the source never contains one (GitHub push protection).
FAKE_BOT_TOKEN='.'.join(('MTIzNDU2Nzg5MDEyMzQ1Njc4OQ','GaBcDe','abcdefghijklmnopqrstuvwxyz'+'0123456789'))
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'doctor'))
import pm_doctor as doc

FAKE_GITHUB_TOKEN='ghp_'+'abcdefghijklmnopqrstuvwxyz0123'
SECRET_CONFIG={'printers':[{'name':'H2D','ip':'192.0.2.5','serial':'0948AB123456','access_code':'12345678'}],
               'discord_token':FAKE_BOT_TOKEN,'github':{'token':FAKE_GITHUB_TOKEN}}
SECRET_LOG=('rtsps://bblp:12345678@192.0.2.5:322/streaming/live/1 failed; token=abc123; '+FAKE_BOT_TOKEN+' '+FAKE_GITHUB_TOKEN
            +' https://discord.com/api/webhooks/1/abcdef')


def snapshot(active='active',healthy=True,restarts=0,startup_error='',**extra):
    s=dict(time=0,service=dict(active=active,sub='running',restarts=restarts,pid=10),healthy=healthy,release='1.8.0' if healthy else '',
           startup_error=startup_error,disk=[dict(label='system',free_gb=20.0,free_pct=60)],memory=dict(available_mb=2000,total_mb=4000,swap_used_mb=0),
           temperature=50.0,throttled='',network=dict(printers=[dict(name='H2D',mqtt=True)],discord=True,github=True,tailscale=''),updater={},doctor='1')
    s.update(extra);s['problems']=doc.problems(s);return s


class FakeChecks(doc.Checks):
    def __init__(self,data_dir):
        super().__init__(data_dir=data_dir,runner=self.record);self.queue=[];self.commands=[]
        self.log='Started 3d-printer-management\n'
    def snapshot(self):
        s=self.queue.pop(0) if len(self.queue)>1 else self.queue[0];s=json.loads(json.dumps(s));s['problems']=[tuple(p) for p in s['problems']];return s
    def record(self,command,timeout=10):   # never runs anything for real
        self.commands.append(command);return SimpleNamespace(stdout='',returncode=0)
    def journal(self,unit=doc.SERVICE,lines=300):return doc.redact_text(self.log)


class Clock:
    def __init__(self):self.now=1000.0
    def __call__(self):return self.now


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name);(self.dir/'data'/'logs').mkdir(parents=True)
        self.checks=FakeChecks(self.dir/'data');self.posts=[];self.clock=Clock()
    def tearDown(self):self.tmp.cleanup()
    def doctor(self,**settings):
        return doc.Doctor(self.checks,settings={'discord_webhook':'https://discord.com/api/webhooks/1/x',**settings},state=self.dir/'state',
                          clock=self.clock,sleep=lambda s:None,poster=lambda url,payload,headers=None:self.posts.append((url,payload,headers)))
    def ticks(self,doctor,*snaps,step=30):
        out=[]
        for s in snaps:
            self.checks.queue=[s];self.clock.now+=step;out.append(doctor.tick())
        return out

    def test_only_the_standard_library(self):
        tree=ast.parse((ROOT/'doctor'/'pm_doctor.py').read_text())
        modules={a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names}
        modules|={n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and n.module}
        self.assertEqual(modules-set(sys.stdlib_module_names),set())   # a broken app or venv can't break it

    def test_secret_rules_match_the_apps_diagnostics(self):
        import diagnostics
        self.assertEqual(doc.redact(SECRET_CONFIG),diagnostics.redact(SECRET_CONFIG))
        for pattern,_ in diagnostics.SECRET_TEXT:self.assertIn(pattern.pattern,[p.pattern for p,_ in doc.SECRET_TEXT])
        text=doc.redact_text(SECRET_LOG)
        for secret in ('12345678','abc123','ghp_','GaBcDe','webhooks/1/abcdef'):self.assertNotIn(secret,text)

    def test_failed_start_opens_one_incident_with_the_traceback_and_closes_once(self):
        d=self.doctor()
        error='Traceback (most recent call last):\n  File "core.py", line 9\nSyntaxError: invalid syntax'
        self.ticks(d,snapshot('failed',False,startup_error=error),snapshot('failed',False,startup_error=error),snapshot('failed',False,startup_error=error))
        self.assertEqual(len(self.posts),1)                                   # once, not every 30 s
        url,payload,_=self.posts[0];embed=payload['embeds'][0]
        self.assertIn('The app service is failed',embed['title']);self.assertIn('SyntaxError',embed['description'])
        incident=d.incidents()[0]
        self.assertTrue((self.dir/'state'/'incidents'/incident/'checks.json').exists())
        self.ticks(d,snapshot())
        self.assertEqual(len(self.posts),2);self.assertIn('back to normal',self.posts[1][1]['embeds'][0]['title'])

    def test_frozen_app_is_detected_with_a_stack_dump_and_not_restarted_by_default(self):
        d=self.doctor()
        self.checks.log='Started 3d-printer-management\nThread 0x0001 (most recent call first):\n  File "x.py", line 3 in wait\nCurrent thread 0x0002:\n  File "main.py", line 81'
        frozen=snapshot('active',False)
        out=self.ticks(d,frozen,frozen,frozen)
        self.assertEqual(self.posts,[]);self.assertIsNone(out[-1]['incident'])   # a slow start gets 2 minutes
        self.ticks(d,frozen)
        self.assertEqual(len(self.posts),1);self.assertIn('does not answer',self.posts[0][1]['embeds'][0]['description'])
        self.assertIn(['systemctl','kill','-s','SIGUSR1','--kill-whom=main',doc.SERVICE],self.checks.commands)
        self.assertIn('main.py',(self.dir/'state'/'incidents'/d.incidents()[0]/'stack-dump.txt').read_text())
        self.ticks(d,*[frozen]*10)
        self.assertNotIn(['systemctl','restart',doc.SERVICE],self.checks.commands)   # auto-restart is opt-in

    def test_opt_in_restart_after_the_set_minutes(self):
        d=self.doctor(restart_after_minutes=3)
        frozen=snapshot('active',False)
        self.ticks(d,*[frozen]*5)
        self.assertNotIn(['systemctl','restart',doc.SERVICE],self.checks.commands)   # 2.5 min
        self.ticks(d,frozen)
        self.assertEqual(self.checks.commands.count(['systemctl','restart',doc.SERVICE]),1)
        self.assertIn('Restarted the app',self.posts[-1][1]['embeds'][0]['title'])
        self.ticks(d,*[frozen]*5)
        self.assertEqual(self.checks.commands.count(['systemctl','restart',doc.SERVICE]),1)   # at most every 10 min

    def test_restart_loop_is_reported_once(self):
        d=self.doctor()
        snaps=[snapshot(restarts=n) for n in (0,1,2,3,4,5)]
        out=self.ticks(d,*snaps,step=60)
        self.assertIn('restart_loop',[k for k,_ in out[3]['problems']])
        self.assertEqual(len(self.posts),1);self.assertIn('restarted 3 times',self.posts[0][1]['embeds'][0]['title'])

    def test_github_issue_when_configured(self):
        d=self.doctor(github={'repository':'owner/repo','token':FAKE_GITHUB_TOKEN})
        self.ticks(d,snapshot('failed',False))
        github=[p for p in self.posts if 'api.github.com' in p[0]]
        self.assertEqual(github[0][0],'https://api.github.com/repos/owner/repo/issues')
        self.assertEqual(github[0][2]['Authorization'],'Bearer '+FAKE_GITHUB_TOKEN)
        self.assertNotIn('ghp_',github[0][1]['body'])

    def test_reports_never_contain_secrets(self):
        d=self.doctor()
        self.checks.log='Started 3d-printer-management\n'+SECRET_LOG
        (self.dir/'data'/'logs'/'management.log').write_text(SECRET_LOG)
        self.ticks(d,snapshot('failed',False,startup_error=SECRET_LOG))
        for data in (d.incident_zip(d.incidents()[0]),d.incident_zip(snap=snapshot(network=dict(SECRET_CONFIG,printers=[],discord=True,github=True,tailscale=''))) ):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                text=''.join(z.read(n).decode() for n in z.namelist())
            for secret in ('12345678','abc123','ghp_abcdef','GaBcDe','webhooks/1/abcdef'):self.assertNotIn(secret,text)
        with self.assertRaisesRegex(ValueError,'Unknown incident'):d.incident_zip('../../etc/passwd')

    def test_startup_error_and_power_parsing(self):
        checks=doc.Checks(data_dir=self.dir)
        journal='Started 3d-printer-management\nold\nStarted 3d-printer-management\nERROR Start-up failed; exiting\nRuntimeError: could not listen'
        self.assertIn('could not listen',checks.startup_error(journal))
        self.assertEqual(checks.startup_error('Started 3d-printer-management\nTraceback (most recent call last):\nboom\nStarted 3d-printer-management\nall good'),'')
        checks.run=lambda c,timeout=10:SimpleNamespace(stdout='throttled=0x50005',returncode=0)
        with patch.object(doc.shutil,'which',return_value='/usr/bin/vcgencmd'):
            self.assertEqual(checks.throttled(),'under-voltage now, throttled now')
        self.assertIn('power',[k for k,_ in doc.problems(snapshot(throttled='under-voltage now'))])


class StatusPageTests(unittest.TestCase):
    def setUp(self):
        from dashboard import password_hash
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'auth.json').write_text(json.dumps(password_hash('correct horse battery')))
        self.checks=FakeChecks(d);self.checks.queue=[snapshot()]
        self.doctor=doc.Doctor(self.checks,settings={},state=d/'state',poster=lambda *a:None);self.doctor.tick()
        self.server=doc.StatusServer(('127.0.0.1',0),doc.make_handler(self.doctor,d/'auth.json'))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_address[1]}'
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    def tearDown(self):self.server.shutdown();self.server.server_close();self.tmp.cleanup()
    def get(self,path):return self.opener.open(self.base+path).read().decode()
    def post(self,path,data):
        return self.opener.open(urllib.request.Request(self.base+path,data=urllib.parse.urlencode(data).encode(),method='POST'))

    def test_login_with_the_dashboard_password_then_status_and_restart(self):
        self.assertIn('Sign in',self.get('/'))
        with self.assertRaises(urllib.error.HTTPError) as wrong:self.post('/login',{'password':'nope'})
        self.assertEqual(wrong.exception.code,401)
        with self.assertRaises(urllib.error.HTTPError) as anonymous:self.post('/restart',{'csrf':'x'})   # not signed in
        self.assertEqual(anonymous.exception.code,403)
        page=self.post('/login',{'password':'correct horse battery'}).read().decode()
        self.assertIn('dashboard answers',page);self.assertIn('Restart the app',page)
        self.assertEqual(json.loads(self.get('/status.json'))['service']['active'],'active')
        with self.assertRaises(urllib.error.HTTPError) as forged:self.post('/restart',{'csrf':'forged'})
        self.assertEqual(forged.exception.code,403);self.assertNotIn(['systemctl','restart',doc.SERVICE],self.checks.commands)
        csrf=re.search(r"name='csrf' value='([^']+)'",page).group(1)
        self.post('/restart',{'csrf':csrf})
        self.assertIn(['systemctl','restart',doc.SERVICE],self.checks.commands)

    def test_report_download_needs_a_login(self):
        self.assertIn('Sign in',self.get('/incident.zip'))
        self.post('/login',{'password':'correct horse battery'})
        data=self.opener.open(self.base+'/incident.zip').read()
        self.assertIn('report/checks.json',zipfile.ZipFile(io.BytesIO(data)).namelist())


if __name__=='__main__':
    unittest.main()
