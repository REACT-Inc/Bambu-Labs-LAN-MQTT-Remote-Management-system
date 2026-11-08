"""Google sign-in through Cloudflare Access at printers.<your domain> (cloudflare_access.py, dashboard.py).

The tests sign tokens the way Cloudflare does (RS256, with a key generated here) and serve the matching keys as the
team's certs endpoint would."""
import base64,hashlib,json,random,subprocess,tempfile,time,unittest
from pathlib import Path

from cloudflare_access import SHA256_PREFIX,Access,actor,clean,probe,rsa_verify
from queueing import Engine,Store
from tests.test_slicer import Core

ROOT=Path(__file__).parents[1]
TEAM='printroom.cloudflareaccess.com'
AUD='4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2'
HOST='printers.example.org'


def prime(rng,bits):
    while True:
        n=rng.getrandbits(bits)|(1<<(bits-1))|1
        if all(n%p for p in (3,5,7,11,13,17,19,23,29,31,37)) and all(pow(a,n-1,n)==1 for a in (2,3,5,7,11,13,17)):
            d,s=n-1,0
            while d%2==0:d//=2;s+=1
            def witness(a):
                x=pow(a,d,n)
                if x in (1,n-1):return False
                for _ in range(s-1):
                    x=pow(x,2,n)
                    if x==n-1:return False
                return True
            if not any(witness(rng.randrange(2,n-2)) for _ in range(20)):return n


def keypair(seed):
    rng=random.Random(seed);e=65537
    while True:
        p,q=prime(rng,512),prime(rng,512)
        phi=(p-1)*(q-1)
        if p!=q and phi%e:return p*q,e,pow(e,-1,phi)


KEYS={'key-1':keypair(1),'key-2':keypair(2)}
OTHER=keypair(3)   # not one of the team's keys


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def jwk(kid,key):
    n,e,_=key
    return {'kid':kid,'kty':'RSA','alg':'RS256','use':'sig','e':b64(e.to_bytes(3,'big')),'n':b64(n.to_bytes(128,'big'))}


def sign(claims=None,kid='key-1',key=None,alg='RS256',**extra):
    n,_,d=key or KEYS[kid]
    now=int(time.time())
    body={'aud':[AUD],'email':'admin@example.org','iss':f'https://{TEAM}','iat':now,'nbf':now,'exp':now+3600,'type':'app',**(claims or {}),**extra}
    signing=f"{b64(json.dumps({'alg':alg,'kid':kid,'typ':'JWT'}).encode())}.{b64(json.dumps(body).encode())}"
    digest=SHA256_PREFIX+hashlib.sha256(signing.encode()).digest()
    padded=b'\x00\x01'+b'\xff'*(128-len(digest)-3)+b'\x00'+digest
    return signing+'.'+b64(pow(int.from_bytes(padded,'big'),d,n).to_bytes(128,'big'))


class Certs:
    def __init__(self,*kids):self.kids,self.calls=list(kids),0
    async def __call__(self):
        self.calls+=1
        return {'keys':[jwk(k,KEYS[k]) for k in self.kids]}


def access(**config):
    return Access({'team_domain':TEAM,'aud':AUD,'hostname':HOST,'admins':['admin@example.org'],**config},fetch=Certs('key-1','key-2'))


