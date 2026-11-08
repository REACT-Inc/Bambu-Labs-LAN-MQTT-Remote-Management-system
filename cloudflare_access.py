"""Signing in with Google through Cloudflare Access, at your own address (printers.<your domain>).

Cloudflare Tunnel (cloudflared on the Pi) serves the dashboard at the public hostname without opening a port, and a
Cloudflare Access application in front of it asks for a Google sign-in before anything reaches the Pi. Each request
then carries Cloudflare's signed token (the Cf-Access-Jwt-Assertion header, or the CF_Authorization cookie); this
module checks it: signed by your Cloudflare team's keys (RS256), for your Access application (aud), not expired.

The settings (Settings & help → Google sign-in saves them in DATA_DIR/cloudflare_access.json, as does
cloudflare-tunnel.sh; "cloudflare_access" in config.json is read when that file doesn't exist):
- team_domain: your Zero Trust team domain, e.g. "yourteam.cloudflareaccess.com".
- aud: the Access application's Application Audience (AUD) tag (a list is allowed, for several applications).
- hostname: the public hostname, e.g. "printers.example.org".
- admins: Google accounts that may use management (Discord, team tools, laptops, the server, settings, updates):
  emails, or "@example.org" for a whole domain. Empty: everyone Access lets in.
- operators: who else may print and control printers (queue, slicer, controls), the same way. Empty: everyone Access
  lets in. Anyone else may only look (view only).
- lan: what the dashboard's local addresses (IP, .local, Tailscale) may do without Cloudflare: "printing" (the
  default: printers, queue, slicer, activity), "full", "view" (look only), or "off".

See docs/cloudflare-access.md.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import time

import aiohttp

LEEWAY = 60                 # seconds of clock difference allowed on exp/nbf
KEYS_FOR = 3600             # Cloudflare rotates its keys every 6 weeks; look again hourly
KEYS_RETRY = 60             # an unknown key id looks again at most once a minute
LAN_MODES = ('printing', 'full', 'view', 'off')
TEAM = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com')
AUD = re.compile(r'[0-9a-f]{64}')
HOSTNAME = re.compile(r'(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
ADMIN = re.compile(r'(?:[^@\s]+)?@[a-z0-9.-]+\.[a-z]{2,63}')
# DigestInfo prefix for SHA-256 in EMSA-PKCS1-v1_5 (RFC 8017, section 9.2)
SHA256_PREFIX = bytes.fromhex('3031300d060960864801650304020105000420')


def b64url(text):
    text = text if isinstance(text, bytes) else text.encode()
    return base64.urlsafe_b64decode(text + b'=' * (-len(text) % 4))


def actor(request):
    """Who did it, for Activity: the Google account signed in through Cloudflare Access, else the dashboard."""
    identity = request.get('identity') if hasattr(request, 'get') else None
    return str(identity.get('email') or 'web administrator') if identity else 'web administrator'


def clean(data):
    """Settings from the dashboard (or the setup script), checked: (settings, problems)."""
    if not isinstance(data, dict):
        raise ValueError('Send the Google sign-in settings.')
    team = str(data.get('team_domain') or '').strip().lower().removeprefix('https://').rstrip('/')
    if team and '.' not in team:
        team += '.cloudflareaccess.com'   # just the team name
    hostname = str(data.get('hostname') or '').strip().lower().removeprefix('https://').rstrip('/').rstrip('.')
    aud = data.get('aud') or ''
    aud = [a.strip().lower() for a in (aud.replace('\n', ',').split(',') if isinstance(aud, str) else aud) if str(a).strip()]
    def people(value):   # one per line or comma-separated
        value = value or ''
        return [a.strip().lower() for a in (value.replace('\n', ',').split(',') if isinstance(value, str) else value) if str(a).strip()]
    admins, operators = people(data.get('admins')), people(data.get('operators'))
    lan = str(data.get('lan') or 'printing')
    if not TEAM.fullmatch(team):
        raise ValueError('Team domain: your Zero Trust team, like yourteam.cloudflareaccess.com (Zero Trust → Settings → Custom pages).')
    if not HOSTNAME.fullmatch(hostname):
        raise ValueError('Address: the full hostname, like printers.example.org.')
    if not aud or len(aud) > 5 or not all(AUD.fullmatch(a) for a in aud):
        raise ValueError('Application Audience (AUD) tag: the 64-character code on the Access application\'s Overview page.')
    if len(admins) > 100 or not all(ADMIN.fullmatch(a) for a in admins):
        raise ValueError('Admins: Google addresses (you@example.org) or whole domains (@example.org), one per line.')
    if len(operators) > 500 or not all(ADMIN.fullmatch(a) for a in operators):
        raise ValueError('Who may print: Google addresses (you@example.org) or whole domains (@example.org), one per line.')
    if lan not in LAN_MODES:
        raise ValueError('The Pi\'s own addresses: printing, full, view or off.')
    return dict(team_domain=team, aud=aud[0] if len(aud) == 1 else aud, hostname=hostname,
                admins=list(dict.fromkeys(admins)), operators=list(dict.fromkeys(operators)), lan=lan)


def rsa_verify(n, e, message, signature):
    """RSASSA-PKCS1-v1_5 with SHA-256 (RS256). Verifying needs only the public key, so plain Python does."""
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, 'big')
    if value >= n:
        return False
    decoded = pow(value, e, n).to_bytes(size, 'big')
    digest = SHA256_PREFIX + hashlib.sha256(message).digest()
    expected = b'\x00\x01' + b'\xff' * (size - len(digest) - 3) + b'\x00' + digest
    return hmac.compare_digest(decoded, expected)


class Access:
    def __init__(self, config=None, fetch=None, clock=time.time):
        config = config or {}
        self.team = str(config.get('team_domain') or '').strip().lower().removeprefix('https://').rstrip('/')
        aud = config.get('aud') or []
        self.aud = {str(a).strip() for a in ([aud] if isinstance(aud, str) else aud) if str(a).strip()}
        self.hostname = str(config.get('hostname') or '').strip().lower().rstrip('.')
        self.admins = [str(a).strip().lower() for a in config.get('admins') or [] if str(a).strip()]
        self.operators = [str(a).strip().lower() for a in config.get('operators') or [] if str(a).strip()]
        self.lan = config.get('lan', 'printing') if config.get('lan', 'printing') in LAN_MODES else 'printing'
        self.enabled = bool(self.team and self.aud and self.hostname)
        self.fetch, self.clock = fetch or self.fetch_keys, clock
        self.keys, self.keys_at, self.tried_at = {}, 0.0, 0.0
        self.error = ''   # why Cloudflare's keys couldn't be read last time
        self.lock = asyncio.Lock()
        self.config = dict(team_domain=self.team, aud=sorted(self.aud), hostname=self.hostname, admins=self.admins, operators=self.operators, lan=self.lan)

    @property
    def issuer(self):
        return f'https://{self.team}'

    def public(self):
        return dict(enabled=self.enabled, hostname=self.hostname, lan=self.lan, admins=len(self.admins))

    def status(self):
        return dict(enabled=self.enabled, keys=len(self.keys), keys_at=self.keys_at or None, error=self.error)

    # ---- which requests came through Cloudflare ----

    def through_cloudflare(self, request):
        """The public hostname, or Cloudflare's own headers (a tunnel request can't drop them): Access must have
        signed these. Anything else came straight to one of the Pi's own addresses."""
        host = request.host.lower().rsplit(':', 1)[0] if request.host.count(':') == 1 else request.host.lower()
        return host.rstrip('.') == self.hostname or 'Cf-Ray' in request.headers or 'Cf-Connecting-Ip' in request.headers

    def token(self, request):
        return request.headers.get('Cf-Access-Jwt-Assertion') or request.cookies.get('CF_Authorization') or ''

    @staticmethod
    def listed(email, people):
        """In the list (an email, or "@domain"); an empty list means everyone."""
        email = str(email or '').lower()
        if not email:
            return False
        return not people or any(email == a or (a.startswith('@') and email.endswith(a)) for a in people)

    def is_admin(self, email):
        return self.listed(email, self.admins)

    def can_print(self, email):
        """Queue, slice and control printers: admins, and the operators (everyone, with no operators listed)."""
        return self.is_admin(email) or self.listed(email, self.operators)

    # ---- checking Cloudflare's token ----

    async def fetch_keys(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f'{self.issuer}/cdn-cgi/access/certs') as response:
                response.raise_for_status()
                return await response.json()

    async def key(self, kid):
        now = self.clock()
        if kid in self.keys and now - self.keys_at < KEYS_FOR:
            return self.keys[kid]
        async with self.lock:
            now = self.clock()
            stale = now - self.keys_at >= KEYS_FOR
            if (stale or kid not in self.keys) and now - self.tried_at >= KEYS_RETRY:
                self.tried_at = now
                try:
                    data = await self.fetch()
                except Exception as exc:
                    self.error = f'Can\'t read Cloudflare\'s keys from {self.issuer}: {exc.__class__.__name__}: {exc}'[:300]
                    raise
                self.error = ''
                self.keys = {k['kid']: (int.from_bytes(b64url(k['n']), 'big'), int.from_bytes(b64url(k['e']), 'big'))
                             for k in data.get('keys', []) if k.get('kty') == 'RSA' and k.get('kid')}
                self.keys_at = now
        if kid not in self.keys:
            raise ValueError('Signed with a key this Cloudflare team doesn\'t have.')
        return self.keys[kid]

    async def verify(self, token):
        """The token's claims (email, ...) when Cloudflare Access signed it for this application; else ValueError."""
        try:
            head, body, signature = token.split('.')
            header, claims = json.loads(b64url(head)), json.loads(b64url(body))
            signature = b64url(signature)
        except (ValueError, TypeError) as exc:
            raise ValueError('Not a Cloudflare Access token.') from exc
        if not isinstance(header, dict) or not isinstance(claims, dict) or header.get('alg') != 'RS256':
            raise ValueError('Not a Cloudflare Access token.')
        n, e = await self.key(header.get('kid'))
        if not rsa_verify(n, e, f'{head}.{body}'.encode(), signature):
            raise ValueError('The Cloudflare Access token\'s signature is wrong.')
        aud = claims.get('aud')
        if not self.aud & set([aud] if isinstance(aud, str) else aud or []):
            raise ValueError('The Cloudflare Access token is for another application.')
        if claims.get('iss') != self.issuer:
            raise ValueError('The Cloudflare Access token is from another team.')
        now = self.clock()
        if not isinstance(claims.get('exp'), (int, float)) or claims['exp'] < now - LEEWAY:
            raise ValueError('The Cloudflare Access sign-in has expired: reload the page.')
        if isinstance(claims.get('nbf'), (int, float)) and claims['nbf'] > now + LEEWAY:
            raise ValueError('The Cloudflare Access token isn\'t valid yet (check the Pi\'s clock).')
        return claims

    async def warm(self):
        """Read Cloudflare's keys ahead of the first sign-in (and notice early when they can't be read)."""
        if not self.enabled:
            return
        try:
            self.tried_at = 0
            await self.key(None)
        except Exception:
            pass   # (self.error says why; an unknown key id is expected here)

    async def identity(self, request):
        """Who is signed in: {'email': ...} from a checked token, or None when the request has none or a bad one."""
        token = self.token(request)
        if not token:
            return None
        try:
            claims = await self.verify(token)
        except ValueError:
            return None
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, KeyError, TypeError):
            return None   # Cloudflare's keys couldn't be read: nobody is let in rather than everybody
        email = str(claims.get('email') or claims.get('common_name') or '')
        return {'email': email, 'admin': self.is_admin(claims.get('email')), 'print': self.can_print(claims.get('email'))}


