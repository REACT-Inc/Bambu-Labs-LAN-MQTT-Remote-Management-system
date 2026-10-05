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
                'ams':{'ams':[{'id':'0','humidity':'4','temp':'24.5',**({'info':'101'} if 'h2d' in (p.get('model') or p['name']).lower() else {}),'tray':[
                    {'id':str(i),'tray_type':t,'tray_color':c,'remain':r} for i,(t,c,r) in enumerate(
                    [('PLA','161616FF',80),('PLA','F2F2F2FF',45),('PETG','2E7DD1FF',100),('TPU','E8412CFF',20)])]}],'tray_now':'0'},
                'nozzle_diameter':'0.4','nozzle_type':'stainless_steel',
                'vt_tray':{'tray_type':'PLA','tray_color':'FFFFFFFF','remain':100},
                # Dual-nozzle demo (H2D): the AMS feeds the left nozzle (info bits 8-11 = 1), which is in use with AMS slot 1;
                # the right nozzle has the right external spool loaded. See filament_sides.py.
                # Each nozzle's temp packs target << 16 | current; device.nozzle lists the fitted hotends (multi-hotend).
                **({'device':{'extruder':{'state':2|1<<4,'info':[{'id':0,'snow':255<<8,'temp':28,'hnow':0},{'id':1,'snow':0,'temp':(220<<16)|214,'hnow':1}]},
                              'nozzle':{'info':[{'id':0,'diameter':0.4,'type':'HS00'},{'id':1,'diameter':0.4,'type':'HH01'}]}},
                    'vir_slot':[{'id':'255','tray_type':'PLA','tray_color':'FFFFFFFF','remain':100},
                                {'id':'254','tray_type':'PETG','tray_color':'161616FF','remain':60}]}
                   if 'h2d' in (p.get('model') or p['name']).lower() else {})}
    for p in (PRINTERS or [{'name':'Demo H2D'}, {'name':'Demo A1'}])
}
import asyncio
import contextlib
import base64
import copy
import difflib
import io
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands # pyright: ignore[reportMissingImports]
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
failure_monitor = None   # set by the dashboard (failureDetection, #70)
notification_view = None   # (printer, title) -> buttons for a notification, set by clear_errors_discord (#34)
alert_targets = None   # other alert destinations (alert_targets.py, #9), set by the dashboard


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
import filament_sides
from diagnostics import log_error

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


# Commands whose replies contain private details stay private even in the commands channel.
PRIVATE_COMMANDS = {'dm', 'publiccommands', 'assign report', 'meeting report assign', 'diagnostics', 'reportissue',
                    'notattending', 'attendance'}


def commands_channel():
    try:
        return int(settings.get('commands_channel_id') or 0) or None
    except (TypeError, ValueError):
        return None


def ephemeral(interaction):
    """Replies are public only in the commands channel; everywhere else only the user sees them."""
    command=getattr(interaction,'command',None)
    name=getattr(command,'qualified_name','')
    if not name:
        data=getattr(interaction,'data',None) or {}
        name=data.get('name','')
    if name in PRIVATE_COMMANDS:
        return True
    # Buttons and follow-ups on a private reply stay private.
    message=getattr(interaction,'message',None)
    if getattr(getattr(message,'flags',None),'ephemeral',False):
        return True
    channel=commands_channel()
    return channel is None or getattr(interaction,'channel_id',None)!=channel


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


# Default: commands only server administrators and approved user IDs may run. Everything else is open to
# every member of an authorized server; state-changing commands ask for confirmation and are logged.
# The dashboard can override the level per command (settings.json "command_permissions").
ADMIN_COMMANDS = {
    'adminhelp','diagnostics','reportissue','temperature','chamber','move','home','plateswap','rename','dm',
    'laptops','laptop','server','reboot','setnotificationchannel','setcommandschannel',
    'publiccommands','archive','unarchive','assign','meeting',
}
# Permission levels, most to least open.
LEVELS = ('everyone', 'role', 'admin', 'disabled')
LEVEL_LABELS = {'everyone': 'Everyone', 'role': 'Allowed roles + admins', 'admin': 'Admins only', 'disabled': 'Off'}
# Commands that can control the Pi or laptops, message people as the server, or change channels:
# these can be admin-only or turned off, never opened to everyone.
LOCKED_COMMANDS = {'reboot', 'laptop', 'laptops', 'dm', 'setnotificationchannel', 'setcommandschannel', 'archive', 'unarchive'}
# Free-text options are not copied into the service log.
PRIVATE_OPTIONS = {'message','text','title','description','command'}


