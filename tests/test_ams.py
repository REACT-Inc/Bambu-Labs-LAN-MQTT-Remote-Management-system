import copy,importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch


def unit(uid,material,**extra):
    return {'id':str(uid),'humidity':'3','temp':'25','tray':[{'id':str(i),'tray_type':material,'tray_color':'FFFFFFFF','remain':90} for i in range(4)],**extra}

FULL={'ams':{'tray_exist_bits':'ff','ams':[unit(0,'PLA'),unit(1,'PETG')]}}


class AmsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        (d/'config.json').write_text(json.dumps({'demo':True,'example_data':{'H2D':{}}}))
        with patch.dict(os.environ,{'PM_CONFIG':str(d/'config.json'),'PM_DATA':str(d)}):
            spec=importlib.util.spec_from_file_location('test_core_ams',Path(__file__).parents[1]/'core.py')
            self.core=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.core)
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()

    def test_partial_report_keeps_other_ams_units(self):
        report=copy.deepcopy(FULL)
        self.core.merge_report(report,{'ams':{'ams':[{'id':'1','humidity':'5','tray':[{'id':'2','remain':40}]}]}})
        ams0,ams1=report['ams']['ams']
        self.assertEqual([t['tray_type'] for t in ams0['tray']],['PLA']*4)
        self.assertEqual(ams1['humidity'],'5')
        self.assertEqual(ams1['tray'][2],{'id':'2','tray_type':'PETG','tray_color':'FFFFFFFF','remain':40})

    def test_new_unit_is_added_and_plain_lists_are_replaced(self):
        report=copy.deepcopy(FULL);report['hms']=[{'attr':1}]
        self.core.merge_report(report,{'ams':{'ams':[unit(2,'ABS')]},'hms':[]})
        self.assertEqual([u['id'] for u in report['ams']['ams']],['0','1','2'])
        self.assertEqual(report['hms'],[])

    def test_filament_embed_shows_all_units_and_removed_spools(self):
        report=copy.deepcopy(FULL)
        self.core.merge_report(report,{'ams':{'tray_exist_bits':'f7','ams':[{'id':'1','humidity':'5'}]}})
        self.core.EXAMPLE_DATA['H2D']=report
        fields={f.name:f.value for f in self.core.filament_embed('H2D').fields}
        self.assertEqual(fields['AMS 0'].count('PLA'),3)
        self.assertIn('**Slot 3** — Empty / not reported',fields['AMS 0'])
        self.assertEqual(fields['AMS 1'].count('PETG'),4)


if __name__=='__main__':
    unittest.main()
