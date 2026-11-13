"""Member updates by DM (discord_Intergration/member_watch.py): what changed, batching, sending, intents, settings."""
import asyncio,datetime,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from discord_Intergration import member_watch as mw
from tests.test_slicer import Core

ROOT=Path(__file__).parents[1]
GUILD=111


def role(i,name):return SimpleNamespace(id=i,name=name)


def presence(status='online',activities=(),**kw):return SimpleNamespace(status=status,activities=list(activities),**kw)


class Bot:
    def __init__(self,forbidden=()):
        self.users={};self.forbidden=set(forbidden);self.intents=discord.Intents.default()
    def get_user(self,i):return self.users.get(i)
    async def fetch_user(self,i):
        user=SimpleNamespace(id=i,sent=[])
        async def send(embed):
            if i in self.forbidden:raise discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'Cannot send messages to this user')
            user.sent.append(embed)
        user.send=send;self.users[i]=user;return user


def fake_core(**watch):
    core=SimpleNamespace(settings={'member_watch':{'enabled':True,'recipients':[900],'watch':list(mw.KINDS),**watch}},ALLOWED_GUILD_IDS={GUILD},
                         bot=Bot(),log=__import__('logging').getLogger('test'),card=lambda title,text:SimpleNamespace(title=title,description=text))
    return core


def member(i=5,name='Sam',bot=False,guild=GUILD,**kw):
    return SimpleNamespace(id=i,display_name=name,name=name.lower(),bot=bot,guild=SimpleNamespace(id=guild),**kw)


class ChangeTests(unittest.TestCase):
    def test_presence(self):
        before=presence('online',[discord.CustomActivity(name='At practice'),discord.Game('Minecraft')])
        after=presence('idle',[discord.CustomActivity(name='Homework'),SimpleNamespace(type=discord.ActivityType.listening,name='Spotify')])
        changes=mw.presence_changes(before,after,set(mw.KINDS))
        self.assertEqual(changes,[('status','🟢 online','🌙 idle'),('custom_status','At practice','Homework'),('activity','Playing Minecraft',None)])
        self.assertEqual(mw.presence_changes(before,after,{'custom_status'}),[('custom_status','At practice','Homework')])
        song1=presence('online',[SimpleNamespace(type=discord.ActivityType.listening,name='Spotify',title='A')])
        song2=presence('online',[SimpleNamespace(type=discord.ActivityType.listening,name='Spotify',title='B')])
        self.assertEqual(mw.presence_changes(song1,song2,set(mw.KINDS)),[])   # a new Spotify song isn't news

    def test_member(self):
        until=datetime.datetime(2026,11,14,12,0,tzinfo=datetime.timezone.utc)
        before=SimpleNamespace(nick='Sam',guild_avatar=None,roles=[role(1,'@everyone'),role(2,'Builder')],timed_out_until=None)
        after=SimpleNamespace(nick='Sammy',guild_avatar='abc',roles=[role(1,'@everyone'),role(3,'Driver')],timed_out_until=until)
        kinds=[c[0] for c in mw.member_changes(before,after,set(mw.KINDS))]
        self.assertEqual(kinds,['nickname','avatar','roles','timeout'])
        self.assertEqual(mw.member_changes(before,after,{'roles'})[0][2],'+Driver, −Builder')
        self.assertEqual(mw.member_changes(before,before,set(mw.KINDS)),[])

    def test_user(self):
        before=SimpleNamespace(name='sam',global_name='Sam',avatar=None)
        after=SimpleNamespace(name='sam_2',global_name='Samuel',avatar='xyz')
        self.assertEqual([c[0] for c in mw.user_changes(before,after,set(mw.KINDS))],['username','username','avatar'])
        self.assertEqual(mw.user_changes(before,after,{'nickname'}),[])


