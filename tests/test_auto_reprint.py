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
        self.store.claim(job_id)   # as the real queue: only a printer's first waiting job can start
        self.started.append((job_id,author))


class ReprintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        configs={'Mini 1':{'model':'A1 mini','camera_type':'jpeg_tcp'},'Mini 2':{'model':'A1 mini','camera_type':'jpeg_tcp'},
                 'Mini 3':{'model':'A1 mini','camera_type':'jpeg_tcp'},'H2D':{'model':'H2D','camera_type':'rtsp'}}
        self.states={'Mini 1':'PAUSE','Mini 2':'IDLE','Mini 3':'IDLE','H2D':'IDLE'}
        self.core=SimpleNamespace(DATA_DIR=d,settings={},names=lambda:list(configs),printer_config=configs.get,
            states=self.states,state_data=lambda n:(self.states[n],0,{},True),display_name=lambda n:n,
            capture_still=AsyncMock(return_value=b'\xff\xd8bed\xff\xd9'),notify=AsyncMock(),BLUE=1,YELLOW=2,GREEN=3,RED=4,EXAMPLE_MODE=False)
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

    def swapmod(self,name,with_file=True):
        """Swapmod on for a printer: the real swapMod.plate_swap.PlateSwap, on the same queue."""
        from swapMod.plate_swap import PlateSwap
        from tests.test_plate_swap import swap_file
        if not hasattr(self.engine,'plate_swap'):self.engine.plate_swap=PlateSwap(self.core,self.store,settle=0)
        self.engine.plate_swap.configure(name,True,'Ana')
        if with_file:self.engine.plate_swap.set_file(name,swap_file(Path(self.tmp.name)/'swap.3mf'),'swap.gcode.3mf',1,'Ana')
        return self.engine.plate_swap

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
        self.clock.now+=2.5*HOUR
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
        self.reprints.flag('Mini 1',self.job);self.clock.now+=2.5*HOUR
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
        self.assertEqual(self.reprints.due(self.job['id']),self.clock.now+2.5*HOUR)

    async def test_priority_sets_how_long_a_paused_print_waits(self):
        waits={}
        for priority in (1,2,3,4,5):
            job=self.store.add('Mini 1',f'P{priority}',self.asset,'',options(1,priority=priority),'t')
            self.reprints.flag('Mini 1',job);waits[priority]=self.reprints.due(job['id'])-self.clock.now
            self.reprints.pending.pop(job['id'])
        self.assertEqual(waits,{1:5*60,2:HOUR,3:1.5*HOUR,4:2*HOUR,5:2.5*HOUR})
        old=dict(self.job,options={k:v for k,v in self.job['options'].items() if k!='priority'})   # queued before priorities
        self.reprints.flag('Mini 1',old);self.assertEqual(self.reprints.due(old['id'])-self.clock.now,2.5*HOUR)
        capped=AutoReprint(self.core,self.engine,{'after_hours':0.5},clock=self.clock)   # after_hours is the most it waits
        capped.flag('Mini 1',old);self.assertEqual(capped.due(old['id'])-self.clock.now,0.5*HOUR)
        with self.assertRaisesRegex(ValueError,'Priority must be 1'):options(1,priority=6)
        with self.assertRaisesRegex(ValueError,'Priority must be 1'):options(1,priority=True)
        self.assertEqual(options(1,priority='')['priority'],5)

    async def test_urgent_print_is_reprinted_after_five_minutes(self):
        job=self.store.add('Mini 1','Urgent',self.asset,'',options(1,priority=1),'t');self.store.set_status(self.job['id'],'failed')
        self.store.set_status(job['id'],'paused')
        self.reprints.flag('Mini 1',job)
        self.clock.now+=4*60;await self.reprints.tick();self.assertEqual(self.engine.started,[])
        self.clock.now+=60;await self.reprints.tick()
        self.assertEqual(self.store.get(self.engine.started[0][0])['label'],'Urgent')

    async def test_mark_available_overrides_what_a_person_can_vouch_for(self):
        self.beds={'Mini 2':'unknown','Mini 3':'parts'}
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertEqual([rows[n]['can_mark'] for n in ('Mini 2','Mini 3','H2D')],[True,True,False])   # never a different model
        self.reprints.mark('Mini 3',True,'Ana')
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertTrue(rows['Mini 3']['ok']);self.assertTrue(rows['Mini 3']['marked']);self.assertEqual(rows['Mini 3']['bed'],'marked')
        self.assertIn('Mini 3 (marked available)',self.reprints.state('Mini 1')['available'])
        self.assertEqual([r['name'] for r in self.reprints.state('Mini 1')['printers'] if r['ok']],['Mini 3'])
        # Things that would make the print fail are never overridden.
        self.states['Mini 3']='RUNNING'
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertFalse(rows['Mini 3']['ok']);self.assertIn('running',rows['Mini 3']['reason']);self.states['Mini 3']='IDLE'
        # Reprint on it, saying who vouched for it.
        copy=await self.reprints.reprint('Mini 1','web administrator')
        self.assertEqual(copy['printer'],'Mini 3');self.assertIn('Ana marked this printer available',self.core.notify.await_args.args[2])
        # Kept across a restart; ends when that printer starts a print, or after 12 h.
        self.make();self.assertTrue(self.reprints.marked('Mini 3'))
        self.reprints.started('Mini 3');self.assertIsNone(self.reprints.marked('Mini 3'))
        self.reprints.mark('Mini 2',True,'Ana');self.clock.now+=12*HOUR+1;self.assertIsNone(self.reprints.marked('Mini 2'))
        self.assertIn('Marked available for reprints',[e['title'] for e in self.store.events()])
        with self.assertRaisesRegex(ValueError,'Unknown printer'):self.reprints.mark('Nope',True,'Ana')

    async def test_the_notification_names_who_marked_the_printer(self):
        # The real queue ends a mark as soon as a print starts on that printer (its on_start_approved hook).
        self.beds={'Mini 2':'unknown','Mini 3':'unknown'}
        original=self.engine.start
        async def start(job_id,confirmed,author,override_error=False):
            await original(job_id,confirmed,author,override_error);self.reprints.started(self.store.get(job_id)['printer'])
        self.engine.start=start
        self.reprints.mark('Mini 2',True,'Ana')
        await self.reprints.reprint('Mini 1','web administrator')
        self.assertIn('Ana marked this printer available',self.core.notify.await_args.args[2])
        self.assertIsNone(self.reprints.marked('Mini 2'))   # and the mark ended with that print

    async def test_an_unknown_model_can_be_marked_and_is_reprinted_there(self):
        configs={'Mini 1':{'model':'A1 mini','camera_type':'jpeg_tcp'},'Spare':{'camera_type':'jpeg_tcp'}}
        self.core.names=lambda:list(configs);self.core.printer_config=configs.get;self.states['Spare']='IDLE'
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertEqual((rows['Spare']['reason'],rows['Spare']['can_mark']),('model unknown',True))
        self.reprints.mark('Spare',True,'Ana')
        copy=await self.reprints.reprint('Mini 1','web administrator')
        self.assertEqual(copy['printer'],'Spare')   # not refused for the unknown model once someone vouched for it

    async def test_nothing_is_stopped_when_the_copy_cannot_be_made(self):
        # A job whose file is still on the failed printer: it's copied off at reprint time, and only then can it be read.
        self.store.set_status(self.job['id'],'failed')
        remote=self.store.add('Mini 1','Remote',None,'cache/remote.gcode.3mf',options(1),'t');self.store.set_status(remote['id'],'paused')
        wrong=make_3mf(Path(self.tmp.name)/'uploads'/'wrong.3mf',model='Bambu Lab H2D')
        with patch('job_transfer.fetch',return_value=str(wrong)):
            with self.assertRaisesRegex(ValueError,'not the A1 mini'):await self.reprints.reprint('Mini 1','web administrator')
        self.assertEqual(self.engine.controls,[])   # the paused print was not stopped
        self.assertEqual(self.store.get(remote['id'])['status'],'paused');self.assertEqual(self.engine.started,[])
        self.assertFalse(wrong.exists())            # the copied file is cleaned up

    async def test_a_mark_ends_when_the_printer_starts_printing_anywhere(self):
        self.beds={'Mini 2':'unknown','Mini 3':'unknown'};looked=[]
        bed_state=self.reprints.bed_state
        async def counting(name,refresh=False):looked.append(name);return await bed_state(name,refresh)
        self.reprints.bed_state=counting
        self.reprints.flag('Mini 1',self.job);await self.reprints.availability(self.store.get(self.job['id']))
        looked.clear();self.reprints.mark('Mini 2',True,'Ana')
        row=lambda:next(r for r in self.reprints.state('Mini 1')['printers'] if r['name']=='Mini 2')
        self.assertTrue(row()['ok']);self.assertTrue(row()['marked']);self.assertEqual(looked,[])   # shown at once, no camera
        self.states['Mini 2']='RUNNING'   # a print started from Bambu Studio or the printer's screen
        await self.reprints.tick()
        self.assertIsNone(self.reprints.marked('Mini 2'))
        self.assertFalse(row()['ok']);self.assertIn('started a print',row()['reason'])
        self.states['Mini 2']='FINISH'    # its part is on the bed now
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertFalse(rows['Mini 2']['ok'])

    async def test_a_job_whose_file_is_gone_says_so(self):
        Path(self.job['asset']).unlink()
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertEqual(rows['Mini 2']['reason'],"the job's file is no longer on the Pi")

    async def test_the_reprint_goes_ahead_of_that_printers_waiting_jobs(self):
        waiting=self.store.add('Mini 2','Waiting',None,'cache/waiting.gcode.3mf',options(),'t')
        copy=await self.reprints.reprint('Mini 1','web administrator')
        self.assertEqual(self.engine.started,[(copy['id'],'web administrator')])
        self.assertEqual([j['label'] for j in self.store.jobs('Mini 2')],['Bracket','Waiting'])
        self.assertEqual(self.store.get(waiting['id'])['status'],'queued')

    async def test_a_swapmod_printer_swaps_its_plate_first_when_its_bed_is_not_clear(self):
        self.beds={'Mini 2':'parts','Mini 3':'unknown'}
        swap=self.swapmod('Mini 3')
        waiting=self.store.add('Mini 3','Waiting',None,'cache/waiting.gcode.3mf',options(),'t')   # already in its queue
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertEqual(rows['Mini 2']['reason'],'parts on the bed')                                # no Swapmod there
        self.assertEqual((rows['Mini 3']['ok'],rows['Mini 3']['swap'],rows['Mini 3']['can_mark']),(True,True,False))
        self.assertIn('Mini 3 (Swapmod swaps its plate first)',self.reprints.state('Mini 1')['available'])
        self.assertTrue(next(r for r in self.reprints.state('Mini 1')['printers'] if r['name']=='Mini 3')['swap'])
        self.reprints.flag('Mini 1',self.job);self.clock.now+=2.5*HOUR
        await self.reprints.tick()
        self.assertEqual(self.engine.controls,[('Mini 1','stop',AUTHOR)])                          # the paused print is stopped
        self.assertIn('reprinted on Mini 3 after a plate swap',self.store.get(self.job['id'])['note'])
        queue=self.store.jobs('Mini 3')
        self.assertEqual([j['label'] for j in queue],['Plate swap','Bracket','Waiting'])            # the swap, then the reprint
        self.assertTrue(queue[0]['options']['swap'])
        self.assertEqual(self.engine.started,[(queue[0]['id'],AUTHOR)])                             # only the swap print starts
        self.assertEqual(queue[1]['status'],'queued');self.assertFalse(self.reprints.state('Mini 1')['pending'])
        self.assertEqual(self.core.notify.await_args.args[:2],('Mini 3','🤖 AI reprint: swapping the plate first'))
        self.assertIn("isn't confirmed empty",self.core.notify.await_args.args[2])
        # When the swap print finishes, Swapmod starts the next waiting job on that printer: the reprint.
        self.store.set_status(queue[0]['id'],'finished')
        await swap.after(self.engine,'Mini 3',queue[0],'FINISH')
        self.assertEqual(self.engine.started[-1],(queue[1]['id'],'Swapmod'))
        self.assertEqual(self.store.get(waiting['id'])['status'],'queued')

    async def test_a_printer_that_can_start_at_once_comes_before_one_that_swaps(self):
        self.beds={'Mini 2':'parts','Mini 3':'clear'}
        self.swapmod('Mini 2');self.swapmod('Mini 3')
        rows=await self.reprints.availability(self.store.get(self.job['id']))
        self.assertEqual([(r['name'],r['ok'],r['swap']) for r in rows],[('Mini 3',True,False),('Mini 2',True,True),('H2D',False,False)])
        copy=await self.reprints.reprint('Mini 1','web administrator')
        self.assertEqual((copy['printer'],copy['swap']),('Mini 3',False))
        self.assertEqual([j['label'] for j in self.store.jobs('Mini 3')],['Bracket'])              # a clear bed: no swap
        self.assertEqual(self.core.notify.await_args.args[1],'🤖 AI reprint started')
        self.beds={'Mini 2':'parts','Mini 3':'parts'};self.reprints.mark('Mini 2',True,'Ana')       # someone looked: no swap
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertEqual((rows['Mini 2']['ok'],rows['Mini 2']['swap'],rows['Mini 2']['bed']),(True,False,'marked'))

    async def test_swapmod_off_or_without_its_swap_file_is_not_used(self):
        self.beds={'Mini 2':'parts','Mini 3':'parts'}
        self.swapmod('Mini 2',with_file=False);self.swapmod('Mini 3').configure('Mini 3',False,'Ana')
        rows={r['name']:r for r in await self.reprints.availability(self.store.get(self.job['id']))}
        self.assertEqual([(rows[n]['ok'],rows[n]['reason']) for n in ('Mini 2','Mini 3')],[(False,'parts on the bed')]*2)
        with self.assertRaisesRegex(ValueError,'No printer available'):await self.reprints.reprint('Mini 1','web administrator')
        self.assertEqual((self.engine.controls,self.engine.started),([],[]))

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


