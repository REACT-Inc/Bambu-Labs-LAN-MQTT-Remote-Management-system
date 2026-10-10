import base64
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dashboard_client import ClientError, DashboardClient, main
from dashboard_mcp import call_tool, dispatch


class ClientTests(unittest.TestCase):
    def client(self):
        return DashboardClient('http://127.0.0.1:8080', 'unused')

    def test_url_restrictions(self):
        for url in ('http://remote.example', 'https://user:secret@example.com',
                    'https://example.com/path', 'https://example.com?secret=x', 'file:///tmp/a'):
            with self.assertRaises(ClientError): DashboardClient(url, 'unused')
        DashboardClient('https://example.com:8443', 'unused')

    def test_status_projection_excludes_secrets_and_csrf(self):
        client = self.client()
        state = {'demo': False, 'csrf': 'PRIVATE', 'settings': {'password': 'PRIVATE'},
            'printers': [{'name': 'Printer A', 'connected': True, 'error': 0,
                'data': {'access_code': 'PRIVATE', 'subtask_name': 'PRIVATE', 'mc_percent': 42}}]}
        with patch.object(client, 'request', return_value=state):
            result = client.state()
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(result['printers'][0]['telemetry'], {'mc_percent': 42})

    def test_movement_and_confirmation_validation(self):
        client = self.client()
        with patch.object(client, 'request') as request:
            for args in ({}, {'confirmed': True}, {'confirmed': True, 'homed': True, 'axis': 'Z', 'value': 2},
                         {'confirmed': True, 'homed': True, 'axis': 'X', 'value': float('nan')}):
                with self.assertRaises(ClientError): client.control('Printer A', 'move', **args)
            request.assert_not_called()
            client.control('Printer A', 'move', 1, 'X', True, True)
            request.assert_called_once_with('/api/printers/Printer%20A/move',
                {'confirmed': True, 'homed': True, 'axis': 'X', 'value': 1})

    def test_camera_name_is_encoded(self):
        client = self.client()
        with patch.object(client, 'request') as request:
            client.snapshot('Printer A/#')
            request.assert_called_once_with('/api/camera/Printer%20A%2F%23', image=True)

    def test_cli_requires_confirmation_before_connecting(self):
        with patch('dashboard_client.DashboardClient') as client, patch('sys.stderr', new=io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--url', 'http://127.0.0.1', '--password-file', 'unused',
                      'control', '--printer', 'Printer A', '--action', 'stop'])
            client.assert_not_called()

    def test_failed_command_is_not_retried(self):
        client = self.client()
        with patch.object(client, 'request', side_effect=ClientError('failed')) as request:
            with self.assertRaises(ClientError):
                client.control('Printer A', 'stop', confirmed=True)
            request.assert_called_once()


class MCPTests(unittest.TestCase):
    def request(self, method, params=None):
        return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}

    def test_initialize_list_and_notifications(self):
        result = dispatch(self.request('initialize', {'protocolVersion': '2025-03-26'}))['result']
        self.assertEqual(result['protocolVersion'], '2025-03-26')
        self.assertEqual(len(dispatch(self.request('tools/list'))['result']['tools']), 3)
        self.assertIsNone(dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))

    def test_status_and_camera(self):
        client = Mock()
        client.state.return_value = {'demo': True, 'printers': []}
        client.snapshot.return_value = b'\xff\xd8image\xff\xd9'
        result = call_tool('printer_status', {}, lambda: client)
        self.assertTrue(json.loads(result['content'][0]['text'])['demo'])
        image = call_tool('printer_camera', {'printer': 'Printer A'}, lambda: client)['content'][0]
        self.assertEqual(base64.b64decode(image['data']), client.snapshot.return_value)
        self.assertEqual(client.logout.call_count, 2)

    def test_no_implicit_authorization(self):
        factory = Mock()
        for args in ({'printer': 'A', 'action': 'stop'},
                     {'printer': 'A', 'action': 'stop', 'confirmed': 'true'},
                     {'printer': 'A', 'action': 'stop', 'confirmed': True, 'extra': 1}):
            result = dispatch(self.request('tools/call', {'name': 'printer_control', 'arguments': args}), factory)
            self.assertTrue(result['result']['isError'])
        factory.assert_not_called()

    def test_control_failure_logs_out_and_hides_details(self):
        client = Mock()
        client.control.side_effect = ClientError('PRIVATE')
        result = dispatch(self.request('tools/call', {'name': 'printer_control',
            'arguments': {'printer': 'A', 'action': 'stop', 'confirmed': True}}), lambda: client)
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        client.control.assert_called_once()
        client.logout.assert_called_once()

    def test_unknown_methods_and_invalid_params(self):
        self.assertEqual(dispatch(self.request('unknown'))['error']['code'], -32601)
        message = self.request('tools/call');message['params'] = []
        self.assertEqual(dispatch(message)['error']['code'], -32602)


if __name__ == '__main__':
    unittest.main()
