"""The Pi's Tailscale address, and listening on it after it changes.

The installer sets "listen" in config.json to the Pi's Tailscale address at the time. Tailscale can give the Pi a new
one: signing it in again (for example to add ACL tags), removing it from the tailnet and adding it back, or a new
tailnet. The dashboard used to keep trying the old address, every 30 s, for good, so it was out of reach from the
tailnet. Now an address in Tailscale's range (100.64.0.0/10) that isn't on this computer any more is replaced by the
Pi's Tailscale address now (Listener; pm-doctor does the same for its page).

Only the standard library here: pm-doctor uses tailscale_ipv4() too.
"""
import asyncio
import fcntl
import ipaddress
import socket
import struct
import subprocess

TAILSCALE = ipaddress.ip_network('100.64.0.0/10')
SIOCGIFADDR = 0x8915   # Linux: an interface's IPv4 address


def is_tailscale(address):
    try:
        return ipaddress.ip_address(str(address)) in TAILSCALE
    except ValueError:
        return False


def interface_ipv4(name):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        packed = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, struct.pack('256s', name.encode()[:15]))
    return socket.inet_ntoa(packed[20:24])


def tailscale_ipv4(run=subprocess.run, interface=interface_ipv4, names=None):
    """This computer's Tailscale IPv4 address now, or None (Tailscale not installed, signed out or stopped).
    Read from the tailscale0 network interface; else asked of the tailscale command."""
    if names is None:
        try:
            names = [n for _, n in socket.if_nameindex() if n.startswith('tailscale')]
        except OSError:
            names = []
    for name in names or ['tailscale0']:
        try:
            address = interface(name)
        except OSError:
            continue
        if is_tailscale(address):
            return address
    try:
        out = run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return next((a for a in (out.stdout or '').split() if is_tailscale(a)), None)


class Listener:
    """The dashboard's addresses ("listen" in config.json). One that isn't there yet (a hotspot, the network still
    starting) is tried again every 30 s while the others work; a Tailscale address that's gone is replaced by the
    Tailscale address now."""

    def __init__(self, open_site, hosts, port, log, tailscale=tailscale_ipv4, event=None, every=30):
        self.open_site, self.port, self.log, self.tailscale, self.event, self.every = open_site, port, log, tailscale, event, every
        self.hosts = list(dict.fromkeys(hosts))
        self.sites, self.missing, self.followed = {}, [], None

    async def open(self, host, quiet=False):
        try:
            self.sites[host] = await self.open_site(host, self.port)
        except OSError as exc:
            if not quiet:   # (retries every 30 s stay out of the log; the status icon shows what's missing)
                self.log.error('Cannot listen on %s:%s (%s).', host, self.port, exc)
            return False
        self.log.info('3D Printer Management listening on %s:%s', host, self.port)
        return True

    async def start(self):
        for host in self.hosts:
            if not await self.open(host):
                self.missing.append(host)
        await self.follow()
        if not self.sites:
            raise RuntimeError(f"The dashboard could not listen on {', '.join(self.hosts)} port {self.port}. Is another copy running, "
                               'or is "listen" in config.json wrong?')
        if self.missing:
            self.log.warning('Retrying %s every %d s.', ', '.join(self.missing), self.every)

    async def follow(self):
        """A configured Tailscale address that isn't on this computer: listen on the Pi's Tailscale address now."""
        gone = [h for h in self.missing if is_tailscale(h)]
        if not gone and not self.followed:
            return
        current = await asyncio.to_thread(self.tailscale)
        if self.followed and current != self.followed:   # changed again, or the configured address is back
            site = self.sites.pop(self.followed, None)
            self.followed = None
            if site:
                try:
                    await site.stop()
                except Exception:
                    pass
        if not gone or not current or current in self.sites:
            return
        if await self.open(current):
            self.followed = current
            text = (f"Tailscale gave this Pi a new address: the dashboard is at http://{current}:{self.port} "
                    f"(\"listen\" in config.json still says {', '.join(gone)}).")
            self.log.warning(text)
            if self.event:
                self.event('Server', 'Dashboard address changed', text)

    async def watch(self):
        while True:
            await asyncio.sleep(self.every)
            for host in list(self.missing):
                if await self.open(host, quiet=True):
                    self.missing.remove(host)
            try:
                await self.follow()
            except Exception as exc:
                self.log.warning('Checking the Tailscale address failed: %s', exc)

    def status(self):
        """The status icon: addresses the dashboard can't open, and a Tailscale address it follows."""
        items = []
        if self.followed:
            items.append(dict(key='listen-tailscale', level='info', title=f'Dashboard at http://{self.followed}:{self.port}',
                              detail=f"Tailscale gave this Pi a new address. \"listen\" in config.json still has the old one "
                                     f"({', '.join(h for h in self.missing if is_tailscale(h))}): put {self.followed} there."))
        for host in self.missing:
            if is_tailscale(host) and self.followed:
                continue
            items.append(dict(key=f'listen:{host}', level='warn', title=f"The dashboard isn't on {host}",
                              detail=(f'Tailscale isn\'t connected on this Pi (or has another address): run "tailscale status".' if is_tailscale(host)
                                      else f'{host} isn\'t an address of this Pi (yet). Tried again every {self.every} s; check "listen" in config.json.')))
        return items
