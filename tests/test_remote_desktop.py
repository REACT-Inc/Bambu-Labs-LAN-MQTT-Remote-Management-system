"""Remote desktop links for MeshCentral laptops in the dashboard (#61)."""
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from queueing import Store
from laptopManagement_Intergration.meshCentral.meshcentral_client import MeshCentral


class RemoteDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db')
        self.store.db.execute('CREATE TABLE device_tasks(id TEXT PRIMARY KEY,device TEXT,action TEXT,status TEXT,created REAL,result TEXT)')
        self.mesh=MeshCentral(SimpleNamespace(DATA_DIR=Path(self.tmp.name)),self.store)
        self.mesh.save(dict(url='https://mesh.example.test/team',username='token-user',password='secret',prefix='REACT-'))
        nodes={'nodes':{'mesh//g':[{'_id':'node//AbCdEf123456','name':'REACT-1','conn':1,'agent':{'id':4,'caps':7}},
                                   {'_id':'node//NoDesk123456','name':'REACT-2','conn':1,'agent':{'id':6,'caps':6}},
                                   {'_id':'node//Offline12345','name':'REACT-3','conn':0,'agent':{'id':4,'caps':15}},
                                   {'_id':'node//OldServer1234','name':'REACT-4','conn':1,'agent':{'id':4}}]}}
        self.mesh.request=AsyncMock(return_value=nodes)
    async def asyncTearDown(self):
        await self.mesh.close(None);self.store.db.close();self.tmp.cleanup()

    async def test_desktop_capability_and_url(self):
        devices={d['name']:d['desktop'] for d in await self.mesh.devices(force=True)}
        self.assertEqual(devices,{'REACT-1':True,'REACT-2':False,'REACT-3':True,'REACT-4':True})   # no caps: Windows agents can
        node,url=await self.mesh.desktop_url('node//AbCdEf123456')
        self.assertEqual((node['name'],url),('REACT-1','https://mesh.example.test/team/?gotonode=AbCdEf123456&viewmode=11&hide=31'))
        self.assertNotIn('secret',url);self.assertTrue(self.mesh.public()['remote_desktop'])

    async def test_refusals(self):
        for device,message in [('node//NoDesk123456',"doesn't offer remote desktop"),('node//Offline12345','offline'),('node//nope12345678','not in the MeshCentral list')]:
            with self.assertRaisesRegex(ValueError,message):await self.mesh.desktop_url(device)
        self.mesh.save(dict(url='https://mesh.example.test/team',username='token-user',password='',prefix='REACT-',remote_desktop=False))
        self.assertFalse(self.mesh.public()['remote_desktop'])
        with self.assertRaisesRegex(ValueError,'switched off'):await self.mesh.desktop_url('node//AbCdEf123456')

    async def test_team_action_records_who_opened_it(self):
        from team import Team
        from aiohttp import web
        core=SimpleNamespace(DATA_DIR=Path(self.tmp.name),ALLOWED_GUILD_IDS=set(),bot=SimpleNamespace(is_ready=lambda:False))
        team=Team(core,self.store,SimpleNamespace(app=web.Application()));team.mesh=self.mesh
        request=SimpleNamespace(match_info={'action':'desktop'},json=AsyncMock(return_value={'device':'node//AbCdEf123456'}))
        response=await team.web_action(request)
        self.assertIn(b'gotonode=AbCdEf123456',response.body)
        self.assertEqual((self.store.events()[0]['title'],self.store.events()[0]['detail']),('Remote desktop opened','REACT-1 • web administrator'))


if __name__=='__main__':
    unittest.main()
