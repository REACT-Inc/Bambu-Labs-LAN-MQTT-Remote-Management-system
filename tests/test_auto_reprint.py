"""Automatic reprint after an AI-paused failure (#67): bed references, availability, countdown, reprint, model updates."""
import asyncio,io,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).parents[1]))
from queueing import Store,options
from failureDetection import auto_reprint as ar
from failureDetection.auto_reprint import AutoReprint,AUTHOR
from tests.test_better_queue import make_3mf

HOUR=3600


class Clock:
    def __init__(self):self.now=1_000_000.0
    def __call__(self):return self.now


class FakeEngine:
    def __init__(self,core,store):
        self.core,self.store=core,store;self.started=[];self.controls=[]
    def ready(self,name,override_error=False):
        state,error,_,connected=self.core.state_data(name)
        if not connected:raise ValueError('Printer is offline or telemetry is stale. Wait for a fresh report.')
        if state not in ('IDLE','FINISH'):raise ValueError(f'Printer reports {state}. Stop any current print.')
        if error:raise ValueError(f'Printer reports error {error}. Inspect the printer.')
    async def control(self,name,action,author):
        self.controls.append((name,action,author))
        job=self.store.active(name)
        if action=='stop' and job:self.store.set_status(job['id'],'needs_review','Stop requested.')
        self.core.states[name]='IDLE'
    async def start(self,job_id,confirmed,author,override_error=False):
        assert confirmed is True
        self.started.append((job_id,author));self.store.set_status(job_id,'staging')


class ReprintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        configs={'Mini 1':{'model':'A1 mini','camera_type':'jpeg_tcp'},'Mini 2':{'model':'A1 mini','camera_type':'jpeg_tcp'},
                 'Mini 3':{'model':'A1 mini','camera_type':'jpeg_tcp'},'H2D':{'model':'H2D','camera_type':'rtsp'}}
        self.states={'Mini 1':'PAUSE','Mini 2':'IDLE','Mini 3':'IDLE','H2D':'IDLE'}
        self.core=SimpleNamespace(DATA_DIR=d,settings={},names=lambda:list(configs),printer_config=configs.get,
            states=self.states,state_data=lambda n:(self.states[n],0,{},True),display_name=lambda n:n,
            capture_still=AsyncMock(return_value=b'\xff\xd8bed\xff\xd9'),notify=AsyncMock(),BLUE=1,YELLOW=2,EXAMPLE_MODE=False)
        self.store=Store(d/'db');self.engine=FakeEngine(self.core,self.store)
        (d/'uploads').mkdir()
        self.asset=str(make_3mf(d/'uploads'/'part.3mf'))
        self.job=self.store.add('Mini 1','Bracket',self.asset,'',options(1),'t')
        self.store.set_status(self.job['id'],'paused')
        self.clock=Clock()
        self.beds={'Mini 2':'clear','Mini 3':'parts'}
        self.make()
    def make(self):
        self.reprints=AutoReprint(self.core,self.engine,{},clock=self.clock)
        async def bed_state(name,refresh=False):return dict(state=self.beds.get(name,'unknown'),detail='test',checked=self.clock())
        self.reprints.bed_state=bed_state
    async def asyncTearDown(self):self.store.db.close();self.tmp.cleanup()

    async def test_availability_reasons(self):
        self.store.add('Mini 3','Other',None,'x.3mf',options(),'t');self.store.set_status(self.store.jobs('Mini 3')[0]['id'],'printing')
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertEqual(set(rows),{'Mini 2','Mini 3','H2D'})                      # never the failed printer itself
        self.assertTrue(rows['Mini 2']['ok']);self.assertEqual(rows['Mini 2']['bed'],'clear')
        self.assertEqual(rows['Mini 3']['reason'],'busy with another queue job')
        self.assertEqual(rows['H2D']['reason'],'different printer model')
        self.assertIn('Available for a reprint: Mini 2',self.reprints.state('Mini 1')['available'])

    async def test_parts_on_bed_or_unknown_bed_blocks(self):
        self.beds={'Mini 2':'unknown','Mini 3':'parts'}
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertFalse(rows['Mini 2']['ok']);self.assertIn('not confirmed empty',rows['Mini 2']['reason'])
        self.assertEqual(rows['Mini 3']['reason'],'parts on the bed')
        self.states['Mini 2']='RUNNING'
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertIn('printer reports running',rows['Mini 2']['reason'])

    async def test_countdown_then_reprint(self):
        self.reprints.flag('Mini 1',self.job)
        await self.reprints.tick();self.assertEqual(self.engine.started,[])        # not due yet
        self.assertTrue(self.reprints.state('Mini 1')['pending'])
        self.clock.now+=12*HOUR
        await self.reprints.tick()
        self.assertEqual(self.engine.controls,[('Mini 1','stop',AUTHOR)])         # the paused print is stopped
        self.assertEqual(self.store.get(self.job['id'])['status'],'failed')
        copy=self.store.get(self.engine.started[0][0])
        self.assertEqual((copy['printer'],copy['label'],self.engine.started[0][1]),('Mini 2','Bracket',AUTHOR))
        self.assertFalse(self.reprints.state('Mini 1')['pending'])
        self.assertIn('AI reprint started',self.core.notify.await_args.args[1])

    async def test_resumed_or_stopped_print_is_left_alone(self):
        self.reprints.flag('Mini 1',self.job)
        self.store.set_status(self.job['id'],'printing');self.states['Mini 1']='RUNNING'   # someone resumed it
        self.clock.now+=13*HOUR;await self.reprints.tick()
        self.assertEqual(self.engine.started,[]);self.assertFalse(self.reprints.state('Mini 1')['pending'])
        self.assertIn('Automatic reprint not needed',[e['title'] for e in self.store.events()])

    async def test_waits_when_no_printer_is_free(self):
        self.beds={'Mini 2':'parts','Mini 3':'parts'}
        self.reprints.flag('Mini 1',self.job);self.clock.now+=12*HOUR
        await self.reprints.tick();await self.reprints.tick()
        self.assertEqual(self.engine.started,[]);self.assertEqual(self.core.notify.await_count,1)   # told once, not every minute
        self.assertIn('parts on the bed',self.reprints.state('Mini 1')['note'])
        self.beds['Mini 3']='clear';await self.reprints.tick()
        self.assertEqual(self.store.get(self.engine.started[0][0])['printer'],'Mini 3')

    async def test_reprint_now_and_cancel(self):
        self.reprints.flag('Mini 1',self.job)
        self.reprints.cancel('Mini 1','web');self.assertFalse(self.reprints.state('Mini 1')['pending'])
        with self.assertRaises(ValueError):self.reprints.cancel('Mini 1','web')
        copy=await self.reprints.reprint('Mini 1','web administrator')   # immediate, without a countdown
        self.assertEqual(copy['printer'],'Mini 2')
        with self.assertRaisesRegex(ValueError,'no paused queue job'):await self.reprints.reprint('Mini 2','web')

    async def test_countdown_survives_a_restart(self):
        self.reprints.flag('Mini 1',self.job);self.make()
        self.assertTrue(self.reprints.state('Mini 1')['pending'])
        self.assertEqual(self.reprints.due(self.job['id']),self.clock.now+12*HOUR)

    async def test_settings(self):
        self.assertEqual(ar.settings_for({'after_hours':-3,'bed_threshold':9})['after_hours'],0.0)
        reprints=AutoReprint(self.core,self.engine,{'enabled':False})
        reprints.flag('Mini 1',self.job);self.assertFalse(reprints.state('Mini 1')['pending'])

    async def test_empty_bed_reference(self):
        reprints=AutoReprint(self.core,self.engine,{})
        await reprints.capture_reference({'printer':'Mini 2'},'web administrator')
        self.assertEqual(reprints.reference('Mini 2').read_bytes(),b'\xff\xd8bed\xff\xd9')
        reprints.reference('Mini 2').unlink()
        await reprints.capture_reference({'printer':'Mini 2'},AUTHOR)              # an automatic start isn't a human check
        self.assertFalse(reprints.reference('Mini 2').exists())


class BedCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest('numpy/Pillow not installed')
        self.tmp=tempfile.TemporaryDirectory();self.d=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def bed(self,path,part=None,light=1.0):
        from PIL import Image,ImageDraw,ImageEnhance
        im=Image.new('RGB',(1280,720),(40,40,40));d=ImageDraw.Draw(im)
        d.polygon([(128,648),(1152,648),(960,144),(320,144)],fill=(120,120,125))
        if part:d.rectangle(part,fill=(230,60,40))
        ImageEnhance.Brightness(im).enhance(light).save(path,quality=75);return path

    def test_compare(self):
        from failureDetection import bed_check
        corners=[[0.1,0.9],[0.9,0.9],[0.75,0.2],[0.25,0.2]]
        ref=self.bed(self.d/'ref.jpg')
        self.assertTrue(bed_check.compare(ref,self.bed(self.d/'a.jpg',light=0.7),corners)['clear'])     # lights dimmed
        self.assertFalse(bed_check.compare(ref,self.bed(self.d/'b.jpg',part=(600,400,660,460)),corners)['clear'])
        self.assertFalse(bed_check.compare(ref,self.bed(self.d/'c.jpg',part=(600,400,660,460)),None)['clear'])

    async def test_through_the_auto_reprint_check(self):
        ref=self.bed(self.d/'ref.jpg');part=self.bed(self.d/'part.jpg',part=(500,300,800,500)).read_bytes()
        core=SimpleNamespace(DATA_DIR=self.d,settings={},printer_config=lambda n:{'camera_type':'rtsp'},capture_still=AsyncMock(return_value=part))
        reprints=AutoReprint(core,None,{},python=sys.executable)
        reprints.beds.mkdir();os.replace(ref,reprints.reference('P'))
        self.assertEqual((await reprints.bed_state('P'))['state'],'parts')
        core.capture_still.return_value=self.bed(self.d/'empty.jpg').read_bytes()
        self.assertEqual((await reprints.bed_state('P'))['state'],'parts')          # cached for 10 minutes
        self.assertEqual((await reprints.bed_state('P',refresh=True))['state'],'clear')
        reprints.reference('P').unlink()
        self.assertIn('no empty-bed picture',(await reprints.bed_state('P',refresh=True))['detail'])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_pause_starts_the_countdown_and_lists_printers(self):
        from failureDetection.detection import FailureMonitor
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'db');job=store.add('Mini 1','Bracket',None,'x.3mf',options(),'t');store.set_status(job['id'],'paused')
            states={'Mini 1':'RUNNING'}
            core=SimpleNamespace(DATA_DIR=Path(d),settings={},names=lambda:['Mini 1'],printer_config=lambda n:{'camera_type':'rtsp'},
                state_data=lambda n:(states[n],0,{},True),notify=AsyncMock(),save_settings=lambda s:None,RED=1,
                CONFIG={'failure_detection':{'enabled':True,'model':'m.onnx','action':'pause'}})
            async def control(name,action,author):states[name]='PAUSE'
            engine=SimpleNamespace(store=store,control=control)
            monitor=FailureMonitor(core,engine,backend=SimpleNamespace(),clock=Clock());monitor.pause_poll=0
            judge=SimpleNamespace(frames=[(0,0.9),(300,0.9)],failing=lambda:2)
            await monitor.act('Mini 1','x.3mf',judge,'spaghetti')
            self.assertTrue(monitor.reprints.state('Mini 1')['pending'])
            text=core.notify.await_args.args[2]
            self.assertIn('No other printer to reprint on',text);self.assertIn('within 12 h',text)
            store.db.close()


class ModelUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from failureDetection.model_updates import ModelUpdates
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.models=d/'opt-models';self.models.mkdir()
        self.current=self.models/'print_failure.onnx';self.current.write_bytes(b'model-1')
        from failureDetection import setup_ai
        (self.models/'.downloaded.json').write_text(json.dumps({'print_failure.onnx':setup_ai.sha256(self.current)}))
        saved={}
        self.core=SimpleNamespace(DATA_DIR=d/'data',settings=saved,save_settings=lambda new:(saved.clear(),saved.update(new)))
        self.monitor=SimpleNamespace(settings={'model':str(self.current),'classes':['spaghetti'],'labels':['spaghetti'],'python':'py','input_size':640},
                                     backend=SimpleNamespace(close=AsyncMock()),geometry=SimpleNamespace(labels=set()),store_event=lambda *a:None)
        self.updates=ModelUpdates(self.core,self.monitor)
    async def asyncTearDown(self):self.tmp.cleanup()

    def release(self,data):
        import hashlib
        return {'assets':[{'name':'print_failure.onnx','url':'u','size':len(data),'digest':'sha256:'+hashlib.sha256(data).hexdigest()}]}

    async def run_check(self,data,works=True):
        def download(asset,target,token=''):Path(target).write_bytes(data)
        with patch('failureDetection.setup_ai.api',return_value=self.release(data)),patch('failureDetection.setup_ai.download',side_effect=download),\
             patch('failureDetection.setup_ai.runs',return_value=(works,'ready' if works else 'broken')):
            return await self.updates.check(force=True)

    async def test_newer_model_switched_to(self):
        message=await self.run_check(b'model-2')
        self.assertIn('Updated the AI model',message)
        new=Path(self.monitor.settings['model']);self.assertEqual(new.read_bytes(),b'model-2');self.assertEqual(new.parent,self.updates.folder)
        self.assertEqual(self.core.settings['ai_model']['path'],str(new));self.monitor.backend.close.assert_awaited()
        self.assertIn('up to date',await self.run_check(b'model-2'))
        self.assertIn('Updated',await self.run_check(b'model-3'))                  # an auto-updated model updates again
        self.assertEqual(len(list(self.updates.folder.glob('*.onnx'))),1)          # old one removed

    async def test_broken_model_not_used(self):
        self.assertIn('did not run',await self.run_check(b'model-2',works=False))
        self.assertEqual(self.monitor.settings['model'],str(self.current))

    async def test_hand_placed_model_never_replaced(self):
        self.current.write_bytes(b'my own model')
        self.assertIn('set by hand',await self.run_check(b'model-2'))
        self.assertEqual(self.monitor.settings['model'],str(self.current))

    async def test_saved_update_applied_at_start(self):
        await self.run_check(b'model-2');new=self.monitor.settings['model']
        self.monitor.settings['model']=str(self.current);self.updates.apply_saved()
        self.assertEqual(self.monitor.settings['model'],new)

    async def test_daily_not_every_loop(self):
        with patch('failureDetection.setup_ai.api') as api:
            self.updates.checked=self.updates.clock();await self.updates.check()
            api.assert_not_called()


class EngineHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_approved_hook(self):
        import inspect,queueing
        self.assertIn('on_start_approved',inspect.getsource(queueing.Engine.start))


if __name__=='__main__':
    unittest.main()
