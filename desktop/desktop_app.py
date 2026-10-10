"""3D Printer Management for Windows laptops: the Pi's dashboard in its own window, and a tray icon that lists the
printers and pops up the dashboard's notifications (prints finished or failed, printer errors, AI alerts).

- **The window is the dashboard itself**, so everything works as in a browser: sign in with the dashboard password,
  then manage the printers, the queue and the settings. The sign-in is kept between starts (until the dashboard's
  12-hour session ends). No password is stored by this app.
- **Closing the window keeps the app running** in the notification area; Quit is in the tray icon's menu.
- **The tray** asks the Pi for a small summary every 15 seconds (/api/desktop) with the window's own sign-in, so it
  only works while you're signed in. It doesn't keep the Pi's cameras busy the way an open dashboard does.
- **Start with Windows** (tray menu) starts it in the tray when you sign in to Windows.

Run from source: pip install -r desktop/requirements.txt, then python desktop/desktop_app.py. desktop/build.py makes
the .exe. See docs/desktop-app.md.
"""
import argparse
import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
import webview

import desktop_state
import icon
import pi_client

try:
    from _version import VERSION   # written by build.py
except ImportError:
    VERSION = 'dev'

APP_NAME = desktop_state.APP_NAME
HERE = Path(getattr(sys, '_MEIPASS', '') or Path(__file__).resolve().parent)
DOCS = 'https://github.com/REACT-Inc/Bambu-Labs-LAN-MQTT-Remote-Management-system/blob/main/docs/desktop-app.md'
WEBVIEW2 = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
POLL_EVERY, NOT_SIGNED_IN_EVERY, UNREACHABLE_EVERY = 15, 5, 20
log = logging.getLogger('desktop')


