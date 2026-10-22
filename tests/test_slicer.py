"""The Slicer tab and its OrcaSlicer workers (slicer/, docs/slicer.md): STL checks and placement, preset flattening,
the settings schema, the job queue with leases, the dashboard and worker APIs, and a whole job from the Slicer tab
through a worker and back into a printer's print queue.

OrcaSlicer itself isn't run here: FAKE_ORCA below is a stand-in that takes the same command line and writes what
OrcaSlicer 2.4.2 writes (result.3mf with Metadata/plate_1.gcode and slice_info.config, or result.json on an error).
The installer's test slice runs the real OrcaSlicer on each worker. The presets are a small made-up tree shaped like
OrcaSlicer's resources/profiles (vendor index, inherits, OrcaFilamentLibrary)."""
import asyncio,io,json,os,re,shutil,struct,subprocess,sys,tarfile,tempfile,threading,time,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT))
from slicer import installer,jobs as jobs_module,meshes,profiles,settings,worker
from slicer.jobs import Jobs

NODE=shutil.which('node')
MACHINE,PROCESS,FILAMENT='Bambu Lab A1 mini 0.4 nozzle','0.20mm Standard @BBL A1M','Bambu PLA Basic @BBL A1M'
IDENTITY=[1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]


def cube_stl(size=20.0,name='cube'):
    stream=io.BytesIO();meshes.write_binary_stl(worker.test_cube(size),stream,name);return stream.getvalue()


def write_profiles(root):
    """A preset tree shaped like OrcaSlicer's: BBL with a printer, a process and a filament inheriting from common
    presets, one of them in OrcaFilamentLibrary; plus a printer the filament isn't for."""
    root=Path(root)
    def put(vendor,kind,name,data):
        path=root/vendor/kind/(name.replace(' ','_').replace('@','')+'.json');path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(dict(data,name=name)));return dict(name=name,sub_path=f'{kind}/{path.name}')
    bbl=dict(machine_list=[
        put('BBL','machine','fdm_machine_common',dict(instantiation='false',printable_height='250',nozzle_diameter=['0.4'],gcode_flavor='marlin')),
        put('BBL','machine',MACHINE,dict(inherits='fdm_machine_common',instantiation='true',printer_model='Bambu Lab A1 mini',
            printable_area=['0x0','180x0','180x180','0x180'],printable_height='180',default_print_profile=PROCESS,default_filament_profile=[FILAMENT])),
        put('BBL','machine','Bambu Lab X1 Carbon 0.4 nozzle',dict(inherits='fdm_machine_common',instantiation='true',printer_model='Bambu Lab X1 Carbon',
            printable_area=['0x0','256x0','256x256','0x256'],default_print_profile=PROCESS,default_filament_profile=[FILAMENT]))],
      process_list=[
        put('BBL','process','fdm_process_common',dict(instantiation='false',layer_height='0.2',wall_loops='2',sparse_infill_density='15%')),
        put('BBL','process',PROCESS,dict(inherits='fdm_process_common',instantiation='true',compatible_printers=[MACHINE,'Bambu Lab X1 Carbon 0.4 nozzle']))],
      filament_list=[put('BBL','filament',FILAMENT,dict(inherits='fdm_filament_pla',instantiation='true',filament_vendor=['Bambu Lab'],
            compatible_printers=[MACHINE],nozzle_temperature=['220']))])
    (root/'BBL.json').write_text(json.dumps(bbl))
    library=dict(filament_list=[put('OrcaFilamentLibrary','filament','fdm_filament_pla',dict(instantiation='false',filament_type=['PLA'],
            nozzle_temperature=['210'],filament_flow_ratio=['0.98']))])
    (root/'OrcaFilamentLibrary.json').write_text(json.dumps(library))
    return root


# Stands in for OrcaSlicer's command line (see the module docstring). FAKE_ORCA_MODE: ok, error, crash, hang, unsliced.
FAKE_ORCA=r'''
import json, os, sys, time, zipfile
args = sys.argv[1:]
def value(flag): return args[args.index(flag) + 1]
out, mode = value('--outputdir'), os.environ.get('FAKE_ORCA_MODE', 'ok')
stls = [a for a in args if a.endswith('.stl')]
if os.environ.get('FAKE_ORCA_LOG'):
    machine, process = value('--load-settings').split(';')
    with open(os.environ['FAKE_ORCA_LOG'], 'w') as log:
        json.dump(dict(args=args, machine=json.load(open(machine)), process=json.load(open(process)),
                       filament=json.load(open(value('--load-filaments'))), stls=[open(p, 'rb').read().hex() for p in stls]), log)
if mode == 'hang': time.sleep(120)
if mode == 'crash': os.kill(os.getpid(), 9)
if mode == 'error':
    json.dump(dict(return_code=-51, error_string='Some objects are too close'), open(os.path.join(out, 'result.json'), 'w'))
    sys.exit(1)
info = """<?xml version="1.0" encoding="UTF-8"?><config><plate><metadata key="index" value="1"/>
<metadata key="prediction" value="1234"/><metadata key="weight" value="5.61"/>
<object identify_id="1" name="cube"/><filament id="1" type="PLA" color="#FFFFFF" used_m="1.88" used_g="5.61"/>
<warning msg="bridge_without_support" level="1"/></plate></config>"""
with zipfile.ZipFile(os.path.join(out, value('--export-3mf')), 'w') as archive:
    archive.writestr('3D/3dmodel.model', '<model/>')
    if mode != 'unsliced':
        archive.writestr('Metadata/plate_1.gcode', '; sliced\nG28\n')
        archive.writestr('Metadata/slice_info.config', info)
json.dump(dict(return_code=0, error_string=''), open(os.path.join(out, 'result.json'), 'w'))
'''


