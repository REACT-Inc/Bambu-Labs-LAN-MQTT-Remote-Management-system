import hashlib,io,json,logging,os,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from Updater.update_package import inspect_package,REQUIRED
from Updater import update_worker as worker


def package(extra=None,change=None):
    files={name:(b'# valid\n' if name.endswith('.py') else b'') for name in REQUIRED}
    files['requirements.txt']=b'aiohttp>=3.10,<4\n'
    manifest={'format':1,'application':'3d-printer-management','version':'test.1','files':{n:hashlib.sha256(d).hexdigest() for n,d in files.items()}}
    if change:change(manifest,files)
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        for name,data in files.items():z.writestr('printer-management/'+name,data)
        z.writestr('printer-management/update-manifest.json',json.dumps(manifest))
        if extra:z.writestr(*extra)
    return out.getvalue()


class PackageTests(unittest.TestCase):
    def test_extract_only_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            result=inspect_package(io.BytesIO(package(('printer-management/install.sh','touch /bad'))),Path(d))
            self.assertEqual(result['version'],'test.1');self.assertFalse((Path(d)/'install.sh').exists())
    def test_traversal_and_links(self):
        for name in ['printer-management/../bad.py','/bad.py','printer-management/./bad.py','printer-management\\bad.py']:
            with self.subTest(name=name),self.assertRaises(ValueError):inspect_package(io.BytesIO(package((name,'bad'))))
        link=zipfile.ZipInfo('printer-management/link');link.create_system=3;link.external_attr=0o120777<<16
        with self.assertRaises(ValueError):inspect_package(io.BytesIO(package((link,'/etc/passwd'))))
    def test_manifest_corruption_and_invalid_python(self):
        def corrupt(m,f):f['main.py']=b'invalid code!'
        def bad_python(m,f):
            corrupt(m,f);m['files']['main.py']=hashlib.sha256(f['main.py']).hexdigest()
        def missing(m,f):del m['files']['main.py']
        def wrong(m,f):m['application']='other'
        def unsafe_requirement(m,f):
            f['requirements.txt']=b'--index-url https://evil.test\n';m['files']['requirements.txt']=hashlib.sha256(f['requirements.txt']).hexdigest()
        for change in [corrupt,bad_python,missing,wrong,unsafe_requirement]:
            with self.subTest(change=change),self.assertRaises(ValueError):inspect_package(io.BytesIO(package(change=change)))
        with self.assertRaises(ValueError):inspect_package(io.BytesIO(b'not a zip'))
    def test_size_limit(self):
        with patch('Updater.update_package.MAX_EXPANDED',1),self.assertRaises(ValueError):inspect_package(io.BytesIO(package()))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for name in ['app','data','config','state','releases']:(self.root/name).mkdir()
        self.patches=[patch.object(worker,k,self.root/v) for k,v in [('APP','app'),('DATA','data'),('CONFIG','config'),('STATE','state'),('RELEASES','releases')]]
        # The worker runs as root in production and hands files to root; CI runs unprivileged, so skip ownership changes there.
        if os.geteuid()!=0:self.patches.append(patch.object(worker.os,'chown'))
        for p in self.patches:p.start()
        (worker.STATE/'backups').mkdir();(worker.DATA/'updates').mkdir();(worker.DATA/'uploads').mkdir()
        (worker.DATA/'uploads/part.3mf').write_text('keep');(worker.DATA/'settings.json').write_text('original')
        (worker.CONFIG/'config.json').write_text('{"port":8080}');(worker.APP/'main.py').write_text('old code')
        self.user=patch.object(worker.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid()));self.user.start()
    def tearDown(self):
        self.user.stop()
        for p in self.patches:p.stop()
        self.tmp.cleanup()
    def test_request_snapshot_and_symlink_rejection(self):
        identity='a'*32;data=package();inbox=worker.DATA/'updates'
        (inbox/(identity+'.zip')).write_bytes(data)
        def request(): (inbox/'request.json').write_text(json.dumps({'id':identity,'sha256':hashlib.sha256(data).hexdigest()}))
        request();self.assertEqual(worker.take_request(),(identity,data));self.assertFalse((inbox/'request.json').exists())
        (inbox/(identity+'.zip')).unlink();(inbox/(identity+'.zip')).symlink_to(worker.CONFIG/'config.json');request()
        with self.assertRaises(OSError):worker.take_request()
        self.assertFalse((inbox/'request.json').exists())
    def test_success_preserves_data_and_uses_unprivileged_pip(self):
        with patch.object(worker,'run') as run,patch.object(worker,'healthy',return_value=True),patch.object(worker.shutil,'disk_usage',return_value=SimpleNamespace(free=2*1024**3)):
            worker.install('b'*32,package())
        self.assertTrue(worker.APP.is_symlink());self.assertEqual((worker.DATA/'settings.json').read_text(),'original')
        self.assertEqual((worker.DATA/'uploads/part.3mf').read_text(),'keep')
        self.assertEqual(json.loads((worker.STATE/'status.json').read_text())['state'],'succeeded')
        calls=[c.args[0] for c in run.call_args_list];pip=next(c for c in calls if 'pip' in c)
        self.assertEqual(pip[:4],['/usr/sbin/runuser','-u','printermanager','--'])
        self.assertEqual((worker.APP/'main.py').stat().st_mode&0o777,0o644)
    def test_failed_health_restores_code_and_database(self):
        def failure(identity):
            (worker.DATA/'settings.json').write_text('new schema');return False
        with patch.object(worker,'run'),patch.object(worker,'healthy',side_effect=failure),patch.object(worker.shutil,'disk_usage',return_value=SimpleNamespace(free=2*1024**3)):
            worker.install('c'*32,package())
        self.assertEqual((worker.APP/'main.py').read_text(),'old code');self.assertEqual((worker.DATA/'settings.json').read_text(),'original')
        self.assertEqual(json.loads((worker.STATE/'status.json').read_text())['state'],'rolled_back')
        self.assertFalse((worker.STATE/'journal.json').exists())
    def test_dependency_failure_never_stops_app(self):
        def fail(args,timeout=120):
            if 'pip' in args:raise RuntimeError('network unavailable')
        with patch.object(worker,'run',side_effect=fail),patch.object(worker,'service') as service,patch.object(worker.shutil,'disk_usage',return_value=SimpleNamespace(free=2*1024**3)),self.assertRaises(RuntimeError):worker.install('d'*32,package())
        service.assert_not_called();self.assertFalse(worker.APP.is_symlink())
    def test_interrupted_switch_recovery(self):
        backup=worker.STATE/'backups/recovery';backup.mkdir();worker.copy_state(backup)
        previous=worker.RELEASES/'old';worker.APP.rename(previous)
        new=worker.RELEASES/'new';new.mkdir();worker.pointer(new)
        (worker.DATA/'settings.json').write_text('changed')
        with patch.object(worker,'service'):worker.rollback({'id':'e'*32,'previous':str(previous),'backup':str(backup)})
        self.assertEqual((worker.APP/'main.py').read_text(),'old code');self.assertEqual((worker.DATA/'settings.json').read_text(),'original')


class UpdateHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,password_hash
        from queueing import Store,Engine
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name);self.store=Store(d/'db')
        self.core=SimpleNamespace(DATA_DIR=d,CONFIG={},EXAMPLE_MODE=True,log=logging.getLogger('test'),names=lambda:['Test'],state_data=lambda n:('IDLE',0,{},True))
        self.dashboard=Dashboard(self.core,self.store,Engine(self.core,self.store))
        (d/'auth.json').write_text(json.dumps(password_hash('test-password-123')))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        self.statuspatch=patch('Updater.web_updates.STATUS',d/'updater-status.json');self.statuspatch.start()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close();self.statuspatch.stop();self.tmp.cleanup()
    async def login(self):
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        return {'X-PM':'1','X-CSRF':(await r.json())['csrf']}
    async def upload(self,headers):
        from aiohttp import FormData
        form=FormData();form.add_field('file',package(),filename='release.zip',content_type='application/zip')
        return await self.client.post('/api/update/upload',data=form,headers=headers)
    async def test_auth_csrf_confirmation_and_maintenance(self):
        self.assertEqual((await self.client.get('/api/update/status')).status,401)
        h=await self.login()
        self.assertEqual((await self.upload({'X-PM':'1'})).status,403)
        r=await self.upload(h);self.assertEqual(r.status,200);preview=await r.json()
        self.assertEqual((await self.client.post('/api/update/install',json={'token':preview['token']},headers=h)).status,400)
        original=Path.exists
        def exists(p):return True if str(p)=='/etc/systemd/system/pm-web-update.path' else original(p)
        with patch.object(Path,'exists',exists):
            r=await self.client.post('/api/update/install',json={'token':preview['token'],'confirmed':True},headers=h)
        self.assertEqual(r.status,200);self.assertTrue(self.core.update_pending())
        self.assertEqual((await self.client.post('/api/testnotification',json={},headers=h)).status,503)
        self.assertEqual((await self.client.get('/health')).status,200)
    async def test_cached_snapshots_keep_their_caching_everything_else_no_store(self):
        await self.login()
        self.dashboard.snapshots.images['Test']=(b'\xff\xd8x\xff\xd9',123.0)
        r=await self.client.get('/api/snapshot/Test?t=123.0')
        self.assertEqual((r.status,r.headers['Cache-Control']),(200,'private, max-age=60'))
        self.assertEqual((await self.client.get('/api/state')).headers['Cache-Control'],'no-store')
        self.assertEqual((await self.client.get('/api/snapshot/Test')).headers['X-Content-Type-Options'],'nosniff')

    async def test_running_printer_blocks_update(self):
        h=await self.login();preview=await (await self.upload(h)).json();self.core.state_data=lambda n:('RUNNING',0,{},True)
        original=Path.exists
        with patch.object(Path,'exists',lambda p:True if str(p)=='/etc/systemd/system/pm-web-update.path' else original(p)):
            r=await self.client.post('/api/update/install',json={'token':preview['token'],'confirmed':True},headers=h)
        self.assertEqual(r.status,400);self.assertIn('Test is printing',(await r.json())['error']);self.assertFalse(self.core.update_pending())
        status=await (await self.client.get('/api/update/status')).json()
        self.assertEqual(status['blockers'],['Test is printing'])
        with patch.object(Path,'exists',lambda p:True if str(p)=='/etc/systemd/system/pm-web-update.path' else original(p)):
            r=await self.client.post('/api/update/install',json={'token':preview['token'],'confirmed':True,'force':True},headers=h)
        self.assertEqual(r.status,200);self.assertTrue(self.core.update_pending())
        forced=[e for e in self.store.events() if e['title']=='Forced software update']
        self.assertEqual(len(forced),1);self.assertIn('while Test is printing',forced[0]['detail']);self.assertIn('dashboard (',forced[0]['detail'])
    async def test_github_settings_require_auth_csrf_and_hide_token(self):
        self.assertEqual((await self.client.get('/api/github/status')).status,401)
        headers=await self.login()
        data={'repository':'test/printers','automatic':False,'token':'test-token-value'}
        self.assertEqual((await self.client.post('/api/github/settings',json=data,headers={'X-PM':'1'})).status,403)
        response=await self.client.post('/api/github/settings',json=data,headers=headers)
        self.assertEqual(response.status,200);self.assertNotIn('test-token-value',await response.text())
        response=await self.client.get('/api/github/status')
        result=await response.json();self.assertTrue(result['has_token']);self.assertEqual(result['repository'],'test/printers')
        self.assertNotIn('token',result)
        response=await self.client.post('/api/github/install',json={},headers=headers)
        self.assertEqual(response.status,400)