async def probe(team, hostname, get=None):
    """Is https://<hostname> really behind Cloudflare Access? Asked from the Pi, like a visitor without a sign-in.
    -> (state, text): protected / open (the dashboard answers with no sign-in!) / tunnel / unreachable."""
    async def fetch(url):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url, allow_redirects=False) as response:
                return response.status, response.headers.get('Location', ''), (await response.text())[:2000]
    try:
        status, location, body = await (get or fetch)(f'https://{hostname}/')
    except Exception as exc:
        return 'unreachable', f'https://{hostname} doesn\'t answer ({exc.__class__.__name__}): check the domain is on Cloudflare and the tunnel has this public hostname.'
    if status in (301, 302, 303, 307, 308) and 'cloudflareaccess.com' in location:
        if f'//{team}/' not in location:
            return 'other-team', f'https://{hostname} asks for a sign-in, but with another Zero Trust team ({location.split("/")[2]}): check the team domain.'
        return 'protected', f'https://{hostname} asks for a Google sign-in through Cloudflare Access.'
    if status in (502, 503, 530) or 'error code: 1033' in body or 'Error 1033' in body:
        return 'tunnel', f'Cloudflare can\'t reach the Pi (HTTP {status}): the tunnel isn\'t connected, or its public hostname doesn\'t point at http://localhost:8080.'
    if status >= 500:
        return 'unreachable', f'https://{hostname} answered HTTP {status}.'
    # anything else got through without a sign-in (the Pi still refuses its API without Access's token)
    return 'open', (f'https://{hostname} reaches the Pi WITHOUT a Google sign-in (HTTP {status}): add a Cloudflare Access application '
                    'for this hostname (step 3 of the guide).')