class TokenTests(unittest.IsolatedAsyncioTestCase):
    def test_rs256_verification(self):
        n,e,d=KEYS['key-1'];message=b'hello'
        digest=SHA256_PREFIX+hashlib.sha256(message).digest()
        good=pow(int.from_bytes(b'\x00\x01'+b'\xff'*(128-len(digest)-3)+b'\x00'+digest,'big'),d,n).to_bytes(128,'big')
        self.assertTrue(rsa_verify(n,e,message,good))
        self.assertFalse(rsa_verify(n,e,b'hellp',good))
        self.assertFalse(rsa_verify(n,e,message,good[:-1]+bytes([good[-1]^1])))
        self.assertFalse(rsa_verify(n,e,message,good[1:]))

    async def test_a_cloudflare_token_is_accepted(self):
        a=access()
        claims=await a.verify(sign())
        self.assertEqual(claims['email'],'admin@example.org')
        self.assertEqual((await a.verify(sign(kid='key-2')))['email'],'admin@example.org')
        self.assertEqual(a.fetch.calls,1)   # the keys are kept

    async def test_bad_tokens_are_refused(self):
        a=access();now=int(time.time())
        n,e,d=KEYS['key-1']
        cases={'another application':sign(aud=['someone-else']),'another team':sign(iss='https://evil.cloudflareaccess.com'),
               'expired':sign(exp=now-600),'not valid yet':sign(nbf=now+600),'not RS256':sign(alg='HS256'),
               'signed by another key':sign(key=OTHER),'garbage':'not.a.token','empty':'',
               'tampered':(lambda t:t.split('.')[0]+'.'+b64(json.dumps({'aud':[AUD],'email':'evil@example.org','iss':f'https://{TEAM}','exp':now+60}).encode())+'.'+t.split('.')[2])(sign())}
        for name,token in cases.items():
            with self.assertRaises(ValueError,msg=name):await a.verify(token)
        self.assertEqual((await a.verify(sign(exp=now-30)))['email'],'admin@example.org')   # (a minute's clock difference is fine)

    async def test_unknown_keys_are_looked_up_again_but_not_too_often(self):
        a=Access({'team_domain':TEAM,'aud':AUD,'hostname':HOST},fetch=Certs('key-1'))
        await a.verify(sign())
        a.fetch.kids=['key-1','key-2']   # Cloudflare rotated its keys
        with self.assertRaises(ValueError):await a.verify(sign(kid='key-2'))   # (looked again less than a minute ago)
        a.tried_at-=61
        self.assertTrue(await a.verify(sign(kid='key-2')))
        self.assertEqual(a.fetch.calls,2)
        for _ in range(5):
            with self.assertRaises(ValueError):await a.verify(sign(kid='key-9',key=OTHER))
        self.assertEqual(a.fetch.calls,2)

    def test_settings(self):
        self.assertFalse(Access({}).enabled);self.assertFalse(Access({'team_domain':TEAM,'aud':AUD}).enabled)
        a=Access({'team_domain':'https://'+TEAM.upper()+'/','aud':AUD,'hostname':'Printers.Example.org.','admins':['Boss@Example.org','@team.example.org'],'lan':'sideways'})
        self.assertTrue(a.enabled);self.assertEqual((a.team,a.hostname,a.lan),(TEAM,HOST,'printing'))
        self.assertTrue(a.is_admin('boss@example.org'));self.assertTrue(a.is_admin('anyone@team.example.org'))
        self.assertFalse(a.is_admin('anyone@example.org'));self.assertFalse(a.is_admin('x@evilteam.example.org'));self.assertFalse(a.is_admin(''))
        self.assertTrue(Access({'team_domain':TEAM,'aud':AUD,'hostname':HOST}).is_admin('anyone@gmail.com'))   # no admins: everyone let in


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def start(self,**config):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'Mini':{'model':'A1 mini'}})
        self.core.CONFIG={'cloudflare_access':{'team_domain':TEAM,'aud':AUD,'hostname':HOST,'admins':['admin@example.org'],**config}} if config is not None else {}
        self.core.settings.update(notification_channel_id=123456789012345678)
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        self.dashboard.access.fetch=Certs('key-1','key-2')
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
    async def asyncTearDown(self):
        for task in list(self.engine.tasks):task.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()

    async def state(self,headers=None):
        r=await self.client.get('/api/state',headers=headers or {})
        return r.status,await r.json()

    async def queue(self,csrf,headers=None):
        return (await self.client.post('/api/jobs',json={'printer':'Mini','label':'Part','remote':'part.3mf'},headers={'X-PM':'1','X-CSRF':csrf,**(headers or {})})).status

    async def test_through_cloudflare_admins_manage(self):
        await self.start()
        cf={'Host':HOST,'Cf-Ray':'8a1b2c3d4e5f-AMS','Cf-Access-Jwt-Assertion':sign()}
        status,state=await self.state(cf)
        self.assertEqual(status,200);self.assertTrue(state['manage'])
        self.assertEqual(state['identity'],{'email':'admin@example.org','admin':True,'print':True,'blocked':False});self.assertTrue(state['access']['enabled'])
        self.assertEqual(state['settings']['notification_channel_id'],'123456789012345678')
        self.assertEqual((await self.client.get('/api/alerttargets',headers=cf)).status,200)
        self.assertEqual(await self.queue(state['csrf'],cf),200)
        # the browser's page is https://printers…, though the tunnel brings it to the Pi as plain http
        self.assertEqual(await self.queue(state['csrf'],{**cf,'Origin':f'https://{HOST}'}),200)
        self.assertEqual(await self.queue(state['csrf'],{**cf,'Origin':'https://attacker.example'}),403)
        self.assertEqual(await self.queue(state['csrf'],{'Host':'192.168.1.50','Origin':f'https://{HOST}'}),403)   # (only through Cloudflare)
        self.client.session.cookie_jar.update_cookies({'CF_Authorization':sign()})   # the cookie Access sets works too
        self.assertTrue((await self.state({'Host':HOST}))[1]['manage'])

    async def test_through_cloudflare_others_only_print(self):
        await self.start()
        cf={'Host':HOST,'Cf-Access-Jwt-Assertion':sign(email='student@gmail.com')}
        status,state=await self.state(cf)
        self.assertEqual(status,200);self.assertFalse(state['manage']);self.assertEqual(state['identity']['email'],'student@gmail.com')
        self.assertNotIn('notification_channel_id',state['settings'])
        self.assertEqual(await self.queue(state['csrf'],cf),200)
        r=await self.client.get('/api/alerttargets',headers=cf)
        self.assertEqual(r.status,403);body=await r.json();self.assertTrue(body['locked']);self.assertIn('student@gmail.com',body['error'])

    async def test_through_cloudflare_without_a_good_token_nothing(self):
        await self.start()
        for headers in ({'Host':HOST},{'Host':HOST,'Cf-Access-Jwt-Assertion':sign(key=OTHER)},
                        {'Host':HOST,'Cf-Access-Jwt-Assertion':sign(aud=['other'])},
                        {'Host':'192.168.1.50','Cf-Ray':'x'},{'Host':'192.168.1.50','Cf-Connecting-Ip':'203.0.113.9'}):
            status,body=await self.state(headers)
            self.assertEqual(status,403,headers);self.assertTrue(body['signin'],headers)
        async def down():raise OSError('no network')
        a=self.dashboard.access;a.fetch=down;a.keys,a.keys_at,a.tried_at={},0,0   # Cloudflare's keys can't be read
        status,body=await self.state({'Host':HOST,'Cf-Access-Jwt-Assertion':sign()})
        self.assertEqual(status,403);self.assertTrue(body['signin'])   # nobody let in, rather than everybody

    async def test_the_pis_own_addresses(self):
        await self.start()   # "lan": "printing" (the default)
        status,state=await self.state({'Host':'192.168.1.50'})
        self.assertEqual(status,200);self.assertFalse(state['manage']);self.assertIsNone(state['identity'])
        self.assertEqual(await self.queue(state['csrf'],{'Host':'192.168.1.50'}),200)
        r=await self.client.get('/api/alerttargets',headers={'Host':'192.168.1.50'})
        self.assertEqual(r.status,403);self.assertIn(f'https://{HOST}',(await r.json())['error'])
        self.dashboard.access.lan='full'
        self.assertEqual((await self.client.get('/api/alerttargets',headers={'Host':'192.168.1.50'})).status,200)
        self.dashboard.access.lan='off'
        self.assertEqual((await self.state({'Host':'192.168.1.50'}))[0],403)
        self.assertEqual((await self.client.get('/health')).status,200)   # (not the API)

    async def test_not_set_up_everyone_manages(self):
        await self.start(**{'aud':''})   # incomplete: off
        self.assertFalse(self.dashboard.access.enabled)
        status,state=await self.state()
        self.assertTrue(state['manage']);self.assertFalse(state['access']['enabled'])
        self.assertEqual((await self.client.get('/api/alerttargets')).status,200)
        self.assertEqual((await self.state({'Host':HOST}))[0],403)   # an unknown address, as before

    async def test_signing_in_with_the_password_still_manages(self):
        await self.start()
        from dashboard import atomic_json,password_hash
        atomic_json(Path(self.tmp.name)/'auth.json',password_hash('test-password-123'))
        self.dashboard.require_login=True
        r=await self.client.post('/api/login',json={'password':'test-password-123'},headers={'X-PM':'1'})
        self.assertEqual(r.status,200)
        self.assertEqual((await self.client.get('/api/alerttargets')).status,200)


