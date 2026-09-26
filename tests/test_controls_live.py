import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from printer_controls import Controls,prepare
from live_camera import Cameras,Feed


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.core=SimpleNamespace(names=lambda:['A1 Mini'],printer_config=lambda n:{'serial':'test','name':n},
            EXAMPLE_MODE=False,last_seen={'A1 Mini':time.time()},state_data=lambda n:('IDLE',0,{},True),clients={'A1 Mini':MagicMock()})
        self.client=self.core.clients['A1 Mini'];self.client.publish.return_value.rc=0
        self.store=SimpleNamespace(jobs=lambda:[],event=MagicMock());self.controls=Controls(self.core,self.store)
    def test_limits_and_injection(self):
        for kind,value,axis in [('bed',81,None),('nozzle',301,None),('fan',101,None),('move',11,'X'),('move',2,'Z'),('move',1,'X\nG28'),('move',float('nan'),'X'),('bed',True,None),('bed','20\nG28',None),('bed',10.5,None)]:
            with self.assertRaises(ValueError):prepare(self.core,'A1 Mini',kind,value,axis)
        self.client.publish.assert_not_called()
    def test_payload(self):
        self.controls.apply('A1 Mini','move',-1,'Z',True,True)
        data=json.loads(self.client.publish.call_args.args[1])['print']
        self.assertEqual(data['command'],'gcode_line')
        self.assertEqual(data['param'],'M211 S\nM211 X1 Y1 Z1\nM1002 push_ref_mode\nG91\nG1 Z-1.0 F300\nM1002 pop_ref_mode\nM211 R\n')
        with self.assertRaises(ValueError):self.controls.apply('A1 Mini','move',-1,'Z',True,True)
    def test_axis_ctrl_firmware_uses_xyz_ctrl(self):
        # Bit 38 of "fun" marks firmware (e.g. H2D) that jogs via xyz_ctrl instead of G-code.
        self.core.state_data=lambda n:('IDLE',0,{'fun':format(1<<38,'x')},True)
        for value,axis,expected in [(10,'X',{'axis':'X','dir':1,'mode':1}),(-1,'Y',{'axis':'Y','dir':-1,'mode':0}),(-1,'Z',{'axis':'Z','dir':-1,'mode':0})]:
            self.controls.moved.clear()
            self.controls.apply('A1 Mini','move',value,axis,True,True)
            data=json.loads(self.client.publish.call_args.args[1])['print']
            self.assertEqual({k:data[k] for k in ('command','axis','dir','mode')},{'command':'xyz_ctrl',**expected})
        self.client.publish.reset_mock();self.controls.moved.clear()
        for value,axis in [(5,'X'),(0.5,'Y')]:
            with self.assertRaises(ValueError):self.controls.apply('A1 Mini','move',value,axis,True,True)
        self.client.publish.assert_not_called()
    def test_axis_ctrl_flag_parsing(self):
        from printer_controls import axis_ctrl_supported
        self.assertFalse(axis_ctrl_supported({}));self.assertFalse(axis_ctrl_supported({'fun':'garbage'}))
        self.assertFalse(axis_ctrl_supported({'fun':format((1<<38)-1,'X')}));self.assertTrue(axis_ctrl_supported({'fun':'4000000000'}))
    def test_move_rechecks(self):
        for state in ['RUNNING','PAUSE','PREPARE','FAILED','unknown']:
            self.core.state_data=lambda n,s=state:(s,0,{},True)
            with self.assertRaises(ValueError):self.controls.apply('A1 Mini','move',1,'X',True,True)
        self.core.state_data=lambda n:('IDLE',0,{},True)
        self.core.last_seen['A1 Mini']=time.time()-60
        with self.assertRaises(ValueError):self.controls.apply('A1 Mini','move',1,'X',True,True)
        self.client.publish.assert_not_called()
    def test_queue_blocks_move(self):
        self.store.jobs=lambda:[{'printer':'A1 Mini','status':'staging'}]
        with self.assertRaises(ValueError):self.controls.apply('A1 Mini','move',1,'X',True,True)
    def test_confirmation_required(self):
        with self.assertRaises(ValueError):self.controls.apply('A1 Mini','bed',60)
        with self.assertRaises(ValueError):self.controls.apply('A1 Mini','move',1,'X',True,False)
    def test_speed_and_fan(self):
        self.assertEqual(prepare(self.core,'A1 Mini','speed','sport')[:2],('print_speed','3'))
        self.assertEqual(prepare(self.core,'A1 Mini','fan',100)[1][0]['param'],'M106 P1 S255\n')
    def test_publish_failure(self):
        self.client.publish.return_value.rc=4
        with self.assertRaises(ValueError):self.controls.apply('A1 Mini','bed',60,confirmed=True)
        self.store.event.assert_not_called()


class CameraTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_connection_and_cleanup(self):
        async def run(feed):await asyncio.Event().wait()
        c=Cameras(SimpleNamespace(names=lambda:['A'],EXAMPLE_MODE=False,printer_config=lambda n:{'camera_type':'jpeg_tcp'}))
        with patch.object(Feed,'run',run):
            a=c.acquire('A');b=c.acquire('A');self.assertIs(a,b)
            await a.put(b'\xff\xd8test\xff\xd9')
            self.assertEqual((await a.next(0))[1],b'\xff\xd8test\xff\xd9')
            self.assertEqual(await c.snapshot('A'),a.frame)
            await c.release('A',a);self.assertIn('A',c.feeds)
            await c.release('A',b);self.assertNotIn('A',c.feeds);self.assertTrue(a.task.done())
    async def test_fragmented_tcp_frame(self):
        reader=asyncio.StreamReader();frame=b'\xff\xd8abc\xff\xd9'
        header=len(frame).to_bytes(4,'little')+bytes(12)
        for b in header+frame:reader.feed_data(bytes([b]))
        reader.feed_eof()
        writer=MagicMock();writer.drain=unittest.mock.AsyncMock();writer.wait_closed=unittest.mock.AsyncMock()
        async def idle(feed):await asyncio.Event().wait()
        with patch.object(Feed,'run',idle):
            feed=Feed({'ip':'test','access_code':'test'})
            with patch('asyncio.open_connection',unittest.mock.AsyncMock(return_value=(reader,writer))):
                with self.assertRaises(asyncio.IncompleteReadError):await feed.tcp()
            self.assertEqual(feed.frame,frame);writer.close.assert_called_once()
            feed.task.cancel();await asyncio.gather(feed.task,return_exceptions=True)
    async def test_stale_frame_not_returned(self):
        async def run(feed):await asyncio.Event().wait()
        with patch.object(Feed,'run',run):
            f=Feed({});await f.put(b'\xff\xd8x\xff\xd9');f.updated=time.time()-60
            with self.assertRaises(asyncio.TimeoutError):await f.next(0,0.01)
            f.task.cancel();await asyncio.gather(f.task,return_exceptions=True)
