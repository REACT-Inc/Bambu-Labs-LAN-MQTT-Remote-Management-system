"""More Slicer tab features (docs/slicer.md): OBJ and 3MF import, saved settings ("My settings"), OrcaSlicer's own
arrange and auto-orient, and the layer-by-layer G-code preview."""
import asyncio,io,json,os,shutil,subprocess,unittest,zipfile
from pathlib import Path

from slicer import imports,meshes,worker
from tests import test_slicer as base

ROOT=Path(__file__).parents[1]
NODE=shutil.which('node')
CORE='http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD='http://schemas.microsoft.com/3dmanufacturing/production/2015/06'
TETRA='<vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/></vertices>' \
      '<triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/><triangle v1="1" v2="2" v3="3"/><triangle v1="0" v2="3" v3="2"/></triangles>'


def model(body,unit='millimeter',extra=''):
    return f'<?xml version="1.0"?><model unit="{unit}" xmlns="{CORE}" xmlns:p="{PROD}" {extra}>{body}</model>'


def make_3mf(parts,start='3D/3dmodel.model'):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('_rels/.rels',f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                 f'<Relationship Target="/{start}" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        for path,text in parts.items():z.writestr(path,text)
    return stream.getvalue()


class ImportTests(unittest.TestCase):
    def test_obj(self):
        text=b'# a square and a triangle\nv 0 0 0\nv 10 0 0\nv 10 10 0\nv 0 10 0\nvt 0 0\nvn 0 0 1\nf 1/1/1 2/1/1 3/1/1 4/1/1\nf -4 -3 -1\n'
        [(name,values)]=imports.read('Part A.obj',text)
        self.assertEqual((name,len(values)//9),('Part A',3))   # the quad is two triangles
        self.assertEqual(meshes.bounds(values),((0,0,0),(10,10,0)))
        for bad,why in ((b'v 0 0 0\n','no faces'),(b'v 0 0 0\nf 1 2 3\n','missing vertex'),(b'v 0 nan 0\nf 1 1 1\n','invalid'),(b'v 1 2\n','broken')):
            with self.assertRaisesRegex(meshes.MeshError,why):imports.read('x.obj',bad)
        with self.assertRaisesRegex(meshes.MeshError,'.stl, .obj or .3mf'):imports.read('x.step',b'')

    def test_3mf_units_transforms_and_several_objects(self):
        data=make_3mf({'3D/3dmodel.model':model(f'<resources><object id="1" name="Tetra" type="model"><mesh>{TETRA}</mesh></object>'
                                                f'<object id="2" name="Second" type="model"><mesh>{TETRA}</mesh></object></resources>'
                                                '<build><item objectid="1" transform="2 0 0 0 2 0 0 0 2 100 0 0"/><item objectid="2"/></build>',unit='inch')})
        objects=imports.read('two.3mf',data)
        self.assertEqual([(n,len(v)//9) for n,v in objects],[('Tetra',4),('Second',4)])
        low,high=meshes.bounds(objects[0][1])
        self.assertEqual([round(v,3) for v in low+high],[100,0,0,150.8,50.8,50.8])   # inches -> mm, scaled 2x, moved 100
        self.assertEqual([round(v,3) for v in meshes.bounds(objects[1][1])[1]],[25.4,25.4,25.4])

    def test_3mf_components_in_other_model_files(self):
        """The way Bambu Studio and OrcaSlicer save projects: build items point to components in 3D/Objects/*.model."""
        part=model(f'<resources><object id="7" type="model"><mesh>{TETRA}</mesh></object></resources><build/>')
        main=model('<resources><object id="1" name="Bracket" type="model"><components>'
                   '<component p:path="/3D/Objects/object_1.model" objectid="7" transform="1 0 0 0 1 0 0 0 1 0 0 5"/>'
                   '<component p:path="/3D/Objects/object_1.model" objectid="7" transform="1 0 0 0 1 0 0 0 1 3 0 0"/></components></object></resources>'
                   '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 128 128 0"/></build>')
        [(name,values)]=imports.read('project.3mf',make_3mf({'3D/3dmodel.model':main,'3D/Objects/object_1.model':part}))
        self.assertEqual((name,len(values)//9),('Bracket',8))
        self.assertEqual([round(v,3) for v in sum(meshes.bounds(values),())],[128,128,0,132,129,6])

    def test_3mf_refusals(self):
        loop=model('<resources><object id="1"><components><component objectid="1"/></components></object></resources><build><item objectid="1"/></build>')
        cases=[(make_3mf({'3D/3dmodel.model':'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "a">]>'+model('')}),'not allowed'),
               (make_3mf({'3D/3dmodel.model':model('<resources/><build><item objectid="9"/></build>')}),'missing'),
               (make_3mf({'3D/3dmodel.model':loop}),'nests'),
               (make_3mf({'3D/3dmodel.model':model('<resources/><build/>')}),'no objects'),
               (make_3mf({'3D/3dmodel.model':model('',unit='parsec')}),'unknown unit'),
               (make_3mf({'3D/3dmodel.model':model('<resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
                                                   '<triangles><triangle v1="0" v2="0" v3="4"/></triangles></mesh></object></resources><build><item objectid="1"/></build>')}),'missing vertex'),
               (make_3mf({'3D/3dmodel.model':'<model'}),'broken XML'),(b'not a zip','readable')]
        for data,why in cases:
            with self.assertRaisesRegex(meshes.MeshError,why):imports.read('bad.3mf',data)


class WebTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard and a worker, set up as in test_slicer, for the new routes and options."""
    asyncSetUp,asyncTearDown=base.DashboardTests.asyncSetUp,base.DashboardTests.asyncTearDown
    enroll,upload,plate=base.DashboardTests.enroll,base.DashboardTests.upload,base.DashboardTests.plate

    async def upload_file(self,data,name):
        from aiohttp import FormData
        form=FormData();form.add_field('file',data,filename=name)
        return await self.client.post('/api/slicer/models',data=form,headers=self.headers)

    async def test_obj_and_3mf_uploads(self):
        r=await self.upload_file(b'v 0 0 0\nv 20 0 0\nv 0 20 0\nv 0 0 20\nf 1 3 2\nf 1 2 4\nf 2 3 4\nf 1 4 3\n','tetra.obj')
        [obj]=(await r.json())['models']
        self.assertEqual((obj['name'],obj['triangles'],obj['size']),('tetra.obj',4,[20.0,20.0,20.0]))
        stl=await (await self.client.get(f"/api/slicer/models/{obj['id']}")).read()
        self.assertEqual(meshes.summary(stl)['size'],[20.0,20.0,20.0])   # what the 3D view loads is what's sliced
        data=make_3mf({'3D/3dmodel.model':model(f'<resources><object id="1" name="A"><mesh>{TETRA}</mesh></object><object id="2" name="B"><mesh>{TETRA}</mesh></object></resources>'
                                                '<build><item objectid="1"/><item objectid="2"/></build>')})
        models=(await (await self.upload_file(data,'pair.3mf')).json())['models']
        self.assertEqual([m['name'] for m in models],['A','B'])
        self.assertEqual((await self.client.get('/api/slicer/models/'+'0'*64)).status,404)
        self.assertEqual((await self.client.get('/api/slicer/models/../x')).status,404)
        r=await self.upload_file(make_3mf({'3D/3dmodel.model':model('<resources/><build/>')}),'empty.3mf')
        self.assertEqual(r.status,400);self.assertIn('no objects',(await r.json())['error'])

    async def test_my_settings(self):
        body=dict(name='Strong PETG',settings=dict(machine=base.MACHINE,process=base.PROCESS,filament=base.FILAMENT),
                  changes={'wall_loops':4,'sparse_infill_density':30,'enable_support':True},options={'orient':True})
        presets=(await (await self.client.post('/api/slicer/presets',json=body,headers=self.headers)).json())['presets']
        self.assertEqual([(p['name'],p['changes']['wall_loops'],p['options']) for p in presets],[('Strong PETG',4,{'arrange':False,'orient':True})])
        body['changes']['wall_loops']=5
        await self.client.post('/api/slicer/presets',json=body,headers=self.headers)   # same name: replaced
        presets=(await (await self.client.get('/api/slicer/presets')).json())['presets']
        self.assertEqual([p['changes']['wall_loops'] for p in presets],[5])
        for bad in (dict(body,name=''),dict(body,changes={'start_gcode':'M112'}),dict(body,changes={'wall_loops':500}),dict(body,options={'orient':'yes'})):
            self.assertEqual((await self.client.post('/api/slicer/presets',json=bad,headers=self.headers)).status,400,bad)
        presets=(await (await self.client.post('/api/slicer/presets/delete',json={'name':'Strong PETG'},headers=self.headers)).json())['presets']
        self.assertEqual(presets,[])

    async def test_options_reach_orcaslicer_and_the_gcode_preview(self):
        auth=await self.enroll();bot=worker.Worker(self.worker_config);await asyncio.to_thread(bot.heartbeat)
        model_id=(await (await self.upload()).json())['models'][0]['id']
        r=await self.client.post('/api/slicer/jobs',json=self.plate(model_id,options={'orient':1}),headers=self.headers)
        self.assertEqual(r.status,400)   # options are true or false
        job=await (await self.client.post('/api/slicer/jobs',json=self.plate(model_id,options={'orient':True,'arrange':True}),headers=self.headers)).json()
        claimed=(await (await self.client.post('/api/slicer/worker/claim',json={},headers=auth)).json())['job']
        self.assertEqual(claimed['options'],{'arrange':True,'orient':True})
        log=Path(self.tmp.name)/'orca.json';os.environ['FAKE_ORCA_LOG']=str(log)
        try:
            with bot.lock:bot.running[job['id']]=True
            await asyncio.to_thread(bot.work,claimed)
        finally:os.environ.pop('FAKE_ORCA_LOG',None)
        self.assertEqual(json.loads(log.read_text())['args'][:6],['--arrange','1','--orient','1','--slice','0'])
        r=await self.client.get(f"/api/slicer/jobs/{job['id']}/gcode",headers={'Accept-Encoding':'gzip'})
        self.assertEqual((r.status,r.headers.get('Content-Encoding')),(200,'gzip'))
        self.assertEqual(await r.text(),'; sliced\nG28\n')
        other=await (await self.client.post('/api/slicer/jobs',json=self.plate(model_id),headers=self.headers)).json()
        self.assertEqual((await self.client.get(f"/api/slicer/jobs/{other['id']}/gcode")).status,400)   # not sliced yet


class PreviewTests(unittest.TestCase):
    """parseGcode() from static/slicer.js, run in node."""
    def run_parser(self,gcode):
        script=(ROOT/'static/slicer.js').read_text()
        start=script.index('const FEATURES=');end=script.index('async function openPreview(')
        js=script[start:end]+f'\nconst r=parseGcode({json.dumps(gcode)});console.log(JSON.stringify({{count:r.count,layers:r.layers.map(l=>[l.start,l.end,+l.z.toFixed(2)]),'\
           'names:r.names,feat:Array.from(r.feat.subarray(0,r.count)),first:Array.from(r.pos.subarray(0,6)),truncated:r.truncated}));'
        out=subprocess.run([NODE,'-e',js],capture_output=True,text=True,timeout=60)
        self.assertEqual(out.returncode,0,out.stderr)
        return json.loads(out.stdout)

    @unittest.skipUnless(NODE,'node is not installed')
    def test_orcaslicer_bambu_gcode(self):
        gcode='\n'.join(['M83','G90','G1 X5 Y5 E2 ; purge before any layer','; CHANGE_LAYER','; Z_HEIGHT: 0.2','G1 Z.2 F600','; FEATURE: Outer wall',
                         'G1 X0 Y0 F3000','G1 X10 Y0 E.5','G1 X10 Y10 E.5','; FEATURE: Sparse infill','G1 X0 Y10 E.5','G1 X0 Y0','; CHANGE_LAYER',
                         'G1 Z.4','; FEATURE: Outer wall','G3 X0 Y10 I0 J5 E1.2','G1 X5 Y10 E0 ; no extrusion: a travel'])
        r=self.run_parser(gcode)
        names=[r['names'][i] for i in sorted(set(r['feat']))]
        self.assertEqual(sorted(names),['Custom','Outer wall','Sparse infill'])
        z=[l[2] for l in r['layers']];self.assertEqual(z[-2:],[0.2,0.4])
        arc=r['layers'][-1];self.assertGreater(arc[1]-arc[0],2)   # the arc is split into short lines
        self.assertEqual(r['first'],[0,0,0,5,5,0])                  # the purge line before the first layer is kept

    @unittest.skipUnless(NODE,'node is not installed')
    def test_other_slicers_absolute_extrusion_and_no_layer_tags(self):
        compat='\n'.join(['M82',';LAYER_CHANGE',';TYPE:Skirt','G1 Z0.3','G1 X0 Y0','G1 X20 Y0 E1','G1 X20 Y20 E2',';LAYER_CHANGE',';TYPE:Support','G1 Z0.6','G1 X0 Y20 E3','G92 E0','G1 X0 Y0 E1'])
        r=self.run_parser(compat)
        self.assertEqual((r['count'],[l[2] for l in r['layers']],[r['names'][i] for i in r['feat']]),(4,[0.3,0.6],['Skirt','Skirt','Support','Support']))
        untagged='\n'.join(['M83','G1 Z0.2','G1 X0 Y0','G1 X10 Y0 E1','G1 Z0.4','G1 X0 Y0 E1','G1 Z0.6','G1 X10 Y0','G1 X0 Y0 E1'])
        self.assertEqual([l[2] for l in self.run_parser(untagged)['layers']],[0.2,0.4,0.6])   # a new layer whenever Z rises


class PageTests(unittest.TestCase):
    def test_new_controls(self):
        page,script=(ROOT/'static/index.html').read_text(),(ROOT/'static/slicer.js').read_text()
        self.assertIn('accept=".stl,.obj,.3mf"',page)
        for element in ('slicerLayFlat','slicerMirrorX','slicerMirrorY','slicerCopies','slicerPreview','slicerLayer','slicerLegend','slicerPreviewClose',
                        'slicerMine','slicerSaveMine','slicerDeleteMine','slicerAutoOrient','slicerAutoArrange'):
            self.assertIn(f'id="{element}"',page);self.assertIn(f"el('{element}')",script)
        self.assertIn("fetch('/api/slicer/models/'+info.id)",script)

    def test_worker_version(self):
        self.assertGreaterEqual(tuple(map(int,worker.VERSION.split('.'))),(1,1,0))   # the options need 1.1.0 or newer: slicer-worker update


if __name__=='__main__':
    unittest.main()
