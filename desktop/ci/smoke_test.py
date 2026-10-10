"""End-to-end test of the desktop app, as someone would use it, against a demo Pi on this computer:

1. The app starts with a fresh profile and shows its Connect page; entering the Pi's address opens the dashboard's
   sign-in, and signing in shows the two demo printers.
2. The tray icon sees both printers, and a notification from the Pi pops up.
3. (Windows) Closing the window keeps the app running in the tray; starting the app again brings the window back.
4. The app quits with --quit. Started again in the tray (--tray, as Start with Windows does), it's still signed in.

The app is driven through its web view's debugging port. Screenshots and the app's log go to --out.

    python desktop/ci/smoke_test.py dist/3d-printer-management-desktop.exe --out desktop-test
    python desktop/ci/smoke_test.py "python desktop/desktop_app.py" --out desktop-test      (from source)
"""
import argparse
import asyncio
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import desktop_state   # noqa: E402

PASSWORD = 'demo-password-123'
TITLE = desktop_state.APP_NAME
WINDOWS = sys.platform == 'win32'


def step(text):
    print(f'\n== {text}', flush=True)


class Page:
    """Just enough of the Chrome DevTools Protocol to drive the app's web view: WebView2 on Windows, or Qt WebEngine
    when run from source on Linux."""

    def __init__(self, http, port):
        self.http, self.port, self.socket, self.count = http, port, None, 0

    async def attach(self):
        async with self.http.get(f'http://127.0.0.1:{self.port}/json/list') as response:
            targets = await response.json(content_type=None)
        target = next(t for t in targets if t.get('type') == 'page')
        self.socket = await self.http.ws_connect(target['webSocketDebuggerUrl'], max_msg_size=0)

    async def call(self, method, timeout=30, **params):
        self.count += 1
        await self.socket.send_json(dict(id=self.count, method=method, params=params))
        return await asyncio.wait_for(self.reply(method, self.count), timeout)

    async def reply(self, method, number):
        async for message in self.socket:
            data = json.loads(message.data)
            if data.get('id') == number:
                if 'error' in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data['result']
        raise ConnectionError('The web view closed the debugging connection.')

    async def js(self, expression):
        result = await self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if 'exceptionDetails' in result:
            raise RuntimeError(result['exceptionDetails'].get('text', 'JavaScript error'))
        return result['result'].get('value')

    async def until(self, expression, what, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if await self.js(expression):
                    return
            except (RuntimeError, asyncio.TimeoutError):   # between two pages while navigating
                pass
            await asyncio.sleep(0.5)
        raise AssertionError(f'Timed out after {timeout} s waiting for {what} (page: {await self.js("location.href")})')

    async def screenshot(self, path):
        Path(path).write_bytes(base64.b64decode((await self.call('Page.captureScreenshot', format='png'))['data']))

    async def close(self):
        if self.socket:
            await self.socket.close()


def shown(selector):
    return f"(() => {{ const e = document.querySelector({json.dumps(selector)}); return !!e && e.offsetParent !== null; }})()"


def type_into(selector, text):
    return (f"(() => {{ const e = document.querySelector({json.dumps(selector)}); e.value = {json.dumps(text)}; "
            "e.dispatchEvent(new Event('input', {bubbles: true})); return true; })()")


class Test:
    def __init__(self, app, out, port, debug_port):
        self.app, self.out, self.port, self.debug_port = app, Path(out), port, debug_port
        self.out.mkdir(parents=True, exist_ok=True)
        self.profile = Path(tempfile.mkdtemp(prefix='desktop-profile-'))
        self.env = dict(os.environ, PM_DESKTOP_DEBUG_PORT=str(debug_port), APPDATA=str(self.profile / 'Roaming'),
                        LOCALAPPDATA=str(self.profile / 'Local'), XDG_CONFIG_HOME=str(self.profile / 'config'),
                        XDG_DATA_HOME=str(self.profile / 'data'))
        self.settings_folder, self.data_folder = desktop_state.folders(self.env)
        self.log = self.data_folder / 'desktop.log'
        self.pi = self.process = self.http = None
        self.shots = 0

    # ---- helpers ----

    def launch(self, *args):
        command = [os.path.abspath(self.app)] if os.path.isfile(self.app) else shlex.split(self.app, posix=not WINDOWS)
        return subprocess.Popen(command + list(args), env=self.env)

    def log_text(self):
        try:
            return self.log.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''

    async def wait(self, condition, what, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.process and self.process.poll() is not None:
                raise AssertionError(f'The app stopped (exit code {self.process.returncode}) while waiting for {what}:\n'
                                     + self.log_text()[-3000:])
            result = condition()
            if result:
                return result
            await asyncio.sleep(0.5)
        raise AssertionError(f'Timed out after {timeout} s waiting for {what}')

    async def wait_log(self, text, timeout=60, after=0):
        """Wait for a line in the app's log, written after the first `after` characters."""
        await self.wait(lambda: text in self.log_text()[after:], f'"{text}" in the app log', timeout)
        print('   log:', text, flush=True)

    async def screenshot(self, name, page=None):
        self.shots += 1
        path = self.out / f'{self.shots:02d}-{name}.png'
        try:
            if page is not None:
                await page.screenshot(path)
            elif WINDOWS:
                from PIL import ImageGrab
                ImageGrab.grab().save(path)
            else:
                return
            print('   screenshot:', path.name, flush=True)
        except Exception as exc:   # a screenshot is only a record; it never fails the test
            print(f'   (no screenshot: {exc})', flush=True)

    async def page(self):
        async def debugging():
            try:
                async with self.http.get(f'http://127.0.0.1:{self.debug_port}/json/list') as response:
                    return any(t.get('type') == 'page' for t in await response.json(content_type=None))
            except aiohttp.ClientError:
                return False
        end = time.monotonic() + 90
        while not await debugging():
            if self.process.poll() is not None or time.monotonic() > end:
                raise AssertionError(f'The app\'s web view did not open (exit code {self.process.poll()}):\n'
                                     + self.log_text()[-3000:])
            await asyncio.sleep(1)
        page = Page(self.http, self.debug_port)
        await page.attach()
        return page

    def window(self):
        import ctypes
        return ctypes.windll.user32.FindWindowW(None, TITLE)

    def visible(self):
        import ctypes
        handle = self.window()
        return bool(handle and ctypes.windll.user32.IsWindowVisible(handle))

    # ---- the test ----

    async def start_pi(self):
        step('Starting a demo Pi')
        self.pi = subprocess.Popen([sys.executable, str(HERE / 'demo_pi.py'), '--port', str(self.port), '--password', PASSWORD])
        end = time.monotonic() + 90
        while time.monotonic() < end:
            if self.pi.poll() is not None:
                raise AssertionError(f'The demo Pi stopped (exit code {self.pi.returncode}); its error is above.')
            try:
                async with self.http.get(f'http://127.0.0.1:{self.port}/health') as response:
                    if (await response.json()).get('application') == '3d-printer-management':
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.5)
        raise AssertionError('The demo Pi did not start.')

    async def pi_notification(self, title):
        async with self.http.post(f'http://127.0.0.1:{self.port}/demo/notify', headers={'X-PM': '1'},
                                  json=dict(printer='Demo A1', title=title, detail='cube.3mf')) as response:
            assert response.status == 200, await response.text()

    async def first_run(self):
        step('First start: connect to the Pi and sign in')
        self.process = self.launch()
        page = await self.page()
        await page.until(shown('#address'), 'the Connect page')
        await self.screenshot('connect-page', page)
        if self.port == 8080:   # Find it for me looks for dashboards on the usual port
            await page.js("document.querySelector('#find').click()")
            found = "[...document.querySelectorAll('#found button')].find(b => b.textContent.includes('http://127.0.0.1:8080'))"
            await page.until(f'!!{found}', 'Find it for me to find the demo Pi', 90)
            await self.screenshot('found', page)
            await page.js(found + '.click()')
        else:
            await page.js(type_into('#address', f'127.0.0.1:{self.port}'))
            await page.js("document.querySelector('#connect').click()")
        await page.until(f"location.href === 'http://127.0.0.1:{self.port}/'", 'the dashboard to open')
        await page.until(shown('#loginPassword'), "the dashboard's sign-in")
        await self.screenshot('sign-in', page)
        await page.js(type_into('#loginPassword', PASSWORD))
        await page.js("document.querySelector('#loginForm').requestSubmit()")
        await page.until(shown('#app') + " && document.querySelectorAll('.printer-card').length >= 2",
                         'the dashboard with the two demo printers')
        await self.screenshot('dashboard', page)
        saved = json.loads((self.settings_folder / 'desktop.json').read_text(encoding='utf-8'))
        assert saved['address'] == f'http://127.0.0.1:{self.port}', saved

        step('The tray sees the printers and pops up a notification')
        await self.wait_log('Tray: 2 printer(s)', 90)
        await self.pi_notification('✅ Print complete')
        await self.wait_log('Notification: ✅ Print complete', 60)

        if WINDOWS:
            step('Closing the window keeps the app in the tray; starting it again shows the window')
            import ctypes
            await self.wait(self.visible, 'the app window', 30)
            await self.screenshot('desktop')
            ctypes.windll.user32.PostMessageW(self.window(), 0x0010, 0, 0)   # WM_CLOSE, like the X button
            await self.wait(lambda: not self.visible(), 'the window to hide', 30)
            await self.wait_log(f'Notification: {TITLE} is still running', 30)
            second = self.launch()
            assert await asyncio.to_thread(second.wait, 30) == 0, 'the second copy should hand over to the first and exit'
            await self.wait(self.visible, 'the window to come back', 30)
            await self.screenshot('window-back')
        else:
            # Qt WebEngine (Linux) saves cookies to disk every 30 seconds, not when the app quits.
            await asyncio.sleep(35)
        await page.close()

        step('--quit stops the app')
        assert await asyncio.to_thread(self.launch('--quit').wait, 30) == 0
        await asyncio.to_thread(self.process.wait, 60)

    async def second_run(self):
        step('Started again in the tray (as Start with Windows does): still signed in')
        before = len(self.log_text())
        self.process = self.launch('--tray')
        page = await self.page()
        await page.until(f"location.href === 'http://127.0.0.1:{self.port}/'", 'the dashboard to open by itself', 90)
        await page.until(shown('#app'), 'the dashboard, signed in')
        assert not await page.js(shown('#loginScreen')), 'the sign-in should have been kept'
        if WINDOWS:
            assert not self.visible(), 'with --tray the window starts hidden'
            # (Qt WebEngine, on Linux, doesn't tell the tray about a sign-in saved by an earlier run.)
            await self.wait_log('Tray: 2 printer(s)', 90, after=before)
        step('Starting the app again opens its window')
        assert await asyncio.to_thread(self.launch().wait, 30) == 0
        if WINDOWS:
            await self.wait(self.visible, 'the window to open', 30)
        await asyncio.sleep(2)
        await self.screenshot('still-signed-in', page)
        await page.close()
        assert await asyncio.to_thread(self.launch('--quit').wait, 30) == 0
        await asyncio.to_thread(self.process.wait, 60)

    async def run(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as self.http:
                await self.start_pi()
                await self.first_run()
                await self.second_run()
            errors = [line for line in self.log_text().splitlines() if ' ERROR ' in line or 'Traceback' in line]
            assert not errors, 'Errors in the app log:\n' + '\n'.join(errors)
            step('Passed')
        finally:
            for process in (self.process, self.pi):
                if process and process.poll() is None:
                    process.kill()
            if self.log.exists():
                shutil.copy(self.log, self.out / 'desktop.log')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('app', help='the .exe, or a command such as "python desktop/desktop_app.py"')
    parser.add_argument('--out', default='desktop-test')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--debug-port', type=int, default=9222)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(Test(args.app, args.out, args.port, args.debug_port).run())


if __name__ == '__main__':
    main()
