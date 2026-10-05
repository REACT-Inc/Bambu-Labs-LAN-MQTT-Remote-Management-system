"""Automatic AI setup (#75): HAT detection, model download rules, config writing, end-to-end with the real helper."""
import hashlib,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]))
from failureDetection import setup_ai


def asset(name,data):
    return {'name':name,'url':'https://api.github.com/x/'+name,'size':len(data),'digest':'sha256:'+hashlib.sha256(data).hexdigest(),'_data':data}


class FakeGitHub:
    def __init__(self,*assets):self.assets=list(assets);self.downloads=[]
    def api(self,url,token=''):
        if not self.assets:raise OSError('404')
        return {'assets':[{k:v for k,v in a.items() if k!='_data'} for a in self.assets]}
    def download(self,item,target,token=''):
        data=next(a['_data'] for a in self.assets if a['name']==item['name'])
        self.downloads.append(item['name']);Path(target).write_bytes(data);return hashlib.sha256(data).hexdigest()


class DetectionTests(unittest.TestCase):
    def test_hailo_found_by_pci_vendor(self):
        with tempfile.TemporaryDirectory() as d:
            for name,vendor in (('0000:01:00.0','0x14e4'),('0001:01:00.0','0x1e60')):
                (Path(d)/name).mkdir();(Path(d)/name/'vendor').write_text(vendor+'\n')
            self.assertEqual(setup_ai.hailo_device(Path(d)),'0001:01:00.0')
            (Path(d)/'0001:01:00.0'/'vendor').write_text('0x8086\n')
            self.assertEqual(setup_ai.hailo_device(Path(d)),'')

    def test_chip_from_identify(self):
        self.assertEqual(setup_ai.chip_from('Board Name: Hailo-8\nDevice Architecture: HAILO8\n'),'hailo8')
        self.assertEqual(setup_ai.chip_from('Device Architecture: HAILO8L'),'hailo8l')
        self.assertEqual(setup_ai.chip_from('Device Architecture: HAILO10H'),'hailo10h')
        self.assertEqual(setup_ai.chip_from(''),'')

    def test_only_with_a_camera_printer(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json'
            path.write_text(json.dumps({'printers':[{'name':'A','camera_type':''}]}));self.assertFalse(setup_ai.has_camera(path))
            path.write_text(json.dumps({'printers':[{'name':'A','camera_type':'rtsp'}]}));self.assertTrue(setup_ai.has_camera(path))


class ModelFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.models=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def fetch(self,github,chip=''):
        with patch.object(setup_ai,'api',github.api),patch.object(setup_ai,'download',github.download):
            return setup_ai.fetch_models('owner/repo',self.models,chip)

    def test_downloads_model_and_metadata(self):
        meta=json.dumps({'classes':['spaghetti','ok'],'labels':['spaghetti'],'threshold':0.5}).encode()
        github=FakeGitHub(asset('print_failure.onnx',b'model-1'),asset('print_failure.json',meta),asset('print_failure_hailo8.hef',b'hef'))
        result,found=self.fetch(github,'hailo8')
        self.assertEqual(result['classes'],['spaghetti','ok']);self.assertEqual(result['threshold'],0.5);self.assertEqual(result['input_size'],640)
        self.assertEqual(set(found),{'onnx','hef'});self.assertEqual((self.models/'print_failure.onnx').read_bytes(),b'model-1')

    def test_up_to_date_model_not_downloaded_again(self):
        github=FakeGitHub(asset('print_failure.onnx',b'model-1'))
        self.fetch(github);self.fetch(github)
        self.assertEqual(github.downloads,['print_failure.onnx'])

    def test_newer_release_model_replaces_the_downloaded_one(self):
        self.fetch(FakeGitHub(asset('print_failure.onnx',b'model-1')))
        self.fetch(FakeGitHub(asset('print_failure.onnx',b'model-2')))
        self.assertEqual((self.models/'print_failure.onnx').read_bytes(),b'model-2')

    def test_model_put_there_by_hand_is_never_replaced(self):
        (self.models/'print_failure.onnx').write_bytes(b'my own better model')
        github=FakeGitHub(asset('print_failure.onnx',b'release model'))
        _,found=self.fetch(github)
        self.assertEqual(github.downloads,[]);self.assertEqual((self.models/'print_failure.onnx').read_bytes(),b'my own better model')
        self.assertIn('onnx',found)

    def test_no_release_keeps_what_is_there(self):
        (self.models/'print_failure.onnx').write_bytes(b'local')
        meta,found=self.fetch(FakeGitHub())
        self.assertEqual(found,{'onnx':self.models/'print_failure.onnx'});self.assertEqual(meta['labels'],['spaghetti','warping'])

    def test_no_hef_without_a_working_hat(self):
        _,found=self.fetch(FakeGitHub(asset('print_failure.onnx',b'm'),asset('print_failure_hailo8.hef',b'h')),chip='')
        self.assertEqual(set(found),{'onnx'})


class DownloadTests(unittest.TestCase):
    def get(self,item,data):
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*a):return False
        with tempfile.TemporaryDirectory() as d,patch('urllib.request.urlopen',lambda *a,**k:Response(data)):
            target=Path(d)/'print_failure.onnx'
            try:return setup_ai.download(item,target),target.read_bytes()
            finally:self.assertEqual([p.name for p in Path(d).iterdir() if p.name.startswith('.download-')],[])   # no leftovers

    def test_checksum_and_size_checked(self):
        good=asset('print_failure.onnx',b'model')
        self.assertEqual(self.get(good,b'model')[1],b'model')
        with self.assertRaisesRegex(ValueError,'checksum'):self.get(good,b'tampr')
        with self.assertRaisesRegex(ValueError,'size'):self.get(dict(good,digest=''),b'model-longer')
        with self.assertRaisesRegex(ValueError,'larger'):self.get(dict(good,size=setup_ai.MAX_MODEL+1),b'x')


