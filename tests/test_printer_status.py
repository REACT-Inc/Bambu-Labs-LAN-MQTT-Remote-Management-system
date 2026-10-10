import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from printer_status import status_document
from status_client import StatusClient, UNAVAILABLE, refresh, write_status


class StatusTests(unittest.TestCase):
    def core(self, entries, demo=False):
        return SimpleNamespace(names=lambda: list(entries), EXAMPLE_MODE=demo,
            last_seen={name: 100 for name in entries}, state_data=lambda name: entries[name])

    def test_counts_progress_and_allowlist(self):
        core = self.core({'Printer A': ('RUNNING', 0, {'mc_percent': 45,
            'mc_remaining_time': 70, 'access_code': 'PRIVATE', 'subtask_name': 'PRIVATE'}, True),
            'Printer B': ('FINISH', 0, {}, True)})
        result = status_document(core, 100)
        self.assertEqual(result['summary'], '1 printing, 1 idle')
        self.assertIn('45% (1h 10m left)', result['detail'])
        self.assertEqual(set(result), {'summary', 'detail', 'tooltip'})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertIn('\n', result['tooltip'])

    def test_stale_missing_and_disconnected_hide_progress(self):
        for seen, connected in ((0, True), (None, True), (100, False), (200, True)):
            core = self.core({'Printer': ('RUNNING', 0, {'mc_percent': 45}, connected)})
            core.last_seen = {'Printer': seen}
            result = status_document(core, 100)
            self.assertEqual(result['summary'], '1 offline')
            self.assertNotIn('45%', result['detail'])

    def test_states_and_errors(self):
        entries = {s: (s, 0, {}, True) for s in ('PAUSE', 'PREPARE', 'IDLE', 'FAILED', 'OTHER')}
        entries['Fault'] = ('RUNNING', 1, {}, True)
        entries['HMS'] = ('IDLE', 0, {'hms': [{'code': 'fault'}]}, True)
        self.assertEqual(status_document(self.core(entries), 100)['summary'],
                         '1 paused, 1 preparing, 1 idle, 3 error, 1 unknown')

    def test_invalid_numbers_and_markup(self):
        for value in (True, -1, 'nan', 'inf', {}, None, 10000000):
            core = self.core({'<b>Printer\n</b>': ('RUNNING', 0,
                {'mc_percent': value, 'mc_remaining_time': value}, True)})
            result = status_document(core, 100)
            self.assertNotIn('%', result['detail'])
            self.assertNotIn('<', result['detail'])
            self.assertNotIn('\n', result['detail'])

    def test_demo_and_empty(self):
        self.assertEqual(status_document(self.core({}), 100)['summary'], 'No printers configured')
        core = self.core({'Printer': ('RUNNING', 0, {'mc_percent': 0}, True)}, True)
        core.last_seen.clear()
        self.assertEqual(status_document(core, 100)['summary'], 'Demo: 1 printing')
        self.assertIn('0%', status_document(core, 100)['detail'])


class ClientTests(unittest.TestCase):
    def test_url_policy(self):
        for url in ('http://remote.example', 'https://user:pass@example.com',
                    'https://example.com/path', 'https://example.com?token=x', 'file:///tmp/status'):
            with self.assertRaises(ValueError): StatusClient(url, 'unused')
        for url in ('https://example.com:8443', 'http://127.0.0.1:8080', 'http://[::1]:8080'):
            StatusClient(url, 'unused')

    def test_atomic_projection_and_failure_replaces_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            write_status(path, {'summary': 'printing', 'detail': 'detail', 'tooltip': 'tip', 'secret': 'PRIVATE'})
            self.assertNotIn('PRIVATE', path.read_text())
            client = SimpleNamespace(fetch=lambda: (_ for _ in ()).throw(OSError('PRIVATE')))
            self.assertFalse(refresh(client, path))
            self.assertEqual(json.loads(path.read_text()), UNAVAILABLE)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_response_replaces_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            self.assertFalse(refresh(SimpleNamespace(fetch=lambda: {'summary': 42}), path))
            self.assertEqual(json.loads(path.read_text()), UNAVAILABLE)

    def test_session_relogin(self):
        with tempfile.TemporaryDirectory() as directory:
            password = Path(directory) / 'private.txt'
            password.write_text('synthetic-password\n')
            client = StatusClient('https://example.com', password)
            error = urllib.error.HTTPError(client.url, 401, 'Unauthorized', {}, None)
            result = dict(UNAVAILABLE)
            with patch.object(client, 'read_status', side_effect=[error, result]), \
                    patch.object(client.opener, 'open') as opened:
                self.assertEqual(client.fetch(), result)
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, 'https://example.com/api/login')
                self.assertEqual(json.loads(request.data), {'password': 'synthetic-password'})


if __name__ == '__main__':
    unittest.main()