class SettingsTests(unittest.TestCase):
    def test_checked(self):
        s=mw.clean({'enabled':True,'recipients':'<@123>\n456, 123','watch':['roles','status','nope'],'members':'','include_bots':True})
        self.assertEqual(s,{'enabled':True,'recipients':[123,456],'watch':['status','roles'],'members':[],'include_bots':True})
        for bad in ({'enabled':True,'recipients':'','watch':['roles']},{'enabled':True,'recipients':'1','watch':[]},
                    {'recipients':'abc'},{'recipients':'\n'.join(str(i) for i in range(1,12))},'nope'):
            with self.assertRaises(ValueError,msg=bad):mw.clean(bad)
        self.assertFalse(mw.clean({'enabled':False})['enabled'])   # off needs nothing else

    def test_intents_asked_for_only_when_needed(self):
        self.assertEqual(mw.needs_intents(mw.settings_of({})),(False,False))
        self.assertEqual(mw.needs_intents(mw.settings_of({'enabled':True,'watch':['nickname']})),(True,False))
        self.assertEqual(mw.needs_intents(mw.settings_of({'enabled':True,'watch':['status']})),(False,True))
        self.assertEqual(mw.settings_of({'enabled':True})['watch'],mw.DEFAULT_WATCH)   # (status left out: noisy)
        watch=mw.MemberWatch(fake_core());bot=Bot()
        self.assertTrue(watch.apply_intents(bot));self.assertTrue(bot.intents.members and bot.intents.presences)
        self.assertTrue(watch.drop_intents(bot));self.assertFalse(bot.intents.members or bot.intents.presences)
        self.assertIn('Server Members and Presence',watch.problem);self.assertFalse(watch.drop_intents(bot))
        self.assertEqual(watch.status()[0]['key'],'member-watch')


class WatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.core=fake_core();self.watch=mw.MemberWatch(self.core,sleep=AsyncMock())

    async def changes(self):
        if self.watch.task:await self.watch.task
        user=self.core.bot.users.get(900)
        return [e.description for e in user.sent] if user else []

    async def test_changes_are_batched_and_merged(self):
        sam=member()
        await self.watch.on_member_update(SimpleNamespace(nick='Sam',guild_avatar=None,roles=[],timed_out_until=None),member(nick='Sammy',guild_avatar=None,roles=[],timed_out_until=None))
        await self.watch.on_member_update(SimpleNamespace(nick='Sammy',guild_avatar=None,roles=[],timed_out_until=None),member(nick='S.',guild_avatar=None,roles=[],timed_out_until=None))
        await self.watch.on_presence_update(presence('online'),member(status='idle',activities=[]))
        await self.watch.on_presence_update(presence('idle'),member(status='online',activities=[]))   # flickered back
        sent=await self.changes()
        self.assertEqual(len(sent),1)
        self.assertIn('Nickname: Sam → S.',sent[0]);self.assertIn('<@5>',sent[0]);self.assertNotIn('Status',sent[0])
        self.assertEqual(self.watch.sent,1)

    async def test_who_is_watched(self):
        nick=lambda **kw:(SimpleNamespace(nick='a',guild_avatar=None,roles=[],timed_out_until=None),member(nick='b',guild_avatar=None,roles=[],timed_out_until=None,**kw))
        for kw in ({'bot':True},{'i':900},{'guild':222}):   # a bot, a recipient themself, another server
            await self.watch.on_member_update(*nick(**kw))
        self.assertEqual(await self.changes(),[])
        self.core.settings['member_watch'].update(members=[7],include_bots=True)
        await self.watch.on_member_update(*nick(i=5));await self.watch.on_member_update(*nick(i=7,bot=True))
        sent=await self.changes()
        self.assertIn('<@7>',sent[0]);self.assertNotIn('<@5>',sent[0])
        self.core.settings['member_watch']['enabled']=False
        await self.watch.on_member_update(*nick(i=7));self.assertIsNone(self.watch.task if self.watch.pending else None)

    async def test_long_batches_split_and_closed_dms_are_reported(self):
        self.core.settings['member_watch']['recipients']=[900,901];self.core.bot.forbidden={901}
        for i in range(60):
            await self.watch.on_user_update(SimpleNamespace(name=f'u{i}'+'x'*40,global_name=None,avatar=None),member(i=1000+i,name=f'User{i}')
                                            .__class__(**{**vars(member(i=1000+i,name=f'User{i}')),'name':f'v{i}'+'y'*40,'global_name':None,'avatar':None}))
        sent=await self.changes()
        self.assertGreater(len(sent),1);self.assertTrue(all(len(d)<=4096 for d in sent))
        self.assertIn('901',self.watch.problem);self.assertEqual(self.watch.status()[0]['level'],'warn')


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_intents_connect_without_them(self):
        from tests.test_issue_fixes import load_core
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        core=load_core(tmp.name);self.addAsyncCleanup(core.bot.close)
        client=SimpleNamespace(starts=0,http=SimpleNamespace(close=AsyncMock()))
        async def start(token):
            client.starts+=1
            if client.starts==1:raise discord.PrivilegedIntentsRequired(None)
        client.start=start
        dropped=[]
        await core.run_discord(client,'token',sleep=AsyncMock(),refused=lambda:dropped.append(1) or True)
        self.assertEqual((client.starts,len(dropped)),(2,1))
        client.starts=0
        await core.run_discord(client,'token',sleep=AsyncMock(),refused=lambda:False)   # nothing to drop: give up as before
        self.assertEqual(client.starts,1)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient,TestServer
        from dashboard import Dashboard
        from queueing import Engine,Store
        self.tmp=tempfile.TemporaryDirectory();d=Path(self.tmp.name)
        self.core=Core(d,{'Mini':{'model':'A1 mini'}});self.core.CONFIG={'protect_management':False}
        self.store=Store(d/'db');self.engine=Engine(self.core,self.store);self.dashboard=Dashboard(self.core,self.store,self.engine)
        self.client=TestClient(TestServer(self.dashboard.app));await self.client.start_server()
        self.csrf=(await (await self.client.get('/api/state')).json())['csrf']
    async def asyncTearDown(self):
        for t in list(self.engine.tasks):t.cancel()
        await self.client.close();self.store.db.close();self.dashboard.slicer.jobs.db.close();self.tmp.cleanup()

    async def test_settings(self):
        state=await (await self.client.get('/api/memberwatch')).json()
        self.assertFalse(state['settings']['enabled']);self.assertEqual([k['key'] for k in state['kinds']],list(mw.KINDS))
        r=await self.client.post('/api/memberwatch',json={'enabled':True,'recipients':'123456789012345678','watch':['status','roles']},headers={'X-PM':'1','X-CSRF':self.csrf})
        body=await r.json()
        self.assertEqual(r.status,200);self.assertEqual(body['settings']['recipients'],['123456789012345678'])   # (IDs as text: too big for JavaScript numbers)
        self.assertEqual(body['needs'],['Server Members','Presence'])
        self.assertEqual(self.core.settings['member_watch']['watch'],['status','roles'])
        self.assertIn('Member updates on',[e['title'] for e in self.store.events()])
        r=await self.client.post('/api/memberwatch',json={'enabled':True,'recipients':'','watch':['roles']},headers={'X-PM':'1','X-CSRF':self.csrf})
        self.assertEqual(r.status,400)

    def test_the_page(self):
        page,app=(ROOT/'static/index.html').read_text(),(ROOT/'static/app.js').read_text()
        for id in ('memberWatchForm','mwEnabled','mwRecipients','mwKinds','mwMembers','mwBots','mwIntents'):self.assertIn(f'id="{id}"',page)
        self.assertIn("api('memberwatch',undefined,'GET')",app)


if __name__=='__main__':
    unittest.main()
