"""AI print-failure detection (#70): only multiple failing frames over several minutes flag a print, and a pause is
sent once, only while printing, and only reported as paused when the printer confirms it."""
import asyncio,json,os,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection.detection import FOCUS_KEEP,Focus,FailureMonitor,HailoBackend,Judge,settings_for,tuned
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


class HoldTests(unittest.TestCase):
    """A real failure's score flickers around the threshold: once two frames reached it, frames at or above the
    lower hold score keep counting."""
    def judge(self,**overrides):
        self.clock=Clock();j=Judge(settings(threshold=0.4,**overrides),self.clock);j.reset('job');self.clock.now+=200;self.n=0;return j
    def feed(self,j,scores):
        out=[]
        for score in scores:
            self.clock.now+=30;self.n+=1;out.append(j.add(score,f'f{self.n}'))
        return out

    def test_flickering_failure_is_caught(self):
        flicker=[0.45,0.3,0.42,0.33,0.38,0.31,0.44,0.29,0.36,0.41,0.32,0.35]   # only 4 of 12 frames reach 40%
        self.assertIn('failure',self.feed(self.judge(),flicker))                # default hold 25%
        self.assertNotIn('failure',self.feed(self.judge(hold=0.4),flicker))     # without hold it never adds up

    def test_one_strong_frame_is_not_enough(self):
        j=self.judge()
        self.assertNotIn('failure',self.feed(j,[0.5]+[0.35]*15))
        self.assertLessEqual(j.failing(),1)

    def test_newest_frame_must_reach_hold(self):
        j=self.judge()
        self.assertNotIn('failure',self.feed(j,[0.5,0.45]+[0.3]*6+[0.1]))
        self.assertEqual(self.feed(j,[0.3]),['failure'])

    def test_hold_defaults_below_the_threshold(self):
        self.assertEqual(settings(threshold=0.6)['hold'],0.45)
        self.assertEqual(settings(threshold=0.1)['hold'],0.05)
        self.assertEqual(settings(threshold=0.5,hold=0.9)['hold'],0.5)   # never above the threshold


class TuningTests(unittest.TestCase):
    def test_presets_and_custom_values(self):
        base=settings(threshold=0.4)
        self.assertEqual(tuned(base,None)['threshold'],0.4);self.assertEqual(tuned(base,None)['preset'],'normal')
        sensitive=tuned(base,{'preset':'sensitive'})
        self.assertEqual((sensitive['threshold'],sensitive['hold'],sensitive['min_minutes'],sensitive['needed_share']),(0.35,0.2,3,0.4))
        custom=tuned(base,{'threshold':0.3,'min_minutes':12,'needed_share':5})
        self.assertEqual((custom['preset'],custom['threshold'],custom['hold'],custom['needed_share']),('custom',0.3,0.15,1.0))
        self.assertEqual(custom['window_minutes'],13)   # the window always covers the minutes asked for
        self.assertEqual(tuned(base,{'preset':'normal','focus':False})['focus'],False)
        self.assertEqual(base['threshold'],0.4)   # the shared settings are never changed

    def test_area_classes_never_count_as_failures(self):
        s=settings_for({'failure_detection':{'classes':['spaghetti','print','bed']}})
        self.assertEqual(s['labels'],['spaghetti'])


