"""Read a sliced Bambu Studio / Orca .3mf for the Queue a print dialog (#7).

- inspect(): the sliced plates (name, print time, weight, filaments, thumbnail) and the printer model it was
  sliced for, from Metadata/slice_info.config, Metadata/model_settings.config and Metadata/plate_N.gcode.
- plate_thumbnail(): Metadata/plate_N.png for the plate picker.
- ams_slots(): every AMS slot (AMS and AMS HT units), named and numbered like Bambu Studio, for the dashboard's tray picker.
- suggest_mapping(): match a plate's filaments to the printer's loaded AMS trays by material and colour.

AMS mapping format (as Bambu Studio sends it): one entry per project filament, in filament order; the entry is
the global tray number (AMS unit * 4 + slot) or -1 for a filament the plate doesn't use.
"""
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_METADATA = 4 * 1024 * 1024   # never parse an oversized metadata file
MAX_THUMBNAIL = 2 * 1024 * 1024


def _read(archive, name, limit=MAX_METADATA):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    return archive.read(info)


def _xml(data):
    if not data or b'<!DOCTYPE' in data or b'<!ENTITY' in data:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _metadata(element):
    return {m.get('key'): m.get('value') for m in element.findall('metadata') if m.get('key')}


def _number(value, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _colour(value):
    value = str(value or '').lstrip('#').upper()
    return '#' + value[:6] if re.fullmatch(r'[0-9A-F]{6}([0-9A-F]{2})?', value) else ''


def inspect(path):
    """{'plates': [...], 'model': model_key or '', 'sliced': bool}. Plates without G-code are listed but not sliced."""
    from job_transfer import sliced_for   # reads the printer model the same way Print on another printer does
    plates = {}
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for name in names:
                match = re.fullmatch(r'Metadata/plate_(\d{1,3})\.gcode', name)
                if match:
                    plates.setdefault(int(match.group(1)), {})['gcode'] = True
            info = _xml(_read(archive, 'Metadata/slice_info.config'))
            for plate in (info.findall('plate') if info is not None else []):
                meta = _metadata(plate)
                index = _number(meta.get('index'), int)
                if not index or not 1 <= index <= 100:
                    continue
                entry = plates.setdefault(index, {})
                entry['prediction'] = _number(meta.get('prediction'), int)
                entry['weight'] = _number(meta.get('weight'))
                entry['filaments'] = [dict(id=_number(f.get('id'), int), type=str(f.get('type') or '')[:20],
                                           colour=_colour(f.get('color')), used_g=_number(f.get('used_g')))
                                      for f in plate.findall('filament') if _number(f.get('id'), int)]
            settings = _xml(_read(archive, 'Metadata/model_settings.config'))
            for plate in (settings.findall('plate') if settings is not None else []):
                meta = _metadata(plate)
                index = _number(meta.get('plater_id'), int)
                if not index or not 1 <= index <= 100:
                    continue
                entry = plates.setdefault(index, {})   # also lists plates that weren't sliced, so the picker can say so
                if meta.get('plater_name'):
                    entry['name'] = str(meta['plater_name'])[:80]
            for index in plates:
                plates[index]['thumbnail'] = f'Metadata/plate_{index}.png' in names
    except (OSError, zipfile.BadZipFile):
        raise ValueError("That isn't a readable .3mf file.")
    result = [dict(index=i, name=p.get('name') or f'Plate {i}', sliced=bool(p.get('gcode')), prediction=p.get('prediction'),
                   weight=p.get('weight'), filaments=p.get('filaments', []), thumbnail=bool(p.get('thumbnail')))
              for i, p in sorted(plates.items())]
    return dict(plates=result, model=sliced_for(path), sliced=any(p['sliced'] for p in result))


def plate_thumbnail(path, index):
    try:
        with zipfile.ZipFile(path) as archive:
            return _read(archive, f'Metadata/plate_{int(index)}.png', MAX_THUMBNAIL)
    except (OSError, zipfile.BadZipFile, ValueError):
        return None


def _rgb(colour):
    colour = _colour(colour)
    return tuple(int(colour[k:k + 2], 16) for k in (1, 3, 5)) if colour else None


def _distance(a, b):
    a, b = _rgb(a), _rgb(b)
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5 if a and b else 999


HT_FIRST, HT_LAST = 128, 135   # AMS HT units (one slot each), as Bambu Studio numbers them


def _letter(index):
    return chr(ord('A') + index) if 0 <= index < 26 else str(index + 1)


def ams_slots(data):
    """Every AMS slot the printer reports, loaded or empty, named and numbered the way Bambu Studio and the printer's
    screen do: AMS units are A, B, C... with slots 1-4 (mapping number = unit * 4 + slot), an AMS HT is HT-A, HT-B...
    (mapping number = its id, 128-135; DevMapping / GUI_App::transition_tridid).

    [{'tray', 'unit', 'slot', 'short': 'B3' / 'HT-A', 'unit_name': 'AMS B' / 'AMS HT-A', 'label', 'loaded', 'type',
      'colour', 'brand', 'remain': 0-100 or None, 'nozzle': 'Left' / 'Right' / 'Both' / None (single nozzle)}]
    """
    import filament_sides
    ams = data.get('ams')
    units = ams if isinstance(ams, list) else (ams or {}).get('ams', []) if isinstance(ams, dict) else []
    dual = filament_sides.is_dual(data)
    slots = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = _number(unit.get('id'), int)
        if unit_id is None:
            continue
        ht = HT_FIRST <= unit_id <= HT_LAST
        if not ht and not 0 <= unit_id <= 63:
            continue
        side = filament_sides.ams_nozzle(unit) if dual else None
        nozzle = 'Both' if side == 'both' else filament_sides.SIDES.get(side) if side is not None else None
        for tray in unit.get('tray') or []:
            slot = _number((tray or {}).get('id'), int)
            if slot is None or not 0 <= slot <= 3 or (ht and slot != 0):
                continue
            remain = _number(tray.get('remain'), int)
            name = f'HT-{_letter(unit_id - HT_FIRST)}' if ht else _letter(unit_id)
            slots.append(dict(
                tray=unit_id if ht else unit_id * 4 + slot, unit=unit_id, slot=slot,
                short=name if ht else f'{name}{slot + 1}', unit_name=f'AMS {name}',
                label=f'AMS {name}' if ht else f'AMS {unit_id + 1} slot {slot + 1}',
                loaded=bool(tray.get('tray_type')), type=str(tray.get('tray_type') or '').upper()[:20],
                colour=_colour(tray.get('tray_color')), brand=str(tray.get('tray_sub_brands') or '')[:40],
                remain=remain if remain is not None and 0 <= remain <= 100 else None, nozzle=nozzle))
    return slots


def loaded_trays(data):
    """The printer's loaded AMS slots (see ams_slots())."""
    return [slot for slot in ams_slots(data) if slot['loaded']]


def suggest_mapping(plate, data):
    """Suggest the AMS mapping for one plate on one printer.

    Each filament the plate uses gets the loaded tray with the same material and the closest colour. Returns
    {'mapping': '0,-1,2' or '', 'rows': [...], 'complete': bool, 'message': text}.
    """
    filaments = [f for f in plate.get('filaments', []) if f.get('id')]
    slots = ams_slots(data)
    trays = [slot for slot in slots if slot['loaded']]
    if not filaments:
        return dict(mapping='', rows=[], complete=False, slots=slots, message="The file doesn't list the plate's filaments; enter the mapping yourself.")
    if not trays:
        return dict(mapping='', rows=[dict(filament=f['id'], type=f.get('type'), colour=f.get('colour'), used_g=f.get('used_g'), tray=None,
                                           tray_label='', tray_colour='', match='none') for f in sorted(filaments, key=lambda f: f['id'])],
                    complete=False, slots=slots, message='No loaded AMS slots reported by this printer.')
    rows, mapping = [], [-1] * max(f['id'] for f in filaments)
    for filament in sorted(filaments, key=lambda f: f['id']):
        same = [t for t in trays if t['type'] == str(filament.get('type') or '').upper()]
        best = min(same, key=lambda t: _distance(t['colour'], filament.get('colour'))) if same else None
        if best:
            mapping[filament['id'] - 1] = best['tray']
            close = _distance(best['colour'], filament.get('colour')) < 60
        rows.append(dict(filament=filament['id'], type=filament.get('type'), colour=filament.get('colour'), used_g=filament.get('used_g'),
                         tray=best['tray'] if best else None, tray_label=best['label'] if best else '', tray_short=best['short'] if best else '',
                         tray_colour=best['colour'] if best else '',
                         match='exact' if best and close else 'material' if best else 'none'))
    complete = all(r['tray'] is not None for r in rows)
    message = ('Matched every filament to a loaded AMS slot.' if complete and all(r['match'] == 'exact' for r in rows) else
               'Matched by material; check the colours.' if complete else
               'Some filaments have no loaded slot of the same material. Load them or enter the mapping yourself.')
    return dict(mapping=','.join(str(t) for t in mapping) if complete else '', rows=rows, complete=complete, slots=slots, message=message)
