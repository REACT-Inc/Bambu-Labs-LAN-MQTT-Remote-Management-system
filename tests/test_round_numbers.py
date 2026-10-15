"""Round numbers everywhere (#42): one set of display rules for Discord (core.py's format_*) and the dashboard
(static/app.js's fmt*): whole degrees and percentages, "2 h 5 min", the AMS humidity level or percentage, and "—" for
anything the printer didn't report. Only what's shown changes; the reported values stay exact."""
import asyncio,copy,importlib.util,json,os,re,shutil,subprocess,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

ROOT=Path(__file__).parents[1]
NODE=shutil.which('node')


def load_core(d):
    (Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1],'demo':True,'example_data':{'A1':{}}}))
    with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
        spec=importlib.util.spec_from_file_location('test_core_round_numbers',ROOT/'core.py')
        core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
    return core


class Helpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.core=load_core(cls.tmp.name)
    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.core.bot.close());cls.tmp.cleanup()

    def test_temperatures(self):
        t=self.core.format_temp
        self.assertEqual([t(219.96875),t('24.5000'),t(0),t(-3.4),t('0.4'),t(219.5),t(220.5)],
                         ['220°C','25°C','0°C','-3°C','0°C','220°C','221°C'])   # halves round up, as on the dashboard
        self.assertEqual([t(None),t(''),t('abc'),t(float('nan')),t([])],['—']*5)

    def test_current_and_target(self):
        h=self.core.format_heat
        self.assertEqual(h(219.96875,230),'220°C → 230°C')
        self.assertEqual(h(25.2,0),'25°C → off');self.assertEqual(h('31','0.0'),'31°C → off')
        self.assertEqual(h(60.4,None),'60°C');self.assertEqual(h(None,None),'—');self.assertEqual(h(None,200),'— → 200°C')

    def test_minutes(self):
        m=self.core.format_minutes
        self.assertEqual([m(0),m(45),m(59),m(60),m(61),m(125),m('312'),m(59.6),m(-5)],
                         ['0 min','45 min','59 min','1 h 0 min','1 h 1 min','2 h 5 min','5 h 12 min','1 h 0 min','0 min'])
        self.assertEqual([m(None),m(''),m('soon')],['—']*3)

    def test_percentages_and_counts(self):
        p,c=self.core.format_percent,self.core.format_count
        self.assertEqual([p(45.6),p('40'),p(0),p(-5),p(140),p(99.5),p(None),p('x')],['46%','40%','0%','0%','100%','100%','—','—'])
        self.assertEqual([c(12),c('7'),c(12.6),c(None),c('')],['12','7','13','—','—'])

    def test_ams_humidity(self):
        h=self.core.format_humidity
        self.assertEqual(h({'humidity':'4'}),'level 4')                       # the 1–5 level, as reported
        self.assertEqual(h({'humidity':'4','humidity_raw':'33.6'}),'34%')    # AMS 2 Pro / AMS HT: the percentage
        self.assertEqual(h({'humidity':'45'}),'45%');self.assertEqual(h({'humidity':''}),'—');self.assertEqual(h({}),'—')

    def test_progress_bar(self):
        self.assertEqual(self.core.progress(45.5),'🟦'*5+'⬜'*5+' **46%**')
        self.assertEqual(self.core.progress(100),'🟦'*10+' **100%**');self.assertEqual(self.core.progress(None),'—')