class ViewOnlyTests(unittest.IsolatedAsyncioTestCase):
    start,state,queue,asyncTearDown=DashboardTests.start,DashboardTests.state,DashboardTests.queue,DashboardTests.asyncTearDown

    async def test_people_not_allowed_to_print_can_only_look(self):
        await self.start(operators=['@club.example.org'])
        student={'Host':HOST,'Cf-Access-Jwt-Assertion':sign(email='student@club.example.org')}
        visitor={'Host':HOST,'Cf-Access-Jwt-Assertion':sign(email='parent@gmail.com')}
        admin={'Host':HOST,'Cf-Access-Jwt-Assertion':sign()}
        status,state=await self.state(student)
        self.assertTrue(state['can_print']);self.assertFalse(state['manage'])
        self.assertEqual(await self.queue(state['csrf'],student),200)
        status,state=await self.state(visitor)
        self.assertEqual(status,200);self.assertFalse(state['can_print']);self.assertFalse(state['identity']['print'])
        self.assertEqual(len(state['printers']),1)   # can look
        r=await self.client.post('/api/jobs',json={'printer':'Mini','label':'Part','remote':'part.3mf'},headers={'X-PM':'1','X-CSRF':state['csrf'],**visitor})
        self.assertEqual(r.status,403);body=await r.json();self.assertTrue(body['viewonly']);self.assertIn('parent@gmail.com',body['error'])
        r=await self.client.post('/api/printers/Mini/light',json={'on':True},headers={'X-PM':'1','X-CSRF':state['csrf'],**visitor})
        self.assertEqual(r.status,403)
        self.assertTrue((await self.state(admin))[1]['can_print'])   # admins always may
        self.assertEqual(len(self.store.jobs('Mini')),1)

    async def test_the_pis_own_addresses_can_look_only(self):
        await self.start(lan='view')
        status,state=await self.state({'Host':'192.168.1.50'})
        self.assertEqual(status,200);self.assertFalse(state['can_print'])
        r=await self.client.post('/api/jobs',json={'printer':'Mini','label':'Part','remote':'part.3mf'},headers={'X-PM':'1','X-CSRF':state['csrf'],'Host':'192.168.1.50'})
        self.assertEqual(r.status,403);self.assertIn(f'https://{HOST}',(await r.json())['error'])

    async def test_recent_sign_ins(self):
        await self.start(lan='full')
        for email,ip in (('admin@example.org','203.0.113.5'),('parent@gmail.com','198.51.100.7'),('admin@example.org','203.0.113.5')):
            await self.state({'Host':HOST,'Cf-Connecting-Ip':ip,'Cf-Access-Jwt-Assertion':sign(email=email)})
        seen={p['email']:p for p in self.dashboard.seen()}
        self.assertEqual(set(seen),{'admin@example.org','parent@gmail.com'})
        self.assertEqual(seen['admin@example.org']['visits'],1)   # (once a minute at most)
        self.assertEqual((seen['parent@gmail.com']['address'],seen['parent@gmail.com']['level']),('198.51.100.7','print'))   # (nobody under Who may print: everyone may)
        self.dashboard.seen_at.clear()
        await self.state({'Host':HOST,'Cf-Connecting-Ip':'203.0.113.9','Cf-Access-Jwt-Assertion':sign()})
        again={p['email']:p for p in self.dashboard.seen()}['admin@example.org']
        self.assertEqual((again['visits'],again['address']),(2,'203.0.113.9'))
        body=await (await self.client.get('/api/access',headers={'Host':HOST,'Cf-Access-Jwt-Assertion':sign()})).json()
        self.assertEqual(body['seen'][0]['email'],'admin@example.org')
        self.assertEqual((await self.client.get('/api/access',headers={'Host':HOST,'Cf-Access-Jwt-Assertion':sign(email='parent@gmail.com')})).status,403)   # admins only


