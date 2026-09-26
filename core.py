# Existing Discord/printer features, now a component of 3D Printer Management.
import os
import json
from pathlib import Path
CONFIG_PATH = Path(os.environ.get('PM_CONFIG', '/etc/3d-printer-management/config.json'))
CONFIG = json.loads(CONFIG_PATH.read_text())
DATA_DIR = Path(os.environ.get('PM_DATA', '/var/lib/3d-printer-management'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DISCORD_BOT_TOKEN = CONFIG.get('discord_token', '')
ALLOWED_GUILD_IDS = set(CONFIG.get('guild_ids', []))
SETTINGS_USER_IDS = set(CONFIG.get('admin_user_ids', []))
SETTINGS_FILE = str(DATA_DIR / 'settings.json')
PRINTERS = CONFIG.get('printers', [])
EXAMPLE_DATA = CONFIG.get('example_data') or {
    p['name']: {'state':'IDLE','error':0,'connected':True,'mc_percent':0,
                'ams':[], 'vt_tray':{'tray_type':'PLA','tray_color':'FFFFFFFF','remain':100}}
    for p in (PRINTERS or [{'name':'Demo H2D'}, {'name':'Demo A1'}])
}
import asyncio
import base64
import copy
import difflib
import io
import json
import logging
import os
import re
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('printer-bot')
STARTED = time.monotonic()
EXAMPLE_MODE = CONFIG.get("demo", False) or not PRINTERS
BLUE, GREEN, YELLOW, RED, GRAY = 0x3498DB, 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x95A5A6
STATE_COLORS = {'RUNNING': BLUE, 'FINISH': GREEN, 'IDLE': GREEN,
                'PAUSE': YELLOW, 'PREPARE': YELLOW, 'FAILED': RED}
data_lock = threading.Lock()
reports, clients, last_seen = {}, {}, {}
camera_tasks = {}
bot_loop = None
from progress_notifications import ProgressTracker
progress_tracker = ProgressTracker()
report_listener = None
event_listener = None


def load_settings():
    try:
        result = json.loads(Path(SETTINGS_FILE).read_text())
        if not isinstance(result, dict):
            raise ValueError('Settings must be an object')
        return result
    except FileNotFoundError:
        return {}


settings = load_settings()
public_channels = {}
from printer_errors import describe as describe_error, errors as decode_errors

def printer_error_text(name, error, data=None):
    printer = printer_config(name) or {}
    model = printer.get("error_model") or str(printer.get("serial", ""))[:3]
    return describe_error(error, data, model)



def store_channel(key, interaction):
    # Settings remain compatible with the original single-server configuration.
    save_settings({**settings, key: interaction.channel_id})


def save_settings(updated):
    parent = Path(SETTINGS_FILE).parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.settings-', dir=parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(updated, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, SETTINGS_FILE)
        settings.clear()
        settings.update(updated)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ephemeral(interaction):
    # Public in the channel where invoked, except the three private workflows.
    command=getattr(interaction,'command',None)
    name=getattr(command,'qualified_name','')
    if not name:
        data=getattr(interaction,'data',None) or {}
        name=data.get('name','')
    if name in ('dm','publiccommands','assign report','meeting report assign'):
        return True
    message=getattr(interaction,'message',None)
    return bool(getattr(getattr(message,'flags',None),'ephemeral',False))


def card(title, description='', color=BLUE):
    embed = discord.Embed(title=str(title)[:256], description=str(description)[:4096],
                          color=color, timestamp=discord.utils.utcnow())
    embed.set_footer(text='3D Printer Management • DEMO DATA' if EXAMPLE_MODE else '3D Printer Management • Live mode')
    return embed


def field(embed, name, value, inline=True):
    embed.add_field(name=name, value=str(value)[:1024] or '—', inline=inline)


def display_name(name):
    return settings.get('printer_names', {}).get(name, name)


def rename_printer(name, new_name):
    if name not in names():raise ValueError('Unknown printer.')
    new_name = new_name.strip()
    if not 1 <= len(new_name) <= 80 or any(ord(c) < 32 for c in new_name):
        raise ValueError('Choose a name of 1–80 characters without line breaks.')
    if any(n != name and new_name.casefold() in (n.casefold(), display_name(n).casefold()) for n in names()):
        raise ValueError('Another printer already uses that name.')
    aliases = dict(settings.get('printer_names', {}))
    if new_name == name:aliases.pop(name, None)
    else:aliases[name] = new_name
    save_settings({**settings, 'printer_names': aliases})
    return new_name


def safe(value):
    return discord.utils.escape_markdown(str(display_name(value) if isinstance(value,str) else value))


async def respond(interaction, embed, view=None):
    kwargs = dict(embed=embed, ephemeral=ephemeral(interaction))
    if view is not None:
        kwargs['view'] = view
    if interaction.response.is_done():
        message = await interaction.followup.send(**kwargs, wait=True)
    else:
        await interaction.response.send_message(**kwargs)
        message = await interaction.original_response()
    if view is not None:
        view.message = message


def names():
    return list(EXAMPLE_DATA) if EXAMPLE_MODE else [p['name'] for p in PRINTERS]


def printer_config(name):
    return next((p for p in PRINTERS if p['name'] == name), None)


def state_data(name):
    if EXAMPLE_MODE:
        data = copy.deepcopy(EXAMPLE_DATA[name])
        return data.get('state', 'IDLE'), data.get('error', 0), data, data.get('connected', True)
    with data_lock:
        data = copy.deepcopy(reports.get(name, {}))
    client = clients.get(name)
    return data.get('gcode_state', 'UNKNOWN'), data.get('print_error', 0), data, bool(client and client.is_connected())


def normalized(text):
    return re.sub(r'[^\w]+', ' ', text.casefold()).strip()


def resolve_name(query):
    available = names()
    query = (query or '').strip()
    exact = [n for n in available if query.casefold() in (n.casefold(), display_name(n).casefold())]
    if len(exact) == 1:
        return exact[0], None
    q = normalized(query)
    if not q:
        return None, None
    partial = [n for n in available if q in normalized(n) or q in normalized(display_name(n))]
    if len(partial) == 1:
        return None, partial[0]
    if partial:
        return None, None
    ranked = sorted(((max(difflib.SequenceMatcher(None,q,normalized(label)).ratio() for label in (n,n.split('(')[0],display_name(n))),n) for n in available),reverse=True)
    if ranked and ranked[0][0] >= 0.60 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.12):
        return None, ranked[0][1]
    return None, None


ADMIN_COMMANDS = {
    'adminhelp','temperature','chamber','speed','fan','fanall','move','plateswap','rename','dm',
    'laptops','laptop','server','reboot','setnotificationchannel','setcommandschannel',
    'publiccommands','archive','unarchive','assign','meeting','queuestart','queueforce',
    'queuemanage','reprint','pause','resume','stop','lighton','lightoff',
}


def admin_allowed(interaction):
    return interaction.guild_id in ALLOWED_GUILD_IDS and (
        interaction.user.id in SETTINGS_USER_IDS or
        isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)


update_pending = lambda: False


class PrinterTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if update_pending():
            await interaction.response.send_message('Management update in progress. Try again when it finishes.', ephemeral=ephemeral(interaction))
            return False
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message(
                embed=card('Unavailable', 'Use this bot in an authorized server.', RED), ephemeral=ephemeral(interaction))
            return False
        command = interaction.command
        if command and command.qualified_name.split()[0] in ADMIN_COMMANDS and not admin_allowed(interaction):
            await interaction.response.send_message('Administrators and approved user IDs only.', ephemeral=ephemeral(interaction))
            return False
        return True


class PrinterBot(commands.Bot):
    async def setup_hook(self):
        await self.tree.sync()


bot = PrinterBot(command_prefix='!', intents=discord.Intents.default(),
                 tree_cls=PrinterTree, help_command=None,
                 allowed_mentions=discord.AllowedMentions.none())


class OwnedView(discord.ui.View):
    def __init__(self, owner):
        super().__init__(timeout=60)
        self.owner, self.message, self.used = owner, None, False
        self.deadline = time.monotonic() + 60

    async def interaction_check(self, interaction):
        if update_pending():
            await interaction.response.send_message('Management update in progress. Try again when it finishes.', ephemeral=ephemeral(interaction))
            return False
        if interaction.user.id != self.owner:
            await interaction.response.send_message('Only the person who ran the command can use these buttons.', ephemeral=ephemeral(interaction))
            return False
        if interaction.guild_id not in ALLOWED_GUILD_IDS or self.used or time.monotonic() >= self.deadline:
            await interaction.response.send_message('This selection has expired. Run the command again.', ephemeral=ephemeral(interaction))
            return False
        return True

    def button(self, label, callback, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(label=label[:80], style=style)
        button.callback = callback
        self.add_item(button)

    async def finish(self, interaction, embed):
        if self.used:
            await interaction.response.send_message('This selection was already used.', ephemeral=ephemeral(interaction))
            return False
        self.used = True
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)
        return True

    async def cancel(self, interaction):
        await self.finish(interaction, card('Cancelled', 'No action was taken.', GRAY))

    async def on_timeout(self):
        self.used = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(embed=card('Selection expired', 'Run the command again to continue.', GRAY), view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        log.error('Button failed', exc_info=(type(error), error, error.__traceback__))
        await private_error(interaction)


class PrinterPicker(OwnedView):
    def __init__(self, owner, action, suggestion=None):
        super().__init__(owner)
        self.action, self.suggestion, self.page = action, suggestion, 0
        self.build()

    def build(self):
        self.clear_items()
        if self.suggestion:
            self.button('Use ' + display_name(self.suggestion), self.choose(self.suggestion), discord.ButtonStyle.primary)
            self.button('Choose another printer', self.show_all)
        else:
            for name in names()[self.page * 20:(self.page + 1) * 20]:
                self.button(display_name(name), self.choose(name), discord.ButtonStyle.primary)
            if self.page:
                self.button('Previous', self.previous)
            if (self.page + 1) * 20 < len(names()):
                self.button('Next', self.next_page)
        self.button('Cancel', self.cancel)

    def choose(self, name):
        async def callback(interaction):
            if await self.finish(interaction, card('Printer selected', safe(name), GREEN)):
                await run_action(interaction, self.action, name)
        return callback

    async def show_all(self, interaction):
        self.suggestion = None
        self.build()
        await interaction.response.edit_message(embed=card('Choose a printer', 'Select the printer you want. • Expires after 60 seconds'), view=self)

    async def previous(self, interaction):
        self.page -= 1
        self.build()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction):
        self.page += 1
        self.build()
        await interaction.response.edit_message(view=self)


async def choose_printer(interaction, action, name):
    exact, suggestion = resolve_name(name)
    if exact:
        await run_action(interaction, action, exact)
        return
    embed = card('Did you mean this printer?' if suggestion else 'Choose a printer',
                 f'Use **{safe(suggestion)}**?\nConfirm below, or choose another printer.' if suggestion
                 else 'Select the printer you want below.\nOnly you can use these buttons. • Expires after 60 seconds')
    await respond(interaction, embed, PrinterPicker(interaction.user.id, action, suggestion))


def snapshot_bytes(printer):
    if not printer:
        return None
    try:
        if printer.get('camera_type') == 'rtsp':
            url = f"rtsps://bblp:{printer['access_code']}@{printer['ip']}:322/streaming/live/1"
            result = subprocess.run(['ffmpeg', '-loglevel', 'error', '-rtsp_transport', 'tcp',
                                     '-i', url, '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
                                    capture_output=True, timeout=15)
            return result.stdout if result.returncode == 0 and result.stdout else None
        if printer.get('camera_type') == 'jpeg_tcp':
            from camera_capture import capture_jpeg
            return capture_jpeg(printer)
    except Exception as error:
        # Avoid logging camera URLs containing printer access codes.
        log.warning('Camera unavailable for %s (%s)', printer['name'], type(error).__name__)
    return None


async def snapshot(name):
    if globals().get("live_cameras"):
        return await live_cameras.snapshot(name)
    if EXAMPLE_MODE or not state_data(name)[3]:
        return None
    task = camera_tasks.get(name)
    if task is None or task.done():
        task = asyncio.create_task(asyncio.to_thread(snapshot_bytes, printer_config(name)))
        camera_tasks[name] = task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=25)
    except asyncio.TimeoutError:
        # Concurrent callers share this capture; cancellation never spawns duplicates.
        log.warning('Camera capture timed out for %s', name)
        return None


def progress(value):
    try:
        percent = max(0, min(100, float(value)))
    except (TypeError, ValueError):
        return 'Unknown'
    filled = round(percent / 10)
    return '🟦' * filled + '⬜' * (10 - filled) + f' **{percent:g}%**'


def printer_embed(name):
    state, error, data, connected = state_data(name)
    embed = card('🖨️ ' + display_name(name), f"{'🟢 Connected' if connected else '🔴 Offline / reconnecting'} • **{safe(state)}**",
                 RED if error or not connected else STATE_COLORS.get(state, GRAY))
    field(embed, 'File', safe(data.get('subtask_name', 'No file reported')), False)
    field(embed, 'Progress', progress(data.get('mc_percent')), False)
    field(embed, 'Time remaining', str(data.get('mc_remaining_time', '?')) + ' min')
    field(embed, 'Layer', f"{data.get('layer_num', '?')} / {data.get('total_layer_num', '?')}")
    for label, current, target in [('Nozzle', 'nozzle_temper', 'nozzle_target_temper'), ('Bed', 'bed_temper', 'bed_target_temper')]:
        field(embed, label, f"{data.get(current, '?')}°C → {data.get(target, '?')}°C")
    if error or data.get('hms'):
        field(embed, '⚠️ Printer error', printer_error_text(name, error, data), False)
    if not EXAMPLE_MODE:
        seen = last_seen.get(name)
        field(embed, 'Last telemetry', f'<t:{int(seen)}:R>' if seen else 'No telemetry received', False)
    return embed


def tray_text(tray):
    if not tray or not tray.get('tray_type'):
        return 'Empty / not reported'
    try:
        remain = float(tray.get('remain', -1))
        remaining = f'{remain:g}%' if remain >= 0 else 'unknown'
    except (TypeError, ValueError):
        remaining = 'unknown'
    return f"{safe(tray['tray_type'])} • #{safe(tray.get('tray_color', '?'))} • {remaining} remaining"


def filament_embed(name):
    _, _, data, connected = state_data(name)
    embed = card('🧵 ' + display_name(name) + ' • Filaments', '' if connected else '🔴 Offline • Last reported data', BLUE if connected else RED)
    ams = data.get('ams', {})
    units = ams if isinstance(ams, list) else ams.get('ams', [])
    for unit in units[:24]:
        text = f"Humidity: {unit.get('humidity', '?')} • Temperature: {unit.get('temp', '?')}°C\n"
        text += '\n'.join(f"**Slot {tray.get('id', '?')}** — {tray_text(tray)}" for tray in unit.get('tray', []))
        field(embed, 'AMS ' + str(unit.get('id', '?')), text, False)
    external = data.get('vt_tray')
    if external:
        field(embed, 'External spool', tray_text(external), False)
    if not embed.fields:
        embed.description = 'No filament data has been reported yet.'
    return embed


def publish_action(name, action, filename=None):
    if EXAMPLE_MODE:
        return
    client = clients.get(name)
    if not client or not client.is_connected():
        raise RuntimeError('Printer is offline. No command was sent.')
    data = {'sequence_id': str(time.time_ns() % 1000000000), 'command': action, 'param': ''}
    if action == 'reprint':
        data.update(command='project_file', param='Metadata/plate_1.gcode',
                    project_id='0', profile_id='0', task_id='0', subtask_id='0',
                    subtask_name=filename, file='', url=f'ftp:///{filename}', md5='',
                    timelapse=False, bed_type='auto', bed_levelling=True, flow_cali=True,
                    vibration_cali=True, layer_inspect=True, ams_mapping='', use_ams=False)
    result = client.publish(f"device/{printer_config(name)['serial']}/request", json.dumps({'print': data}), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError('MQTT could not queue the command. Check the printer connection.')


def publish_light(name, on):
    if name not in names() or type(on) is not bool:
        raise ValueError('Choose a known printer and on/off state.')
    if EXAMPLE_MODE:
        EXAMPLE_DATA[name]['lights_report'] = [{'node': 'chamber_light', 'mode': 'on' if on else 'off'}]
        return
    client = clients.get(name)
    if not client or not client.is_connected():
        raise RuntimeError('Printer is offline. No light command was sent.')
    payload = {'system': dict(sequence_id=str(time.time_ns() % 1000000000),
        command='ledctrl', led_node='chamber_light', led_mode='on' if on else 'off',
        led_on_time=500, led_off_time=500, loop_times=0, interval_time=0)}
    result = client.publish(f"device/{printer_config(name)['serial']}/request", json.dumps(payload), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError('MQTT could not queue the light command.')


async def send_action(interaction, name, action, filename=None):
    try:
        publish_action(name, action, filename)
        description = f'**{safe(name)}**\n'
        description += 'Demo only — no printer was controlled.' if EXAMPLE_MODE else 'Command queued. Check printer status to confirm the result.'
        if filename:
            description += '\nFile: ' + safe(filename)
        embed = card(('🧪 Demo: ' if EXAMPLE_MODE else '📡 ') + action.title(), description, GREEN)
    except RuntimeError as error:
        embed = card('Command not sent', str(error), RED)
    await respond(interaction, embed)


class ActionConfirm(OwnedView):
    def __init__(self, owner, name, action, filename=None):
        super().__init__(owner)
        self.name, self.action, self.filename = name, action, filename
        self.button('Stop print' if action == 'stop' else 'Reprint', self.confirm, discord.ButtonStyle.danger)
        self.button('Cancel', self.cancel)

    async def confirm(self, interaction):
        if await self.finish(interaction, card('Confirmed', f'{self.action.title()} • {safe(self.name)}', YELLOW)):
            await send_action(interaction, self.name, self.action, self.filename)


async def run_action(interaction, action, name):
    if action == 'printer':
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral(interaction))
        embed = printer_embed(name)
        picture = await snapshot(name)
        kwargs = dict(embed=embed, ephemeral=ephemeral(interaction))
        if picture:
            embed.set_image(url='attachment://printer.jpg')
            kwargs['file'] = discord.File(io.BytesIO(picture), filename='printer.jpg')
        else:
            field(embed, 'Camera', 'Unavailable' if not EXAMPLE_MODE else 'No camera in demo mode', False)
        await interaction.followup.send(**kwargs)
    elif action == 'filaments':
        await respond(interaction, filament_embed(name))
    elif action in ('stop', 'reprint'):
        filename = state_data(name)[2].get('subtask_name') if action == 'reprint' else None
        if action == 'reprint' and not filename:
            await respond(interaction, card('No file available', 'No previous filename has been reported.', YELLOW))
            return
        description = f'Cancel the current print on **{safe(name)}**?' if action == 'stop' else (
            f'Reprint **{safe(filename)}** on **{safe(name)}**?\n'
            'Clear the build plate before confirming.\n'
            'Experimental: the reported job name may not be a printable file on storage. '
            'Uses plate 1 and external spool; requires a compatible printer configuration.')
        await respond(interaction, card('⚠️ Confirm ' + action, description, RED),
                      ActionConfirm(interaction.user.id, name, action, filename))
    else:
        await send_action(interaction, name, action)


async def settings_allowed(interaction):
    permitted = interaction.guild_id in ALLOWED_GUILD_IDS and (
        interaction.user.id in SETTINGS_USER_IDS or
        isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)
    if not permitted:
        await interaction.response.send_message(embed=card('Settings restricted', 'Only server administrators and approved users can change settings.', RED), ephemeral=ephemeral(interaction))
    return permitted


@bot.tree.command(name='setnotificationchannel', description='Set notifications here (admins or approved users)')
@app_commands.guild_only()
async def set_notification_channel(interaction: discord.Interaction):
    if await settings_allowed(interaction):
        store_channel('notification_channel_id', interaction)
        await respond(interaction, card('🔔 Notifications configured', 'Automatic printer updates will appear in this channel.', GREEN))


@bot.tree.command(name='setcommandschannel', description='Set the main commands channel (admins or approved users)')
@app_commands.guild_only()
async def set_commands_channel(interaction: discord.Interaction):
    if await settings_allowed(interaction):
        store_channel('commands_channel_id', interaction)
        await respond(interaction, card('💬 Commands channel configured', 'Main commands channel saved. Replies are public in every server channel except publiccommands, report assignment and dm.', GREEN))


@bot.tree.command(name='status', description='Bot uptime and all printer connections')
@app_commands.guild_only()
async def status_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=ephemeral(interaction))
    uptime = int(time.monotonic() - STARTED)
    available = names()
    online = sum(state_data(n)[3] for n in available)
    lines = [f'**Uptime:** {uptime // 3600}h {(uptime % 3600) // 60}m', f'**Connected:** {online}/{len(available)}', '']
    for name in available:
        state, error, data, connected = state_data(name)
        line = f"{'🟢' if connected else '🔴'} **{safe(name)}** — {safe(state)}"
        if state == 'RUNNING':
            line += f" • {data.get('mc_percent', '?')}%"
        if error:
            line += ' • ⚠️ ' + safe(printer_error_text(name, error).splitlines()[0][:250])
        lines.append(line)
    # Split long inventories into multiple messages.
    text = ''
    for line in lines:
        if len(text) + len(line) > 3800:
            await respond(interaction, card('🖨️ Printer fleet', text, GREEN if online == len(available) else YELLOW))
            text = ''
        text += line + '\n'
    await respond(interaction, card('🖨️ Printer fleet', text, GREEN if online == len(available) else YELLOW))


def register_printer_command(action, description):
    async def callback(interaction: discord.Interaction, name: str = None):
        await choose_printer(interaction, action, name)
    callback.__name__ = action + '_command'
    callback = app_commands.describe(name='Full name, partial name, typo, or leave blank to choose')(callback)
    callback = app_commands.guild_only()(callback)
    bot.tree.command(name=action, description=description)(callback)


for action, description in [('printer', 'Printer status, temperatures and camera'),
                            ('filaments', 'AMS slots and external spool'),
                            ('pause', 'Pause a print'), ('resume', 'Resume a print'),
                            ('stop', 'Cancel a print with confirmation'),
                            ('reprint', 'Experimental reprint with confirmation')]:
    register_printer_command(action, description)


def help_embed(admin=False):
    # Read the final command tree at invocation time. Feature installers never
    # wrap this callback or send their own extra help messages.
    groups = [
        ('📊 Printers & files', {'status','printer','filaments','file','help','adminhelp'}, 'Printer status, camera snapshots, filament and stored files.'),
        ('🎛️ Printer controls', {'pause','resume','stop','lighton','lightoff','temperature','chamber','speed','fan','fanall','move'}, 'Pause/resume/cancel, lights, temperatures, speed, fan and axis jogging.'),
        ('📋 Print queues', {'queueadd','queue','queuestart','queueforce','queuemanage','reprint'}, 'Manage jobs and confirm starts.' if admin else 'View the queue and add files for an administrator to start.'),
        ('🔄 Swapmod', {'plateswap'}, 'Configure equipped printers, approve Swaplist batches and check the starting setup.'),
        ('⚙️ Administration', {'setnotificationchannel','setcommandschannel','publiccommands','rename','dm','archive','unarchive'}, 'Channel settings, temporary public replies, printer names, DMs and archives.'),
        ('🗓️ Team & reminders', {'ftc','website','management','rememberthis','remember','forget','remindme','reminders','cancelreminder','meeting','assign'}, 'Assign meeting-report writers.' if admin else 'Team links, shared notes and personal reminders.'),
        ('💻 Laptops & server', {'laptops','laptop','server','reboot'}, 'MeshCentral laptops and commands, Pi status and reboot.'),
    ]
    commands = []
    def collect(command, root=None):
        root = root or command.name
        if isinstance(command, app_commands.Group):
            for child in command.commands:
                collect(child, root)
        else:
            if (root in ADMIN_COMMANDS) == admin:
                commands.append((root, command.qualified_name))
    for command in bot.tree.get_commands():
        collect(command)
    embed = card('🔐 Administration commands' if admin else '📖 3D Printer Management', 'Choose a command below. Discord shows its description and available options when you select it.')
    used = set()
    for title, roots, description in groups + [('More commands', {r for r, _ in commands} - set().union(*(g[1] for g in groups)), 'Additional installed tools.')]:
        entries = ['`/' + name + '`' for root, name in commands if root in roots and name not in used]
        used.update(name for root, name in commands if root in roots)
        if entries:
            field(embed, title, description + '\n' + ' · '.join(sorted(entries)), False)
    field(embed, 'Selection, permissions & privacy',
          'Use printer-name autocomplete or the selection buttons when available. Admin commands require a server administrator or an approved user ID. '
          'Replies are public in the current server channel, except publiccommands, report assignment and dm.', False)
    return embed


@bot.tree.command(name='help', description='Everyday printer and team commands')
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    await respond(interaction, help_embed())


@bot.tree.command(name='adminhelp', description='Command guide for administrators and approved IDs')
@app_commands.guild_only()
async def admin_help_command(interaction: discord.Interaction):
    if not admin_allowed(interaction):
        await interaction.response.send_message('Administrators and approved user IDs only.', ephemeral=ephemeral(interaction))
        return
    await interaction.response.send_message(embed=help_embed(admin=True), ephemeral=ephemeral(interaction))


async def private_error(interaction):
    embed = card('Something went wrong', 'The error was logged. Check the bot service log for details.', RED)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral(interaction))
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral(interaction))
    except discord.HTTPException as error:
        log.warning('Could not deliver error response (%s); original command error is logged above.', error.code)


