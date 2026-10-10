"""Talking to the Pi's dashboard from the desktop app: addresses, the health check, finding the Pi on the network, and
the summary the tray icon shows. No GUI code here, so it's tested on any computer (tests/test_desktop_app.py).

Traffic to the Pi never goes through a proxy: the Pi is on the local network or the tailnet, and a school or office
proxy would only get in the way.
"""
import concurrent.futures
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request

APPLICATION = '3d-printer-management'   # what the Pi's /health answers with
DEFAULT_PORT = 8080
SESSION_COOKIE = 'pm_session'
HOSTNAME = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                      r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$')
TAILSCALE = (r'C:\Program Files\Tailscale\tailscale.exe', '/Applications/Tailscale.app/Contents/MacOS/Tailscale')
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PiError(Exception):
    """Something went wrong talking to the Pi, worded for the person using the app."""


class SignedOut(PiError):
    """The dashboard session ended (signed out, 12 hours passed, or the Pi app restarted)."""


class TooOld(PiError):
    """The Pi's app is older than the desktop app (no /api/desktop yet)."""


def normalise(address):
    """'192.168.1.50', 'pi.local:8080' or 'http://100.64.1.2:8080/anything' -> 'http://192.168.1.50:8080'."""
    text = str(address or '').strip()
    if not text:
        raise PiError('Enter the address of your Pi, for example 192.168.1.50.')
    if '://' not in text:
        text = 'http://' + text
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise PiError(f'"{address}" is not an address. Use the one the installer showed, for example 192.168.1.50.') from None
    if parts.scheme not in ('http', 'https'):
        raise PiError('The dashboard address starts with http://, for example http://192.168.1.50:8080.')
    if parts.username is not None or parts.password is not None:
        raise PiError("Leave the password out of the address: you'll sign in on the dashboard itself.")
    host = parts.hostname or ''
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        if not HOSTNAME.match(host):
            raise PiError(f'"{address}" is not an address. Use the one the installer showed, for example 192.168.1.50.')
    if port is None:
        port = DEFAULT_PORT if parts.scheme == 'http' else 443
    host = f'[{ip.compressed}]' if ip and ip.version == 6 else (ip.compressed if ip else host.lower())
    return f'{parts.scheme}://{host}' + ('' if (parts.scheme, port) == ('https', 443) else f':{port}')


def host_of(base):
    """The host name a cookie for this dashboard is stored under ('192.168.1.50', '::1', 'pi.local')."""
    return (urllib.parse.urlsplit(base).hostname or '').lower()


def _reason(exc):
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return 'no answer within a few seconds'
    if isinstance(reason, ConnectionRefusedError):
        return 'the connection was refused'
    if isinstance(reason, socket.gaierror):
        return 'that name could not be found'
    return str(reason) or type(reason).__name__


def _get(base, path, timeout, headers=None, limit=4 * 1024 * 1024):
    request = urllib.request.Request(base + path, headers={'Accept': 'application/json', **(headers or {})})
    with OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read(limit))


def check(base, timeout=3):
    """The Pi's release ID when 3D Printer Management's dashboard answers at base, else PiError saying why not."""
    not_dashboard = f"Something answered at {base}, but it isn't the 3D Printer Management dashboard. Check the address and port."
    try:
        data = _get(base, '/health', timeout)
    except urllib.error.HTTPError:
        raise PiError(not_dashboard) from None
    except (urllib.error.URLError, OSError) as exc:
        raise PiError(f'Nothing answered at {base} ({_reason(exc)}). Is this laptop on the same network as the Pi '
                      '(or signed in to Tailscale), and is the Pi on?') from None
    except ValueError:
        raise PiError(not_dashboard) from None
    if not isinstance(data, dict) or data.get('application') != APPLICATION:
        raise PiError(not_dashboard)
    return str(data.get('release') or '')


def summary(base, session, timeout=6):
    """The tray's view of the Pi (GET /api/desktop), using the dashboard window's own session."""
    if not session:
        raise SignedOut('Not signed in.')
    try:
        data = _get(base, '/api/desktop', timeout, {'Cookie': f'{SESSION_COOKIE}={session}'})
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SignedOut('Signed out of the dashboard.') from None
        if exc.code == 404:
            raise TooOld('Update the Pi to version 1.7.8 or newer to see the printers here.') from None
        raise PiError(f'The Pi answered with an error (HTTP {exc.code}).') from None
    except (urllib.error.URLError, OSError) as exc:
        raise PiError(f"Can't reach the Pi ({_reason(exc)}).") from None
    except ValueError:
        raise PiError('The Pi sent something unexpected.') from None
    if not isinstance(data, dict) or data.get('application') != APPLICATION:
        raise PiError('The Pi sent something unexpected.')
    return data


# ---- finding the Pi ----

def local_networks(limit=4):
    """The private IPv4 /24 networks this computer is on (Wi-Fi, Ethernet...), the default route's first."""
    addresses = []
    try:   # the address of the network the default route uses (connecting a UDP socket sends nothing)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('192.0.2.1', 9))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        addresses += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    networks = []
    for address in addresses:
        ip = ipaddress.IPv4Address(address)
        if ip.is_private and not (ip.is_loopback or ip.is_link_local):
            network = ipaddress.IPv4Network(f'{ip}/24', strict=False)
            if network not in networks:
                networks.append(network)
    return networks[:limit]


def tailscale_devices(run=subprocess.run, which=shutil.which):
    """(IPv4 address, name) of this computer's online tailnet devices, when the Tailscale app is installed here."""
    exe = which('tailscale') or next((path for path in TAILSCALE if os.path.isfile(path)), None)
    if not exe:
        return []
    try:
        result = run([exe, 'status', '--json'], capture_output=True, timeout=6,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(result.stdout or b'{}')
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    devices = []
    for device in list((data.get('Peer') or {}).values()) + [data.get('Self') or {}]:
        if device is not data.get('Self') and not device.get('Online'):
            continue
        name = str(device.get('DNSName') or device.get('HostName') or '').rstrip('.')
        devices += [(ip, name) for ip in device.get('TailscaleIPs') or [] if '.' in str(ip)]
    return devices


def find(port=DEFAULT_PORT, networks=None, devices=None, reachable=None, check_base=check, workers=64):
    """Every 3D Printer Management dashboard this computer can reach: on its own networks (each /24), its tailnet,
    and this computer itself. [{'url', 'name', 'release'}], tailnet and named devices first."""
    candidates = {'127.0.0.1': 'this computer'}
    for ip, name in tailscale_devices() if devices is None else devices:
        candidates.setdefault(ip, name)
    for network in local_networks() if networks is None else networks:
        for host in network.hosts():
            candidates.setdefault(str(host), '')

    def open_port(ip):
        try:
            with socket.create_connection((ip, port), timeout=0.6):
                return True
        except OSError:
            return False

    def probe(ip):
        if not (reachable or open_port)(ip):
            return None
        base = f'http://{ip}:{port}'
        try:
            return dict(url=base, name=candidates[ip], release=check_base(base, timeout=3))
        except PiError:
            return None

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        found = [result for result in pool.map(probe, candidates) if result]
    return sorted(found, key=lambda pi: (not pi['name'], pi['name'] == 'this computer'))