def who(interaction):
    """Who ran a Discord action, for the activity log: display name plus the stable user ID."""
    user = interaction.user
    return f'Discord {getattr(user, "display_name", None) or user} ({user.id})'


def command_summary(interaction):
    data = getattr(interaction, 'data', None) or {}
    parts, options = [data.get('name', '?')], data.get('options') or []
    while options and options[0].get('type') in (1, 2):  # subcommand / subcommand group
        parts.append(options[0].get('name', '?'))
        options = options[0].get('options') or []
    values = ' '.join(f"{o.get('name')}={'[text]' if o.get('name') in PRIVATE_OPTIONS else str(o.get('value'))[:80]}" for o in options)
    return '/' + ' '.join(parts) + (' ' + values if values else '')


def admin_allowed(interaction):
    return interaction.guild_id in ALLOWED_GUILD_IDS and (
        interaction.user.id in SETTINGS_USER_IDS or
        isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)


def default_level(root):
    return 'admin' if root in ADMIN_COMMANDS else 'everyone'


def command_level(root):
    """Effective permission level for a top-level command name (e.g. 'plateswap' for /plateswap check)."""
    level = (settings.get('command_permissions') or {}).get(root, default_level(root))
    if level not in LEVELS:
        level = default_level(root)
    if root in LOCKED_COMMANDS and level in ('everyone', 'role'):
        level = 'admin'
    return level


def has_allowed_role(interaction):
    allowed = {int(r) for r in settings.get('member_role_ids') or []}
    return bool(allowed) and any(getattr(role, 'id', None) in allowed for role in (getattr(interaction.user, 'roles', None) or []))


def command_allowed(interaction, root):
    """The single permission check for every command, its buttons and its autocomplete."""
    if interaction.guild_id not in ALLOWED_GUILD_IDS:
        return False
    level = command_level(root)
    if level == 'everyone':
        return True
    if level == 'disabled':
        return False
    return admin_allowed(interaction) or (level == 'role' and has_allowed_role(interaction))


def denial_text(root):
    level = command_level(root)
    if level == 'disabled':
        return f'/{root} is turned off. An administrator can turn it on in the dashboard under Settings → Discord command permissions.'
    if level == 'role':
        return 'Only members with an allowed role, administrators and approved user IDs can use this.'
    return 'Administrators and approved user IDs only.'


async def require(interaction, root):
    """Check permission for a command (or one of its buttons) and tell the user if denied."""
    if command_allowed(interaction, root):
        return True
    log.warning('Denied %s for %s (%s)', command_summary(interaction) if getattr(interaction, 'data', None) else '/' + root, who(interaction), command_level(root))
    text = denial_text(root)
    # Always private, even in the commands channel: nobody else needs to see who was refused what (#23).
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    return False


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
        if command and not await require(interaction, command.qualified_name.split()[0]):
            return False
        log.info('%s ran %s in channel %s', who(interaction), command_summary(interaction), interaction.channel_id)
        return True


async def run_discord(client, token, sleep=asyncio.sleep, first_delay=15, max_delay=300):
    """Keep the Discord bot connected (#14). Once connected, discord.py reconnects by itself after a dropped
    connection. But if the internet is down when the bot first logs in (a Pi booting before its network, or a router
    restart), start() fails, and the bot used to stay offline until the service was restarted. Now it tries again
    (15 s, doubling to every 5 min). A rejected token is not retried: that needs fixing in config.json."""
    delay = first_delay
    while True:
        try:
            await client.start(token)
            return   # closed on purpose (the service is stopping)
        except discord.LoginFailure:
            log.error('Discord rejected the bot token. Fix "discord_token" in config.json and restart; the dashboard keeps working.')
            return
        except discord.PrivilegedIntentsRequired:
            log.error('Discord requires an intent that is not enabled for this bot in the Developer Portal; the dashboard keeps working.')
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Discord could not connect (%s: %s); retrying in %d s. The dashboard keeps working.', type(exc).__name__, exc, delay)
        with contextlib.suppress(Exception):
            await client.http.close()   # the next login opens a fresh HTTP session
        await sleep(delay)
        delay = min(max_delay, delay * 2)


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
        await private_error(interaction, log_error(log, 'Button failed', error))


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