class MeshTests(unittest.TestCase):
    def test_binary_and_ascii(self):
        data=cube_stl()
        self.assertEqual(meshes.kind(data),'binary')
        self.assertEqual(meshes.summary(data),dict(triangles=12,size=[20.0,20.0,20.0],min=[0.0,0.0,0.0],max=[20.0,20.0,20.0]))
        ascii_stl=b'solid t\nfacet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n vertex 0 2 3.5e0\n endloop\nendfacet\nendsolid t\n'
        self.assertEqual(meshes.kind(ascii_stl),'ascii');self.assertEqual(meshes.summary(ascii_stl)['size'],[1.0,2.0,3.5])
        self.assertFalse(data[:5].lower()==b'solid')   # our binary header never looks like ASCII STL

    def test_bad_files(self):
        for bad in (b'',b'hello world, not an stl',b'\0'*80+struct.pack('<I',0),cube_stl()[:-10]):
            with self.assertRaises(meshes.MeshError):meshes.summary(bad)
        nan=bytearray(cube_stl());struct.pack_into('<f',nan,84+12,float('nan'))
        with self.assertRaises(meshes.MeshError):meshes.summary(bytes(nan))
        with patch.object(meshes,'MAX_TRIANGLES',5),self.assertRaises(meshes.MeshError):meshes.kind(cube_stl())

    def test_matrices(self):
        self.assertEqual(meshes.check_matrix(IDENTITY),[float(v) for v in IDENTITY])
        for bad in (None,[1]*15,['x']+IDENTITY[1:],[float('inf')]+IDENTITY[1:],IDENTITY[:3]+[1]+IDENTITY[4:],[0]*15+[1]):
            with self.assertRaises(meshes.MeshError):meshes.check_matrix(bad)

    def test_transform_moves_rotates_and_keeps_faces_outward(self):
        values=meshes.triangles(cube_stl(10))
        moved=meshes.transform(values,[2,0,0,0, 0,1,0,0, 0,0,1,0, 50,60,0,1])   # x scaled 2, moved to 50,60
        self.assertEqual(meshes.bounds(moved),((50,60,0),(70,70,10)))
        turned=meshes.transform(values,[0,1,0,0, -1,0,0,0, 0,0,1,0, 0,0,0,1])   # 90° about Z
        self.assertEqual([tuple(round(v,4) for v in b) for b in meshes.bounds(turned)],[(-10,0,0),(0,10,10)])
        def normal(v):
            u=[v[3]-v[0],v[4]-v[1],v[5]-v[2]];w=[v[6]-v[0],v[7]-v[1],v[8]-v[2]];return u[0]*w[1]-u[1]*w[0]
        mirrored=meshes.transform(values,[-1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1])
        self.assertLess(normal(values[0:9]),0);self.assertLess(normal(mirrored[0:9]),0)   # the bottom still faces down
        stream=io.BytesIO();meshes.write_binary_stl(moved,stream);self.assertEqual(meshes.summary(stream.getvalue())['min'],[50,60,0])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.library=profiles.Profiles(write_profiles(self.tmp.name))
    def tearDown(self):self.tmp.cleanup()

    def test_flattening_follows_inherits_across_vendors(self):
        flat=self.library.flat('filament',FILAMENT)
        self.assertEqual((flat['filament_type'],flat['nozzle_temperature'],flat['filament_flow_ratio']),(['PLA'],['220'],['0.98']))
        self.assertEqual((flat['inherits'],flat['from'],flat['name'],flat['type']),('','system',FILAMENT,'filament'))
        machine=self.library.flat('machine',MACHINE)
        self.assertEqual((machine['printable_height'],machine['gcode_flavor']),('180','marlin'))

    def test_changes_make_a_user_preset(self):
        process=self.library.resolved('process',PROCESS,{'wall_loops':4,'sparse_infill_density':'25%','layer_height':0.12})
        self.assertEqual((process['wall_loops'],process['sparse_infill_density'],process['layer_height']),('4','25%','0.12'))
        self.assertEqual((process['inherits'],process['name'],process['from']),(PROCESS,PROCESS+' (web)','User'))
        filament=self.library.resolved('filament',FILAMENT,{'nozzle_temperature':230,'enable_x':True})
        self.assertEqual((filament['nozzle_temperature'],filament['enable_x']),(['230'],'1'))   # lists stay lists
        self.assertEqual(self.library.resolved('process',PROCESS)['from'],'system')

    def test_errors(self):
        with self.assertRaises(profiles.ProfileError):self.library.flat('process','nope')
        loop=Path(self.tmp.name)/'BBL'/'process'/'fdm_process_common.json'
        loop.write_text(json.dumps(dict(name='fdm_process_common',inherits=PROCESS)))
        with self.assertRaisesRegex(profiles.ProfileError,'loop'):profiles.Profiles(self.tmp.name).flat('process',PROCESS)
        with self.assertRaises(profiles.ProfileError):profiles.Profiles(Path(self.tmp.name)/'missing')

    def test_catalog(self):
        catalog=self.library.catalog()
        self.assertEqual([m['name'] for m in catalog['machines']],['Bambu Lab A1 mini 0.4 nozzle','Bambu Lab X1 Carbon 0.4 nozzle'])
        mini=catalog['machines'][0]
        self.assertEqual((mini['model'],mini['nozzle'],mini['bed'][2],mini['height'],mini['default_process'],mini['default_filament']),
                         ('Bambu Lab A1 mini','0.4',[180.0,180.0],180.0,PROCESS,FILAMENT))
        self.assertEqual(catalog['processes'],[dict(name=PROCESS,printers=[MACHINE,'Bambu Lab X1 Carbon 0.4 nozzle'],layer_height=0.2)])
        self.assertEqual(catalog['filaments'],[dict(name=FILAMENT,printers=[MACHINE],material='PLA',brand='Bambu Lab')])
        json.dumps(catalog)