class FocusTests(unittest.TestCase):
    def setUp(self):self.clock=Clock();self.focus=Focus(self.clock);self.focus.reset('A1','job')

    def test_zooms_in_on_a_suspicious_spot_for_a_while(self):
        self.focus.update('A1',[{'label':'spaghetti','score':0.3,'box':[0.6,0.2,0.7,0.3]},{'label':'spaghetti','score':0.05,'box':[0,0,0.1,0.1]}],['spaghetti'])
        crops=self.focus.crops('A1',[])
        self.assertEqual(len(crops),1)
        x0,y0,x1,y1=crops[0]
        self.assertAlmostEqual(x1-x0,0.3);self.assertAlmostEqual((x0+x1)/2,0.65)   # 2.5x the box, at least 30% of the picture
        self.assertIn('suspicious spot (30%',self.focus.describe('A1'))
        self.clock.now+=FOCUS_KEEP+1
        self.assertEqual(self.focus.crops('A1',[]),[]);self.assertEqual(self.focus.describe('A1'),'')

    def test_weak_or_unlabelled_detections_are_ignored(self):
        self.focus.update('A1',[{'label':'spaghetti','score':0.08,'box':[0.1,0.1,0.2,0.2]},{'label':'spaghetti','score':0.9,'box':None}],['spaghetti'])
        self.assertEqual(self.focus.crops('A1',[]),[])

    def test_print_class_gives_the_area(self):
        self.focus.update('A1',[{'label':'print','score':0.8,'box':[0.4,0.4,0.6,0.6]}],['spaghetti'])
        self.assertEqual(self.focus.crops('A1',[]),[[0.35,0.35,0.65,0.65]])
        self.assertIn('print the model found',self.focus.describe('A1'))

    def test_skips_a_close_up_already_covered_and_resets_per_print(self):
        self.focus.update('A1',[{'label':'spaghetti','score':0.5,'box':[0.4,0.4,0.6,0.6]}],['spaghetti'])
        self.assertEqual(self.focus.crops('A1',[[0.26,0.25,0.75,0.76]]),[])
        self.focus.reset('A1','job');self.assertEqual(len(self.focus.crops('A1',[])),1)   # same print: kept
        self.focus.reset('A1','next');self.assertEqual(self.focus.crops('A1',[]),[])


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


class WorkerInputTests(unittest.TestCase):
    def test_input_is_writeable_for_hailort(self):
        try:
            import numpy  # noqa: F401  (installed with hailo-all on the Pi; optional here)
            from PIL import Image
        except ImportError:
            self.skipTest('numpy/Pillow not installed')
        from failureDetection.hailo_worker import input_batch,letterbox
        frame,_,_,_=letterbox(Image.new('RGB',(1920,1080)),640,640)
        batch=input_batch(frame)
        self.assertEqual(batch.shape,(1,640,640,3));self.assertTrue(batch.flags.writeable);self.assertTrue(batch.flags.c_contiguous)


def yolo_output(boxes,classes=3,count=50):
    """A YOLOv8-shaped output (1, 4 + classes, count): boxes are (cx, cy, w, h, class, score) in 640-px input space."""
    import numpy as np
    out=np.zeros((1,4+classes,count),dtype=np.float32)
    for i,(cx,cy,w,h,c,score) in enumerate(boxes):
        out[0,:4,i]=(cx,cy,w,h);out[0,4+c,i]=score
    return out


class YoloDecodeTests(unittest.TestCase):
    def setUp(self):
        try:import numpy  # noqa: F401
        except ImportError:self.skipTest('numpy not installed')
        from failureDetection.hailo_worker import decode_yolov8,summarise
        self.decode,self.summarise=decode_yolov8,summarise
        self.names=['spaghetti','stringing','warping']

    def test_boxes_classes_and_score(self):
        out=yolo_output([(320,320,200,100,0,0.91),(100,100,40,40,1,0.97),(500,500,60,60,2,0.03)])
        detections=self.decode(out,self.names,640,640)
        self.assertEqual([d['label'] for d in detections],['spaghetti','stringing'])   # 0.03 is below the floor
        self.assertEqual(detections[0]['box'],[0.3438,0.4219,0.6562,0.5781])
        # Stringing is the strongest detection but isn't a failure label, so it doesn't count.
        self.assertEqual(self.summarise(detections,{'spaghetti','warping'})['score'],0.91)

    def test_overlapping_boxes_merged_per_class(self):
        out=yolo_output([(320,320,200,100,0,0.9),(325,322,200,100,0,0.8),(320,320,200,100,2,0.7)])
        detections=self.decode(out,self.names,640,640)
        self.assertEqual(sorted((d['label'],d['score']) for d in detections),[('spaghetti',0.9),('warping',0.7)])

    def test_boxes_first_layout(self):
        out=yolo_output([(320,320,100,100,2,0.8)]).transpose(0,2,1)
        self.assertEqual(self.decode(out,self.names,640,640)[0]['label'],'warping')

    def test_nothing_found(self):
        self.assertEqual(self.summarise(self.decode(yolo_output([]),self.names,640,640),{'spaghetti'}),{'score':0.0,'detections':[]})


