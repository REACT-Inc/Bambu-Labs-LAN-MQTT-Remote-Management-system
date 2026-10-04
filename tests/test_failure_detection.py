"""AI HAT print-failure detection (#70): only multiple failing frames over several minutes flag a print, and a pause is
sent once, only while printing, and only reported as paused when the printer confirms it."""
import asyncio,json,os,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection.detection import FailureMonitor,HailoBackend,Judge,settings_for
from failureDetection.hailo_worker import parse


class Clock:
    def __init__(self):self.now=1000.0
    def __call__(self):return self.now


def settings(**overrides):
    return settings_for({'failure_detection':{**dict(enabled=True,model='m.hef',interval=30,window=10,needed=6,min_minutes=4,warm_up=3),**overrides}})


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.judge=Judge(settings(),self.clock);self.judge.reset('job.3mf');self.frame=0
    def add(self,score,step=30):
        self.clock.now+=step;self.frame+=1
        return self.judge.add(score,f'frame{self.frame}')

    def test_settings_are_clamped(self):
        s=settings_for({'failure_detection':{'threshold':5,'interval':1,'window':500,'needed':999,'min_minutes':0,'action':'stop'}})
        self.assertEqual((s['threshold'],s['interval'],s['window'],s['needed'],s['min_minutes'],s['action']),(0.99,10,60,60,1.0,'notify'))
        self.assertFalse(settings_for({})['enabled'])
        self.assertEqual(settings_for({'failure_detection':{'classes':['ok','spaghetti']}})['labels'],['ok','spaghetti'])

    def test_first_minutes_are_not_judged(self):
        self.assertEqual(self.add(0.99),'warming_up')
        self.assertEqual(len(self.judge.frames),0)

    def test_identical_frame_is_skipped(self):
        self.clock.now+=200
        self.assertEqual(self.judge.add(0.99,'same'),'watching')
        self.assertEqual(self.judge.add(0.99,'same'),'duplicate')
        self.assertEqual(len(self.judge.frames),1)

    def test_bad_frames_in_quick_succession_are_only_suspect(self):
        self.clock.now+=200
        verdicts=[self.add(0.95,step=10) for _ in range(10)]   # 10 bad frames, but within 100 s
        self.assertNotIn('failure',verdicts);self.assertEqual(verdicts[-1],'suspect')

    def test_failure_needs_enough_frames_over_the_minimum_time(self):
        self.clock.now+=200
        verdicts=[self.add(0.9) for _ in range(9)]   # a bad frame every 30 s
        # The 6th failing frame is only 2.5 min after the first; it takes 9 frames (4 min) to be a failure.
        self.assertEqual(verdicts[:8],['watching']+['suspect']*7)
        self.assertEqual(verdicts[8],'failure')

    def test_newest_frame_must_be_failing(self):
        self.clock.now+=200
        for _ in range(8):self.add(0.9)
        self.assertEqual(self.add(0.1),'suspect')   # 8 failing over 3.5 min… then a clean frame
        self.assertEqual(self.add(0.9),'failure')

    def test_mostly_good_frames_never_fail(self):
        self.clock.now+=200
        verdicts=[self.add(0.9 if i%3==0 else 0.1) for i in range(40)]
        self.assertNotIn('failure',verdicts)

    def test_camera_gap_starts_the_evidence_again(self):
        self.clock.now+=200
        for _ in range(7):self.add(0.9)
        self.assertEqual(self.add(0.9,step=600),'watching')   # 10 min without a picture
        self.assertEqual(len(self.judge.frames),1)


class WorkerParseTests(unittest.TestCase):
    def test_nms_output(self):
        outputs={'yolo/nms':[[[[0.1,0.2,0.5,0.6,0.82]],[],[[0.0,0.0,1.0,1.0,0.3]]]]}
        result=parse(outputs,['spaghetti','stringing','ok'],{'spaghetti','stringing'})
        self.assertEqual(result['score'],0.82)
        self.assertEqual(result['detections'][0],{'label':'spaghetti','score':0.82,'box':[0.2,0.1,0.6,0.5]})
        self.assertEqual(result['detections'][1]['label'],'ok')

    def test_only_failure_labels_count(self):
        result=parse({'nms':[[[],[],[[0,0,1,1,0.97]]]]},['spaghetti','stringing','ok'],{'spaghetti'})
        self.assertEqual(result['score'],0.0)

    def test_classification_output(self):
        class Array:
            def __init__(self,v):self.v=v
            def reshape(self,_):return self
            def tolist(self):return self.v
        result=parse({'softmax':Array([0.1,0.9])},['ok','failure'],{'failure'})
        self.assertEqual(result['score'],0.9);self.assertIsNone(result['detections'][0]['box'])


