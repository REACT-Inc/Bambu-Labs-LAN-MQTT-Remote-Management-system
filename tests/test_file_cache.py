"""Faster queueing and reprinting (file_cache.py): the Pi's library of sliced files (a file isn't uploaded twice, and
Recent files queues one without uploading), and files already on a printer that aren't sent to it again."""
import asyncio,ftplib,hashlib,io,json,os,shutil,subprocess,tempfile,time,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

import file_cache
import queueing
from queueing import Engine,Store,options
from tests.test_better_queue import AMS,make_3mf

ROOT=Path(__file__).parents[1]
NODE=shutil.which('node')


def big_3mf(path,gcode):
    """A sliced .3mf whose G-code is large and stored uncompressed, so the change is in the middle of the file."""
    with zipfile.ZipFile(path,'w',zipfile.ZIP_STORED) as z:
        z.writestr('Metadata/plate_1.gcode',gcode)
        z.writestr('Metadata/slice_info.config','<?xml version="1.0"?><config><plate><metadata key="index" value="1"/></plate></config>')
    return path


class FingerprintTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.d=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def test_size_head_and_tail(self):
        small=self.d/'small.bin';small.write_bytes(b'abc')
        self.assertEqual(file_cache.fingerprint(small),hashlib.sha256(b'3\nabc').hexdigest())
        data=bytes(range(256))*1024   # 256 KiB
        big=self.d/'big.bin';big.write_bytes(data)
        self.assertEqual(file_cache.fingerprint(big),hashlib.sha256(b'262144\n'+data[:65536]+data[-65536:]).hexdigest())

    def test_same_size_different_file_never_matches(self):
        one=big_3mf(self.d/'one.3mf','G1 X10\n'*60000);two=big_3mf(self.d/'two.3mf','G1 X20\n'*60000)   # one change in the middle
        self.assertEqual(one.stat().st_size,two.stat().st_size)
        self.assertNotEqual(file_cache.fingerprint(one),file_cache.fingerprint(two))   # the zip's last bytes list each part's CRC-32


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.d=Path(self.tmp.name);self.now=[1000.0]
        self.store=Store(self.d/'db');self.settings={}
        self.cache=file_cache.FileCache(self.store,self.d/'uploads',self.settings,clock=lambda:self.now[0])
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def upload(self,name='part.3mf',**kw):
        path=make_3mf(self.d/'uploads'/'a1b2.3mf',**kw)
        return path,self.cache.remember(path,name,{'plates':[{'index':1,'sliced':True}],'model':'A1 mini'})

    def test_remember_use_and_list(self):
        path,key=self.upload('Bracket.gcode.3mf')
        self.assertEqual(key,file_cache.fingerprint(path))
        self.assertEqual(os.stat(self.cache.path(key)).st_ino,os.stat(path).st_ino)   # a hard link: no second copy
        self.now[0]=2000
        asset,entry=self.cache.use(key,'Bracket again.3mf')
        copy=self.d/'uploads'/f'{asset}.3mf'
        self.assertEqual((copy.read_bytes(),(self.d/'uploads'/f'{asset}.name').read_text()),(path.read_bytes(),'Bracket again.3mf'))
        self.assertEqual(entry['info']['model'],'A1 mini')
        [listed]=self.cache.recent()
        self.assertEqual((listed['name'],listed['uses'],listed['used'],listed['plates']),('Bracket again.3mf',2,2000,[{'index':1,'sliced':True}]))
        path.unlink();copy.unlink()
        self.assertIsNotNone(self.cache.entry(key))   # the library's own link keeps the file
        self.cache.forget(key)
        self.assertEqual((self.cache.recent(),self.cache.path(key).exists()),([],False))
        with self.assertRaisesRegex(ValueError,'upload it again'):self.cache.use(key)
        with self.assertRaises(ValueError):self.cache.path('../etc')

    def test_a_missing_file_is_forgotten(self):
        _,key=self.upload()
        self.cache.path(key).unlink()
        self.assertIsNone(self.cache.entry(key));self.assertEqual(self.cache.recent(),[])

    def test_size_and_age_limits(self):
        keys=[]
        for i in range(3):
            self.now[0]=1000+i
            path=big_3mf(self.d/'uploads'/f'{i}.3mf','G1\n'*(1000+i));keys.append(self.cache.remember(path,f'{i}.3mf',{}))
        size=self.cache.path(keys[0]).stat().st_size
        with patch.object(file_cache,'LIBRARY_BYTES',size*2+10):
            self.cache.prune()
        self.assertEqual([e['name'] for e in self.cache.recent()],['2.3mf','1.3mf'])   # the least recently used went
        self.now[0]+=file_cache.LIBRARY_DAYS*86400+10;self.cache.prune()
        self.assertEqual(self.cache.recent(),[])

    def test_printer_side_record(self):
        self.assertFalse(self.cache.on_printer('Mini','part.gcode.3mf','a'*64,100))
        self.cache.sent('Mini','part.gcode.3mf','a'*64,100)
        self.assertTrue(self.cache.on_printer('Mini','part.gcode.3mf','a'*64,100))
        self.assertFalse(self.cache.on_printer('Mini','part.gcode.3mf','b'*64,100))   # another file under that name
        self.assertFalse(self.cache.on_printer('Mini','part.gcode.3mf','a'*64,101))
        self.assertFalse(self.cache.on_printer('H2D','part.gcode.3mf','a'*64,100))
        self.settings['reuse_printer_files']=False
        self.assertFalse(self.cache.on_printer('Mini','part.gcode.3mf','a'*64,100))