def low_priority(command):
    """Run camera ffmpeg below the service and the rest of the Pi (#54): decoding the H2D's 1080p stream is the
    heaviest thing the service does, and at normal priority it can starve the Pi."""
    nice = shutil.which('nice')
    return [nice, '-n', '10', *command] if nice else list(command)


def snapshot_bytes(printer):
    if not printer:
        return None
    try:
        if printer.get('camera_type') == 'rtsp':
            url = f"rtsps://bblp:{printer['access_code']}@{printer['ip']}:322/streaming/live/1"
            # One still, as cheaply as possible (#54): one decoder thread, only keyframes decoded, scaled down.
            result = subprocess.run(low_priority(['ffmpeg', '-nostdin', '-loglevel', 'error', '-threads', '1', '-skip_frame', 'nokey',
                                     '-rtsp_transport', 'tcp', '-i', url, '-frames:v', '1', '-vf', 'scale=960:-2',
                                     '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', 'pipe:1']),
                                    capture_output=True, timeout=20)
            return result.stdout if result.returncode == 0 and result.stdout else None
        if printer.get('camera_type') == 'jpeg_tcp':
            from camera_capture import capture_jpeg
            return capture_jpeg(printer)
    except Exception as error:
        # Avoid logging camera URLs containing printer access codes.
        log.warning('Camera unavailable for %s (%s)', printer['name'], type(error).__name__)
    return None


async def capture_still(name, timeout=25):
    """One camera still. Concurrent callers for the same printer share one capture, so a camera never has two
    snapshot connections (or two ffmpeg decodes) at once, whether they come from Discord or the dashboard."""
    task = camera_tasks.get(name)
    if task is None or task.done():
        task = asyncio.create_task(asyncio.to_thread(snapshot_bytes, printer_config(name)))
        camera_tasks[name] = task
    return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


def recent_picture(name, max_age=60):
    """A picture that's already on hand: a frame from an open live view, or the dashboard's latest still."""
    feed = getattr(globals().get('live_cameras'), 'feeds', {}).get(name)
    if feed and feed.frame and time.time() - feed.updated < 5:
        return feed.frame
    still = getattr(globals().get('snapshot_rotation'), 'images', {}).get(name)
    if still and time.time() - still[1] < max_age:
        return still[0]
    return None


async def snapshot(name, timeout=25, max_age=60):
    # Never start a continuous live feed just for one picture (#54): reuse one on hand, else take a single still.
    # max_age: how old a reused picture may be (the AI asks for one newer than its check interval).
    if EXAMPLE_MODE:
        return None
    picture = recent_picture(name, max_age)
    if picture:
        return picture
    if not state_data(name)[3] or (printer_config(name) or {}).get('camera_type') not in ('rtsp', 'jpeg_tcp'):
        return None
    try:
        return await capture_still(name, timeout)
    except asyncio.TimeoutError:
        # Concurrent callers share this capture; cancellation never spawns duplicates.
        log.warning('Camera capture timed out for %s', name)
        return None


def whole(value):
    """A reported number rounded for display (219.96875 -> '220'), or '?' (#42)."""
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return '?'


def duration(minutes):
    """Minutes for display: '45 min', '5 h 12 min', or '?' (#42)."""
    try:
        total = max(0, round(float(minutes)))
    except (TypeError, ValueError):
        return '?'
    return f'{total // 60} h {total % 60:02d} min' if total >= 60 else f'{total} min'


def progress(value):
    try:
        percent = max(0, min(100, float(value)))
    except (TypeError, ValueError):
        return 'Unknown'
    filled = round(percent / 10)
    return '🟦' * filled + '⬜' * (10 - filled) + f' **{percent:g}%**'


