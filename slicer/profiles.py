"""OrcaSlicer's presets (printer, process and filament profiles), as its command line needs them.

OrcaSlicer ships its system presets in resources/profiles/<vendor>/{machine,process,filament}/*.json, listed in
<vendor>.json. Each preset only holds what differs from the preset it "inherits" from, and the command line's
--load-settings / --load-filaments read a file as it is, without following "inherits" (OrcaSlicer.cpp, CLI
load_config_file). So a worker flattens the chain here: the parent's settings, overridden by each child down to the
chosen preset. A parent is looked up in the preset's own vendor first, then in OrcaFilamentLibrary (the shared
filament presets Bambu filaments inherit from), as OrcaSlicer's PresetBundle does.

Changed settings (settings.py) are applied on top: the preset is then a user preset ("from": "User") that inherits
the system one, like a preset saved in OrcaSlicer.

Standard library only: this runs on the workers.
"""
import json
from pathlib import Path

KINDS = ('machine', 'process', 'filament')
LISTS = {'machine': 'machine_list', 'process': 'process_list', 'filament': 'filament_list'}
SHARED = 'OrcaFilamentLibrary'
MAX_DEPTH = 20


class ProfileError(ValueError):
    pass


class Profiles:
    def __init__(self, root, vendors=('BBL',)):
        """root: OrcaSlicer's resources/profiles folder. vendors: whose printers the catalog offers."""
        self.root, self.vendors = Path(root), tuple(vendors)
        self.index = {}   # vendor -> kind -> preset name -> its file
        self.cache = {}
        for vendor in dict.fromkeys(self.vendors + (SHARED,)):
            listing = self.root / f'{vendor}.json'
            try:
                data = json.loads(listing.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            self.index[vendor] = {kind: {item['name']: self.root / vendor / item['sub_path']
                                         for item in data.get(LISTS[kind]) or []
                                         if isinstance(item, dict) and item.get('name') and item.get('sub_path')}
                                  for kind in KINDS}
        if not any(v in self.index for v in self.vendors):
            raise ProfileError(f'No OrcaSlicer presets for {", ".join(self.vendors)} under {self.root}.')

    def find(self, kind, name, vendor=None):
        """(vendor, file) of a preset: in `vendor` first, then the configured vendors, then the shared library."""
        for v in dict.fromkeys(((vendor,) if vendor else ()) + self.vendors + (SHARED,)):
            path = self.index.get(v, {}).get(kind, {}).get(name)
            if path:
                return v, path
        raise ProfileError(f'OrcaSlicer has no {kind} preset called "{name}".')

    def raw(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ProfileError(f'Could not read the preset file {Path(path).name}: {exc}') from None
        if not isinstance(data, dict):
            raise ProfileError(f'The preset file {Path(path).name} is not a preset.')
        return data

    def flat(self, kind, name, vendor=None):
        """Every setting of a preset, with its parents' settings filled in."""
        key = (kind, name, vendor)
        if key not in self.cache:
            chain, seen = [], set()
            vendor_now, current = vendor, name
            while current:
                if current in seen or len(chain) >= MAX_DEPTH:
                    raise ProfileError(f'The {kind} preset "{name}" inherits in a loop.')
                seen.add(current)
                vendor_now, path = self.find(kind, current, vendor_now)
                data = self.raw(path)
                chain.append(data)
                current = data.get('inherits') or ''
            merged = {}
            for data in reversed(chain):
                merged.update(data)
            merged.update(name=name, type=kind, inherits='', **{'from': 'system'})
            self.cache[key] = merged
        return dict(self.cache[key])

    def resolved(self, kind, name, changes=None):
        """The preset ready for --load-settings / --load-filaments, with `changes` applied ({key: value})."""
        config = self.flat(kind, name)
        if changes:
            for key, value in changes.items():
                old = config.get(key)
                text = value if isinstance(value, str) else ('1' if value is True else '0' if value is False else f'{value:g}' if isinstance(value, float) else str(value))
                config[key] = [text] * max(1, len(old)) if isinstance(old, list) else text
            config.update(inherits=name, name=f'{name} (web)', **{'from': 'User'})
        return config

    # ---- what the Slicer tab offers ----

    def catalog(self):
        """The selectable printers, and the processes and filaments each one can use."""
        machines, processes, filaments = [], [], []
        for vendor in self.vendors:
            for name in self.index.get(vendor, {}).get('machine', {}):
                data = self.raw(self.index[vendor]['machine'][name])
                if str(data.get('instantiation', '')).lower() != 'true':
                    continue
                flat = self.flat('machine', name, vendor)
                machines.append(dict(
                    name=name, vendor=vendor, model=flat.get('printer_model', ''),
                    nozzle=_first(flat.get('nozzle_diameter')), bed=_area(flat.get('printable_area')),
                    height=_number(flat.get('printable_height')), default_process=flat.get('default_print_profile', ''),
                    default_filament=_first(flat.get('default_filament_profile'))))
            names = {m['name'] for m in machines}
            for kind, out in (('process', processes), ('filament', filaments)):
                for name in self.index.get(vendor, {}).get(kind, {}):
                    data = self.raw(self.index[vendor][kind][name])
                    if str(data.get('instantiation', '')).lower() != 'true':
                        continue
                    flat = self.flat(kind, name, vendor)
                    printers = [p for p in flat.get('compatible_printers') or [] if p in names]
                    if not printers:
                        continue
                    entry = dict(name=name, printers=printers)
                    if kind == 'process':
                        entry['layer_height'] = _number(flat.get('layer_height'))
                    else:
                        entry.update(material=_first(flat.get('filament_type')), brand=_first(flat.get('filament_vendor')))
                    out.append(entry)
        return dict(machines=sorted(machines, key=lambda m: m['name']), processes=sorted(processes, key=lambda p: p['name']),
                    filaments=sorted(filaments, key=lambda f: f['name']))


def _first(value):
    return (value[0] if value else '') if isinstance(value, list) else (value or '')


def _number(value):
    try:
        return float(_first(value))
    except (TypeError, ValueError):
        return None


def _area(value):
    """printable_area ["0x0", "180x0", ...] as [[x, y], ...]."""
    points = []
    for item in value or []:
        try:
            x, y = str(item).split('x')
            points.append([float(x), float(y)])
        except ValueError:
            continue
    return points
