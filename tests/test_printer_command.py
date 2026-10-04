"""/printer on an RTSP camera printer (H2D) must be cheap for the Pi and never wait on the camera (#54, #44)."""
import asyncio,importlib.util,json,os,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch

JPEG=b'\xff\xd8picture\xff\xd9'


def interaction():
    message=SimpleNamespace(edit=AsyncMock())
    i=MagicMock();i.guild_id=123;i.channel_id=789;i.user=SimpleNamespace(id=7)
    i.response=SimpleNamespace(is_done=lambda:True,defer=AsyncMock())
    i.sent_fields=[]
    async def send(**kwargs):
        i.sent_fields.append([f.value for f in kwargs['embed'].fields]);return message   # as sent, before later edits
    i.followup=SimpleNamespace(send=AsyncMock(side_effect=send));i.message_=message
    return i


class PrinterCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'guild_ids':[123],'printers':[
            {'name':'H2D','model':'H2D','ip':'192.0.2.1','serial':'094TEST','access_code':'12345678','camera_type':'rtsp'}]}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('test_core_printer',Path(__file__).parents[1]/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
        self.core.state_data=lambda n:('IDLE',0,{'nozzle_temper':25},True)
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()

    async def test_status_card_first_then_picture(self):
        started=threading.Event()
        def slow_capture(printer):
            started.set();time.sleep(0.3);return JPEG
        i=interaction()
        with patch.object(self.core,'snapshot_bytes',slow_capture):
            await self.core.run_action(i,'printer','H2D')
        first=i.followup.send.call_args.kwargs
        self.assertNotIn('file',first);self.assertTrue(first['wait'])        # sent before the camera answered
        self.assertIn('Getting a picture',i.sent_fields[0][-1])
        edit=i.message_.edit.call_args.kwargs
        self.assertEqual(len(edit['attachments']),1);self.assertEqual(edit['embed'].image.url,'attachment://printer.jpg')
        self.assertNotIn('Camera',[f.name for f in edit['embed'].fields])

    async def test_camera_that_never_answers_still_gets_a_card(self):
        i=interaction()
        with patch.object(self.core,'snapshot_bytes',lambda p:None):
            await self.core.run_action(i,'printer','H2D')
        self.assertEqual(i.followup.send.await_count,1)
        edit=i.message_.edit.call_args.kwargs
        self.assertEqual(edit['attachments'],[]);self.assertIn('Unavailable',[f.value for f in edit['embed'].fields if f.name=='Camera'][0])

    async def test_reuses_a_picture_on_hand_instead_of_opening_the_camera(self):
        capture=MagicMock()
        self.core.snapshot_rotation=SimpleNamespace(images={'H2D':(JPEG,time.time()-10)})
        i=interaction()
        with patch.object(self.core,'snapshot_bytes',capture):
            await self.core.run_action(i,'printer','H2D')
        capture.assert_not_called();self.assertIn('file',i.followup.send.call_args.kwargs)
        self.core.snapshot_rotation.images['H2D']=(JPEG,time.time()-300)           # too old: take a new one
        self.assertIsNone(self.core.recent_picture('H2D'))

    async def test_concurrent_requests_share_one_capture(self):
        calls=[]
        def capture(printer):
            calls.append(1);time.sleep(0.2);return JPEG
        with patch.object(self.core,'snapshot_bytes',capture):
            results=await asyncio.gather(*(self.core.snapshot('H2D') for _ in range(4)))
        self.assertEqual(results,[JPEG]*4);self.assertEqual(len(calls),1)

    def test_single_still_is_cheap_and_low_priority(self):
        run=MagicMock(return_value=SimpleNamespace(returncode=0,stdout=JPEG))
        with patch.object(self.core.subprocess,'run',run),patch.object(self.core.shutil,'which',return_value='/usr/bin/nice'):
            self.assertEqual(self.core.snapshot_bytes(self.core.printer_config('H2D')),JPEG)
        command=run.call_args.args[0]
        self.assertEqual(command[:3],['/usr/bin/nice','-n','10'])
        for option in (['-threads','1'],['-skip_frame','nokey'],['-frames:v','1']):
            self.assertIn(option,[command[k:k+2] for k in range(len(command))])
        with patch.object(self.core.shutil,'which',return_value=None):
            self.assertEqual(self.core.low_priority(['ffmpeg']),['ffmpeg'])   # no nice: still works


class LiveFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_feed_is_low_priority_with_capped_threads(self):
        import live_camera
        seen=[]
        async def fake_exec(*args,**kwargs):
            seen.append(args);raise OSError('stop here')
        with patch.object(live_camera.asyncio,'create_subprocess_exec',fake_exec),patch.object(live_camera.shutil,'which',return_value='/usr/bin/nice'):
            feed=live_camera.Feed({'name':'H2D','ip':'192.0.2.1','access_code':'x','camera_type':'rtsp'})
            await asyncio.sleep(0.05);feed.task.cancel();await asyncio.gather(feed.task,return_exceptions=True)
        args=list(seen[0])
        self.assertEqual(args[:4],['/usr/bin/nice','-n','10','ffmpeg']);self.assertIn(['-threads','2'],[args[k:k+2] for k in range(len(args))])


if __name__=='__main__':
    unittest.main()