class DesktopApp:
    def __init__(self, settings, startup, start_hidden=False):
        self.settings, self.startup, self.start_hidden = settings, startup, start_hidden
        self.window = self.tray = None
        self.tray_ok = self.quitting = self.minimized = False
        self.stop, self.wake = threading.Event(), threading.Event()
        self.auto_connect = True   # connect straight away when the app starts; not after "Change Pi address"
        self.summary, self.problem, self.level, self.headline, self.menu = None, 'Starting…', None, '', None
        self.signed_in = None   # unknown until the first look; then True/False
        self.session = ''   # the window's sign-in, kept while its own page (not the dashboard) is showing

    # ---- the window ----

    def create_window(self):
        self.window = webview.create_window(
            APP_NAME, html=self.connect_page(), width=1280, height=820, min_size=(760, 540), hidden=self.start_hidden,
            text_select=True, background_color='#101413')
        # Exposed functions answer only the app's own page (own_page): the dashboard can't call them.
        self.window.expose(self.start, self.connect, self.find)
        self.window.events.closing += self.on_closing
        self.window.events.minimized += lambda: setattr(self, 'minimized', True)
        self.window.events.restored += lambda: setattr(self, 'minimized', False)
        self.window.events.maximized += lambda: setattr(self, 'minimized', False)

    @staticmethod
    def connect_page():
        return (HERE / 'connect.html').read_text(encoding='utf-8')

    def own_page(self):
        url = str(self.window.get_current_url() or '')
        if url.lower().startswith(('http:', 'https:')):
            raise PermissionError('Only the desktop app\'s own page can do this.')

    def start(self):
        """What the connect page shows: the saved address, and whether to connect straight away."""
        self.own_page()
        automatic, self.auto_connect = self.auto_connect, False
        address = self.settings['address']
        return dict(address=address, auto=automatic and bool(address), back=bool(address) and not automatic, version=VERSION)

    def connect(self, address):
        """Check that the dashboard answers at this address, then remember it; the page then opens it."""
        self.own_page()
        try:
            base = pi_client.normalise(address)
            pi_client.check(base)
        except pi_client.PiError as exc:
            return dict(ok=False, message=str(exc))
        if base != self.settings['address']:
            self.settings.update(address=base)
            self.session, self.signed_in = '', None   # another Pi: not "signed out" of it
            log.info('Dashboard address set to %s', base)
        self.wake.set()   # the tray looks straight away
        return dict(ok=True, url=base + '/')

    def find(self):
        self.own_page()
        return dict(found=pi_client.find())

    def show(self, *_):
        self.window.show()
        if self.minimized:
            self.window.restore()

    def change_address(self, *_):
        self.auto_connect = False
        self.window.load_html(self.connect_page())
        self.show()

    def reload(self, *_):
        address = self.settings['address']
        if address:
            self.window.load_url(address + '/')
        else:
            self.window.load_html(self.connect_page())
        self.show()

    def on_closing(self):
        """Closing the window keeps the app in the tray (unless there's no tray to come back from)."""
        if self.quitting or not self.tray_ok:
            return True
        threading.Thread(target=self.window.hide, daemon=True).start()
        if not self.settings['told_tray']:
            self.settings.update(told_tray=True)
            self.notify(f'{APP_NAME} is still running',
                        'It keeps an eye on the printers from the notification area. Right-click its icon to quit.')
        return False

    def quit(self, *_):
        self.quitting = True
        self.stop.set()
        self.wake.set()
        if self.tray_ok:
            self.tray.stop()
        self.window.destroy()

    def command(self, command):
        """From another copy of the app (desktop_state.SingleInstance): 'show' when it's started again, or 'quit'."""
        if command == 'show':
            self.show()
        elif command == 'quit':
            self.quit()

    # ---- the tray icon ----

    def create_tray(self):
        try:
            self.tray = pystray.Icon('3d-printer-management', icon.draw(64, 'offline'), APP_NAME,
                                     menu=pystray.Menu(self.menu_items))
            # Its own (daemon) thread rather than run_detached(), whose threads can outlive Quit on some systems.
            threading.Thread(target=self.tray.run, daemon=True, name='tray').start()
            for _ in range(50):   # it shows within moments where there is a notification area
                if self.tray.visible:
                    break
                time.sleep(0.1)
            self.tray_ok, self.level = self.tray.visible, 'offline'
        except Exception:
            log.exception('The tray icon could not be shown')
        if not self.tray_ok:
            log.warning('No tray icon: closing the window quits the app')

    def menu_items(self):
        def item(text, action=None, **options):
            return pystray.MenuItem(desktop_state.menu_text(text), action, **options)

        def choose(choice):
            def chosen(*_):
                self.settings.update(notify=choice)
            return chosen

        lines = desktop_state.tray_lines(self.summary or {}, self.problem)
        return [item(f'Open {APP_NAME}', self.show, default=True), pystray.Menu.SEPARATOR,
                *[item(line, self.show) for line in lines], pystray.Menu.SEPARATOR,
                item('Notifications', pystray.Menu(*[
                    item(label, choose(choice), checked=lambda _, choice=choice: self.settings['notify'] == choice, radio=True)
                    for choice, label in desktop_state.NOTIFY])),
                item('Start with Windows', self.toggle_startup, checked=lambda _: self.startup.enabled(),
                     visible=self.startup.available()),
                item('Change Pi address…', self.change_address),
                item('Reload the dashboard', self.reload),
                item('Help', lambda *_: webbrowser.open(DOCS)),
                pystray.Menu.SEPARATOR,
                item(f'Desktop app {VERSION}' + (f" · Pi {self.summary['version']}" if (self.summary or {}).get('version') else ''),
                     None, enabled=False),
                item('Quit', self.quit)]

    def toggle_startup(self, *_):
        try:
            self.startup.set(not self.startup.enabled())
        except OSError:
            log.exception('Could not change Start with Windows')

    def notify(self, title, message):
        log.info('Notification: %s', title)
        if self.tray_ok and self.tray.HAS_NOTIFICATION:   # Windows; not every Linux tray can show pop-ups
            try:
                self.tray.notify(desktop_state.fit(message, 256), desktop_state.fit(title, 64))
            except Exception:
                log.exception('The notification could not be shown')

    def show_state(self, summary, problem):
        headline = problem or f"{len((summary or {}).get('printers') or [])} printer(s)"
        if headline != self.headline:
            log.info('Tray: %s', headline)
        self.summary, self.problem, self.headline = summary, problem, headline
        if not self.tray_ok:
            return
        level = desktop_state.icon_level(summary or {}, problem)
        if level != self.level:
            self.tray.icon, self.level = icon.draw(64, level if level != 'ok' else None), level
        self.tray.title = desktop_state.tooltip(summary or {}, problem)
        menu = (desktop_state.tray_lines(summary or {}, problem), (summary or {}).get('version'))
        if menu != self.menu:   # only when it changed: rebuilding it can close the menu while it's open
            self.menu = menu
            self.tray.update_menu()

    # ---- keeping an eye on the Pi ----

    def window_session(self, base):
        """The dashboard's sign-in cookie from the window. While the app's own page shows (changing the address), the
        last one seen is used: the window then only has cookies for that page."""
        host = pi_client.host_of(base)
        try:
            for cookie in self.window.get_cookies() or []:
                for name, morsel in cookie.items():
                    if name == pi_client.SESSION_COOKIE and morsel['domain'].lstrip('.').strip('[]').lower() == host.strip('[]'):
                        self.session = morsel.value
                        return self.session
        except Exception:
            log.debug('Cookies not readable yet', exc_info=True)
        url = str(self.window.get_current_url() or '')
        if url.lower().startswith(('http:', 'https:')):
            self.session = ''   # the dashboard is showing, so there really is no sign-in
        return self.session

    def poll(self):
        """One look at the Pi; returns the seconds until the next one."""
        base = self.settings['address']
        if not base:
            self.show_state(None, 'Not connected to a Pi yet')
            return NOT_SIGNED_IN_EVERY
        try:
            summary = pi_client.summary(base, self.window_session(base))
        except pi_client.SignedOut:
            try:
                pi_client.check(base)
            except pi_client.PiError:
                self.show_state(None, "Can't reach the Pi")
                return UNREACHABLE_EVERY
            if self.signed_in:
                self.notify('Signed out of the dashboard',
                            'Open the app and sign in again to keep getting notifications (the dashboard restarted, '
                            'or 12 hours passed).')
            self.signed_in = False
            self.show_state(None, 'Not signed in: open the app to sign in')
            return NOT_SIGNED_IN_EVERY
        except pi_client.TooOld as exc:
            self.show_state(None, str(exc))
            return 60
        except pi_client.PiError:
            self.show_state(None, "Can't reach the Pi")
            return UNREACHABLE_EVERY
        self.signed_in = True
        notes, memory = desktop_state.new_notifications(summary, self.settings['seen'].get(base, {}))
        if memory != self.settings['seen'].get(base):
            self.settings.update(seen={**self.settings['seen'], base: memory})
        names = {p.get('name'): p.get('display_name') for p in summary.get('printers') or []}
        message = desktop_state.toast(notes, self.settings['notify'], names)
        if message:
            self.notify(*message)
        self.show_state(summary, None)
        return POLL_EVERY

    def poll_forever(self):
        delay = 3
        while not self.stop.is_set():
            self.wake.wait(delay)
            self.wake.clear()
            if self.stop.is_set():
                return
            try:
                delay = self.poll()
            except Exception:
                log.exception('Looking at the Pi failed')
                delay = UNREACHABLE_EVERY