class SettingsTests(unittest.TestCase):
    def test_schema(self):
        keys=[s['key'] for s in settings.SCHEMA]
        self.assertEqual(len(keys),len(set(keys)))
        for s in settings.SCHEMA:
            self.assertIn(s['preset'],('process','filament'));self.assertIn(s['type'],('float','int','percent','bool','enum'))
            if s['type']=='enum':self.assertTrue(s['options'])
            elif s['type']!='bool':self.assertLess(s['min'],s['max'])

    def test_check(self):
        self.assertEqual(settings.check(None),{'process':{},'filament':{}})
        out=settings.check({'layer_height':0.16,'wall_loops':3,'sparse_infill_density':20,'enable_support':True,'nozzle_temperature':215,
                            'support_type':'tree(auto)'})
        self.assertEqual(out,{'process':{'layer_height':0.16,'wall_loops':3,'sparse_infill_density':'20%','enable_support':True,'support_type':'tree(auto)'},
                              'filament':{'nozzle_temperature':215}})
        for bad in ({'layer_height':5},{'wall_loops':2.5},{'enable_support':1},{'support_type':'magic'},{'start_gcode':'M112'},
                    {'layer_height':'0.2'},{'wall_loops':True},[1]):
            with self.assertRaises(ValueError,msg=bad):settings.check(bad)


