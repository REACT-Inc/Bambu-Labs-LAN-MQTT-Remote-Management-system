"""/printplan: change a printer's speed, temperatures or fan when its print reaches a percentage or a layer.

/printplan add name when change to   e.g. when: 10%  change: Speed  to: 277%   (and when: layer 60, to: normal)
/printplan show name · /printplan remove name step · /printplan clear name

Each change also needs the permission of its own command (/speed, /temperature, /fan, /chamber), so a plan can't do
what its maker couldn't do by hand. See print_plans.py.
"""
import discord
from discord import app_commands

from print_plans import ACTIONS, PERMISSION, when

ICONS = {'waiting': '⏳', 'done': '✅', 'skipped': '⏭️', 'failed': '⚠️'}


def describe(core, printer, plan):
    name = core.safe(core.display_name(printer))
    if not plan:
        return f'{name} has no print plan.\nAdd a step with `/printplan add`.'
    head = ('For the print now running' + (f" (at {plan['progress']['percent']} %, layer {plan['progress']['layer']})" if plan.get('progress') else '')
            if plan['current'] else 'For the next print this printer starts')
    lines = [f"{ICONS[s['state']]} `{s['id']}` · at **{when(s)}**: {core.safe(s['label'])}" + (f" ({core.safe(s['note'])})" if s['note'] and s['state'] != 'done' else '')
             for s in plan['steps']]
    return f'**{name}** · {head}\n' + '\n'.join(lines)


def install(core, plans):
    group = app_commands.Group(name='printplan', description='Change speed, temperatures or the fan when a print reaches a percentage or layer')

    async def printer(i, name):
        exact, suggestion = core.resolve_name(name)
        if not exact:
            hint = f' Did you mean {core.safe(core.display_name(suggestion))}?' if suggestion else ''
            await core.respond(i, core.card('Choose a printer', 'Pick the printer from the list (start typing its name).' + hint, core.RED))
        return exact

    async def printer_options(i: discord.Interaction, current: str):
        return [app_commands.Choice(name=core.display_name(n)[:100], value=core.display_name(n)[:100]) for n in core.names()
                if current.casefold() in (n + ' ' + core.display_name(n)).casefold()][:25]

    async def value_options(i: discord.Interaction, current: str):
        change = getattr(i.namespace, 'change', None)
        options = {'speed': ['normal', 'silent', 'sport', 'ludicrous', '150%', '200%', '277%'],
                   'fan': ['0', '50', '80', '100'], 'nozzle': ['220', '240', '260'], 'bed': ['55', '65', '80'], 'chamber': ['0', '45', '60']}.get(change, [])
        return [app_commands.Choice(name=o, value=o) for o in options if current.lower() in o][:25]

    @group.command(name='add', description='At a percentage or layer of the print, change the speed, a temperature or the fan')
    @app_commands.guild_only()
    @app_commands.describe(name='The printer', when='A percentage like 10% or a layer like "layer 60"', change='What to change',
                           to='Speed: normal, silent, sport, ludicrous or a percentage like 277%. Temperatures in °C, fan 0–100 %.')
    @app_commands.choices(change=[app_commands.Choice(name=label, value=key) for key, label in ACTIONS.items()])
    @app_commands.autocomplete(name=printer_options, to=value_options)
    async def add(i: discord.Interaction, name: str, when: str, change: str, to: str):
        if not await core.require(i, 'printplan') or not await core.require(i, PERMISSION[change]):
            return
        exact = await printer(i, name)
        if not exact:
            return
        try:
            _, step = plans.add(exact, when, change, to, core.who(i))
        except ValueError as exc:
            await core.respond(i, core.card('Step not added', str(exc), core.RED))
            return
        note = (f"\n⏭️ The print is already past {when_text(step)}, so this won't run. Use `/speed` (or `/temperature`, `/fan`) to change it now."
                if step['state'] == 'skipped' else '')
        if 'M220' in step['label']:
            note += '\nA custom percentage is sent as the G-code speed override (M220); the printer\'s firmware decides how fast it really goes.'
        await core.respond(i, core.card('Print plan step added', describe(core, exact, plans.public(exact)) + note, core.GREEN))

    def when_text(step):
        return when(step)

    @group.command(name='show', description='Show a printer\'s print plan')
    @app_commands.guild_only()
    @app_commands.autocomplete(name=printer_options)
    async def show(i: discord.Interaction, name: str):
        exact = await printer(i, name)
        if exact:
            await core.respond(i, core.card('Print plan', describe(core, exact, plans.public(exact))))

    @group.command(name='remove', description='Remove one step from a printer\'s print plan')
    @app_commands.guild_only()
    @app_commands.describe(step='The step\'s code, shown by /printplan show')
    @app_commands.autocomplete(name=printer_options)
    async def remove(i: discord.Interaction, name: str, step: str):
        if not await core.require(i, 'printplan'):
            return
        exact = await printer(i, name)
        if not exact:
            return
        try:
            plans.remove(exact, step.strip().strip('`'), core.who(i))
        except ValueError as exc:
            await core.respond(i, core.card('Step not removed', str(exc), core.RED))
            return
        await core.respond(i, core.card('Print plan', describe(core, exact, plans.public(exact))))

    @group.command(name='clear', description='Remove a printer\'s whole print plan')
    @app_commands.guild_only()
    @app_commands.autocomplete(name=printer_options)
    async def clear(i: discord.Interaction, name: str):
        if not await core.require(i, 'printplan'):
            return
        exact = await printer(i, name)
        if not exact:
            return
        had = plans.clear(exact, core.who(i))
        await core.respond(i, core.card('Print plan', f"{core.safe(core.display_name(exact))}: {'plan cleared' if had else 'there was no plan'}."))

    core.bot.tree.add_command(group)