class DiscordReplies(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.core=load_core(self.tmp.name)
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()

    def test_printer_card(self):
        data={'nozzle_temper':219.96875,'nozzle_target_temper':0,'bed_temper':24.6,'bed_target_temper':0,'mc_percent':45.5,
              'mc_remaining_time':125.4,'subtask_name':'benchy','gcode_state':'RUNNING'}   # no layer reported yet
        self.core.state_data=lambda name:('RUNNING',0,data,True)
        fields={f.name:f.value for f in self.core.printer_embed('A1').fields}
        self.assertEqual((fields['Nozzle'],fields['Bed']),('220°C → off','25°C → off'))
        self.assertEqual((fields['Time remaining'],fields['Layer']),('2 h 5 min','— / —'))
        self.assertTrue(fields['Progress'].endswith('**46%**'))

    def test_progress_notification(self):
        text=self.core.progress_description({'subtask_name':'benchy','mc_percent':40,'mc_remaining_time':61,'layer_num':12,
                                             'total_layer_num':240.0,'nozzle_temper':219.96875})
        self.assertIn('**Remaining:** 1 h 1 min',text);self.assertIn('**Layer:** 12/240',text)
        self.assertIn('**Nozzle:** 220°C • **Bed:** —',text);self.assertNotIn('?',text)

    def test_filaments(self):
        self.core.EXAMPLE_DATA['A1']={'ams':{'ams':[{'id':'0','humidity':'4','temp':'24.5','tray':[
            {'id':'0','tray_type':'PLA','tray_color':'FF0000FF','remain':42.7},{'id':'1','tray_type':'PETG','tray_color':'00FF00FF','remain':-1}]},
            {'id':'1','humidity_raw':'28','temp':None,'tray':[]}]}}
        fields={f.name:f.value for f in self.core.filament_embed('A1').fields}
        self.assertTrue(fields['AMS 0'].startswith('Humidity: level 4 • Temperature: 25°C'))
        self.assertIn('43% remaining',fields['AMS 0']);self.assertIn('unknown remaining',fields['AMS 0'])   # -1: no RFID tag
        self.assertTrue(fields['AMS 1'].startswith('Humidity: 28% • Temperature: —'))

    async def test_status(self):
        data={'mc_percent':45.5}
        self.core.names=lambda:['A1'];self.core.state_data=lambda name:('RUNNING',0,data,True)
        sent=[]
        async def respond(interaction,embed,view=None):sent.append(embed.description)
        self.core.respond=respond
        interaction=SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),command=None,data={},channel_id=0,user=SimpleNamespace(id=1))
        await self.core.status_command.callback(interaction)
        self.assertIn('Printing • 46%',sent[0])
        data.pop('mc_percent');sent.clear()
        await self.core.status_command.callback(interaction)
        self.assertIn('Printing • —',sent[0])


class Dashboard(unittest.TestCase):
    """The dashboard's helpers live once, in app.js, and follow the same rules as Discord."""
    def test_one_set_of_helpers_used_for_the_ams_and_times(self):
        app,controls=(ROOT/'static/app.js').read_text(),(ROOT/'static/controls.js').read_text()
        for name in ('num','fmtTemp','fmtPercent','fmtMinutes','fmtHumidity'):
            self.assertEqual(len(re.findall(rf'^function {name}\(',app,re.M)),1,name)
            self.assertNotIn(f'function {name}(',controls)
        self.assertIn('fmtHumidity(u)',controls);self.assertIn('fmtTemp(u.temp)',controls)
        self.assertNotIn("u.humidity??'—'",controls);self.assertNotIn("remain+'%'",controls)
        self.assertNotIn("left%60",controls)   # one minutes helper

    @unittest.skipUnless(NODE,'node is not installed')
    def test_same_numbers_as_discord(self):
        app=(ROOT/'static/app.js').read_text()
        source='\n'.join(re.search(rf'^function {name}\(.*$',app,re.M).group(0) for name in ('num','fmtTemp','fmtPercent','fmtMinutes','fmtHumidity'))
        values=[219.96875,'24.5000',0,-3.4,219.5,220.5,59,60,61,125.4,'312',45.6,-5,140,99.5,None,'','abc']
        units=[{'humidity':'4'},{'humidity':'4','humidity_raw':'33.6'},{'humidity':'45'},{'humidity':''},{}]
        script=source+f"""
const values={json.dumps(values)},units={json.dumps(units)};
console.log(JSON.stringify({{temp:values.map(fmtTemp),percent:values.map(fmtPercent),minutes:values.map(fmtMinutes),humidity:units.map(fmtHumidity)}}));"""
        result=json.loads(subprocess.run([NODE,'-e',script],capture_output=True,text=True,check=True,timeout=30).stdout)
        with tempfile.TemporaryDirectory() as d:
            core=load_core(d)
            try:
                self.assertEqual(result['temp'],[core.format_temp(v).replace('°C','°') for v in values])   # tiles say 220°
                self.assertEqual(result['percent'],[core.format_percent(v) for v in values])
                self.assertEqual(result['minutes'],[core.format_minutes(v).replace('—','') for v in values])   # '' = not shown
                self.assertEqual(result['humidity'],[core.format_humidity(u) for u in units])
            finally:
                asyncio.run(core.bot.close())


if __name__=='__main__':
    unittest.main()