class Clock:
    def __init__(self):self.now=1000.0
    def __call__(self):return self.now


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.clock=Clock();self.jobs=Jobs(Path(self.tmp.name)/'s.sqlite3',self.clock)
        self.request=dict(objects=[dict(model='a'*64,name='cube.stl',matrix=IDENTITY)],settings=dict(machine=MACHINE,process=PROCESS,filament=FILAMENT),changes={})
        code,_=self.jobs.new_enrollment();self.worker,self.secret=self.jobs.enroll(code,'laptop 1',{})
    def tearDown(self):self.jobs.db.close();self.tmp.cleanup()
    def add(self,label='plate'):
        job=self.jobs.add('web administrator',label,self.request);self.clock.now+=1;return job

    def test_enrollment_codes_work_once_and_expire(self):
        self.assertTrue(self.worker.startswith('w-'))
        self.assertEqual(self.jobs.authenticate(self.worker,self.secret)['name'],'laptop 1')
        with self.assertRaises(PermissionError):self.jobs.authenticate(self.worker,'wrong')
        self.assertNotIn(self.secret,json.dumps(self.jobs.workers()))
        stored=self.jobs.db.execute('SELECT token FROM slice_workers').fetchone()[0];self.assertEqual(stored,jobs_module.token_hash(self.secret))
        code,_=self.jobs.new_enrollment();self.jobs.enroll(code,'laptop 2',{})
        with self.assertRaises(PermissionError):self.jobs.enroll(code,'again',{})
        code,_=self.jobs.new_enrollment();self.clock.now+=jobs_module.ENROLL_VALID+1
        with self.assertRaises(PermissionError):self.jobs.enroll(code,'late',{})

    def test_claim_progress_complete(self):
        first,second=self.add('one'),self.add('two')
        self.assertEqual((self.jobs.public(first)['queue_position'],self.jobs.public(second)['queue_position']),(1,2))
        job=self.jobs.claim(self.worker)
        self.assertEqual((job['id'],job['status'],job['attempts']),(first['id'],'assigned',1))
        self.assertIsNone(self.jobs.claim(self.worker))   # concurrency 1
        self.jobs.progress(job['id'],self.worker,job['lease'],'slicing','OrcaSlicer is slicing',40)
        with self.assertRaises(ValueError):self.jobs.progress(job['id'],self.worker,job['lease'],'printing')
        with self.assertRaises(PermissionError):self.jobs.progress(job['id'],self.worker,'stolen','slicing')
        done=self.jobs.complete(job['id'],self.worker,job['lease'],'/x/result.3mf',{'seconds':5})
        self.assertEqual((done['status'],done['stats'],done['lease']),('completed',{'seconds':5},None))
        with self.assertRaises(PermissionError):self.jobs.complete(job['id'],self.worker,job['lease'],'/x/again.3mf',{})   # only once
        public=self.jobs.public(done);self.assertTrue(public['has_result']);self.assertNotIn('lease',public);self.assertNotIn('result',public)

    def test_a_silent_worker_loses_the_job(self):
        job=self.add();claimed=self.jobs.claim(self.worker)
        self.clock.now+=60;self.jobs.heartbeat(self.worker,{'running':[job['id']]})   # renews
        self.clock.now+=60;self.assertEqual(self.jobs.expire(),[])
        self.clock.now+=60;self.jobs.heartbeat(self.worker,{'running':[]})              # restarted: forgot the job
        self.clock.now+=jobs_module.LEASE;self.assertEqual(self.jobs.expire(),[job['id']])
        again=self.jobs.get(job['id']);self.assertEqual((again['status'],again['worker']),('queued',None))
        self.assertIn('stopped answering',again['stage_note'])
        with self.assertRaises(PermissionError):self.jobs.complete(job['id'],self.worker,claimed['lease'],'/x',{})
        for attempt in (2,3):
            self.assertEqual(self.jobs.claim(self.worker)['attempts'],attempt);self.clock.now+=jobs_module.LEASE+1;self.jobs.expire()
        failed=self.jobs.get(job['id']);self.assertEqual(failed['status'],'failed');self.assertIn('3 times',failed['error'])

    def test_failures_retry_only_for_worker_trouble(self):
        job=self.add();claimed=self.jobs.claim(self.worker)
        self.assertEqual(self.jobs.fail(job['id'],self.worker,claimed['lease'],'OrcaSlicer crashed',retry=True)['status'],'queued')
        claimed=self.jobs.claim(self.worker)
        failed=self.jobs.fail(job['id'],self.worker,claimed['lease'],'Some objects are too close','log text')
        self.assertEqual((failed['status'],failed['error'],failed['log']),('failed','Some objects are too close','log text'))

    def test_cancel_tells_the_worker_to_stop(self):
        job=self.add();self.jobs.claim(self.worker)
        self.assertEqual(self.jobs.cancel(job['id'])['status'],'cancelled')
        with self.assertRaises(ValueError):self.jobs.cancel(job['id'])
        self.assertEqual(self.jobs.cancelled(self.worker,[job['id'],'f'*16]),[job['id'],'f'*16])
        other=self.add();self.jobs.claim(self.worker);self.assertEqual(self.jobs.cancelled(self.worker,[other['id']]),[])

    def test_worker_settings(self):
        self.add();self.add();self.add()
        self.jobs.configure(self.worker,concurrency=2)
        self.assertTrue(self.jobs.claim(self.worker));self.assertTrue(self.jobs.claim(self.worker));self.assertIsNone(self.jobs.claim(self.worker))
        with self.assertRaises(ValueError):self.jobs.remove(self.worker)   # still slicing
        self.jobs.configure(self.worker,concurrency=3,draining=True);self.assertIsNone(self.jobs.claim(self.worker))
        self.jobs.configure(self.worker,draining=False,enabled=False);self.assertIsNone(self.jobs.claim(self.worker))
        for bad in (dict(concurrency=0),dict(concurrency=9),dict(concurrency='2'),dict(enabled='yes')):
            with self.assertRaises(ValueError):self.jobs.configure(self.worker,**bad)
        info=self.jobs.worker(self.worker);self.assertEqual((info['online'],len(info['jobs']),info['draining']),(True,2,0))
        self.clock.now+=jobs_module.OFFLINE+1;self.assertFalse(self.jobs.worker(self.worker)['online'])

    def test_catalog(self):
        self.assertEqual(self.jobs.catalog(),(None,None));self.assertFalse(self.jobs.has_catalog('v2.4.2'))
        self.jobs.save_catalog('v2.4.2',{'machines':[]});self.assertEqual(self.jobs.catalog(),('v2.4.2',{'machines':[]}))
        self.assertTrue(self.jobs.has_catalog('v2.4.2'))

    def test_survives_a_restart(self):
        job=self.add();self.jobs.claim(self.worker);self.jobs.db.close()
        self.jobs=Jobs(Path(self.tmp.name)/'s.sqlite3',self.clock);self.clock.now+=jobs_module.LEASE+1
        self.assertEqual(self.jobs.claim(self.worker)['id'],job['id'])   # expired on claim, given out again


