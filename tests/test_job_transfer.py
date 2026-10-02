"""Send a finished print to another printer's queue (#57)."""
import json,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from queueing import Store,options
import job_transfer


def sliced(path,model=None,model_id=None):
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('Metadata/plate_1.gcode','G28\n')
        if model:z.writestr('Metadata/project_settings.config',json.dumps({'printer_model':model}))
        if model_id:z.writestr('Metadata/slice_info.config',f'<config><plate><metadata key="printer_model_id" value="{model_id}"/></plate></config>')
    return str(path)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        configs={'Mini 1':{'model':'A1 mini'},'Mini 2':{'serial':'030ABC'},'Big A1':{'model':'A1'},'H2D':{'model':'H2D'},'Mystery':{}}
        self.core=SimpleNamespace(names=lambda:list(configs),printer_config=configs.get,display_name=lambda n:n)
        self.store=Store(d/'db')
        self.mini_file=sliced(d/'mini.3mf','Bambu Lab A1 mini')
        self.job=self.store.add('Mini 1','Bracket',self.mini_file,'',options(1,True,'2'),'tester')
        self.store.set_status(self.job['id'],'finished')
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def test_reads_the_model_a_file_was_sliced_for(self):
        d=Path(self.tmp.name)
        self.assertEqual(job_transfer.sliced_for(self.mini_file),'a1mini')
        self.assertEqual(job_transfer.sliced_for(sliced(d/'h.3mf',model_id='O1D')),'h2d')
        self.assertEqual(job_transfer.sliced_for(sliced(d/'x.3mf','Bambu Lab X1 Carbon')),'x1c')
        self.assertEqual(job_transfer.sliced_for(sliced(d/'none.3mf')),'')
        self.assertEqual(job_transfer.sliced_for(d/'missing.3mf'),'')
        self.assertEqual(job_transfer.printer_model(self.core,'Mini 2'),'a1mini')   # from the serial number

    def test_same_model_is_queued_on_the_other_printer(self):
        copy=job_transfer.send(self.core,self.store,self.job['id'],'Mini 2',True,'0,1',None,'web administrator')
        self.assertEqual((copy['printer'],copy['status'],copy['label'],copy['asset']),('Mini 2','queued','Bracket',self.mini_file))
        self.assertEqual(copy['options'],options(1,True,'0,1'))                   # new printer's AMS trays
        self.assertEqual(self.store.get(self.job['id'])['status'],'finished')      # the history entry is unchanged
        self.assertIn('from Mini 1',self.store.events()[0]['detail'])

    def test_wrong_model_is_refused_and_unknown_needs_a_tick(self):
        with self.assertRaisesRegex(ValueError,'not the H2D. Re-slice'):job_transfer.send(self.core,self.store,self.job['id'],'H2D',False,'',True,'x')
        with self.assertRaisesRegex(ValueError,'not the A1'):job_transfer.send(self.core,self.store,self.job['id'],'Big A1',False,'',True,'x')
        with self.assertRaisesRegex(ValueError,'Tick the confirmation'):job_transfer.send(self.core,self.store,self.job['id'],'Mystery',False,'',None,'x')
        self.assertEqual(job_transfer.send(self.core,self.store,self.job['id'],'Mystery',False,'',True,'x')['printer'],'Mystery')
        rows={t['name']:t['compatible'] for t in job_transfer.targets(self.core,self.store.get(self.job['id']))}
        self.assertEqual(rows,{'Mini 2':True,'Big A1':False,'H2D':False,'Mystery':None})

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError,'already printed on'):job_transfer.send(self.core,self.store,self.job['id'],'Mini 1',False,'',True,'x')
        with self.assertRaisesRegex(ValueError,'Choose a printer'):job_transfer.send(self.core,self.store,self.job['id'],'Nope',False,'',True,'x')
        waiting=self.store.add('Mini 1','Other',self.mini_file,'',options(),'tester')
        with self.assertRaisesRegex(ValueError,'Only finished'):job_transfer.send(self.core,self.store,waiting['id'],'Mini 2',False,'',True,'x')
        remote=self.store.add('Mini 1','On SD','', 'cache/part.gcode.3mf',options(),'tester');self.store.set_status(remote['id'],'finished')
        with self.assertRaisesRegex(ValueError,'stored on the printer'):job_transfer.send(self.core,self.store,remote['id'],'Mini 2',False,'',True,'x')
        with self.assertRaisesRegex(ValueError,'AMS mapping'):job_transfer.send(self.core,self.store,self.job['id'],'Mini 2',True,'',True,'x')
        Path(self.mini_file).unlink()
        with self.assertRaisesRegex(ValueError,'no longer on the Pi'):job_transfer.send(self.core,self.store,self.job['id'],'Mini 2',False,'',True,'x')


if __name__=='__main__':
    unittest.main()
