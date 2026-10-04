"""Left/right nozzle information for dual-nozzle printers such as the H2D (#5).

Decoded the way Bambu Studio does (DeviceCore/DevExtruderSystem.cpp, DevFilaSystem.cpp, DevDefs.h):

- print.device.extruder.state: bits 0-3 = number of nozzles, bits 4-7 = the nozzle in use.
- print.device.extruder.info[]: one entry per nozzle; id 0 is the right (main) nozzle, id 1 the left.
  "snow" is the slot loaded in that nozzle: bits 8-15 AMS id, bits 0-7 slot (0xFFFF = none).
- print.ams.ams[].info (hex string): bits 8-11 = the nozzle that AMS feeds (0xE = either, through a switch).
- print.vir_slot[]: the external spools; id 255 feeds the right nozzle, 254 the left (single-nozzle printers
  only report print.vt_tray, which is 255).
"""

RIGHT, LEFT = 0, 1
SIDES = {RIGHT: 'Right', LEFT: 'Left'}
EXTERNAL_RIGHT, EXTERNAL_LEFT = 255, 254
EXTERNAL_SIDE = {EXTERNAL_RIGHT: RIGHT, EXTERNAL_LEFT: LEFT}


def _int(value, base=10):
    try:
        return int(str(value), base) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def extruder(data):
    device = data.get('device') if isinstance(data.get('device'), dict) else {}
    return device.get('extruder') if isinstance(device.get('extruder'), dict) else {}


def nozzle_count(data):
    ext = extruder(data)
    info = ext.get('info') if isinstance(ext.get('info'), list) else []
    state = _int(ext.get('state'))
    return max(len(info), state & 0xF if state is not None else 0, 1)


def is_dual(data):
    return nozzle_count(data) >= 2


def active_nozzle(data):
    """The nozzle currently in use (0 right, 1 left), or None when unknown or single-nozzle."""
    state = _int(extruder(data).get('state'))
    if not is_dual(data) or state is None:
        return None
    nozzle = state >> 4 & 0xF
    return nozzle if nozzle in SIDES else None


def ams_nozzle(unit):
    """The nozzle an AMS unit feeds: 0 right, 1 left, 'both' (through a filament switch), or None if unknown."""
    info = _int(unit.get('info'), 16)
    if info is None:
        return None
    nozzle = info >> 8 & 0xF
    return 'both' if nozzle == 0xE else nozzle if nozzle in SIDES else None


def external_id(value):
    # Bambu Studio's MachineObject::parse_vt_tray: ids may be encoded as (ams << 8) | slot.
    number = _int(value)
    if number is None:
        return None
    return (number >> 8) + (number & 0xFF) if number >> 8 > 0 else number


def external_spools(data):
    """[(ams_id, tray, nozzle or None)] for the external spool holders, right first."""
    slots = data.get('vir_slot')
    if isinstance(slots, list) and slots:
        found = {}
        for tray in slots:
            if isinstance(tray, dict) and external_id(tray.get('id')) in EXTERNAL_SIDE:
                found[external_id(tray.get('id'))] = tray
        dual = is_dual(data)
        return [(ams_id, found[ams_id], EXTERNAL_SIDE[ams_id] if dual else None)
                for ams_id in (EXTERNAL_RIGHT, EXTERNAL_LEFT) if ams_id in found]
    tray = data.get('vt_tray')
    return [(EXTERNAL_RIGHT, tray, None)] if isinstance(tray, dict) and tray else []


def loaded_slots(data):
    """{(ams_id, slot): nozzle} for what each nozzle of a dual-nozzle printer has loaded."""
    if not is_dual(data):
        return {}
    result = {}
    for item in extruder(data).get('info') or []:
        if not isinstance(item, dict):
            continue
        nozzle, now = _int(item.get('id')), _int(item.get('snow'))
        if nozzle not in SIDES or now is None or now == 0xFFFF:
            continue
        ams_id, slot = now >> 8 & 0xFF, now & 0xFF
        if ams_id in EXTERNAL_SIDE:
            slot = 0
        elif slot == 0xFF:
            continue
        result[(ams_id, slot)] = nozzle
    return result


def side_label(nozzle):
    return 'Both nozzles' if nozzle == 'both' else f'{SIDES[nozzle]} nozzle' if nozzle in SIDES else ''


