"""Open issues finished in 1.7.5: the Discord bot reconnecting after a failed first connection (#14), rounded numbers
in Discord (#42) and one Start / Print now button (#65). Private permission refusals (#23) are in test_new_features."""
import asyncio,importlib.util,json,os,re,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

import discord

ROOT=Path(__file__).parents[1]


def load_core(d):
    (Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1],'demo':True}))
    with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
        spec=importlib.util.spec_from_file_location('test_core_issue_fixes',ROOT/'core.py')
        core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
    return core


class CoreFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.core=load_core(self.tmp.name)
    async def asyncTearDown(self):
        await self.core.bot.close();self.tmp.cleanup()

    # --- #14: Discord comes back without a restart -------------------------------------------------------------
    async def test_discord_retries_until_it_connects(self):
        attempts=[]
        class Client:
            http=SimpleNamespace(close=AsyncMock())
            async def start(self,token):
                attempts.append(token)
                if len(attempts)<3:raise OSError('Cannot connect to host discord.com:443')
        waits=[]
        async def sleep(seconds):waits.append(seconds)
        await self.core.run_discord(Client(),'token',sleep=sleep)
        self.assertEqual(len(attempts),3)                # no internet twice, then connected
        self.assertEqual(waits,[15,30])                  # backing off
        self.assertEqual(Client.http.close.await_count,2)

    async def test_bad_token_is_not_retried(self):
        class Client:
            http=SimpleNamespace(close=AsyncMock());calls=0
            async def start(self,token):
                Client.calls+=1;raise discord.LoginFailure('Improper token has been passed.')
        with self.assertLogs(self.core.log,'ERROR') as logs:
            await self.core.run_discord(Client(),'bad',sleep=AsyncMock())
        self.assertEqual(Client.calls,1);self.assertIn('discord_token',logs.output[0])

    async def test_backoff_is_capped(self):
        waits=[]
        class Client:
            http=SimpleNamespace(close=AsyncMock())
            async def start(self,token):
                if len(waits)<8:raise OSError('offline')
        async def sleep(seconds):waits.append(seconds)
        await self.core.run_discord(Client(),'token',sleep=sleep)
        self.assertEqual(max(waits),300)

    # --- #42: rounded numbers in Discord ----------------------------------------------------------------------
    def test_numbers_are_rounded(self):
        self.assertEqual((self.core.whole(219.96875),self.core.whole('24.5000'),self.core.whole(None)),('220','24','?'))
        self.assertEqual((self.core.duration(45),self.core.duration('312'),self.core.duration(None)),('45 min','5 h 12 min','?'))

    def test_printer_card_and_progress_use_rounded_numbers(self):
        data={'nozzle_temper':219.96875,'nozzle_target_temper':220.0,'bed_temper':59.81,'bed_target_temper':60,
              'mc_remaining_time':312,'mc_percent':40,'subtask_name':'benchy','gcode_state':'RUNNING'}
        self.core.state_data=lambda name:('RUNNING',0,data,True)
        fields={f.name:f.value for f in self.core.printer_embed('A1').fields}
        self.assertEqual(fields['Nozzle'],'220°C → 220°C');self.assertEqual(fields['Bed'],'60°C → 60°C')
        self.assertEqual(fields['Time remaining'],'5 h 12 min')
        text=self.core.progress_description(data)
        self.assertIn('**Remaining:** 5 h 12 min',text);self.assertIn('**Nozzle:** 220°C',text);self.assertNotIn('219.96875',text)


class ReadableStates(unittest.TestCase):
    """FAILED and FINISH are what Bambu printers keep reporting while idle and ready after a print."""
    def test_states_people_understand(self):
        with tempfile.TemporaryDirectory() as d:
            core=load_core(d)
            self.assertEqual([core.display_state(s) for s in ('IDLE','FINISH','FAILED','RUNNING','PAUSE','PREPARE')],
                             ['Ready','Ready','Ready','Printing','Paused','Preparing'])
            self.assertEqual(core.display_state('FAILED',0x0300800A),'Error');self.assertEqual(core.display_state('IDLE',0,False),'Offline')
            self.assertEqual(core.last_print_note('FAILED'),'last print failed or was cancelled');self.assertEqual(core.last_print_note('FAILED',5),'')
            asyncio.run(core.bot.close())


class AlertPings(unittest.TestCase):
    """Important alerts ping only the people chosen in the dashboard: nobody by default, never @here/@everyone."""
    def test_only_chosen_people_are_pinged(self):
        with tempfile.TemporaryDirectory() as d:
            core=load_core(d)
            self.assertIsNone(core.ping_for('🤖 AI paused a failing print')[0])   # nobody by default
            core.settings['alert_ping_users']=[111111111111111111,222222222222222222]
            text,mentions=core.ping_for('🤖 AI: print may be failing')
            self.assertEqual(text,'<@111111111111111111> <@222222222222222222>')
            self.assertEqual([u.id for u in mentions.users],[111111111111111111,222222222222222222])
            self.assertFalse(mentions.everyone);self.assertFalse(mentions.roles)
            self.assertEqual(core.ping_for('🛑 Printer error')[0],text)
            self.assertIsNone(core.ping_for('✅ Print complete')[0])          # routine updates stay quiet
            core.settings['alert_ping']='everyone';core.settings['alert_ping_users']=[]
            self.assertIsNone(core.ping_for('🛑 Printer error')[0])           # the old @here/@everyone setting is ignored
            asyncio.run(core.bot.close())


class OneStartButton(unittest.TestCase):
    """#65: the queue's Start next and the dialog's Print now are one button each, which asks to start anyway while
    the printer reports an error."""
    def test_no_second_ignore_error_button(self):
        app=(ROOT/'static/app.js').read_text();page=(ROOT/'static/index.html').read_text()
        self.assertNotIn("'startOverride'",app);self.assertNotIn('printNowOverride',page)
        self.assertIn("printerError(j.printer)?actionButton('start','Start next (printer reports an error)…'",app)
        start=app[app.index("if(action==='start')"):]
        self.assertIn('override_error:true',start[:start.index("if(action==='resolve')")])
        now=app[app.index("$('printNow').onclick"):app.index("$('confirmForm').onsubmit")]
        self.assertIn('printerError(data.printer)',now);self.assertIn('override_error:true',now)


if __name__=='__main__':
    unittest.main()
