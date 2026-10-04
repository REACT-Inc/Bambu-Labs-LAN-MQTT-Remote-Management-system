"""Printer model registry (#6): detection, limits, chamber, fans, FTPS and file matching for every model."""
import json,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
import printer_models as pm


def core_for(configs,data=None):
    return SimpleNamespace(names=lambda:list(configs),printer_config=configs.get,display_name=lambda n:n,
                           state_data=lambda n:('IDLE',0,dict(data or {}),True))


class DetectionTests(unittest.TestCase):
    def test_from_model_text(self):
        cases={'A1 mini':'a1mini','A1M':'a1mini','A1':'a1','A2L':'a2l','P1P':'p1p','P1S':'p1s','P2S':'p2s','X1':'x1',
               'X1 Carbon':'x1c','X1C':'x1c','X1E':'x1e','X2D':'x2d','H2D':'h2d','Bambu Lab H2D Pro':'h2dpro','H2S':'h2s',
               'H2C':'h2c','bambu-lab x1-carbon':'x1c','Something else':''}
        self.assertEqual({t:pm.key_from_text(t) for t in cases},cases)

    def test_from_serial_and_studio_id(self):
        serials={'030':'a1mini','039':'a1','26A':'a2l','01S':'p1p','01P':'p1s','22E':'p2s','00W':'x1','00M':'x1c',
                 '03W':'x1e','20P':'x2d','094':'h2d','239':'h2dpro','093':'h2s','31B':'h2c'}
        self.assertEqual({s:pm.key_for({'serial':s+'XXXXXXXXXXXX'}) for s in serials},serials)
        ids={'N1':'a1mini','N2S':'a1','N9':'a2l','C11':'p1p','C12':'p1s','N7':'p2s','BL-P002':'x1','BL-P001':'x1c',
             'C13':'x1e','N6':'x2d','O1D':'h2d','O1E':'h2dpro','O1S':'h2s','O1C':'h2c'}
        self.assertEqual({i:pm.key_from_studio_id(i) for i in ids},ids)

    def test_model_beats_serial_beats_name(self):
        self.assertEqual(pm.key_for({'model':'P1S','serial':'094X'},'Lab H2D'),'p1s')
        self.assertEqual(pm.key_for({'serial':'094X'},'Lab A1'),'h2d')
        self.assertEqual(pm.key_for({'serial':'ZZZX'},'Andrew H2D'),'h2d')   # unknown serial: fall back to the name
        self.assertEqual(pm.key_for({},'Shop printer'),'')
        self.assertEqual(pm.key_for({'model':'Mystery 9'},'Lab H2D'),'')       # an unrecognised model isn't overridden by the name


class LimitTests(unittest.TestCase):
    def test_limits_per_model(self):
        expected={'A1 mini':(300,80,0),'A1':(300,100,0),'A2L':(300,100,0),'P1P':(300,100,0),'P1S':(300,100,0),'P2S':(300,110,0),
                  'X1 Carbon':(300,110,0),'X1E':(320,120,0),'X2D':(300,120,65),'H2D':(350,120,65),'H2D Pro':(350,120,65),
                  'H2S':(350,120,65),'H2C':(350,120,65),'Unknown thing':(300,80,0)}
        core=core_for({m:{'model':m} for m in expected})
        self.assertEqual({m:tuple(pm.limits(core,m).values()) for m in expected},expected)

    def test_tested_printers_unchanged(self):
        # H2D, A1 and A1 mini are the printers this has been used with: their limits must not move.
        core=core_for({'Andrew':{'serial':'094X'},'BOB':{'model':'A1 mini'},'Big':{'model':'A1'}})
        self.assertEqual([pm.limits(core,n) for n in ('Andrew','BOB','Big')],
                         [{'nozzle':350,'bed':120,'chamber':65},{'nozzle':300,'bed':80,'chamber':0},{'nozzle':300,'bed':100,'chamber':0}])
        self.assertTrue(all(pm.MODELS[k]['tested'] for k in ('h2d','a1','a1mini')))
        self.assertFalse(any(m['tested'] for k,m in pm.MODELS.items() if k not in ('h2d','a1','a1mini')))

    def test_config_overrides_are_bounded(self):
        core=core_for({'P':{'model':'P1S','limits':{'nozzle':280,'bed':105}},'Bad':{'model':'P1S','limits':{'nozzle':999,'bed':True}}})
        self.assertEqual(pm.limits(core,'P'),{'nozzle':280,'bed':105,'chamber':0})
        self.assertEqual(pm.limits(core,'Bad'),{'nozzle':300,'bed':100,'chamber':0})


class UsedByTests(unittest.TestCase):
    def test_controls_use_the_registry(self):
        from printer_controls import prepare
        core=core_for({'X2D':{'model':'X2D'},'X1E':{'model':'X1E'},'H2S':{'model':'H2S'},'P1S':{'model':'P1S'}})
        core.names=lambda:['X2D','X1E','H2S','P1S']
        self.assertEqual(prepare(core,'H2S','chamber',65)[1],[{'command':'set_ctt','ctt_val':65}])
        self.assertEqual(prepare(core,'X2D','chamber',50)[1],[{'command':'set_ctt','ctt_val':50}])
        for name in ('X1E','P1S'):
            with self.assertRaisesRegex(ValueError,'only available on'):prepare(core,name,'chamber',50)
        self.assertEqual(prepare(core,'X1E','nozzle',320)[1],'M104 S320\n')
        with self.assertRaises(ValueError):prepare(core,'P1S','bed',101)

    def test_fans_on_older_firmware(self):
        from thermal_controls import fans
        core=core_for({'P1S':{'model':'P1S'},'A1':{'model':'A1'},'H2C':{'model':'H2C'}})
        self.assertEqual([[f['key'] for f in fans(core,n)] for n in ('P1S','A1','H2C')],
                         [['part','auxiliary','chamber'],['part'],['part','auxiliary','chamber']])

    def test_ftps_unwrap_for_h2_series(self):
        import queueing,sys
        from unittest.mock import patch
        seen=[]
        with patch.dict(sys.modules,{'bambulabs_api.ftp_client':SimpleNamespace(ImplicitFTP_TLS=lambda **k:seen.append(k['unwrap']))}):
            for printer in ({'model':'H2S'},{'model':'H2C'},{'model':'X1 Carbon'},{'model':'P2S'},{'model':'P2S','ftp_tls_unwrap':True}):
                queueing.ftps_client(printer)
        self.assertEqual(seen,[True,True,False,False,True])

    def test_job_transfer_knows_the_new_models(self):
        import job_transfer
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'f.3mf'
            with zipfile.ZipFile(path,'w') as z:z.writestr('Metadata/slice_info.config','<metadata key="printer_model_id" value="O1S"/>')
            self.assertEqual(job_transfer.sliced_for(path),'h2s')
            with zipfile.ZipFile(path,'w') as z:z.writestr('Metadata/project_settings.config',json.dumps({'printer_model':'Bambu Lab H2D Pro'}))
            self.assertEqual(job_transfer.sliced_for(path),'h2dpro')
        self.assertEqual(job_transfer.label('h2dpro'),'H2D Pro')

    def test_swapmod_still_a1_family_only(self):
        from swapMod.plate_swap import a_series_model
        core=core_for({'A':{'model':'A1 mini'},'B':{'model':'A1'},'C':{'model':'A2L'},'D':{'model':'P1S'}})
        self.assertEqual([a_series_model(core,n) for n in 'ABCD'],['A1 mini','A1',None,None])


if __name__=='__main__':
    unittest.main()