# ---- Multi-hotend details (dual-nozzle printers such as the H2D / H2D Pro / H2C / X2D) -------------------------
# Decoded as in Bambu Studio's DevExtruderSystem.cpp / DevNozzleSystem.cpp:
# - device.extruder.info[].temp: bits 0-15 current, bits 16-31 target (°C); "hnow": the hotend mounted on that nozzle.
# - device.nozzle.info[]: one entry per hotend. "id" hex digit 0 = hotend number, digit 1 = 1 for a hotend parked
#   in the H2C's rack; "diameter" in mm; "type" either a name (stainless_steel) or a code like "HH01" (char 2 = flow,
#   chars 3-4 = material).
_FLOWS = {'S': 'standard', 'A': 'standard', 'X': 'standard', 'H': 'high flow', 'E': 'high flow'}
_MATERIALS = {'00': 'stainless steel', '01': 'hardened steel', '05': 'tungsten carbide',
              'stainless_steel': 'stainless steel', 'hardened_steel': 'hardened steel', 'tungsten_carbide': 'tungsten carbide'}


def hotend(entry):
    """One device.nozzle.info entry as {'slot', 'rack', 'diameter', 'material', 'flow', 'wear', 'label'}, or None."""
    raw = _int(entry.get('id')) if isinstance(entry, dict) else None
    if raw is None:
        return None
    kind = str(entry.get('type') or '')
    material = _MATERIALS.get(kind) or (_MATERIALS.get(kind[2:4], '') if len(kind) >= 4 else '')
    flow = _FLOWS.get(kind[1:2], '') if len(kind) >= 4 and kind not in _MATERIALS else ''
    try:
        diameter = round(float(entry.get('diameter')), 2)
    except (TypeError, ValueError):
        diameter = None
    wear = _int(entry.get('wear'))
    label = ' '.join(p for p in (f'{diameter:g} mm' if diameter else '', material) if p) or 'not reported'
    if flow == 'high flow':
        label += ', high flow'
    return dict(slot=raw & 0xF, rack=(raw >> 4) & 0xF == 1, diameter=diameter, material=material, flow=flow,
                wear=wear, label=label)


def hotends(data):
    """(mounted {hotend number: hotend}, rack [hotends]) from device.nozzle.info."""
    device = data.get('device') if isinstance(data.get('device'), dict) else {}
    nozzle = device.get('nozzle') if isinstance(device.get('nozzle'), dict) else {}
    mounted, rack = {}, []
    for entry in nozzle.get('info') or []:
        item = hotend(entry)
        if item:
            (rack.append(item) if item['rack'] else mounted.__setitem__(item['slot'], item))
    return mounted, sorted(rack, key=lambda h: h['slot'])


def nozzles(data):
    """Per-nozzle details for a dual-nozzle printer, right (0) then left (1); [] for single-nozzle printers."""
    if not is_dual(data):
        return []
    mounted, _ = hotends(data)
    loaded = {nozzle: key for key, nozzle in loaded_slots(data).items()}
    active, result = active_nozzle(data), []
    for item in sorted((i for i in extruder(data).get('info') or [] if isinstance(i, dict)), key=lambda i: _int(i.get('id')) or 0):
        nozzle = _int(item.get('id'))
        if nozzle not in SIDES:
            continue
        temp = _int(item.get('temp'))
        current = temp & 0xFFFF if temp is not None else None
        target = temp >> 16 & 0xFFFF if temp is not None else None
        fitted = _int(item.get('hnow'))
        hardware = mounted.get(fitted if fitted is not None and fitted in mounted else nozzle)
        slot = loaded.get(nozzle)
        if slot is None:
            filament = ''
        elif slot[0] in EXTERNAL_SIDE:
            filament = f'{SIDES[EXTERNAL_SIDE[slot[0]]]} external spool'
        else:
            filament = f'AMS {slot[0] + 1} slot {slot[1] + 1}'
        result.append(dict(nozzle=nozzle, side=SIDES[nozzle], current=current, target=target, active=nozzle == active,
                           hotend=hardware, filament=filament))
    return result


def summary(data):
    """Everything the dashboard needs to label a printer's filament by nozzle (JSON-friendly)."""
    ams = data.get('ams')
    units = ams if isinstance(ams, list) else (ams or {}).get('ams', []) if isinstance(ams, dict) else []
    dual = is_dual(data)
    return {
        'dual': dual,
        'active': active_nozzle(data),
        'ams': {str(u.get('id')): ams_nozzle(u) for u in units if isinstance(u, dict)} if dual else {},
        'external': [{'ams': ams_id, 'nozzle': nozzle, 'tray': tray} for ams_id, tray, nozzle in external_spools(data)],
        'loaded': [{'ams': a, 'slot': s, 'nozzle': n} for (a, s), n in loaded_slots(data).items()],
        'nozzles': nozzles(data),
        'rack': hotends(data)[1],
    }
