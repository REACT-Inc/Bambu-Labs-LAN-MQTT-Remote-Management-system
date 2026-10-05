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
