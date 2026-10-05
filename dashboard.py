import asyncio
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from aiohttp import web
from Updater.web_updates import WebUpdates
from Updater.github_updates import GitHubUpdates
from printer_errors import describe as describe_error
from printer_files import Browser, render as render_files
from printer_controls import Controls, limits
import job_transfer
import sliced_file
import filament_sides
from live_camera import Cameras
from camera_snapshots import SnapshotRotation
from failureDetection.detection import FailureMonitor
from object_skip import ObjectSkip
from queueing import MAX_UPLOAD, options, validate_archive
import diagnostics
from issue_reports import IssueReports


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {'salt':salt, 'hash':digest}


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


class Dashboard:
    def __init__(self, core, store, engine):
        self.core, self.store, self.engine = core, store, engine
        self.sessions, self.failures = {}, {}
        self.file_browser = Browser(core)
        self.controls = Controls(core,store)
        self.objects = ObjectSkip(core,store,self.controls)   # cancel single objects mid-print
        self.cameras = Cameras(core)
        core.live_cameras = self.cameras
        self.snapshots = SnapshotRotation(core, self.cameras)
        core.snapshot_rotation = self.snapshots   # Discord /printer reuses its recent stills (#54)
        self.auth_file = core.DATA_DIR/'auth.json'
        self.uploads = core.DATA_DIR/'uploads'
        self.uploads.mkdir(exist_ok=True)
        self.app = web.Application(middlewares=[self.guard], client_max_size=MAX_UPLOAD+1024*1024)
        self.app['dashboard']=self
        self.updater = WebUpdates(self)
        self.github_updater = GitHubUpdates(self)
        self.issue_reports = IssueReports(core, store, self.app)
        try:self.release=json.loads((Path(__file__).parent/'release.json').read_text()).get('id','')
        except (OSError,ValueError):self.release=''
        self.app.on_shutdown.append(self.cameras.close)
        self.app.on_startup.append(self.snapshots.start);self.app.on_shutdown.append(self.snapshots.stop)
        self.failure = FailureMonitor(core, engine)
        core.failure_monitor = self.failure   # Discord /printer shows the AI watch line (#70)
        self.app.on_startup.append(self.failure.start);self.app.on_shutdown.append(self.failure.stop)
        self.app.add_routes([
            web.get('/api/liveframe/{name}',self.cameras.frame_response),
            web.get('/api/snapshot/{name}',self.snapshots.response),
            web.get('/api/live/{name}',self.cameras.stream),
            web.post('/api/plateswap/{name}/{action}',self.plate_swap),
            web.get('/api/objects/{name}',self.list_objects),web.post('/api/objects/{name}',self.skip_objects),
            web.get('/api/objects/{name}/plate.png',self.objects_picture),
            web.post('/api/ai/{name}',self.ai_watch),
            web.get('/', self.index), web.get('/assets/{name}', self.asset),
            web.get('/health', self.health), web.get('/api/files/{name}', self.files),
            web.post('/api/login', self.login), web.post('/api/logout', self.logout),
            web.get('/api/state', self.state), web.post('/api/upload', self.upload),
            web.post('/api/jobs', self.add_job), web.post('/api/jobs/{id}/{action}', self.job_action),
            web.get('/api/jobs/{id}/targets', self.job_targets),
            web.get('/api/uploads/{asset}/plate/{index}', self.upload_thumbnail),
            web.get('/api/uploads/{asset}/suggest', self.upload_suggest),
            web.post('/api/printers/{name}/{action}', self.control),
            web.get('/api/camera/{name}', self.camera), web.post('/api/settings', self.settings),
            web.get('/api/permissions', self.permissions), web.post('/api/permissions', self.save_permissions),
            web.get('/api/diagnostics', self.diagnostic_report), web.get('/api/errors', self.recent_errors),
            web.post('/api/password', self.change_password), web.post('/api/testnotification', self.test_notification),
        ])

    @web.middleware
    async def guard(self, request, handler):
        try:
            if request.method != 'GET':
                origin = request.headers.get('Origin')
                if request.headers.get('X-PM') != '1' or (origin and origin != f'{request.scheme}://{request.host}'):
                    raise web.HTTPForbidden(text='Invalid request origin.')
            if request.path.startswith('/api/') and request.path != '/api/login':
                token = request.cookies.get('pm_session','')
                session = self.sessions.get(token)
                if not session or session['expires'] < time.time():
                    self.sessions.pop(token,None)
                    raise web.HTTPUnauthorized()
                request['session'] = session
                if request.method != 'GET' and not hmac.compare_digest(request.headers.get('X-CSRF',''),session['csrf']):
                    raise web.HTTPForbidden(text='Invalid session token.')
            if request.method != 'GET' and request.path not in ('/api/login','/api/logout') and not request.path.startswith('/api/update/') and self.updater.busy():
                raise web.HTTPServiceUnavailable(text='Management update in progress. Try again after it finishes.')
            response = await handler(request)
        except (ValueError, KeyError, TypeError) as exc:
            response = web.json_response({'error':str(exc)}, status=400)
        except web.HTTPException as exc:
            response = web.json_response({'error':exc.text or exc.reason}, status=exc.status)
        except Exception as exc:
            error_id = diagnostics.log_error(self.core.log, f'Dashboard request {request.method} {request.path} failed', exc)
            response = web.json_response({'error':f'Request failed (error ID {error_id}). Download a diagnostic report under Settings.'}, status=500)
        # Default to no-store, but keep a handler's own caching (cached camera stills are reloaded by the cards every 5 s).
        response.headers.setdefault('Cache-Control','no-store')
        response.headers.update({'X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'same-origin', 'Content-Security-Policy':"default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    async def ai_watch(self, request):
        name=request.match_info['name']
        if not self.failure.enabled:raise ValueError('AI failure detection is not enabled in config.json.')
        data=await request.json()
        self.failure.set_watching(name,bool(data.get('watch')))
        return web.json_response(self.failure.state(name))

    async def list_objects(self, request):
        name=request.match_info['name']
        if name not in self.core.names():raise ValueError('Unknown printer.')
        # The queue database is only used from this thread; reading slice_info.config is small (capped at 4 MB).
        result=self.objects.objects(name)
        job=self.objects.job(name)
        result['picture']=bool(job and sliced_file.plate_thumbnail(job['asset'],job['options'].get('plate',1)))
        return web.json_response(result)

    async def skip_objects(self, request):
        name=request.match_info['name']
        if name not in self.core.names():raise ValueError('Unknown printer.')
        data=await request.json()
        message=self.objects.skip(name,data.get('ids'),data.get('confirmed'),'web administrator')
        return web.json_response({'message':message})

    async def objects_picture(self, request):
        name=request.match_info['name']
        job=self.objects.job(name) if name in self.core.names() else None
        picture=job and await asyncio.to_thread(sliced_file.plate_thumbnail,job['asset'],job['options'].get('plate',1))
        if not picture:raise web.HTTPNotFound()
        return web.Response(body=picture,content_type='image/png')

    async def files(self, request):
        name=request.match_info['name']
        if name not in self.core.names():raise ValueError('Unknown printer.')
        result=await self.file_browser.browse(name,request.query.get('path','/'))
        return web.json_response(dict(result,text=render_files(result,request.query.get('printable')=='1')))

    async def diagnostic_report(self, request):
        filename, data = await asyncio.to_thread(diagnostics.build_report, self.core, self.store)
        return web.Response(body=data, content_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    async def recent_errors(self, request):
        return web.json_response({'errors': [dict(e, text=e['text'][:600]) for e in reversed(diagnostics.snapshot_errors())][:20]})

    async def health(self, request):
        return web.json_response({'ok':True, 'application':'3d-printer-management','release':self.release})

    async def index(self, request):
        return web.FileResponse(Path(__file__).parent/'static/index.html')

    async def asset(self, request):
        name=request.match_info['name']
        if name not in ('app.js','team.js','controls.js','updates.js','style.css'): raise web.HTTPNotFound()
        return web.FileResponse(Path(__file__).parent/'static'/name)

    async def login(self, request):
        peer=request.remote or '?'
        recent=[t for t in self.failures.get(peer,[]) if t>time.time()-300]
        if len(recent)>=8: raise web.HTTPTooManyRequests(text='Too many attempts. Wait five minutes.')
        data=await request.json()
        password=str(data.get('password',''))
        if not 1<=len(password)<=1024: raise ValueError('Invalid password.')
        auth=json.loads(self.auth_file.read_text())
        result=await asyncio.to_thread(password_hash,password,auth['salt'])
        if not hmac.compare_digest(result['hash'],auth['hash']):
            if len(self.failures)>1000: self.failures.clear()
            self.failures[peer]=recent+[time.time()]
            raise web.HTTPUnauthorized(text='Incorrect password.')
        self.failures.pop(peer,None)
        self.sessions={k:v for k,v in self.sessions.items() if v['expires']>time.time()}
        token, csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        self.sessions[token]={'csrf':csrf,'expires':time.time()+12*3600}
        response=web.json_response({'csrf':csrf})
        response.set_cookie('pm_session',token,httponly=True,samesite='Strict',secure=request.secure,max_age=43200,path='/')
        return response

    async def logout(self, request):
        self.sessions.pop(request.cookies.get('pm_session'),None)
        response=web.json_response({'ok':True});response.del_cookie('pm_session');return response

    async def state(self, request):
        self.snapshots.touch()
        printers=[]
        for name in self.core.names():
            state,error,data,connected=self.core.state_data(name)
            printers.append(dict(ai=self.failure.state(name),plate_swap=self.engine.plate_swap.state(name),limits=limits(self.core,name),camera=self.cameras.state(name),has_camera=self.snapshots.has_camera(name),snapshot=self.snapshots.state(name),name=name,display_name=self.core.display_name(name) if hasattr(self.core,"display_name") else name,state=state,error=error,error_text=(self.core.printer_error_text(name,error,data) if hasattr(self.core,"printer_error_text") else describe_error(error,data)) if error or data.get("hms") else "",connected=connected,data=data,sides=filament_sides.summary(data),last_seen=self.core.last_seen.get(name)))
        jobs=self.store.jobs()
        for j in jobs: j['has_file']=bool(j.pop('asset',None))   # the path stays on the server; the UI only needs to know (#57)
        return web.json_response(dict(title='3D Printer Management', demo=self.core.EXAMPLE_MODE,
            discord=self.core.bot.is_ready(), printers=printers,jobs=jobs,events=self.store.events(),
            settings={**self.core.settings, 'notification_channel_id':str(self.core.settings.get('notification_channel_id') or ''), 'commands_channel_id':str(self.core.settings.get('commands_channel_id') or ''), 'admin_user_ids':[str(x) for x in sorted(self.core.SETTINGS_USER_IDS)]},
            csrf=request['session']['csrf']))

    async def upload(self, request):
        if __import__('shutil').disk_usage(self.uploads).free < MAX_UPLOAD*2:
            raise ValueError('Less than 512 MiB free disk space. Free space before uploading.')
        reader=await request.multipart();part=await reader.next()
        if not part or part.name!='file' or not (part.filename or '').lower().endswith('.3mf'):
            raise ValueError('Select a sliced .3mf file.')
        token=secrets.token_hex(16);path=self.uploads/(token+'.3mf');total=0
        try:
            with path.open('xb') as stream:
                while chunk:=await part.read_chunk(65536):
                    total+=len(chunk)
                    if total>MAX_UPLOAD: raise ValueError('Maximum upload is 256 MiB.')
                    stream.write(chunk)
            # Read the plates now, so the dialog can offer a plate picker and catch an unsliced file straight away (#7).
            info=await asyncio.to_thread(sliced_file.inspect,path)
            if not info['sliced']:
                raise ValueError("This .3mf isn't sliced: it has no plate G-code. In Bambu Studio, slice it and use "
                                 "File → Export → Export plate sliced file (or Export all sliced file), then upload that.")
            return web.json_response({'asset':token,'filename':Path(part.filename).name,'plates':info['plates'],
                'model':job_transfer.label(info['model']) if info['model'] else '','model_key':info['model']})
        except BaseException:
            path.unlink(missing_ok=True);raise

    def upload_path(self,asset):
        if not __import__('re').fullmatch('[0-9a-f]{32}',str(asset)): raise ValueError('Invalid upload.')
        path=self.uploads/(asset+'.3mf')
        if not path.is_file(): raise web.HTTPNotFound(text='Upload not found.')
        return path

    async def upload_thumbnail(self,request):
        path=self.upload_path(request.match_info['asset'])
        image=await asyncio.to_thread(sliced_file.plate_thumbnail,path,int(request.match_info['index']))
        if not image: raise web.HTTPNotFound(text='No thumbnail for that plate.')
        return web.Response(body=image,content_type='image/png',headers={'Cache-Control':'private, max-age=3600'})

    async def upload_suggest(self,request):
        """Suggested AMS mapping for one plate of an upload on one printer (#7)."""
        path=self.upload_path(request.match_info['asset']);printer=request.query.get('printer','')
        if printer not in self.core.names(): raise ValueError('Unknown printer.')
        index=int(request.query.get('plate','1'))
        info=await asyncio.to_thread(sliced_file.inspect,path)
        plate=next((p for p in info['plates'] if p['index']==index),None)
        if not plate: raise ValueError('That plate is not in the file.')
        suggestion=sliced_file.suggest_mapping(plate,self.core.state_data(printer)[2])
        printer_key=job_transfer.printer_model(self.core,printer)
        mismatch=bool(info['model'] and printer_key and info['model']!=printer_key)
        return web.json_response(dict(suggestion,model_warning=f"This file was sliced for the {job_transfer.label(info['model'])}, not the "
            f"{job_transfer.label(printer_key)}. Re-slice it for this printer." if mismatch else ''))

    def add(self, data, author):
        printer=data.get('printer')
        if printer not in self.core.names(): raise ValueError('Unknown printer.')
        opts=options(data.get('plate',1),data.get('use_ams',False),data.get('mapping',''),data.get('bed','textured_plate'))
        asset=data.get('asset');remote=data.get('remote','')
        if bool(asset)==bool(remote): raise ValueError('Choose an uploaded file OR a path on the printer.')
        path=None
        if asset:
            if not __import__('re').fullmatch('[0-9a-f]{32}',str(asset)): raise ValueError('Invalid upload.')
            path=str(self.uploads/(asset+'.3mf'))
            if not Path(path).is_file(): raise ValueError('Upload not found.')
            validate_archive(path,opts['plate'])
            # G-code is model-specific: refuse a file sliced for a different printer model (#7, same check as #57).
            sliced,model=job_transfer.sliced_for(path),job_transfer.printer_model(self.core,printer)
            if sliced and model and sliced!=model:
                raise ValueError(f'This file was sliced for the {job_transfer.label(sliced)}, not the {job_transfer.label(model)}. Re-slice it for {printer}.')
        return self.store.add(printer,str(data.get('label','')),path,remote,opts,author,self.core.EXAMPLE_MODE)

    async def add_job(self,request):
        data=await request.json()
        if data.get('print_now'):
            return web.json_response(await self.print_now(data,'web administrator'))
        job=self.add(data,'web administrator')
        await self.core.notify(job['printer'],'📋 Job queued',job['label'],self.core.BLUE)
        return web.json_response({'id':job['id']})

    async def print_now(self,data,author):
        """Print straight away without waiting in the queue (#7): the job goes to the front and starts now, with the
        usual checks and confirmation. If it can't start, it's cancelled, so nothing is left waiting in the queue."""
        if data.get('confirmed') is not True: raise ValueError('Confirm the plate is clear and the file is sliced for this printer.')
        printer=data.get('printer')
        if printer in self.core.names():self.engine.ready(printer)   # fail fast before anything is added
        job=self.add(data,author)
        try:
            self.store.move_to_front(job['id'])
            await self.engine.start(job['id'],True,author)
        except Exception as exc:
            if self.store.get(job['id'])['status']=='queued':
                self.store.set_status(job['id'],'cancelled',f'Print now could not start: {exc}'[:300])
            raise ValueError(f'Not started: {exc} Nothing was left in the queue.') from exc
        return {'id':job['id'],'started':True}

    def copy_job(self,job_id,author):
        job=self.store.get(job_id)
        return self.store.add(job['printer'],job['label'],job['asset'],job['remote'],{k:v for k,v in job['options'].items() if k!='swap_revision'},author,bool(job['demo']))

    async def job_action(self,request):
        data=await request.json();job_id=request.match_info['id'];action=request.match_info['action']
        if action=='swapapprove':
            self.engine.plate_swap.approve_job(job_id,data.get('confirmed'),'web administrator',data.get('plates'))
        elif action=='start':
            await self.engine.start(job_id,data.get('confirmed'),'web administrator',data.get('override_error',False))
        elif action=='resolve':
            self.engine.resolve(job_id,data.get('outcome'),data.get('confirmed'),'web administrator')
        elif action=='reprint': self.copy_job(job_id,'web administrator')
        elif action=='sendto':
            # Send a finished print to another printer's queue (#57). It still needs the normal start confirmation.
            # A file that's only on the original printer is copied to the Pi first, then uploaded to the new one at start.
            printer,checked=data.get('printer'),data.get('checked')
            job=job_transfer.validate(self.core,self.store,job_id,printer,checked)
            asset=await asyncio.to_thread(job_transfer.fetch,self.core,job,self.uploads) if job_transfer.needs_fetch(self.core,job) else None
            try:copy=job_transfer.send(self.core,self.store,job_id,printer,data.get('use_ams',False),data.get('mapping',''),checked,'web administrator',asset)
            except Exception:
                if asset:Path(asset).unlink(missing_ok=True)
                raise
            return web.json_response({'ok':True,'id':copy['id'],'copied':bool(asset)})
        else: self.store.edit(job_id,action,'web administrator')
        return web.json_response({'ok':True})

    async def job_targets(self,request):
        job=self.store.get(request.match_info['id'])
        sliced,how=await asyncio.to_thread(job_transfer.file_model,self.core,job)
        targets=await asyncio.to_thread(job_transfer.targets,self.core,job)
        return web.json_response({'job':job['id'],'from':job['printer'],'sliced_for':job_transfer.label(sliced) if sliced else '','how':how,
            'remote':not job.get('asset'),'targets':targets})

    async def control(self,request):
        name=request.match_info['name'];action=request.match_info['action'];data=await request.json()
        if name not in self.core.names(): raise ValueError('Unknown printer.')
        if action in ('nozzle','nozzle_left','nozzle_right','bed','chamber','speed','fan','fanall','move','home','filament','nozzle_size') or action.startswith('fan_'):
            message=self.controls.apply(name,action,data.get('value'),data.get('axis'),data.get('confirmed'),data.get('homed'),author='web administrator')
            return web.json_response({'ok':True,'message':message})
        if action=='stop' and data.get('confirmed') is not True: raise ValueError('Confirm stopping the print.')
        await self.engine.control(name,action,'web administrator')
        return web.json_response({'ok':True,'message':'Command submitted; wait for printer telemetry.'})

    async def plate_swap(self,request):
        name=request.match_info['name'];action=request.match_info['action'];data=await request.json()
        if name not in self.core.names():raise ValueError('Unknown printer.')
        if action=='configure':
            self.engine.plate_swap.configure(name,data.get('enabled'),data.get('model',''),data.get('spares'),data.get('confirmed'),'web administrator')
        elif action=='check':
            self.engine.ready(name)
            self.engine.plate_swap.verify(name,data.get('confirmed'),'web administrator')
        else:raise ValueError('Unknown plate-swap action.')
        return web.json_response({'ok':True})

    async def camera(self,request):
        name=request.match_info['name']
        if name not in self.core.names(): raise ValueError('Unknown printer.')
        image=await self.core.snapshot(name)
        if not image: raise web.HTTPNotFound(text='Camera unavailable.')
        return web.Response(body=image,content_type='image/jpeg')

    async def settings(self,request):
        data=await request.json();updated=dict(self.core.settings)
        for key in ('notification_channel_id','commands_channel_id'):
            value=data.get(key)
            updated[key]=int(value) if value else None
            if updated[key] is not None and updated[key]<=0: raise ValueError('IDs must be positive.')
        ids=[int(v.strip()) for v in str(data.get('admin_user_ids','')).replace('\n',',').split(',') if v.strip()]
        if any(v<=0 for v in ids): raise ValueError('IDs must be positive.')
        updated['admin_user_ids']=ids
        atomic_json(Path(self.core.SETTINGS_FILE),updated)
        self.core.settings.clear();self.core.settings.update(updated)
        self.core.SETTINGS_USER_IDS=set(ids)
        return web.json_response({'ok':True})

    def permission_state(self):
        """Every top-level Discord command with its default and current permission level."""
        core=self.core;rows=[]
        for command in sorted(core.bot.tree.get_commands(),key=lambda c:c.name):
            names=[c.qualified_name for c in command.walk_commands() if not hasattr(c,'commands')] if hasattr(command,'walk_commands') else [command.name]
            rows.append({'command':command.name,'subcommands':names,'description':command.description,
                'default':core.default_level(command.name),'level':core.command_level(command.name),'locked':command.name in core.LOCKED_COMMANDS})
        return {'commands':rows,'levels':core.LEVEL_LABELS,'member_role_ids':[str(r) for r in core.settings.get('member_role_ids') or []]}

    async def permissions(self,request):
        return web.json_response(self.permission_state())

    async def save_permissions(self,request):
        data=await request.json();core=self.core
        levels=data.get('levels') or {}
        if not isinstance(levels,dict):raise ValueError('Send levels as {command: level}.')
        known={row['command'] for row in self.permission_state()['commands']}
        overrides=dict(core.settings.get('command_permissions') or {})
        for name,level in levels.items():
            if name not in known:raise ValueError(f'Unknown command /{name}.')
            if level not in core.LEVELS:raise ValueError(f'Choose one of: {", ".join(core.LEVELS)}.')
            if name in core.LOCKED_COMMANDS and level in ('everyone','role'):raise ValueError(f'/{name} can only be admins only or off.')
            if level==core.default_level(name):overrides.pop(name,None)
            else:overrides[name]=level
        roles=[int(v.strip()) for v in str(data.get('member_role_ids','')).replace('\n',',').split(',') if v.strip()]
        if any(v<=0 for v in roles):raise ValueError('Role IDs must be positive numbers.')
        before={name:core.command_level(name) for name in known};before_roles=core.settings.get('member_role_ids') or []
        core.save_settings({**core.settings,'command_permissions':overrides,'member_role_ids':roles})
        changes=[f'/{n}: {core.LEVEL_LABELS[before[n]]} → {core.LEVEL_LABELS[core.command_level(n)]}' for n in sorted(known) if before[n]!=core.command_level(n)]
        if sorted(before_roles)!=sorted(roles):changes.append('allowed role IDs: '+(', '.join(map(str,roles)) or 'none'))
        if changes:
            self.store.event(None,'Discord permissions changed','; '.join(changes)[:1800]+' • web administrator')
            core.log.info('Discord permissions changed by web administrator: %s','; '.join(changes))
        return web.json_response(dict(self.permission_state(),changed=changes))

    async def change_password(self,request):
        data=await request.json();password=str(data.get('password',''))
        if not 12<=len(password)<=1024: raise ValueError('Use at least 12 characters.')
        atomic_json(self.auth_file,await asyncio.to_thread(password_hash,password))
        self.sessions.clear()
        return web.json_response({'ok':True})

    async def test_notification(self,request):
        if not self.core.bot.is_ready(): raise ValueError('Discord is not connected.')
        if not self.core.settings.get('notification_channel_id'): raise ValueError('Set a notification channel first.')
        await self.core.notify('SIMULATED PRINTER','🧪 Test notification','3D Printer Management is connected.',self.core.BLUE)
        return web.json_response({'ok':True})
