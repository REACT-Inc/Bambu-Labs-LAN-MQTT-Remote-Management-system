"""The AI helper with the bed check model (hailo_worker.py), against a fake Hailo library: both models on one AI HAT,
only one active at a time, the failure model active again after each bed check, and failure detection unaffected
when the bed model can't load."""
import base64,io,json,sys,types,unittest
from types import SimpleNamespace
from unittest.mock import patch


def fake_hailo(fail_bed=False):
    device_state={'active':None,'log':[]}
    class HEF:
        def __init__(self,path):
            if fail_bed and 'clip' in path:raise RuntimeError('HEF was compiled for a different HailoRT')
            self.path=path;self.shape=(288,288,3) if 'clip' in path else (640,640,3)
        def get_input_vstream_infos(self):return [SimpleNamespace(name=self.path+'/in',shape=self.shape)]
    class Activation:
        def __init__(self,group):self.group=group
        def __enter__(self):
            assert device_state['active'] is None,'two network groups active at once'
            device_state['active']=self.group;device_state['log'].append(self.group.hef.path)
        def __exit__(self,*a):device_state['active']=None
    class Group:
        def __init__(self,hef):self.hef=hef
        def activate(self,params):return Activation(self)
        def create_params(self):return {}
    class Pipeline:
        def __init__(self,group,i,o):self.group=group
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def infer(self,inputs):
            import numpy as np
            assert device_state['active'] is self.group,'inference on an inactive network group'
            (name,batch),=inputs.items();assert batch.shape[1:3]==self.group.hef.shape[:2]
            if 'clip' in self.group.hef.path:return {'embedding':np.array([[3.0,4.0]+[0.0]*638],dtype=np.float32)}
            return {'nms':[[np.array([[0.1,0.2,0.3,0.4,0.9]])]]}
    class VDevice:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def configure(self,hef,params):return [Group(hef)]
        def get_physical_devices(self):
            return [SimpleNamespace(control=SimpleNamespace(identify=lambda:SimpleNamespace(device_architecture='HAILO8')))]
    module=types.ModuleType('hailo_platform')
    module.__version__='4.20.0'
    module.HEF,module.VDevice,module.InferVStreams=HEF,VDevice,Pipeline
    module.ConfigureParams=SimpleNamespace(create_from_hef=lambda hef,interface:{})
    module.HailoStreamInterface=SimpleNamespace(PCIe='pcie')
    module.InputVStreamParams=module.OutputVStreamParams=SimpleNamespace(make=lambda group,format_type:{})
    module.FormatType=SimpleNamespace(UINT8='u8',FLOAT32='f32')
    return module,device_state


class WorkerBedTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401  (installed with hailo-all on the Pi; optional here)
            from PIL import Image
        except ImportError:
            self.skipTest('numpy/Pillow not installed')
        stream=io.BytesIO();Image.new('RGB',(320,240),(90,90,90)).save(stream,'JPEG')
        self.jpeg=base64.b64encode(stream.getvalue()).decode()

    def run_worker(self,requests,fail_bed=False):
        from failureDetection import hailo_worker
        module,device=fake_hailo(fail_bed)
        stdin=io.StringIO(''.join(json.dumps(r)+'\n' for r in requests));stdout=io.StringIO()
        argv=['hailo_worker.py','/m/print_failure.hef','["spaghetti"]','["spaghetti"]','640','/m/clip_resnet_50x4.hef']
        with patch.dict(sys.modules,{'hailo_platform':module}),patch.object(sys,'argv',argv),patch.object(sys,'stdin',stdin),patch.object(sys,'stdout',stdout):
            hailo_worker.main()
        return [json.loads(line) for line in stdout.getvalue().splitlines()],device

    def test_bed_check_runs_next_to_the_failure_model(self):
        replies,device=self.run_worker([{'jpeg':self.jpeg},{'embed':self.jpeg,'crops':[[0.1,0.1,0.9,0.9]]},{'jpeg':self.jpeg}])
        ready,first,bed,after=replies
        self.assertEqual((ready['bed'],ready['bed_error'],ready['hailort'],ready['chip']),(True,'','4.20.0','hailo8'))
        self.assertEqual(first['score'],0.9);self.assertEqual(after['score'],0.9)   # failure detection before and after
        self.assertEqual(len(bed['vectors']),2)   # the picture and the crop
        self.assertAlmostEqual(bed['vectors'][0][0],0.6);self.assertAlmostEqual(bed['vectors'][0][1],0.8)   # unit length
        # The failure model is active, the bed model only for the bed check, then the failure model again.
        self.assertEqual(device['log'],['/m/print_failure.hef','/m/clip_resnet_50x4.hef','/m/print_failure.hef'])

    def test_failure_detection_works_when_the_bed_model_cannot_load(self):
        replies,_=self.run_worker([{'embed':self.jpeg},{'jpeg':self.jpeg}],fail_bed=True)
        ready,bed,failure=replies
        self.assertFalse(ready['bed']);self.assertIn('different HailoRT',ready['bed_error'])
        self.assertIn('different HailoRT',bed['error']);self.assertEqual(failure['score'],0.9)


if __name__=='__main__':
    unittest.main()


class BackendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_helper_starts_without_the_bed_model_if_it_crashes_with_it(self):
        import os,tempfile
        from pathlib import Path
        from failureDetection.detection import HailoBackend,settings_for
        with tempfile.TemporaryDirectory() as d:
            model=Path(d)/'print_failure.hef';model.write_bytes(b'hef')
            # Stands in for the system Python: with the bed model (an extra argument) the helper dies at start-up.
            python=Path(d)/'python3';python.write_text('#!/bin/sh\n[ $# -ge 6 ] && { echo "Segmentation fault" >&2; exit 139; }\n'
                                                        'echo \'{"ready": true, "backend": "hailo", "hailort": "4.20.0", "chip": "hailo8"}\'\nexec cat >/dev/null\n')
            os.chmod(python,0o755)
            s=settings_for({'failure_detection':{'enabled':True,'model':str(model),'python':str(python)}});s['bed_model']=str(Path(d)/'clip.hef')
            backend=HailoBackend(s)
            try:
                await backend.warm()
                self.assertEqual(backend.error,'');self.assertEqual(backend.backend_name,'AI HAT (Hailo-8)')   # failure detection runs
                self.assertFalse(backend.bed_ready);self.assertIn('stopped the AI helper from starting',backend.bed_error)
                self.assertEqual(backend.device,{'hailort':'4.20.0','chip':'hailo8'})
            finally:
                await backend.close()
