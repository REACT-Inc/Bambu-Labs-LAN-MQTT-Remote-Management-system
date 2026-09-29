import asyncio
import contextlib
import signal
from aiohttp import web # pyright: ignore[reportMissingImports]
import core
import diagnostics
import issue_reports
from queueing import Engine, Store
from dashboard import Dashboard
from discord_Intergration.discord_queue import install
from discord_Intergration.extra_discord import install as install_extras
from discord_Intergration.controls_discord import install as install_controls
from swapMod.plate_swap_discord import install as install_plate_swap
from ftcTeamManagement.team import Team
from ftcTeamManagement.team_discord import install as install_team
from discord_Intergration.server_discord import install as install_server
from backupDiscordBot import heartbeat


async def main():
    diagnostics.setup(core.DATA_DIR,core.log)
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
    install_server(core,team)
    install_extras(core,store)
    install_controls(core,dashboard.controls)
    install_plate_swap(core,engine)
    diagnostics.install_discord(core,store)
    issue_reports.install_discord(core,dashboard.issue_reports)
    # Printer monitoring and web UI do not depend on Discord being connected.
    if not core.EXAMPLE_MODE:
        for printer in core.PRINTERS:
            try:core.connect_printer(printer)
            except Exception:core.log.exception('Printer connection setup failed for %s',printer['name'])
    runner=web.AppRunner(dashboard.app,access_log=None)
    await runner.setup()
    for host in dict.fromkeys(core.CONFIG.get('listen',['127.0.0.1'])):
        await web.TCPSite(runner,host,int(core.CONFIG.get('port',8080))).start()
        core.log.info('3D Printer Management listening on %s:%s',host,core.CONFIG.get('port',8080))
    stopped=asyncio.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig,stopped.set)

    async def discord_task():
        if not core.DISCORD_BOT_TOKEN:
            core.log.warning('No Discord token configured: dashboard-only mode.');return
        try:
            await core.bot.start(core.DISCORD_BOT_TOKEN)
        except Exception:
            core.log.exception('Discord stopped. Dashboard remains available; correct token/access and restart the service.')
    task=asyncio.create_task(discord_task())
    scheduler=asyncio.create_task(team.scheduler())
    # Tells the backup Discord bot (backupDiscordBot/) that this service is alive and its bot is connected.
    beat=asyncio.create_task(heartbeat.run(core,core.DATA_DIR,dashboard.release))
    try:
        await stopped.wait()
    finally:
        scheduler.cancel();beat.cancel()
        await asyncio.gather(scheduler,beat,return_exceptions=True)
        for t in list(engine.tasks):t.cancel()
        await asyncio.gather(*engine.tasks,return_exceptions=True)
        await core.bot.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):await task
        for client in core.clients.values():
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
        await runner.cleanup()
        store.db.close()


if __name__=='__main__':
    asyncio.run(main())