@bot.tree.error
async def command_error(interaction, error):
    log.error('Slash command failed', exc_info=(type(error), error, error.__traceback__))
    await private_error(interaction)


async def notify(name, title, description, color, camera=False):
    if event_listener:
        event_listener(name, title, description)
    channel_id = settings.get('notification_channel_id')
    if not channel_id or not bot.is_ready():
        return
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        if getattr(getattr(channel, 'guild', None), 'id', None) not in ALLOWED_GUILD_IDS:
            return
        embed = card(title, f'**{safe(name)}**\n{description}', color)
        picture = await snapshot(name) if camera else None
        kwargs = dict(embed=embed)
        if picture:
            embed.set_image(url='attachment://printer.jpg')
            kwargs['file'] = discord.File(io.BytesIO(picture), filename='printer.jpg')
        elif camera:
            field(embed, 'Camera', 'Snapshot unavailable; status update delivered without an image.', False)
        await channel.send(**kwargs)
    except Exception:
        log.exception('Notification failed for %s', name)


def queue_notification(*args):
    if bot_loop is not None and not bot_loop.is_closed():
        asyncio.run_coroutine_threadsafe(notify(*args), bot_loop)


def progress_description(data):
    return (
        f"**File:** {safe(data.get('subtask_name', 'Unknown'))}\n"
        f"**Status:** {safe(data.get('gcode_state', data.get('state', 'Unknown')))}\n"
        f"{progress(data.get('mc_percent'))}\n"
        f"**Remaining:** {safe(data.get('mc_remaining_time', '?'))} min • "
        f"**Layer:** {safe(data.get('layer_num', '?'))}/{safe(data.get('total_layer_num', '?'))}\n"
        f"**Nozzle:** {safe(data.get('nozzle_temper', '?'))}°C • **Bed:** {safe(data.get('bed_temper', '?'))}°C"
    )


