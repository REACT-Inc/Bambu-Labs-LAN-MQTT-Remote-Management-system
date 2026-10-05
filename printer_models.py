"""Conservative LAN control defaults for Bambu printer families.

Firmware telemetry takes precedence for features such as air-duct fans. New
models can still use the common MQTT/FTPS functions without an exact profile.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    nozzle: int = 300
    bed: int = 80
    chamber: int = 0
    legacy_fans: tuple = (1,)
    camera: str = ''
    ftp_tls_unwrap: bool = False


PROFILES = {
    'A1 mini': Profile('A1 mini', camera='jpeg_tcp'),
    'A1': Profile('A1', bed=100, camera='jpeg_tcp'),
    'A2L': Profile('A2L', camera='jpeg_tcp'),
    'P1P': Profile('P1P', bed=100, camera='jpeg_tcp'),
    'P1S': Profile('P1S', bed=100, legacy_fans=(1, 2, 3), camera='jpeg_tcp'),
    'P2S': Profile('P2S', bed=110, legacy_fans=(), camera='rtsp'),
    'X1': Profile('X1', bed=110, legacy_fans=(1, 2, 3), camera='rtsp'),
    'X1C': Profile('X1C', bed=110, legacy_fans=(1, 2, 3), camera='rtsp'),
    'X1E': Profile('X1E', nozzle=320, bed=110, chamber=60, legacy_fans=(1, 2, 3), camera='rtsp'),
    'X2D': Profile('X2D', bed=120, chamber=65, legacy_fans=(), camera='rtsp'),
    'H2S': Profile('H2S', nozzle=350, bed=120, chamber=65, legacy_fans=(), camera='rtsp'),
    'H2D': Profile('H2D', nozzle=350, bed=120, chamber=65, legacy_fans=(), camera='rtsp', ftp_tls_unwrap=True),
    'H2D Pro': Profile('H2D Pro', nozzle=350, bed=120, chamber=65, legacy_fans=(), camera='rtsp', ftp_tls_unwrap=True),
    'H2C': Profile('H2C', nozzle=350, bed=120, chamber=65, legacy_fans=(), camera='rtsp'),
}

UNKNOWN = Profile('Unknown')
ALIASES = {'x1carbon': 'X1C', 'x1e': 'X1E', 'x1enterprise': 'X1E', 'h2dpro': 'H2D Pro'}


def normalize(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def profile(printer):
    explicit = printer.get('model')
    if explicit:
        key = normalize(explicit)
    else:
        # Keep old configurations with descriptive names such as BOB (H2D).
        key = normalize(printer.get('name', ''))
    names = {normalize(name): name for name in PROFILES}
    names.update(ALIASES)
    if key.startswith('bambulab'):
        key = key[len('bambulab'):]
    for suffix in ('laserfullcombo', 'lasercombo', 'combo', 'laser'):
        if key.endswith(suffix) and key[:-len(suffix)] in names:
            key = key[:-len(suffix)]
            break
    if key in names:
        return PROFILES[names[key]]
    if explicit:
        return UNKNOWN
    for alias in sorted(names, key=len, reverse=True):
        if key.endswith(alias):
            return PROFILES[names[alias]]
    return UNKNOWN


def camera_type(printer):
    configured = printer.get('camera_type', '')
    return profile(printer).camera if configured == 'auto' else configured
