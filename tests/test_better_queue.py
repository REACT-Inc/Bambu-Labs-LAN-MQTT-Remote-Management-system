"""Queue a print improvements (#7): reading plates from the .3mf, AMS suggestions, model checks and Print now."""
import io,json,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sliced_file
from queueing import Store,Engine,options

PNG=b'\x89PNG\r\n\x1a\nfake'


def make_3mf(path,model='Bambu Lab A1 mini',plates=((1,'Brackets',True),(2,'',True),(3,'Spare',False))):
    slice_plates=''.join(f'<plate><metadata key="index" value="{i}"/><metadata key="prediction" value="{i*3600+300}"/>'
                         f'<metadata key="weight" value="{i*10.5}"/><filament id="1" type="PLA" color="#FF0000" used_g="5"/>'
                         f'<filament id="3" type="PETG" color="#00FF00FF" used_g="2"/></plate>' for i,_,sliced in plates if sliced)
    settings=''.join(f'<plate><metadata key="plater_id" value="{i}"/><metadata key="plater_name" value="{name}"/></plate>' for i,name,_ in plates)
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('Metadata/slice_info.config',f'<?xml version="1.0"?><config>{slice_plates}</config>')
        z.writestr('Metadata/model_settings.config',f'<?xml version="1.0"?><config>{settings}</config>')
        z.writestr('Metadata/project_settings.config',json.dumps({'printer_model':model}))
        for i,_,sliced in plates:
            if sliced:z.writestr(f'Metadata/plate_{i}.gcode','G28\n');z.writestr(f'Metadata/plate_{i}.png',PNG)
    return path


AMS={'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tray_color':'FE0101FF'},{'id':'1','tray_type':'PLA','tray_color':'0000FFFF'},
                                     {'id':'2','tray_type':'PETG','tray_color':'00EE00FF'},{'id':'3'}]}]}}


class SlicedFileTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=make_3mf(Path(self.tmp.name)/'f.3mf')
    def tearDown(self):self.tmp.cleanup()

    def test_reads_plates(self):
        info=sliced_file.inspect(self.path)
        self.assertEqual((info['model'],info['sliced']),('a1mini',True))
        first,second,third=info['plates']
        self.assertEqual((first['index'],first['name'],first['sliced'],first['prediction'],first['weight'],first['thumbnail']),(1,'Brackets',True,3900,10.5,True))
        self.assertEqual(first['filaments'],[{'id':1,'type':'PLA','colour':'#FF0000','used_g':5.0},{'id':3,'type':'PETG','colour':'#00FF00','used_g':2.0}])
        self.assertEqual(second['name'],'Plate 2')                                  # no name in the file
        self.assertEqual((third['name'],third['sliced'],third['thumbnail']),('Spare',False,False))
        self.assertEqual(sliced_file.plate_thumbnail(self.path,1),PNG);self.assertIsNone(sliced_file.plate_thumbnail(self.path,3))

    def test_unsliced_and_broken_files(self):
        unsliced=make_3mf(Path(self.tmp.name)/'u.3mf',plates=((1,'Only',False),))
        self.assertFalse(sliced_file.inspect(unsliced)['sliced'])
        bad=Path(self.tmp.name)/'bad.3mf';bad.write_bytes(b'nope')
        with self.assertRaisesRegex(ValueError,'readable'):sliced_file.inspect(bad)
        evil=Path(self.tmp.name)/'evil.3mf'
        with zipfile.ZipFile(evil,'w') as z:
            z.writestr('Metadata/plate_1.gcode','G28');z.writestr('Metadata/slice_info.config','<!DOCTYPE x [<!ENTITY a "aaaa">]><config/>')
        self.assertEqual(sliced_file.inspect(evil)['plates'][0]['filaments'],[])     # entity declarations are never parsed

    def test_suggests_ams_mapping(self):
        plate=sliced_file.inspect(self.path)['plates'][0]
        result=sliced_file.suggest_mapping(plate,AMS)
        self.assertEqual((result['mapping'],result['complete']),('0,-1,2',True))   # filament 2 isn't used by this plate
        self.assertEqual([(r['filament'],r['tray_label'],r['match']) for r in result['rows']],[(1,'AMS 1 slot 1','exact'),(3,'AMS 1 slot 3','exact')])
        only_blue={'ams':{'ams':[{'id':'1','tray':[{'id':'2','tray_type':'PLA','tray_color':'0000FFFF'}]}]}}
        partial=sliced_file.suggest_mapping(plate,only_blue)
        self.assertEqual((partial['complete'],partial['mapping'],partial['rows'][0]['tray'],partial['rows'][0]['match'],partial['rows'][1]['match']),(False,'',6,'material','none'))
        self.assertIn('No loaded AMS',sliced_file.suggest_mapping(plate,{})['message'])

    def test_mapping_allows_unused_filaments(self):
        self.assertEqual(options(1,True,'0,-1,2')['ams_mapping'],[0,-1,2])
        with self.assertRaises(ValueError):options(1,True,'-1,-1')
        with self.assertRaises(ValueError):options(1,True,'0,-2')
        with self.assertRaisesRegex(ValueError,'commas'):options(1,True,'zero')


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        configs={'Mini':{'model':'A1 mini'},'H2D':{'model':'H2D'}}
        async def notify(*a):pass
        self.state='IDLE'
        self.core=SimpleNamespace(DATA_DIR=d,SETTINGS_FILE=str(d/'settings.json'),settings={},SETTINGS_USER_IDS=set(),EXAMPLE_MODE=True,
            names=lambda:list(configs),printer_config=configs.get,state_data=lambda n:(self.state,getattr(self,'error',0),dict(AMS),True),last_seen={},
            bot=SimpleNamespace(is_ready=lambda:False),log=__import__('logging').getLogger('test'),notify=notify,BLUE=1,YELLOW=2,GREEN=3,RED=4)
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.headers={'X-PM':'1','X-CSRF':(await r.json())['csrf']}
    async def asyncTearDown(self):
        for t in list(self.engine.tasks):t.cancel()
        await self.client.close();self.store.db.close();self.tmp.cleanup()
    async def upload(self,path):
        from aiohttp import FormData
        form=FormData();form.add_field('file',path.read_bytes(),filename=path.name)
        return await self.client.post('/api/upload',data=form,headers=self.headers)

    async def test_upload_returns_plates_and_refuses_unsliced(self):
        r=await self.upload(make_3mf(Path(self.tmp.name)/'part.3mf'));data=await r.json()
        self.assertEqual(r.status,200);self.assertEqual(data['model'],'A1 mini')
        self.assertEqual([p['index'] for p in data['plates'] if p['sliced']],[1,2])
        thumb=await self.client.get(f"/api/uploads/{data['asset']}/plate/1");self.assertEqual((thumb.status,await thumb.read()),(200,PNG))
        self.assertEqual((await self.client.get(f"/api/uploads/{data['asset']}/plate/3")).status,404)
        self.assertEqual((await self.client.get('/api/uploads/../plate/1')).status,404)
        r=await self.upload(make_3mf(Path(self.tmp.name)/'raw.3mf',plates=((1,'x',False),)))
        self.assertEqual(r.status,400);self.assertIn("isn't sliced",(await r.json())['error'])
        self.assertEqual(len(list((Path(self.tmp.name)/'uploads').glob('*.3mf'))),1)    # the refused upload was deleted
        self.assertEqual(len(list((Path(self.tmp.name)/'uploads').glob('*.name'))),1)   # and its name file

    async def test_suggestion_and_model_check(self):
        asset=(await (await self.upload(make_3mf(Path(self.tmp.name)/'part.3mf'))).json())['asset']
        s=await (await self.client.get(f'/api/uploads/{asset}/suggest?printer=Mini&plate=1')).json()
        self.assertEqual((s['mapping'],s['model_warning']),('0,-1,2',''))
        s=await (await self.client.get(f'/api/uploads/{asset}/suggest?printer=H2D&plate=1')).json()
        self.assertIn('sliced for the A1 mini, not the H2D',s['model_warning'])
        r=await self.client.post('/api/jobs',json={'printer':'H2D','label':'Part','asset':asset,'plate':1},headers=self.headers)
        self.assertEqual(r.status,400);self.assertIn('Re-slice',(await r.json())['error'])
        r=await self.client.post('/api/jobs',json={'printer':'Mini','label':'Part','asset':asset,'plate':2,'use_ams':True,'mapping':'0,-1,2'},headers=self.headers)
        self.assertEqual(r.status,200);job=self.store.jobs('Mini')[0]
        self.assertEqual((job['status'],job['options']['plate'],job['options']['ams_mapping']),('queued',2,[0,-1,2]))

    async def test_print_now_skips_the_queue(self):
        asset=(await (await self.upload(make_3mf(Path(self.tmp.name)/'part.3mf'))).json())['asset']
        waiting=self.store.add('Mini','Waiting job',None,'old.3mf',options(),'t',True)
        body={'printer':'Mini','label':'Urgent','asset':asset,'plate':1,'print_now':True}
        self.assertEqual((await self.client.post('/api/jobs',json=body,headers=self.headers)).status,400)   # needs the confirmation
        with patch.object(self.engine,'spawn',side_effect=lambda coro:coro.close()):
            r=await self.client.post('/api/jobs',json={**body,'confirmed':True},headers=self.headers)
        self.assertEqual(r.status,200);new=self.store.get((await r.json())['id'])
        self.assertEqual(new['status'],'staging')                                        # started ahead of the waiting job
        self.assertEqual(self.store.get(waiting['id'])['status'],'queued')

    async def test_print_now_that_cannot_start_leaves_nothing(self):
        asset=(await (await self.upload(make_3mf(Path(self.tmp.name)/'part.3mf'))).json())['asset']
        self.state='RUNNING'
        r=await self.client.post('/api/jobs',json={'printer':'Mini','label':'Urgent','asset':asset,'plate':1,'print_now':True,'confirmed':True},headers=self.headers)
        self.assertEqual(r.status,400);self.assertIn('RUNNING',(await r.json())['error'])
        self.assertEqual(self.store.jobs('Mini'),[])                                     # refused before anything was added
        self.state='IDLE';self.store.add('Mini','Busy',None,'x.3mf',options(),'t',True)
        self.store.set_status(self.store.jobs('Mini')[0]['id'],'printing')               # another job is active
        r=await self.client.post('/api/jobs',json={'printer':'Mini','label':'Urgent','asset':asset,'plate':1,'print_now':True,'confirmed':True},headers=self.headers)
        self.assertEqual(r.status,400);self.assertIn('Nothing was left in the queue',(await r.json())['error'])
        self.assertEqual([j['status'] for j in self.store.jobs('Mini') if j['label']=='Urgent'],['cancelled'])

    async def test_uploaded_file_keeps_its_name_on_the_printer(self):
        asset=(await (await self.upload(make_3mf(Path(self.tmp.name)/'Bracket v2.gcode.3mf'))).json())['asset']
        body={'printer':'Mini','label':'Bracket','asset':asset,'plate':1}
        first=await (await self.client.post('/api/jobs',json=body,headers=self.headers)).json()
        self.assertEqual(self.store.get(first['id'])['remote'],'Bracket_v2.gcode.3mf')
        # A second waiting job with the same file gets its own name, so neither can swap the other's file.
        second=await (await self.client.post('/api/jobs',json=body,headers=self.headers)).json()
        self.assertEqual(self.store.get(second['id'])['remote'],f"Bracket_v2-{second['id'][:6]}.gcode.3mf")
        # Once the first is done its name is free again; a reprint keeps the original name.
        self.store.set_status(first['id'],'finished');self.store.set_status(second['id'],'cancelled')
        await self.client.post(f"/api/jobs/{first['id']}/reprint",json={},headers=self.headers)
        self.assertEqual([j['remote'] for j in self.store.jobs('Mini') if j['status']=='queued'],['Bracket_v2.gcode.3mf'])

    async def test_print_now_can_ignore_a_failed_state_or_error(self):
        asset=(await (await self.upload(make_3mf(Path(self.tmp.name)/'part.3mf'))).json())['asset']
        body={'printer':'Mini','label':'Again','asset':asset,'plate':1,'print_now':True,'confirmed':True}
        for state,error,expected in (('RUNNING',0,'RUNNING'),('IDLE',0x0300800A,'error')):   # FAILED alone is ready now
            self.state,self.error=state,error
            r=await self.client.post('/api/jobs',json=body,headers=self.headers)
            self.assertEqual(r.status,400);self.assertIn(expected,(await r.json())['error'])
            self.assertEqual(self.store.jobs('Mini'),[])                                 # refused before anything was added
        self.state,self.error='FAILED',0x0300800A
        r=await self.client.post('/api/jobs',json={**body,'override_error':'yes'},headers=self.headers)
        self.assertEqual(r.status,400);self.assertIn('boolean',(await r.json())['error'])
        with patch.object(self.engine,'spawn',side_effect=lambda coro:coro.close()):
            r=await self.client.post('/api/jobs',json={**body,'override_error':True},headers=self.headers)
        self.assertEqual(r.status,200);self.assertEqual(self.store.get((await r.json())['id'])['status'],'staging')
        self.assertIn('Error override approved',[e['title'] for e in self.store.events()])
        # The override never allows starting over a print that's still running.
        self.store.set_status(self.store.jobs('Mini')[0]['id'],'finished');self.state,self.error='RUNNING',0
        r=await self.client.post('/api/jobs',json={**body,'override_error':True},headers=self.headers)
        self.assertEqual(r.status,400);self.assertIn('RUNNING',(await r.json())['error'])
    async def test_cancel_objects_through_the_dashboard(self):
        info='<?xml version="1.0"?><config><plate><metadata key="index" value="1"/><object identify_id="75" name="Cube" skipped="false"/><object identify_id="92" name="Bracket" skipped="false"/></plate></config>'
        path=Path(self.tmp.name)/'objects.3mf'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('Metadata/slice_info.config',info);z.writestr('Metadata/plate_1.gcode','G1');z.writestr('Metadata/plate_1.png',PNG)
        asset=(await (await self.upload(path)).json())['asset']
        job=(await (await self.client.post('/api/jobs',json={'printer':'Mini','label':'Plate','asset':asset,'plate':1},headers=self.headers)).json())
        self.store.set_status(job['id'],'printing');self.state='RUNNING'
        r=await self.client.get('/api/objects/Mini');data=await r.json()
        self.assertEqual(r.status,200,data);self.assertEqual([o['name'] for o in data['objects']],['Cube','Bracket']);self.assertTrue(data['picture'])
        self.assertEqual(await (await self.client.get('/api/objects/Mini/plate.png')).read(),PNG)
        self.core.EXAMPLE_MODE=True;self.core.EXAMPLE_DATA={'Mini':{}}
        r=await self.client.post('/api/objects/Mini',json={'ids':[75],'confirmed':True},headers=self.headers)
        self.assertEqual(r.status,200,await r.text());self.assertEqual(self.core.EXAMPLE_DATA['Mini']['s_obj'],[75])
        r=await self.client.post('/api/objects/Mini',json={'ids':[92],'confirmed':True},headers=self.headers)
        self.assertEqual(r.status,200)   # demo data doesn't feed s_obj back into state_data here; the real printer reports it


class PrinterFileNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db')
    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def test_safe_names(self):
        name=lambda w:self.store.printer_file_name('P',w,'abcdef123456')
        self.assertEqual(name('Bracket v2.gcode.3mf'),'Bracket_v2.gcode.3mf')
        self.assertEqual(name('My Part (final).3mf'),'My_Part_final.gcode.3mf')
        self.assertEqual(name('../../etc/passwd.3mf'),'passwd.gcode.3mf')
        self.assertEqual(name('C:\\Users\\x\\Benchy.gcode.3mf'),'Benchy.gcode.3mf')
        self.assertEqual(name(''),'print.gcode.3mf')
        self.assertLessEqual(len(name('x'*300+'.3mf')),80+len('.gcode.3mf'))

    def test_old_pm_names_fall_back_to_the_job_name(self):
        job=self.store.add('P','Phone stand','/tmp/a.3mf','pm_0123456789ab.gcode.3mf',options(),'t')
        self.assertEqual(job['remote'],'Phone_stand.gcode.3mf')


if __name__=='__main__':
    unittest.main()
