"""Better detection on your own cameras: close-up crops, merging, and training-picture collection."""
import base64,io,json,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection.detection import crops_for,DEFAULT_CROPS,settings_for
from failureDetection.hailo_worker import from_crop,merge,look,letterbox
from failureDetection.training import TrainingPictures


def scale(crop,width=1680,height=1080,size=640):
    """How much bigger something in this crop looks to the model than in the whole picture."""
    w,h=(crop[2]-crop[0])*width,(crop[3]-crop[1])*height
    return (size/max(w,h))/(size/max(width,height))


class CropTests(unittest.TestCase):
    def test_default_crops_enlarge_the_bed_area(self):
        # The A1 mini picture that was missed: 1680x1080, spaghetti at x 0.29-0.70, y 0.17-0.30.
        mess=(0.29,0.17,0.70,0.30)
        covering=[c for c in DEFAULT_CROPS if c[0]<=mess[0] and c[2]>=mess[2] and c[1]<=mess[1] and c[3]>=mess[3]]
        self.assertTrue(covering)                                         # at least one close-up holds the whole mess
        self.assertGreater(max(scale(c) for c in covering),1.6)            # and shows it 1.6x+ bigger
        self.assertTrue(all(scale(c)>1.5 for c in DEFAULT_CROPS))
        self.assertEqual(scale([0,0,1,0.65]),1.0)                          # why full-width strips aren't used

    def test_calibrated_crops_follow_the_bed(self):
        narrow=crops_for([[0.3,0.8],[0.7,0.8],[0.62,0.4],[0.38,0.4]])
        self.assertEqual(len(narrow),1)
        x0,y0,x1,y1=narrow[0]
        self.assertLessEqual(x0,0.3);self.assertGreaterEqual(x1,0.7);self.assertLess(y0,0.4-0.1)   # room above for tall parts
        wide=crops_for([[0.05,0.95],[0.95,0.95],[0.8,0.3],[0.2,0.3]])
        self.assertEqual(len(wide),3);self.assertGreater(wide[1][2],wide[2][0])                    # halves overlap
        self.assertEqual(crops_for(None),DEFAULT_CROPS)

    def test_setting(self):
        self.assertEqual(settings_for({'failure_detection':{'crops':'off'}})['crops'],'off')
        self.assertEqual(settings_for({})['crops'],'auto')


