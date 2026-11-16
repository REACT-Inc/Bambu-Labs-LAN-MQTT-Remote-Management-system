import asyncio
import contextlib
import faulthandler
import os
import signal
import threading
from aiohttp import web # pyright: ignore[reportMissingImports]
import core
import diagnostics
import issue_reports
from queueing import Engine, Store
from dashboard import Dashboard
from discord_Intergration.discord_queue import install
from discord_Intergration.extra_discord import install as install_extras
from discord_Intergration.controls_discord import install as install_controls
from discord_Intergration.clear_errors_discord import install as install_clear_errors
from swapMod.plate_swap_discord import install as install_plate_swap
from team import Team
from discord_Intergration.team_discord import install as install_team
from discord_Intergration.member_watch import install as install_member_watch
from loop_watchdog import LoopWatchdog
import printer_setup
from tailscale_address import Listener


def exit_soon(seconds=15):
    """End the process after seconds even if shutdown is stuck (asyncio.run waits for every task to finish)."""
    timer=threading.Timer(seconds,os._exit,(1,))
    timer.daemon=True
    timer.start()


async def start_site(runner,host,port):
    site=web.TCPSite(runner,host,port)
    await site.start()
    return site


async def main():
    diagnostics.setup(core.DATA_DIR,core.log)
    # pm-doctor (#43) sends SIGUSR1 when the app stops answering: every thread's stack goes to the journal.
    faulthandler.register(signal.SIGUSR1,all_threads=True)
    diagnostics.install_loop_handler(asyncio.get_running_loop(),core.log)
    store=Store(core.DATA_DIR/'management.sqlite3')
    engine=Engine(core,store)
    dashboard=Dashboard(core,store,engine)
    core.SETTINGS_USER_IDS=set(core.settings.get('admin_user_ids',core.SETTINGS_USER_IDS))
    core.report_listener=engine.telemetry
    core.event_listener=store.event
    core.bot_loop=asyncio.get_running_loop()
    install(core,store,engine,dashboard)
    team=Team(core,store,dashboard)
    install_team(core,team)
    install_extras(core,store)
    install_controls(core,dashboard.controls)
    install_clear_errors(core,dashboard.alerts)
    install_plate_swap(core,engine)
    install_member_watch(core,dashboard.member_watch)   # Member updates by DM
    diagnostics.install_discord(core,store)
    issue_reports.install_discord(core,dashboard.issue_reports)
    # Printer monitoring and web UI do not depend on Discord being connected.
    if not core.EXAMPLE_MODE:
        for printer in core.PRINTERS:
            try:core.connect_printer(printer)
            except Exception:core.log.exception('Printer connection setup failed for %s',printer['name'])
    # Started before the web server, so a hang during start-up is caught (and the process restarted) too.
    watchdog=LoopWatchdog(core.log,stall_file=diagnostics.log_dir(core.DATA_DIR)/'stalls.log')
    watchdog.start()
    runner=web.AppRunner(dashboard.app,access_log=None)
    # The addresses in "listen": one not there yet is tried again every 30 s, and a Tailscale address that has
    # changed (the Pi signed in to Tailscale again, e.g. for ACL tags) is followed (tailscale_address.py).
    listener=Listener(lambda host,port:start_site(runner,host,port),core.CONFIG.get('listen',['127.0.0.1']),int(core.CONFIG.get('port',8080)),
                      core.log,event=store.event)
    dashboard.status.add_source(listener.status)
    try:
        await runner.setup()
        await listener.start()
    except Exception:
        # Log the reason now (not after shutdown), and make sure the process ends so systemd starts it again.
        core.log.exception('Start-up failed; exiting so systemd restarts the service')
        exit_soon()
        raise
    stopped=asyncio.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig,stopped.set)

    async def discord_task():
        if not core.DISCORD_BOT_TOKEN:
            core.log.warning('No Discord token configured: dashboard-only mode.');return
        watch=dashboard.member_watch
        watch.apply_intents(core.bot)   # Member updates need privileged intents: asked for only while they're on
        await core.run_discord(core.bot,core.DISCORD_BOT_TOKEN,refused=lambda:watch.drop_intents(core.bot))   # retries until connected; reconnects by itself after
    task=asyncio.create_task(discord_task())
    listening=asyncio.create_task(listener.watch())
    scheduler=asyncio.create_task(team.scheduler())
    try:
        await stopped.wait()
    finally:
        exit_soon(30)   # a background task that won't stop must never keep a stopped service alive
        listening.cancel()
        await watchdog.stop()
        scheduler.cancel()
        await asyncio.gather(scheduler,return_exceptions=True)
        for t in list(engine.tasks):t.cancel()
        await asyncio.gather(*engine.tasks,return_exceptions=True)
        await dashboard.member_watch.stop()
        await core.bot.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):await task
        for client in core.clients.values():
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
        await runner.cleanup()
        store.db.close()


def exit_code():
    """0 after a normal stop. After the dashboard's Restart now (printer or Discord settings changed), a non-zero
    code, so systemd's Restart=on-failure starts the app again (printer_setup.py)."""
    return printer_setup.RESTART_EXIT if core.restart_requested else 0


if __name__=='__main__':
    asyncio.run(main())
    raise SystemExit(exit_code())
