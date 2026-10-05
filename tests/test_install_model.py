"""Installing a new failure model (an Ultralytics Platform Hailo export): metadata, hailortcli checks, test run,
backups, the config change, verification after restart and automatic rollback."""
import io,json,os,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection import install_model as m

METADATA='''description: Ultralytics best model trained on /content/failure-data/data.yaml
date: '2026-10-05T03:04:02.441137+00:00'
version: 8.4.173
task: detect
imgsz:
- 640
- 640
names:
  0: spaghetti
  1: stringing
  2: warping
args:
  data: coco128.yaml
  conf: 0.25
  iou: 0.7
channels: 3
hailo_arch: hailo8
nms: true
'''
NMS={'nms_scores_th':0.25,'nms_iou_th':0.7,'image_dims':[640,640],'max_proposals_per_class':100,'classes':3,
     'bbox_decoders':[{'name':'bbox_decoder_8','stride':8}]}
PARSE_HEF='''Architecture HEF was compiled for: HAILO8
Network group name: best, Multi Context - Number of contexts: 2
    Network name: best/best
        VStream infos:
            Input  best/input_layer1 UINT8, NHWC(640x640x3)
            Output best/yolov8_nms_postprocess FLOAT32, HAILO NMS BY CLASS(number of classes: 3, maximum bounding boxes per class: 100, maximum frame size: 4812)
'''
IDENTIFY='''Executing on device: 0001:01:00.0
Identifying board
Control Protocol Version: 2
Firmware Version: 4.23.0 (release,app,extended context switch buffer)
Board Name: Hailo-8
Device Architecture: HAILO8
'''


def export_zip(folder,metadata=METADATA,nms=NMS,model='best.hef'):
    path=Path(folder)/'best.zip'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('best_hailo_model/'+model,b'HEF-new')
        if metadata is not None:archive.writestr('best_hailo_model/metadata.yaml',metadata)
        if nms is not None:archive.writestr('best_hailo_model/nms_config.json',json.dumps(nms))
    return path


class FakeSystem:
    """hailortcli, systemctl and journalctl as on the team Pi."""
    def __init__(self,parse_hef=PARSE_HEF,identify=IDENTIFY,journal=('AI model ready: print_failure.hef on AI HAT (Hailo-8)',)):
        self.parse_hef,self.identify,self.journal,self.calls=parse_hef,identify,list(journal),[]
        self.restarts=0
    def __call__(self,command,timeout=60,**extra):
        self.calls.append(command)
        ok=lambda out='':subprocess.CompletedProcess(command,0,out,'')
        if command[:2]==['hailortcli','--version']:return ok('HailoRT-CLI version 4.23.0\n')
        if command[:3]==['hailortcli','fw-control','identify']:return ok(self.identify)
        if command[:2]==['hailortcli','parse-hef']:return ok(self.parse_hef)
        if command[:2]==['systemctl','restart']:self.restarts+=1;return ok()
        if command[:2]==['systemctl','is-failed']:return subprocess.CompletedProcess(command,1,'','')
        if command[0]=='journalctl':
            line=self.journal[min(self.restarts,len(self.journal))-1] if self.journal else ''
            return ok('Starting\n'+line+'\n')
        return subprocess.CompletedProcess(command,1,'','unknown')


