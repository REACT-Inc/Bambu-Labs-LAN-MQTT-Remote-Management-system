import asyncio
import importlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from types import SimpleNamespace

from queueing import Store, Engine, options, validate_archive
from dashboard import Dashboard, password_hash, atomic_json


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.store=Store(self.path)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def add(self,printer='A'):
        return self.store.add(printer,'Part',None,'cache/part.gcode.3mf',options(),'test')
    def test_only_one_active_per_printer(self):
        a,b=self.add(),self.add();self.store.claim(a['id'])
        with self.assertRaises(ValueError):self.store.claim(b['id'])
    def test_fifo(self):
        a,b=self.add(),self.add()
        with self.assertRaises(ValueError):self.store.claim(b['id'])
        self.store.edit(b['id'],'up','test');self.store.claim(b['id'])
    def test_restart_requires_review(self):
        a=self.add();self.store.claim(a['id']);self.store.db.close();self.store=Store(self.path)
        self.assertEqual(self.store.get(a['id'])['status'],'needs_review')
    def test_waiting_jobs_survive_restart(self):
        a=self.add();self.store.db.close();self.store=Store(self.path)
        self.assertEqual(self.store.get(a['id'])['status'],'queued')
    def test_other_printers_independent(self):
        a,b=self.add(),self.add('B');self.store.claim(a['id']);self.store.claim(b['id'])
    def test_no_active_removal(self):
        a=self.add();self.store.claim(a['id'])
        with self.assertRaises(ValueError):self.store.edit(a['id'],'remove','test')
    def test_path_injection_rejected(self):
        for path in ('../evil.3mf','/root.3mf','part.3mf\r\nDELE file','https://site/file.3mf'):
            with self.assertRaises(ValueError):self.store.add('A','Part',None,path,options(),'test')
    def test_options(self):
        with self.assertRaises(ValueError):options(use_ams=True)
        with self.assertRaises(ValueError):options(plate=0)
        with self.assertRaises(ValueError):options(use_ams='false')
        self.assertEqual(options(use_ams=True,mapping='0,1')['ams_mapping'],[0,1])
    def test_sliced_file_required(self):
        path=Path(self.tmp.name)/'file.3mf'
        with zipfile.ZipFile(path,'w') as z:z.writestr('3D/model.model','model')
        with self.assertRaises(ValueError):validate_archive(path,1)
        with zipfile.ZipFile(path,'a') as z:z.writestr('Metadata/plate_1.gcode','G1 X0')
        validate_archive(path,1)
        with self.assertRaises(ValueError):validate_archive(path,2)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db');self.sent=[]
        async def notify(*args):self.sent.append(args)
        self.core=SimpleNamespace(EXAMPLE_MODE=True,EXAMPLE_DATA={'A':{'state':'IDLE','connected':True}},BLUE=1,GREEN=2,YELLOW=3,RED=4,notify=notify,
            publish_action=lambda *args:None,last_seen={'A':time.time()},state_data=lambda name:('IDLE',0,{},True))
        self.engine=Engine(self.core,self.store)
        self.job=self.store.add('A','Part',None,'part.3mf',options(),'test',demo=True)
    async def asyncTearDown(self):
        for t in list(self.engine.tasks):t.cancel()
        await asyncio.gather(*self.engine.tasks,return_exceptions=True)
        self.store.db.close();self.tmp.cleanup()
    async def test_error_override(self):
        self.core.state_data=lambda n:('FAILED',123,{},True)
        with self.assertRaises(ValueError):await self.engine.start(self.job['id'],True,'test')
        await self.engine.start(self.job['id'],True,'test',True)
        await asyncio.sleep(0)
        self.assertNotEqual(self.store.get(self.job['id'])['status'],'failed')
        self.assertTrue(any(e['title']=='Error override approved' for e in self.store.events()))

    async def test_override_still_blocks_active_unknown_offline(self):
        for state in ('RUNNING','PAUSE','PREPARE','UNKNOWN'):
            self.core.state_data=lambda n,s=state:(s,123,{},True)
            with self.assertRaises(ValueError):await self.engine.start(self.job['id'],True,'test',True)
        self.core.state_data=lambda n:('IDLE',123,{},False)
        with self.assertRaises(ValueError):await self.engine.start(self.job['id'],True,'test',True)
        with self.assertRaises(ValueError):await self.engine.start(self.job['id'],True,'test','true')

    async def test_override_rechecks_before_publish(self):
        self.store.claim(self.job['id'])
        self.core.state_data=lambda n:('RUNNING',0,{},True)
        await self.engine.dispatch(self.store.get(self.job['id']),True)
        self.assertEqual(self.store.get(self.job['id'])['status'],'failed')

    async def test_light_does_not_change_queue(self):
        sent=[];self.core.publish_light=lambda *a:sent.append(a)
        await self.engine.control('A','lighton')
        await self.engine.control('A','lightoff')
        self.assertEqual(sent,[('A',True),('A',False)])
        self.assertEqual(self.store.get(self.job['id'])['status'],'queued')

    async def test_demo_job_cannot_start_live(self):
        self.core.EXAMPLE_MODE=False
        with self.assertRaises(ValueError):await self.engine.start(self.job['id'],True,'test')
    async def test_requires_confirmation(self):
        with self.assertRaises(ValueError):await self.engine.start(self.job['id'],False,'test')
    async def test_concurrent_start_one_wins(self):
        results=await asyncio.gather(self.engine.start(self.job['id'],True,'a'),self.engine.start(self.job['id'],True,'b'),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results),1)
    async def test_no_unrelated_completion(self):
        self.store.claim(self.job['id']);self.store.set_status(self.job['id'],'awaiting_start')
        await self.engine.telemetry('A',{'gcode_state':'FINISH','subtask_name':'other.3mf'})
        self.assertEqual(self.store.get(self.job['id'])['status'],'awaiting_start')
    async def test_conflicting_filename_does_not_match_stale_name(self):
        self.store.claim(self.job['id']);self.store.set_status(self.job['id'],'awaiting_start')
        await self.engine.telemetry('A',{'gcode_state':'RUNNING','subtask_name':'part.3mf','gcode_file':'other.3mf'})
        self.assertEqual(self.store.get(self.job['id'])['status'],'awaiting_start')
    async def test_only_matching_running_then_finish(self):
        self.store.claim(self.job['id']);self.store.set_status(self.job['id'],'awaiting_start')
        await self.engine.telemetry('A',{'gcode_state':'FINISH','subtask_name':'part.3mf'})
        self.assertEqual(self.store.get(self.job['id'])['status'],'awaiting_start')
        await self.engine.telemetry('A',{'gcode_state':'RUNNING','subtask_name':'part.3mf'})
        await self.engine.telemetry('A',{'gcode_state':'PAUSE','subtask_name':'part.3mf'})
        self.assertEqual(self.store.get(self.job['id'])['status'],'paused')
        await self.engine.telemetry('A',{'gcode_state':'FINISH','subtask_name':'part.3mf'})
        self.assertEqual(self.store.get(self.job['id'])['status'],'finished')
    async def test_stop_while_staging_cancels(self):
        self.store.claim(self.job['id']);await self.engine.control('A','stop')
        self.assertEqual(self.store.get(self.job['id'])['status'],'cancelled')
        await self.engine.dispatch(self.store.get(self.job['id']))
        self.assertEqual(self.store.get(self.job['id'])['status'],'cancelled')
    async def test_stale_live_telemetry_blocks(self):
        self.core.EXAMPLE_MODE=False;self.core.last_seen={'A':time.time()-1000}
        with self.assertRaises(ValueError):await self.engine.start(self.job['id'],True,'test')
    async def test_restart_does_not_autoresolve(self):
        self.store.set_status(self.job['id'],'needs_review')
        await self.engine.telemetry('A',{'gcode_state':'FINISH','subtask_name':'part.3mf'})
        self.assertEqual(self.store.get(self.job['id'])['status'],'needs_review')


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=SimpleNamespace(DATA_DIR=d,SETTINGS_FILE=str(d/'settings.json'),settings={'notification_channel_id':123456789012345678},SETTINGS_USER_IDS={123456789012345679},EXAMPLE_MODE=True,
           names=lambda:['A'],state_data=lambda n:('IDLE',0,{},True),last_seen={},bot=SimpleNamespace(is_ready=lambda:False),log=__import__('logging').getLogger('test'))
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();self.store.db.close();self.tmp.cleanup()
    async def login(self):
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.assertEqual(r.status,200)
        return (await r.json())['csrf']
    async def test_file_listing_requires_login(self):
        r=await self.client.get('/api/files/A')
        self.assertEqual(r.status,401)
        await self.login()
        r=await self.client.get('/api/files/A')
        self.assertEqual(r.status,200)
        self.assertIn('demo.gcode.3mf',(await r.json())['text'])

    async def test_error_description_in_state(self):
        self.core.state_data=lambda n:('IDLE',int('0502C014',16),{},True)
        await self.login()
        r=await self.client.get('/api/state')
        data=await r.json()
        self.assertIn('error_text',data['printers'][0])
        self.assertIn('https://e.bambulab.com/',data['printers'][0]['error_text'])

    async def test_http_override(self):
        self.core.state_data=lambda n:('FAILED',123,{},True)
        job=self.store.add('A','Part',None,'part.3mf',options(),'test',True)
        csrf=await self.login();headers={'X-PM':'1','X-CSRF':csrf}
        with patch.object(self.engine,'spawn',side_effect=lambda coro:coro.close()):
            r=await self.client.post('/api/jobs/'+job['id']+'/start',json={'confirmed':True,'override_error':True},headers=headers)
        self.assertEqual(r.status,200)
        self.assertEqual(self.store.get(job['id'])['status'],'staging')

    async def test_auth_required(self):self.assertEqual((await self.client.get('/api/state')).status,401)
    async def test_cross_origin_login_blocked(self):
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1','Origin':'https://attacker.example'})
        self.assertEqual(r.status,403)
    async def test_csrf_required(self):
        await self.login();r=await self.client.post('/api/settings',json={},headers={'X-PM':'1'})
        self.assertEqual(r.status,403)
    async def test_id_precision_preserved(self):
        await self.login();r=await self.client.get('/api/state');d=await r.json()
        self.assertEqual(d['settings']['notification_channel_id'],'123456789012345678')
        self.assertEqual(d['settings']['admin_user_ids'],['123456789012345679'])
    async def test_add_persists(self):
        csrf=await self.login();r=await self.client.post('/api/jobs',json={'printer':'A','label':'Part','remote':'part.3mf'},headers={'X-PM':'1','X-CSRF':csrf})
        # Notification method added for this test core below.
        self.assertEqual(r.status,200)
        self.assertEqual(len(self.store.jobs('A')),1)
    async def test_wrong_password(self):
        r=await self.client.post('/api/login',json={'password':'wrong'},headers={'X-PM':'1'})
        self.assertEqual(r.status,401)
    async def test_live_camera_requires_login(self):
        r=await self.client.get('/api/live/A')
        self.assertEqual(r.status,401)
        

    async def test_unknown_static_file(self):self.assertEqual((await self.client.get('/assets/nope')).status,404)

# Provide an async no-op notification for HTTP tests without Discord.
_original_setup=HTTPTests.asyncSetUp
async def _setup(self):
    await _original_setup(self)
    async def notify(*args):pass
    self.core.notify=notify;self.core.BLUE=1
HTTPTests.asyncSetUp=_setup

if __name__=='__main__':unittest.main()