class LookTests(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow not installed')
        self.Image=Image

    def test_boxes_from_crops_land_on_the_whole_picture(self):
        self.assertEqual(from_crop([{'label':'s','score':0.9,'box':[0.5,0.5,1.0,1.0]}],(0.2,0.1,0.6,0.5))[0]['box'],[0.4,0.3,0.6,0.5])

    def test_merge_keeps_the_strongest_duplicate(self):
        found=merge([{'label':'spaghetti','score':0.3,'box':[0.30,0.20,0.70,0.30]},{'label':'spaghetti','score':0.7,'box':[0.31,0.19,0.69,0.31]},
                     {'label':'stringing','score':0.5,'box':[0.30,0.20,0.70,0.30]},{'label':'spaghetti','score':0.4,'box':[0.0,0.8,0.1,0.9]}])
        self.assertEqual([(d['label'],d['score']) for d in found],[('spaghetti',0.7),('stringing',0.5),('spaghetti',0.4)])

    def test_look_runs_the_model_on_every_crop(self):
        image=self.Image.new('RGB',(1680,1080));sizes=[]
        def run(frame):
            sizes.append(frame.size);return [{'label':'spaghetti','score':0.2+0.1*len(sizes),'box':[0.4,0.4,0.6,0.6]}]
        found=look(image,DEFAULT_CROPS,run,640,640)
        self.assertEqual(len(sizes),1+len(DEFAULT_CROPS));self.assertTrue(all(s==(640,640) for s in sizes))
        self.assertAlmostEqual(max(d['score'] for d in found),0.6)
        self.assertTrue(all(0<=v<=1 for d in found for v in d['box']))
        self.assertEqual(len(look(image,[[0,0,0.01,0.01],[1,2]],run,640,640)),1)   # tiny / malformed crops skipped


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=[1_800_000_000.0]
        self.pictures=TrainingPictures(SimpleNamespace(DATA_DIR=Path(self.tmp.name)),{},clock=lambda:self.now[0])
    def tearDown(self):self.tmp.cleanup()
    def tick(self,seconds):self.now[0]+=seconds

    def test_periodic_and_flagged_frames(self):
        self.assertIsNotNone(self.pictures.save('James','job1',b'jpg',0.1,'watching'))
        self.tick(30);self.assertIsNone(self.pictures.save('James','job1',b'jpg',0.1,'watching'))   # not a minute yet
        self.assertIsNotNone(self.pictures.save('James','job1',b'jpg',0.5,'suspect'))               # suspicious: kept
        self.tick(10);self.assertIsNone(self.pictures.save('James','job1',b'jpg',0.5,'suspect'))    # not twice in one check
        self.tick(30);self.assertIsNotNone(self.pictures.save('James','job1',b'jpg',0.1,'watching')) # a minute since the last still
        self.assertIsNotNone(self.pictures.save('Keith','job2',b'jpg',0.1,'watching'))              # per printer
        self.assertEqual(self.pictures.summary()['count'],4)

    def test_capped(self):
        pictures=TrainingPictures(SimpleNamespace(DATA_DIR=Path(self.tmp.name)),{'max_pictures':50,'every_minutes':1},clock=lambda:self.now[0])
        for i in range(60):
            self.tick(61);pictures.save('P','j',b'x',0.1,'watching')
        self.assertEqual(pictures.summary()['count'],50)

    def test_export_sorted_by_outcome(self):
        for job in ('good','bad','running'):
            self.tick(400);self.pictures.save('James',job,b'jpg',0.2,'watching')
        target=Path(self.tmp.name)/'out.zip'
        self.pictures.export({'good':'finished','bad':'failed','running':'printing'},target)
        names=zipfile.ZipFile(target).namelist()
        self.assertIn('README.txt',names)
        self.assertEqual(sorted(n.split('/')[0] for n in names if n.endswith('.jpg')),['failed','finished','other'])

    def test_low_disk_is_reported(self):
        from unittest.mock import patch
        with patch('shutil.disk_usage',return_value=SimpleNamespace(free=300*1024**2)):
            self.assertIsNone(self.pictures.save('James','job1',b'jpg',0.1,'watching'))
        self.assertIn('only 0.3 GB free',self.pictures.summary()['problem'])
        self.tick(120);self.assertIsNotNone(self.pictures.save('James','job1',b'jpg',0.1,'watching'))
        self.assertEqual(self.pictures.summary()['problem'],'')

    def test_defaults(self):
        self.assertEqual((self.pictures.settings['every_minutes'],self.pictures.settings['max_pictures']),(1.0,5000))

    def test_off_and_clear(self):
        off=TrainingPictures(SimpleNamespace(DATA_DIR=Path(self.tmp.name)),{'enabled':False})
        self.assertIsNone(off.save('P','j',b'x',0.1,'suspect'))
        self.assertIsNone(TrainingPictures(SimpleNamespace(),{}).save('P','j',b'x',0.1,'suspect'))   # no data folder
        self.pictures.save('P','j',b'x',0.1,'watching');self.pictures.clear()
        self.assertEqual(self.pictures.summary()['count'],0)


class RouteTests(unittest.TestCase):
    def test_routes(self):
        import inspect,dashboard
        source=inspect.getsource(dashboard.Dashboard.__init__)
        for route in ("'/api/ai-training'","'/api/ai-training.zip'","'/api/ai-training/clear'"):self.assertIn(route,source)


if __name__=='__main__':
    unittest.main()


class TestNowTests(unittest.IsolatedAsyncioTestCase):
    """"Test AI now": a check on a fresh picture at any time, which never touches the failure rules."""
    def monitor(self,state='IDLE',camera='jpeg_tcp',picture=b'\xff\xd8pic\xff\xd9',score=0.7):
        from unittest.mock import AsyncMock
        from failureDetection.detection import FailureMonitor
        self.tmp=tempfile.TemporaryDirectory()
        core=SimpleNamespace(DATA_DIR=Path(self.tmp.name),settings={},names=lambda:['James'],printer_config=lambda n:{'camera_type':camera},
            state_data=lambda n:(state,0,{'layer_num':0},True),capture_still=AsyncMock(return_value=picture),snapshot=AsyncMock(return_value=None),
            notify=AsyncMock(),save_settings=lambda s:None,event_listener=lambda *a:self.events.append(a),RED=1,
            CONFIG={'failure_detection':{'enabled':True,'model':'m.onnx','threshold':0.4,'labels':['spaghetti','warping'],'classes':['spaghetti','stringing','warping']}})
        self.events=[];self.crops=[]
        test=self
        class Backend:
            async def score(self,jpeg,crops=None):
                test.crops.append(crops);return score,[{'label':'spaghetti','score':score,'box':[0.3,0.2,0.7,0.3]}]
        return FailureMonitor(core,SimpleNamespace(),backend=Backend())
    def tearDown(self):self.tmp.cleanup()

    async def test_works_when_idle_and_keeps_the_picture(self):
        m=self.monitor()
        r=await m.test('James')
        self.assertEqual((r['score'],r['threshold'],r['failing']),(0.7,0.4,True))
        self.assertEqual(r['detections'][0]['label'],'spaghetti');self.assertTrue(r['crops'])   # close-ups used
        self.assertEqual(base64.b64decode(r['picture']),b'\xff\xd8pic\xff\xd9')
        self.assertEqual(m.training.summary()['count'],1)                                       # kept for training
        self.assertEqual(m.state('James')['status'],'idle');self.assertNotIn('James',m.judges)  # failure rules untouched
        self.assertEqual(self.events[0][1],'AI test')
        await m.test('James');self.assertEqual(m.training.summary()['count'],2)                 # every test is kept

    async def test_below_threshold(self):
        r=await self.monitor(score=0.12).test('James')
        self.assertFalse(r['failing'])

    async def test_clear_errors(self):
        with self.assertRaisesRegex(ValueError,'no camera'):await self.monitor(camera='').test('James')
        self.tmp.cleanup()
        with self.assertRaisesRegex(ValueError,'No camera picture'):await self.monitor(picture=None).test('James')


class TestRouteTests(unittest.TestCase):
    def test_route(self):
        import inspect,dashboard
        self.assertIn("'/api/ai/{name}/test'",inspect.getsource(dashboard.Dashboard.__init__))


class CheckNowTests(unittest.IsolatedAsyncioTestCase):
    """"Check AI now": a real check while printing that acts on this frame alone."""
    def monitor(self,state='RUNNING',score=0.7,action='pause'):
        from unittest.mock import AsyncMock
        from failureDetection.detection import FailureMonitor
        self.tmp=tempfile.TemporaryDirectory();self.states={'James':state}
        self.core=SimpleNamespace(DATA_DIR=Path(self.tmp.name),settings={},names=lambda:['James'],printer_config=lambda n:{'camera_type':'jpeg_tcp'},
            state_data=lambda n:(self.states[n],0,{'subtask_name':'job','layer_num':3},True),capture_still=AsyncMock(side_effect=lambda n,t:b'pic%f'%__import__('time').time()),
            snapshot=AsyncMock(return_value=None),notify=AsyncMock(),save_settings=lambda s:None,RED=1,
            CONFIG={'failure_detection':{'enabled':True,'model':'m.onnx','threshold':0.4,'action':action}})
        async def control(name,action,author):self.states[name]='PAUSE'
        self.engine=SimpleNamespace(control=AsyncMock(side_effect=control))
        self.score=score;test=self
        class Backend:
            async def score(self,jpeg,crops=None):return test.score,[{'label':'spaghetti','score':test.score,'box':[0.3,0.2,0.7,0.3]}]
        m=FailureMonitor(self.core,self.engine,backend=Backend());m.pause_poll=0
        return m
    def tearDown(self):self.tmp.cleanup()

    async def test_failing_frame_pauses_straight_away(self):
        m=self.monitor()
        r=await m.check_now('James')   # first frame of the print, even inside the warm-up minutes
        self.assertTrue(r['acted']);self.engine.control.assert_awaited_once_with('James','pause','AI failure detection')
        self.assertEqual(m.state('James')['status'],'paused')
        self.assertIn('Checked on request',self.core.notify.await_args.args[2])
        self.states['James']='RUNNING'                                   # resumed: a second request doesn't pause again
        r=await m.check_now('James');self.assertTrue(r['already']);self.engine.control.assert_awaited_once()

    async def test_good_frame_just_counts(self):
        m=self.monitor(score=0.1)
        r=await m.check_now('James')
        self.assertFalse(r['acted']);self.engine.control.assert_not_awaited();self.core.notify.assert_not_awaited()
        self.assertEqual(m.state('James')['status'],'watching')

    async def test_notify_mode_reports_without_pausing(self):
        m=self.monitor(action='notify')
        r=await m.check_now('James')
        self.assertTrue(r['acted']);self.engine.control.assert_not_awaited();self.core.notify.assert_awaited_once()

    async def test_only_while_printing(self):
        m=self.monitor(state='IDLE')
        with self.assertRaisesRegex(ValueError,'while a print is running'):await m.check_now('James')


class CheckRouteTests(unittest.TestCase):
    def test_route(self):
        import inspect,dashboard
        self.assertIn("'/api/ai/{name}/check'",inspect.getsource(dashboard.Dashboard.__init__))


class FreshFrameTests(unittest.IsolatedAsyncioTestCase):
    """Each 30 s check must judge a new frame: a picture reused for up to 60 s halved the counted frames."""
    async def test_every_check_counts(self):
        from unittest.mock import AsyncMock
        from failureDetection.detection import FailureMonitor
        now=[1_000_000.0];taken=[]
        async def snapshot(name,timeout=25,max_age=60):
            # like core.snapshot: reuse the last still while it's younger than max_age, else take a new one
            if taken and now[0]-taken[-1][0]<max_age:return taken[-1][1]
            taken.append((now[0],b'pic%d'%len(taken)));return taken[-1][1]
        with tempfile.TemporaryDirectory() as d:
            core=SimpleNamespace(DATA_DIR=Path(d),settings={},names=lambda:['James'],printer_config=lambda n:{'camera_type':'jpeg_tcp'},
                state_data=lambda n:('RUNNING',0,{'subtask_name':'job'},True),snapshot=snapshot,notify=AsyncMock(),save_settings=lambda s:None,RED=1,
                CONFIG={'failure_detection':{'enabled':True,'model':'m.onnx','warm_up':0}})
            class Backend:
                async def score(self,jpeg,crops=None):return 0.1,[]
            m=FailureMonitor(core,SimpleNamespace(),backend=Backend(),clock=lambda:now[0])
            for _ in range(10):
                await m.check('James');now[0]+=30
            self.assertEqual(m.state('James')['frames'],10)   # was 5 with 60 s reuse


class ThrottleTests(unittest.TestCase):
    """Adaptive interval: more often while the Pi is quiet, less while it's busy, never over half the time."""
    def throttle(self,load,model='m.onnx',**extra):
        from failureDetection.detection import Throttle,settings_for
        self.settings=settings_for({'failure_detection':{'enabled':True,'model':model,**extra}})
        self.loads=list(load) if isinstance(load,(list,tuple)) else [load]
        return Throttle(self.settings,load=lambda:self.loads[0] if len(self.loads)==1 else self.loads.pop(0))

    def test_quiet_pi_speeds_up_to_the_floor(self):
        t=self.throttle(0.2)
        values=[t.next(1.0) for _ in range(20)]
        self.assertLess(values[0],30);self.assertEqual(values[-1],10)   # CPU model: 10 s floor
        self.assertEqual(self.settings['interval'],10)

    def test_ai_hat_floor_is_lower(self):
        t=self.throttle(0.2,model='m.hef')
        for _ in range(20):t.next(0.5)
        self.assertEqual(t.current,5)

    def test_busy_pi_backs_off(self):
        t=self.throttle(1.5)
        for _ in range(10):t.next(1.0)
        self.assertEqual(t.current,60);self.assertIn('busy',t.reason)
        t=self.throttle(0.2)
        t.next(1.0,lateness=2.0);self.assertEqual(t.current,45)        # a late wake-up counts as busy

    def test_never_more_than_half_the_time(self):
        t=self.throttle(0.2)
        for _ in range(20):t.next(8.0)                                # a round of checks takes 8 s
        self.assertEqual(t.current,16)

    def test_ai_hat_starts_at_five_seconds_and_may_check_back_to_back(self):
        t=self.throttle(0.2,model='m.hef')
        self.assertEqual(t.current,5)                                  # no slow start with the AI HAT
        self.assertEqual(t.next(0.5),5)
        self.assertEqual(t.next(7.0),7)                                # cameras slower than 5 s: as soon as done
        self.assertEqual(self.throttle(0.2).current,30)                # the CPU still starts calmly

    def test_fixed_interval(self):
        t=self.throttle(1.5,interval=20)
        self.assertEqual(t.next(1.0),20);self.assertIn('fixed',t.reason)


class ParallelCheckTests(unittest.IsolatedAsyncioTestCase):
    """Every printing printer is checked in each round, at the same time."""
    async def test_printers_are_checked_together(self):
        import asyncio,time
        from failureDetection.detection import FailureMonitor
        class Core:
            EXAMPLE_MODE=False;settings={}
            CONFIG={'failure_detection':{'enabled':True,'model':'m.hef'}}
            def names(self):return ['A','B','C','D']
            def printer_config(self,name):return {'camera_type':'rtsp'}
            def state_data(self,name):return ('RUNNING',0,{'subtask_name':'job'},True)
            async def snapshot(self,name,timeout=25,max_age=60):
                await asyncio.sleep(0.2);return name.encode()+str(time.monotonic()).encode()
        class Backend:
            def __init__(self):self.seen=[]
            async def score(self,jpeg,crops=None):self.seen.append(jpeg[:1]);return 0.1,[]
        backend=Backend();m=FailureMonitor(Core(),SimpleNamespace(),backend)
        costs=[]
        def stop(cost,lateness=0.0):
            costs.append(cost);raise asyncio.CancelledError
        m.throttle.next=stop
        with self.assertRaises(asyncio.CancelledError):await m.run()
        self.assertEqual(sorted(backend.seen),[b'A',b'B',b'C',b'D'])
        self.assertLess(costs[0],0.6)   # four 0.2 s cameras in about 0.2 s, not 0.8 s


class TimeBasedJudgeTests(unittest.TestCase):
    """The decision means the same however often the AI looks."""
    def judge(self,interval):
        from failureDetection.detection import Judge,settings_for
        s=settings_for({'failure_detection':{'enabled':True,'model':'m.onnx','warm_up':0,'threshold':0.5}})
        now=[0.0];j=Judge(s,clock=lambda:now[0]);j.reset('job')
        def add(score):
            now[0]+=interval;return j.add(score,str(now[0]))
        return j,add,s

    def test_old_settings_convert(self):
        _,_,s=self.judge(30)
        self.assertEqual((s['window_minutes'],s['needed_share'],s['interval']),(5.0,0.6,30))

    def test_fast_looking_still_needs_four_minutes(self):
        for interval in (5,10,30,60):
            j,add,_=self.judge(interval)
            verdicts=[add(0.9) for _ in range(int(400/interval))]
            first=verdicts.index('failure')
            self.assertGreaterEqual((first+1)*interval-interval,240,interval)   # span of failing frames >= 4 min
            self.assertLessEqual((first+1)*interval,300,interval)              # but not much later

    def test_mostly_good_never_fails_at_any_speed(self):
        for interval in (5,10,30,60):
            j,add,_=self.judge(interval)
            verdicts=[add(0.9 if i%2==0 else 0.1) for i in range(int(900/interval))]   # 50% failing < 60%
            self.assertNotIn('failure',verdicts,interval)

    def test_window_forgets_old_frames(self):
        j,add,_=self.judge(10)
        for _ in range(60):add(0.1)
        self.assertLessEqual(len(j.frames),31)                               # 5 minutes at 10 s
