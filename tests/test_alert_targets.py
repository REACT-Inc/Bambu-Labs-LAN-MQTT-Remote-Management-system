"""Alerts beyond Discord (#9): what each target type receives (checked with a real local HTTP server), validation,
tokens never reaching the browser, "important only", failures reported, and core.notify sending without the bot."""
import asyncio,importlib.util,json,logging,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

sys.path.insert(0,str(Path(__file__).parents[1]))
from alert_targets import AlertTargets,RED


class Core:
    def __init__(self):self.settings={}
    def save_settings(self,updated):self.settings=dict(updated)


class Receiver:
    """A local HTTP server that records what it is sent."""
    async def start(self,status=200):
        self.got,self.status=[],status
        app=web.Application();app.router.add_post('/{tail:.*}',self.handle)
        self.runner=web.AppRunner(app);await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.base=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self
    async def handle(self,request):
        self.got.append(dict(path=request.path,query=dict(request.query),headers=dict(request.headers),body=await request.read()))
        return web.Response(status=self.status)
    async def stop(self):await self.runner.cleanup()


class AlertTargetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server=await Receiver().start();self.core=Core()
        self.alerts=AlertTargets(self.core,logging.getLogger('test-alerts'),clock=lambda:1700000000)
    async def asyncTearDown(self):
        await self.alerts.close();await self.server.stop()

    def add(self,**target):
        return self.alerts.save([{**dict(type='webhook',url=self.server.base+'/hook'),**target}])[0]

    async def send(self,title='🛑 Printer error',message='**AMS A** slot 2 ran out',color=RED):
        self.alerts.dispatch('H2D',title,message,color)
        await asyncio.gather(*list(self.alerts.tasks))
        return self.server.got

    async def test_ntfy(self):
        self.add(type='ntfy',url=self.server.base+'/printers',token='tk_1')
        got=(await self.send())[0]
        self.assertEqual(got['path'],'/printers');self.assertEqual(got['body'].decode(),'AMS A slot 2 ran out')   # markdown removed
        self.assertEqual(got['query'],{'title':'H2D: 🛑 Printer error','priority':'high','tags':'printer'})   # UTF-8 title
        self.assertEqual(got['headers']['Authorization'],'Bearer tk_1')

    async def test_home_assistant_webhook_and_notify_service(self):
        self.alerts.save([dict(type='home_assistant',url=self.server.base+'/api/webhook/abc'),
                          dict(type='home_assistant',url=self.server.base+'/api/services/notify/mobile_app_phone',token='ha_token')])
        service,hook=sorted(await self.send(),key=lambda g:g['path'])   # /api/services… sorts before /api/webhook…
        self.assertEqual(json.loads(service['body']),{'title':'H2D: 🛑 Printer error','message':'AMS A slot 2 ran out'})
        self.assertEqual(service['headers']['Authorization'],'Bearer ha_token')
        self.assertEqual(json.loads(hook['body']),{'printer':'H2D','title':'🛑 Printer error','message':'AMS A slot 2 ran out','level':'important'})
        self.assertNotIn('Authorization',hook['headers'])

    async def test_generic_webhook_and_discord_webhook(self):
        self.alerts.save([dict(type='webhook',url=self.server.base+'/hook'),dict(type='discord_webhook',url=self.server.base+'/discord')])
        discord,hook=sorted(await self.send(title='✅ Print complete',color=0x2ECC71),key=lambda g:g['path'])
        self.assertEqual(json.loads(hook['body']),{'printer':'H2D','title':'✅ Print complete','message':'AMS A slot 2 ran out',
                                                   'level':'info','color':0x2ECC71,'time':1700000000})
        embed=json.loads(discord['body'])['embeds'][0]
        self.assertEqual((embed['title'],embed['color']),('✅ Print complete',0x2ECC71));self.assertIn('**H2D**',embed['description'])

    async def test_important_only_and_switched_off(self):
        self.alerts.save([dict(type='webhook',url=self.server.base+'/important',events='important'),
                          dict(type='webhook',url=self.server.base+'/off',enabled=False)])
        await self.send(title='✅ Print complete',color=0x2ECC71)
        self.assertEqual(self.server.got,[])
        await self.send(title='❌ Print failed',color=RED)
        self.assertEqual([g['path'] for g in self.server.got],['/important'])

    async def test_tokens_stay_on_the_pi(self):
        target=self.add(type='ntfy',token='secret')
        self.assertNotIn('token',target);self.assertTrue(target['has_token'])
        again=self.alerts.save([dict(id=target['id'],type='ntfy',url=self.server.base+'/x',token='')])   # blank keeps it
        self.assertEqual(self.core.settings['alert_targets'][0]['token'],'secret');self.assertNotIn('secret',json.dumps(again))
        self.alerts.save([dict(id=target['id'],type='ntfy',url=self.server.base+'/x',clear_token=True)])
        self.assertNotIn('token',self.core.settings['alert_targets'][0])

    async def test_validation(self):
        for bad,message in (([{'type':'sms','url':'https://x'}],'target type'),([{'type':'ntfy','url':'ftp://x'}],'http'),
                            ([{'type':'ntfy','url':'https://x','events':'some'}],'important'),('nope','list')):
            with self.assertRaisesRegex(ValueError,message):self.alerts.save(bad)

    async def test_failures_are_reported_and_test_button(self):
        await self.server.stop();self.server=await Receiver().start(status=500)
        target=self.add()
        result=await self.alerts.test(target['id'])
        self.assertTrue(result.startswith('Failed: HTTP 500'));self.assertTrue(self.alerts.public()[0]['status'].startswith('Failed'))
        with self.assertRaisesRegex(ValueError,'Save the target first'):await self.alerts.test('unknown')
        dead=self.alerts.save([dict(type='webhook',url='http://127.0.0.1:9/nothing')])[0]   # nothing listens there
        self.assertIn('Failed',await self.alerts.test(dead['id']))


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_notify_sends_without_the_discord_bot(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'config.json').write_text(json.dumps({'guild_ids':[1],'demo':True}))
            with patch.dict(os.environ,{'PM_CONFIG':str(Path(d)/'config.json'),'PM_DATA':d}):
                spec=importlib.util.spec_from_file_location('test_core_alerts',Path(__file__).parents[1]/'core.py')
                core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)
            sent=[]
            core.alert_targets=type('T',(),{'dispatch':lambda self,*a:sent.append(a)})()
            await core.notify('H2D','❌ Print failed','benchy',core.RED)   # no notification channel, bot not connected
            self.assertEqual(sent,[('H2D','❌ Print failed','benchy',core.RED)])
            await core.bot.close()


if __name__=='__main__':
    unittest.main()
