"""Send a finished print to another printer's queue (#57)."""
import json,sys,tempfile,unittest,zipfile
from unittest.mock import patch
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
        with self.assertRaisesRegex(ValueError,'AMS mapping'):job_transfer.send(self.core,self.store,self.job['id'],'Mini 2',True,'',True,'x')
        Path(self.mini_file).unlink()
        with self.assertRaisesRegex(ValueError,'no longer on the Pi'):job_transfer.send(self.core,self.store,self.job['id'],'Mini 2',False,'',True,'x')


class RemoteFileTests(unittest.TestCase):
    """A remote: job's file is only on the original printer: it's copied to the Pi, then queued like an upload."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name);(d/'uploads').mkdir()
        configs={'Mini 1':{'name':'Mini 1','model':'A1 mini','ip':'192.0.2.1','access_code':'x'},'Mini 2':{'model':'A1 mini'},'H2D':{'model':'H2D'}}
        self.core=SimpleNamespace(names=lambda:list(configs),printer_config=configs.get,display_name=lambda n:n,EXAMPLE_MODE=False)
        self.store=Store(d/'db');self.uploads=d/'uploads'
        self.job=self.store.add('Mini 1','On SD','','cache/part.gcode.3mf',options(),'tester');self.store.set_status(self.job['id'],'finished')
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def download_as(self,model):
        calls=[]
        def download(printer,remote,dest):calls.append((printer['name'],remote));sliced(dest,model)
        return download,calls

    def test_file_is_copied_from_the_original_printer_and_queued(self):
        job=self.store.get(self.job['id'])
        self.assertTrue(job_transfer.needs_fetch(self.core,job))
        self.assertEqual(job_transfer.check(self.core,job,'Mini 2')['compatible'],True)      # printed on an A1 mini
        self.assertEqual(job_transfer.check(self.core,job,'H2D')['compatible'],False)
        download,calls=self.download_as('Bambu Lab A1 mini')
        path=job_transfer.fetch(self.core,job,self.uploads,download)
        self.assertEqual(calls,[('Mini 1','cache/part.gcode.3mf')]);self.assertTrue(Path(path).is_file())
        copy=job_transfer.send(self.core,self.store,job['id'],'Mini 2',False,'',None,'x',asset=path)
        self.assertEqual((copy['printer'],copy['asset'],copy['remote']),('Mini 2',path,'part.gcode.3mf'))  # uploaded at start, under the original file's name
        self.assertIn('file copied from that printer',self.store.events()[0]['detail'])

    def test_the_real_file_is_checked_after_copying(self):
        download,_=self.download_as('Bambu Lab H2D')
        path=job_transfer.fetch(self.core,self.store.get(self.job['id']),self.uploads,download)
        with self.assertRaisesRegex(ValueError,'Sliced for the H2D, not the A1 mini'):
            job_transfer.send(self.core,self.store,self.job['id'],'Mini 2',False,'',None,'x',asset=path)

    def test_a_failed_or_bad_copy_leaves_nothing_behind(self):
        def offline(printer,remote,dest):raise RuntimeError("Couldn't copy the file from Mini 1")
        with self.assertRaisesRegex(RuntimeError,"Couldn't copy"):job_transfer.fetch(self.core,self.store.get(self.job['id']),self.uploads,offline)
        def not_sliced(printer,remote,dest):Path(dest).write_bytes(b'not a zip')
        with self.assertRaisesRegex(ValueError,'sliced Bambu'):job_transfer.fetch(self.core,self.store.get(self.job['id']),self.uploads,not_sliced)
        self.assertEqual(list(self.uploads.iterdir()),[])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dest=Path(self.tmp.name)/'f.3mf'
        self.printer={'name':'Mini 1','ip':'192.0.2.1','access_code':'secret99','model':'A1 mini'}
    def tearDown(self):self.tmp.cleanup()
    def ftp(self,data,reported=None):
        class FTP:
            def __init__(self,**kwargs):pass
            def connect(self,*a):pass
            def login(self,*a):pass
            def prot_p(self):pass
            def voidcmd(self,c):pass
            def size(self,remote):return len(data) if reported is None else reported
            def retrbinary(self,cmd,callback,blocksize):
                assert cmd=='RETR cache/part.gcode.3mf'
                for k in range(0,len(data),4):callback(data[k:k+4])
            def close(self):pass
        return patch.dict(sys.modules,{'bambulabs_api.ftp_client':SimpleNamespace(ImplicitFTP_TLS=FTP)})
    def test_download(self):
        import queueing
        with self.ftp(b'0123456789'):queueing.download(self.printer,'cache/part.gcode.3mf',self.dest)
        self.assertEqual(self.dest.read_bytes(),b'0123456789')
    def test_size_mismatch_or_missing_file_leaves_nothing(self):
        import queueing
        with self.ftp(b'0123456789',reported=4),self.assertRaisesRegex(RuntimeError,"Couldn't copy the file from Mini 1"):
            queueing.download(self.printer,'cache/part.gcode.3mf',self.dest)
        self.assertFalse(self.dest.exists())
        with self.ftp(b'',reported=0),self.assertRaisesRegex(RuntimeError,'missing on the printer') as error:
            queueing.download(self.printer,'cache/part.gcode.3mf',self.dest)
        self.assertNotIn('secret99',str(error.exception));self.assertFalse(self.dest.exists())


if __name__=='__main__':
    unittest.main()
