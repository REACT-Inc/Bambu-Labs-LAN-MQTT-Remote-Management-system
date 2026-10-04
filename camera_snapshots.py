"""Staggered still snapshots for the dashboard's printer cards (#40).

Continuous camera streams for every printer overloaded the A1-family cameras, and browser requests that waited for a
frame used up the browser's few connections to the dashboard, so control clicks never reached the server. Instead:

- one background task takes a single still from one printer at a time (connect, one frame, disconnect),
  spreading printers across the interval so at most one camera connection is ever open for snapshots;
- it only runs while someone has the dashboard open (the page polls /api/state);
- a camera that fails is skipped for a while instead of being retried straight away;
- the browser only downloads the latest cached still, which never waits on a camera.
"""
import asyncio
import contextlib
import time

from aiohttp import web


class SnapshotRotation:
    def __init__(self, core, cameras, interval=30, spacing=5, timeout=25, backoff=120, idle_after=45):
        self.core, self.cameras = core, cameras
        self.interval, self.spacing, self.timeout, self.backoff, self.idle_after = interval, spacing, timeout, backoff, idle_after
        self.images = {}      # name -> (jpeg bytes, unix time taken)
        self.errors = {}      # name -> (message, unix time)
        self.retry_at = {}    # name -> monotonic time before which the camera is skipped
        self.viewed = None
        self.task = None

    def touch(self):
        """Called whenever a dashboard polls /api/state: snapshots only run while someone is looking."""
        self.viewed = time.monotonic()

    def viewing(self):
        return self.viewed is not None and time.monotonic() - self.viewed < self.idle_after

    def has_camera(self, name):
        config = getattr(self.core, 'printer_config', lambda n: None)(name) or {}
        return not getattr(self.core, 'EXAMPLE_MODE', False) and config.get('camera_type') in ('rtsp', 'jpeg_tcp')

    def state(self, name):
        image, error = self.images.get(name), self.errors.get(name)
        return {'time': image[1] if image else None, 'error': error[0] if error and (not image or error[1] > image[1]) else ''}

    async def capture(self, name):
        # Reuse a live view that's already streaming this printer: A1-family cameras accept one client at a time.
        feed = self.cameras.feeds.get(name)
        if feed and feed.frame and time.time() - feed.updated < 5:
            return feed.frame
        # Shared with Discord /printer: never two captures of the same camera at once (#54).
        capture_still = getattr(self.core, 'capture_still', None)
        if capture_still:
            return await capture_still(name, self.timeout)
        config = self.core.printer_config(name)
        return await asyncio.wait_for(asyncio.to_thread(self.core.snapshot_bytes, config), self.timeout)

    async def take(self, name):
        try:
            image = await self.capture(name)
        except Exception as exc:  # includes asyncio.TimeoutError
            image = None
            reason = 'Camera timed out' if isinstance(exc, asyncio.TimeoutError) else 'Camera unavailable'
        else:
            reason = 'Camera unavailable'
        if image:
            self.images[name] = (image, time.time())
            self.errors.pop(name, None)
            self.retry_at.pop(name, None)
        else:
            self.errors[name] = (reason + '; retrying in a few minutes.', time.time())
            self.retry_at[name] = time.monotonic() + self.backoff

    async def run(self):
        while True:
            try:
                names = [n for n in self.core.names() if self.has_camera(n)] if self.viewing() else []
                if not names:
                    await asyncio.sleep(3)
                    continue
                gap = max(self.spacing, self.interval / len(names))
                for name in names:
                    if not self.viewing():
                        break
                    connected = self.core.state_data(name)[3]
                    if connected and time.monotonic() >= self.retry_at.get(name, 0):
                        await self.take(name)
                        await asyncio.sleep(gap)
                    else:
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.core.log.exception('Snapshot rotation error')
                await asyncio.sleep(10)

    async def start(self, app):
        self.task = asyncio.create_task(self.run())

    async def stop(self, app):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def response(self, request):
        """The latest cached still. Returns immediately and never opens a camera connection."""
        name = request.match_info['name']
        if name not in self.core.names():
            raise ValueError('Unknown printer.')
        image = self.images.get(name)
        if not image:
            return web.Response(status=204, headers={'Cache-Control': 'no-store'})
        return web.Response(body=image[0], content_type='image/jpeg',
                            headers={'Cache-Control': 'private, max-age=60', 'X-Snapshot-Time': str(image[1])})
