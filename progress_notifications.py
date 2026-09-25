"""Deduplicate progress milestones across partial reports and reconnects."""
import math


class ProgressTracker:
    def __init__(self):
        self.jobs = {}

    def update(self, name, data):
        state = data.get('gcode_state', data.get('state', 'UNKNOWN'))
        if state == 'UNKNOWN':
            return None
        identity = str(data.get('subtask_id') or data.get('gcode_file') or data.get('subtask_name') or '')
        previous = self.jobs.get(name)
        if state in ('IDLE', 'FINISH', 'FAILED', 'PREPARE'):
            self.jobs[name] = dict(identity=identity, active=False, milestone=0)
            return None
        if state not in ('RUNNING', 'PAUSE'):
            return None
        try:
            percent = float(data['mc_percent'])
            if not math.isfinite(percent) or not 0 <= percent <= 100:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        # Completion is covered by the existing FINISH notification.
        milestone = min(90, int(percent // 10) * 10)
        if previous is None:
            # Starting the service mid-print should not flood old milestones.
            self.jobs[name] = dict(identity=identity, active=True, milestone=milestone)
            return None
        if not previous['active'] or (identity and previous['identity'] and identity != previous['identity']):
            previous = dict(identity=identity, active=True, milestone=0)
            self.jobs[name] = previous
        if identity:
            previous['identity'] = identity
        if state == 'RUNNING' and milestone > previous['milestone']:
            previous['milestone'] = milestone
            return milestone
        return None
