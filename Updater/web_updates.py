"""Authenticated update intake; the web process never writes system application files."""
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from aiohttp import web
from Updater.update_package import inspect_package,MAX_ZIP

STATUS=Path('/var/lib/pm-updater/status.json')
BUSY={'queued','validating','preparing','backing_up','installing','checking','rolling_back','recovery_required'}


class WebUpdates:
    def __init__(self,dashboard):
        self.dashboard=dashboard;self.core=dashboard.core;self.inbox=self.core.DATA_DIR/'updates'
        self.inbox.mkdir(mode=0o700,exist_ok=True);self.lock=asyncio.Lock();self.previews={};self.pending=None;self.queued_at=0
        self.core.update_pending=self.busy
        dashboard.app.add_routes([web.get('/api/update/status',self.status),web.post('/api/update/upload',self.upload),web.post('/api/update/install',self.install)])

    def read_status(self):
        try:return json.loads(STATUS.read_text())
        except (OSError,ValueError):return {'state':'idle','message':'No web update has run yet.'}

    def busy(self):
        state=self.read_status()
        if self.pending:
            if (state.get('id')==self.pending or (state.get('state')=='failed' and state.get('time',0)>=self.queued_at)) and state.get('state') not in BUSY:self.pending=None
            else:return True
        return (self.inbox/'request.json').exists() or state.get('state') in BUSY

    async def status(self,request):
        return web.json_response(dict(self.read_status(),enabled=Path('/etc/systemd/system/pm-web-update.path').exists(),busy=self.busy()))

    async def upload(self,request):
        async with self.lock:
            if self.busy():raise ValueError('An update is already pending.')
            if shutil.disk_usage(self.inbox).free<256*1024*1024:raise ValueError('Free at least 256 MiB before uploading an update.')
            reader=await request.multipart();part=await reader.next()
            if not part or part.name!='file' or not (part.filename or '').lower().endswith('.zip'):raise ValueError('Choose a management release ZIP.')
            token=secrets.token_hex(16);path=self.inbox/(token+'.zip');size=0;digest=hashlib.sha256()
            try:
                with path.open('xb') as stream:
                    while chunk:=await part.read_chunk(65536):
                        size+=len(chunk)
                        if size>MAX_ZIP:raise ValueError('Maximum update ZIP size is 32 MiB.')
                        stream.write(chunk);digest.update(chunk)
                preview=await asyncio.to_thread(inspect_package,path)
                # Keep only a few recent, uncommitted uploads.
                for old in sorted(self.inbox.glob('*.zip'),key=lambda p:p.stat().st_mtime)[:-3]:old.unlink(missing_ok=True)
                self.previews={token:dict(preview,sha256=digest.hexdigest(),session=request.cookies.get('pm_session'))}
                return web.json_response(dict(preview,token=token,sha256=digest.hexdigest()))
            except BaseException:path.unlink(missing_ok=True);raise

    async def install(self,request):
        async with self.lock:
            if self.busy():raise ValueError('An update is already pending.')
            data=await request.json();token=data.get('token');preview=self.previews.get(token)
            if data.get('confirmed') is not True or not preview or preview['session']!=request.cookies.get('pm_session'):
                raise ValueError('Upload and review the ZIP in this session, then confirm installation.')
            self.queue_package(token,preview['sha256'])
            self.previews.clear()
            return web.json_response({'ok':True,'id':token,'message':'Update queued. The dashboard will disconnect during restart.'})

    def idle_check(self,automatic=False):
        if self.busy():raise ValueError('An update is already pending.')
        if not Path('/etc/systemd/system/pm-web-update.path').exists():raise ValueError('Install this release once using sudo bash update.sh to enable updates.')
        if any(j['status'] in ('staging','awaiting_start','printing','paused','needs_review') for j in self.dashboard.store.jobs()):
            raise ValueError('Finish or resolve active queue jobs before updating.')
        for name in self.core.names():
            state,_,_,connected=self.core.state_data(name)
            if state in ('RUNNING','PAUSE','PREPARE'):raise ValueError('Wait until printers finish before updating.')
            if automatic and not self.core.EXAMPLE_MODE:
                if not connected or state not in ('IDLE','FINISH','FAILED') or time.time()-self.core.last_seen.get(name,0)>90:
                    raise ValueError('Automatic updates require fresh, connected, idle printer reports.')

    def queue_package(self,token,digest,automatic=False):
        self.idle_check(automatic)
        request_data={'id':token,'sha256':digest}
        temp=self.inbox/'request.tmp';temp.write_text(json.dumps(request_data));os.replace(temp,self.inbox/'request.json')
        self.pending=token;self.queued_at=time.time()
