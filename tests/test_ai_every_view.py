"""AI boxes on every camera view: drawn onto pictures that leave as images (Discord, Take camera snapshot, the raw
live stream), and sent with every dashboard still so the browser draws them over it. The AI itself, training
pictures and the calibration picture always stay plain."""
import asyncio,importlib.util,io,json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT))
from PIL import Image
from failureDetection import overlay_draw
from camera_snapshots import SnapshotRotation

OVERLAY=dict(boxes=[dict(label='spaghetti',score=0.9,box=[0.25,0.25,0.75,0.75],counts=True)],threshold=0.6,hold=0.45,zoom=[])


def jpeg(colour=(60,60,60),size=(640,360)):
    out=io.BytesIO();Image.new('RGB',size,colour).save(out,'JPEG');return out.getvalue()


class DrawTests(unittest.TestCase):
    def test_boxes_are_drawn_onto_the_picture(self):
        drawn=Image.open(io.BytesIO(overlay_draw.draw(jpeg(),OVERLAY)))
        self.assertEqual(drawn.size,(640,360))
        r,g,b=drawn.getpixel((160,180))   # the left edge of the box: the failure red
        self.assertGreater(r,200);self.assertLess(g,140)
        self.assertLess(max(drawn.getpixel((320,180))),90)   # inside the box: the picture as it was
        nothing=Image.open(io.BytesIO(overlay_draw.draw(jpeg(),dict(OVERLAY,boxes=[]))))
        self.assertLess(max(nothing.getpixel((160,180))),90)   # no boxes, only the small "AI · nothing found" note

    def test_colours_follow_the_dashboard(self):
        box=lambda score,counts=True:dict(score=score,counts=counts)
        self.assertEqual([overlay_draw.colour(b,OVERLAY) for b in (box(0.9),box(0.5),box(0.2),box(0.9,False))],
                         [overlay_draw.RED,overlay_draw.YELLOW,overlay_draw.FAINT,overlay_draw.BLUE])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self,backend):
        from failureDetection.detection import FailureMonitor
        core=SimpleNamespace(CONFIG={'failure_detection':{'enabled':True,'model':'m.hef'}},settings={},names=lambda:['A1'],
                             DATA_DIR=Path(tempfile.mkdtemp()),EXAMPLE_MODE=False,printer_config=lambda n:{'camera_type':'rtsp'})
        return FailureMonitor(core,SimpleNamespace(),backend)

    async def test_annotated_draws_and_falls_back_to_the_plain_picture(self):
        class Backend:
            calls=0
            async def score(self,picture,crops=None):
                Backend.calls+=1;return 0.9,[dict(label='spaghetti',score=0.9,box=[0.25,0.25,0.75,0.75])]
        m=self.monitor(Backend());picture=jpeg()
        self.assertNotEqual(await m.annotated('A1',picture),picture);self.assertEqual(Backend.calls,1)
        # Boxes the AI already found in this very picture: drawn without looking again.
        self.assertNotEqual(await m.annotated('A1',picture,OVERLAY),picture);self.assertEqual(Backend.calls,1)
        m.core.settings['ai_watch_off']=['A1']
        self.assertEqual(await m.annotated('A1',picture),picture)            # watching off: no boxes
        self.assertEqual(await m.annotated('A1',picture,OVERLAY),picture)    # ... not even ones found earlier
        class Broken:
            async def score(self,picture,crops=None):raise RuntimeError('AI helper stopped')
        self.assertEqual(await self.monitor(Broken()).annotated('A1',picture),picture)   # the AI can't look: sent anyway


