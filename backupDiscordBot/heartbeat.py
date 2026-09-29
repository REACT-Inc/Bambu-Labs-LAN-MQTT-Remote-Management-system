"""Heartbeat the main service writes so the backup bot knows whether it is healthy.

The main service writes DATA_DIR/heartbeat.json every 15 s from its event loop. If the file goes stale the main
process is stopped, crashed or frozen (#40); if discord_ready is false its Discord bot is disconnected.
"""
import json
import os
import time
from pathlib import Path

NAME = 'heartbeat.json'
INTERVAL = 15
MAX_AGE = 45   # three missed beats


def write(data_dir, discord_ready, release=''):
    path = Path(data_dir) / NAME
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'time': time.time(), 'discord_ready': bool(discord_ready), 'pid': os.getpid(), 'release': release}))
    os.replace(temp, path)


def read(data_dir):
    try:
        data = json.loads((Path(data_dir) / NAME).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def main_is_healthy(beat, now=None, max_age=MAX_AGE):
    """True when the main service is running, not frozen, and its Discord bot is connected."""
    if not beat:
        return False
    now = time.time() if now is None else now
    try:
        fresh = 0 <= now - float(beat.get('time', 0)) <= max_age
    except (TypeError, ValueError):
        return False
    return fresh and beat.get('discord_ready') is True


async def run(core, data_dir, release=''):
    """Main-service side: write a heartbeat every INTERVAL seconds until cancelled."""
    import asyncio
    while True:
        try:
            write(data_dir, core.bot.is_ready(), release)
        except Exception:
            core.log.exception('Could not write the backup-bot heartbeat')
        await asyncio.sleep(INTERVAL)
