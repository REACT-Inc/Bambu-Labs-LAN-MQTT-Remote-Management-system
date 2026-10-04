"""Bambu Lab printer models: what the app needs to know about each one (#6).

Everything that depends on the model (temperature limits, chamber heating, fan layout, FTPS settings, how a
printer and a sliced file are recognised) reads it from here, instead of each module checking names itself.

Only the H2D, A1 and A1 mini have been tested on real printers. The others use published specs and
conservative limits, and are marked tested=False; a printer's limits can be overridden in config.json with
"limits": {"nozzle": 300, "bed": 100}.

A printer's model comes from its config.json "model" (e.g. "X1 Carbon", "P1S"), otherwise from the first
3 characters of its serial number, otherwise from its name.
"""
import re

UNKNOWN = dict(key='', name='Unknown model', series='', nozzle=300, bed=80, chamber=0, camera=None,
               aux_fan=False, chamber_fan=False, dual_nozzle=False, ftp_unwrap=False, tested=False)


def _model(key, name, series, nozzle, bed, camera, *, chamber=0, aux_fan=False, chamber_fan=False,
           dual_nozzle=False, ftp_unwrap=False, tested=False, serials=(), studio_ids=(), aliases=()):
    return dict(UNKNOWN, key=key, name=name, series=series, nozzle=nozzle, bed=bed, chamber=chamber, camera=camera,
                aux_fan=aux_fan, chamber_fan=chamber_fan, dual_nozzle=dual_nozzle, ftp_unwrap=ftp_unwrap, tested=tested,
                serials=tuple(serials), studio_ids=tuple(studio_ids), aliases=tuple(aliases))


# Values from Bambu Studio's resources/printers/<model_id>.json ("sn_prefix", "nozzle_temp_range", "bed_temp_range",
# "support_chamber_temp_edit_range", "support_aux_fan", "support_chamber_fan", ipcam liveview "local" = JPEG camera).
# Bed maximums Studio doesn't list come from Bambu's published specs (conservative where unsure).
# camera: the camera_type to use in config.json ('jpeg_tcp' = TLS JPEG stream on port 6000, 'rtsp' = RTSPS on 322).
# chamber: highest active chamber-heating target the app sends (set_ctt, 40 °C and up, like the H2D); 0 = not offered.
# serials: first 3 characters of the serial number. studio_ids: Bambu Studio "printer_model_id" in sliced files.
MODELS = {m['key']: m for m in [
    _model('a1mini', 'A1 mini', 'A', 300, 80, 'jpeg_tcp', tested=True, serials=['030'], studio_ids=['N1'], aliases=['a1m']),
    _model('a1', 'A1', 'A', 300, 100, 'jpeg_tcp', tested=True, serials=['039'], studio_ids=['N2S']),
    _model('a2l', 'A2L', 'A', 300, 100, 'jpeg_tcp', serials=['26A'], studio_ids=['N9']),
    _model('p1p', 'P1P', 'P', 300, 100, 'jpeg_tcp', aux_fan=True, chamber_fan=True, serials=['01S'], studio_ids=['C11']),
    _model('p1s', 'P1S', 'P', 300, 100, 'jpeg_tcp', aux_fan=True, chamber_fan=True, serials=['01P'], studio_ids=['C12']),
    _model('p2s', 'P2S', 'P', 300, 110, 'rtsp', aux_fan=True, chamber_fan=True, serials=['22E'], studio_ids=['N7']),
    _model('x1', 'X1', 'X', 300, 110, 'rtsp', aux_fan=True, chamber_fan=True, serials=['00W'], studio_ids=['BL-P002']),
    _model('x1c', 'X1 Carbon', 'X', 300, 110, 'rtsp', aux_fan=True, chamber_fan=True, serials=['00M'],
           studio_ids=['BL-P001'], aliases=['x1carbon']),
    # The X1E heats its chamber (to 60 °C) but Studio gives no switch-on threshold for it, so the app doesn't send set_ctt.
    _model('x1e', 'X1E', 'X', 320, 120, 'rtsp', aux_fan=True, chamber_fan=True, serials=['03W'], studio_ids=['C13']),
    _model('x2d', 'X2D', 'X', 300, 120, 'rtsp', chamber=65, aux_fan=True, chamber_fan=True, dual_nozzle=True,
           serials=['20P'], studio_ids=['N6']),
    _model('h2d', 'H2D', 'H', 350, 120, 'rtsp', chamber=65, aux_fan=True, chamber_fan=True, dual_nozzle=True,
           ftp_unwrap=True, tested=True, serials=['094'], studio_ids=['O1D']),
    _model('h2dpro', 'H2D Pro', 'H', 350, 120, 'rtsp', chamber=65, aux_fan=True, chamber_fan=True, dual_nozzle=True,
           ftp_unwrap=True, serials=['239'], studio_ids=['O1E']),
    _model('h2s', 'H2S', 'H', 350, 120, 'rtsp', chamber=65, aux_fan=True, chamber_fan=True, ftp_unwrap=True,
           serials=['093'], studio_ids=['O1S']),
    _model('h2c', 'H2C', 'H', 350, 120, 'rtsp', chamber=65, aux_fan=True, chamber_fan=True, dual_nozzle=True,
           ftp_unwrap=True, serials=['31B'], studio_ids=['O1C']),
]}
_ALIASES = {alias: m['key'] for m in MODELS.values() for alias in m['aliases']}
_SERIALS = {s: m['key'] for m in MODELS.values() for s in m['serials']}
_STUDIO_IDS = {i: m['key'] for m in MODELS.values() for i in m['studio_ids']}