class MetadataTests(unittest.TestCase):
    def test_reads_the_ultralytics_export(self):
        with tempfile.TemporaryDirectory() as d,tempfile.TemporaryDirectory() as work:
            model,meta=m.unpack(export_zip(d),work)
            self.assertEqual(model.name,'best.hef');self.assertEqual(model.read_bytes(),b'HEF-new')
        self.assertEqual(meta['classes'],['spaghetti','stringing','warping'])
        self.assertEqual((meta['input_size'],meta['hailo_arch'],meta['calibration']),(640,'hailo8','coco128.yaml'))
        self.assertEqual((meta['nms_score_threshold'],meta['nms_iou_threshold'],meta['nms_max_boxes']),(0.25,0.7,100))
        self.assertEqual((meta['ultralytics'],meta['exported']),('8.4.173','2026-10-05T03:04:02.441137+00:00'))

    def test_class_order_follows_the_numbers(self):
        data=m.parse_yaml('names:\n  2: warping\n  0: spaghetti\n  1: stringing\n')
        self.assertEqual(m.class_names(data['names']),['spaghetti','stringing','warping'])
        with self.assertRaises(m.Refused):m.class_names({0:'a',2:'b'})
        self.assertEqual(m.class_names(['a','b']),['a','b'])

    def test_yaml_subset(self):
        data=m.parse_yaml("a:\n  b:\n    c: 1\n  d: [1, 2]\ne: 'x: y'\nf:\n  - 3\n  - 4\ng: true # comment\n")
        self.assertEqual(data,{'a':{'b':{'c':1},'d':[1,2]},'e':'x: y','f':[3,4],'g':True})

    def test_refuses_unusable_files(self):
        with tempfile.TemporaryDirectory() as d,tempfile.TemporaryDirectory() as work:
            pt=Path(d)/'best.pt';pt.write_bytes(b'x')
            with self.assertRaisesRegex(m.Refused,'exported first'):m.unpack(pt,work)
            bad=Path(d)/'evil.zip'
            with zipfile.ZipFile(bad,'w') as archive:archive.writestr('../../etc/evil.hef',b'x')
            with self.assertRaisesRegex(m.Refused,'unsafe path'):m.unpack(bad,work)
            empty=Path(d)/'empty.zip'
            with zipfile.ZipFile(empty,'w') as archive:archive.writestr('readme.txt','x')
            with self.assertRaisesRegex(m.Refused,'no .hef or .onnx'):m.unpack(empty,work)

    def test_plain_onnx(self):
        with tempfile.TemporaryDirectory() as d,tempfile.TemporaryDirectory() as work:
            onnx=Path(d)/'best.onnx';onnx.write_bytes(b'onnx')
            model,meta=m.unpack(onnx,work)
        self.assertEqual((model.name,meta),('best.onnx',{'source':'best.onnx'}))


class HefTests(unittest.TestCase):
    def test_parse_hef(self):
        hef=m.parse_hef(PARSE_HEF)
        self.assertEqual(hef['arch'],'hailo8')
        self.assertEqual(hef['inputs'],[{'type':'UINT8','order':'NHWC','height':640,'width':640,'channels':3}])
        self.assertTrue(hef['nms']);self.assertEqual((hef['classes'],hef['max_boxes']),(3,100))

    def test_system_info(self):
        with patch.object(m,'run',FakeSystem()):info=m.system_info()
        self.assertEqual((info['chip'],info['hailort']),('hailo8','4.23.0'))
        self.assertTrue(info['firmware'].startswith('4.23.0'))

    def check(self,parse_hef=PARSE_HEF,chip='hailo8',classes=('spaghetti','stringing','warping')):
        with patch.object(m,'run',FakeSystem(parse_hef)):return m.check_hef('best.hef',list(classes),{'chip':chip})

    def test_accepts_the_platform_export(self):
        self.assertEqual(self.check()['inputs'][0]['width'],640)

    def test_refuses_what_cannot_work(self):
        with self.assertRaisesRegex(m.Refused,'compiled for HAILO8, this AI HAT is HAILO8L'):self.check(chip='hailo8l')
        with self.assertRaisesRegex(m.Refused,'no working AI HAT'):self.check(chip='')
        with self.assertRaisesRegex(m.Refused,'3 classes but 2 names'):self.check(classes=('spaghetti','warping'))
        with self.assertRaisesRegex(m.Refused,'no Hailo NMS'):self.check(PARSE_HEF.replace('HAILO NMS BY CLASS','NHWC'))
        with self.assertRaisesRegex(m.Refused,'unexpected input'):self.check(PARSE_HEF.replace('UINT8, NHWC(640x640x3)','FLOAT32, NCHW(3x640x640)'))

    def test_hailo8l_model_on_hailo8_is_allowed(self):
        with patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(self.check(PARSE_HEF.replace('compiled for: HAILO8','compiled for: HAILO8L'))['arch'],'hailo8l')
        self.assertIn('slower',out.getvalue())