# ---- start-up ----

def message_box(text, buttons=0):
    """A Windows message box (0x40: information icon); returns the button pressed (6 = Yes)."""
    if sys.platform != 'win32':
        print(text, file=sys.stderr)
        return 0
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40 | buttons)


def webview2_missing():
    """Windows without the Microsoft Edge WebView2 Runtime (rare: it comes with Windows 11 and updated Windows 10)."""
    if sys.platform != 'win32':
        return False
    from webview.platforms import winforms
    return not winforms.is_chromium


def exit_soon(code, seconds=10):
    """End the process even if a library's thread won't stop after Quit (Python waits for those threads). Normally
    the process ends first, after the web view has saved the sign-in."""
    timer = threading.Timer(seconds, os._exit, (code,))
    timer.daemon = True
    timer.start()


def setup_logging(folder):
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(folder / 'desktop.log', maxBytes=256_000, backupCount=1, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger('pywebview').setLevel(logging.WARNING)
    sys.excepthook = lambda *info: log.error('Unexpected error', exc_info=info)
    threading.excepthook = lambda args: log.error('Unexpected error in %s', args.thread, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='3d-printer-management-desktop', description=__doc__.split('\n')[0])
    parser.add_argument('--tray', action='store_true', help='start in the tray, without opening the window')
    parser.add_argument('--quit', action='store_true', help='quit the copy that is running')
    args = parser.parse_args(argv)
    settings_folder, data_folder = desktop_state.folders()
    setup_logging(data_folder)
    instance = desktop_state.SingleInstance(data_folder)
    running = instance.running()
    if running and sys.platform == 'win32':   # let the running copy bring its window to the front
        ctypes.windll.user32.AllowSetForegroundWindow(int(running.get('pid') or -1))
    if instance.signal('quit' if args.quit else 'show') or args.quit:
        return 0
    log.info('%s desktop app %s starting', APP_NAME, VERSION)
    if webview2_missing():
        if message_box(f'{APP_NAME} needs the Microsoft Edge WebView2 Runtime, a free part of Windows from Microsoft.\n\n'
                       'Open the download page now? Install it, then start this app again.', 0x4) == 6:
            webbrowser.open(WEBVIEW2)
        return 1
    startup = desktop_state.Startup(sys.executable if getattr(sys, 'frozen', False) else None)
    if startup.repair():
        log.info('Start with Windows now points at %s', sys.executable)
    app = DesktopApp(desktop_state.Settings(settings_folder / 'desktop.json'), startup, start_hidden=args.tray)
    app.create_window()
    instance.listen(app.command)
    app.create_tray()
    threading.Thread(target=app.poll_forever, daemon=True, name='tray-poll').start()
    webview.settings['ALLOW_DOWNLOADS'] = True   # diagnostic reports, AI training pictures
    if os.environ.get('PM_DESKTOP_DEBUG_PORT'):   # only for the automated test (desktop/ci/smoke_test.py)
        webview.settings['REMOTE_DEBUGGING_PORT'] = int(os.environ['PM_DESKTOP_DEBUG_PORT'])
    try:
        webview.start(private_mode=False, storage_path=str(data_folder / 'WebView2'))
    finally:
        app.stop.set()
        app.wake.set()
        if app.tray_ok:
            app.tray.stop()
        instance.close()
        log.info('Stopped')
    return 0


if __name__ == '__main__':
    try:
        code = main()
    except Exception as error:
        log.exception('The desktop app stopped')
        message_box(f'{APP_NAME} stopped because of an error:\n\n{error}\n\nThe details are in desktop.log in '
                    f'{desktop_state.folders()[1]}.')
        code = 1
    exit_soon(code)
    sys.exit(code)
