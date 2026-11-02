"""Without signing in, printing is open but management asks for the dashboard password first (dashboard.py).

Discord, team tools, laptops, the server, settings, problem reports and installing updates: each browser unlocks them
with the password for MANAGE_HOURS, and Lock management (or a new password) locks them again."""
import tempfile,time,unittest
from pathlib import Path

from queueing import Engine,Store
from tests.test_slicer import Core

ROOT=Path(__file__).parents[1]


class ManagementLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,atomic_json,password_hash
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'Mini':{'model':'A1 mini'}});self.core.CONFIG={}
        self.core.settings.update(notification_channel_id=123456789012345678,alert_ping_users=[223456789012345678])
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        atomic_json(d/'auth.json',password_hash('test-password-123'))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        self.state=await (await self.client.get('/api/state')).json()
        self.headers={'X-PM':'1','X-CSRF':self.state['csrf']}
    async def asyncTearDown(self):
        for task in list(self.engine.tasks):task.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()

    async def post(self,path,body=None):
        return await self.client.post(path,json=body if body is not None else {},headers=self.headers)

    async def unlock(self,password='test-password-123'):
        return await self.post('/api/manage/unlock',{'password':password})

    async def test_printing_is_open_management_is_locked(self):
        self.assertFalse(self.state['manage'])
        self.assertNotIn('notification_channel_id',self.state['settings']);self.assertNotIn('alert_ping_users',self.state['settings'])
        r=await self.post('/api/jobs',{'printer':'Mini','label':'Part','remote':'part.3mf'})
        self.assertEqual(r.status,200)
        for method,path in (('GET','/api/team'),('GET','/api/setup'),('GET','/api/permissions'),('GET','/api/alerttargets'),
                            ('GET','/api/diagnostics'),('GET','/api/errors'),('GET','/api/report/settings'),
                            ('POST','/api/settings'),('POST','/api/setup/discord'),('POST','/api/setup/printer'),
                            ('POST','/api/team/settings'),('POST','/api/password'),('POST','/api/testnotification'),
                            ('POST','/api/update/install'),('POST','/api/github/settings'),('POST','/api/slicer/workers/enroll')):
            r=await (self.client.get(path) if method=='GET' else self.post(path))
            self.assertEqual(r.status,403,path);self.assertTrue((await r.json())['locked'],path)
        for path in ('/api/update/status','/api/github/status','/api/slicer/workers','/api/library'):   # looking is fine
            self.assertEqual((await self.client.get(path)).status,200,path)

    async def test_the_password_unlocks_this_browser(self):
        r=await self.unlock('wrong-password')
        self.assertEqual(r.status,403);self.assertNotIn('locked',await r.json())   # not 401: that's the sign-in screen
        self.assertEqual((await self.unlock()).status,200)
        state=await (await self.client.get('/api/state')).json()
        self.assertTrue(state['manage']);self.assertEqual(state['settings']['notification_channel_id'],'123456789012345678')
        self.assertEqual((await self.client.get('/api/alerttargets')).status,200)
        self.assertIn('Management unlocked',[e['title'] for e in self.store.events()])
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.dashboard.app)) as other:   # another browser is still locked
            self.assertFalse((await (await other.get('/api/state')).json())['manage'])
            self.assertEqual((await other.get('/api/alerttargets')).status,403)

    async def test_lock_expiry_and_a_new_password_lock_it_again(self):
        await self.unlock()
        self.assertEqual((await self.post('/api/manage/lock')).status,200)
        self.assertEqual((await self.client.get('/api/alerttargets')).status,403)
        await self.unlock()
        session=next(s for s in self.dashboard.sessions.values() if s['csrf']==self.state['csrf'])
        session['manage_until']=time.time()-1
        self.assertEqual((await self.client.get('/api/alerttargets')).status,403)
        await self.unlock()
        self.assertEqual((await self.post('/api/password',{'password':'a-new-password-456'})).status,200)
        self.assertEqual((await self.client.get('/api/alerttargets')).status,403)   # locked, but the page still works
        self.assertEqual((await self.post('/api/jobs',{'printer':'Mini','label':'Part','remote':'part.3mf'})).status,200)
        self.assertEqual((await self.unlock()).status,403);self.assertEqual((await self.unlock('a-new-password-456')).status,200)

    async def test_wrong_passwords_wait(self):
        for _ in range(8):await self.unlock('wrong-password')
        self.assertEqual((await self.unlock()).status,429)

    async def test_a_session_is_needed(self):
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.dashboard.app)) as fresh:   # no cookie: nothing to unlock
            r=await fresh.post('/api/manage/unlock',json={'password':'test-password-123'},headers=self.headers)
            self.assertEqual(r.status,403);self.assertFalse(self.dashboard.anonymous.get('manage_until'))

    async def test_signing_in_or_turning_it_off_means_management(self):
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.assertEqual((await self.client.get('/api/alerttargets')).status,200)
        self.dashboard.require_login=True
        self.assertTrue((await (await self.client.get('/api/state')).json())['manage'])
        self.dashboard.require_login=False;self.dashboard.protect_management=False
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.dashboard.app)) as fresh:
            self.assertTrue((await (await fresh.get('/api/state')).json())['manage'])
            self.assertEqual((await fresh.get('/api/alerttargets')).status,200)

    def test_the_page(self):
        page,app,team=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text(),(ROOT/'static/team.js').read_text()
        for id in ('manageDialog','manageForm','managePassword','manageError','manageLock'):self.assertIn(f'id="{id}"',page)
        self.assertIn("const MANAGED=new Set(['team','devices','server','settings']);",app)
        self.assertIn("if(r.status===403&&result.locked)askUnlock();",app)
        self.assertIn('showManage();',app);self.assertIn('!state.manage||teamBusy',team)


if __name__=='__main__':
    unittest.main()