class TestRunTests(unittest.TestCase):
    def test_runs_every_picture_through_the_helper(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'hailo_worker.py').write_text(
                'import json,sys\nprint(json.dumps({"ready":True,"backend":"hailo"}),flush=True)\n'
                'for n,line in enumerate(sys.stdin):\n    print(json.dumps({"score":n/10,"detections":[]}),flush=True)\n')
            with patch.object(m,'HERE',Path(d)),patch.object(m.os,'geteuid',lambda:1000):
                backend,scores=m.test_run(sys.executable,'x.hef',['spaghetti'],['spaghetti'],640,[b'a',b'b',b'c'],30)
        self.assertEqual((backend,scores),('hailo',[0.0,0.1,0.2]))

    def test_reports_a_helper_that_does_not_start(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'hailo_worker.py').write_text('import sys\nprint("HAILO_OUT_OF_PHYSICAL_DEVICES(74)",file=sys.stderr);sys.exit(1)\n')
            with patch.object(m,'HERE',Path(d)),patch.object(m.os,'geteuid',lambda:1000):
                with self.assertRaisesRegex(m.Refused,'does not run: HAILO_OUT_OF_PHYSICAL_DEVICES'):
                    m.test_run(sys.executable,'x.hef',['a'],['a'],640,[b'a'],30)

    def test_labels_and_section(self):
        self.assertEqual(m.trigger_labels(['spaghetti','stringing','warping'],['warping','blob']),['warping'])
        self.assertEqual(m.trigger_labels(['spaghetti','stringing','warping'],[]),['spaghetti','warping'])
        self.assertEqual(m.trigger_labels(['fail'],['spaghetti']),['fail'])
        same={'model':'/m/print_failure.onnx','classes':['spaghetti','stringing','warping'],'labels':['spaghetti'],'threshold':0.4}
        self.assertEqual(m.new_section(same,'/m/print_failure.hef',same['classes'],640),{**same,'model':'/m/print_failure.hef'})
        changed=m.new_section(same,'/m/x.hef',['fail','ok'],320)
        self.assertEqual((changed['classes'],changed['labels'],changed['input_size']),(['fail','ok'],['fail','ok'],320))


