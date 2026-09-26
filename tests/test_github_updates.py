import asyncio,hashlib,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from aiohttp import web
from Updater.github_updates import GitHubUpdates,semver,release_info,download_host,ASSET
from test_updates import package


def release(version='v1.0.1',size=100):
    return {'tag_name':version,'draft':False,'prerelease':False,'assets':[
        {'name':ASSET,'id':1,'state':'uploaded','size':size},
        {'name':ASSET+'.sha256','id':2,'state':'uploaded','size':93}]}

class GitHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Fake releases are v1.0.x; pin the installed version so real version bumps don't break these tests.
        version_patch=patch('Updater.github_updates.VERSION','1.0.0');version_patch.start();self.addCleanup(version_patch.stop)
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.updater=SimpleNamespace(inbox=root,lock=asyncio.Lock(),busy=lambda:False,idle_check=Mock(),queue_package=Mock())
        self.gh=GitHubUpdates(SimpleNamespace(app=web.Application(),core=SimpleNamespace(DATA_DIR=root),updater=self.updater))
        self.gh.config={'repository':'test/printers','token':'secret-token','automatic':True}
    async def asyncTearDown(self):await self.gh.close(None);self.tmp.cleanup()
    def payload(self,version='v1.0.1'):
        return package(change=lambda manifest,files:manifest.update(version=version))
    def setup_fetch(self,payload,checksum=None):
        checksum=checksum or (hashlib.sha256(payload).hexdigest()+'  '+ASSET+'\n').encode()
        self.gh.fetch=AsyncMock(side_effect=[json.dumps(release(size=len(payload))).encode(),checksum,payload])
    def test_version_rules_and_redirect_hosts(self):
        self.assertGreater(semver('v1.10.0'),semver('1.2.0'))
        for value in ['v1.0.1-rc1','main','v1.0','v01.2.3','1.0.0;rm']:
            with self.assertRaises(ValueError):semver(value)
        self.assertTrue(download_host('https://release-assets.githubusercontent.com/a?signature=secret'))
        for url in ['http://github.com/a','https://github.com.evil.test/a','https://user:secret@github.com/a','https://127.0.0.1/a']:
            self.assertFalse(download_host(url))
    def test_drafts_and_missing_assets_rejected(self):
        for field in ['draft','prerelease']:
            data=release();data[field]=True
            with self.assertRaises(ValueError):release_info(data,'test/printers')
        data=release();data['assets'].pop()
        with self.assertRaises(ValueError):release_info(data,'test/printers')
    async def test_verified_install_uses_existing_worker_and_persists_attempt(self):
        payload=self.payload();self.setup_fetch(payload)
        await self.gh.install(automatic=True)
        self.updater.queue_package.assert_called_once();args=self.updater.queue_package.call_args.args
        self.assertEqual((self.updater.inbox/(args[0]+'.zip')).read_bytes(),payload)
        self.assertEqual(args[1],hashlib.sha256(payload).hexdigest());self.assertTrue(args[2])
        self.assertEqual(json.loads(self.gh.path.read_text())['attempted'],'1.0.1')
        self.assertEqual(self.gh.path.stat().st_mode&0o777,0o600)
        self.assertNotIn('secret-token',json.dumps(self.gh.public()))
        self.assertEqual(self.updater.idle_check.call_count,2)
    async def test_checksum_mismatch_never_queues(self):
        self.setup_fetch(self.payload(),('0'*64+'  '+ASSET).encode())
        with self.assertRaisesRegex(ValueError,'checksum'):await self.gh.install()
        self.updater.queue_package.assert_not_called()
    async def test_manifest_version_mismatch_never_queues(self):
        self.setup_fetch(self.payload('v1.0.2'))
        with self.assertRaisesRegex(ValueError,'do not match'):await self.gh.install()
        self.updater.queue_package.assert_not_called();self.assertFalse(list(self.updater.inbox.glob('*.zip')))
    async def test_failed_release_is_not_automatically_retried(self):
        self.gh.config['attempted']='1.0.1';self.gh.fetch=AsyncMock(return_value=json.dumps(release()).encode())
        await self.gh.install(automatic=True)
        self.assertEqual(self.gh.fetch.await_count,1);self.updater.queue_package.assert_not_called()
    async def test_manual_retry_allowed_but_new_latest_requires_review(self):
        self.gh.config['attempted']='1.0.1';self.setup_fetch(self.payload())
        await self.gh.install(expected='1.0.1');self.updater.queue_package.assert_called_once()
        self.setup_fetch(self.payload())
        with self.assertRaisesRegex(ValueError,'changed'):await self.gh.install(expected='1.0.2')
    async def test_printer_starts_during_download(self):
        self.setup_fetch(self.payload());self.updater.idle_check.side_effect=[None,ValueError('Printer busy')]
        with self.assertRaisesRegex(ValueError,'busy'):await self.gh.install(automatic=True)
        self.updater.queue_package.assert_not_called();self.assertNotIn('attempted',self.gh.config)
    async def test_settings_confirmation_and_token_retarget(self):
        request=SimpleNamespace(json=AsyncMock(return_value={'repository':'new/repo','automatic':True,'confirmed':True}))
        with self.assertRaisesRegex(ValueError,'token'):await self.gh.settings(request)
        request.json.return_value={'repository':'test/printers','automatic':True}
        with self.assertRaisesRegex(ValueError,'Confirm'):await self.gh.settings(request)
        request.json.return_value={'repository':'test/printers','automatic':False,'clear_token':True}
        await self.gh.settings(request);self.assertFalse(self.gh.config['token'])
    async def test_private_asset_redirect_strips_authorization(self):
        calls=[]
        class Content:
            async def iter_chunked(self,n):yield b'zip'
        class Response:
            def __init__(self,n):self.status=302 if n==1 else 200;self.headers={'Location':'https://release-assets.githubusercontent.com/file'};self.content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        class Session:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def get(self,url,**kwargs):calls.append((url,kwargs));return Response(len(calls))
        with patch('Updater.github_updates.aiohttp.ClientSession',Session):
            data=await self.gh.fetch('https://api.github.com/repos/test/printers/releases/assets/1',100,True)
        self.assertEqual(data,b'zip');self.assertIn('Authorization',calls[0][1]['headers']);self.assertNotIn('Authorization',calls[1][1]['headers'])

class AutomaticIdleTests(unittest.TestCase):
    def test_offline_stale_and_busy_printers_block_auto(self):
        from Updater.web_updates import WebUpdates
        with tempfile.TemporaryDirectory() as root:
            core=SimpleNamespace(DATA_DIR=Path(root),EXAMPLE_MODE=False,names=lambda:['A'],last_seen={'A':time.time()},state_data=lambda n:('IDLE',0,{},True))
            dashboard=SimpleNamespace(core=core,store=SimpleNamespace(jobs=lambda:[]),app=web.Application())
            updater=WebUpdates(dashboard);updater.busy=lambda:False
            with patch('Updater.web_updates.Path.exists',return_value=True):
                updater.idle_check(True)
                core.last_seen['A']=0
                with self.assertRaises(ValueError):updater.idle_check(True)
                core.last_seen['A']=time.time();core.state_data=lambda n:('IDLE',0,{},False)
                with self.assertRaises(ValueError):updater.idle_check(True)
                core.state_data=lambda n:('RUNNING',0,{},True)
                with self.assertRaises(ValueError):updater.idle_check(True)
