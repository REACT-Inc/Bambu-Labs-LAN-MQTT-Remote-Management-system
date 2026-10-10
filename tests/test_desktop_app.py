"""The desktop app for laptops (desktop/) and the Pi's summary for its tray icon (/api/desktop).

The GUI itself (the window and tray) is tested end to end on Windows by desktop/ci/smoke_test.py; here are its
decisions (notifications, tray text, settings, finding the Pi...) and the window/tray glue with stand-ins.
"""
import asyncio
import importlib
import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parents[1]
DESKTOP = ROOT / 'desktop'
sys.path.insert(0, str(ROOT))
if DESKTOP.is_dir():   # release ZIPs leave the desktop app out
    sys.path.insert(0, str(DESKTOP))
    import desktop_state
    import pi_client

from dashboard import Dashboard, atomic_json, password_hash
from queueing import Engine, Store

needs_desktop = unittest.skipUnless(DESKTOP.is_dir(), 'the desktop app is not part of release ZIPs')


def summary(notes=(), started=1.0, latest=None, printers=(), level='ok', demo=False):
    return dict(application='3d-printer-management', started=started, demo=demo, printers=list(printers),
                status=dict(level=level, items=[], notifications=list(notes),
                            latest=latest if latest is not None else max([n['id'] for n in notes] or [0])))


def note(id, title='✅ Print complete', level='ok', at=None, printer='A1', detail='cube.3mf'):
    return dict(id=id, title=title, level=level, at=at if at is not None else 1000.0 + id, printer=printer, detail=detail)


class PiSummaryTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/desktop on the Pi."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.data = {'A': ('RUNNING', 0, {'mc_percent': 45, 'mc_remaining_time': 80, 'subtask_name': 'cube.3mf'}, True),
                     'B': ('IDLE', int('0502C014', 16), {}, True)}
        self.core = SimpleNamespace(
            DATA_DIR=d, SETTINGS_FILE=str(d / 'settings.json'), settings={}, SETTINGS_USER_IDS=set(), EXAMPLE_MODE=False,
            names=lambda: ['A', 'B'], state_data=lambda n: self.data[n], last_seen={},
            bot=SimpleNamespace(is_ready=lambda: False), log=__import__('logging').getLogger('test'),
            display_name=lambda n: {'A': 'H2D'}.get(n, n), display_state=lambda s, e, c: 'Error' if e else {'RUNNING': 'Printing'}.get(s, 'Ready'))
        self.store = Store(d / 'db')
        self.dashboard = Dashboard(self.core, self.store, Engine(self.core, self.store))
        atomic_json(d / 'auth.json', password_hash('test-password-123'))
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.db.close()
        self.tmp.cleanup()

    async def login(self):
        response = await self.client.post('/api/login', json={'password': 'test-password-123'}, headers={'X-PM': '1'})
        self.assertEqual(response.status, 200)

    async def test_needs_sign_in(self):
        self.assertEqual((await self.client.get('/api/desktop')).status, 401)

    async def test_printers_status_and_notifications(self):
        self.dashboard.status.note('A', '✅ Print complete', '**cube.3mf** done')
        await self.login()
        data = await (await self.client.get('/api/desktop')).json()
        self.assertEqual(data['application'], '3d-printer-management')
        self.assertEqual(data['started'], self.dashboard.started)
        self.assertIn('version', data)
        h2d, other = data['printers']
        self.assertEqual((h2d['name'], h2d['display_name'], h2d['display_state'], h2d['progress'], h2d['remaining'], h2d['file']),
                         ('A', 'H2D', 'Printing', 45, 80, 'cube.3mf'))
        self.assertEqual(other['display_state'], 'Error')
        self.assertIn('https://e.bambulab.com/', other['error_text'])
        self.assertNotIn('data', h2d)   # much smaller than /api/state
        self.assertEqual([n['title'] for n in data['status']['notifications']], ['✅ Print complete'])
        self.assertEqual(data['status']['notifications'][0]['detail'], 'cube.3mf done')

    async def test_a_laptop_in_the_background_does_not_keep_the_cameras_busy(self):
        await self.login()
        await self.client.get('/api/desktop')
        self.assertFalse(self.dashboard.snapshots.viewing())
        await self.client.get('/api/state')   # an open dashboard does
        self.assertTrue(self.dashboard.snapshots.viewing())

    @needs_desktop
    async def test_the_desktop_client_reads_it_with_the_window_sign_in(self):
        base = str(self.client.make_url('')).rstrip('/')
        self.assertEqual(await asyncio.to_thread(pi_client.check, base), '')
        with self.assertRaises(pi_client.SignedOut):
            await asyncio.to_thread(pi_client.summary, base, 'not-a-session')
        response = await self.client.post('/api/login', json={'password': 'test-password-123'}, headers={'X-PM': '1'})
        session = response.cookies['pm_session'].value
        data = await asyncio.to_thread(pi_client.summary, base, session)
        self.assertEqual(len(data['printers']), 2)



