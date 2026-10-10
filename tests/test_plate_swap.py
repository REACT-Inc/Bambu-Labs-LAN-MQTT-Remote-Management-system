"""Swapmod: on/off and a swap print file. After a finished print the swap print runs, then the next queued job starts;
a failed print doesn't swap on its own. Only for A1-family printers."""
import asyncio,shutil,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).parents[1]))
from queueing import Store,options
from swapMod.plate_swap import PlateSwap


def swap_file(path):
    with zipfile.ZipFile(path,'w') as z:z.writestr('Metadata/plate_1.gcode','G28\n')
    return path


class FakeEngine:
    def __init__(self,store):self.store,self.started=store,[]
    async def start(self,job_id,confirmed,author,override_error=False):
        assert confirmed is True
        self.started.append((self.store.get(job_id)['label'],author));self.store.set_status(job_id,'staging')
        return self.store.get(job_id)


class SwapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=SimpleNamespace(DATA_DIR=d,names=lambda:['Mini','H2D'],notify=AsyncMock(),RED=1,GREEN=2,YELLOW=3,EXAMPLE_MODE=False,
                                  printer_config=lambda n:{'model':'A1 mini' if n=='Mini' else 'H2D'},CONFIG={})
        self.store=Store(d/'db');self.swap=PlateSwap(self.core,self.store,settle=0);self.engine=FakeEngine(self.store)
        self.file=swap_file(d/'swap.3mf')
    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def ready(self):
        self.swap.configure('Mini',True,'Ana');self.swap.set_file('Mini',self.file,'swap.gcode.3mf',1,'Ana')

    def job(self,label,**opts):
        job=self.store.add('Mini',label,'','cache/'+label+'.gcode.3mf',dict(options(1),**opts),'t');return job

    def test_only_on_and_a_file(self):
        self.assertEqual(self.swap.state('Mini'),dict(available=True,enabled=False,file='',plate=1,ready=False,printer_model='A1 mini'))
        self.ready()
        state=self.swap.state('Mini');self.assertEqual((state['enabled'],state['file'],state['ready']),(True,'swap.gcode.3mf',True))
        self.file.unlink();self.assertTrue(self.swap.state('Mini')['ready'])   # a private copy is kept
        with self.assertRaisesRegex(ValueError,'only available for Bambu Lab A-series'):self.swap.configure('H2D',True,'Ana')
        self.assertFalse(self.swap.state('H2D')['available'])

    async def test_finished_print_swaps_then_starts_the_next_job(self):
        self.ready();printed=self.job('Bracket');following=self.job('Hook')
        self.store.set_status(printed['id'],'staging');self.store.set_status(printed['id'],'finished')   # as the queue does
        await self.swap.after(self.engine,'Mini',printed,'FINISH')
        self.assertEqual(self.engine.started,[('Plate swap','Swapmod')])
        swap=next(j for j in self.store.jobs('Mini') if j['label']=='Plate swap')
        self.assertTrue(swap['options']['swap']);self.assertEqual(swap['asset'],str(Path(self.tmp.name)/'swapmod'/Path(swap['asset']).name))
        self.store.set_status(swap['id'],'finished')   # the queue marks the swap print finished first
        await self.swap.after(self.engine,'Mini',swap,'FINISH')
        self.assertEqual(self.engine.started[-1],('Hook','Swapmod'))
        self.assertIn('next print started',self.core.notify.await_args.args[1],self.core.notify.await_args.args[2])

    async def test_failed_print_does_not_swap_and_swap_now_works(self):
        self.ready();printed=self.job('Bracket')
        await self.swap.after(self.engine,'Mini',printed,'FAILED')
        self.assertEqual(self.engine.started,[]);self.assertIn('Not swapping',self.core.notify.await_args.args[1])
        await self.swap.swap_now(self.engine,'Mini','Ana');self.assertEqual(self.engine.started,[('Plate swap','Ana')])

    async def test_failed_swap_stops_the_queue_and_off_does_nothing(self):
        self.ready();self.job('Hook')
        swap=self.swap.swap_job('Mini','t')
        await self.swap.after(self.engine,'Mini',swap,'FAILED')
        self.assertEqual(self.engine.started,[]);self.assertIn('Plate swap failed',self.core.notify.await_args.args[1])
        self.swap.configure('Mini',False,'Ana')
        await self.swap.after(self.engine,'Mini',self.job('Other'),'FINISH');self.assertEqual(self.engine.started,[])
        with self.assertRaisesRegex(ValueError,'Turn Swapmod on'):await self.swap.swap_now(self.engine,'Mini','Ana')

    async def test_nothing_waiting_after_the_swap(self):
        self.ready();swap=self.swap.swap_job('Mini','t')
        await self.swap.after(self.engine,'Mini',swap,'FINISH')
        self.assertEqual(self.engine.started,[]);self.assertIn('Plate swapped',self.core.notify.await_args.args[1])

    async def test_bed_check_ai_holds_the_next_job_when_it_sees_parts(self):
        self.ready();self.job('Hook');swap=self.swap.swap_job('Mini','t')
        verdicts=[]
        async def gate(name):verdicts.append(name);return {'state':'parts','detail':'looks like a bed with parts on it'}
        self.engine.bed_check=gate
        await self.swap.after(self.engine,'Mini',swap,'FINISH')
        self.assertEqual((verdicts,self.engine.started),(['Mini'],[]))   # held: nothing started on a bed with parts
        self.assertIn('does not look clear',self.core.notify.await_args.args[1])
        async def unsure(name):return {'state':'unknown','detail':'still learning'}
        self.engine.bed_check=unsure   # unsure (or not set up): the queue carries on as before
        swap=self.swap.swap_job('Mini','t');await self.swap.after(self.engine,'Mini',swap,'FINISH')
        self.assertEqual(self.engine.started,[('Hook','Swapmod')])

    def test_old_settings_table_is_kept_working(self):
        db=self.store.db
        db.execute("INSERT OR REPLACE INTO plate_swap(printer,enabled) VALUES('Mini',1)");db.commit()
        PlateSwap(self.core,self.store)   # upgrading an existing database adds the new columns
        self.assertEqual(self.swap.state('Mini')['file'],'')


if __name__=='__main__':
    unittest.main()
