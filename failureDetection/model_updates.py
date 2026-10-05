"""Keep the AI model up to date without running anything by hand (#67 follow-up to #75).

Once a day the dashboard service looks at the repository's `ai-model` release (the same one setup_ai.py installs
from). When print_failure.onnx there is newer than the model in use, it downloads it into the data folder, checks it
runs (one test picture through the helper), then switches to it: no restart, no setup, and it works for updates
installed from the dashboard too (which never run scripts).

It only ever replaces a model that came from that release: one setup_ai.py downloaded, or an earlier automatic
update. A model you copied in yourself, or set to another path in config.json, is never touched.
"""
import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path

from failureDetection import setup_ai

log = logging.getLogger('failure-detection')
CHECK_EVERY = 24 * 3600


class ModelUpdates:
    def __init__(self, core, monitor, clock=time.time):
        self.core, self.monitor, self.clock = core, monitor, clock
        self.folder = Path(getattr(core, 'DATA_DIR', tempfile.gettempdir())) / 'models'
        self.checked, self.message = 0, ''

    def managed(self, path):
        """Did this model come from the ai-model release (setup download or an earlier automatic update)?"""
        path = Path(path)
        if not path.is_file() or path.suffix.lower() != '.onnx':
            return False
        if path.parent == self.folder:
            return True
        try:
            record = json.loads((path.parent / '.downloaded.json').read_text())
        except (OSError, ValueError):
            return False
        return record.get(path.name) == setup_ai.sha256(path)

    def apply_saved(self):
        """At start-up: use the model a previous automatic update switched to (saved in settings.json)."""
        saved = self.core.settings.get('ai_model') or {}
        settings = self.monitor.settings
        if saved.get('path') and Path(saved['path']).is_file() and self.managed(settings['model']):
            settings['model'] = saved['path']
            for key in ('classes', 'labels'):
                if saved.get(key):
                    settings[key] = list(saved[key])

    def repository(self):
        try:
            saved = json.loads((Path(getattr(self.core, 'DATA_DIR', '.')) / 'github-updates.json').read_text())
        except (OSError, ValueError):
            saved = {}
        return saved.get('repository') or setup_ai.DEFAULT_REPO, saved.get('token', '')

    async def check(self, force=False):
        """Download and switch to a newer release model when there is one. Returns a short message."""
        settings = self.monitor.settings
        if not force and self.clock() - self.checked < CHECK_EVERY:
            return self.message
        self.checked = self.clock()
        if not self.managed(settings['model']):
            self.message = 'Model set by hand; automatic model updates are off for it.'
            return self.message
        repo, token = self.repository()
        try:
            release = await asyncio.to_thread(setup_ai.api, f'https://api.github.com/repos/{repo}/releases/tags/{setup_ai.TAG}', token)
        except Exception as exc:
            self.message = f'Model update check failed ({type(exc).__name__}).'
            return self.message
        assets = {a.get('name'): a for a in release.get('assets') or []}
        asset = assets.get('print_failure.onnx')
        digest = str((asset or {}).get('digest') or '')
        if not asset or not digest.startswith('sha256:'):
            self.message = 'No model with a checksum in the ai-model release.'
            return self.message
        if digest[7:] == setup_ai.sha256(settings['model']):
            self.message = 'The AI model is up to date.'
            return self.message
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f'print_failure-{digest[7:15]}.onnx'
        meta = dict(setup_ai.DEFAULT_META, classes=settings['classes'], labels=settings['labels'])
        try:
            await asyncio.to_thread(setup_ai.download, asset, target, token)
            if 'print_failure.json' in assets:
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / 'meta.json'
                    await asyncio.to_thread(setup_ai.download, assets['print_failure.json'], path, token)
                    loaded = json.loads(path.read_text())
                meta.update({k: loaded[k] for k in ('classes', 'labels', 'input_size') if k in loaded})
            ok, detail = await asyncio.to_thread(setup_ai.runs, settings['python'], target, meta)
        except Exception as exc:
            target.unlink(missing_ok=True)
            self.message = f'Model update failed: {exc}'
            return self.message
        if not ok:
            target.unlink(missing_ok=True)
            self.message = f'New model did not run ({detail}); keeping the current one.'
            return self.message
        await self.switch(target, meta)
        for old in self.folder.glob('print_failure-*.onnx'):   # keep only the model in use
            if old != target:
                old.unlink(missing_ok=True)
        self.message = f'Updated the AI model ({digest[7:15]}).'
        return self.message

    async def switch(self, path, meta):
        settings = self.monitor.settings
        settings['model'], settings['classes'], settings['labels'] = str(path), list(meta['classes']), list(meta['labels'])
        settings['input_size'] = int(meta.get('input_size', settings.get('input_size', 640)))
        self.core.save_settings({**self.core.settings, 'ai_model': {'path': str(path), 'classes': settings['classes'],
                                                                   'labels': settings['labels'], 'time': self.clock()}})
        backend = getattr(self.monitor, 'backend', None)
        if backend and hasattr(backend, 'close'):
            await backend.close()   # the helper restarts with the new model on the next picture
        self.monitor.geometry.labels = set(settings['labels'])
        self.monitor.store_event('', 'AI model updated', f'{Path(path).name} from the ai-model release')
        log.info('AI model updated to %s', path)
