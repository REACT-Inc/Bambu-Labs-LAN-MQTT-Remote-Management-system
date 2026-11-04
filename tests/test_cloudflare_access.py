"""Google sign-in through Cloudflare Access at printers.<your domain> (cloudflare_access.py, dashboard.py).

The tests sign tokens the way Cloudflare does (RS256, with a key generated here) and serve the matching keys as the
team's certs endpoint would."""
import base64,hashlib,json,random,subprocess,tempfile,time,unittest
from pathlib import Path

from cloudflare_access import SHA256_PREFIX,Access,rsa_verify
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
        self.assertEqual(state['identity'],{'email':'admin@example.org','admin':True});self.assertTrue(state['access']['enabled'])
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


class PageTests(unittest.TestCase):
    def test_the_page(self):
        page,app=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text()
        self.assertIn('href="/cdn-cgi/access/logout"',page);self.assertIn('id="accessSignOut"',page)
        for gone in ('manageDialog','managePassword','manageLock'):self.assertNotIn(gone,page)
        self.assertNotIn('manage/unlock',app);self.assertIn('function lockedText()',app)

    def test_the_setup_script(self):
        script=(ROOT/'cloudflare-tunnel.sh').read_text()
        self.assertEqual(subprocess.run(['bash','-n',str(ROOT/'cloudflare-tunnel.sh')]).returncode,0)
        for part in ('cloudflared service install "$TOKEN"','https://pkg.cloudflare.com/cloudflare-main.gpg',"config['cloudflare_access']",'--remove'):
            self.assertIn(part,script)


if __name__=='__main__':
    unittest.main()