class FakeCore:
    EXAMPLE_MODE=False;RED=1
    def __init__(self,state='RUNNING',camera='rtsp'):
        self.settings={};self.current=state;self.camera=camera;self.pictures=0;self.events=[]
        self.notify=AsyncMock();self.event_listener=lambda *a:self.events.append(a)
        self.CONFIG={}
    def names(self):return ['H2D']
    def printer_config(self,name):return {'camera_type':self.camera}
    def state_data(self,name):return (self.current,0,{'subtask_name':'benchy'},True)
    async def snapshot(self,name,timeout=25):
        self.pictures+=1;return b'jpeg%d'%self.pictures
    def save_settings(self,updated):self.settings=dict(updated)


class FakeBackend:
    def __init__(self,score=0.95):self.score_value=score;self.calls=0
    async def score(self,jpeg):
        self.calls+=1
        if isinstance(self.score_value,Exception):raise self.score_value
        return self.score_value,[{'label':'spaghetti','score':self.score_value}]


class FakeEngine:
    def __init__(self,core,applies=True):self.core,self.applies=core,applies;self.control=AsyncMock(side_effect=self._control)
    async def _control(self,name,action,author):
        if self.applies:self.core.current='PAUSE'


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self,core,action='notify',backend=None,applies=True):
        core.CONFIG={'failure_detection':dict(enabled=True,model='m.hef',action=action)}
        self.clock=Clock();self.engine=FakeEngine(core,applies)
        m=FailureMonitor(core,self.engine,backend or FakeBackend(),self.clock);m.pause_poll=0
        return m
    async def run_checks(self,m,count,step=30):
        for _ in range(count):
            self.clock.now+=step;await m.check('H2D')

    async def test_off_without_config(self):
        m=FailureMonitor(FakeCore(),None)
        self.assertFalse(m.enabled);self.assertEqual(m.state('H2D'),{'enabled':False,'status':'off'})

    async def test_notify_only_reports_once_and_never_pauses(self):
        core=FakeCore();m=self.monitor(core)
        await self.run_checks(m,20)
        self.engine.control.assert_not_called()
        self.assertEqual(core.notify.await_count,1)
        title=core.notify.await_args.args[1];self.assertIn('may be failing',title)
        self.assertTrue(core.notify.await_args.args[4])   # with the camera picture
        self.assertEqual(m.state('H2D')['status'],'failure')

    async def test_pause_is_sent_once_and_confirmed(self):
        core=FakeCore();m=self.monitor(core,'pause')
        await self.run_checks(m,15)
        self.engine.control.assert_awaited_once_with('H2D','pause','AI failure detection')
        self.assertEqual(m.state('H2D')['status'],'paused')
        self.assertIn('paused',core.notify.await_args.args[1])
        # The user resumes: the same print is never paused again.
        core.current='RUNNING'
        await self.run_checks(m,15)
        self.engine.control.assert_awaited_once();self.assertEqual(core.notify.await_count,1)

    async def test_pause_not_confirmed_is_not_reported_as_paused(self):
        core=FakeCore();m=self.monitor(core,'pause',applies=False)
        m.confirm_pause=AsyncMock(return_value=False)
        await self.run_checks(m,15)
        self.assertEqual(m.state('H2D')['status'],'failure')
        self.assertIn('not reported PAUSE',core.notify.await_args.args[2])

    async def test_no_pause_when_printer_stopped_meanwhile(self):
        core=FakeCore();m=self.monitor(core,'pause')
        await self.run_checks(m,14)
        real=core.state_data;calls=[0]
        def finishing(name):
            calls[0]+=1
            return ('FINISH',0,{'subtask_name':'benchy'},True) if calls[0]>1 else real(name)
        core.state_data=finishing
        await self.run_checks(m,1)
        self.engine.control.assert_not_called()
        self.assertIn('Not paused',core.notify.await_args.args[2])

    async def test_only_watches_running_prints_with_a_camera(self):
        core=FakeCore(state='IDLE');backend=FakeBackend();m=self.monitor(core,backend=backend)
        await self.run_checks(m,3)
        self.assertEqual(backend.calls,0);self.assertEqual(m.state('H2D')['status'],'idle')
        core.current='RUNNING';core.camera='';await self.run_checks(m,3)
        self.assertEqual(backend.calls,0)

    async def test_new_print_is_judged_from_scratch(self):
        core=FakeCore();m=self.monitor(core)
        await self.run_checks(m,20);self.assertEqual(core.notify.await_count,1)
        core.state_data=lambda n:('RUNNING',0,{'subtask_name':'next'},True)
        await self.run_checks(m,3)
        self.assertEqual(m.state('H2D')['status'],'watching')   # warming up on the new print
        await self.run_checks(m,20);self.assertEqual(core.notify.await_count,2)

    async def test_per_printer_opt_out(self):
        core=FakeCore();backend=FakeBackend();m=self.monitor(core,backend=backend)
        m.set_watching('H2D',False)
        self.assertEqual(core.settings['ai_watch_off'],['H2D']);self.assertFalse(m.state('H2D')['watching'])
        await self.run_checks(m,20)
        self.assertEqual(backend.calls,0);core.notify.assert_not_awaited()
        m.set_watching('H2D',True);self.assertTrue(m.watching('H2D'))
        with self.assertRaises(ValueError):m.set_watching('Nope',True)

    async def test_unavailable_hat_is_shown_not_raised(self):
        core=FakeCore();m=self.monitor(core,backend=FakeBackend(RuntimeError('No Hailo device found')))
        await self.run_checks(m,2)
        self.assertEqual(m.state('H2D')['status'],'unavailable');self.assertIn('No Hailo',m.state('H2D')['message'])
        core.notify.assert_not_awaited()

    async def test_good_print_never_reports(self):
        core=FakeCore();m=self.monitor(core,'pause',backend=FakeBackend(0.2))
        await self.run_checks(m,40)
        core.notify.assert_not_awaited();self.engine.control.assert_not_called()

    async def test_backend_reports_missing_model(self):
        backend=HailoBackend(settings(model='/nonexistent/model.hef'))
        with self.assertRaisesRegex(RuntimeError,'Model file not found'):await backend.score(b'x')
        with self.assertRaisesRegex(RuntimeError,'Model file not found'):await backend.score(b'x')   # backs off, same message


class WorkerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_talks_json_lines_to_the_helper(self):
        # A stand-in helper with the same protocol as hailo_worker.py.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fake=Path(d)/'fake.py';model=Path(d)/'m.hef';model.write_bytes(b'x')
            fake.write_text('import json,sys\nprint(json.dumps({"ready":True,"input":[640,640]}),flush=True)\n'
                            'for line in sys.stdin:\n    print(json.dumps({"score":0.7,"detections":[{"label":"spaghetti","score":0.7}]}),flush=True)\n')
            import failureDetection.detection as detection
            original=detection.WORKER;detection.WORKER=fake
            try:
                backend=HailoBackend(settings(model=str(model),python=sys.executable))
                self.assertEqual((await backend.score(b'jpeg'))[0],0.7)
                self.assertEqual((await backend.score(b'jpeg2'))[0],0.7)
                await backend.close()
            finally:detection.WORKER=original


class DashboardStateTests(unittest.TestCase):
    def test_discord_line(self):
        import importlib.util,tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1],'printers':[{'name':'H2D','ip':'192.0.2.1','serial':'0','access_code':'1'}]}))
            with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
                spec=importlib.util.spec_from_file_location('test_core_ai',Path(__file__).parents[1]/'core.py')
                core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
            core.state_data=lambda n:('RUNNING',0,{},True)
            self.assertNotIn('AI',' '.join(f.name for f in core.printer_embed('H2D').fields))
            core.failure_monitor=SimpleNamespace(state=lambda n:dict(enabled=True,watching=True,status='paused',message='7 of the last 10 frames'))
            fields={f.name:f.value for f in core.printer_embed('H2D').fields}
            self.assertIn('Paused by AI',fields['🤖 AI failure watch'])
            asyncio.run(core.bot.close())


if __name__=='__main__':
    unittest.main()