def printer_reply(embed, picture):
    """The /printer embed with its camera picture attached, or a note that there isn't one."""
    if picture:
        embed.set_image(url='attachment://printer.jpg')
        return dict(embed=embed, file=discord.File(io.BytesIO(picture), filename='printer.jpg'))
    field(embed, 'Camera', 'No camera in demo mode' if EXAMPLE_MODE else
          'Unavailable. Check the camera settings (LAN live view on the printer, `camera_type` in config.json).', False)
    return dict(embed=embed)


def printer_embed(name):
    state, error, data, connected = state_data(name)
    embed = card('🖨️ ' + display_name(name), f"{'🟢 Connected' if connected else '🔴 Offline / reconnecting'} • **{safe(state)}**",
                 RED if error or not connected else STATE_COLORS.get(state, GRAY))
    field(embed, 'File', safe(data.get('subtask_name', 'No file reported')), False)
    field(embed, 'Progress', progress(data.get('mc_percent')), False)
    field(embed, 'Time remaining', duration(data.get('mc_remaining_time')))
    field(embed, 'Layer', f"{data.get('layer_num', '?')} / {data.get('total_layer_num', '?')}")
    sides = filament_sides.nozzles(data)
    if sides:
        # Dual-nozzle printers: each nozzle's temperature, fitted hotend and loaded filament (multi-hotend).
        for n in sides:
            heat = f"{whole(n['current'])}°C → {whole(n['target']) + '°C' if n['target'] else 'off'}"
            details = [heat, n['hotend']['label'] if n['hotend'] else '', n['filament'] and 'Loaded: ' + n['filament']]
            field(embed, f"{n['side']} nozzle" + (' • in use' if n['active'] else ''), '\n'.join(d for d in details if d))
    else:
        field(embed, 'Nozzle', f"{whole(data.get('nozzle_temper'))}°C → {whole(data.get('nozzle_target_temper'))}°C")
    field(embed, 'Bed', f"{whole(data.get('bed_temper'))}°C → {whole(data.get('bed_target_temper'))}°C")
    rack = filament_sides.hotends(data)[1]
    if rack:
        field(embed, 'Hotend rack', '\n'.join(f"Slot {h['slot'] + 1}: {h['label']}" for h in rack), False)
    if error or data.get('hms'):
        field(embed, '⚠️ Printer error', printer_error_text(name, error, data), False)
    ai = failure_monitor.state(name) if failure_monitor else {}
    if ai.get('enabled'):
        field(embed, '🤖 AI failure watch', ai_watch_text(ai), False)
    if not EXAMPLE_MODE:
        seen = last_seen.get(name)
        field(embed, 'Last telemetry', f'<t:{int(seen)}:R>' if seen else 'No telemetry received', False)
    return embed


def ai_watch_text(ai):
    if not ai.get('watching'):
        return 'Off for this printer'
    label = {'failure': '⚠️ Possible failure', 'paused': '⏸️ Paused by AI', 'suspect': '👀 Suspect frames',
             'unavailable': '❌ AI unavailable', 'watching': '✅ Watching'}.get(ai.get('status'), 'Waiting for a print')
    return f"{label}" + (f" • {safe(ai['message'])}" if ai.get('message') else '')


def tray_text(tray):
    if not tray or not tray.get('tray_type'):
        return 'Empty / not reported'
    try:
        remain = float(tray.get('remain', -1))
        remaining = f'{remain:g}%' if remain >= 0 else 'unknown'
    except (TypeError, ValueError):
        remaining = 'unknown'
    return f"{safe(tray['tray_type'])} • #{safe(tray.get('tray_color', '?'))} • {remaining} remaining"


def tray_present(exist_bits, unit, tray):
    # tray_exist_bits is a hex mask with bit (unit * 4 + slot) set for each loaded 4-slot AMS tray.
    # Merged reports keep the last known tray data, so this is what tells us a spool was removed.
    try:
        unit_id, tray_id = int(unit.get('id')), int(tray.get('id'))
        if exist_bits is None or unit_id >= 32:
            return True
        return bool(int(str(exist_bits), 16) >> (unit_id * 4 + tray_id) & 1)
    except (TypeError, ValueError):
        return True


