"""Every OrcaSlicer setting in the Slicer tab (docs/slicer.md): the list generated from OrcaSlicer's source
(tools/make_slicer_options.py -> slicer/orca_options.py), how each kind of value is checked, the presets' own values
shown next to each setting (sent by a worker), and the page's All settings window."""
import asyncio,gzip,json,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path

from slicer import orca_options,profiles,settings,worker
from tests import test_slicer as base

ROOT=Path(__file__).parents[1]
NODE=shutil.which('node')
sys.path.insert(0,str(ROOT/'tools'))
import make_slicer_options as generator  # noqa: E402

PRINT_CONFIG=r'''
const int max_temp = 1500;
void PrintConfigDef::init_fff_params()
{
    def = this->add("layer_height", coFloat);
    def->label = L("Layer height");
    def->tooltip = L("Slicing height for each layer. "
                     "Smaller layer height means more accurate.");
    def->sidetext = "mm";
    def->min = 0;
    def->set_default_value(new ConfigOptionFloat(0.2));

    auto def_top_fill_pattern = def = this->add("top_surface_pattern", coEnum);
    def->label = L("Top surface pattern");
    def->enum_values.push_back("monotonic");
    def->enum_values.push_back("concentric");
    def->enum_labels.push_back(L("Monotonic"));
    def->enum_labels.push_back(L("Concentric"));
    def->mode = comSimple;

    def = this->add("bottom_surface_pattern", coEnum);
    def->label = L("Bottom surface pattern");
    def->enum_values = def_top_fill_pattern->enum_values;
    def->enum_labels = def_top_fill_pattern->enum_labels;

    def = this->add("nozzle_temperature", coInts);
    def->label = L("Other layers");
    def->full_label = L("Nozzle temperature");
    def->sidetext = L(u8"℃" /* °C */);	// degrees Celsius
    def->min = 0;
    def->max = max_temp;
    def->set_default_value(new ConfigOptionInts { 200 });

    def = this->add("outer_wall_line_width", coFloatOrPercent);
    def->label = L("Outer wall");
    def->sidetext = L("mm or %");
    def->set_default_value(new ConfigOptionFloatOrPercent(0., false));

    def = this->add("filament_start_gcode", coStrings);
    def->label = L("Start G-code");

    def = this->add("enable_support", coBool);
    def->label = L("Enable support");
    def->set_default_value(new ConfigOptionBool(false));

    def = this->add("secret_setting", coBool);
    def->label = L("For developers");
    def->mode = comDevelop;
}
'''
TAB=r'''
void TabPrint::build()
{
    auto page = add_options_page(L("Quality"), "icon");
        auto optgroup = page->new_optgroup(L("Layer height"), L"param");
        optgroup->append_single_option_line("layer_height", "help");
        optgroup = page->new_optgroup(L("Line width"), L"param");
        optgroup->append_single_option_line("outer_wall_line_width");
    page = add_options_page(L("Strength"), "icon");
        optgroup = page->new_optgroup(L("Top/bottom shells"), L"param");
        optgroup->append_single_option_line("top_surface_pattern");
        optgroup->append_single_option_line("bottom_surface_pattern");
    page = add_options_page(L("Support"), "icon");
        optgroup = page->new_optgroup(L("Support"), L"param");
        optgroup->append_single_option_line("enable_support");
        optgroup->append_single_option_line("secret_setting");
}

void TabFilament::build()
{
    auto page = add_options_page(L("Filament"), "icon");
        auto optgroup = page->new_optgroup(L("Print temperature"), L"param");
        Line line = { L("Nozzle"), L("Nozzle temperature when printing") };
        line.append_option(optgroup->get_option("nozzle_temperature", 0));
        optgroup = page->new_optgroup(L("G-code"), L"param");
        optgroup->append_single_option_line("filament_start_gcode");
}

void TabPrinter::build()
{
}
'''