class LevelTests(unittest.IsolatedAsyncioTestCase):
    def test_levels(self):
        a=Access({'team_domain':TEAM,'aud':AUD,'hostname':HOST,'admins':['@staff.example.org'],'operators':['@example.org'],
                  'people':{'coach@staff.example.org':'print','kid@example.org':'view','alum@gmail.com':'admin','troll@example.org':'block','bad@example.org':'sideways'}})
        self.assertEqual([a.level(e) for e in ('head@staff.example.org','coach@staff.example.org','member@example.org','kid@example.org','alum@gmail.com','troll@example.org','x@gmail.com','')],
                         ['admin','print','print','view','admin','block','view','view'])   # their own level wins over the lists
        self.assertNotIn('bad@example.org',a.people)
        self.assertEqual(clean({'hostname':HOST,'team_domain':TEAM,'aud':AUD,'people':{'Kid@Example.org':'view'}})['people'],{'kid@example.org':'view'})
        for bad in ({'@example.org':'view'},{'kid@example.org':'boss'},['kid@example.org']):
            with self.assertRaises(ValueError,msg=bad):clean({'hostname':HOST,'team_domain':TEAM,'aud':AUD,'people':bad})

    async def test_only_application_tokens(self):
        a=access()
        with self.assertRaises(ValueError):await a.verify(sign(type='org'))
        self.assertTrue(await a.verify(sign()))