class CpuWorkerTests(unittest.IsolatedAsyncioTestCase):
    """The real helper process with OpenCV, on a tiny ONNX model whose output is a fixed YOLOv8-shaped tensor."""
    async def test_onnx_model_through_the_backend(self):
        try:
            import cv2  # noqa: F401
            import onnx
            from onnx import helper,TensorProto,numpy_helper
            from PIL import Image
        except ImportError:
            self.skipTest('OpenCV/onnx/Pillow not installed')
        import io as stdio,tempfile
        fixed=yolo_output([(320,320,200,100,0,0.88),(100,100,40,40,1,0.95)],count=8400)
        graph=helper.make_graph(
            [helper.make_node('ReduceSum',['images'],['total'],keepdims=1),
             helper.make_node('Mul',['total','zero'],['nothing']),
             helper.make_node('Add',['fixed','nothing'],['output0'])],
            'fake_yolo',[helper.make_tensor_value_info('images',TensorProto.FLOAT,[1,3,640,640])],
            [helper.make_tensor_value_info('output0',TensorProto.FLOAT,[1,7,8400])],
            [numpy_helper.from_array(fixed,'fixed'),numpy_helper.from_array(__import__('numpy').zeros((1,1,1,1),'float32'),'zero')])
        model=helper.make_model(graph,opset_imports=[helper.make_opsetid('',11)]);model.ir_version=7
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'best.onnx';onnx.save(model,str(path))
            picture=stdio.BytesIO();Image.new('RGB',(1280,720),(40,40,40)).save(picture,'JPEG')
            backend=HailoBackend(settings(model=str(path),python=sys.executable,classes=['spaghetti','stringing','warping'],labels=['spaghetti','warping']))
            try:
                score,detections=await backend.score(picture.getvalue())
            finally:
                await backend.close()
        self.assertEqual(score,0.88)
        self.assertEqual({d['label'] for d in detections},{'spaghetti','stringing'})

    async def test_missing_opencv_gets_a_hint(self):
        backend=HailoBackend(settings())
        self.assertIn('python3-opencv',backend.hint("ModuleNotFoundError: No module named 'cv2'"))
        self.assertIn('hailo-all',backend.hint("ModuleNotFoundError: No module named 'hailo_platform'"))


class FakeCore:
    EXAMPLE_MODE=False;RED=1
    def __init__(self,state='RUNNING',camera='rtsp'):
        self.settings={};self.current=state;self.camera=camera;self.pictures=0;self.events=[]
        self.notify=AsyncMock();self.event_listener=lambda *a:self.events.append(a)
        self.CONFIG={}
    def names(self):return ['H2D']
    def printer_config(self,name):return {'camera_type':self.camera}
    def state_data(self,name):return (self.current,0,{'subtask_name':'benchy'},True)
    async def snapshot(self,name,timeout=25,max_age=60):
        self.pictures+=1;return b'jpeg%d'%self.pictures
    def save_settings(self,updated):self.settings=dict(updated)


