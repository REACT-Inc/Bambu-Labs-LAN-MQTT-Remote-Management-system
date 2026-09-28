import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from aiohttp import web
from queueing import Store
from team import Team

class TeamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db')
        self.core=SimpleNamespace(DATA_DIR=Path(self.tmp.name),CONFIG={},ALLOWED_GUILD_IDS={123})
        self.team=Team(self.core,self.store,SimpleNamespace(app=web.Application()))
    async def asyncTearDown(self):self.store.db.close();self.tmp.cleanup()
    def test_notes_are_server_scoped(self):
        self.team.remember(123,1,'Hello','World');self.team.remember(456,2,'Other','Server')
        self.assertEqual(len(self.team.notes(123)),1)
    def test_reminder_persistence_and_bounds(self):
        rid=self.team.reminder(123,1,'Test',60)
        row=self.store.db.execute('SELECT * FROM reminders WHERE id=?',(rid,)).fetchone()
        self.assertGreater(row['due'],time.time()+3500)
        with self.assertRaises(ValueError):self.team.reminder(123,1,'Test',0)
    def test_schedule_validation(self):
        with self.assertRaises(ValueError):self.team.save_config({'practice_enabled':True})
        self.team.save_config({'practice_enabled':True,'practice_channel_id':'1234','roster':['123456789012345678'],'practice_days':[0,2],'practice_time':'16:30'})
        self.assertEqual(json.loads(self.team.config_file.read_text())['roster'],['123456789012345678'])
    def test_url_scheme_restriction(self):
        with self.assertRaises(ValueError):self.team.save_config({'website_url':'javascript:alert(1)'})
    async def test_meshcentral_unconfigured_is_empty(self):
        self.assertEqual(await self.team.devices(),[])
        with self.assertRaises(ValueError):await self.team.device_command('node//x','hostname')
    def test_custom_agent_routes_removed(self):
        paths=[r.resource.canonical for r in self.team.dashboard.app.router.routes()]
        self.assertNotIn('/agent/heartbeat',paths);self.assertNotIn('/agent/result',paths)
        self.assertFalse(hasattr(self.team,'enroll'))
    def test_unknown_deliveries_not_retried_on_restart(self):
        self.store.db.execute("INSERT INTO reminders(id,guild,owner,body,due,status) VALUES('r','123','1','Test',0,'sending')")
        self.store.db.commit()
        Team(self.core,self.store,SimpleNamespace(app=web.Application()))
        self.assertEqual(self.store.db.execute("SELECT status FROM reminders WHERE id='r'").fetchone()[0],'needs_review')
    async def test_reboot_disabled_by_default(self):
        with self.assertRaises(ValueError):await self.team.reboot(True)

class AssignmentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=TeamTests.asyncSetUp
    asyncTearDown=TeamTests.asyncTearDown
    async def test_assignments_rotate_and_occurrence_is_idempotent(self):
        import discord
        class Guild:
            id=123
            async def fetch_member(self,uid):return SimpleNamespace(id=uid)
        sent=[]
        class Channel:
            id=456;guild=Guild()
            async def send(self,**kw):sent.append(kw)
        async def channel(cid):return Channel()
        self.team.channel=channel;self.team.config.update(roster=['1','2','3'],practice_channel_id='456')
        self.core.card=lambda *a:a;self.core.BLUE=1
        chosen=[]
        for i in range(3):chosen.append((await self.team.assign('practice-'+str(i)))['member'])
        self.assertEqual(len(set(chosen)),3)
        await self.team.assign('practice-0')
        self.assertEqual(len(sent),3)

    async def test_archive_roundtrip_restores_permissions(self):
        from unittest.mock import MagicMock
        import discord
        role=MagicMock(spec=discord.Role);role.id=1
        category=SimpleNamespace(id=90)
        class Guild:
            id=123;default_role=role;me=MagicMock(spec=discord.Member);categories=[]
            async def create_category(self,*a,**kw):return category
            def get_role(self,rid):return role if rid==1 else None
            def get_channel(self,cid):return SimpleNamespace(id=cid)
        class Channel:
            id=789;guild=Guild();category_id=80
            overwrites={role:discord.PermissionOverwrite(send_messages=True,view_channel=True)}
            def permissions_for(self,member):return SimpleNamespace(view_channel=True,manage_channels=True,manage_roles=True)
            async def edit(self,**kw):self.overwrites=kw['overwrites'];self.category_id=kw['category'].id
        ch=Channel()
        async def channel(cid):return ch
        self.team.channel=channel;self.core.settings={}
        original_edit=ch.edit
        async def forbidden(**kw):raise discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),{'code':50013,'message':'Missing Permissions'})
        ch.edit=forbidden
        with self.assertRaisesRegex(ValueError,'Manage Roles'):await self.team.archive(789)
        self.assertEqual(self.team.db.execute('SELECT status FROM archives WHERE channel=?',('789',)).fetchone()[0],'pending')
        ch.edit=original_edit
        await self.team.archive(789)
        self.assertIs(ch.overwrites[role].send_messages,False)
        self.assertEqual(ch.category_id,90)
        await self.team.archive(789,True)
        self.assertIs(ch.overwrites[role].send_messages,True)
        self.assertEqual(ch.category_id,80)

    async def test_archive_preflight_no_mutation(self):
        from unittest.mock import AsyncMock
        ch=SimpleNamespace(id=700,guild=SimpleNamespace(me=object()),permissions_for=lambda me:SimpleNamespace(view_channel=True,manage_channels=True,manage_roles=False),edit=AsyncMock())
        self.team.channel=AsyncMock(return_value=ch)
        with self.assertRaisesRegex(ValueError,'Manage Roles'):await self.team.archive(700)
        ch.edit.assert_not_awaited()
        self.assertIsNone(self.team.db.execute('SELECT * FROM archives WHERE channel=?',('700',)).fetchone())

    async def test_specific_private_assignment_without_roster(self):
        from unittest.mock import AsyncMock
        guild=SimpleNamespace(id=123,fetch_member=AsyncMock(return_value=SimpleNamespace(id=88,bot=False)))
        ch=SimpleNamespace(id=456,guild=guild,send=AsyncMock())
        self.team.channel=AsyncMock(return_value=ch)
        self.team.config['roster']=[]
        result=await self.team.assign('private-1',channel_id=456,member_id=88,announce=False,guild_id=123)
        self.assertEqual(result['member'],'88')
        self.assertEqual(result['status'],'assigned_private')
        ch.send.assert_not_awaited()
        with self.assertRaises(ValueError):await self.team.assign('wrong',channel_id=456,member_id=88,announce=False,guild_id=999)


class AttendanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=TeamTests.asyncSetUp
    asyncTearDown=TeamTests.asyncTearDown
    def meetings_on(self,days,today):
        from datetime import date
        self.team.config.update(practice_days=days,roster=['1','2','3'])
        self.team.today=lambda:date.fromisoformat(today)

    def test_next_meeting_is_today_on_a_meeting_day_else_next_one(self):
        self.meetings_on([0,2,4],'2026-09-28')  # Monday
        self.assertEqual(self.team.meeting_date(''),'2026-09-28')
        self.meetings_on([2,4],'2026-09-28')
        self.assertEqual(self.team.meeting_date('next'),'2026-09-30')
        self.assertEqual([d.isoformat() for d in self.team.upcoming_meetings(3)],['2026-09-30','2026-10-02','2026-10-07'])

    def test_date_validation(self):
        self.team.config['practice_days']=[]
        with self.assertRaisesRegex(ValueError,'No meeting days'):self.team.meeting_date('')
        self.meetings_on([0,2,4],'2026-09-28')
        self.assertEqual(self.team.meeting_date('2026-10-02'),'2026-10-02')
        for bad,message in [('2026-09-29','no meeting'),('2026-09-25','already happened'),('2027-06-07','120 days'),('Friday','date like')]:
            with self.assertRaisesRegex(ValueError,message):self.team.meeting_date(bad)

    def test_replies_replace_each_other_and_list_no_replies(self):
        self.meetings_on([0,2,4],'2026-09-28')
        self.team.set_attendance(123,'1','not_attending',reason='Dentist',name='Sam')
        self.team.set_attendance(123,'1','attending',source='Dashboard')
        self.team.set_attendance(123,'2','not_attending','2026-09-30')
        today,wednesday=self.team.attendance(123,['2026-09-28','2026-09-30'])
        (reply,)=today['replies']
        self.assertEqual((reply['status'],reply['reason'],reply['name']),('attending','','Sam'))
        self.assertEqual(today['no_reply'],['2','3'])
        self.assertEqual(self.team.absent(123,'2026-09-30'),{'2'})
        self.assertEqual(self.team.attendance(456,['2026-09-28'])[0]['replies'],[])
        with self.assertRaisesRegex(ValueError,'Discord user ID'):self.team.set_attendance(123,'abc','attending')
        with self.assertRaisesRegex(ValueError,'300'):self.team.set_attendance(123,'3','not_attending',reason='x'*301)
        self.team.clear_attendance(123,'1','2026-09-28')
        self.assertEqual(self.team.attendance(123,['2026-09-28'])[0]['no_reply'],['1','2','3'])

    async def test_report_writer_skips_people_not_attending(self):
        from unittest.mock import AsyncMock
        self.meetings_on([0,2,4],'2026-09-28')
        guild=SimpleNamespace(id=123,fetch_member=AsyncMock(side_effect=lambda uid:SimpleNamespace(id=uid,bot=False)))
        self.team.channel=AsyncMock(return_value=SimpleNamespace(id=456,guild=guild,send=AsyncMock()))
        for member in ('1','2'):self.team.set_attendance(123,member,'not_attending')
        for n in range(3):
            self.assertEqual((await self.team.assign(f'm{n}',channel_id=456,announce=False))['member'],'3')
        self.team.set_attendance(123,'3','not_attending')
        self.assertIn((await self.team.assign('all-away',channel_id=456,announce=False))['member'],{'1','2','3'})