class PeopleTests(unittest.IsolatedAsyncioTestCase):
    start,state,queue,asyncTearDown=DashboardTests.start,DashboardTests.state,DashboardTests.queue,DashboardTests.asyncTearDown

    async def test_levels_from_recent_sign_ins(self):
        await self.start(operators=['@example.org'])
        admin={'Host':HOST,'Cf-Access-Jwt-Assertion':sign()}
        member={'Host':HOST,'Cf-Access-Jwt-Assertion':sign(email='member@example.org')}
        csrf=(await self.state(admin))[1]['csrf']
        mcsrf=(await self.state(member))[1]['csrf']
        async def person(email,level,headers=admin):
            r=await self.client.post('/api/access/person',json={'email':email,'level':level},headers={'X-PM':'1','X-CSRF':csrf,**headers})
            return r.status,await r.json()
        status,body=await person('member@example.org','block')
        self.assertEqual(status,200);self.assertEqual({p['email']:p for p in body['seen']}['member@example.org']['level'],'block')
        status,body=await self.state(member)   # blocked at once, despite a good Cloudflare sign-in
        self.assertEqual(status,403);self.assertTrue(body['blocked']);self.assertIn('member@example.org',body['error'])
        self.assertIn('Google sign-in: member@example.org is blocked',[e['title'] for e in self.store.events()])
        await person('member@example.org','view')
        status,state=await self.state(member);self.assertEqual(status,200);self.assertFalse(state['can_print'])   # beats the @example.org operators
        await person('member@example.org',None)
        self.assertTrue((await self.state(member))[1]['can_print'])   # back to the lists
        self.assertEqual(json.loads((Path(self.tmp.name)/'cloudflare_access.json').read_text())['people'],{})
        status,body=await person('admin@example.org','view')
        self.assertEqual(status,400);self.assertIn('your own level',body['error'])
        status,body=await person('member@example.org','boss');self.assertEqual(status,400)
        status,body=await person('member@example.org','admin',member)   # (not an admin)
        self.assertEqual(status,403)
        # saving the panel's form keeps each person's level
        await person('member@example.org','admin')
        r=await self.client.post('/api/access',json={'hostname':HOST,'team_domain':TEAM,'aud':AUD,'admins':'admin@example.org','operators':'@example.org','lan':'printing'},headers={'X-PM':'1','X-CSRF':csrf,**admin})
        self.assertEqual(r.status,200);self.assertEqual(self.dashboard.access.people,{'member@example.org':'admin'})


