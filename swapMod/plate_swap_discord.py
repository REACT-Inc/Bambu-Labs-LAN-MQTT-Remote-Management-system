import discord
from discord import app_commands
from swapMod.plate_swap import NOT_A_SERIES


def install(core, engine):
    """/plateswap set (on/off) and /plateswap now (run the swap print). The swap print file is chosen in the dashboard."""
    group = app_commands.Group(name='plateswap', description='Swapmod: swap the plate with its swap print (A1 / A1 mini)')

    async def printer(i, name):
        exact, _ = core.resolve_name(name)
        if not exact:
            await i.followup.send('Choose a printer from autocomplete or enter its full management name.', ephemeral=core.ephemeral(i))
            return None
        if not engine.plate_swap.available(exact):
            await i.followup.send(NOT_A_SERIES, ephemeral=core.ephemeral(i))
            return None
        return exact

    @group.command(name='set', description='Turn Swapmod on or off for a printer')
    @app_commands.guild_only()
    async def set_(i: discord.Interaction, name: str, enabled: bool):
        if not await core.require(i, 'plateswap'):
            return
        await i.response.defer(ephemeral=core.ephemeral(i))
        exact = await printer(i, name)
        if not exact:
            return
        try:
            state = engine.plate_swap.configure(exact, enabled, core.who(i))
            text = f"Swapmod {'on' if enabled else 'off'} for {core.safe(core.display_name(exact))}." + (
                '' if state['file'] or not enabled else ' Choose its swap print file in the dashboard (printer panel → Swapmod).')
        except ValueError as exc:
            text = str(exc)
        await i.followup.send(text, ephemeral=core.ephemeral(i))

    @group.command(name='now', description='Swap the plate now by running the swap print')
    @app_commands.guild_only()
    async def now(i: discord.Interaction, name: str):
        if not await core.require(i, 'plateswap'):
            return
        await i.response.defer(ephemeral=core.ephemeral(i))
        exact = await printer(i, name)
        if not exact:
            return
        try:
            await engine.plate_swap.swap_now(engine, exact, core.who(i))
            text = f'Swapping the plate on {core.safe(core.display_name(exact))}.'
        except ValueError as exc:
            text = str(exc)
        await i.followup.send(text, ephemeral=core.ephemeral(i))

    async def autocomplete(i, current):
        # Only A-series printers (A1 / A1 mini) can use Swapmod (#17).
        return [app_commands.Choice(name=core.display_name(n)[:100], value=core.display_name(n)) for n in core.names()
                if engine.plate_swap.available(n) and current.casefold() in (n + ' ' + core.display_name(n)).casefold()][:25]
    set_.autocomplete('name')(autocomplete)
    now.autocomplete('name')(autocomplete)
    core.bot.tree.add_command(group)