def filament_embed(name):
    _, _, data, connected = state_data(name)
    embed = card('🧵 ' + display_name(name) + ' • Filaments', '' if connected else '🔴 Offline • Last reported data', BLUE if connected else RED)
    ams = data.get('ams', {})
    units = ams if isinstance(ams, list) else ams.get('ams', [])
    exist_bits = None if isinstance(ams, list) else ams.get('tray_exist_bits')
    # Dual-nozzle printers (H2D): which nozzle each AMS / external spool feeds and what each nozzle has loaded (#5).
    dual, loaded = filament_sides.is_dual(data), filament_sides.loaded_slots(data)
    if dual and filament_sides.active_nozzle(data) is not None:
        embed.description = (embed.description + '\n' if embed.description else '') + \
            f"Nozzle in use: **{filament_sides.side_label(filament_sides.active_nozzle(data))}**"

    def in_nozzle(ams_id, slot):
        nozzle = loaded.get((ams_id, slot))
        return f" • ◀ loaded in the {filament_sides.SIDES[nozzle].lower()} nozzle" if nozzle is not None else ''

    for unit in units[:24]:
        text = f"Humidity: {unit.get('humidity', '?')} • Temperature: {whole(unit.get('temp'))}°C\n"
        try:
            unit_id = int(unit.get('id'))
        except (TypeError, ValueError):
            unit_id = None
        text += '\n'.join(f"**Slot {tray.get('id', '?')}** — {tray_text(tray if tray_present(exist_bits, unit, tray) else None)}"
                          + (in_nozzle(unit_id, int(tray['id'])) if str(tray.get('id', '')).isdigit() else '')
                          for tray in unit.get('tray', []))
        side = filament_sides.side_label(filament_sides.ams_nozzle(unit)) if dual else ''
        field(embed, 'AMS ' + str(unit.get('id', '?')) + (f' • {side}' if side else ''), text, False)
    for ams_id, tray, nozzle in filament_sides.external_spools(data):
        name = f'External spool • {filament_sides.side_label(nozzle)}' if nozzle is not None else 'External spool'
        field(embed, name, tray_text(tray) + in_nozzle(ams_id, 0), False)
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
        if event_listener:
            event_listener(name, 'Control submitted', f'{action} • {who(interaction)}')
        description = f'**{safe(name)}**\n'
        description += 'Demo only — no printer was controlled.' if EXAMPLE_MODE else 'Command queued. Check printer status to confirm the result.'
        if filename:
            description += '\nFile: ' + safe(filename)
        embed = card(('🧪 Demo: ' if EXAMPLE_MODE else '📡 ') + action.title(), description, GREEN)
    except RuntimeError as error:
        embed = card('Command not sent', str(error), RED)
    await respond(interaction, embed)


# Printer actions anyone may run; each asks for confirmation first.
CONFIRMED_ACTIONS = {
    'pause': ('Pause print', 'Pause the current print on **{}**?'),
    'resume': ('Resume print', 'Resume the paused print on **{}**?\nCheck the printer and build plate are ready first.'),
    'stop': ('Stop print', 'Cancel the current print on **{}**?\nThis cannot be undone.'),
    'lighton': ('Turn light on', 'Turn the chamber light **on** on **{}**?'),
    'lightoff': ('Turn light off', 'Turn the chamber light **off** on **{}**?'),
}


class ActionConfirm(OwnedView):
    def __init__(self, owner, name, action, filename=None):
        super().__init__(owner)
        self.name, self.action, self.filename = name, action, filename
        label = CONFIRMED_ACTIONS[action][0] if action in CONFIRMED_ACTIONS else 'Reprint'
        self.button(label, self.confirm, discord.ButtonStyle.primary if action in ('lighton', 'lightoff', 'resume') else discord.ButtonStyle.danger)
        self.button('Cancel', self.cancel)

    async def confirm(self, interaction):
        if await self.finish(interaction, card('Confirmed', f'{self.action.title()} • {safe(self.name)}', YELLOW)):
            await send_action(interaction, self.name, self.action, self.filename)


