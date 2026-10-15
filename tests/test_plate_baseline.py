"""More reliable spaghetti detection on plates the model takes for spaghetti: while a print starts (warm-up, layer 0
or 1) the plate is still empty, so what the model scores then is its false-alarm level for this plate, and the
print's failure scores are raised above it (Judge.baseline). Training pictures are off by default: they never change
the AI by themselves."""
import asyncio,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection.detection import FailureMonitor,Judge,settings_for
from failureDetection.training import TrainingPictures


class Clock:
    def __init__(self):self.now=1000.0
    def __call__(self):return self.now


def settings(**overrides):
    return settings_for({'failure_detection':{'enabled':True,'model':'m.hef',**overrides}})


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.judge=Judge(settings(),self.clock);self.judge.reset('benchy.3mf');self.n=0
    def add(self,score,start=False,step=30):
        self.clock.now+=step;self.n+=1
        return self.judge.add(score,f'frame{self.n}',start=start)
    def warm_up(self,*scores,start=True):
        for score in scores:self.assertEqual(self.add(score,start=start),'warming_up')

    def test_a_plate_the_model_takes_for_spaghetti_raises_this_prints_scores(self):
        self.warm_up(0.48,0.5,0.52,0.5,0.49)   # nothing printed yet: the model's score for the plate itself
        self.assertEqual(self.judge.baseline(),0.5)
        self.assertEqual((self.judge.threshold(),self.judge.hold()),(0.65,0.5))   # 0.5 + 0.15; the hold moves as much
        self.assertIn('This plate scored 50% before anything was printed',self.judge.plate_text())
        verdicts=[self.add(0.62) for _ in range(20)]     # would be a failure at the usual 60%
        self.assertNotIn('failure',verdicts);self.assertEqual(self.judge.failing(),0)
        verdicts=[self.add(0.85) for _ in range(12)]     # a real failure scores well above the plate
        self.assertEqual(verdicts[-1],'failure')

    def test_a_low_scoring_plate_changes_nothing(self):
        self.warm_up(0.05,0.1,0.02)
        self.assertEqual((self.judge.raised(),self.judge.threshold(),self.judge.hold()),(0,0.6,0.45))
        self.assertEqual(self.judge.plate_text(),'')
        self.clock.now+=200   # past the warm-up
        self.assertEqual([self.add(0.7) for _ in range(9)][-1],'failure')

    def test_one_odd_frame_does_not_count(self):
        self.warm_up(0.1,0.97,0.12,0.08)   # the second highest decides
        self.assertEqual(self.judge.baseline(),0.12);self.assertEqual(self.judge.threshold(),0.6)

    def test_only_frames_from_the_start_of_the_print(self):
        self.warm_up(0.9,0.9,0.9,start=False)   # the watch began mid-print: a real failure could be on the plate
        self.assertEqual(self.judge.baseline(),0.0)
        self.warm_up(0.3)   # one starting frame isn't enough
        self.assertEqual(self.judge.baseline(),0.0)

    def test_a_plate_it_cannot_tell_from_a_failure(self):
        self.warm_up(0.9,0.92,0.95)
        self.assertEqual(self.judge.threshold(),0.99)
        self.assertIn("can't tell a failure from the plate: this print isn't judged",self.judge.plate_text())
        self.assertNotIn('failure',[self.add(0.95) for _ in range(20)])

    def test_learned_again_for_every_print_and_can_be_turned_off(self):
        self.warm_up(0.5,0.5)
        self.judge.reset('next.3mf');self.assertEqual((self.judge.plate,self.judge.baseline()),([],0.0))
        judge=Judge(settings(plate_baseline=False),self.clock);judge.reset('job')
        for i in range(3):judge.add(0.5,f'x{i}',start=True)
        self.assertEqual((judge.baseline(),judge.threshold()),(0.0,0.6))

    def test_settings(self):
        self.assertEqual((settings()['plate_baseline'],settings()['plate_margin']),(True,0.15))
        self.assertEqual(settings(plate_margin=2)['plate_margin'],0.5);self.assertEqual(settings(plate_margin=0)['plate_margin'],0.05)
        judge=Judge(settings(plate_margin=0.3),self.clock);judge.reset('job')
        for i in range(2):judge.add(0.5,f'y{i}',start=True)
        self.assertEqual(judge.threshold(),0.8)


class Core:
    EXAMPLE_MODE=False;RED=1
    def __init__(self):
        self.settings={};self.layer=0;self.pictures=0;self.events=[];self.notify=AsyncMock()
        self.event_listener=lambda *a:self.events.append(a)
        self.CONFIG={'failure_detection':{'enabled':True,'model':'m.hef','action':'notify'}}
    def names(self):return ['A1']
    def printer_config(self,name):return {'camera_type':'jpeg_tcp'}
    def state_data(self,name):return ('RUNNING',0,{'subtask_name':'benchy','layer_num':self.layer},True)
    async def snapshot(self,name,timeout=25,max_age=60):
        self.pictures+=1;return b'jpeg%d'%self.pictures
    def save_settings(self,updated):self.settings=dict(updated)


