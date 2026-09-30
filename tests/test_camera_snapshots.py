import asyncio,time,unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from camera_snapshots import SnapshotRotation


class SnapshotRotationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls=[];self.active=0;self.max_active=0
        def snapshot_bytes(config):
            self.active+=1;self.max_active=max(self.max_active,self.active)
            try:
                self.calls.append(config['name'])
                if config['name']=='A1':time.sleep(0.2);return None  # camera that never answers
                time.sleep(0.05);return b'\xff\xd8jpeg\xff\xd9'
            finally:self.active-=1
        configs={'H2D':{'name':'H2D','camera_type':'rtsp'},'A1':{'name':'A1','camera_type':'jpeg_tcp'},'Mini':{'name':'Mini','camera_type':'jpeg_tcp'},'Off':{'name':'Off'}}
        self.core=SimpleNamespace(names=lambda:list(configs),printer_config=configs.get,EXAMPLE_MODE=False,snapshot_bytes=snapshot_bytes,
            state_data=lambda n:('IDLE',0,{},True),log=MagicMock())
        self.rotation=SnapshotRotation(self.core,SimpleNamespace(feeds={}),interval=0.3,spacing=0.05,timeout=2,backoff=60)

    async def run_for(self,seconds):
        task=asyncio.create_task(self.rotation.run());await asyncio.sleep(seconds);task.cancel()
        await asyncio.gather(task,return_exceptions=True)

    async def test_nothing_happens_while_nobody_is_viewing(self):
        with patch('camera_snapshots.time.monotonic', return_value=1.0):
            self.assertFalse(self.rotation.viewing())  # even just after the Pi boots
        await self.run_for(0.3)
        self.assertEqual(self.calls,[])

    async def test_one_camera_at_a_time_and_failing_cameras_back_off(self):
        self.rotation.touch()
        await self.run_for(1.2)
        self.assertEqual(self.max_active,1)                       # never two camera connections at once
        self.assertNotIn('Off',self.calls)                        # printers without a camera are skipped
        self.assertEqual(self.calls.count('A1'),1)                # failed once, then backed off
        self.assertGreater(self.calls.count('H2D'),1)
        self.assertTrue(self.rotation.state('H2D')['time']);self.assertEqual(self.rotation.state('H2D')['error'],'')
        self.assertIsNone(self.rotation.state('A1')['time']);self.assertIn('unavailable',self.rotation.state('A1')['error'])

    async def test_reuses_an_open_live_view_instead_of_a_second_connection(self):
        self.rotation.cameras.feeds['A1']=SimpleNamespace(frame=b'\xff\xd8live\xff\xd9',updated=time.time())
        await self.rotation.take('A1')
        self.assertEqual(self.calls,[]);self.assertEqual(self.rotation.images['A1'][0],b'\xff\xd8live\xff\xd9')

    async def test_offline_printers_are_skipped(self):
        self.core.state_data=lambda n:('IDLE',0,{},False);self.rotation.touch()
        await self.run_for(0.4)
        self.assertEqual(self.calls,[])

    async def test_auto_camera_profile_appears_as_available(self):
        self.core.printer_config=lambda _: {'name':'X1C','model':'X1C','camera_type':'auto'}
        self.assertTrue(self.rotation.has_camera('X1C'))

    async def test_response_never_waits_for_a_camera(self):
        request=SimpleNamespace(match_info={'name':'H2D'})
        self.assertEqual((await self.rotation.response(request)).status,204)
        self.rotation.images['H2D']=(b'\xff\xd8x\xff\xd9',123.0)
        response=await self.rotation.response(request)
        self.assertEqual((response.status,response.body,response.headers['X-Snapshot-Time']),(200,b'\xff\xd8x\xff\xd9','123.0'))
        with self.assertRaises(ValueError):await self.rotation.response(SimpleNamespace(match_info={'name':'nope'}))


if __name__=='__main__':
    unittest.main()