async def run_action(interaction, action, name):
    if action == 'printer':
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral(interaction))
        # Reply with the status straight away; the camera picture is added when it arrives (#44, #54).
        embed = printer_embed(name)
        picture = recent_picture(name) if not EXAMPLE_MODE else None
        if picture or EXAMPLE_MODE:
            await interaction.followup.send(**printer_reply(embed, picture), ephemeral=ephemeral(interaction))
            return
        field(embed, 'Camera', '📷 Getting a picture…', False)
        message = await interaction.followup.send(embed=embed, ephemeral=ephemeral(interaction), wait=True)
        picture = await snapshot(name, timeout=20)
        embed.remove_field(len(embed.fields) - 1)
        reply = printer_reply(embed, picture)
        try:
            await message.edit(embed=reply['embed'], attachments=[reply['file']] if 'file' in reply else [])
        except discord.HTTPException as error:
            log.warning('Could not add the camera picture to /printer for %s (%s)', name, type(error).__name__)
    elif action == 'filaments':
        await respond(interaction, filament_embed(name))
    elif action in CONFIRMED_ACTIONS:
        await respond(interaction, card('⚠️ Confirm ' + CONFIRMED_ACTIONS[action][0].lower(), CONFIRMED_ACTIONS[action][1].format(safe(name)), YELLOW),
                      ActionConfirm(interaction.user.id, name, action))
    elif action == 'reprint':
        filename = state_data(name)[2].get('subtask_name')
        if not filename:
            await respond(interaction, card('No file available', 'No previous filename has been reported.', YELLOW))
            return
        description = (
            f'Reprint **{safe(filename)}** on **{safe(name)}**?\n'
            'Clear the build plate before confirming.\n'
            'Experimental: the reported job name may not be a printable file on storage. '
            'Uses plate 1 and external spool; requires a compatible printer configuration.')
        await respond(interaction, card('⚠️ Confirm ' + action, description, RED),
                      ActionConfirm(interaction.user.id, name, action, filename))
    else:
        await send_action(interaction, name, action)


@bot.tree.command(name='setnotificationchannel', description='Set notifications here (admins or approved users)')
@app_commands.guild_only()
async def set_notification_channel(interaction: discord.Interaction):
    if await require(interaction, 'setnotificationchannel'):
        store_channel('notification_channel_id', interaction)
        await respond(interaction, card('🔔 Notifications configured', 'Automatic printer updates will appear in this channel.', GREEN))


@bot.tree.command(name='setcommandschannel', description='Set the main commands channel (admins or approved users)')
@app_commands.guild_only()
async def set_commands_channel(interaction: discord.Interaction):
    if await require(interaction, 'setcommandschannel'):
        store_channel('commands_channel_id', interaction)
        await respond(interaction, card('💬 Commands channel configured', 'Commands channel saved. Replies are public here and private (only the person who ran the command sees them) in every other channel. To hide the commands elsewhere, limit the bot under Server Settings → Integrations → Channels.', GREEN))


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
        ('🎛️ Printer controls', {'pause','resume','stop','lighton','lightoff','temperature','chamber','speed','fan','fanall','move','home'}, 'Pause/resume/cancel, lights, temperatures, speed, fan, homing and axis jogging.'),
        ('📋 Print queues', {'queueadd','queue','queuestart','queueforce','queuemanage','reprint'}, 'View, add, start and manage jobs. Starts and changes ask for confirmation and are logged.'),
        ('🔄 Swapmod', {'plateswap'}, 'Configure equipped printers, approve Swaplist batches and check the starting setup.'),
        ('⚙️ Administration', {'setnotificationchannel','setcommandschannel','publiccommands','rename','dm','archive','unarchive','diagnostics','reportissue'}, 'Channel settings, temporary public replies, printer names, DMs, archives, diagnostic reports and problem reports.'),
        ('🗓️ Team & reminders', {'ftc','website','management','rememberthis','remember','forget','remindme','reminders','cancelreminder','attending','notattending','attendance','meeting','assign'}, 'Assign meeting-report writers.' if admin else 'Team links, shared notes, reminders and meeting attendance.'),
        ('💻 Laptops & server', {'laptops','laptop','server','reboot'}, 'MeshCentral laptops and commands, Pi status and reboot.'),
    ]
    commands = []
    def collect(command, root=None):
        root = root or command.name
        if isinstance(command, app_commands.Group):
            for child in command.commands:
                collect(child, root)
        else:
            # /help lists what everyone (or allowed roles) can use; /adminhelp lists admin-only commands. Off commands are hidden.
            level = command_level(root)
            if level != 'disabled' and (level == 'admin') == admin:
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
          'Use printer-name autocomplete or the selection buttons when available. Admin commands require a server administrator or an approved user ID; '
          'administrators can change who may use each command in the dashboard. '
          'Replies are public in the commands channel and private everywhere else. DMs, diagnostics, problem reports, report assignment and attendance replies are always private.', False)
    return embed