class FakeBackend:
    def __init__(self,score=0.95):self.score_value=score;self.calls=0
    async def score(self,jpeg,crops=None):
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

    async def test_sensitivity_preset_catches_low_scores(self):
        core=FakeCore();m=self.monitor(core,'notify',backend=FakeBackend(0.4))
        await self.run_checks(m,20);core.notify.assert_not_awaited()   # 40% is under config's 60%
        tuning=m.set_tuning('H2D',{'preset':'sensitive'})
        self.assertEqual(core.settings['ai_tuning'],{'H2D':{'preset':'sensitive'}});self.assertEqual(tuning['threshold'],0.35)
        await self.run_checks(m,12);core.notify.assert_awaited_once()
        self.assertEqual(m.state('H2D')['tuning']['preset'],'sensitive')
        self.assertIn('AI sensitivity changed',[e[1] for e in core.events])

    async def test_tuning_validation_and_reset(self):
        core=FakeCore();m=self.monitor(core)
        with self.assertRaisesRegex(ValueError,'between'):m.set_tuning('H2D',{'threshold':2})
        with self.assertRaisesRegex(ValueError,'not be above'):m.set_tuning('H2D',{'threshold':0.3,'hold':0.5})
        with self.assertRaisesRegex(ValueError,'Unknown sensitivity'):m.set_tuning('H2D',{'preset':'max'})
        with self.assertRaisesRegex(ValueError,'Unknown printer'):m.set_tuning('X',{'preset':'normal'})
        m.set_tuning('H2D',{'threshold':0.3,'hold':0.2,'min_minutes':2,'needed_share':0.5,'focus':False})
        state=m.state('H2D')['tuning']
        self.assertEqual((state['preset'],state['threshold'],state['focus']),('custom',0.3,False))
        self.assertEqual(state['presets']['normal']['threshold'],0.6);self.assertEqual(state['nms_floor'],0.25)
        m.set_tuning('H2D',{'preset':'normal','focus':True})
        self.assertEqual(core.settings['ai_tuning'],{})

    async def test_close_up_follows_a_suspicious_spot(self):
        class Spotting(FakeBackend):
            def __init__(self):super().__init__(0.2);self.crops=[]
            async def score(self,jpeg,crops=None):
                self.crops.append(crops);return 0.2,[{'label':'spaghetti','score':0.2,'box':[0.7,0.1,0.8,0.2]}]
        core=FakeCore();backend=Spotting();m=self.monitor(core,backend=backend)
        await self.run_checks(m,2)
        self.assertEqual(len(backend.crops[0]),3)    # the default close-ups
        self.assertEqual(len(backend.crops[1]),4)    # plus one around the spot seen last time
        self.assertIn('suspicious spot',m.state('H2D')['focus'])
        m.set_tuning('H2D',{'preset':'normal','focus':False})
        await self.run_checks(m,1);self.assertEqual(len(backend.crops[2]),3)

    async def test_camera_view_gets_the_last_boxes(self):
        class Boxes(FakeBackend):
            async def score(self,jpeg,crops=None):
                return 0.5,[{'label':'spaghetti','score':0.5,'box':[0.6,0.2,0.7,0.3]},{'label':'print','score':0.9,'box':[0.4,0.1,0.8,0.5]},
                            {'label':'spaghetti','score':0.2,'box':None}]
        core=FakeCore();m=self.monitor(core,backend=Boxes())
        await self.run_checks(m,1)
        state=m.state('H2D')
        self.assertEqual([b['label'] for b in state['boxes']],['spaghetti','print'])   # only boxed detections
        self.assertEqual([b['counts'] for b in state['boxes']],[True,False])
        self.assertEqual((state['boxes_at'],state['threshold'],state['hold']),(self.clock.now,0.6,0.45))
        self.assertEqual(len(state['zoom']),2)   # where it zooms in next: the print area and the spot

    async def test_live_view_gets_boxes_while_idle(self):
        import time as _time
        class Boxes(FakeBackend):
            async def score(self,jpeg,crops=None):
                self.calls+=1;return 0.3,[{'label':'spaghetti','score':0.3,'box':[0.1,0.1,0.2,0.2]}]
        core=FakeCore(state='IDLE');backend=Boxes();m=self.monitor(core,backend=backend)
        await self.run_checks(m,1)
        self.assertEqual(backend.calls,0)                     # idle and no live view: nothing to look at
        core.live_cameras=SimpleNamespace(feeds={'H2D':SimpleNamespace(frame=b'live',updated=_time.time())})
        await self.run_checks(m,1)
        state=m.state('H2D')
        self.assertEqual(backend.calls,1);self.assertEqual(state['boxes'][0]['label'],'spaghetti')
        self.assertEqual(state['status'],'idle');core.notify.assert_not_awaited()   # display only, never judged

    async def test_stringing_can_count_as_a_failure_for_all_printers(self):
        class Stringy(FakeBackend):
            async def score(self,jpeg,crops=None):
                return 0.0,[{'label':'stringing','score':0.7,'box':[0.1,0.1,0.2,0.2]}]   # the helper counts only its own labels
        core=FakeCore();m=self.monitor(core,backend=Stringy())
        m.settings.update(classes=['spaghetti','stringing','warping','print'],labels=['spaghetti','warping'])
        await self.run_checks(m,1);self.assertEqual(m.state('H2D')['score'],0.0)
        with self.assertRaisesRegex(ValueError,'Choose at least one'):m.set_labels(['print'])   # an area class never counts
        m.set_labels(['stringing','spaghetti','warping'])
        self.assertEqual(core.settings['ai_labels'],['spaghetti','stringing','warping'])
        await self.run_checks(m,1);self.assertEqual(m.state('H2D')['score'],0.7)
        t=m.state('H2D')['tuning'];self.assertEqual((t['classes'],t['labels']),(['spaghetti','stringing','warping'],['spaghetti','stringing','warping']))

    async def test_dashboard_switch_makes_a_failure_pause_the_print(self):
        core=FakeCore();m=self.monitor(core,'notify',backend=FakeBackend(0.95))   # config.json says notify
        self.assertEqual(m.state('H2D')['action'],'notify')
        with self.assertRaisesRegex(ValueError,'pause or notify'):m.set_action('stop')
        m.set_action('pause')
        self.assertEqual((core.settings['ai_action'],m.state('H2D')['action']),('pause','pause'))
        await self.run_checks(m,40)
        self.engine.control.assert_awaited_once_with('H2D','pause','AI failure detection')

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


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    """A .hef that won't start (no AI HAT, driver missing) falls back to the .onnx next to it, says so, and slows down."""
    def helper(self,d):
        fake=Path(d)/'fake.py'
        fake.write_text('import json,sys\n'
                        'if sys.argv[1].endswith(".hef"):\n    print("HailoRTStatusException: 74",file=sys.stderr,flush=True);sys.exit(1)\n'
                        'print(json.dumps({"ready":True,"input":[640,640],"backend":"cpu"}),flush=True)\n'
                        'for line in sys.stdin:\n    print(json.dumps({"score":0.3,"detections":[]}),flush=True)\n')
        return fake

    async def run_with(self,d,make_onnx):
        import failureDetection.detection as detection
        model=Path(d)/'print_failure.hef';model.write_bytes(b'x')
        if make_onnx:(Path(d)/'print_failure.onnx').write_bytes(b'x')
        original=detection.WORKER;detection.WORKER=self.helper(d)
        backend=HailoBackend(settings(model=str(model),python=sys.executable))
        try:
            self.assertEqual(backend.settings['min_interval'],5)
            with self.assertLogs('failure-detection','INFO') as logs:
                await backend.warm()
            return backend,'\n'.join(logs.output)
        finally:
            await backend.close();detection.WORKER=original

    async def test_falls_back_to_the_cpu_model(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            backend,logs=await self.run_with(d,True)
        self.assertIn('falling back to the CPU model print_failure.onnx',logs)
        self.assertIn('AI model ready: print_failure.onnx on CPU (fallback)',logs)
        info=backend.describe()
        self.assertEqual((info['backend'],info['active_model']),('CPU','print_failure.onnx'))
        self.assertIn('HailoRTStatusException',info['backend_note'])
        self.assertEqual(backend.settings['min_interval'],10)

    async def test_no_fallback_reports_the_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            backend,logs=await self.run_with(d,False)
        self.assertIn('AI model did not start',logs)
        self.assertEqual(backend.describe()['backend'],'unavailable')
        self.assertIn('HailoRTStatusException',backend.describe()['backend_note'])

    async def test_hat_model_logs_where_it_runs(self):
        import tempfile,failureDetection.detection as detection
        with tempfile.TemporaryDirectory() as d:
            fake=Path(d)/'fake.py';model=Path(d)/'m.hef';model.write_bytes(b'x')
            fake.write_text('import json,sys\nprint(json.dumps({"ready":True,"input":[640,640],"backend":"hailo"}),flush=True)\nsys.stdin.read()\n')
            original=detection.WORKER;detection.WORKER=fake
            backend=HailoBackend(settings(model=str(model),python=sys.executable))
            try:
                with self.assertLogs('failure-detection','INFO') as logs:await backend.warm()
            finally:await backend.close();detection.WORKER=original
        self.assertIn('AI model ready: m.hef on AI HAT (Hailo-8)',logs.output[-1])
        self.assertEqual(backend.describe()['backend_note'],'')


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