class GeneratorTests(unittest.TestCase):
    def test_reads_orcaslicer_source(self):
        with tempfile.TemporaryDirectory() as d:
            for path,text in (('src/libslic3r/PrintConfig.cpp',PRINT_CONFIG),('src/slic3r/GUI/Tab.cpp',TAB)):
                (Path(d)/path).parent.mkdir(parents=True,exist_ok=True);(Path(d)/path).write_text(text)
            options={o['key']:o for o in generator.build(Path(d))}
        self.assertEqual(list(options),['layer_height','outer_wall_line_width','top_surface_pattern','bottom_surface_pattern','enable_support','nozzle_temperature'])
        layer=options['layer_height']
        self.assertEqual((layer['label'],layer['unit'],layer['min'],layer['default'],layer['page'],layer['group'],layer['preset']),
                         ('Layer height','mm',0,0.2,'Quality','Layer height','process'))
        self.assertEqual(layer['tooltip'],'Slicing height for each layer. Smaller layer height means more accurate.')
        temp=options['nozzle_temperature']
        self.assertEqual((temp['label'],temp['unit'],temp['max'],temp['list'],temp['preset'],temp['default']),('Nozzle temperature','℃',1500,True,'filament',200))
        self.assertEqual(options['bottom_surface_pattern']['options'],[['monotonic','Monotonic'],['concentric','Concentric']])   # copied
        self.assertEqual((options['top_surface_pattern']['level'],options['outer_wall_line_width']['type']),('simple','float_or_percent'))
        self.assertNotIn('filament_start_gcode',options);self.assertNotIn('secret_setting',options)   # text, and developer-only

    def test_the_generated_list(self):
        keys={o['key']:o for o in orca_options.OPTIONS}
        self.assertGreater(len(keys),350);self.assertEqual(len(keys),len(orca_options.OPTIONS))
        self.assertEqual(orca_options.ORCA_VERSION,__import__('slicer.installer',fromlist=['x']).ORCA_VERSION)
        for key in ('top_surface_pattern','seam_gap','wall_generator','brim_width','support_interface_top_layers','filament_retraction_length',
                    'fan_cooling_layer_time','ironing_speed','curr_bed_type'):
            self.assertIn(key,keys)
        self.assertFalse([k for k in keys if 'gcode' in k or 'post_process' in k])   # never text a worker would run
        self.assertEqual({o['type'] for o in keys.values()},{'float','int','percent','float_or_percent','bool','enum'})
        for group,entries in settings.QUICK:
            for key,_ in entries:self.assertIn(key,keys)


class CheckTests(unittest.TestCase):
    def test_every_kind_of_value(self):
        out=settings.check({'outer_wall_line_width':'110%','line_width':0.42,'sparse_infill_density':25,'top_surface_pattern':'concentric',
                            'enable_support':False,'wall_loops':3,'brim_width':5,'curr_bed_type':'Textured PEI Plate'})
        self.assertEqual(out['process'],{'outer_wall_line_width':'110%','line_width':0.42,'sparse_infill_density':'25%','top_surface_pattern':'concentric',
                                         'enable_support':False,'wall_loops':3,'brim_width':5.0,'curr_bed_type':'Textured PEI Plate'})
        for bad in ({'outer_wall_line_width':'wide'},{'outer_wall_line_width':'-5%'},{'top_surface_pattern':'Concentric'},{'layer_height':2},
                    {'nozzle_temperature':600},{'brim_width':float('nan')},{'filament_start_gcode':'M112'},{'sparse_infill_density':150}):
            with self.assertRaises(ValueError,msg=bad):settings.check(bad)

    def test_values_go_into_the_presets(self):
        with tempfile.TemporaryDirectory() as d:
            library=profiles.Profiles(base.write_profiles(d))
            process=library.resolved('process',base.PROCESS,settings.check({'outer_wall_line_width':'110%','sparse_infill_density':30})['process'])
            self.assertEqual((process['outer_wall_line_width'],process['sparse_infill_density']),('110%','30%'))

    def test_presets_own_values(self):
        with tempfile.TemporaryDirectory() as d:
            table=profiles.Profiles(base.write_profiles(d)).values({'process':['layer_height','wall_loops','seam_gap'],'filament':['nozzle_temperature']})
        process=table['process'];row=process['rows'][process['presets'][base.PROCESS]]
        self.assertEqual((process['keys'],row),(['layer_height','wall_loops','seam_gap'],['0.2','2',None]))   # None: OrcaSlicer's default
        self.assertEqual(table['filament']['rows'][table['filament']['presets'][base.FILAMENT]],['220'])        # the first extruder's value


class ValuesWebTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp,asyncTearDown=base.DashboardTests.asyncSetUp,base.DashboardTests.asyncTearDown
    enroll=base.DashboardTests.enroll

    async def test_a_worker_sends_the_values_and_the_tab_reads_them(self):
        auth=await self.enroll()
        r=await self.client.get('/api/slicer/values?kind=process&name='+base.PROCESS)
        self.assertEqual(await r.json(),{'values':None})   # nothing yet: the page shows OrcaSlicer's defaults
        answer=await (await self.client.post('/api/slicer/worker/heartbeat',json=dict(orca_version='v2.4.2',running=[]),headers=auth)).json()
        self.assertIn('layer_height',answer['values_wanted']['process']);self.assertIn('nozzle_temperature',answer['values_wanted']['filament'])
        bot=worker.Worker(self.worker_config);await asyncio.to_thread(bot.heartbeat)   # sends the catalog and the values
        values=(await (await self.client.get('/api/slicer/values?kind=process&name='+base.PROCESS)).json())['values']
        self.assertEqual((values['layer_height'],values['wall_loops']),('0.2','2'));self.assertNotIn('seam_gap',values)
        filament=(await (await self.client.get('/api/slicer/values?kind=filament&name='+base.FILAMENT)).json())['values']
        self.assertEqual(filament['nozzle_temperature'],'220')
        answer=await (await self.client.post('/api/slicer/worker/heartbeat',json=dict(orca_version='v2.4.2',running=[]),headers=auth)).json()
        self.assertIsNone(answer['values_wanted'])
        self.assertEqual((await self.client.get('/api/slicer/values?kind=machine&name=x')).status,400)
        catalog=await (await self.client.get('/api/slicer/catalog')).json()
        self.assertEqual(len(catalog['schema']),len(settings.SCHEMA));self.assertEqual([g['group'] for g in catalog['quick']][0],'Quality')

    async def test_bad_values_are_refused(self):
        auth=await self.enroll()
        url='/api/slicer/worker/values?version=v2.4.2'
        for body in (b'not gzip',gzip.compress(b'[1,2]'),gzip.compress(json.dumps({'process':{'keys':['a'],'rows':[[1,2]],'presets':{}},
                                                                                   'filament':{'keys':[],'rows':[],'presets':{}}}).encode()),
                     gzip.compress(b' '*(200*1024*1024))):
            r=await self.client.post(url,data=body,headers=auth);self.assertEqual(r.status,400)
        self.assertEqual((await self.client.post(url,data=gzip.compress(b'{}'))).status,401)   # workers only
        self.assertFalse(list(self.slicer.folder.glob('values-*')))


class PageTests(unittest.TestCase):
    def test_all_settings_window(self):
        page,script=(ROOT/'static/index.html').read_text(),(ROOT/'static/slicer.js').read_text()
        for element in ('slicerAll','slicerAllOpen','slicerAllSearch','slicerAllChanged','slicerAllClose','slicerAllNav','slicerAllList'):
            self.assertIn(f'id="{element}"',page);self.assertIn(f"el('{element}')",script)
        self.assertIn("api(`slicer/values?kind=${kind}&name=",script)

    @unittest.skipUnless(NODE,'node is not installed')
    def test_typed_values(self):
        script=(ROOT/'static/slicer.js').read_text()
        start=script.index('function settingName(');end=script.index('\nfunction changeSetting(')
        cases=[('line_width','0.42'),('outer_wall_line_width','110%'),('outer_wall_line_width','0.45'),('enable_support','true'),('wall_loops','3'),
               ('wall_loops','2.5'),('layer_height','2'),('outer_wall_line_width','x%'),('top_surface_pattern','concentric'),('layer_height','')]
        js=script[start:end]+f'\nconst by={json.dumps({o["key"]:o for o in settings.SCHEMA})};'+\
           f'console.log(JSON.stringify({json.dumps(cases)}.map(([k,v])=>{{try{{const r=parseSetting(by[k],v);return r===undefined?"blank":r;}}catch(e){{return "error";}}}})));'
        out=subprocess.run([NODE,'-'],input=js,capture_output=True,text=True,timeout=30)   # (too long for -e)
        self.assertEqual(out.returncode,0,out.stderr)
        self.assertEqual(json.loads(out.stdout),[0.42,'110%',0.45,True,3,'error','error','error','concentric','blank'])


if __name__=='__main__':
    unittest.main()