class StillTests(unittest.IsolatedAsyncioTestCase):
    def rotation(self,pictures):
        core=SimpleNamespace(names=lambda:['A1'],printer_config=lambda n:{'camera_type':'rtsp'},EXAMPLE_MODE=False,
                             capture_still=AsyncMock(side_effect=pictures),state_data=lambda n:('IDLE',0,{},True),log=MagicMock())
        return SnapshotRotation(core,SimpleNamespace(feeds={}))

    async def test_every_dashboard_still_comes_with_its_own_boxes(self):
        rotation=self.rotation([b'still-1',b'still-2']);looked=[]
        async def look(name,picture):looked.append(picture);return OVERLAY
        rotation.ai=look
        await rotation.take('A1')
        self.assertEqual(looked,[b'still-1']);self.assertEqual(rotation.state('A1')['ai'],OVERLAY)
        self.assertEqual(rotation.overlay_for('A1',rotation.images['A1'][0]),OVERLAY)   # Discord reuses these boxes
        async def broken(name,picture):raise RuntimeError('AI helper stopped')
        rotation.ai=broken;await rotation.take('A1')
        self.assertTrue(rotation.state('A1')['time'])         # the new still is shown
        self.assertIsNone(rotation.state('A1')['ai'])         # ... never with the previous still's boxes
        self.assertIsNone(rotation.overlay_for('A1',rotation.images['A1'][0]))

    async def test_a_still_is_shown_straight_away_and_its_boxes_follow(self):
        rotation=self.rotation([b'still']);release=asyncio.Event()
        async def slow(name,picture):await release.wait();return OVERLAY   # a busy AI
        rotation.ai=slow
        taking=asyncio.create_task(rotation.take('A1'));await asyncio.sleep(0.05)
        self.assertTrue(rotation.state('A1')['time']);self.assertIsNone(rotation.state('A1')['ai'])   # shown already
        release.set();await taking
        self.assertEqual(rotation.state('A1')['ai'],OVERLAY)


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        d=tempfile.mkdtemp();(Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1]}))
        with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
            spec=importlib.util.spec_from_file_location('core_ai_views',ROOT/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
    async def asyncTearDown(self):await self.core.bot.close()

    async def test_discord_pictures_get_the_boxes(self):
        core=self.core
        self.assertEqual(await core.ai_picture('A1',b'plain'),b'plain')   # AI off
        core.failure_monitor=SimpleNamespace(enabled=True,annotated=AsyncMock(return_value=b'boxed'))
        self.assertEqual(await core.ai_picture('A1',b'plain'),b'boxed')
        self.assertIsNone(await core.ai_picture('A1',None))
        source=(ROOT/'core.py').read_text()
        # /printer (both ways it gets a picture) and every notification with a camera picture.
        self.assertIn('await ai_picture(name, recent_picture(name))',source)
        self.assertIn('await ai_picture(name, await snapshot(name, timeout=20))',source)
        self.assertIn('picture = await ai_picture(name, await snapshot(name)) if camera else None',source)

    async def test_a_picture_the_ai_already_checked_is_not_checked_again(self):
        core=self.core;still,frame=b'dashboard still',b'live frame'
        rotation=SnapshotRotation(SimpleNamespace(),SimpleNamespace(feeds={}))
        rotation.images['A1']=(still,100.0);rotation.overlays['A1']=(100.0,OVERLAY)
        core.snapshot_rotation=rotation
        core.live_cameras=SimpleNamespace(feeds={'A1':SimpleNamespace(scored=(7,dict(OVERLAY,sync=True),time.time(),frame))})
        core.failure_monitor=SimpleNamespace(enabled=True,annotated=AsyncMock(return_value=b'boxed'))
        for picture in (still,frame,b'something new'):await core.ai_picture('A1',picture)
        known=[call.args[2] for call in core.failure_monitor.annotated.await_args_list]
        self.assertEqual(known[0],OVERLAY);self.assertEqual(known[1]['boxes'],OVERLAY['boxes'])
        self.assertIsNone(known[2])   # a picture the AI hasn't seen: it looks at it


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def stream(self,score,frames):
        """Frames as the raw /api/live stream sends them (until one with boxes arrives)."""
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        import live_camera
        async def idle(feed):await asyncio.Event().wait()
        session={'expires':time.time()+60}
        cameras=live_camera.Cameras(SimpleNamespace(EXAMPLE_MODE=False,names=lambda:['A1'],printer_config=lambda n:{'name':n,'camera_type':'rtsp'}))
        drawn=[]
        async def drawer(frame,overlay):drawn.append(frame);return b'\xff\xd8boxed\xff\xd9'
        cameras.scorer,cameras.drawer=score,drawer
        @web.middleware
        async def login(request,handler):request['session']=session;return await handler(request)
        app=web.Application(middlewares=[login]);app['dashboard']=SimpleNamespace(sessions={'t':session})
        app.router.add_get('/api/live/{name}',cameras.stream)
        with patch.object(live_camera.Feed,'run',idle):
            async with TestClient(TestServer(app)) as client:
                response=await client.get('/api/live/A1',cookies={'pm_session':'t'})
                feed=cameras.feeds['A1']
                for frame in frames:
                    await feed.put(frame);await asyncio.sleep(0.15)
                chunk=b''
                while chunk.count(b'boxed')<len(frames):chunk+=await asyncio.wait_for(response.content.read(64),5)
                response.close()
            await cameras.close(None)
        return drawn

    async def test_the_raw_live_stream_has_the_boxes_drawn_on(self):
        async def score(name,frame):return dict(OVERLAY,sync=True,gap=0)   # the AI HAT keeps up
        self.assertEqual(await self.stream(score,[b'\xff\xd8frame-1\xff\xd9']),[b'\xff\xd8frame-1\xff\xd9'])

    async def test_on_the_cpu_the_latest_look_is_drawn_on_every_frame(self):
        async def score(name,frame):return dict(OVERLAY,sync=False,gap=60)   # looks once, then not for a while
        frames=[b'\xff\xd8frame-1\xff\xd9',b'\xff\xd8frame-2\xff\xd9']
        self.assertEqual(await self.stream(score,frames),frames)   # no flicker: both frames have the boxes


class HelperProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_caller_that_gives_up_never_leaves_its_answer_for_the_next_one(self):
        """Discord pictures and dashboard stills wait a limited time for the AI. Giving up must not leave the helper's
        answer unread, or the next question (maybe a real failure check) would get that old answer."""
        from failureDetection.detection import HailoBackend,settings_for
        with tempfile.TemporaryDirectory() as d:
            model=Path(d)/'print_failure.hef';model.write_bytes(b'hef')
            python=Path(d)/'python3'   # stands in for the AI helper: answers each question 0.3 s later, numbered
            python.write_text('#!/bin/sh\necho \'{"ready": true, "backend": "hailo"}\'\nn=0\n'
                              'while read line; do n=$((n+1)); sleep 0.3; echo "{\\"score\\": $n, \\"detections\\": []}"; done\n')
            os.chmod(python,0o755)
            backend=HailoBackend(settings_for({'failure_detection':{'enabled':True,'model':str(model),'python':str(python)}}))
            try:
                with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(backend.score(b'first'),0.1)
                score,_=await backend.score(b'second')
                self.assertEqual(score,2.0)   # its own answer, not the abandoned first one
            finally:
                await backend.close()


if __name__=='__main__':
    unittest.main()