def report_progress(name, data):
    milestone = progress_tracker.update(name, data)
    if milestone is not None:
        queue_notification(name, f'🖨️ Print progress • {milestone}%', progress_description(data), BLUE, True)


def merge_report(old, new):
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(old.get(key), dict):
            merge_report(old[key], value)
        else:
            old[key] = value


def connect_printer(printer):
    name = printer['name']
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set('bblp', printer['access_code'])
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.reconnect_delay_set(min_delay=5, max_delay=60)
    previously_connected = False
    baseline = {}

    def on_connect(client, userdata, flags, reason_code, properties):
        nonlocal previously_connected
        if reason_code != 0:
            log.warning('%s: MQTT connection rejected: %s', name, reason_code)
            return
        client.subscribe(f"device/{printer['serial']}/report")
        client.publish(f"device/{printer['serial']}/request", json.dumps({'pushing': {'sequence_id': '0', 'command': 'pushall'}}))
        baseline.clear()
        with data_lock:
            last_seen.pop(name, None)
            reports.setdefault(name, {})['gcode_state'] = 'UNKNOWN'
        log.info('Connected to %s', name)
        if previously_connected:
            queue_notification(name, '🟢 Back online', 'Printer connection restored.', GREEN)
        previously_connected = True

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning('%s disconnected; reconnecting', name)
        if previously_connected:
            queue_notification(name, '🔴 Printer offline', 'Connection lost. Retrying automatically.', RED)

    def on_connect_fail(client, userdata):
        log.warning('%s unreachable; will retry automatically', name)

    def on_message(client, userdata, message):
        try:
            new = json.loads(message.payload).get('print', {})
            if not isinstance(new, dict) or not new:
                return
            listener=globals().get('control_response_listener')
            if listener and bot_loop and new.get('command') in ('set_fan','set_ctt','gcode_line','print_speed'):
                bot_loop.call_soon_threadsafe(listener,name,dict(new))
            with data_lock:
                current = reports.setdefault(name, {})
                merge_report(current, new)
                last_seen[name] = time.time()
                job = current.get('subtask_name', 'a job')
                report_copy = copy.deepcopy(current)
            if report_listener and bot_loop:
                asyncio.run_coroutine_threadsafe(report_listener(name, report_copy), bot_loop)
            if 'mc_percent' in new or 'gcode_state' in new:
                report_progress(name, report_copy)
            state = new.get('gcode_state')
            if state is not None:
                previous = baseline.get('state')
                baseline['state'] = state
                if previous is not None and state != previous:
                    events = {
                        'FINISH': ('✅ Print complete • 100%', progress_description(dict(report_copy, mc_percent=100, mc_remaining_time=0)), GREEN, True),
                        'RUNNING': ('▶️ Print resumed' if previous == 'PAUSE' else '🖨️ Print started', safe(job), BLUE, False),
                        'PAUSE': ('⏸️ Print paused', safe(job), YELLOW, False),
                        'FAILED': ('❌ Print failed', safe(job), RED, True),
                        'PREPARE': ('⏳ Preparing print', safe(job), YELLOW, False),
                        'IDLE': ('🟢 Printer idle', 'Printer reports an idle state.', GREEN, False),
                    }
                    queue_notification(name, *events.get(state, ('ℹ️ State changed', f'{previous} → {state}', BLUE, False)))
            if 'hms' in new:
                signature = json.dumps(new['hms'], sort_keys=True)
                previous_hms = baseline.get('hms')
                baseline['hms'] = signature
                if new['hms'] and signature != previous_hms:
                    queue_notification(name, '⚠️ Printer health alert', printer_error_text(name, 0, report_copy), YELLOW, False)
            error = new.get('print_error')
            if error is not None:
                previous_error = baseline.get('error')
                baseline['error'] = error
                if previous_error is not None and error != previous_error:
                    queue_notification(name, '🛑 Printer error' if error else '✅ Error cleared',
                                       printer_error_text(name, error, report_copy) if error else 'Printer error cleared.', RED if error else GREEN, bool(error))
        except Exception:
            log.exception('Telemetry processing failed for %s', name)

    client.on_connect, client.on_disconnect = on_connect, on_disconnect
    client.on_connect_fail, client.on_message = on_connect_fail, on_message
    clients[name] = client
    client.connect_async(printer['ip'], 8883, keepalive=60)
    client.loop_start()


@bot.event
async def on_guild_join(guild):
    if guild.id not in ALLOWED_GUILD_IDS:
        await guild.leave()


@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    for guild in bot.guilds:
        if guild.id not in ALLOWED_GUILD_IDS:
            await guild.leave()
    for printer in ([] if EXAMPLE_MODE else PRINTERS):
        if printer['name'] not in clients:
            try:
                connect_printer(printer)
            except Exception:
                clients.pop(printer['name'], None)
                log.exception('Invalid connection configuration for %s', printer['name'])
    log.info('Ready as %s | %s | %s printers', bot.user, 'DEMO' if EXAMPLE_MODE else 'LIVE', len(names()))


