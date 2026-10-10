"""Authenticated CLI for inspecting and controlling a running printer dashboard."""
import argparse
import http.cookiejar
import json
import math
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class ClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DashboardClient:
    def __init__(self, url, password_file):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))):
            raise ClientError('Use HTTPS, or HTTP on loopback only, without URL credentials or paths.')
        self.url = url.rstrip('/')
        self.password_file = Path(password_file)
        self.csrf = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())

    def request(self, path, payload=None, image=False):
        headers = {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers = {'Content-Type': 'application/json', 'X-PM': '1'}
            if self.csrf:
                headers['X-CSRF'] = self.csrf
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ClientError('Dashboard response exceeds the client limit.')
                if image:
                    if response.headers.get_content_type() != 'image/jpeg' or not (
                            body.startswith(b'\xff\xd8') and body.endswith(b'\xff\xd9')):
                        raise ClientError('The dashboard did not return a JPEG camera frame.')
                    return body
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            # Error bodies can contain runtime details. Never echo them or replay commands.
            raise ClientError(f'Dashboard returned HTTP {exc.code}. No command was retried; '
                              'check dashboard Activity and current printer state.') from None
        except (OSError, ValueError) as exc:
            raise ClientError('Dashboard connection or response failed. No command was retried; '
                              'check current printer state before submitting again.') from None

    def login(self):
        try:
            password = self.password_file.read_text(encoding='utf-8').rstrip('\r\n')
        except OSError:
            raise ClientError('Cannot read the private dashboard password file.') from None
        result = self.request('/api/login', {'password': password})
        if not isinstance(result, dict) or not isinstance(result.get('csrf'), str):
            raise ClientError('Invalid dashboard login response.')
        self.csrf = result['csrf']

    def logout(self):
        if self.csrf:
            try:
                self.request('/api/logout', {})
            except ClientError:
                pass
            self.csrf = None

    def state(self):
        result = self.request('/api/state')
        # Deliberately omit settings, CSRF, job filenames, events and raw telemetry.
        fields = ('mc_percent', 'mc_remaining_time', 'nozzle_temper', 'nozzle_target_temper',
                  'bed_temper', 'bed_target_temper', 'chamber_temper', 'spd_lvl')
        try:
            printers = []
            for p in result['printers']:
                data = p.get('data', {})
                printers.append({**{k: p.get(k) for k in
                    ('name', 'display_name', 'state', 'connected', 'last_seen')},
                    'has_error': bool(p.get('error') or data.get('hms')),
                    'telemetry': {k: data[k] for k in fields if k in data}})
            return {'demo': bool(result['demo']), 'printers': printers}
        except (KeyError, TypeError, AttributeError):
            raise ClientError('Invalid dashboard state response.') from None

    def snapshot(self, printer):
        return self.request('/api/camera/' + urllib.parse.quote(printer, safe=''), image=True)

    def control(self, printer, action, value=None, axis=None, confirmed=False, homed=False):
        allowed = ('pause', 'resume', 'stop', 'lighton', 'lightoff', 'nozzle', 'bed', 'chamber',
                   'speed', 'fan', 'fanall', 'move')
        if action not in allowed:
            raise ClientError('Unsupported printer action.')
        if confirmed is not True:
            raise ClientError('An explicit --confirm is required for every printer command.')
        if action == 'move':
            if homed is not True:
                raise ClientError('Movement requires --homed-clear after checking the printer.')
            if axis not in ('X', 'Y', 'Z') or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ClientError('Movement requires an axis and numeric millimeters.')
            if not math.isfinite(value) or not 0.1 <= abs(value) <= (1 if axis == 'Z' else 10):
                raise ClientError('Movement must be 0.1–10 mm for X/Y or 0.1–1 mm for Z.')
        if action in ('nozzle', 'bed', 'chamber', 'fan', 'fanall', 'speed') and value is None:
            raise ClientError('This action requires --value.')
        path = '/api/printers/' + urllib.parse.quote(printer, safe='') + '/' + action
        return self.request(path, {'confirmed': True, 'homed': homed, 'axis': axis, 'value': value})


def save_image(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--password-file', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    snapshot = commands.add_parser('snapshot')
    snapshot.add_argument('--printer', required=True)
    snapshot.add_argument('--output', required=True)
    control = commands.add_parser('control')
    control.add_argument('--printer', required=True)
    control.add_argument('--action', required=True, choices=('pause', 'resume', 'stop', 'lighton',
        'lightoff', 'nozzle', 'bed', 'chamber', 'speed', 'fan', 'fanall', 'move'))
    control.add_argument('--value', help='Degrees, percent, millimeters, or a speed mode.')
    control.add_argument('--axis', choices=('X', 'Y', 'Z'))
    control.add_argument('--confirm', action='store_true')
    control.add_argument('--homed-clear', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'control' and not args.confirm:
        parser.error('Printer commands require --confirm.')
    if args.command == 'control' and args.action == 'move' and not args.homed_clear:
        parser.error('Movement requires --homed-clear after verifying homing and clearance.')
    client = None
    try:
        value = None
        if args.command == 'control' and args.value is not None:
            if args.action == 'speed':
                value = args.value
            elif args.action == 'move':
                value = float(args.value)
            else:
                value = int(args.value)
        client = DashboardClient(args.url, args.password_file)
        client.login()
        if args.command == 'status':
            print(json.dumps(client.state(), indent=2))
        elif args.command == 'snapshot':
            save_image(args.output, client.snapshot(args.printer))
            print('Camera frame saved.')
        else:
            result = client.control(args.printer, args.action, value, args.axis,
                                    args.confirm, args.homed_clear)
            print(json.dumps(result))
        return 0
    except (ClientError, OSError, ValueError):
        # No traceback, password, service address or arbitrary remote error text.
        print('Request failed. No command was retried. Check connectivity, authentication, '
              'dashboard Activity and current printer state before submitting again.')
        return 1
    finally:
        if client:
            client.logout()


if __name__ == '__main__':
    raise SystemExit(main())
