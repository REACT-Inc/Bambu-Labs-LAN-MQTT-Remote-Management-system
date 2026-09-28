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
# Printer states that mean a print is in progress. Only these (and a file transfer) hold back an update.
PRINTING={'RUNNING','PREPARE','PAUSE'}
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
        return web.json_response(dict(self.read_status(),enabled=Path('/etc/systemd/system/pm-web-update.path').exists(),busy=self.busy(),blockers=self.blockers()))

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
            self.queue_package(token,preview['sha256'],force=data.get('force') is True,who=f'dashboard ({request.remote or "?"})')
            self.previews.clear()
            return web.json_response({'ok':True,'id':token,'message':'Update queued. The dashboard will disconnect during restart.'})

    def blockers(self,automatic=False):
        """What an update would interrupt right now, as short readable reasons. Empty means it's safe.

        Updating restarts this service, not the printers: a printer keeps printing from its own storage.
        So only an actual print, or a file transfer to a printer, holds an update back. Idle, finished,
        failed and offline printers, and queue jobs waiting for a start or a review, don't.
        """
        names=getattr(self.core,'display_name',lambda n:n)
        reasons,connected={},{}
        for name in self.core.names():
            state,_,_,online=self.core.state_data(name);connected[name]=online
            if online and state in PRINTING:
                reasons[name]=f"{names(name)} is {'paused mid-print' if state=='PAUSE' else 'printing'}"
        for job in self.dashboard.store.jobs():
            printer=job['printer']
            if job['status']=='staging':
                reasons.setdefault(printer,f"{names(printer)} is receiving a print file")
            elif automatic and job['status'] in ('printing','paused') and not connected.get(printer,False):
                # An offline printer can't tell us whether it's still printing; only a person can decide.
                reasons.setdefault(printer,f"{names(printer)} is offline with a print in progress")
        return list(reasons.values())

    def idle_check(self,automatic=False,force=False):
        """Refuse an update that would interrupt a print. Returns the blockers a forced update overrode."""
        if self.busy():raise ValueError('An update is already pending.')
        if not Path('/etc/systemd/system/pm-web-update.path').exists():raise ValueError('Install this release once using sudo bash update.sh to enable updates.')
        if force and automatic:raise ValueError('Automatic updates are never forced.')
        reasons=self.blockers(automatic)
        if reasons and not force:
            if automatic:raise ValueError('Automatic update waiting: '+'; '.join(reasons)+'.')
            raise ValueError('Not updating while '+'; '.join(reasons)+'. Wait for it to finish, or use Force update.')
        return reasons

    def queue_package(self,token,digest,automatic=False,force=False,who='dashboard'):
        overridden=self.idle_check(automatic,force)
        request_data={'id':token,'sha256':digest}
        temp=self.inbox/'request.tmp';temp.write_text(json.dumps(request_data));os.replace(temp,self.inbox/'request.json')
        self.pending=token;self.queued_at=time.time()
        if force:
            detail=f"{who} forced the update"+(' while '+'; '.join(overridden) if overridden else ' (nothing was printing)')
            log=getattr(self.core,'log',None)
            if log:log.warning('Forced software update: %s',detail)
            self.dashboard.store.event(None,'Forced software update',detail)
