"""Discord: /clearerror, and a Clear error button on printer error notifications (#34). See printer_alerts.py."""
import discord
from discord import app_commands

ERROR_TITLES = ('🛑 Printer error', '⚠️ Printer health alert')


def install(core, alerts):
    def describe(printer, items):
        if not items:
            return 'The printer reports no error or health alert.'
        return '\n'.join(f"**{ {'error': 'Error ' + a['code'], 'failed': 'Print failed'}.get(a['kind'], 'Health alert ' + a['code'])}** {core.safe(a['message'])}" + (f"\n{a['url']}" if a['url'] else '')
                         for a in items)[:3800]

    async def confirm(click, printer, ids):
        """Ask the person who clicked to confirm, then clear and report what the printer says afterwards."""
        items = alerts.current(printer)
        chosen = items if ids == 'all' else [a for a in items if a['id'] in ids]
        if not chosen:
            await click.response.send_message('The printer no longer reports that alert.', ephemeral=True)
            return

        class Confirm(core.OwnedView):
            def __init__(self):
                super().__init__(click.user.id)
                self.button('Clear' if len(chosen) == 1 else f'Clear {len(chosen)} alerts', self.apply, discord.ButtonStyle.danger)
                self.button('Cancel', self.cancel)

            async def apply(self, event):
                if not await core.require(event, 'clearerror'):
                    return
                if self.used:
                    return
                self.used = True
                self.stop()
                await event.response.edit_message(embed=core.card('Clearing…', 'Waiting for the printer to confirm.', core.YELLOW), view=None)
                try:
                    result = await alerts.clear(printer, ids, True, author=core.who(event))
                    core.log.info('%s cleared %s on %s: %s', core.who(event), ids, printer, result['message'])
                    embed = core.card('✅ Cleared' if not result['still'] else '⚠️ Still reported', core.safe(result['message']),
                                      core.GREEN if not result['still'] else core.YELLOW)
                except ValueError as exc:
                    embed = core.card('Not cleared', core.safe(str(exc)), core.RED)
                await event.edit_original_response(embed=embed, view=None)

        view = Confirm()
        text = (f"**{core.safe(core.display_name(printer))}**\n{describe(printer, chosen)}\n\n"
                "Check the printer first: clearing only dismisses the message, it doesn't fix the cause. "
                'Health alerts are hidden in the dashboard until the printer stops reporting them.')
        await click.response.send_message(embed=core.card('⚠️ Clear printer alert?', text, core.YELLOW), view=view, ephemeral=True)
        view.message = await click.original_response()

    async def show(interaction, printer, edit=False):
        items = alerts.current(printer)

        class Choices(core.OwnedView):
            def __init__(self):
                super().__init__(interaction.user.id)
                for alert in items[:4]:
                    self.button(f"{'Dismiss' if alert['kind'] == 'hms' else 'Clear'} {alert['code']}", self.pick([alert['id']]), discord.ButtonStyle.danger)
                if len(items) > 1:
                    self.button('Clear all', self.pick('all'), discord.ButtonStyle.danger)
                self.button('Cancel', self.cancel)

            def pick(self, ids):
                async def callback(click):
                    if not await core.require(click, 'clearerror'):
                        return
                    await confirm(click, printer, ids)
                return callback

        embed = core.card(f'Errors and alerts: {core.safe(core.display_name(printer))}', describe(printer, items),
                          core.RED if items else core.GREEN)
        view = Choices() if items else None
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=core.ephemeral(interaction), wait=True)
        if view:
            view.message = message

    @core.bot.tree.command(name='clearerror', description="Show a printer's errors and health alerts and clear them, with confirmation")
    @app_commands.guild_only()
    async def clearerror(i: discord.Interaction, name: str = None):
        if not await core.require(i, 'clearerror'):
            return
        await i.response.defer(ephemeral=core.ephemeral(i))
        exact, suggestion = core.resolve_name(name)
        if exact:
            await show(i, exact)
            return

        class Picker(core.PrinterPicker):
            def choose(self, printer):
                async def picked(click):
                    if not await core.require(click, 'clearerror'):
                        return
                    if await self.finish(click, core.card('Printer selected', core.safe(printer))):
                        await show(click, printer)
                return picked
        view = Picker(i.user.id, 'clearerror', suggestion)
        view.message = await i.followup.send(embed=core.card('Choose printer', 'Select the printer whose errors to show.'),
                                             view=view, ephemeral=core.ephemeral(i), wait=True)

    class NotificationButton(discord.ui.View):
        """The Clear error button under an error notification. Anyone allowed to use /clearerror may press it; the
        confirmation is private to whoever pressed it. Buttons stop working after a restart (use /clearerror)."""
        def __init__(self, printer):
            super().__init__(timeout=12 * 3600)
            button = discord.ui.Button(label='Clear error…', style=discord.ButtonStyle.danger)
            button.callback = self.clear
            self.add_item(button)
            self.printer = printer

        async def clear(self, click):
            if not await core.require(click, 'clearerror'):
                return
            await confirm(click, self.printer, 'all')

    def notification_view(printer, title):
        if title.startswith(ERROR_TITLES) and alerts.current(printer):
            return NotificationButton(printer)
        return None

    core.notification_view = notification_view
