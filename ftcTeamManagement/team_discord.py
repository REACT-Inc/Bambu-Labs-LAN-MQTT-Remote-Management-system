"""FTC team management in Discord: every team-facing command lives under /ftcteam.

/ftcteam links · attending · notattending · attendance · remind · reminders · cancelreminder
/ftcteam note save | find | delete · /ftcteam report assign

Each subcommand (or subcommand group, like /ftcteam note) has its own permission level, set in the dashboard.
Admin and server tools (archive, laptops, server, reboot) are in discord_Intergration/server_discord.py.
"""
import secrets
import time
from datetime import date
import discord
from discord import app_commands


def install(core,team):
    bot=core.bot
    def permitted(i):
        # Administrator or approved user ID (e.g. may delete anyone's note or see absence reasons). Command access uses core.require.
        return i.guild_id in core.ALLOWED_GUILD_IDS and (i.user.id in core.SETTINGS_USER_IDS or isinstance(i.user,discord.Member) and i.user.guild_permissions.administrator)

    ftcteam=app_commands.Group(name='ftcteam',description='FTC team tools: attendance, report writers, notes, reminders and links',guild_only=True)
    note=app_commands.Group(name='note',description='Shared team notes',parent=ftcteam)
    report=app_commands.Group(name='report',description='Meeting-report writers',parent=ftcteam)

    # ---- Links ----------------------------------------------------------------------------
    @ftcteam.command(name='links',description='FTC Discord invite, team website and management dashboard')
    async def links(i:discord.Interaction):
        rows=[(label,team.config.get(key)) for label,key in [('FTC Discord invite','ftc_url'),('Team website','website_url'),('3D Printer Management dashboard','management_url')]]
        text='\n'.join(f'**{label}:** {value}' if value else f'**{label}:** not set yet' for label,value in rows)
        if not any(value for _,value in rows):text+='\n\nAn administrator can set these in the dashboard under Team tools.'
        await core.respond(i,core.card('🔗 Team links',text))

    # ---- Notes ----------------------------------------------------------------------------
    @note.command(name='save',description='Save a shared team note')
    async def note_save(i:discord.Interaction,title:str,text:str):
        note_id=team.remember(i.guild_id,i.user.id,title,text)
        await core.respond(i,core.card('📝 Remembered',f'{core.safe(title)}\nNote ID: `{note_id}`\nUse /ftcteam note find to find it.'))

    @note.command(name='find',description='Find shared team notes')
    async def note_find(i:discord.Interaction,query:str=''):
        rows=[r for r in team.notes(i.guild_id) if query.casefold() in (r['title']+' '+r['body']).casefold()]
        if not rows:await core.respond(i,core.card('Team notes','No matching notes.'));return
        for row in rows[:5]:await core.respond(i,core.card('📝 '+row['title'],row['body']+f"\n\nID: `{row['id']}`"))
        if len(rows)>5:await core.respond(i,core.card('More results','Refine your search or use the dashboard to view more notes.'))

    @note.command(name='delete',description='Delete one of your notes (admins may delete any team note)')
    async def note_delete(i:discord.Interaction,note_id:str):
        row=team.db.execute('SELECT * FROM notes WHERE id=? AND guild=?',(note_id,str(i.guild_id))).fetchone()
        if not row or (row['owner']!=str(i.user.id) and not permitted(i)):
            await i.response.send_message('Note not found or you do not own it.',ephemeral=core.ephemeral(i));return
        with team.db:team.db.execute('DELETE FROM notes WHERE id=?',(note_id,))
        await i.response.send_message('Note deleted.',ephemeral=core.ephemeral(i))

    # ---- Reminders --------------------------------------------------------------------------
    @ftcteam.command(name='remind',description='Send yourself a Discord DM reminder after a number of minutes')
    async def remind(i:discord.Interaction,minutes:int,text:str):
        rid=team.reminder(i.guild_id,i.user.id,text,minutes)
        await i.response.send_message(embed=core.card('⏰ Reminder saved',f"I will DM you <t:{int(time.time()+minutes*60)}:R>. Enable DMs from this server.\nID: `{rid}`"),ephemeral=core.ephemeral(i))

    @ftcteam.command(name='reminders',description='View your reminders and delivery status')
    async def reminders(i:discord.Interaction):
        rows=team.db.execute('SELECT * FROM reminders WHERE owner=? AND guild=? ORDER BY due DESC LIMIT 15',(str(i.user.id),str(i.guild_id)))
        text='\n'.join(f"`{r['id']}` • {r['status']} • <t:{int(r['due'])}:R> • {core.safe(r['body'][:80])}" for r in rows)
        await i.response.send_message(embed=core.card('Your reminders',text or 'No reminders.'),ephemeral=core.ephemeral(i))

    @ftcteam.command(name='cancelreminder',description='Cancel your pending reminder')
    async def cancelreminder(i:discord.Interaction,reminder_id:str):
        with team.db:cursor=team.db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND owner=? AND guild=? AND status='pending'",(reminder_id,str(i.user.id),str(i.guild_id)))
        await i.response.send_message('Cancelled.' if cursor.rowcount else 'No pending reminder with that ID belongs to you.',ephemeral=core.ephemeral(i))

    # ---- Meeting attendance ---------------------------------------------------------------------
    def meeting_label(meeting):
        day=date.fromisoformat(meeting)
        return ('Today, ' if day==team.today() else '')+day.strftime('%A %B %d').replace(' 0',' ')

    async def meeting_choices(i,current):
        return [app_commands.Choice(name=meeting_label(d.isoformat()),value=d.isoformat()) for d in team.upcoming_meetings(10)
                if current.casefold() in (d.isoformat()+' '+meeting_label(d.isoformat())).casefold()][:25]

    async def reply_attendance(i,status,meeting,reason=''):
        # /ftcteam notattending is always private (reasons can be personal); /ftcteam attending follows the channel rule.
        try:meeting=team.set_attendance(i.guild_id,i.user.id,status,meeting,reason,getattr(i.user,'display_name',str(i.user)))
        except ValueError as exc:
            await i.response.send_message(embed=core.card('Attendance not saved',str(exc),core.RED),ephemeral=core.ephemeral(i));return
        if status=='attending':text=f"✅ You're marked as **attending** the meeting on **{meeting_label(meeting)}**."
        else:text=f"❌ You're marked as **not attending** the meeting on **{meeting_label(meeting)}**."+(f"\nReason: {core.safe(reason)}" if reason else '')
        other='/ftcteam notattending' if status=='attending' else '/ftcteam attending'
        await i.response.send_message(embed=core.card('Meeting attendance',text+f'\nChanged your mind? Use `{other}` for the same date.',core.GREEN if status=='attending' else core.YELLOW),ephemeral=core.ephemeral(i))

    @ftcteam.command(name='attending',description="Say you'll be at the next meeting (or a chosen meeting date)")
    @app_commands.describe(meeting='Meeting date; leave blank for the next meeting')
    async def attending(i:discord.Interaction,meeting:str=''):
        await reply_attendance(i,'attending',meeting)
    attending.autocomplete('meeting')(meeting_choices)

    @ftcteam.command(name='notattending',description="Say you won't be at the next meeting (or a chosen meeting date)")
    @app_commands.describe(reason='Optional; only admins see it',meeting='Meeting date; leave blank for the next meeting')
    async def notattending(i:discord.Interaction,reason:app_commands.Range[str,0,300]='',meeting:str=''):
        await reply_attendance(i,'not_attending',meeting,reason)
    notattending.autocomplete('meeting')(meeting_choices)

    @ftcteam.command(name='attendance',description='Who is and isn\'t coming to the next meeting (or a chosen date)')
    @app_commands.describe(meeting='Meeting date; leave blank for the next meeting')
    async def attendance(i:discord.Interaction,meeting:str=''):
        try:meeting=team.meeting_date(meeting)
        except ValueError as exc:
            await i.response.send_message(embed=core.card('Attendance',str(exc),core.RED),ephemeral=True);return
        info=team.attendance(i.guild_id,[meeting])[0];admin_view=permitted(i)
        def who(r):
            reason=f" — {core.safe(r['reason'])}" if admin_view and r['reason'] else ''
            return f"<@{r['member']}>{reason}"
        coming=[who(r) for r in info['replies'] if r['status']=='attending']
        away=[who(r) for r in info['replies'] if r['status']=='not_attending']
        lines=[f"**Attending ({len(coming)})**",'\n'.join(coming) or '—','',f"**Not attending ({len(away)})**",'\n'.join(away) or '—']
        if info['no_reply']:lines+=['',f"**No reply yet ({len(info['no_reply'])})**",' '.join(f'<@{m}>' for m in info['no_reply'])]
        await i.response.send_message(embed=core.card(f'🗓️ Meeting attendance · {meeting_label(meeting)}','\n'.join(lines)[:4000]),
            ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    attendance.autocomplete('meeting')(meeting_choices)

    # ---- Report writers ----------------------------------------------------------------------
    @report.command(name='assign',description='Assign a meeting-report writer (admins/approved IDs; private reply)')
    @app_commands.describe(member='Choose a person, or omit for a random roster selection',announce='Post in the practice channel; your command reply stays private')
    async def report_assign(i:discord.Interaction,member:discord.Member=None,announce:bool=False):
        if not await core.require(i,'ftcteam report'):return
        await i.response.defer(ephemeral=True)
        try:
            result=await team.assign('manual:'+secrets.token_hex(8),channel_id=None if announce else i.channel_id,member_id=member.id if member else None,announce=announce,guild_id=i.guild_id)
            detail='An announcement was requested in the practice channel.' if announce else 'Saved privately. No channel announcement or DM was sent.'
            await i.followup.send(f"Assignment `{result['id']}`: <@{result['member']}> • {result['status']}\n{detail}",ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            core.log.warning('Manual report assignment failed (%s)',type(exc).__name__)
            await i.followup.send('Could not assign report: '+str(exc)[:1500],ephemeral=True)

    bot.tree.add_command(ftcteam)
