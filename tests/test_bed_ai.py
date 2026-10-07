"""Bed check AI (failureDetection/bed_model.py): Hailo's precompiled CLIP model is downloaded for this AI HAT in the
background (with progress in the status icon), each printer learns its own empty bed and bed with parts, and the
verdict is used by auto-reprint and Swapmod."""
import asyncio,json,math,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection import bed_model
from failureDetection.bed_model import BedAI,MIN_EACH,zoo_version
from failureDetection.auto_reprint import AutoReprint
from status_center import StatusCenter


def unit(*v):
    n=math.sqrt(sum(x*x for x in v));return [x/n for x in v]

EMPTY,PARTS=unit(1,0.1,0),unit(0.2,1,0)


class Clock:
    def __init__(self):self.now=1_000_000.0
    def __call__(self):return self.now


class Backend:
    """Stands in for HailoBackend: what the helper reported, and fingerprints for pictures."""
    def __init__(self):
        self.device={'hailort':'4.20.0','chip':'hailo8'};self.bed_ready=False;self.bed_error='';self.restarts=0;self.crops=[]
    async def restart(self):self.restarts+=1;self.bed_ready=True
    async def embed(self,jpeg,crops=None):
        self.crops.append(crops);return [{b'empty':EMPTY,b'parts':PARTS,b'between':unit(EMPTY[0]+PARTS[0],EMPTY[1]+PARTS[1],0)}[jpeg]]*(1+len(crops or []))


class BedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.picture=b'empty'
        self.center=StatusCenter()
        self.core=SimpleNamespace(DATA_DIR=d,settings={},names=lambda:['A1','H2D'],EXAMPLE_MODE=False,status_center=self.center,
                                  capture_still=AsyncMock(side_effect=lambda name,timeout:self.picture),state_data=lambda n:('FINISH',0,{},True))
        self.backend=Backend();self.clock=Clock()
        self.monitor=SimpleNamespace(settings={'model':'/models/print_failure.hef','bed_ai':{}},backend=self.backend)
        self.downloads=[]
        def fetch(url,path,progress):
            self.downloads.append(url);progress(0.5);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'hef');return 3
        self.bed=BedAI(self.core,self.monitor,clock=self.clock,fetch=fetch)
    async def asyncTearDown(self):self.tmp.cleanup()

    async def setup(self):
        await self.bed.tick();await self.bed.task

    def test_model_zoo_release_matches_hailort(self):
        self.assertEqual([zoo_version(v) for v in ('4.20.0','4.21.1','4.23.0','4.30.0','4.18.0','')],
                         ['v2.14.0','v2.15.0','v2.17.0','v2.17.0',None,None])

    async def test_downloads_the_model_for_this_ai_hat_in_the_background(self):
        await self.setup()
        self.assertEqual(self.downloads,['https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.14.0/hailo8/clip_resnet_50x4.hef'])
        self.assertTrue(self.monitor.settings['bed_model'].endswith('clip_resnet_50x4-v2.14.0-hailo8.hef'))
        self.assertEqual(self.backend.restarts,1);self.assertTrue(self.bed.ready)
        item=self.center.snapshot()['items'][0];self.assertEqual((item['title'],item['level']),('Bed check AI ready','ok'))
        await self.bed.tick();self.assertIsNone(self.bed.task.result())   # the next tick: nothing to do for a day
        self.assertEqual(len(self.downloads),1)
        # HailoRT updated (apt): the model for the new version is downloaded the next day, and the old one removed.
        self.backend.device['hailort']='4.21.0';self.clock.now+=bed_model.CHECK_EVERY+1
        await self.setup()
        self.assertIn('/v2.15.0/hailo8/',self.downloads[-1])
        self.assertEqual([p.name for p in (Path(self.tmp.name)/'models'/'bed').glob('*.hef')],['clip_resnet_50x4-v2.15.0-hailo8.hef'])

    async def test_download_progress_and_failure_show_in_the_status_icon(self):
        import threading
        seen,shown=[],threading.Event()
        def fetch(url,path,progress):   # runs in a thread: waits until its progress update is on screen, then fails
            progress(0.4);shown.wait(5);raise OSError('network unreachable')
        self.bed.fetch=fetch
        original=self.center.set
        def spy(key,title,detail='',level='busy',progress=None):
            seen.append((title,level,progress));original(key,title,detail,level,progress)
            if progress==0.4:shown.set()
        self.center.set=spy
        await self.setup();await asyncio.sleep(0)
        self.assertEqual(seen[0],('Downloading the bed check AI','busy',0))
        self.assertIn(('Downloading the bed check AI','busy',0.4),seen)
        item=self.center.snapshot()['items'][0];self.assertEqual((item['title'],item['level']),('Bed check AI setup failed','warn'))
        self.assertIn('network unreachable',item['detail']);self.assertFalse(self.bed.ready)

    async def test_a_late_progress_update_does_not_cover_the_result(self):
        late=[]
        def fetch(url,path,progress):
            late.append(progress);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'hef')
        self.bed.fetch=fetch
        await self.setup()
        late[0](0.99);await asyncio.sleep(0.01)   # handled only after the download ended
        item=self.center.snapshot()['items'][0];self.assertEqual((item['title'],item['level']),('Bed check AI ready','ok'))

    async def test_nothing_without_an_ai_hat(self):
        self.monitor.settings['model']='/models/print_failure.onnx'   # CPU model: no AI HAT
        await self.bed.tick();self.assertIsNone(self.bed.task);self.assertEqual(self.center.snapshot()['items'],[])
        self.monitor.settings['model']='/m.hef';self.backend.device={'hailort':'4.20.0','chip':'hailo10h'}
        await self.setup()
        self.assertEqual(self.downloads,[]);self.assertEqual(self.center.snapshot()['items'][0]['title'],'Bed check AI not available')

    async def test_learns_each_printer_and_decides(self):
        await self.setup()
        result=await self.bed.check('A1');self.assertEqual(result['state'],'unknown');self.assertIn('still learning',result['detail'])
        for _ in range(MIN_EACH):
            self.picture=b'empty';await self.bed.learn('A1','empty','print started')
            self.picture=b'parts';await self.bed.learn('A1','parts','print finished',still=lambda:True)
        self.assertEqual(self.bed.counts('A1'),{'empty':MIN_EACH,'parts':MIN_EACH});self.assertEqual(self.bed.counts('H2D'),{'empty':0,'parts':0})
        self.picture=b'empty';self.assertEqual((await self.bed.check('A1'))['state'],'clear')
        self.picture=b'parts';self.assertEqual((await self.bed.check('A1'))['state'],'parts')
        self.picture=b'between';self.assertEqual((await self.bed.check('A1'))['state'],'unknown')   # too close to call
        # A print started meanwhile: no "parts" picture of whatever is on the bed now.
        await self.bed.learn('A1','parts','print finished',still=lambda:False);self.assertEqual(self.bed.counts('A1')['parts'],MIN_EACH)

    async def test_your_answer_teaches_it(self):
        await self.setup()
        with self.assertRaisesRegex(ValueError,'Check the bed first'):self.bed.answer('A1',True,'Ana')
        self.picture=b'parts';await self.bed.check('A1')
        self.assertEqual(self.bed.answer('A1',False,'Ana'),{'empty':0,'parts':1})
        saved=json.loads(self.bed.path('A1').read_text())['parts'][0];self.assertEqual(saved['source'],'answer by Ana')

    async def test_calibrated_bed_is_cropped(self):
        await self.setup()
        self.core.settings['ai_calibration']={'A1':{'corners':[[0.2,0.5],[0.8,0.5],[0.9,0.9],[0.1,0.9]]}}
        await self.bed.check('A1')
        self.assertEqual(self.backend.crops[-1],[[0.07,0.35,0.93,0.93]])

    async def test_auto_reprint_asks_the_ai_first(self):
        await self.setup()
        self.core.printer_config=lambda n:{'camera_type':'jpeg_tcp'}
        reprints=AutoReprint(self.core,SimpleNamespace(),{},clock=self.clock);reprints.bed_ai=self.bed
        for _ in range(MIN_EACH):
            self.picture=b'empty';await self.bed.learn('A1','empty','t')
            self.picture=b'parts';await self.bed.learn('A1','parts','t')
        self.picture=b'parts'
        result=await reprints.bed_state('A1',refresh=True)
        self.assertEqual((result['state'],result['by']),('parts','ai'));self.assertTrue(result['detail'].startswith('AI: '))
        # Not learned yet: the picture comparison decides, and says what the AI thought.
        result=await reprints.bed_state('H2D',refresh=True)
        self.assertEqual(result['state'],'unknown');self.assertIn('AI still learning',result['detail'])


if __name__=='__main__':
    unittest.main()