class InstallTests(unittest.TestCase):
    """main() end to end on a fake Pi: models folder, config.json, hailortcli, systemd."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.models=root/'models';self.models.mkdir()
        self.onnx=self.models/'print_failure.onnx';self.onnx.write_bytes(b'ONNX-old')
        self.config=root/'config.json'
        self.section={'enabled':True,'model':str(self.onnx),'classes':['spaghetti','stringing','warping'],'labels':['spaghetti','warping'],
                      'threshold':0.4,'action':'pause'}
        self.config.write_text(json.dumps({'printers':[{'name':'A1'}],'failure_detection':self.section}))
        os.chmod(self.config,0o640)
        self.export=export_zip(root)
        self.scores=[0.0]
    def tearDown(self):self.tmp.cleanup()

    def main(self,*extra,system=None,scores=None):
        self.system=system or FakeSystem()
        fake_test=lambda python,model,classes,labels,size,pictures,timeout:('hailo',(scores or [0.0]*len(pictures))[:len(pictures)])
        out=io.StringIO()
        with patch.object(m,'run',self.system),patch.object(m,'test_run',fake_test),patch.object(m,'blank_picture',lambda:b'jpeg'),\
             patch.object(m.os,'geteuid',lambda:0),patch.object(m.time,'sleep',lambda s:None),patch('sys.stdout',out):
            code=m.main([*extra,'--config',str(self.config),'--models',str(self.models),'--wait','10'])
        return code,out.getvalue()

    def installed_config(self):return json.loads(self.config.read_text())['failure_detection']

    def test_installs_backs_up_and_verifies(self):
        code,out=self.main(str(self.export))
        self.assertEqual(code,0,out)
        hef=self.models/'print_failure.hef'
        self.assertEqual(hef.read_bytes(),b'HEF-new');self.assertEqual(hef.stat().st_mode&0o777,0o644)
        self.assertEqual(self.installed_config(),{**self.section,'model':str(hef)})   # only the model changed
        self.assertEqual(self.config.stat().st_mode&0o777,0o640)
        backups=list(self.config.parent.glob('config.json.backup-*'))
        self.assertEqual(len(backups),1);self.assertEqual(json.loads(backups[0].read_text())['failure_detection'],self.section)
        record=json.loads((self.models/'print_failure.hef.json').read_text())
        self.assertEqual(record['classes'],['spaghetti','stringing','warping'])
        self.assertEqual((record['export']['calibration'],record['export']['nms_score_threshold']),('coco128.yaml',0.25))
        self.assertEqual((record['system']['hailort'],record['hef']['arch'],record['test']['backend']),('4.23.0','hailo8','hailo'))
        self.assertEqual(len(record['sha256']),64)
        self.assertEqual(self.onnx.read_bytes(),b'ONNX-old')   # the CPU model stays as the fallback
        self.assertIn('DONE: AI model ready: print_failure.hef on AI HAT',out)
        self.assertIn('on-chip NMS drops detections below 0.25',out)

    def test_replacing_a_model_keeps_a_numbered_backup(self):
        (self.models/'print_failure.hef').write_bytes(b'HEF-old')
        self.assertEqual(self.main(str(self.export))[0],0)
        self.assertEqual(self.main(str(self.export))[0],0)
        backups=sorted((self.models/'backups').glob('print_failure.hef.*'))
        self.assertEqual(len(backups),2);self.assertEqual(backups[0].read_bytes(),b'HEF-old')

    def test_rolls_back_when_the_app_falls_back(self):
        (self.models/'print_failure.hef').write_bytes(b'HEF-old')
        system=FakeSystem(journal=('AI model ready: print_failure.onnx on CPU (fallback)','AI model ready: print_failure.onnx on CPU'))
        code,out=self.main(str(self.export),system=system)
        self.assertEqual(code,1)
        self.assertIn('FAILED: the app fell back',out);self.assertIn('rolled back',out)
        self.assertEqual((self.models/'print_failure.hef').read_bytes(),b'HEF-old')
        self.assertEqual(self.installed_config(),self.section)
        self.assertEqual(system.restarts,2)
        self.assertEqual(m.load_history(self.models),[])

    def test_rolls_back_a_first_hef_by_removing_it(self):
        system=FakeSystem(journal=('AI model did not start: HAILO_OUT_OF_PHYSICAL_DEVICES','AI model ready: print_failure.onnx on CPU'))
        code,out=self.main(str(self.export),system=system)
        self.assertEqual(code,1);self.assertFalse((self.models/'print_failure.hef').exists())
        self.assertEqual(self.installed_config(),self.section)

    def test_rollback_option_undoes_the_last_install(self):
        self.assertEqual(self.main(str(self.export))[0],0)
        system=FakeSystem(journal=('AI model ready: print_failure.onnx on CPU',))
        code,out=self.main('--rollback',system=system)
        self.assertEqual(code,0,out)
        self.assertEqual(self.installed_config(),self.section);self.assertFalse((self.models/'print_failure.hef').exists())
        self.assertIn('service restarted: AI model ready: print_failure.onnx',out)
        self.assertEqual(self.main('--rollback')[0],1)   # nothing left to undo

    def test_check_only_changes_nothing(self):
        before=self.config.read_text()
        code,out=self.main(str(self.export),'--check-only')
        self.assertEqual(code,0,out);self.assertIn('nothing changed',out)
        self.assertEqual(self.config.read_text(),before);self.assertFalse((self.models/'print_failure.hef').exists())
        self.assertFalse([c for c in self.system.calls if c[0]=='systemctl'])

    def test_refused_model_changes_nothing(self):
        system=FakeSystem(identify='Device Architecture: HAILO8L\n')
        code,out=self.main(str(self.export),system=system)
        self.assertEqual(code,1);self.assertIn('NOT installed: the model was compiled for HAILO8',out)
        self.assertFalse((self.models/'print_failure.hef').exists());self.assertEqual(self.installed_config(),self.section)

    def test_measures_failure_and_healthy_pictures(self):
        root=Path(self.tmp.name)
        for folder,count in (('failed',3),('good',2)):
            (root/folder).mkdir()
            for n in range(count):(root/folder/f'{n}.jpg').write_bytes(b'x')
        # blank, 3 failures (two caught at 0.4), 2 healthy (one false alarm)
        scores=[0.0,0.9,0.5,0.1,0.6,0.0]
        code,out=self.main(str(self.export),'--failure',str(root/'failed'),'--healthy',str(root/'good'),'--check-only',scores=scores)
        self.assertEqual(code,0,out)
        self.assertIn('caught 2 of 3 at threshold 0.40',out);self.assertIn('falsely flagged 1 of 2',out)
        code,out=self.main(str(self.export),'--failure',str(root/'failed'),'--min-catch','0.9',scores=scores)
        self.assertEqual(code,1);self.assertIn('below --min-catch',out)
        self.assertFalse((self.models/'print_failure.hef').exists())

    def test_needs_root_to_install(self):
        with patch.object(m.os,'geteuid',lambda:1000),patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(m.main([str(self.export),'--config',str(self.config),'--models',str(self.models)]),2)
        self.assertIn('run with sudo',out.getvalue())


if __name__=='__main__':
    unittest.main()