class FakeFTP:
    def __init__(self,remote_size=None,missing=False):self.remote_size,self.missing,self.stored=remote_size,missing,[]
    def connect(self,*a):pass
    def login(self,*a):pass
    def prot_p(self):pass
    def voidcmd(self,*a):pass
    def size(self,name):
        if self.missing and not self.stored:raise ftplib.error_perm('550 not found')
        return self.remote_size if not self.stored else self.stored[-1]
    def storbinary(self,command,stream,blocksize,callback):
        data=stream.read();callback(data);self.stored.append(len(data));return '226 done'
    def close(self):pass


class UploadTests(unittest.TestCase):
    def test_reuse_only_when_the_printer_has_that_size(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'f.3mf';source.write_bytes(b'x'*1000);printer={'ip':'1.2.3.4','access_code':'12345678'}
            for ftp,reuse,expected,stored in ((FakeFTP(1000),True,'reused',[]),(FakeFTP(999),True,'uploaded',[1000]),
                                              (FakeFTP(missing=True),True,'uploaded',[1000]),(FakeFTP(1000),False,'uploaded',[1000])):
                with patch.object(queueing,'ftps_client',return_value=ftp):
                    self.assertEqual(queueing.upload(printer,str(source),'f.gcode.3mf',reuse),expected)
                self.assertEqual(ftp.stored,stored)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    """Starting a job: the file goes to the printer once; a reprint of the same file isn't sent again."""
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        published=self.published=[]
        self.core=SimpleNamespace(EXAMPLE_MODE=False,settings={},DATA_DIR=d,last_seen={'Mini':time.time()},names=lambda:['Mini'],
            state_data=lambda n:('IDLE',0,{},True),printer_config=lambda n:{'ip':'1.2.3.4','access_code':'12345678','serial':'S1'},
            clients={'Mini':SimpleNamespace(publish=lambda topic,payload,qos:published.append(payload) or SimpleNamespace(rc=0))},
            notify=AsyncMock(),BLUE=1,YELLOW=2,RED=3,log=__import__('logging').getLogger('test'))
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store)
        self.engine.files=file_cache.FileCache(self.store,d/'uploads',self.core.settings)
        self.asset=make_3mf(d/'uploads'/'job.3mf')
    async def asyncTearDown(self):
        for t in list(self.engine.tasks):t.cancel()
        self.store.db.close();self.tmp.cleanup()
    async def run_job(self):
        job=self.store.add('Mini','Bracket',str(self.asset),'Bracket.gcode.3mf',options(1),'t')
        job=self.store.claim(job['id'])
        calls=[]
        def fake_upload(printer,source,remote,reuse=False):
            calls.append(reuse);return 'reused' if reuse else 'uploaded'
        with patch.object(queueing,'upload',fake_upload),patch.object(queueing.asyncio,'sleep',AsyncMock()):
            await self.engine.dispatch(job)
        return calls

    async def test_second_print_of_the_same_file_is_not_sent_again(self):
        self.assertEqual(await self.run_job(),[False])   # first time: sent, and remembered
        self.store.db.execute("UPDATE jobs SET status='finished'");self.store.db.commit()
        self.assertEqual(await self.run_job(),[True])    # a reprint: the printer is asked first
        self.assertIn('File already on the printer',[e['title'] for e in self.store.events()])
        self.assertEqual(len(self.published),2)          # both prints were started
        self.store.db.execute("UPDATE jobs SET status='finished'");self.store.db.commit()
        self.core.settings['reuse_printer_files']=False
        self.assertEqual(await self.run_job(),[False])


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        self.tmp=tempfile.TemporaryDirectory();d=self.d=Path(self.tmp.name)
        configs={'Mini':{'model':'A1 mini'}}
        saved={}
        def save_settings(updated):saved.update(updated);self.core.settings.clear();self.core.settings.update(updated)
        self.core=SimpleNamespace(DATA_DIR=d,SETTINGS_FILE=str(d/'settings.json'),settings={},SETTINGS_USER_IDS=set(),EXAMPLE_MODE=True,
            names=lambda:list(configs),printer_config=configs.get,state_data=lambda n:('IDLE',0,dict(AMS),True),last_seen={},save_settings=save_settings,
            bot=SimpleNamespace(is_ready=lambda:False),log=__import__('logging').getLogger('test'),notify=AsyncMock(),BLUE=1,YELLOW=2,GREEN=3,RED=4)
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        state=await (await self.client.get('/api/state')).json()
        self.headers={'X-PM':'1','X-CSRF':state['csrf']}
    async def asyncTearDown(self):
        for t in list(self.engine.tasks):t.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()
    async def upload(self,path):
        from aiohttp import FormData
        form=FormData();form.add_field('file',path.read_bytes(),filename=path.name)
        return await (await self.client.post('/api/upload',data=form,headers=self.headers)).json()

    async def test_the_same_file_is_not_uploaded_twice(self):
        path=make_3mf(self.d/'Bracket.gcode.3mf')
        check=lambda size=None,key=None:self.client.post('/api/upload/check',json={'name':'Bracket copy.gcode.3mf','size':size or path.stat().st_size,
                                                                                 'fingerprint':key or file_cache.fingerprint(path)},headers=self.headers)
        self.assertEqual(await (await check()).json(),{'found':False})
        first=await self.upload(path)
        again=await (await check()).json()
        self.assertTrue(again['reused']);self.assertNotEqual(again['asset'],first['asset'])
        self.assertEqual((again['plates'],again['model'],again['filename']),(first['plates'],first['model'],'Bracket copy.gcode.3mf'))
        self.assertEqual(await (await check(size=path.stat().st_size+1)).json(),{'found':False})
        self.assertEqual((await check(key='nope')).status,400)
        job=await (await self.client.post('/api/jobs',json={'printer':'Mini','label':'Bracket','asset':again['asset'],'plate':1},headers=self.headers)).json()
        self.assertEqual(self.store.get(job['id'])['remote'],'Bracket_copy.gcode.3mf')   # the name it was picked under

    async def test_recent_files(self):
        first=await self.upload(make_3mf(self.d/'Bracket.gcode.3mf'))
        library=await (await self.client.get('/api/library')).json()
        [entry]=library['files'];self.assertTrue(library['reuse_printer_files'])
        self.assertEqual((entry['name'],entry['model'],[p['index'] for p in entry['plates']]),('Bracket.gcode.3mf','A1 mini',[1,2]))
        thumb=await self.client.get(f"/api/library/{entry['fingerprint']}/plate/1");self.assertEqual(thumb.status,200)
        used=await (await self.client.post(f"/api/library/{entry['fingerprint']}/use",json={},headers=self.headers)).json()
        self.assertEqual((used['plates'],used['filename']),(first['plates'],'Bracket.gcode.3mf'))
        self.assertTrue((self.d/'uploads'/f"{used['asset']}.3mf").is_file())
        r=await self.client.post(f"/api/library/{entry['fingerprint']}/delete",json={},headers=self.headers)
        self.assertEqual((await r.json())['files'],[])
        self.assertTrue((self.d/'uploads'/f"{used['asset']}.3mf").is_file())   # the queued copy stays
        self.assertEqual((await self.client.post('/api/library/../use',json={},headers=self.headers)).status,404)
        self.assertEqual((await self.client.post(f"/api/library/{'0'*64}/use",json={},headers=self.headers)).status,400)

    async def test_the_reuse_setting(self):
        r=await self.client.post('/api/files/reuse',json={'on':False},headers=self.headers)
        self.assertEqual((await r.json())['on'],False);self.assertFalse(self.dashboard.file_library.reuse_enabled())
        self.assertEqual((await self.client.post('/api/files/reuse',json={'on':'no'},headers=self.headers)).status,400)
        await self.client.post('/api/files/reuse',json={'on':True},headers=self.headers);self.assertTrue(self.dashboard.file_library.reuse_enabled())


