"""The dashboard without signing in (the default; "require_login": true in config.json brings the password back).

Anyone who can reach it can use it, but other websites still can't send commands through a visitor's browser: the
page gets a session and a CSRF token when it loads, changes need both (plus the X-PM header and a matching Origin),
and only addresses a website can't take over with a DNS trick are answered."""
import tempfile,unittest
from pathlib import Path

from queueing import Engine,Store
from tests.test_slicer import Core

ROOT=Path(__file__).parents[1]


class OpenDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'Mini':{'model':'A1 mini'}});self.core.CONFIG={'allowed_hosts':['printers.example.org']}
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
    async def asyncTearDown(self):
        for task in list(self.engine.tasks):task.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()

    async def test_the_page_opens_without_a_password(self):
        r=await self.client.get('/api/state')
        state=await r.json()
        self.assertEqual((r.status,state['login']),(200,False));self.assertTrue(state['csrf'])
        self.assertIn('pm_session',r.cookies)
        again=await (await self.client.get('/api/state')).json()
        self.assertEqual(again['csrf'],state['csrf'])   # the same session, kept by its cookie
        r=await self.client.post('/api/jobs',json={'printer':'Mini','label':'Part','remote':'part.3mf'},headers={'X-PM':'1','X-CSRF':state['csrf']})
        self.assertEqual(r.status,200);self.assertEqual(len(self.store.jobs('Mini')),1)
        self.assertEqual((await self.client.get('/api/desktop')).status,200)

    async def test_other_websites_still_cant_send_commands(self):
        csrf=(await (await self.client.get('/api/state')).json())['csrf']
        job={'printer':'Mini','label':'Part','remote':'part.3mf'}
        cases=[{'X-PM':'1'},                                                      # no CSRF token (another site can't read it)
               {'X-PM':'1','X-CSRF':'guessed'},
               {'X-CSRF':csrf},                                                    # a plain form post can't add X-PM
               {'X-PM':'1','X-CSRF':csrf,'Origin':'https://attacker.example'}]
        for headers in cases:
            self.assertEqual((await self.client.post('/api/jobs',json=job,headers=headers)).status,403,headers)
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.dashboard.app)) as fresh:   # no session cookie at all
            r=await fresh.post('/api/jobs',json=job,headers={'X-PM':'1','X-CSRF':csrf})
            self.assertEqual(r.status,403);self.assertIn('reload',(await r.json())['error'])
        self.assertEqual(self.store.jobs('Mini'),[])

    async def test_only_addresses_a_website_cant_take_over(self):
        for host in ('127.0.0.1:8080','192.168.1.50','[fd00::1]:8080','[::1]','raspberrypi','printers.local:8080','pi.tail1234.ts.net','pi.lan',
                     'printers.example.org'):
            self.assertEqual((await self.client.get('/api/state',headers={'Host':host})).status,200,host)
        for host in ('attacker.example','printers.local.attacker.example','rebind.example.com:8080'):
            r=await self.client.get('/api/state',headers={'Host':host})
            self.assertEqual(r.status,403,host);self.assertIn('allowed_hosts',(await r.json())['error'])
        self.assertEqual((await self.client.get('/',headers={'Host':'attacker.example'})).status,200)   # the page itself has no data

    async def test_the_password_can_still_be_required(self):
        self.dashboard.require_login=True
        self.assertEqual((await self.client.get('/api/state')).status,401)
        self.assertEqual((await self.client.get('/api/state',headers={'Host':'attacker.example'})).status,401)   # (no host check: the password protects it)
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.assertEqual(r.status,200)
        self.assertTrue((await (await self.client.get('/api/state')).json())['login'])

    def test_sessions_are_capped(self):
        for _ in range(5100):self.dashboard.new_session(1)
        self.assertLessEqual(len(self.dashboard.sessions),5000)

    def test_the_page(self):
        page,app=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text()
        self.assertIn('id="accessNote"',page);self.assertIn('id="passwordSave"',page)
        self.assertIn("$('logout').hidden=!state.login",app);self.assertIn('showAccess();',app)


if __name__=='__main__':
    unittest.main()
