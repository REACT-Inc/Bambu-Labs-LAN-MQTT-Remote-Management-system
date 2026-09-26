import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from queueing import Store,Engine,options
from swapMod.plate_swap import PlateSwap


class SwapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db')
        self.core=SimpleNamespace(names=lambda:['A1 Mini','A1','BOB H2D'],printer_config=lambda n:{'name':n},EXAMPLE_MODE=True,state_data=lambda n:('IDLE',0,{},True))
        self.engine=Engine(self.core,self.store);self.swap=self.engine.plate_swap
    async def asyncTearDown(self):
        for task in list(self.engine.tasks):task.cancel()
        import asyncio
        await asyncio.gather(*self.engine.tasks,return_exceptions=True)
        self.store.db.close();self.tmp.cleanup()
    def job(self,name='A1 Mini'):
        return self.store.add(name,'part',None,'part.3mf',options(),'test',True)
    def enable(self):self.swap.configure('A1 Mini',True,'A1 mini',2,True,'admin')
    def test_default_off_and_isolated(self):
        self.assertFalse(self.swap.state('A1 Mini')['enabled']);self.enable()
        self.assertFalse(self.swap.state('A1')['enabled'])
        self.assertTrue(self.swap.state('A1 Mini')['enabled'])
    def test_wrong_models_and_count(self):
        for name,model,count in [('BOB H2D','A1 Mini',2),('A1','A1 Mini',2),('A1 Mini','A1 Mini',-1),('A1 Mini','A1 Mini',True)]:
            with self.assertRaises(ValueError):self.swap.configure(name,True,model,count,True,'admin')
    def test_checks_required(self):
        self.enable();job=self.job()
        with self.assertRaises(ValueError):self.swap.check(job)
        self.swap.approve_job(job['id'],True,'admin');job=self.store.get(job['id'])
        with self.assertRaises(ValueError):self.swap.check(job)
        self.swap.verify('A1 Mini',True,'admin');self.swap.check(job)
    async def test_start_reserves_once_and_other_printer_unchanged(self):
        self.enable();job=self.job();self.swap.approve_job(job['id'],True,'admin');self.swap.verify('A1 Mini',True,'admin')
        self.engine.dispatch=AsyncMock()
        await self.engine.start(job['id'],True,'admin')
        self.assertEqual(self.swap.state('A1 Mini')['spares'],1);self.assertFalse(self.swap.state('A1 Mini')['verified'])
        with self.assertRaises(ValueError):await self.engine.start(job['id'],True,'admin')
        self.assertEqual(self.swap.state('A1 Mini')['spares'],1)
        with self.assertRaises(ValueError):self.swap.configure('A1 Mini',False,'',0,True,'admin')
        other=self.job('A1');await self.engine.start(other['id'],True,'admin')
    def test_disabled_swap_job_blocked(self):
        self.enable();job=self.job();self.swap.approve_job(job['id'],True,'admin')
        self.swap.configure('A1 Mini',False,'',0,True,'admin')
        with self.assertRaises(ValueError):self.swap.check(self.store.get(job['id']))
    def test_restart_and_reconfigure_invalidate_approvals(self):
        self.enable();job=self.job();self.swap.approve_job(job['id'],True,'admin');self.swap.verify('A1 Mini',True,'admin')
        reopened=PlateSwap(self.core,self.store)
        self.assertFalse(reopened.state('A1 Mini')['verified']);reopened.verify('A1 Mini',True,'admin')
        with self.assertRaises(ValueError):reopened.check(self.store.get(job['id']))
    def test_empty_feeder(self):
        self.swap.configure('A1 Mini',True,'A1 mini',0,True,'admin');job=self.job();self.swap.approve_job(job['id'],True,'admin');self.swap.verify('A1 Mini',True,'admin')
        with self.assertRaisesRegex(ValueError,'Not enough'):self.swap.check(self.store.get(job['id']))
    def test_transaction_rollback(self):
        self.enable();job=self.job();self.swap.approve_job(job['id'],True,'admin')
        def bad(j):self.swap.reserve(j);raise ValueError('failure')
        with self.assertRaises(ValueError):self.store.claim(job['id'],bad)
        self.assertEqual(self.store.get(job['id'])['status'],'queued');self.assertEqual(self.swap.state('A1 Mini')['spares'],2)

    async def test_reserves_entire_batch(self):
        self.swap.configure('A1 Mini',True,'A1 mini',5,True,'admin')
        job=self.job();self.swap.approve_job(job['id'],True,'admin',3);self.swap.verify('A1 Mini',True,'admin')
        self.engine.dispatch=AsyncMock();await self.engine.start(job['id'],True,'admin')
        self.assertEqual(self.swap.state('A1 Mini')['spares'],2)
    def test_legacy_configuration_disabled(self):
        self.store.db.execute("UPDATE plate_swap SET provider='' ")
        self.store.db.execute("INSERT OR REPLACE INTO plate_swap(printer,enabled,model,spares,verified,revision,provider) VALUES('A1 Mini',1,'A1 mini',5,1,1,'')")
        self.store.db.commit()
        new=PlateSwap(self.core,self.store)
        self.assertFalse(new.state('A1 Mini')['enabled']);self.assertEqual(new.state('A1 Mini')['spares'],0)
    def test_wrong_provider_rejected(self):
        self.enable();job=self.job();self.swap.approve_job(job['id'],True,'admin');job=self.store.get(job['id'])
        job['options']['swap_provider']='infinity_flow'
        with self.assertRaises(ValueError):self.swap.check(job,True)

    def test_old_job_cannot_be_relabelled(self):
        import json
        self.enable();job=self.job();opts=dict(job['options'],swap_model='A1 mini',swap_revision=1)
        self.store.db.execute('UPDATE jobs SET options=? WHERE id=?',(json.dumps(opts),job['id']));self.store.db.commit()
        with self.assertRaisesRegex(ValueError,'another swap system'):self.swap.approve_job(job['id'],True,'admin',2)
