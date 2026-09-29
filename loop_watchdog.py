"""Observe the shared Discord/dashboard asyncio loop from a separate thread.

The existing service uses Restart=on-failure. On startup, Store marks jobs with
uncertain print outcomes needs_review; restarting never resubmits them.
"""
import asyncio
import faulthandler
import os
import sys
import threading
import time


class LoopWatchdog:
    def __init__(self, log, interval=1, warn_after=15, restart_after=60,
                 clock=time.monotonic, exit_process=os._exit, dump=None, stall_file=None):
        if not 0 < interval < warn_after < restart_after:
            raise ValueError('Watchdog deadlines must be increasing and positive.')
        self.log, self.interval = log, interval
        self.warn_after, self.restart_after = warn_after, restart_after
        self.clock, self.exit_process = clock, exit_process
        # Also keep the stacks in a file under logs/, so diagnostic reports include them (not only the journal).
        self.stall_file = stall_file
        self.dump = dump or self._dump_stacks
        self.last_beat = self.clock()
        self.reported = False
        self.stopped = threading.Event()
        self.thread = None
        self.task = None

    def _dump_stacks(self):
        # stderr goes to the service journal; this shows code locations, not locals.
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        if self.stall_file:
            try:
                path = str(self.stall_file)
                # Keep the file small: start over once it passes 256 KB.
                if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
                    os.replace(path, path + '.1')
                with open(path, 'a', encoding='utf-8') as stream:
                    stream.write(f'\n=== Event loop stall at {time.strftime("%Y-%m-%d %H:%M:%S %z")} ===\n')
                    stream.flush()
                    faulthandler.dump_traceback(file=stream, all_threads=True)
            except OSError as exc:
                self.log.warning('Could not write the stall report file: %s', exc)

    async def _heartbeat(self):
        while not self.stopped.is_set():
            self.last_beat = self.clock()
            await asyncio.sleep(self.interval)

    def _check(self):
        if self.stopped.is_set():
            return
        lag = self.clock() - self.last_beat
        if lag < self.warn_after:
            if self.reported:
                self.log.warning('Event loop recovered from a stall.')
            self.reported = False
            return
        if not self.reported:
            self.log.critical('Event loop has not run for %.1f seconds; dumping thread stacks to the journal.', lag)
            self.dump()
            self.reported = True
        if lag >= self.restart_after and not self.stopped.is_set():
            self.log.critical('Event loop stalled for %.1f seconds; exiting for systemd restart.', lag)
            self.exit_process(1)

    def _observe(self):
        while not self.stopped.wait(self.interval):
            self._check()

    def start(self):
        self.last_beat = self.clock()
        self.task = asyncio.create_task(self._heartbeat(), name='loop-watchdog-heartbeat')
        self.thread = threading.Thread(target=self._observe, name='loop-watchdog', daemon=True)
        self.thread.start()

    async def stop(self):
        self.stopped.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.thread:
            await asyncio.to_thread(self.thread.join, self.interval + 1)
