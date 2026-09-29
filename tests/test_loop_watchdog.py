import asyncio
import time
import unittest
from unittest.mock import Mock

from loop_watchdog import LoopWatchdog


class LoopWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_loop_does_not_dump_or_exit(self):
        log, dump, exit_process = Mock(), Mock(), Mock()
        watcher = LoopWatchdog(log, interval=0.01, warn_after=0.04,
                               restart_after=0.10, dump=dump, exit_process=exit_process)
        watcher.start()
        try:
            await asyncio.sleep(0.05)
        finally:
            await watcher.stop()
        dump.assert_not_called()
        exit_process.assert_not_called()

    async def test_blocked_loop_logs_stacks_and_triggers_service_restart(self):
        log, dump, exits = Mock(), Mock(), []
        watcher = LoopWatchdog(log, interval=0.01, warn_after=0.03,
                               restart_after=0.09, dump=dump,
                               exit_process=lambda code: (exits.append(code), watcher.stopped.set()))
        watcher.start()
        try:
            await asyncio.sleep(0.03)
            # Deliberately block the asyncio loop while the observer thread remains alive.
            time.sleep(0.15)
        finally:
            await watcher.stop()
        dump.assert_called_once()
        log.critical.assert_called()
        self.assertEqual(exits, [1])

    async def test_recovered_loop_rearms_stall_warning(self):
        now = [10.0]
        log, dump = Mock(), Mock()
        watcher = LoopWatchdog(log, clock=lambda: now[0], dump=dump,
                               exit_process=Mock())
        now[0] = 30.0
        watcher._check()
        watcher._check()
        dump.assert_called_once()
        watcher.last_beat = now[0]
        watcher._check()
        now[0] = 50.0
        watcher._check()
        self.assertEqual(dump.call_count, 2)


if __name__ == '__main__':
    unittest.main()