def normalize(text):
    """'Bambu Lab X1 Carbon' / 'X1-Carbon' -> 'x1c'; 'A1 mini' -> 'a1mini'."""
    key = re.sub(r'[^a-z0-9]', '', str(text or '').lower()).removeprefix('bambulab')
    return _ALIASES.get(key, key)


def key_from_text(text):
    """The model named in free text such as a printer name ('Lab X1C #2'), or ''. Longest match wins."""
    key = normalize(text)
    if key in MODELS:
        return key
    for candidate in sorted(list(MODELS) + list(_ALIASES), key=len, reverse=True):
        if candidate in key:
            return _ALIASES.get(candidate, candidate)
    return ''


def key_from_studio_id(model_id):
    return _STUDIO_IDS.get(str(model_id or ''), '')


def key_for(config, name=''):
    """The model key for a printer's config.json entry: its "model", else its serial number, else its name."""
    config = config or {}
    if config.get('model'):
        return key_from_text(config['model'])
    serial = str(config.get('serial') or '')[:3]
    if serial in _SERIALS:
        return _SERIALS[serial]
    return key_from_text(config.get('name') or name)


def info(config, name=''):
    """The model's details (UNKNOWN's conservative defaults if the model isn't recognised)."""
    return MODELS.get(key_for(config, name), UNKNOWN)


def printer(core, name):
    return info(getattr(core, 'printer_config', lambda n: None)(name) or {}, name)


def label(key):
    return MODELS[key]['name'] if key in MODELS else (key.upper() if key else 'unknown model')


def limits(core, name):
    """Nozzle / bed / chamber maximums for a printer, with config.json "limits" overrides (bounded)."""
    config = getattr(core, 'printer_config', lambda n: None)(name) or {}
    model = info(config, name)
    result = dict(nozzle=model['nozzle'], bed=model['bed'], chamber=model['chamber'])
    overrides = config.get('limits') if isinstance(config.get('limits'), dict) else {}
    for kind, ceiling in (('nozzle', 400), ('bed', 150)):
        value = overrides.get(kind)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= ceiling:
            result[kind] = int(value)
    return result
