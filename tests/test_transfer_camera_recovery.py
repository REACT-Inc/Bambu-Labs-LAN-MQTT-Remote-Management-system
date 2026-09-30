import asyncio,ftplib,json,logging,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from queueing import upload
from live_camera import Feed,Cameras,camera_error

class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'print.3mf';self.path.write_bytes(b'file data')
        self.ftp=Mock();self.ftp.storbinary.return_value='226 Transfer complete';self.ftp.size.return_value=9
        self.factory=Mock(return_value=self.ftp)
        self.module=patch.dict(sys.modules,{'bambulabs_api':SimpleNamespace(),'bambulabs_api.ftp_client':SimpleNamespace(ImplicitFTP_TLS=self.factory)})
        self.module.start();self.printer={'name':'BOB (H2D)','ip':'test','access_code':'secret'}
    def tearDown(self):self.module.stop();self.tmp.cleanup()
    def test_h2d_tls_close_and_size_verification(self):
        upload(self.printer,self.path,'print.3mf')
        self.assertTrue(self.factory.call_args.kwargs['unwrap']);self.assertEqual(self.factory.call_args.kwargs['timeout'],60)
        self.ftp.size.assert_called_once_with('print.3mf');self.ftp.close.assert_called_once()
    def test_other_printers_keep_existing_close_behavior(self):
        self.printer['name']='A1 Mini';upload(self.printer,self.path,'print.3mf');self.assertFalse(self.factory.call_args.kwargs['unwrap'])
    def test_h2d_pro_uses_family_default_and_override_is_respected(self):
        self.printer.update(name='New printer',model='H2D Pro')
        upload(self.printer,self.path,'print.3mf')
        self.assertTrue(self.factory.call_args.kwargs['unwrap'])
        self.printer['ftp_tls_unwrap']=False
        upload(self.printer,self.path,'print.3mf')
        self.assertFalse(self.factory.call_args.kwargs['unwrap'])
    def test_426_is_failure_even_if_all_bytes_sent(self):
        def transfer(cmd,stream,**kwargs):
            kwargs['callback'](stream.read());raise ftplib.error_temp('426 Failure reading network stream secret')
        self.ftp.storbinary.side_effect=transfer
        with self.assertRaises(RuntimeError) as error:upload(self.printer,self.path,'print.3mf')
        message=str(error.exception);self.assertIn('9/9 bytes',message);self.assertIn('426',message);self.assertNotIn('secret',message)
        self.ftp.size.assert_not_called();self.ftp.close.assert_called_once()
    def test_size_mismatch_blocks_success(self):
        self.ftp.size.return_value=4
        with self.assertRaisesRegex(RuntimeError,'size did not match'):upload(self.printer,self.path,'print.3mf')

class CameraRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_requests_reuse_camera_connection(self):
        async def idle(feed):await asyncio.Event().wait()
        core=SimpleNamespace(names=lambda:['A'],EXAMPLE_MODE=False,printer_config=lambda n:{'camera_type':'jpeg_tcp'})
        camera=Cameras(core)
        with patch.object(Feed,'run',idle):
            feed=camera.acquire('A');await camera.release('A',feed,linger=1)
            self.assertIn('A',camera.feeds)
            again=camera.acquire('A');self.assertIs(again,feed);self.assertNotIn('A',camera.idle)
            await camera.release('A',feed,linger=1);await camera.close(None)
            self.assertTrue(feed.task.done());self.assertFalse(camera.idle)
    async def test_poll_endpoint_authenticated_and_returns_jpeg(self):
        from dashboard import Dashboard,password_hash
        from queueing import Store,Engine
        from aiohttp.test_utils import TestClient,TestServer
        async def fake(feed):
            await feed.put(b'\xff\xd8fakejpeg\xff\xd9');await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=Store(root/'queue.db');core=SimpleNamespace(DATA_DIR=root,CONFIG={},names=lambda:['A'],EXAMPLE_MODE=False,printer_config=lambda n:{'camera_type':'jpeg_tcp'},log=logging.getLogger('test'))
            dashboard=Dashboard(core,store,Engine(core,store));(root/'auth.json').write_text(json.dumps(password_hash('testing-password')))
            async with TestClient(TestServer(dashboard.app)) as client:
                self.assertEqual((await client.get('/api/liveframe/A')).status,401)
                login=await client.post('/api/login',json={'password':'testing-password'},headers={'X-PM':'1'});self.assertEqual(login.status,200)
                with patch.object(Feed,'run',fake):
                    response=await client.get('/api/liveframe/A');self.assertEqual(response.status,200)
                    self.assertEqual(response.content_type,'image/jpeg');self.assertEqual(await response.read(),b'\xff\xd8fakejpeg\xff\xd9')
                    self.assertEqual(response.headers['X-Camera-Version'],'1');await dashboard.cameras.close(None)
            store.db.close()
    async def test_errors_redact_camera_credentials(self):
        text=camera_error({'access_code':'privatecode'},RuntimeError('rtsps://bblp:privatecode@printer connection refused'))
        self.assertNotIn('privatecode',text);self.assertIn('connection refused',text)
