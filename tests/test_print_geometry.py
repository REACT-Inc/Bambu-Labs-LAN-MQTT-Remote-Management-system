"""Comparing AI detections with the print file (#79): G-code geometry, camera calibration, on/off-part weighting."""
import asyncio,json,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection import print_geometry as pg
from failureDetection.detection import GeometryCheck,FailureMonitor,settings_for
from failureDetection.hailo_worker import to_picture

# A 20 x 20 mm square at X/Y 100-120, three layers tall, on a 256 mm bed; plus a front prime line.
GCODE='''; HEADER_BLOCK_START
M83
G90
G1 X10 Y2 F3000
G1 X60 Y2 E5 ; prime line
; CHANGE_LAYER
; layer num/total_layer_count: 1/3
G1 Z0.2
G1 X100 Y100
G1 X120 Y100 E1
G1 X120 Y120 E1
G1 X100 Y120 E1
G1 X100 Y100 E1
G1 X110 Y110 E-0.8 ; wipe, not printed
; CHANGE_LAYER
; layer num/total_layer_count: 2/3
G1 Z0.4
G1 X100 Y100
G1 X120 Y120 E2
; CHANGE_LAYER
; layer num/total_layer_count: 3/3
G1 Z10
G1 X110 Y100 E1
G1 X200 Y200 ; travel only
; CONFIG_BLOCK_START
; printable_area = 0x0,256x0,256x256,0x256
'''
# Camera looking at the bed: front-left bottom-left of the picture, back edge narrower (perspective).
CORNERS=[[0.1,0.9],[0.9,0.9],[0.75,0.2],[0.25,0.2]]


def bed_to_picture(x,y):
    calibration=pg.Calibration(CORNERS,(0,0,256,256))
    return pg.apply(calibration.to_picture,x,y)


def box_around(x,y,half=4):
    corners=[bed_to_picture(x+dx,y+dy) for dx in (-half,half) for dy in (-half,half)]
    return [min(c[0] for c in corners),min(c[1] for c in corners),max(c[0] for c in corners),max(c[1] for c in corners)]