@needs_desktop
class AddressTests(unittest.TestCase):
    def test_normalise(self):
        cases = {'192.168.1.50': 'http://192.168.1.50:8080', ' 192.168.1.50:8081 ': 'http://192.168.1.50:8081',
                 'http://100.64.1.2:8080/anything?x=1': 'http://100.64.1.2:8080', 'Pi.Local': 'http://pi.local:8080',
                 'https://printers.example.org': 'https://printers.example.org', 'https://p.example.org:8443': 'https://p.example.org:8443',
                 '[fd00::1]': 'http://[fd00::1]:8080', 'printers.tail1234.ts.net': 'http://printers.tail1234.ts.net:8080'}
        for given, expected in cases.items():
            self.assertEqual(pi_client.normalise(given), expected, given)

    def test_refused(self):
        for bad in ('', '   ', 'ftp://192.168.1.50', 'http://admin:secret@192.168.1.50', 'not an address', 'http://pi_host',
                    '192.168.1.50:99999', 'http://'):
            with self.assertRaises(pi_client.PiError, msg=bad):
                pi_client.normalise(bad)

    def test_host_of(self):
        self.assertEqual(pi_client.host_of('http://[fd00::1]:8080'), 'fd00::1')
        self.assertEqual(pi_client.host_of('http://Pi.local:8080'), 'pi.local')


