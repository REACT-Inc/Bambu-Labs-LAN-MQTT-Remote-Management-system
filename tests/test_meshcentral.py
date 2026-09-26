import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import aiohttp
from queueing import Store
from meshcentral_client import MeshCentral


class MeshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db')
        self.store.db.execute('CREATE TABLE device_tasks(id TEXT PRIMARY KEY,device TEXT,action TEXT,status TEXT,created REAL,result TEXT)')
        self.mesh=MeshCentral(SimpleNamespace(DATA_DIR=Path(self.tmp.name)),self.store)
        self.mesh.save(dict(url='https://mesh.example.test/customer',username='token-user',password='secret',prefix='REACT-'))
    async def asyncTearDown(self):
        await self.mesh.close(None);self.store.db.close();self.tmp.cleanup()
    def nodes(self):return {'nodes':{'mesh//group':[{'_id':'node//one','name':'REACT-1','conn':1,'agent':{'id':4}}, {'_id':'node//other','name':'OTHER','conn':1,'agent':{'id':4}}, {'_id':'node//off','name':'REACT-2','conn':0,'agent':{'id':4}}]}}
    def test_credentials_private_and_retarget_protected(self):
        self.assertNotIn('secret',json.dumps(self.mesh.public()))
        self.assertEqual(self.mesh.path.stat().st_mode&0o777,0o600)
        with self.assertRaises(ValueError):self.mesh.save(dict(url='https://different.test',username='token-user',password='',prefix='REACT-'))
        with self.assertRaises(ValueError):self.mesh.save(dict(url='http://mesh.test',username='x',password='x'))
    def test_devices_filtered_and_connection_state(self):
        result=self.mesh.parse_nodes(self.nodes());self.assertEqual(len(result),2)
        self.assertTrue(result[0]['online']);self.assertFalse(result[1]['online'])
        self.assertIn('hostname',result[0]['report']['commands'])
    async def test_offline_unknown_and_arbitrary_commands_blocked(self):
        self.mesh.request=AsyncMock(return_value=self.nodes())
        for node,cmd in [('node//off','hostname'),('node//other','hostname'),('node//one','rm -rf /')]:
            with self.assertRaises(ValueError):await self.mesh.command(node,cmd)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM device_tasks').fetchone()[0],0)
    async def test_command_result_and_no_shell_interpolation(self):
        self.mesh.request=AsyncMock(side_effect=[self.nodes(),{'result':'REACT-1'}])
        task=await self.mesh.command('node//one','hostname');await asyncio.gather(*self.mesh.tasks)
        row=self.store.db.execute('SELECT * FROM device_tasks WHERE id=?',(task,)).fetchone()
        self.assertEqual(row['status'],'response');self.assertEqual(row['result'],'REACT-1')
        payload=self.mesh.request.call_args.args[1]
        self.assertEqual(payload['nodeids'],['node//one']);self.assertEqual(payload['cmds'],'hostname');self.assertTrue(payload['reply'])
    async def test_unknown_results_not_retried(self):
        calls=[]
        async def request(action,payload=None,sent=None):
            calls.append(action)
            if action=='nodes':return self.nodes()
            sent();raise ValueError('connection lost')
        self.mesh.request=request
        task=await self.mesh.command('node//one','hostname');await asyncio.gather(*self.mesh.tasks)
        row=self.store.db.execute('SELECT status FROM device_tasks WHERE id=?',(task,)).fetchone()
        self.assertEqual(row['status'],'unknown');self.assertEqual(calls.count('runcommands'),1)
    async def test_protocol_headers_and_response_correlation(self):
        recorded={}
        class WS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send_json(self,data):recorded['payload']=data
            def __aiter__(self):return self
            async def __anext__(self):
                if recorded.get('returned'):raise StopAsyncIteration
                recorded['returned']=True
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps({'action':'msg','type':'runcommands','responseid':recorded['payload']['responseid'],'result':'output'}))
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def ws_connect(self,url,**kwargs):recorded.update(url=url,kwargs=kwargs);return WS()
        with patch('meshcentral_client.aiohttp.ClientSession',Session):
            result=await self.mesh.request('runcommands',{'nodeids':['node//one'],'cmds':'hostname'})
        self.assertEqual(result['result'],'output')
        self.assertEqual(recorded['url'],'wss://mesh.example.test/customer/control.ashx')
        auth=recorded['kwargs']['headers']['x-meshauth'].split(',')
        self.assertEqual(base64.b64decode(auth[0]),b'token-user');self.assertEqual(base64.b64decode(auth[1]),b'secret')
        self.assertTrue(recorded['kwargs']['ssl'].check_hostname)

class MeshHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import logging
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard,password_hash
        from queueing import Engine
        from team import Team
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name);self.store=Store(d/'db')
        core=SimpleNamespace(DATA_DIR=d,CONFIG={},ALLOWED_GUILD_IDS={123},EXAMPLE_MODE=True,log=logging.getLogger('test'))
        self.dashboard=Dashboard(core,self.store,Engine(core,self.store));self.team=Team(core,self.store,self.dashboard)
        (d/'auth.json').write_text(json.dumps(password_hash('test-password-123')))
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close();self.tmp.cleanup()
    async def test_mesh_api_auth_settings_and_result(self):
        self.assertEqual((await self.client.get('/api/team')).status,401)
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'});csrf=(await r.json())['csrf'];headers={'X-PM':'1','X-CSRF':csrf}
        data=dict(url='https://mesh.example.test',username='token-user',password='secret-value',prefix='REACT-')
        self.assertEqual((await self.client.post('/api/team/meshsettings',json=data,headers={'X-PM':'1'})).status,403)
        nodes={'nodes':{'mesh//g':[{'_id':'node//one','name':'REACT-1','conn':1,'agent':{'id':4}}]}}
        self.team.mesh.request=AsyncMock(return_value=nodes)
        self.assertEqual((await self.client.post('/api/team/meshsettings',json=data,headers=headers)).status,200)
        state=await (await self.client.get('/api/team')).json()
        self.assertEqual(state['devices'][0]['name'],'REACT-1');self.assertNotIn('secret-value',json.dumps(state))
        self.assertEqual((await self.client.post('/api/team/command',json={'device':'node//one','command':'hostname'},headers=headers)).status,400)
        self.team.mesh.request=AsyncMock(side_effect=[nodes,{'result':'REACT-1'}])
        r=await self.client.post('/api/team/command',json={'device':'node//one','command':'hostname','confirmed':True},headers=headers)
        self.assertEqual(r.status,200);task=(await r.json())['id'];await asyncio.gather(*self.team.mesh.tasks)
        row=self.store.db.execute('SELECT result FROM device_tasks WHERE id=?',(task,)).fetchone();self.assertEqual(row['result'],'REACT-1')
        self.assertEqual((await self.client.post('/api/team/enroll',json={'name':'old agent'},headers=headers)).status,400)
        self.assertEqual((await self.client.post('/agent/heartbeat',json={},headers={'X-PM':'1'})).status,404)