class PageTests(unittest.TestCase):
    def test_the_dialog(self):
        page,app=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text()
        for element in ('jobLibrary','reusePrinterFiles'):self.assertIn(f'id="{element}"',page)
        self.assertIn('<option value="library">',page)
        self.assertIn("api('upload/check',{name:f.name,size:f.size,fingerprint:await fileFingerprint(f)})",app)

    @unittest.skipUnless(NODE,'node is not installed')
    def test_the_browser_fingerprint_matches_the_pis(self):
        """fileFingerprint() and its plain-JavaScript sha256() (used over http://, where crypto.subtle is missing)."""
        app=(ROOT/'static/app.js').read_text()
        end="return H.map(x=>x.toString(16).padStart(8,'0')).join('');}"
        source=app[app.index('async function fileFingerprint('):app.index(end)+len(end)]
        with tempfile.TemporaryDirectory() as d:
            files=[Path(d)/'empty.bin',Path(d)/'small.bin',Path(d)/'big.bin']
            files[0].write_bytes(b'');files[1].write_bytes(b'hello 3mf'*100);files[2].write_bytes(os.urandom(300_000))
            expected=[file_cache.fingerprint(f) for f in files]
            script=source+'\nconst fs=require("fs");(async()=>{const out=[];for(const subtle of [false,true]){'+\
                'globalThis.window=subtle?{crypto:globalThis.crypto}:{};for(const p of '+json.dumps([str(f) for f in files])+'){'+\
                'const b=fs.readFileSync(p);const file=new Blob([b]);out.push(await fileFingerprint(file));}}console.log(JSON.stringify(out));})();'
            run=subprocess.run([NODE,'-'],input=script,capture_output=True,text=True,timeout=60)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout),expected*2)


if __name__=='__main__':
    unittest.main()
