import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from discord_lifecycle import maintain_discord


class DiscordLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Mock()
        self.bot.start = AsyncMock()
        self.bot.close = AsyncMock()
        self.log = Mock()

    async def test_transient_failure_closes_and_resets_client_before_retry(self):
        events = []

        async def start(*args, **kwargs):
            events.append('start')
            if events.count('start') == 1:
                raise OSError('offline')
            raise asyncio.CancelledError

        async def close():
            events.append('close')

        def clear():
            events.append('clear')

        async def sleep(delay):
            events.append(('sleep', delay))

        self.bot.start.side_effect = start
        self.bot.close.side_effect = close
        self.bot.clear.side_effect = clear
        with patch('discord_lifecycle.asyncio.sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await maintain_discord(self.bot, 'token', self.log)
        self.assertEqual(events, ['start', 'close', 'clear', ('sleep', 5), 'start'])
        self.assertEqual(self.bot.start.call_args_list[0].kwargs, {'reconnect': True})

    async def test_repeated_failures_have_bounded_backoff(self):
        delays = []
        self.bot.start.side_effect = OSError('offline')

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 6:
                raise asyncio.CancelledError

        with patch('discord_lifecycle.asyncio.sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await maintain_discord(self.bot, 'token', self.log)
        self.assertEqual(delays, [5, 10, 20, 40, 60, 60])
        self.assertEqual(self.bot.close.await_count, 6)
        self.assertEqual(self.bot.clear.call_count, 6)

    async def test_login_failure_does_not_retry(self):
        self.bot.start.side_effect = discord.LoginFailure('bad token')
        await maintain_discord(self.bot, 'token', self.log)
        self.bot.start.assert_awaited_once()
        self.bot.clear.assert_not_called()

    async def test_cancellation_interrupts_retry(self):
        self.bot.start.side_effect = OSError('offline')
        sleeping = asyncio.Event()

        async def sleep(_):
            sleeping.set()
            await asyncio.Event().wait()

        with patch('discord_lifecycle.asyncio.sleep', sleep):
            task = asyncio.create_task(maintain_discord(self.bot, 'token', self.log))
            await sleeping.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.bot.start.await_count, 1)

    async def test_no_token_keeps_dashboard_only_mode(self):
        await maintain_discord(self.bot, '', self.log)
        self.bot.start.assert_not_awaited()
