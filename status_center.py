"""The status icon in the dashboard's top-left corner: one place for what the Pi is doing and what it told you.

- **Status:** what matters right now, worst first: an app update installing, failed or available, an AI model being
  downloaded or set up, the AI helper not running, printers reporting errors, Discord offline. Background jobs report
  progress here with set()/clear(); everything else is read live from the parts that own it (sources).
- **Notifications:** the last 50 alerts the app sent (print finished or failed, printer errors, AI alerts…), the same
  ones Discord and the other alert targets get, so they're visible without Discord too.

The icon's colour is the worst status level: error (red), warn (yellow), busy (blue, something is working), else ok.
"""
import logging
import re
import time
from collections import deque

log = logging.getLogger(__name__)
ORDER = {'error': 0, 'warn': 1, 'busy': 2, 'info': 3, 'ok': 4}
KEEP = 50


def plain(text):
    """Discord formatting (**bold**, __underline__, `code`, the progress bar squares) as plain text."""
    text = re.sub(r'(\*\*|__|`)', '', str(text or ''))
    text = re.sub(r'[\U0001F7E5-\U0001F7EB\u2B1B\u2B1C]+\s*', '', text)   # coloured / white / black squares
    return re.sub(r'[ \t]+', ' ', text).strip()


class StatusCenter:
    def __init__(self, colors=None, clock=time.time):
        # colors: Discord embed colour -> level, for notifications (core.RED -> error, core.YELLOW -> warn…)
        self.colors, self.clock = colors or {}, clock
        self.jobs, self.sources, self.feed, self.counter = {}, [], deque(maxlen=KEEP), 0

    # ---- background jobs ----

    def set(self, key, title, detail='', level='busy', progress=None):
        """Show (or update) a job or state, e.g. a model download: progress is 0-1, or None when unknown."""
        if level not in ORDER:
            raise ValueError(f'Unknown status level {level!r}')
        self.jobs[key] = dict(key=key, title=str(title)[:120], detail=str(detail)[:400], level=level,
                              progress=None if progress is None else round(min(1.0, max(0.0, float(progress))), 3),
                              at=self.clock())

    def clear(self, key):
        self.jobs.pop(key, None)

    def add_source(self, source):
        """source() -> [item]: status read live from the part that owns it (updates, the AI helper, printers)."""
        self.sources.append(source)

    # ---- notifications ----

    def note(self, printer, title, detail='', color=None):
        title, printer = str(title)[:120], str(printer or '')
        if 'progress' in title.lower():   # progress every 10 %: only the latest per printer is kept
            for old in [n for n in self.feed if n['printer'] == printer and 'progress' in n['title'].lower()]:
                self.feed.remove(old)
        self.counter += 1
        self.feed.appendleft(dict(id=self.counter, printer=printer, title=title, detail=plain(detail)[:500],
                                  level=self.colors.get(color, 'info'), at=self.clock()))

    # ---- what the dashboard shows ----

    def snapshot(self):
        items = list(self.jobs.values())
        for source in self.sources:
            try:
                items += [dict(item, at=item.get('at') or self.clock()) for item in source() or []]
            except Exception:
                log.exception('Status source failed')
        items.sort(key=lambda item: (ORDER.get(item['level'], 9), -item['at']))
        worst = next((item['level'] for item in items if item['level'] in ('error', 'warn', 'busy')), 'ok')
        return dict(level=worst, items=items, notifications=list(self.feed), latest=self.counter)
