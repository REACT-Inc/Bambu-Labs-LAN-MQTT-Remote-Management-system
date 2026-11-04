"""Signing in with Google through Cloudflare Access, at your own address (printers.<your domain>).

Cloudflare Tunnel (cloudflared on the Pi) serves the dashboard at the public hostname without opening a port, and a
Cloudflare Access application in front of it asks for a Google sign-in before anything reaches the Pi. Each request
then carries Cloudflare's signed token (the Cf-Access-Jwt-Assertion header, or the CF_Authorization cookie); this
module checks it: signed by your Cloudflare team's keys (RS256), for your Access application (aud), not expired.

config.json, "cloudflare_access":
- team_domain: your Zero Trust team domain, e.g. "yourteam.cloudflareaccess.com".
- aud: the Access application's Application Audience (AUD) tag (a list is allowed, for several applications).
- hostname: the public hostname, e.g. "printers.example.org".
- admins: Google accounts that may use management (Discord, team tools, laptops, the server, settings, updates):
  emails, or "@example.org" for a whole domain. Empty: everyone Access lets in.
- lan: what the dashboard's local addresses (IP, .local, Tailscale) may do without Cloudflare: "printing" (the
  default: printers, queue, slicer, activity), "full", or "off".

See docs/cloudflare-access.md.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time

import aiohttp

LEEWAY = 60                 # seconds of clock difference allowed on exp/nbf
KEYS_FOR = 3600             # Cloudflare rotates its keys every 6 weeks; look again hourly
KEYS_RETRY = 60             # an unknown key id looks again at most once a minute
LAN_MODES = ('printing', 'full', 'off')
# DigestInfo prefix for SHA-256 in EMSA-PKCS1-v1_5 (RFC 8017, section 9.2)
SHA256_PREFIX = bytes.fromhex('3031300d060960864801650304020105000420')


def b64url(text):
    text = text if isinstance(text, bytes) else text.encode()
    return base64.urlsafe_b64decode(text + b'=' * (-len(text) % 4))


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
        self.lan = config.get('lan', 'printing') if config.get('lan', 'printing') in LAN_MODES else 'printing'
        self.enabled = bool(self.team and self.aud and self.hostname)
        self.fetch, self.clock = fetch or self.fetch_keys, clock
        self.keys, self.keys_at, self.tried_at = {}, 0.0, 0.0
        self.lock = asyncio.Lock()

    @property
    def issuer(self):
        return f'https://{self.team}'

    def public(self):
        return dict(enabled=self.enabled, hostname=self.hostname, lan=self.lan, admins=len(self.admins))

    # ---- which requests came through Cloudflare ----

    def through_cloudflare(self, request):
        """The public hostname, or Cloudflare's own headers (a tunnel request can't drop them): Access must have
        signed these. Anything else came straight to one of the Pi's own addresses."""
        host = request.host.lower().rsplit(':', 1)[0] if request.host.count(':') == 1 else request.host.lower()
        return host.rstrip('.') == self.hostname or 'Cf-Ray' in request.headers or 'Cf-Connecting-Ip' in request.headers

    def token(self, request):
        return request.headers.get('Cf-Access-Jwt-Assertion') or request.cookies.get('CF_Authorization') or ''

    def is_admin(self, email):
        email = str(email or '').lower()
        if not email:
            return False
        if not self.admins:
            return True
        return any(email == a or (a.startswith('@') and email.endswith(a)) for a in self.admins)

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
                data = await self.fetch()
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
        return {'email': email, 'admin': self.is_admin(claims.get('email'))}