@bot.tree.command(name='help', description='Everyday printer and team commands')
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    await respond(interaction, help_embed())


@bot.tree.command(name='adminhelp', description='Command guide for administrators and approved IDs')
@app_commands.guild_only()
async def admin_help_command(interaction: discord.Interaction):
    if not await require(interaction, 'adminhelp'):
        return
    await interaction.response.send_message(embed=help_embed(admin=True), ephemeral=ephemeral(interaction))


async def private_error(interaction, error_id=None):
    reference = f'\nError ID: `{error_id}` (an administrator can find it with /diagnostics).' if error_id else ''
    embed = card('Something went wrong', 'The error was logged.' + reference, RED)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral(interaction))
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral(interaction))
    except discord.HTTPException as error:
        log.warning('Could not deliver error response (%s); original command error is logged above.', error.code)


@bot.tree.error
async def command_error(interaction, error):
    name = interaction.command.qualified_name if interaction.command else '?'
    await private_error(interaction, log_error(log, f'Slash command /{name} failed', getattr(error, 'original', error)))


async def notify(name, title, description, color, camera=False):
    if event_listener:
        event_listener(name, title, description)
    if alert_targets:   # Home Assistant, ntfy, webhooks (#9): also without the Discord bot
        alert_targets.dispatch(name, title, description, color)
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
        view = notification_view(name, title) if notification_view else None   # e.g. Clear error (#34)
        if view:
            kwargs['view'] = view
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
        f"**Remaining:** {duration(data.get('mc_remaining_time'))} • "
        f"**Layer:** {safe(data.get('layer_num', '?'))}/{safe(data.get('total_layer_num', '?'))}\n"
        f"**Nozzle:** {whole(data.get('nozzle_temper'))}°C • **Bed:** {whole(data.get('bed_temper'))}°C"
    )


def report_progress(name, data):
    milestone = progress_tracker.update(name, data)
    if milestone is not None:
        queue_notification(name, f'🖨️ Print progress • {milestone}%', progress_description(data), BLUE, True)


def keyed_by_id(items):
    return isinstance(items, list) and all(isinstance(x, dict) and 'id' in x for x in items)


def merge_report(old, new):
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(old.get(key), dict):
            merge_report(old[key], value)
        elif keyed_by_id(value) and keyed_by_id(old.get(key)):
            # Incremental reports (e.g. ams.ams, ams.ams[].tray) only list the units/slots that
            # changed; replacing the whole list would drop the others, such as AMS 0 on the H2D.
            existing = {str(item['id']): item for item in old[key]}
            for item in value:
                current = existing.get(str(item['id']))
                if current is None:
                    old[key].append(item)
                else:
                    merge_report(current, item)
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
            payload = json.loads(message.payload)
            listener=globals().get('control_response_listener')
            system = payload.get('system') if isinstance(payload, dict) else None
            if listener and bot_loop and isinstance(system, dict) and system.get('command') == 'set_accessories':
                bot_loop.call_soon_threadsafe(listener,name,dict(system))
            new = payload.get('print', {}) if isinstance(payload, dict) else {}
            if not isinstance(new, dict) or not new:
                return
            if listener and bot_loop and new.get('command') in ('set_fan','set_ctt','gcode_line','print_speed','xyz_ctrl','back_to_center','ams_filament_setting'):
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