class AskTests(unittest.IsolatedAsyncioTestCase):
    start,state,queue,asyncTearDown=DashboardTests.start,DashboardTests.state,DashboardTests.queue,DashboardTests.asyncTearDown

    async def test_asking_to_print(self):
        await self.start(operators=['@club.example.org'])
        admin={'Host':HOST,'Cf-Access-Jwt-Assertion':sign()}
        visitor={'Host':HOST,'Cf-Access-Jwt-Assertion':sign(email='parent@gmail.com')}
        status,vstate=await self.state(visitor)
        self.assertFalse(vstate['can_print']);self.assertIsNone(vstate['my_request']);self.assertEqual(vstate['access_requests'],0)
        async def post(path,body,headers,csrf):
            r=await self.client.post(path,json=body,headers={'X-PM':'1','X-CSRF':csrf,**headers})
            return r.status,await r.json()
        status,body=await post('/api/ask-access',{'level':'print','note':'For the robotics club'},visitor,vstate['csrf'])   # allowed though view only
        self.assertEqual((status,body['wants']),(200,'print'))
        self.assertEqual((await self.state(visitor))[1]['my_request'],'print')
        status,astate=await self.state(admin)
        self.assertEqual(astate['access_requests'],1)
        seen=(await (await self.client.get('/api/access',headers=admin)).json())['seen']
        self.assertEqual((seen[0]['email'],seen[0]['wants'],seen[0]['note']),('parent@gmail.com','print','For the robotics club'))   # asking: first
        self.assertTrue(seen[0]['online']);self.assertTrue(seen[0]['new'])
        status,body=await post('/api/access/person',{'email':'parent@gmail.com','level':'print'},admin,astate['csrf'])   # Allow
        self.assertEqual(status,200);self.assertIsNone({p['email']:p for p in body['seen']}['parent@gmail.com']['wants'])
        self.assertEqual((await self.state(admin))[1]['access_requests'],0)
        status,vstate=await self.state(visitor);self.assertTrue(vstate['can_print'])
        status,body=await post('/api/ask-access',{'level':'print'},visitor,vstate['csrf'])
        self.assertEqual(status,400);self.assertIn('already',body['error'])
        # asked to be an admin, turned down
        await post('/api/ask-access',{'level':'admin'},visitor,vstate['csrf'])
        status,body=await post('/api/access/person',{'email':'parent@gmail.com','dismiss':True},admin,astate['csrf'])
        self.assertEqual(status,200);self.assertEqual((await self.state(admin))[1]['access_requests'],0)
        self.assertIn('Google sign-in: request from parent@gmail.com dismissed',[e['title'] for e in self.store.events()])
        status,body=await post('/api/ask-access',{'level':'everything'},visitor,vstate['csrf']);self.assertEqual(status,400)
        lan=(await self.state({'Host':'192.168.1.50'}))[1]
        status,body=await post('/api/ask-access',{'level':'print'},{'Host':'192.168.1.50'},lan['csrf'])
        self.assertEqual(status,400);self.assertIn(f'https://{HOST}',body['error'])


class SettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'Mini':{'model':'A1 mini'}});self.core.CONFIG={}
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        self.csrf=(await (await self.client.get('/api/state')).json())['csrf']
    async def asyncTearDown(self):
        for task in list(self.engine.tasks):task.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()

    def keys(self):   # (saving starts reading the real keys in the background: use the test's keys instead)
        a=self.dashboard.access;a.fetch=Certs('key-1','key-2');a.tried_at=0

    def settings(self,**extra):
        return {'hostname':HOST,'team_domain':TEAM,'aud':AUD,'admins':'admin@example.org','lan':'printing',**extra}

    async def post(self,path,body,headers=None):
        r=await self.client.post(path,json=body,headers={'X-PM':'1','X-CSRF':self.csrf,**(headers or {})})
        return r.status,await r.json()

    async def test_saving_from_the_pis_own_address(self):
        status,body=await self.post('/api/access',self.settings())
        self.assertEqual(status,409);self.assertTrue(body['confirm']);self.assertIn('only print',body['error'])   # would lose management here
        self.assertFalse(self.dashboard.access.enabled)
        status,body=await self.post('/api/access',self.settings(lan='full'))   # keeps management here: no question
        self.assertEqual(status,200);self.assertEqual(body['settings']['hostname'],HOST);self.assertTrue(self.dashboard.access.enabled)
        saved=json.loads((Path(self.tmp.name)/'cloudflare_access.json').read_text())
        self.assertEqual(saved,{'team_domain':TEAM,'aud':AUD,'hostname':HOST,'admins':['admin@example.org'],'operators':[],'people':{},'lan':'full'})
        self.assertIn('Google sign-in saved',[e['title'] for e in self.store.events()])
        status,body=await self.post('/api/access',{**self.settings(),'confirmed':True})
        self.assertEqual(status,200);self.assertEqual(self.dashboard.access.lan,'printing')
        self.assertEqual((await self.client.get('/api/access')).status,403)   # printing only now, applied at once
        status,body=await self.post('/api/access',self.settings(aud='nope'))
        self.assertEqual(status,403)

    async def test_saving_through_cloudflare_keeps_you_an_admin(self):
        await self.post('/api/access',self.settings(lan='full'))
        self.keys()
        cf={'Host':HOST,'Cf-Access-Jwt-Assertion':sign()}
        status,body=await self.post('/api/access',self.settings(admins='someone@example.org'),cf)
        self.assertEqual(status,409);self.assertIn('admin@example.org would no longer be an admin',body['error'])
        status,body=await self.post('/api/access',self.settings(admins='admin@example.org\nsomeone@example.org'),cf)
        self.assertEqual(status,200);self.assertEqual(body['identity']['email'],'admin@example.org')
        self.keys()
        self.assertIn('admin@example.org',self.store.events()[0]['detail'])   # Activity says who
        r=await self.client.post('/api/jobs',json={'printer':'Mini','label':'Part','remote':'part.3mf'},headers={'X-PM':'1','X-CSRF':self.csrf,**{**cf,'Cf-Access-Jwt-Assertion':sign()}})
        self.assertEqual(r.status,200);self.assertIn('admin@example.org',json.dumps(self.store.events()[:3]))
        # turning it off from the public address: asked first, since that address stops working
        status,body=await self.post('/api/access',{'remove':True},{**cf,'Cf-Access-Jwt-Assertion':sign()})
        self.assertEqual(status,409);self.assertIn('stops working',body['error'])
        status,body=await self.post('/api/access',{'remove':True,'confirmed':True},{**cf,'Cf-Access-Jwt-Assertion':sign()})
        self.assertEqual(status,200);self.assertIsNone(body['settings']);self.assertFalse(self.dashboard.access.enabled)
        self.assertEqual(json.loads((Path(self.tmp.name)/'cloudflare_access.json').read_text()),{})

    async def test_the_saved_file_wins_over_config_json(self):
        from dashboard import Dashboard
        self.core.CONFIG={'cloudflare_access':{'team_domain':TEAM,'aud':AUD,'hostname':HOST}}
        self.assertTrue(Dashboard(self.core,self.store,self.engine).access.enabled)
        (Path(self.tmp.name)/'cloudflare_access.json').write_text('{}')   # turned off in Settings
        self.assertFalse(Dashboard(self.core,self.store,self.engine).access.enabled)

    async def test_test_button(self):
        import cloudflare_access,dashboard
        async def fake_probe(team,hostname,get=None):return 'protected',f'https://{hostname} asks for a Google sign-in.'
        async def fake_fetch(self):return {'keys':[jwk('key-1',KEYS['key-1'])]}
        old=dashboard.probe_access,cloudflare_access.Access.fetch_keys
        dashboard.probe_access,cloudflare_access.Access.fetch_keys=fake_probe,fake_fetch
        try:
            status,body=await self.post('/api/access/test',self.settings())
        finally:
            dashboard.probe_access,cloudflare_access.Access.fetch_keys=old
        self.assertEqual(status,200);self.assertTrue(body['keys']['ok']);self.assertIn('1 signing keys',body['keys']['text'])
        self.assertEqual(body['address']['state'],'protected')

    async def test_status_icon(self):
        await self.post('/api/access',self.settings(lan='full'))
        a=self.dashboard.access
        async def down():raise OSError('no route to host')
        a.fetch=down;await a.warm()
        self.dashboard.tunnel=False
        keys=[i['key'] for i in self.dashboard.access_status()]
        self.assertEqual(keys,['access-keys','access-tunnel']);self.assertIn('no route to host',self.dashboard.access_status()[0]['detail'])
        a.fetch=Certs('key-1');await a.warm();self.dashboard.tunnel=True
        self.assertEqual(self.dashboard.access_status(),[]);self.assertEqual(a.status()['keys'],1)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_are_checked(self):
        good={'hostname':'https://Printers.Example.org/','team_domain':'PrintRoom','aud':AUD.upper(),'admins':'Boss@Example.org\n@staff.example.org, boss@example.org','lan':'full'}
        self.assertEqual(clean(good),{'hostname':HOST,'team_domain':TEAM,'aud':AUD,'admins':['boss@example.org','@staff.example.org'],'operators':[],'people':{},'lan':'full'})
        self.assertEqual(clean({**good,'operators':'Student@Example.org\n@club.example.org'})['operators'],['student@example.org','@club.example.org'])
        with self.assertRaises(ValueError):clean({**good,'operators':'everyone'})
        self.assertEqual(clean({**good,'lan':'view'})['lan'],'view')
        self.assertEqual(clean({**good,'admins':'','lan':None})['admins'],[])
        for key,bad in (('hostname','printers'),('hostname','printers .example.org'),('team_domain','printroom.example.com'),
                        ('aud','abc'),('aud',AUD+'0'),('admins','not an email'),('lan','sometimes')):
            with self.assertRaises(ValueError,msg=(key,bad)):clean({**good,key:bad})
        with self.assertRaises(ValueError):clean('nope')

    async def test_is_the_address_behind_access(self):
        def answer(*result):
            async def get(url):
                self.assertEqual(url,f'https://{HOST}/');return result
            return get
        login=f'https://{TEAM}/cdn-cgi/access/login/{HOST}?kid=abc'
        self.assertEqual((await probe(TEAM,HOST,answer(302,login,'')))[0],'protected')
        self.assertEqual((await probe(TEAM,HOST,answer(302,'https://other.cloudflareaccess.com/cdn-cgi/access/login/x','')))[0],'other-team')
        self.assertEqual((await probe(TEAM,HOST,answer(200,'','<html>3D Printer Management')))[0],'open')
        self.assertEqual((await probe(TEAM,HOST,answer(403,'','{"error":"Unknown address"}')))[0],'open')   # it reached the Pi
        self.assertEqual((await probe(TEAM,HOST,answer(530,'','error code: 1033')))[0],'tunnel')
        async def down(url):raise OSError('no route')
        self.assertEqual((await probe(TEAM,HOST,down))[0],'unreachable')

    def test_who_did_it(self):
        self.assertEqual(actor({'identity':{'email':'boss@example.org','admin':True}}),'boss@example.org')
        self.assertEqual(actor({'identity':None}),'web administrator');self.assertEqual(actor({}),'web administrator')


