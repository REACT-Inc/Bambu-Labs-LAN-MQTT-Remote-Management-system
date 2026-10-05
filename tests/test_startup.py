"""Start-up never hangs: a failed start logs why and exits (so systemd restarts it), an address that isn't up yet
doesn't stop the dashboard, and the AI helper starts only once the app is up (1.7.2/1.7.3 hung on the Pi when
start-up failed while the AI HAT model was still loading)."""
import asyncio,json,os,socket,subprocess,sys,tempfile,time,unittest,urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).parents[1]))
ROOT=Path(__file__).parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name);self.port=free_port()
        (self.dir/'data').mkdir()
        # A slow AI helper (like the AI HAT loading its model): the case that used to hang a failed start.
        helper=self.dir/'slowpy';helper.write_text('#!/usr/bin/env python3\nimport json,sys,time\ntime.sleep(30)\n'
                                                   'print(json.dumps({"ready":True,"backend":"hailo"}),flush=True)\nsys.stdin.read()\n')
        helper.chmod(0o755);(self.dir/'m.hef').write_bytes(b'x')
        self.fd={'enabled':True,'model':str(self.dir/'m.hef'),'python':str(helper),'action':'notify'}
    def tearDown(self):self.tmp.cleanup()

    def start(self,listen):
        (self.dir/'config.json').write_text(json.dumps({'port':self.port,'listen':listen,'failure_detection':self.fd,
            'printers':[{'name':'A1','ip':'192.0.2.10','serial':'0','access_code':'12345678','camera_type':'jpeg_tcp'}]}))
        env={**os.environ,'PM_CONFIG':str(self.dir/'config.json'),'PM_DATA':str(self.dir/'data')}
        return subprocess.Popen([sys.executable,str(ROOT/'main.py')],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)

    def test_failed_start_logs_why_and_exits(self):
        with socket.socket() as busy:
            busy.bind(('127.0.0.1',self.port));busy.listen()
            started=time.monotonic();process=self.start(['127.0.0.1'])
            try:
                output,_=process.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill();self.fail('a failed start hung instead of exiting')
        self.assertNotEqual(process.returncode,0)
        self.assertLess(time.monotonic()-started,30)
        self.assertIn('Start-up failed',output);self.assertIn('Is another copy running',output)

    def test_missing_address_does_not_stop_the_dashboard(self):
        process=self.start(['127.0.0.1','192.0.2.55'])
        try:
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/health',timeout=1) as r:
                        health=json.load(r);break
                except OSError:time.sleep(0.25)
            else:self.fail('the dashboard did not start')
            self.assertEqual(health['application'],'3d-printer-management')
        finally:
            started=time.monotonic();process.terminate()
            output,_=process.communicate(timeout=40)
        self.assertLess(time.monotonic()-started,35)   # stops even with the AI helper still loading
        self.assertIn('Cannot listen on 192.0.2.55',output);self.assertIn('Retrying 192.0.2.55 every 30 s',output)


class AiStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_helper_waits_for_the_app_then_watches(self):
        from failureDetection.detection import FailureMonitor
        calls=[]
        class Backend:
            async def warm(self):calls.append('warm')
        core=SimpleNamespace(EXAMPLE_MODE=False,settings={},CONFIG={'failure_detection':{'enabled':True,'model':'m.hef'}})
        m=FailureMonitor(core,SimpleNamespace(),Backend())
        async def run():calls.append('run')
        m.run=run
        await m.start()
        await asyncio.sleep(0);self.assertEqual(calls,[])   # nothing started during start-up itself
        m.task.cancel();await asyncio.gather(m.task,return_exceptions=True)
        await m.begin(delay=0);self.assertEqual(calls,['warm','run'])


if __name__=='__main__':
    unittest.main()