class ConfigTests(unittest.TestCase):
    def test_added_once_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json';path.write_text(json.dumps({'port':8080,'printers':[]}));os.chmod(path,0o640)
            self.assertTrue(setup_ai.configure(path,Path('/opt/m/print_failure.onnx'),dict(setup_ai.DEFAULT_META)))
            section=json.loads(path.read_text())['failure_detection']
            self.assertEqual((section['enabled'],section['action'],section['threshold'],section['labels']),(True,'notify',0.4,['spaghetti','warping']))
            self.assertEqual(os.stat(path).st_mode&0o777,0o640);self.assertEqual(json.loads(path.read_text())['port'],8080)
            mine=json.loads(path.read_text());mine['failure_detection']['action']='pause';path.write_text(json.dumps(mine))
            self.assertFalse(setup_ai.configure(path,Path('/other.onnx'),dict(setup_ai.DEFAULT_META)))
            self.assertEqual(json.loads(path.read_text())['failure_detection']['action'],'pause')


class EndToEndTests(unittest.TestCase):
    """main() with a camera printer: model from the release, tested through the real helper, then enabled."""
    def test_setup_enables_a_working_model(self):
        try:
            import cv2,onnx  # noqa: F401
            from onnx import helper,TensorProto,numpy_helper
            import numpy as np
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest('OpenCV/onnx/numpy/Pillow not installed')
        fixed=np.zeros((1,7,8400),dtype=np.float32)
        graph=helper.make_graph([helper.make_node('ReduceSum',['images'],['t'],keepdims=1),helper.make_node('Mul',['t','z'],['n']),helper.make_node('Add',['f','n'],['output0'])],
            'g',[helper.make_tensor_value_info('images',TensorProto.FLOAT,[1,3,640,640])],[helper.make_tensor_value_info('output0',TensorProto.FLOAT,[1,7,8400])],
            [numpy_helper.from_array(fixed,'f'),numpy_helper.from_array(np.zeros((1,1,1,1),'float32'),'z')])
        model=helper.make_model(graph,opset_imports=[helper.make_opsetid('',11)]);model.ir_version=7
        data=model.SerializeToString()
        with tempfile.TemporaryDirectory() as d:
            config=Path(d)/'config.json';config.write_text(json.dumps({'printers':[{'name':'H2D','camera_type':'rtsp'}]}))
            github=FakeGitHub(asset('print_failure.onnx',data))
            out=io.StringIO()
            with patch.object(setup_ai,'api',github.api),patch.object(setup_ai,'download',github.download),patch('sys.stdout',out):
                setup_ai.main(['--config',str(config),'--models',str(Path(d)/'models'),'--data',d,'--no-packages','--python',sys.executable])
            section=json.loads(config.read_text()).get('failure_detection')
            self.assertIsNotNone(section,out.getvalue())
            self.assertEqual(section['model'],str(Path(d)/'models'/'print_failure.onnx'))
            self.assertIn('works (ready, test picture scored 0.0)',out.getvalue())

    def test_broken_model_leaves_ai_off(self):
        with tempfile.TemporaryDirectory() as d:
            config=Path(d)/'config.json';config.write_text(json.dumps({'printers':[{'name':'H2D','camera_type':'rtsp'}]}))
            github=FakeGitHub(asset('print_failure.onnx',b'not a model'))
            out=io.StringIO()
            with patch.object(setup_ai,'api',github.api),patch.object(setup_ai,'download',github.download),patch('sys.stdout',out):
                setup_ai.main(['--config',str(config),'--models',str(Path(d)/'models'),'--data',d,'--no-packages','--python',sys.executable])
            self.assertNotIn('failure_detection',json.loads(config.read_text()))
            self.assertIn('stays off',out.getvalue())

    def test_no_camera_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            config=Path(d)/'config.json';config.write_text(json.dumps({'printers':[]}))
            with patch.object(setup_ai,'prepare_system') as prepare,patch('sys.stdout',io.StringIO()):
                setup_ai.main(['--config',str(config),'--data',d])
            prepare.assert_not_called()


if __name__=='__main__':
    unittest.main()
