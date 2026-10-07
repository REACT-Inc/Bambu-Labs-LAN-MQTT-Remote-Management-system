"""AI boxes on every live-view frame: the frame is sent together with the boxes the AI found in it."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import live_camera

JPEG = lambda n: b'\xff\xd8frame-' + str(n).encode() + b'\xff\xd9'


class FakeCore:
    EXAMPLE_MODE = False
    def names(self): return ['A1']
    def printer_config(self, name): return {'name': name, 'camera_type': 'rtsp'}


def request(after=0, feed=''):
    return SimpleNamespace(match_info={'name': 'A1'}, query={'after': str(after), 'feed': feed})


class LiveAI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def idle(feed):   # no real camera: the test puts frames itself
            await asyncio.Event().wait()
        self.patch = patch.object(live_camera.Feed, 'run', idle);self.patch.start()
        self.cameras = live_camera.Cameras(FakeCore());self.looked = []

    async def asyncTearDown(self):
        await self.cameras.close(None);self.patch.stop()

    def scorer(self, sync, delay=0):
        async def score(name, frame):
            await asyncio.sleep(delay);self.looked.append(frame)
            return dict(boxes=[{'label': 'spaghetti', 'score': 0.7, 'box': [0, 0, .5, .5]}], boxes_at=1, sync=sync, gap=0 if sync else 1)   # the CPU: a pause between looks
        return score

    async def frames(self, feed, *numbers):
        for n in numbers:
            await feed.put(JPEG(n));await asyncio.sleep(0.01)

    async def test_with_the_ai_hat_each_frame_comes_with_its_own_boxes(self):
        self.cameras.scorer = self.scorer(True)
        first = asyncio.create_task(self.cameras.frame_response(request()))   # starts the AI for this live view
        await asyncio.sleep(0);feed = self.cameras.feeds['A1']
        await self.frames(feed, 1)
        response = await first;ai = json.loads(response.headers['X-AI'])
        self.assertEqual(response.body, JPEG(1));self.assertEqual(self.looked, [JPEG(1)])
        self.assertTrue(ai['same']);self.assertEqual(ai['boxes'][0]['label'], 'spaghetti')
        # The next request gets the next frame the AI looked at, again with its own boxes.
        version, feed_id = response.headers['X-Camera-Version'], response.headers['X-Camera-Feed']
        await self.frames(feed, 2)
        response = await self.cameras.frame_response(request(version, feed_id))
        self.assertEqual(response.body, JPEG(2));self.assertTrue(json.loads(response.headers['X-AI'])['same'])

    async def test_on_the_cpu_the_picture_does_not_wait_for_the_ai(self):
        self.cameras.scorer = self.scorer(False)
        feed = self.cameras.acquire('A1')
        task = asyncio.create_task(self.cameras.frame_response(request()))
        await asyncio.sleep(0);await self.frames(feed, 1)
        await task
        self.assertFalse(feed.ai_sync)
        await self.frames(feed, 2)
        response = await self.cameras.frame_response(request(1, str(id(feed))))
        self.assertEqual(response.body, JPEG(2))                       # the newest picture straight away
        ai = json.loads(response.headers['X-AI'])
        self.assertEqual(ai['frame'], 1);self.assertFalse(ai['same'])  # with the AI's latest look
        await self.cameras.release('A1', feed)

    async def test_no_ai_means_plain_frames(self):
        task = asyncio.create_task(self.cameras.frame_response(request()))
        await asyncio.sleep(0);await self.frames(self.cameras.feeds['A1'], 1)
        response = await task
        self.assertEqual(response.body, JPEG(1));self.assertNotIn('X-AI', response.headers)

    async def test_stopping_the_view_never_cuts_the_ai_off_mid_frame(self):
        self.cameras.scorer = self.scorer(True, delay=0.05)
        task = asyncio.create_task(self.cameras.frame_response(request()))
        await asyncio.sleep(0);feed = self.cameras.feeds['A1']
        await feed.put(JPEG(1));await asyncio.sleep(0.01)            # the AI is busy with frame 1
        await feed.stop()
        await asyncio.sleep(0.1)
        self.assertEqual(self.looked, [JPEG(1)])                     # the helper's reply was still read
        task.cancel();await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