class GcodeTests(unittest.TestCase):
    def setUp(self):self.geometry=pg.parse_gcode(GCODE.splitlines())

    def test_layers_heights_and_bed(self):
        g=self.geometry
        self.assertEqual((g.layers,g.width,g.depth),(3,256.0,256.0))
        self.assertEqual(g.layer_z,{1:0.2,2:0.4,3:10.0});self.assertEqual(g.height(2),0.4)

    def test_extrusion_marks_travel_and_wipes_dont(self):
        g=self.geometry
        self.assertTrue(g.covered(110,100,1));self.assertTrue(g.covered(30,2,1))   # square edge, prime line
        self.assertFalse(g.covered(110,110,1))   # inside the square: only the wipe went there on layer 1
        self.assertTrue(g.covered(110,110,2))    # the layer-2 diagonal crosses the middle
        self.assertFalse(g.covered(160,160,3))   # travel only
        self.assertFalse(g.covered(-5,5,3))      # off the bed

    def test_margin(self):
        self.assertFalse(self.geometry.covered(130,110,1,margin=5));self.assertTrue(self.geometry.covered(130,110,1,margin=12))

    def test_cache_roundtrip(self):
        again=pg.Geometry.from_json(json.loads(json.dumps(self.geometry.to_json())))
        self.assertEqual((again.first,again.layer_z,again.layers),(self.geometry.first,self.geometry.layer_z,3))

    def test_absolute_extrusion_and_other_slicers(self):
        g=pg.parse_gcode(['M82','G92 E0',';LAYER:0','G1 Z0.3','G1 X50 Y50','G1 X70 Y50 E1','G1 X70 Y70 E0.5','; printable_area = 0x0,180x0,180x180,0x180'])
        self.assertEqual((g.width,g.layers),(180.0,1));self.assertTrue(g.covered(60,50,1));self.assertFalse(g.covered(70,60,1))   # E went backwards

    def test_from_3mf(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'job.3mf'
            with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('Metadata/plate_2.gcode',GCODE)
            self.assertEqual(pg.from_3mf(path,2).layers,3)
            with self.assertRaisesRegex(ValueError,'plate 1'):pg.from_3mf(path,1)


class CalibrationTests(unittest.TestCase):
    def test_corners_map_both_ways(self):
        c=pg.Calibration(CORNERS,(0,0,256,256))
        for (u,v),bed in zip(CORNERS,[(0,0),(256,0),(256,256),(0,256)]):
            x,y=c.bed_point(u,v);self.assertAlmostEqual(x,bed[0],3);self.assertAlmostEqual(y,bed[1],3)
        x,y=c.bed_point(*bed_to_picture(110,110));self.assertAlmostEqual(x,110,3);self.assertAlmostEqual(y,110,3)

    def test_bad_corners_refused(self):
        with self.assertRaisesRegex(ValueError,'cross'):pg.Calibration([CORNERS[0],CORNERS[2],CORNERS[1],CORNERS[3]],(0,0,1,1))
        with self.assertRaisesRegex(ValueError,'four'):pg.Calibration(CORNERS[:3],(0,0,1,1))
        with self.assertRaises(ValueError):pg.Calibration([[0.1,0.1],[0.2,0.2],[0.3,0.3],[0.4,0.4]],(0,0,1,1))

    def test_inside_fraction(self):
        g=pg.parse_gcode(GCODE.splitlines());c=pg.Calibration(CORNERS,(0,0,256,256))
        self.assertGreaterEqual(pg.inside_fraction(box_around(110,100),g,c,1,margin=3),0.5)   # on the part's front wall: counts as on the part
        self.assertEqual(pg.inside_fraction(box_around(180,60),g,c,1,margin=3),0.0)       # empty bed
        # Taller part (10 mm at layer 3): the margin grows with height, so a box just beside it counts as on the part.
        self.assertEqual(pg.inside_fraction(box_around(130,110,2),g,c,2,margin=3),0.0)
        self.assertGreater(pg.inside_fraction(box_around(130,110,2),g,c,3,margin=3),0.9)


class WorkerBoxTests(unittest.TestCase):
    def test_letterbox_undone(self):
        # 1280x720 picture letterboxed into 640x640: scale 0.5, 140 px grey bars top and bottom.
        d=[{'label':'spaghetti','score':0.9,'box':[0.25,140/640,0.75,500/640]},{'label':'x','score':0.5,'box':None}]
        to_picture(d,640,640,0.5,0,140,1280,720)
        self.assertEqual(d[0]['box'],[0.25,0.0,0.75,1.0]);self.assertIsNone(d[1]['box'])


class FakeStore:
    def __init__(self,job):self.job=job
    def active(self,name):return self.job


class GeometryCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.asset=d/'job.3mf'
        with zipfile.ZipFile(self.asset,'w') as z:z.writestr('Metadata/plate_1.gcode',GCODE)
        saved={}
        self.core=SimpleNamespace(DATA_DIR=d,settings=saved,names=lambda:['H2D'],save_settings=lambda new:(saved.clear(),saved.update(new)))
        self.engine=SimpleNamespace(store=FakeStore({'id':'j1','asset':str(self.asset),'options':{'plate':1}}))
        self.settings=settings_for({'failure_detection':{'enabled':True,'classes':['spaghetti','stringing','warping'],'labels':['spaghetti','warping'],'threshold':0.4}})
        self.check=GeometryCheck(self.core,self.engine,self.settings)
    async def asyncTearDown(self):self.tmp.cleanup()

    async def loaded(self):
        self.assertIsNone(self.check.current('H2D'))   # starts the background read
        self.assertIn('Reading',self.check.state('H2D')['message'])
        await asyncio.wait_for(self.check.tasks['H2D'],30)
        return self.check.current('H2D')

    async def test_reads_the_job_file_in_the_background(self):
        g=await self.loaded()
        self.assertEqual(g.layers,3);self.assertIn('3 layers',self.check.state('H2D')['message'])
        self.assertTrue((Path(self.tmp.name)/'geometry'/'j1-1.json').is_file())   # cached for the rest of the print

    async def test_off_part_keeps_score_on_part_counts_less(self):
        await self.loaded();self.check.set_corners('H2D',CORNERS)
        off=[{'label':'spaghetti','score':0.8,'box':box_around(180,60)}]
        on=[{'label':'spaghetti','score':0.8,'box':box_around(110,100)}]
        self.assertEqual(self.check.adjust('H2D',0.8,off,1),(0.8,'outside where the print file puts plastic'))
        self.assertEqual(self.check.adjust('H2D',0.8,on,1),(0.4,'on the part itself (counted less)'))
        self.assertEqual(off[0]['on_part'],0.0)
        # Stringing isn't a failure label: it doesn't change anything.
        self.assertEqual(self.check.adjust('H2D',0.3,[{'label':'stringing','score':0.9,'box':box_around(180,60)}],1),(0.3,''))

    async def test_passes_through_without_calibration_file_or_boxes(self):
        det=[{'label':'spaghetti','score':0.8,'box':box_around(180,60)}]
        self.assertEqual(self.check.adjust('H2D',0.8,det,1),(0.8,''))   # file still being read
        await self.check.tasks['H2D']
        self.assertEqual(self.check.adjust('H2D',0.8,det,1),(0.8,''))   # not calibrated
        self.check.set_corners('H2D',CORNERS)
        self.assertEqual(self.check.adjust('H2D',0.8,[{'label':'spaghetti','score':0.8,'box':None}],1),(0.8,''))   # classification model
        self.engine.store.job=None
        self.assertEqual(self.check.adjust('H2D',0.8,det,1),(0.8,''))   # print not started by the app

    async def test_unreadable_file_reported(self):
        self.asset.write_bytes(b'not a zip')
        self.check.current('H2D');await self.check.tasks['H2D']
        self.assertIn('not used',self.check.state('H2D')['message'])

    async def test_calibration_saved_validated_and_cleared(self):
        self.check.set_corners('H2D',CORNERS)
        self.assertEqual(self.core.settings['ai_calibration']['H2D']['corners'],CORNERS);self.assertTrue(self.check.state('H2D')['calibrated'])
        with self.assertRaises(ValueError):self.check.set_corners('H2D',[CORNERS[0],CORNERS[2],CORNERS[1],CORNERS[3]])
        with self.assertRaises(ValueError):self.check.set_corners('Nope',CORNERS)
        self.check.set_corners('H2D',None);self.assertFalse(self.check.state('H2D')['calibrated'])

    async def test_can_be_turned_off(self):
        settings=settings_for({'failure_detection':{'enabled':True,'geometry':{'enabled':False}}})
        check=GeometryCheck(self.core,self.engine,settings)
        self.assertIsNone(check.current('H2D'));self.assertEqual(check.tasks,{})


class DashboardTests(unittest.TestCase):
    def test_calibration_endpoint(self):
        import inspect,dashboard
        self.assertIn("'/api/ai/{name}/calibration'",inspect.getsource(dashboard.Dashboard.__init__))


if __name__=='__main__':
    unittest.main()
