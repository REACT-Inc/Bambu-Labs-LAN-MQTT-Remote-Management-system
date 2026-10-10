"""A demo Pi for testing the desktop app: the real dashboard with its two demo printers, on this computer. No
printers, Discord or systemd needed, so it also runs on the Windows test machine.

    python desktop/ci/demo_pi.py --port 8080 --password demo-password-123

POST /demo/notify {"printer", "title", "detail"} makes it send a notification, as a finished print would.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


async def serve(core, dashboard, port):
    from aiohttp import web

    async def notify(request):
        data = await request.json()
        await core.notify(data.get('printer', 'Demo A1'), data.get('title', '✅ Print complete'), data.get('detail', ''), core.GREEN)
        return web.json_response({'ok': True})

    dashboard.app.router.add_post('/demo/notify', notify)
    runner = web.AppRunner(dashboard.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', port).start()
    print(f'Demo Pi on http://127.0.0.1:{port}', flush=True)
    await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--password', required=True)
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix='demo-pi-'))
    (folder / 'config.json').write_text(json.dumps(dict(demo=True, printers=[], discord_token='', listen=['127.0.0.1'],
                                                        port=args.port)))
    os.environ.update(PM_CONFIG=str(folder / 'config.json'), PM_DATA=str(folder / 'data'))
    sys.path.insert(0, str(ROOT))
    import core
    from dashboard import Dashboard, atomic_json, password_hash
    from queueing import Engine, Store
    store = Store(core.DATA_DIR / 'management.sqlite3')
    dashboard = Dashboard(core, store, Engine(core, store))
    core.event_listener = store.event
    atomic_json(core.DATA_DIR / 'auth.json', password_hash(args.password))
    asyncio.run(serve(core, dashboard, args.port))


if __name__ == '__main__':
    main()
