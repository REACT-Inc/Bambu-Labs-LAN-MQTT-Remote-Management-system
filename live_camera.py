"""Shared camera connections. No recording; keep only the latest JPEG in RAM."""
import asyncio
import contextlib
import logging
import shutil
import ssl
import json
import struct
import time
from aiohttp import web

MAX_FRAME=8*1024*1024
log=logging.getLogger(__name__)

def camera_error(printer,error):
    text=str(error) or 'No complete camera frame arrived before timeout.'
    code=str(printer.get('access_code',''))
    if code:text=text.replace(code,'[hidden]')
    return (type(error).__name__+': '+text.replace('\n',' '))[:300]



LIVE_AI_WAIT=2.5   # how long a live frame request waits for the AI to finish that frame before sending it without


class Feed:
    def __init__(self,printer):
        self.printer=printer;self.users=0;self.frame=None;self.version=0
        self.updated=0;self.status='Connecting';self.error='';self.condition=asyncio.Condition()
        # Live AI boxes: the newest frame the AI looked at, with what it found. sync: the AI keeps up with the camera
        # (AI HAT), so frames are sent together with their own boxes; on the CPU the boxes follow when they're ready.
        self.scored=None;self.ai_sync=False;self.ai_task=None
        self.task=asyncio.create_task(self.run())

    async def stop(self):
        tasks=[t for t in (self.task,self.ai_task) if t]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def put(self,frame):
        if not (frame.startswith(b'\xff\xd8') and frame.endswith(b'\xff\xd9')):return
        async with self.condition:
            self.frame=frame;self.version+=1;self.updated=time.time();self.status='Live';self.error=''
            self.condition.notify_all()

    async def next(self,version,timeout=25):
        async with self.condition:
            await asyncio.wait_for(self.condition.wait_for(lambda:self.version>version and time.time()-self.updated<5),timeout)
            return self.version,self.frame

    async def tcp(self):
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE
        reader,writer=await asyncio.wait_for(asyncio.open_connection(self.printer['ip'],6000,ssl=ctx),8)
        try:
            writer.write(struct.pack('<IIII32s32s',0x40,0x3000,0,0,b'bblp',self.printer['access_code'].encode()))
            await writer.drain()
            while True:
                header=await asyncio.wait_for(reader.readexactly(16),20)
                size=int.from_bytes(header[:4],'little')
                if not 4<=size<=MAX_FRAME:raise ValueError('Invalid frame length')
                await self.put(await asyncio.wait_for(reader.readexactly(size),20))
        finally:
            writer.close()
            with contextlib.suppress(Exception):await asyncio.wait_for(writer.wait_closed(),2)

    async def rtsp(self):
        p=self.printer;url=f"rtsps://bblp:{p['access_code']}@{p['ip']}:322/streaming/live/1"
        # Lower priority and at most 2 decoder threads, so a live view never starves the Pi (#54).
        nice=shutil.which('nice');prefix=[nice,'-n','10'] if nice else []
        process=await asyncio.create_subprocess_exec(*prefix,'ffmpeg','-nostdin','-loglevel','error','-threads','2','-rtsp_transport','tcp','-i',url,
            '-an','-vf','fps=5,scale=960:-2','-f','image2pipe','-vcodec','mjpeg','-q:v','5','pipe:1',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        errors=bytearray()
        async def read_errors():
            while chunk:=await process.stderr.read(2048):
                errors.extend(chunk);del errors[:-4096]
        error_task=asyncio.create_task(read_errors())
        try:
            buffer=b''
            while True:
                chunk=await asyncio.wait_for(process.stdout.read(65536),20)
                if not chunk:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(error_task),2)
                    raise EOFError(errors.decode(errors='replace') or 'ffmpeg camera stream ended')
                buffer+=chunk
                while True:
                    start=buffer.find(b'\xff\xd8');end=buffer.find(b'\xff\xd9',max(start,0)+2)
                    if start<0 or end<0:break
                    await self.put(buffer[start:end+2]);buffer=buffer[end+2:]
                if len(buffer)>MAX_FRAME:raise ValueError('Frame too large')
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):process.terminate()
                try:await asyncio.wait_for(process.wait(),3)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):process.kill()
                    await process.wait()
            error_task.cancel()
            await asyncio.gather(error_task,return_exceptions=True)

    async def run(self):
        while True:
            try:
                self.status='Connecting'
                if self.printer.get('camera_type')=='rtsp':await self.rtsp()
                else:await self.tcp()
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.error=camera_error(self.printer,exc)
                self.status='Camera unavailable — reconnecting'
                log.warning('Camera %s: %s',self.printer.get('name','printer'),self.error)
                await asyncio.sleep(3)