class WorkerTests(unittest.TestCase):
    """The worker's job steps, with FAKE_ORCA in OrcaSlicer's place."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'fake_orca.py').write_text(FAKE_ORCA)
        self.config=dict(worker.DEFAULTS,orca=[sys.executable,str(d/'fake_orca.py')],profiles=str(write_profiles(d/'profiles')),
                         xvfb=False,work_dir=str(d/'work'),timeout=30)
        self.log=d/'orca.json'
        self.job=dict(id='j',objects=[dict(name='cube',matrix=[1,0,0,0, 0,1,0,0, 0,0,1,0, 80,80,0,1])],
                      settings=dict(machine=MACHINE,process=PROCESS,filament=FILAMENT),changes=settings.check({'wall_loops':4,'nozzle_temperature':230}))
        self.stages=[]
    def tearDown(self):self.tmp.cleanup()
    def run_job(self,mode='ok',**kw):
        with patch.dict(os.environ,FAKE_ORCA_MODE=mode,FAKE_ORCA_LOG=str(self.log)):
            return worker.slice_job(self.job,self.config,lambda i:cube_stl(),lambda stage,note:self.stages.append(stage),**kw)

    def test_a_job(self):
        data,stats,log=self.run_job()
        self.assertEqual(zipfile.ZipFile(io.BytesIO(data)).read('Metadata/plate_1.gcode'),b'; sliced\nG28\n')
        self.assertEqual(stats,dict(seconds=1234,grams=5.61,filaments=[dict(type='PLA',color='#FFFFFF',meters=1.88,grams=5.61)],objects=1,
                                    warnings=['bridge_without_support']))
        self.assertEqual(self.stages,['preparing','slicing','processing'])
        seen=json.loads(self.log.read_text())
        args=seen['args'];self.assertEqual(args[:6],['--arrange','0','--orient','0','--slice','0'])
        self.assertEqual(args[args.index('--export-3mf')+1],'result.3mf')
        self.assertEqual((seen['process']['wall_loops'],seen['process']['inherits'],seen['filament']['nozzle_temperature']),('4',PROCESS,['230']))
        self.assertEqual((seen['machine']['inherits'],seen['machine']['printable_height']),('','180'))
        self.assertEqual(meshes.summary(bytes.fromhex(seen['stls'][0]))['min'],[80,80,0])   # placed where the Slicer tab put it
        self.assertEqual(list(Path(self.config['work_dir']).iterdir()),[])                     # temporary folder removed

    def test_orca_errors(self):
        with self.assertRaises(worker.SliceError) as caught:self.run_job('error')
        self.assertEqual((str(caught.exception),caught.exception.retry),('OrcaSlicer: Some objects are too close (code -51)',False))
        with self.assertRaises(worker.SliceError) as caught:self.run_job('crash')
        self.assertTrue(caught.exception.retry)   # killed by a signal: this computer's trouble, another may manage
        with self.assertRaisesRegex(worker.SliceError,'without G-code'):self.run_job('unsliced')
        self.config['timeout']=1
        with self.assertRaises(worker.SliceError) as caught:self.run_job('hang')
        self.assertTrue(caught.exception.retry);self.assertIn('longer than',str(caught.exception))
        self.assertEqual(list(Path(self.config['work_dir']).iterdir()),[])

    def test_cancel_kills_orca(self):
        started=time.monotonic()
        with self.assertRaises(worker.Lost):self.run_job('hang',alive=lambda:time.monotonic()-started<1.5)
        self.assertLess(time.monotonic()-started,20)

    def test_bad_inputs(self):
        self.job['settings']['process']='missing'
        with self.assertRaisesRegex(worker.SliceError,'no process preset'):self.run_job()
        self.job['settings']['process']=PROCESS
        with self.assertRaises(worker.SliceError):
            worker.slice_job(self.job,self.config,lambda i:b'not an stl at all',lambda *a:None)

    def test_command_line(self):
        command=worker.orca_command(dict(self.config,xvfb=False),dict(machine='m.json',process='p.json',filament='f.json'),['a.stl'],'out')
        self.assertEqual(command[2:],['--arrange','0','--orient','0','--slice','0','--load-settings','m.json;p.json','--load-filaments','f.json',
                                      '--outputdir','out','--export-3mf','result.3mf','a.stl'])
        with patch.object(worker.shutil,'which',return_value='/usr/bin/xvfb-run'):
            self.assertEqual(worker.orca_command(dict(self.config,xvfb=True),dict(machine='m',process='p',filament='f'),[],'o')[:2],['xvfb-run','-a'])

    def test_test_slice(self):
        with patch.dict(os.environ,FAKE_ORCA_MODE='ok',FAKE_ORCA_LOG=str(self.log)),patch('builtins.print'):
            size,stats=worker.test_slice(self.config)
        self.assertGreater(size,0);self.assertEqual(stats['seconds'],1234)
        self.assertEqual(meshes.summary(bytes.fromhex(json.loads(self.log.read_text())['stls'][0]))['min'],[80,80,0])   # middle of the 180 mm bed


class Core:
    """The parts of core.py the queue and the dashboard use."""
    def __init__(self,d,configs):
        self.DATA_DIR,self.SETTINGS_FILE,self.settings,self.SETTINGS_USER_IDS=d,str(d/'settings.json'),{},set()
        self.EXAMPLE_MODE,self.configs,self.last_seen=True,configs,{}
        self.EXAMPLE_DATA={name:{} for name in configs}
        self.bot=SimpleNamespace(is_ready=lambda:False);self.log=__import__('logging').getLogger('test')
        self.notify=AsyncMock();self.BLUE,self.YELLOW,self.GREEN,self.RED=1,2,3,4
    def names(self):return list(self.configs)
    def printer_config(self,name):return self.configs.get(name)
    def display_name(self,name):return name
    def state_data(self,name):return 'IDLE',0,{},True


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        from queueing import Engine,Store
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'Mini':{'model':'A1 mini'}})
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        self.slicer=self.dashboard.slicer
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.headers={'X-PM':'1','X-CSRF':(await r.json())['csrf']}
        (d/'fake_orca.py').write_text(FAKE_ORCA)
        self.profiles=write_profiles(d/'profiles')
        self.worker_config=dict(worker.DEFAULTS,orca=[sys.executable,str(d/'fake_orca.py')],profiles=str(self.profiles),xvfb=False,
                                work_dir=str(d/'work'),timeout=30,orca_version='v2.4.2',server=str(self.client.make_url('')).rstrip('/'))
        self.env=patch.dict(os.environ,{'no_proxy':'*','NO_PROXY':'*','FAKE_ORCA_MODE':'ok'});self.env.start()
    async def asyncTearDown(self):
        self.env.stop()
        for task in list(self.engine.tasks):task.cancel()
        await self.client.close();self.store.db.close();self.slicer.jobs.db.close();self.tmp.cleanup()

    async def enroll(self):
        r=await self.client.post('/api/slicer/workers/enroll',json={},headers=self.headers);answer=await r.json()
        self.assertIn(f"--code {answer['code']}",answer['command']);self.assertIn('/slicer/worker/install.sh | sudo bash',answer['command'])
        r=await self.client.post('/api/slicer/worker/enroll',json=dict(code=answer['code'],name='laptop 1',info={}))
        self.assertEqual(r.status,200);answer=await r.json()
        self.worker_config.update(worker_id=answer['worker_id'],token=answer['token'])
        return {'Authorization':f"Bearer {answer['worker_id']}:{answer['token']}"}

    async def upload(self,data=None,name='cube.stl'):
        from aiohttp import FormData
        form=FormData();form.add_field('file',data or cube_stl(),filename=name)
        return await self.client.post('/api/slicer/models',data=form,headers=self.headers)

    def plate(self,model,**extra):
        return dict(label='Cubes',settings=dict(machine=MACHINE,process=PROCESS,filament=FILAMENT),changes={'wall_loops':3},
                    objects=[dict(model=model,name='cube.stl',matrix=[1,0,0,0, 0,1,0,0, 0,0,1,0, 90,90,0,1])],**extra)

    async def test_whole_job_from_the_tab_to_the_print_queue(self):
        auth=await self.enroll()
        r=await self.client.post('/api/slicer/jobs',json=self.plate('0'*64),headers=self.headers)
        self.assertEqual(r.status,400)   # no presets yet, unknown model
        model=(await (await self.upload()).json())['models'][0]
        self.assertEqual((model['triangles'],model['size'],len(model['id'])),(12,[20.0,20.0,20.0],64))
        r=await self.client.post('/api/slicer/jobs',json=self.plate(model['id']),headers=self.headers)
        self.assertIn('No slicing worker has reported its presets',(await r.json())['error'])

        bot=worker.Worker(self.worker_config)
        await asyncio.to_thread(bot.heartbeat)   # the Pi has no presets for v2.4.2: the worker sends them
        catalog=await (await self.client.get('/api/slicer/catalog')).json()
        self.assertEqual((catalog['version'],catalog['workers'],catalog['printers']),('v2.4.2',1,[dict(name='Mini',model='A1 mini')]))
        self.assertEqual(len(catalog['catalog']['machines']),2);self.assertTrue(catalog['schema'])
        r=await self.client.post('/api/slicer/worker/heartbeat',json=dict(orca_version='v2.4.2',running=[]),headers=auth)
        self.assertFalse((await r.json())['catalog_wanted'])

        wrong=self.plate(model['id']);wrong['settings']['machine']='Bambu Lab X1 Carbon 0.4 nozzle'
        r=await self.client.post('/api/slicer/jobs',json=wrong,headers=self.headers)
        self.assertIn('is not for the Bambu Lab X1 Carbon',(await r.json())['error'])
        for bad in (dict(changes={'start_gcode':'M112'}),dict(objects=[]),dict(objects=[dict(model=model['id'],matrix=[0]*16)])):
            r=await self.client.post('/api/slicer/jobs',json=dict(self.plate(model['id']),**bad),headers=self.headers);self.assertEqual(r.status,400,bad)
        job=await (await self.client.post('/api/slicer/jobs',json=self.plate(model['id']),headers=self.headers)).json()
        self.assertEqual((job['status'],job['queue_position'],job['label']),('queued',1,'Cubes'))

        claimed=(await (await self.client.post('/api/slicer/worker/claim',json={},headers=auth)).json())['job']
        self.assertEqual((claimed['id'],claimed['changes']),(job['id'],{'process':{'wall_loops':3},'filament':{}}))
        self.assertNotIn('model',claimed['objects'][0])   # the worker downloads models by index, with its lease
        r=await self.client.get(f"/api/slicer/worker/jobs/{job['id']}/models/0",headers=dict(auth,**{'X-Lease':'wrong'}))
        self.assertEqual(r.status,409)
        with bot.lock:bot.running[job['id']]=True
        await asyncio.to_thread(bot.work,claimed)
        done=await (await self.client.get(f"/api/slicer/jobs/{job['id']}")).json()
        self.assertEqual((done['status'],done['has_result'],done['stats']['seconds'],done['worker']),('completed',True,1234,self.worker_config['worker_id']))

        r=await self.client.get(f"/api/slicer/jobs/{job['id']}/result")
        self.assertEqual(r.status,200);self.assertIn('Cubes.gcode.3mf',r.headers['Content-Disposition'])
        self.assertIn('Metadata/plate_1.gcode',zipfile.ZipFile(io.BytesIO(await r.read())).namelist())
        r=await self.client.post(f"/api/slicer/jobs/{job['id']}/queue",json=dict(printer='Mini'),headers=self.headers)
        queued=await r.json();self.assertEqual((r.status,queued['printer']),(200,'Mini'))
        entry=self.store.get(queued['job']);self.assertEqual((entry['label'],entry['status'],entry['remote']),('Cubes','queued','Cubes.gcode.3mf'))
        r=await self.client.post(f"/api/slicer/jobs/{job['id']}/queue",json=dict(printer='Nope'),headers=self.headers)
        self.assertEqual(r.status,400);self.assertEqual(len(list(self.dashboard.uploads.glob('*.3mf'))),1)   # no file left behind
        titles=[e['title'] for e in self.store.events()]
        for title in ('Slicing job queued','Slicing job started','Slicing job finished','Sliced plate sent to a print queue'):self.assertIn(title,titles)

    async def test_failed_slice_shows_why(self):
        auth=await self.enroll();bot=worker.Worker(self.worker_config);await asyncio.to_thread(bot.heartbeat)
        model=(await (await self.upload()).json())['models'][0]
        job=await (await self.client.post('/api/slicer/jobs',json=self.plate(model['id']),headers=self.headers)).json()
        claimed=(await (await self.client.post('/api/slicer/worker/claim',json={},headers=auth)).json())['job']
        os.environ['FAKE_ORCA_MODE']='error'
        with bot.lock:bot.running[job['id']]=True
        await asyncio.to_thread(bot.work,claimed)
        failed=await (await self.client.get(f"/api/slicer/jobs/{job['id']}")).json()
        self.assertEqual((failed['status'],failed['error']),('failed','OrcaSlicer: Some objects are too close (code -51)'))
        r=await self.client.get(f"/api/slicer/jobs/{job['id']}/result");self.assertEqual(r.status,400)

    async def test_worker_api_needs_the_worker_token_and_lease(self):
        r=await self.client.post('/api/slicer/worker/heartbeat',json={});self.assertEqual(r.status,401)
        r=await self.client.post('/api/slicer/worker/heartbeat',json={},headers={'Authorization':'Bearer w-1234:nope'});self.assertEqual(r.status,401)
        r=await self.client.post('/api/slicer/worker/enroll',json=dict(code='made-up',name='x'));self.assertEqual(r.status,403)
        auth=await self.enroll()
        r=await self.client.post('/api/slicer/worker/heartbeat',json=dict(running=[]),headers=auth)   # no dashboard session or X-PM needed
        self.assertEqual(r.status,200);self.assertEqual((await r.json())['concurrency'],1)
        self.assertEqual((await self.client.post('/api/slicer/worker/claim',json={},headers=auth)).status,200)
        model=(await (await self.upload()).json())['models'][0]
        self.slicer.jobs.save_catalog('v2.4.2',profiles.Profiles(self.profiles).catalog())
        job=await (await self.client.post('/api/slicer/jobs',json=self.plate(model['id']),headers=self.headers)).json()
        claimed=(await (await self.client.post('/api/slicer/worker/claim',json={},headers=auth)).json())['job']
        r=await self.client.get(f"/api/slicer/worker/jobs/{job['id']}/models/0",headers=dict(auth,**{'X-Lease':claimed['lease']}))
        self.assertEqual(await r.read(),cube_stl())
        r=await self.client.post(f"/api/slicer/worker/jobs/{job['id']}/result?lease={claimed['lease']}",data=b'not a 3mf',headers=auth)
        self.assertEqual(r.status,400)   # rejected, and the job goes back in the queue for another try
        self.assertEqual(self.slicer.jobs.get(job['id'])['status'],'queued')
        self.assertEqual(list(self.slicer.results.iterdir()),[])
        claimed=(await (await self.client.post('/api/slicer/worker/claim',json={},headers=auth)).json())['job']
        await self.client.post(f"/api/slicer/jobs/{job['id']}/cancel",json={},headers=self.headers)
        r=await self.client.post('/api/slicer/worker/heartbeat',json=dict(running=[job['id']]),headers=auth)
        self.assertEqual((await r.json())['stop'],[job['id']])
        r=await self.client.post(f"/api/slicer/worker/jobs/{job['id']}/progress",json=dict(lease=claimed['lease'],stage='slicing'),headers=auth)
        self.assertEqual(r.status,409)
        # The dashboard's own slicer routes still need signing in.
        from aiohttp.test_utils import TestClient,TestServer
        anonymous=TestClient(TestServer(self.dashboard.app));await anonymous.start_server()
        try:
            self.assertEqual((await anonymous.get('/api/slicer/jobs')).status,401)
            self.assertEqual((await anonymous.post('/api/slicer/workers/enroll',json={},headers={'X-PM':'1'})).status,401)
        finally:await anonymous.close()

    async def test_uploads_and_admin(self):
        self.assertEqual((await self.upload(b'not an stl at all, sorry')).status,400)
        self.assertEqual((await self.upload(name='cube.step')).status,400)
        first=(await (await self.upload()).json())['models'][0];second=(await (await self.upload()).json())['models'][0]
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(list(self.slicer.models.iterdir())),1)
        await self.enroll()
        workers=(await (await self.client.get('/api/slicer/workers')).json())['workers'];wid=workers[0]['id']
        self.assertNotIn('token',workers[0])
        r=await self.client.post(f'/api/slicer/workers/{wid}',json=dict(concurrency=2,draining=True),headers=self.headers)
        self.assertEqual(((await r.json())['concurrency'],(await r.json())['draining']),(2,1))
        self.assertEqual((await self.client.post(f'/api/slicer/workers/{wid}',json=dict(concurrency=50),headers=self.headers)).status,400)
        self.assertEqual((await self.client.post(f'/api/slicer/workers/{wid}/remove',json={},headers=self.headers)).status,200)
        self.assertEqual((await (await self.client.get('/api/slicer/workers')).json())['workers'],[])

    async def test_live_updates(self):
        r=await self.client.get('/api/slicer/events')
        self.assertEqual(r.headers['Content-Type'],'text/event-stream')
        line=await asyncio.wait_for(r.content.readline(),5)
        self.assertEqual(json.loads(line.decode()[len('data: '):]),dict(jobs=[],workers=[]))
        r.close()

    async def test_worker_setup_files(self):
        r=await self.client.get('/slicer/worker/install.sh');text=await r.text()
        self.assertEqual(r.status,200);self.assertIn(f'ORCA_VERSION_DEFAULT="{installer.ORCA_VERSION}"',text)
        r=await self.client.get('/slicer/worker/bundle.tar.gz')
        with tarfile.open(fileobj=io.BytesIO(await r.read())) as archive:
            self.assertEqual(sorted(archive.getnames()),['meshes.py','profiles.py','worker.py'])
            for name in archive.getnames():self.assertEqual(archive.extractfile(name).read(),(ROOT/'slicer'/name).read_bytes())

    async def test_assets(self):
        for name in ('slicer.js','three.module.min.js','OrbitControls.js','TransformControls.js','STLLoader.js'):
            r=await self.client.get(f'/assets/{name}');self.assertEqual(r.status,200,name)


class BundleTests(unittest.TestCase):
    def test_worker_runs_from_its_bundle(self):
        """On a worker computer, worker.py sits next to meshes.py and profiles.py, without the rest of this app."""
        with tempfile.TemporaryDirectory() as d:
            for name in ('worker.py','meshes.py','profiles.py'):shutil.copy(ROOT/'slicer'/name,d)
            out=subprocess.run([sys.executable,'-I',str(Path(d)/'worker.py'),'--help'],capture_output=True,text=True,timeout=60)
            self.assertEqual(out.returncode,0,out.stderr);self.assertIn('enroll',out.stdout)
            out=subprocess.run([sys.executable,'-I',str(Path(d)/'worker.py'),'--config',str(Path(d)/'none.json'),'run'],capture_output=True,text=True,timeout=60)
            self.assertIn('Not registered yet',out.stderr)

    def test_installer_script(self):
        script=installer.install_script()
        self.assertNotIn('__ORCA_VERSION__',script)
        with tempfile.NamedTemporaryFile('w',suffix='.sh',delete=False) as f:f.write(script)
        try:self.assertEqual(subprocess.run(['bash','-n',f.name],capture_output=True).returncode,0)
        finally:os.unlink(f.name)
        for needed in ('--appimage-extract','MemoryMax=$MEMORY_MAX','test-slice','ProtectSystem=strict','User=orcaworker','chmod 0640',
                       'HandleLidSwitch=ignore','slicer-worker-update.timer','ln -sfn "$APP/slicer-worker" /usr/local/bin/slicer-worker'):
            self.assertIn(needed,script)
        self.assertNotIn('\n  . /etc/os-release',script)   # it sets NAME, VERSION...: only read in a subshell (os_field)

    def run_script(self,*args):
        return subprocess.run(['bash','-s','--',*args],input=installer.install_script(),capture_output=True,text=True,timeout=60)

    def test_installer_commands(self):
        out=self.run_script('help')
        self.assertEqual(out.returncode,0)
        for command in ('install','status','logs','test','update','restart','doctor','uninstall','--auto-update','--no-keep-awake'):
            self.assertIn(command,out.stdout)
        out=self.run_script('--frobnicate');self.assertEqual(out.returncode,2);self.assertIn('Unknown option',out.stderr)
        out=self.run_script('install','--memory-max','lots');self.assertEqual(out.returncode,1);self.assertIn('--memory-max',out.stderr)
        out=self.run_script('install','--orca-version','latest;rm');self.assertEqual(out.returncode,1);self.assertIn('release tag',out.stderr)

    def test_installer_helpers(self):
        script=installer.install_script()
        functions='\n'.join(re.search(rf'^{name}\(\) \{{.*?^\}}$|^{name}\(\) \{{.*$',script,re.M|re.S).group(0) for name in ('normalise_server','os_field'))
        test=functions+'\nNAME=mine; for s in 192.168.1.10 pi.local:9000 http://10.0.0.2:8080/ https://pi.example; do normalise_server "$s"; done; os_field ID >/dev/null; echo "$NAME"'
        out=subprocess.run(['bash','-c',test],capture_output=True,text=True,timeout=30)
        self.assertEqual(out.stdout.split(),['http://192.168.1.10:8080','http://pi.local:9000','http://10.0.0.2:8080','https://pi.example:8080','mine'])


class PageTests(unittest.TestCase):
    def test_every_element_the_script_uses_is_on_the_page(self):
        import re
        page,script=(ROOT/'static/index.html').read_text(),(ROOT/'static/slicer.js').read_text()
        ids=set(re.findall(r"el\('([A-Za-z]+)'\)",script))
        self.assertTrue(ids);self.assertEqual(sorted(i for i in ids if f'id="{i}"' not in page),[])
        for mode in ('translate','rotate','scale'):self.assertIn(f'data-gizmo="{mode}"',page)
        for field in ('px','py','pz','rx','ry','rz','sx','sy','sz','uniform'):self.assertIn(f'name="{field}"',page)
        self.assertIn('<script type="module" src="/assets/slicer.js"></script>',page);self.assertIn('data-tab="slicer"',page)
        self.assertIn("new CustomEvent('tab',{detail:name})",(ROOT/'static/app.js').read_text())
        self.assertFalse(re.search(r'<[^>]+\son[a-z]+=',page),'inline event handlers are blocked by the CSP')

    def test_vendored_three_imports_stay_local(self):
        import re
        for name in ('OrbitControls.js','TransformControls.js','STLLoader.js'):
            sources=set(re.findall(r"from\s+'([^']+)'",(ROOT/'static'/name).read_text()))
            self.assertEqual(sources,{'./three.module.min.js'},name)

    @unittest.skipUnless(NODE,'node is not installed')
    def test_scripts_parse(self):
        for name in ('slicer.js','app.js'):
            out=subprocess.run([NODE,'--check',str(ROOT/'static'/name)],capture_output=True,text=True,timeout=60)
            self.assertEqual(out.returncode,0,out.stderr)


if __name__=='__main__':
    unittest.main()
