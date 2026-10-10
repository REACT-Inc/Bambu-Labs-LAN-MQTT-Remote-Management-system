"""Dependency-free MCP stdio bridge to the authenticated management dashboard."""
import base64
import json
import os
import sys

from dashboard_client import ClientError, DashboardClient


TOOLS = [
    {'name': 'printer_status', 'description': 'Read current dashboard printer status and telemetry. '
     'Check demo, connected and last_seen before treating this as live data.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'printer_camera', 'description': 'Get a camera JPEG for one exact printer name.',
     'inputSchema': {'type': 'object', 'properties': {'printer': {'type': 'string'}},
                     'required': ['printer'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'printer_control', 'description': 'Submit one printer control only when the user has '
     'authorized that specific printer and action. Movement additionally requires the operator '
     'to confirm homing and physical clearance. Never retry a command automatically; a response '
     'indicates submission, not verified physical success.',
     'inputSchema': {'type': 'object', 'properties': {
         'printer': {'type': 'string'},
         'action': {'type': 'string', 'enum': ['pause', 'resume', 'stop', 'lighton', 'lightoff',
                    'nozzle', 'bed', 'chamber', 'speed', 'fan', 'fanall', 'move']},
         'value': {'type': ['number', 'string']},
         'axis': {'type': 'string', 'enum': ['X', 'Y', 'Z']},
         'confirmed': {'type': 'boolean', 'description': 'User authorized this exact action.'},
         'homed_clear': {'type': 'boolean', 'description': 'Operator checked homing and clearance.'}},
         'required': ['printer', 'action', 'confirmed'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': False}},
]


def call_tool(name, args, client_factory=None):
    if not isinstance(args, dict):
        raise ClientError('Tool arguments must be an object.')
    schema = next((t['inputSchema'] for t in TOOLS if t['name'] == name), None)
    if schema is None or set(args) - set(schema['properties']) or not set(schema.get('required', [])) <= set(args):
        raise ClientError('Unknown tool or invalid arguments.')
    if 'printer' in args and (not isinstance(args['printer'], str) or not args['printer']):
        raise ClientError('An exact printer name is required.')
    if name == 'printer_control' and args.get('confirmed') is not True:
        raise ClientError('Explicit user authorization is required.')
    factory = client_factory or (lambda: DashboardClient(
        os.environ.get('PM_DASHBOARD_URL', ''), os.environ.get('PM_DASHBOARD_PASSWORD_FILE', '')))
    client = factory()
    try:
        client.login()
        if name == 'printer_camera':
            content = [{'type': 'image', 'mimeType': 'image/jpeg',
                        'data': base64.b64encode(client.snapshot(args['printer'])).decode()}]
        else:
            if name == 'printer_status':
                result = client.state()
            else:
                result = client.control(args['printer'], args['action'], args.get('value'),
                    args.get('axis'), args['confirmed'], args.get('homed_clear', False))
            content = [{'type': 'text', 'text': json.dumps(result)}]
        return {'content': content, 'isError': False}
    finally:
        client.logout()


def dispatch(message, client_factory=None):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
    if 'id' not in message:
        return None
    response = {'jsonrpc': '2.0', 'id': message['id']}
    method = message.get('method')
    params = message.get('params', {})
    if not isinstance(params, dict):
        response['error'] = {'code': -32602, 'message': 'Invalid params'}
    elif method == 'initialize':
        requested = params.get('protocolVersion')
        version = requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-06-18'
        response['result'] = {'protocolVersion': version, 'capabilities': {'tools': {}},
            'serverInfo': {'name': 'printer-dashboard', 'version': '1.0.0'}}
    elif method == 'ping':
        response['result'] = {}
    elif method == 'tools/list':
        response['result'] = {'tools': TOOLS}
    elif method == 'tools/call':
        try:
            response['result'] = call_tool(params.get('name'), params.get('arguments', {}), client_factory)
        except (ClientError, OSError, ValueError, TypeError, KeyError):
            response['result'] = {'isError': True, 'content': [{'type': 'text', 'text':
                'Tool failed. No command was retried. Check configuration, authorization, '
                'dashboard Activity and current printer state before submitting again.'}]}
    else:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    return response


def main():
    # MCP stdio uses one JSON-RPC message per line. Stdout is protocol-only.
    for line in sys.stdin:
        try:
            response = dispatch(json.loads(line))
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None,
                        'error': {'code': -32700, 'message': 'Parse error'}}
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
