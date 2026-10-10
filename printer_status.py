"""Minimal status-bar projection; never serialize configuration or raw telemetry."""
import math
import time
from collections import Counter


def clean_text(value):
    # Status-bar labels interpret angle brackets as markup.
    return ''.join(c for c in str(value) if c.isprintable() and c not in '<>{}')[:80]


def number(value, maximum):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return int(value) if math.isfinite(value) and 0 <= value <= maximum else None
    except (ValueError, TypeError, OverflowError):
        return None


def status_document(core, now=None):
    now = time.time() if now is None else now
    counts, lines = Counter(), []
    for name in core.names():
        state, error, data, connected = core.state_data(name)
        seen = core.last_seen.get(name)
        fresh = core.EXAMPLE_MODE or (isinstance(seen, (int, float)) and 0 <= now - seen <= 90)
        if not connected or not fresh:
            category = 'offline'
        elif error or data.get('hms'):
            category = 'error'
        else:
            category = {'RUNNING': 'printing', 'PAUSE': 'paused', 'PREPARE': 'preparing',
                        'IDLE': 'idle', 'FINISH': 'idle', 'FAILED': 'error'}.get(state, 'unknown')
        counts[category] += 1
        label = core.display_name(name) if hasattr(core, 'display_name') else name
        line = f'{clean_text(label)}: {category}'
        if category in ('printing', 'paused', 'preparing'):
            percent = number(data.get('mc_percent'), 100)
            remaining = number(data.get('mc_remaining_time'), 525600)
            if percent is not None:
                line += f' {percent}%'
            if remaining is not None:
                hours, minutes = divmod(remaining, 60)
                duration = f'{hours}h {minutes}m' if hours else f'{minutes}m'
                line += f' ({duration} left)'
        lines.append(line)
    summary = ', '.join(f'{counts[k]} {k}' for k in
                        ('printing', 'paused', 'preparing', 'idle', 'error', 'offline', 'unknown') if counts[k])
    if core.EXAMPLE_MODE:
        summary = 'Demo: ' + (summary or 'No printers configured')
    return {'summary': summary or 'No printers configured',
            'detail': ' | '.join(lines) or 'No printers configured',
            'tooltip': '\n'.join(lines) or 'No printers configured'}