class Cameras:
    def __init__(self,core):
        self.core=core;self.feeds={};self.closing=False;self.idle={}
        self.scorer=None   # async (name, jpeg) -> AI overlay for that frame, or None; set by the dashboard

    async def score_frames(self,name,feed):
        """While someone watches a printer live, the AI looks at every new frame it can keep up with (the newest one
        each time, never a backlog) so the boxes follow the picture. Display only: nothing is judged or acted on."""
        version=0
        while True:
            try:version,frame=await feed.next(version,25)
            except asyncio.TimeoutError:continue
            # Shielded: stopping the live view mid-frame must not cut the AI helper off halfway through a reply.
            try:overlay=await asyncio.shield(self.scorer(name,frame))
            except asyncio.CancelledError:raise
            except Exception:overlay=None
            if not overlay:
                feed.ai_sync=False;feed.scored=None
                await asyncio.sleep(5);continue
            async with feed.condition:
                feed.scored=(version,overlay,time.time(),frame);feed.ai_sync=bool(overlay.get('sync'))
                feed.condition.notify_all()
            if overlay.get('gap'):await asyncio.sleep(overlay['gap'])   # the CPU: not every frame

    def acquire(self,name):
        if name not in self.core.names():raise ValueError('Unknown printer.')
        if self.core.EXAMPLE_MODE:raise ValueError('No camera in demo mode.')
        p=self.core.printer_config(name)
        if not p or p.get('camera_type') not in ('rtsp','jpeg_tcp'):raise ValueError('Camera type is not configured.')
        idle=self.idle.pop(name,None)
        if idle:idle.cancel()
        feed=self.feeds.get(name)
        if feed is None:feed=self.feeds[name]=Feed(p)
        if feed.users>=12:raise ValueError('Too many camera viewers. Close another view.')
        feed.users+=1
        return feed

    async def release(self,name,feed,linger=0):
        feed.users-=1
        if linger and feed.users==0 and not self.closing:
            async def expire():
                await asyncio.sleep(linger)
                if feed.users==0 and self.feeds.get(name) is feed:
                    self.feeds.pop(name,None);await feed.stop()
                self.idle.pop(name,None)
            self.idle[name]=asyncio.create_task(expire());return
        if feed.users==0 and self.feeds.get(name) is feed:
            self.feeds.pop(name,None);await feed.stop()

    async def snapshot(self,name):
        try:feed=self.acquire(name)
        except ValueError:return None
        try:
            _,frame=await feed.next(0)
            return frame
        except asyncio.TimeoutError:return None
        finally:await self.release(name,feed)

    def state(self,name):
        feed=self.feeds.get(name)
        if not feed:return {'status':'Stopped','updated':None}
        return {'status':feed.status,'error':feed.error,'updated':feed.updated or None}

    async def close(self,app):
        self.closing=True
        idle=list(self.idle.values());self.idle.clear()
        for task in idle:task.cancel()
        await asyncio.gather(*idle,return_exceptions=True)
        feeds=list(self.feeds.values());self.feeds.clear()
        await asyncio.gather(*(f.stop() for f in feeds),return_exceptions=True)

    async def frame_response(self,request):
        name=request.match_info['name'];feed=self.acquire(name)
        try:
            version=0
            try:version=int(request.query.get('after','0'))
            except ValueError:pass
            # A new feed resets its counter; only use versions belonging to this feed.
            if request.query.get('feed')!=str(id(feed)):version=0
            if self.scorer and not feed.ai_task:
                # Assume the AI keeps up until its first look says otherwise, so even the first frame has its boxes.
                feed.ai_sync=True;feed.ai_task=asyncio.create_task(self.score_frames(name,feed))
            frame=None
            if feed.ai_sync:   # the AI keeps up: send the next frame it has looked at, with its own boxes
                frame=await self.scored_frame(feed,version)
                if frame:version=frame[0];frame=frame[1]
            if frame is None:
                try:version,frame=await feed.next(version,10)
                except asyncio.TimeoutError:
                    raise web.HTTPServiceUnavailable(text=feed.error or 'No camera frames received. Check camera settings, access code and LAN connectivity.')
            headers={'Cache-Control':'no-store','X-Camera-Version':str(version),'X-Camera-Feed':str(id(feed))}
            scored=feed.scored
            if scored and time.time()-scored[2]<10:
                headers['X-AI']=json.dumps(dict(scored[1],frame=scored[0],same=scored[0]==version),separators=(',',':'))
            return web.Response(body=frame,content_type='image/jpeg',headers=headers)
        finally:await self.release(name,feed,linger=15)

    async def scored_frame(self,feed,version):
        """(version, jpeg) of the next frame the AI has finished, or None if it doesn't come in time. The frame is
        the one the AI looked at, so its boxes match it exactly."""
        async with feed.condition:
            try:
                await asyncio.wait_for(feed.condition.wait_for(lambda:feed.scored and feed.scored[0]>version),LIVE_AI_WAIT)
            except asyncio.TimeoutError:return None
            return feed.scored[0],feed.scored[3]

    async def stream(self,request):
        name=request.match_info['name'];feed=self.acquire(name)
        response=web.StreamResponse(headers={'Content-Type':'multipart/x-mixed-replace; boundary=frame','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        try:
            await response.prepare(request);version=0
            while not self.closing and request.transport and not request.transport.is_closing():
                token=request.cookies.get('pm_session','')
                if request.app['dashboard'].sessions.get(token) is not request['session'] or request['session']['expires']<=time.time():break
                try:version,frame=await feed.next(version,5)
                except asyncio.TimeoutError:continue
                await asyncio.wait_for(response.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(frame)).encode()+b'\r\n\r\n'+frame+b'\r\n'),5)
        except (ConnectionError,asyncio.TimeoutError):pass
        finally:await self.release(name,feed)
        return response