class Scores:
    def __init__(self):self.value=0.5
    async def score(self,jpeg,crops=None):
        return self.value,[{'label':'spaghetti','score':self.value,'box':[0.4,0.6,0.5,0.7]}]


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.core=Core();self.core.DATA_DIR=Path(self.tmp.name);self.backend=Scores();self.clock=Clock()
        self.m=FailureMonitor(self.core,SimpleNamespace(),self.backend,self.clock)
    async def asyncTearDown(self):self.tmp.cleanup()
    async def checks(self,count,score):
        self.backend.value=score
        for _ in range(count):
            self.clock.now+=30;await self.m.check('A1')

    async def test_the_plate_does_not_raise_alarms_but_a_real_failure_does(self):
        await self.checks(6,0.5)   # the first 3 minutes, layer 0: heating and levelling over the empty plate
        self.assertIn('learns what this plate looks like',self.m.state('A1')['message'])
        self.core.layer=5
        await self.checks(20,0.62)
        self.core.notify.assert_not_awaited()
        state=self.m.state('A1')
        self.assertIn('This plate scored 50%',state['message'])
        self.assertEqual((state['threshold'],state['hold']),(0.65,0.5))   # the camera view draws the plate's boxes as not counting
        self.assertEqual([e[2] for e in self.core.events if e[1]=='AI plate baseline'],
                         ['This plate scored 50% before anything was printed, so frames count as failing from 65% for this print.'])
        await self.checks(12,0.85)
        self.core.notify.assert_awaited_once()

    async def test_the_boxes_on_other_camera_views_use_the_same_scores(self):
        await self.checks(6,0.5);self.core.layer=5;await self.checks(1,0.62)
        overlay=await self.m.look('A1',b'picture')
        self.assertEqual((overlay['threshold'],overlay['hold']),(0.65,0.5))

    async def test_check_ai_now_does_not_act_on_the_plate(self):
        await self.checks(6,0.5);self.core.layer=5
        self.backend.value=0.62;self.clock.now+=30
        result=await self.m.check('A1',on_request=True,picture=b'asked')
        self.assertEqual((result['verdict'],result['acted'],result['threshold']),('watching',False,0.65))
        self.core.notify.assert_not_awaited()

    async def test_a_printer_that_reports_no_layer_is_judged_as_before(self):
        self.core.state_data=lambda name:('RUNNING',0,{'subtask_name':'benchy'},True)
        await self.checks(6,0.5);await self.checks(12,0.62)
        self.core.notify.assert_awaited_once()


class CollectingTests(unittest.TestCase):
    """Training pictures are only kept when someone turns collecting on."""
    def test_off_by_default_and_the_dashboard_switch(self):
        with tempfile.TemporaryDirectory() as d:
            core=SimpleNamespace(DATA_DIR=Path(d),settings={})
            pictures=TrainingPictures(core,{})
            self.assertFalse(pictures.collecting);self.assertIsNone(pictures.save('A1','job',b'jpg',0.9,'failure'))
            self.assertIsNone(pictures.save_empty('A1',b'jpg'));self.assertEqual(pictures.summary()['enabled'],False)
            core.settings['ai_collect']=True
            self.assertTrue(pictures.collecting);self.assertIsNotNone(pictures.save('A1','job',b'jpg',0.9,'failure'))
            on_in_config=TrainingPictures(core,{'enabled':True});core.settings['ai_collect']=False
            self.assertFalse(on_in_config.collecting)   # the dashboard's choice wins over config.json
            del core.settings['ai_collect'];self.assertTrue(on_in_config.collecting)

    def test_set_collect(self):
        with tempfile.TemporaryDirectory() as d:
            core=Core();core.DATA_DIR=Path(d)
            m=FailureMonitor(core,SimpleNamespace(),Scores(),Clock())
            self.assertFalse(m.training.collecting)
            m.set_collect(True)
            self.assertEqual(core.settings['ai_collect'],True);self.assertTrue(m.training.collecting)
            self.assertIn('Collecting training pictures',core.events[-1][2])
            m.set_collect(False);self.assertFalse(m.training.collecting)
            with self.assertRaisesRegex(ValueError,'on or off'):m.set_collect('yes')

    def test_dashboard_route_and_switch(self):
        import inspect,dashboard
        self.assertIn("'/api/ai-training/collect'",inspect.getsource(dashboard.Dashboard.__init__))
        page=(Path(__file__).parents[1]/'static/index.html').read_text()
        self.assertIn('id="aiCollect"',page)

    def test_the_route_turns_it_on(self):
        import dashboard
        with tempfile.TemporaryDirectory() as d:
            core=Core();core.DATA_DIR=Path(d)
            m=FailureMonitor(core,SimpleNamespace(),Scores(),Clock())
            class Request:
                async def json(self):return {'on':True}
            response=asyncio.run(dashboard.Dashboard.ai_training_collect(SimpleNamespace(failure=m),Request()))
            self.assertEqual(response.status,200);self.assertTrue(m.training.collecting)


if __name__=='__main__':
    unittest.main()
