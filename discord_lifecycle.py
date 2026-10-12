"""Keep Discord available when a temporary connection failure escapes discord.py."""

import asyncio

import discord


async def maintain_discord(bot, token, log, *, initial_delay=5, max_delay=60):
    if not token:
        log.warning('No Discord token configured: dashboard-only mode.')
        return

    delay = initial_delay
    while True:
        try:
            # discord.py retries ordinary gateway disconnects internally. This
            # loop also covers failures during login and exceptions that escape it.
            await bot.start(token, reconnect=True)
            log.warning('Discord connection ended; retrying in %s seconds.', delay)
        except (discord.LoginFailure, discord.PrivilegedIntentsRequired, discord.Forbidden):
            log.exception('Discord credentials or permissions need attention; dashboard remains available.')
            return
        except discord.HTTPException as error:
            if error.status in (401, 403):
                log.exception('Discord rejected the bot credentials or permissions; dashboard remains available.')
                return
            log.exception('Discord connection failed; retrying in %s seconds.', delay)
        except Exception:
            log.exception('Discord connection failed; retrying in %s seconds.', delay)

        # A failed start may leave the client closed or partly initialized.
        # clear() makes the same bot instance safe to log in again.
        await bot.close()
        bot.clear()
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)