@needs_desktop
class CheckTests(unittest.TestCase):
    """The health check against small stand-in web servers."""

    def serve(self, status, body):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def test_the_dashboard(self):
        base = self.serve(200, b'{"ok": true, "application": "3d-printer-management", "release": "abc"}')
        self.assertEqual(pi_client.check(base), 'abc')

    def test_something_else(self):
        for status, body in ((200, b'{"application": "other"}'), (200, b'<html>router</html>'), (404, b'')):
            with self.assertRaisesRegex(pi_client.PiError, "isn't the 3D Printer Management dashboard"):
                pi_client.check(self.serve(status, body))

    def test_nothing_there(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        with self.assertRaisesRegex(pi_client.PiError, 'Nothing answered .*refused'):
            pi_client.check(f'http://127.0.0.1:{port}')

    def test_old_pi(self):
        base = self.serve(404, b'{"error": "Not Found"}')
        with self.assertRaises(pi_client.TooOld):
            pi_client.summary(base, 'session')
        with self.assertRaises(pi_client.SignedOut):
            pi_client.summary(base, '')


@needs_desktop
class FindTests(unittest.TestCase):
    def test_finds_dashboards_on_the_network_and_tailnet(self):
        import ipaddress
        answering = {'192.168.7.20': '', '100.70.1.5': 'printers.tail.ts.net', '127.0.0.1': ''}
        checked = []

        def check(base, timeout):
            checked.append(base)
            return 'r1'
        found = pi_client.find(networks=[ipaddress.IPv4Network('192.168.7.0/24')],
                               devices=[('100.70.1.5', 'printers.tail.ts.net'), ('100.70.1.6', 'laptop')],
                               reachable=lambda ip: ip in answering, check_base=check)
        self.assertEqual([pi['url'] for pi in found],
                         ['http://100.70.1.5:8080', 'http://127.0.0.1:8080', 'http://192.168.7.20:8080'])
        self.assertEqual(found[0]['name'], 'printers.tail.ts.net')
        self.assertEqual(found[1]['name'], 'this computer')
        self.assertEqual(sorted(checked), sorted(pi['url'] for pi in found))   # only open ports get a request

    def test_a_web_server_that_is_not_the_dashboard_is_skipped(self):
        def check(base, timeout):
            raise pi_client.PiError('no')
        self.assertEqual(pi_client.find(networks=[], devices=[], reachable=lambda ip: True, check_base=check), [])

    def test_tailscale_devices(self):
        status = dict(Self=dict(TailscaleIPs=['100.70.1.9', 'fd7a::9'], DNSName='laptop.tail.ts.net.'),
                      Peer={'a': dict(TailscaleIPs=['100.70.1.5'], DNSName='printers.tail.ts.net.', Online=True),
                            'b': dict(TailscaleIPs=['100.70.1.6'], HostName='old-pi', Online=False)})
        run = MagicMock(return_value=SimpleNamespace(stdout=json.dumps(status).encode()))
        devices = pi_client.tailscale_devices(run=run, which=lambda name: '/usr/bin/tailscale')
        self.assertEqual(devices, [('100.70.1.5', 'printers.tail.ts.net'), ('100.70.1.9', 'laptop.tail.ts.net')])
        self.assertEqual(run.call_args[0][0], ['/usr/bin/tailscale', 'status', '--json'])
        with patch('os.path.isfile', return_value=False):
            self.assertEqual(pi_client.tailscale_devices(run=run, which=lambda name: None), [])
        broken = MagicMock(side_effect=OSError('no'))
        self.assertEqual(pi_client.tailscale_devices(run=broken, which=lambda name: 'tailscale'), [])

    def test_local_networks_are_private_slash_24s(self):
        for network in pi_client.local_networks():
            self.assertEqual(network.prefixlen, 24)
            self.assertTrue(network.is_private)


@needs_desktop
class SettingsTests(unittest.TestCase):
    def test_folders(self):
        roaming, local = desktop_state.folders({'APPDATA': r'C:\U\AppData\Roaming', 'LOCALAPPDATA': r'C:\U\AppData\Local'}, 'win32', '/h')
        self.assertEqual((roaming.name, local.name), ('3D Printer Management', '3D Printer Management'))
        self.assertIn('Roaming', str(roaming))
        self.assertIn('Local', str(local))
        config, data = desktop_state.folders({}, 'linux', '/h')
        self.assertEqual((str(config), str(data)), ('/h/.config/3d-printer-management-desktop', '/h/.local/share/3d-printer-management-desktop'))
        mac, same = desktop_state.folders({}, 'darwin', '/h')
        self.assertEqual(mac, same)

    def test_defaults_saving_and_bad_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sub' / 'desktop.json'
            settings = desktop_state.Settings(path)
            self.assertEqual((settings['address'], settings['notify'], settings['seen']), ('', 'important', {}))
            settings.update(address='http://192.168.1.50:8080', notify='problems')
            again = desktop_state.Settings(path)
            self.assertEqual((again['address'], again['notify']), ('http://192.168.1.50:8080', 'problems'))
            path.write_text(json.dumps({'address': 5, 'notify': 'loud', 'unknown': 1, 'told_tray': True}))
            odd = desktop_state.Settings(path)
            self.assertEqual((odd['address'], odd['notify'], odd['told_tray']), ('', 'important', True))
            self.assertNotIn('unknown', odd.values)
            path.write_text('{broken')
            self.assertEqual(desktop_state.Settings(path)['address'], '')
            # Every Settings starts from its own copy of the defaults.
            desktop_state.Settings(Path(folder) / 'x.json')['seen']['changed'] = 1
            self.assertEqual(desktop_state.DEFAULTS['seen'], {})


@needs_desktop
class NotificationTests(unittest.TestCase):
    def test_nothing_is_replayed_the_first_time(self):
        new, memory = desktop_state.new_notifications(summary([note(1), note(2)]), {})
        self.assertEqual(new, [])
        self.assertEqual(memory, dict(started=1.0, seen=2, seen_at=1002.0))

    def test_new_ones_oldest_first(self):
        memory = dict(started=1.0, seen=2, seen_at=1002.0)
        new, memory = desktop_state.new_notifications(summary([note(4), note(3), note(2)]), memory)
        self.assertEqual([n['id'] for n in new], [3, 4])
        self.assertEqual(memory['seen'], 4)
        self.assertEqual(desktop_state.new_notifications(summary([note(4), note(3)]), memory)[0], [])

    def test_after_the_pi_app_restarted(self):
        # Its numbers start again from 1: the ones newer than what was already seen count.
        memory = dict(started=1.0, seen=9, seen_at=1009.0)
        restarted = summary([note(2, at=1020.0), note(1, at=1005.0)], started=2.0)
        new, memory = desktop_state.new_notifications(restarted, memory)
        self.assertEqual([n['id'] for n in new], [2])
        self.assertEqual(memory, dict(started=2.0, seen=2, seen_at=1020.0))

    def test_choices(self):
        problem, done, progress = note(1, '⚠️ Queue needs review', 'warn'), note(2), note(3, '🖨️ Print progress • 50%', 'info')
        pick = lambda choice: [n['id'] for n in (problem, done, progress) if desktop_state.wanted(n, choice)]
        self.assertEqual(pick('important'), [1, 2])
        self.assertEqual(pick('all'), [1, 2, 3])
        self.assertEqual(pick('problems'), [1])
        self.assertEqual(pick('off'), [])
        self.assertEqual([choice for choice, _ in desktop_state.NOTIFY], ['important', 'all', 'problems', 'off'])

    def test_one_pop_up(self):
        title, message = desktop_state.toast([note(1)], 'important', {'A1': 'Bench A1'})
        self.assertEqual((title, message), ('✅ Print complete', 'Bench A1 · cube.3mf'))
        self.assertIsNone(desktop_state.toast([note(1)], 'off'))
        self.assertIsNone(desktop_state.toast([], 'all'))
        self.assertEqual(desktop_state.toast([note(1, printer='', detail='')], 'all')[1], 'Open the app for details.')

    def test_many_at_once_become_one_pop_up(self):
        notes = [note(i, f'Alert {i}') for i in range(1, 6)]
        title, message = desktop_state.toast(notes, 'all')
        self.assertEqual(title, '5 new notifications')
        self.assertEqual(message.splitlines(), ['• A1: Alert 5', '• A1: Alert 4', '• A1: Alert 3', '…and 2 more in the app'])

    def test_pop_ups_fit_windows_limits(self):
        long = '🖨️ ' * 100   # emoji take two UTF-16 units each
        title, message = desktop_state.toast([note(1, long, detail=long)], 'all')
        self.assertLess(len(title.encode('utf-16-le')) // 2, 64)
        self.assertLess(len(message.encode('utf-16-le')) // 2, 256)
        self.assertTrue(title.endswith('…'))
        self.assertEqual(desktop_state.fit('short', 64), 'short')


@needs_desktop
class TrayTextTests(unittest.TestCase):
    printers = [dict(name='A', display_name='H2D', display_state='Printing', progress=45.4, remaining=80),
                dict(name='B', display_name='A1 mini', display_state='Paused', progress=12),
                dict(name='C', display_state='Error', error_text='HMS 0300: Nozzle clog detected. Check the nozzle.\nMore: https://e.bambulab.com/x'),
                dict(name='D', display_state='Offline'), dict(name='E', display_state='Ready'),
                dict(name='F', display_state='Printing', progress=None, remaining=None)]

    def test_printer_lines(self):
        lines = [desktop_state.printer_line(p) for p in self.printers]
        self.assertEqual(lines, ['H2D — Printing 45% · 1 h 20 min left', 'A1 mini — Paused at 12%',
                                 'C — Error: HMS 0300: Nozzle clog detected. Check the nozzle.', 'D — Offline', 'E — Ready',
                                 'F — Printing'])
        self.assertEqual(desktop_state.printer_line(dict(name='G', display_state='Error', error_text='x' * 200))[-1], '…')

    def test_lines_tooltip_and_icon(self):
        data = summary(printers=self.printers, level='warn')
        self.assertEqual(len(desktop_state.tray_lines(data)), 6)
        self.assertEqual(desktop_state.tray_lines(summary(), "Can't reach the Pi"), ["Can't reach the Pi"])
        self.assertEqual(desktop_state.tray_lines(summary()), ['No printers set up yet'])
        self.assertTrue(desktop_state.tray_lines(summary(printers=self.printers[:1], demo=True))[0].startswith('Demo printers'))
        self.assertEqual(desktop_state.tooltip(data),
                         '3D Printer Management\n2 printing · 1 paused · 1 with an error · 1 offline · 1 ready')
        self.assertEqual(desktop_state.tooltip(summary()), '3D Printer Management\nNo printers yet')
        self.assertEqual(desktop_state.icon_level(data), 'warn')
        self.assertEqual(desktop_state.icon_level(summary(level='info')), 'ok')
        self.assertEqual(desktop_state.icon_level(data, 'Not signed in'), 'offline')
        self.assertLess(len(desktop_state.tooltip(summary(), 'x' * 300).encode('utf-16-le')) // 2, 128)

    def test_ampersands_in_windows_menus(self):
        self.assertEqual(desktop_state.menu_text('Tom & Jerry — Ready', windows=True), 'Tom && Jerry — Ready')
        self.assertEqual(desktop_state.menu_text('Tom & Jerry', windows=False), 'Tom & Jerry')


class FakeRegistry:
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = 'HKCU', 2, 1

    def __init__(self):
        self.values = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def OpenKey(self, root, path):
        return self.Key()

    def CreateKeyEx(self, root, path, reserved, access):
        assert path == desktop_state.RUN_KEY
        return self.Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


@needs_desktop
class StartupTests(unittest.TestCase):
    def test_start_with_windows(self):
        registry = FakeRegistry()
        startup = desktop_state.Startup(r'C:\Apps\3d-printer-management-desktop.exe', registry)
        self.assertTrue(startup.available())
        self.assertFalse(startup.enabled())
        startup.set(True)
        self.assertEqual(registry.values['3D Printer Management'], r'"C:\Apps\3d-printer-management-desktop.exe" --tray')
        self.assertTrue(startup.enabled())
        self.assertFalse(startup.repair())
        startup.set(False)
        startup.set(False)   # already off
        self.assertFalse(startup.enabled())

    def test_moved_exe_is_repaired(self):
        registry = FakeRegistry()
        registry.values['3D Printer Management'] = r'"C:\Old\app.exe" --tray'
        startup = desktop_state.Startup(r'C:\New\app.exe', registry)
        self.assertTrue(startup.repair())
        self.assertEqual(registry.values['3D Printer Management'], r'"C:\New\app.exe" --tray')

    def test_only_for_the_exe(self):
        startup = desktop_state.Startup(None)
        self.assertFalse(startup.available())
        self.assertFalse(startup.enabled())
        self.assertFalse(startup.repair())


@needs_desktop
class SingleInstanceTests(unittest.TestCase):
    def test_a_second_copy_hands_over(self):
        with tempfile.TemporaryDirectory() as folder:
            first, commands = desktop_state.SingleInstance(folder), []
            done = threading.Event()
            first.listen(lambda command: (commands.append(command), done.set()))
            second = desktop_state.SingleInstance(folder)
            self.assertTrue(second.signal('show'))
            self.assertTrue(done.wait(5))
            self.assertEqual(second.running()['pid'], __import__('os').getpid())
            self.assertFalse(second.signal('format c:'))   # only show and quit
            info = second.running()
            Path(folder, 'running.json').write_text(json.dumps(dict(info, token='guess')))
            self.assertFalse(second.signal('quit'))   # the secret is needed
            Path(folder, 'running.json').write_text(json.dumps(info))
            first.close()
            self.assertFalse(Path(folder, 'running.json').exists())
            self.assertFalse(second.signal('show'))   # nobody is running
            self.assertEqual(commands, ['show'])

    def test_a_stale_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            Path(folder, 'running.json').write_text(json.dumps(dict(port=port, token='x', pid=1)))
            self.assertFalse(desktop_state.SingleInstance(folder).signal())
            Path(folder, 'running.json').write_text('{broken')
            self.assertFalse(desktop_state.SingleInstance(folder).signal())


@needs_desktop
class IconAndBuildTests(unittest.TestCase):
    def test_icon(self):
        import icon
        plain, warn = icon.draw(64), icon.draw(64, 'warn')
        self.assertEqual((plain.size, plain.mode), ((64, 64), 'RGBA'))
        self.assertNotEqual(plain.tobytes(), warn.tobytes())
        self.assertEqual(icon.draw(16).size, (16, 16))
        with tempfile.TemporaryDirectory() as folder:
            icon.write_ico(Path(folder) / 'icon.ico')
            from PIL import Image
            with Image.open(Path(folder) / 'icon.ico') as ico:
                self.assertIn((256, 256), ico.info['sizes'])
                self.assertIn((16, 16), ico.info['sizes'])

    def test_build_inputs(self):
        import build
        self.assertEqual(build.version_numbers('v1.7.8-beta.1'), (1, 7, 8, 1))
        self.assertEqual(build.version_numbers('v2.0.0'), (2, 0, 0, 0))
        self.assertEqual(build.version_numbers('dev'), (0, 0, 0, 0))
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            build.prepare('v1.7.8-beta.1', work)
            self.assertEqual((work / '_version.py').read_text(), "VERSION = '1.7.8-beta.1'\n")
            info = (work / 'version_info.txt').read_text()
            self.assertIn('filevers=(1, 7, 8, 1)', info)
            self.assertIn("StringStruct('FileDescription', '3D Printer Management')", info)
            self.assertTrue((work / 'icon.ico').stat().st_size > 1000)
            command = build.command(work, work / 'dist')
            for option in ('--onefile', '--windowed', '--noconfirm'):
                self.assertIn(option, command)
            self.assertTrue(command[-1].endswith('desktop_app.py'))
            self.assertIn('--exclude-module=tkinter', command)

    def test_connect_page_loads_nothing_from_outside_and_shows_found_text_safely(self):
        page = (DESKTOP / 'connect.html').read_text(encoding='utf-8')
        self.assertNotRegex(page, r'<script[^>]+src=|<link[^>]+href=|@import|url\(')
        self.assertNotIn('innerHTML', page)   # names and addresses from the network are set as text
        for element in ('id="address"', 'id="connect"', 'id="find"', 'id="back"', 'id="found"', 'id="status"'):
            self.assertIn(element, page)

    def test_requirements_are_pinned(self):
        for name in ('requirements.txt', 'requirements-build.txt'):
            for line in (DESKTOP / name).read_text().splitlines():
                line = line.split('#')[0].strip()
                if line and not line.startswith('-r'):
                    self.assertRegex(line, r'^[A-Za-z0-9_.-]+==[0-9][^ ;]*( *;.*)?$', line)


class FakeWindow:
    def __init__(self):
        self.url, self.cookies, self.calls = None, [], []

    def get_current_url(self):
        return self.url

    def get_cookies(self):
        return self.cookies

    def hide(self):
        self.calls.append('hide')

    def show(self):
        self.calls.append('show')

    def restore(self):
        self.calls.append('restore')


@needs_desktop
class WindowAndTrayTests(unittest.TestCase):
    """desktop_app.py's glue between the window, the tray and the Pi, with stand-ins for pywebview and pystray."""

    def setUp(self):
        fakes = {'webview': types.ModuleType('webview'), 'pystray': types.ModuleType('pystray')}
        fakes['pystray'].Menu = MagicMock()
        fakes['pystray'].MenuItem = MagicMock()
        patcher = patch.dict(sys.modules, fakes)
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop('desktop_app', None)
        self.module = importlib.import_module('desktop_app')
        self.addCleanup(sys.modules.pop, 'desktop_app', None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = desktop_state.Settings(Path(self.tmp.name) / 'desktop.json')
        self.app = self.module.DesktopApp(self.settings, desktop_state.Startup(None))
        self.app.window = FakeWindow()
        self.app.tray = MagicMock(HAS_NOTIFICATION=True)
        self.app.tray_ok = True

    def cookie(self, value, domain='192.168.1.50'):
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        cookie['pm_session'] = value
        cookie['pm_session']['domain'] = domain
        return cookie

    def test_the_connect_page_functions_refuse_the_dashboard(self):
        self.app.window.url = 'http://192.168.1.50:8080/'
        for call in (self.app.start, self.app.find, lambda: self.app.connect('192.168.1.50')):
            with self.assertRaises(PermissionError):
                call()
        self.app.window.url = 'about:blank'
        self.assertEqual(self.app.start()['version'], self.module.VERSION)

    def test_connecting_saves_the_address_once_it_answers(self):
        self.app.window.url = 'about:blank'
        with patch.object(pi_client, 'check', side_effect=pi_client.PiError('Nothing answered')):
            self.assertEqual(self.app.connect('192.168.1.50'), dict(ok=False, message='Nothing answered'))
        self.assertEqual(self.settings['address'], '')
        self.app.signed_in = True
        with patch.object(pi_client, 'check', return_value=''):
            self.assertEqual(self.app.connect('192.168.1.50'), dict(ok=True, url='http://192.168.1.50:8080/'))
        self.assertEqual(self.settings['address'], 'http://192.168.1.50:8080')
        self.assertIsNone(self.app.signed_in)   # a new Pi: not "signed out" of it
        self.assertTrue(self.app.wake.is_set())

    def test_start_connects_straight_away_only_when_the_app_starts(self):
        self.app.window.url = 'about:blank'
        self.settings.update(address='http://192.168.1.50:8080')
        self.assertEqual({k: v for k, v in self.app.start().items() if k != 'version'},
                         dict(address='http://192.168.1.50:8080', auto=True, back=False))
        self.assertEqual(self.app.start()['auto'], False)   # Change Pi address shows the form instead
        self.assertEqual(self.app.start()['back'], True)

    def test_closing_hides_to_the_tray_and_says_so_once(self):
        self.assertFalse(self.app.on_closing())
        self.assertFalse(self.app.on_closing())
        time.sleep(0.1)
        self.assertEqual(self.app.window.calls, ['hide', 'hide'])
        self.assertEqual(self.app.tray.notify.call_count, 1)
        self.app.tray_ok = False   # without a tray there would be no way back
        self.assertTrue(self.app.on_closing())
        self.app.tray_ok, self.app.quitting = True, True
        self.assertTrue(self.app.on_closing())

    def test_reading_the_window_sign_in(self):
        base = 'http://192.168.1.50:8080'
        self.app.window.url = base + '/'
        self.app.window.cookies = [self.cookie('other', '10.0.0.9'), self.cookie('s3cret')]
        self.assertEqual(self.app.window_session(base), 's3cret')
        self.app.window.url, self.app.window.cookies = 'about:blank', []   # the app's own page: keep the last one
        self.assertEqual(self.app.window_session(base), 's3cret')
        self.app.window.url = base + '/'   # the dashboard shows and has no sign-in: signed out
        self.assertEqual(self.app.window_session(base), '')

    def test_polling(self):
        base = 'http://192.168.1.50:8080'
        self.settings.update(address=base)
        self.app.window.url = base + '/'
        self.app.window.cookies = [self.cookie('s3cret')]
        printers = [dict(name='A1', display_name='Bench A1', display_state='Ready')]
        with patch.object(pi_client, 'summary', return_value=summary([note(1)], printers=printers)) as get:
            self.assertEqual(self.app.poll(), self.module.POLL_EVERY)
        get.assert_called_with(base, 's3cret')
        self.app.tray.notify.assert_not_called()   # nothing replayed the first time
        self.assertEqual(self.settings['seen'][base]['seen'], 1)
        with patch.object(pi_client, 'summary', return_value=summary([note(2), note(1)], printers=printers)):
            self.app.poll()
        self.app.tray.notify.assert_called_once_with('Bench A1 · cube.3mf', '✅ Print complete')
        self.app.tray.update_menu.assert_called()
        self.assertIn('1 ready', self.app.tray.title)

        # Signed out (the Pi restarted, or 12 hours passed): said once, then the tray says why.
        with patch.object(pi_client, 'summary', side_effect=pi_client.SignedOut('x')), patch.object(pi_client, 'check'):
            self.assertEqual(self.app.poll(), self.module.NOT_SIGNED_IN_EVERY)
            self.app.poll()
        self.assertEqual([c.args[1] for c in self.app.tray.notify.call_args_list][1:], ['Signed out of the dashboard'])
        self.assertEqual(self.app.problem, 'Not signed in: open the app to sign in')
        with patch.object(pi_client, 'summary', side_effect=pi_client.SignedOut('x')), \
                patch.object(pi_client, 'check', side_effect=pi_client.PiError('down')):
            self.assertEqual(self.app.poll(), self.module.UNREACHABLE_EVERY)
        self.assertEqual(self.app.problem, "Can't reach the Pi")
        with patch.object(pi_client, 'summary', side_effect=pi_client.TooOld('Update the Pi to version 1.7.8')):
            self.app.poll()
        self.assertEqual(self.app.problem, 'Update the Pi to version 1.7.8')

    def test_no_pi_yet(self):
        self.assertEqual(self.app.poll(), self.module.NOT_SIGNED_IN_EVERY)
        self.assertEqual(self.app.problem, 'Not connected to a Pi yet')

    def test_pop_ups_only_where_the_tray_can_show_them(self):
        self.app.tray.HAS_NOTIFICATION = False
        with self.assertLogs('desktop', 'INFO'):
            self.app.notify('Title', 'Message')
        self.app.tray.notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