class PageTests(unittest.TestCase):
    def test_the_page(self):
        page,app=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text()
        self.assertIn('href="/cdn-cgi/access/logout"',page);self.assertIn('id="accessSignOut"',page)
        for gone in ('manageDialog','managePassword','manageLock'):self.assertNotIn(gone,page)
        self.assertNotIn('manage/unlock',app);self.assertIn('function lockedText()',app)

    def test_the_setup_script(self):
        script=(ROOT/'cloudflare-tunnel.sh').read_text()
        self.assertEqual(subprocess.run(['bash','-n',str(ROOT/'cloudflare-tunnel.sh')]).returncode,0)
        for part in ('cloudflared service install "$TOKEN"','https://pkg.cloudflare.com/cloudflare-main.gpg','SETTINGS="$DATA/cloudflare_access.json"',
                     '--remove','--domain) HOST="printers.${2:-}"','--operator) OPERATORS+=','from cloudflare_access import clean','asks for a Google sign-in'):
            self.assertIn(part,script)

    def test_the_settings_panel(self):
        page,app=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text()
        for id in ('accessForm','accessHostname','accessTeam','accessAud','accessAdmins','accessOperators','accessLan','accessTest','accessRemove','accessConfirm','whoami',
                   'accessSeen','signinBanner','signinAgain','viewBanner'):
            self.assertIn(f'id="{id}"',page)
        self.assertIn("api('access/person',{email:sel.dataset.person,level:sel.value||null})",app)
        self.assertIn("api('ask-access',{level:'print'})",app);self.assertIn("document.querySelector('nav button[data-tab=settings]').dataset.badge=waiting",app)
        self.assertIn("api('access/test',accessData())",app);self.assertIn('<option value="view">',page)
        self.assertIn("if(r.status===403&&result.signin)$('signinBanner').hidden=false;",app);self.assertIn("document.body.classList.toggle('view-only',state.can_print===false)",app)
        self.assertIn('err.result?.confirm',app)
        self.assertIn("throw Object.assign(new Error(result.error||'Request failed'),{result});",app.replace("if(r.status===403&&result.signin)$('signinBanner').hidden=false;",''))


if __name__=='__main__':
    unittest.main()