class SwapOnTheRealQueueTests(unittest.IsolatedAsyncioTestCase):
    """The whole chain on the real queue (queueing.Engine) and Swapmod, in demo mode, with the printer's reports played
    by hand: the reprint goes to a Swapmod printer with parts on its bed, which swaps its plate, then prints the job."""
    async def asyncSetUp(self):
        import queueing
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        configs={'Mini 1':{'model':'A1 mini','camera_type':'jpeg_tcp'},'Mini 2':{'model':'A1 mini','camera_type':'jpeg_tcp'}}
        self.data={'Mini 1':{'state':'PAUSE'},'Mini 2':{'state':'FINISH'}}   # Mini 2's last print is still on its plate
        self.core=SimpleNamespace(DATA_DIR=d,settings={},names=lambda:list(configs),printer_config=configs.get,display_name=lambda n:n,
            state_data=lambda n:(self.data[n]['state'],0,{},True),last_seen={},EXAMPLE_MODE=True,EXAMPLE_DATA=self.data,
            publish_action=lambda name,action:None,notify=AsyncMock(),BLUE=1,GREEN=2,RED=3,YELLOW=4)
        self.store=Store(d/'db');self.engine=queueing.Engine(self.core,self.store)
        self.engine.demo=AsyncMock()   # no simulated print: the printer's reports are played below
        swap=self.engine.plate_swap;swap.settle=0
        from tests.test_plate_swap import swap_file
        swap.configure('Mini 2',True,'Ana');swap.set_file('Mini 2',swap_file(d/'swap.3mf'),'swap.gcode.3mf',1,'Ana')
        (d/'uploads').mkdir()
        self.job=self.store.add('Mini 1','Bracket',str(make_3mf(d/'uploads'/'part.3mf')),'',options(1),'t',True)
        self.store.set_status(self.job['id'],'paused')
        self.reprints=AutoReprint(self.core,self.engine,{},clock=Clock())
        async def bed_state(name,refresh=False):return dict(state='parts',detail='test',checked=0)
        self.reprints.bed_state=bed_state
    async def asyncTearDown(self):self.store.db.close();self.tmp.cleanup()

    async def settle(self):
        while self.engine.tasks:await asyncio.gather(*list(self.engine.tasks))

    async def report(self,job,state):
        self.data['Mini 2']['state']=state
        await self.engine.telemetry('Mini 2',{'gcode_state':state,'subtask_name':job['remote']})
        await self.settle()

    async def test_swap_then_reprint(self):
        copy=await self.reprints.reprint('Mini 1',AUTHOR);await self.settle()
        self.assertTrue(copy['swap']);self.assertEqual(self.store.get(self.job['id'])['status'],'failed')
        swap,queued=self.store.jobs('Mini 2')
        self.assertEqual((swap['label'],swap['status'],queued['id'],queued['status']),('Plate swap','awaiting_start',copy['id'],'queued'))
        await self.report(swap,'RUNNING');await self.report(swap,'FINISH')
        self.assertEqual(self.store.get(swap['id'])['status'],'finished')
        self.assertEqual(self.store.get(copy['id'])['status'],'awaiting_start')   # the reprint was started after the swap
        titles=[c.args[1] for c in self.core.notify.await_args_list]
        self.assertEqual(titles,['🤖 AI reprint: swapping the plate first','♻️ Plate swapped, next print started'])
        self.assertIn(f"{copy['id']} • Swapmod",[e['detail'] for e in self.store.events() if e['title']=='Start approved'])


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
            judge=SimpleNamespace(frames=[(0,0.9),(300,0.9)],failing=lambda:2,failing_times=lambda:[0,300])
            await monitor.act('Mini 1','x.3mf',judge,'spaghetti')
            self.assertTrue(monitor.reprints.state('Mini 1')['pending'])
            text=core.notify.await_args.args[2]
            self.assertIn('No other printer to reprint on',text);self.assertIn('within 2.5 h (priority 5)',text)
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
