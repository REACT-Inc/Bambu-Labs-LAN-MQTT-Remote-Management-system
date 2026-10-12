"""Training pictures that teach the failure model what is NOT a failure: each printer's empty bed (a print started on a
cleared plate, or "It's clear" to the bed check) and the false alarms (pictures the AI scored as failing in prints that
finished), sorted into their own folders of the ZIP with instructions to mark them Null in Roboflow. A model trained
without them can take the plate's texture for spaghetti."""
import asyncio,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection.training import EMPTY_BED,README,TrainingPictures

EMPTY=b'\xff\xd8empty\xff\xd9'


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=[1_800_000_000.0]
        self.pictures=TrainingPictures(SimpleNamespace(DATA_DIR=Path(self.tmp.name)),{},clock=lambda:self.now[0])
    def tearDown(self):self.tmp.cleanup()
    def tick(self,seconds=61):self.now[0]+=seconds

    def export(self,thresholds=None):
        target=Path(self.tmp.name)/'out.zip'
        self.pictures.export({'good':'finished','bad':'failed'},target,thresholds)
        return sorted(n for n in zipfile.ZipFile(target).namelist() if n.endswith('.jpg'))

    def test_empty_beds_and_false_alarms_get_their_own_folders(self):
        kept=self.pictures.save_empty('Mini 2',EMPTY)
        self.assertEqual((kept.parent.name,kept.read_bytes()),(EMPTY_BED,EMPTY))
        self.tick();self.pictures.save('Mini 2','good',b'jpg',0.72,'suspect')    # the AI thought it failed, but it finished
        self.tick();self.pictures.save('Mini 2','good',b'jpg',0.20,'watching')
        self.tick();self.pictures.save('Mini 2','bad',b'jpg',0.80,'failure')
        names=self.export({'Mini 2':0.6})
        self.assertEqual([n.split('/')[0] for n in names],['empty-bed','failed','false-alarms','finished'])
        self.assertEqual(names[0],f'empty-bed/Mini_2/{kept.name}')
        self.assertTrue(names[2].endswith('_score0.72.jpg'))
        # Without the failure scores (or for a printer that's gone) nothing is called a false alarm.
        self.assertEqual([n.split('/')[0] for n in self.export()],['empty-bed','failed','finished','finished'])
        self.assertEqual([n.split('/')[0] for n in self.export({'Other':0.1})],['empty-bed','failed','finished','finished'])

    def test_the_readme_says_to_mark_them_null(self):
        for words in ('empty-bed/','false-alarms/','Null','leaves pictures that have no boxes','--healthy empty-bed'):
            self.assertIn(words,README)
        self.assertNotIn('Leave pictures of good prints without boxes',README)   # Roboflow drops those

    def test_same_limits_as_the_other_pictures(self):
        self.assertIsNone(TrainingPictures(SimpleNamespace(DATA_DIR=Path(self.tmp.name)),{'enabled':False}).save_empty('P',EMPTY))
        self.assertIsNone(TrainingPictures(SimpleNamespace(),{}).save_empty('P',EMPTY))   # no data folder
        self.assertIsNone(self.pictures.save_empty('P',b''))
        with patch('shutil.disk_usage',return_value=SimpleNamespace(free=300*1024**2)):
            self.assertIsNone(self.pictures.save_empty('P',EMPTY))
        self.assertIn('only 0.3 GB free',self.pictures.summary()['problem'])
        first,second=self.pictures.save_empty('P',EMPTY),self.pictures.save_empty('P',EMPTY)   # same second
        self.assertNotEqual(first,second);self.assertEqual(self.pictures.summary()['count'],2)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self,camera='jpeg_tcp',example=False,auto_reprint=True):
        from failureDetection.detection import FailureMonitor
        self.tmp=tempfile.TemporaryDirectory()
        self.core=SimpleNamespace(DATA_DIR=Path(self.tmp.name),settings={},names=lambda:['James'],printer_config=lambda n:{'camera_type':camera},
            state_data=lambda n:('IDLE',0,{},True),capture_still=AsyncMock(return_value=EMPTY),EXAMPLE_MODE=example,notify=AsyncMock(),
            save_settings=lambda s:None,RED=1,
            CONFIG={'failure_detection':{'enabled':True,'model':'m.onnx','threshold':0.45,'auto_reprint':{'enabled':auto_reprint}}})
        return FailureMonitor(self.core,SimpleNamespace(),backend=SimpleNamespace())
    def tearDown(self):self.tmp.cleanup()
    def kept(self,m):return [p.read_bytes() for p in m.training.pictures() if p.parent.name==EMPTY_BED]

    async def test_a_queue_print_start_keeps_the_empty_bed(self):
        m=self.monitor()
        await m.start_approved({'printer':'James','options':{}},'web administrator')
        self.assertEqual(self.kept(m),[EMPTY])
        self.assertEqual(self.core.capture_still.await_count,1)   # the same picture as the auto-reprint's reference
        self.assertEqual(m.reprints.reference('James').read_bytes(),EMPTY)
        await m.start_approved({'printer':'James','options':{'swap':True}},'Swapmod')   # the old part is still on the plate
        await m.start_approved({'printer':'James','options':{}},'AI auto reprint')      # nobody looked at the plate
        self.assertEqual(len(self.kept(m)),1)

    async def test_without_auto_reprint_too_but_never_without_a_camera_or_in_demo_mode(self):
        m=self.monitor(auto_reprint=False)
        await m.start_approved({'printer':'James','options':{}},'Ana')
        self.assertEqual(self.kept(m),[EMPTY]);self.assertFalse(m.reprints.reference('James').exists())
        for options in ({'camera':''},{'example':True}):
            self.tmp.cleanup();m=self.monitor(**options)
            await m.start_approved({'printer':'James','options':{}},'Ana')
            self.assertEqual(self.kept(m),[])
        self.tmp.cleanup();m=self.monitor()
        self.core.capture_still.side_effect=TimeoutError
        await m.start_approved({'printer':'James','options':{}},'Ana')   # no picture: the start goes on as before
        self.assertEqual(self.kept(m),[])

    async def test_answering_its_clear_keeps_the_picture_the_bed_check_looked_at(self):
        m=self.monitor()
        def looked_at(picture):
            m.bed.last['James']=dict(state='unknown',detail='',checked=m.bed.clock(),vector=[1.0,0.0],picture=picture)
        looked_at(b'clear bed');self.assertEqual(await m.bed_answer('James',True,'Ana'),{'empty':1,'parts':0})
        looked_at(b'parts on it');self.assertEqual(await m.bed_answer('James',False,'Ana'),{'empty':1,'parts':1})
        self.assertEqual(self.kept(m),[b'clear bed'])
        with self.assertRaisesRegex(ValueError,'Check the bed first'):await m.bed_answer('James',True,'Ana')

    async def test_the_failure_scores_used_for_the_zip(self):
        m=self.monitor();self.assertEqual(m.training_thresholds(),{'James':0.45})   # config.json's
        self.core.save_settings=lambda updated:setattr(self.core,'settings',dict(updated))
        m.set_tuning('James',{'preset':'cautious'});self.assertEqual(m.training_thresholds(),{'James':0.6})   # the printer's own


class BedCheckPictureTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_checked_picture_is_kept_but_never_shown(self):
        from failureDetection.bed_model import BedAI
        with tempfile.TemporaryDirectory() as d:
            core=SimpleNamespace(DATA_DIR=Path(d),settings={},capture_still=AsyncMock(return_value=EMPTY))
            backend=SimpleNamespace(embed=AsyncMock(return_value=[[1.0,0.0]]))
            bed=BedAI(core,SimpleNamespace(settings={'model':'m.hef','bed_ai':{}},backend=backend))
            with patch.object(BedAI,'ready',True):
                await bed.check('A1')
                self.assertEqual(bed.last['A1']['picture'],EMPTY);self.assertEqual(core.capture_still.await_count,1)
                self.assertNotIn('picture',bed.state('A1')['last'])
                core.capture_still.return_value=None
                self.assertIn('No camera picture',(await bed.check('A1'))['detail'])
                self.assertEqual(core.capture_still.await_count,2)   # asked once, not again inside


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard's training ZIP and bed check answer go through the monitor."""
    async def test_zip_and_answer(self):
        import dashboard
        with tempfile.TemporaryDirectory() as d:
            pictures=TrainingPictures(SimpleNamespace(DATA_DIR=Path(d)),{})
            pictures.save('James','job1',b'jpg',0.9,'failure');pictures.save_empty('James',EMPTY)
            answers=[]
            async def bed_answer(name,clear,author):answers.append((name,clear,author));return {'empty':1,'parts':0}
            failure=SimpleNamespace(enabled=True,training=pictures,training_thresholds=lambda:{'James':0.5},bed_answer=bed_answer,
                                    bed=SimpleNamespace(ready=True),store_event=lambda *a:None)
            fake=SimpleNamespace(failure=failure,store=SimpleNamespace(jobs=lambda:[{'id':'job1','status':'finished'}]),
                                 core=SimpleNamespace(DATA_DIR=Path(d),names=lambda:['James']))
            await dashboard.Dashboard.ai_training_zip(fake,None)
            names=zipfile.ZipFile(Path(d)/'training-pictures.zip').namelist()
            self.assertTrue(any(n.startswith('false-alarms/James/job1_') for n in names))
            self.assertTrue(any(n.startswith('empty-bed/James/') for n in names))

            class Request(dict):
                match_info={'name':'James'}
                async def json(self):return {'action':'answer','clear':True}
            request=Request(session={'name':'Ana'})
            response=await dashboard.Dashboard.ai_bed(fake,request)
            self.assertEqual(answers,[('James',True,'Ana')]);self.assertEqual(response.status,200)


if __name__=='__main__':
    unittest.main()
