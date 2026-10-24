"""The slicing settings people can change in the Slicer tab, on top of the chosen presets.

All of OrcaSlicer's process and filament settings that are numbers, percentages, on/off or fixed choices
(slicer/orca_options.py, generated from OrcaSlicer's own source by tools/make_slicer_options.py), with OrcaSlicer's
labels, pages, groups, units, limits and choices. QUICK picks the common ones for the panel next to the plate; the
**All settings** window has the rest. The Pi checks every value against SCHEMA before a job is queued, so a worker
only ever receives known keys with values in range, and never text (no G-code, no scripts).
"""
import math

from slicer.orca_options import OPTIONS, ORCA_VERSION   # noqa: F401  (ORCA_VERSION: the release they come from)

PROCESS, FILAMENT = 'process', 'filament'
NO_LIMIT = 1e6   # for a number OrcaSlicer gives no limit for

# The panel next to the plate: (group, [(key, shorter label)]), in this order.
QUICK = [
    ('Quality', [('layer_height', 'Layer height'), ('initial_layer_print_height', 'First layer height'), ('line_width', 'Line width'),
                 ('seam_position', 'Seam position'), ('ironing_type', 'Ironing')]),
    ('Strength', [('wall_loops', 'Walls'), ('top_shell_layers', 'Top layers'), ('bottom_shell_layers', 'Bottom layers'),
                  ('sparse_infill_density', 'Infill density'), ('sparse_infill_pattern', 'Infill pattern')]),
    ('Speed', [('outer_wall_speed', 'Outer wall speed'), ('inner_wall_speed', 'Inner wall speed'), ('sparse_infill_speed', 'Infill speed'),
               ('initial_layer_speed', 'First layer speed'), ('travel_speed', 'Travel speed'), ('default_acceleration', 'Acceleration')]),
    ('Support', [('enable_support', 'Supports'), ('support_type', 'Support type'), ('support_threshold_angle', 'Overhang angle'),
                 ('support_on_build_plate_only', 'Supports on the build plate only'), ('brim_type', 'Brim')]),
    ('Material', [('nozzle_temperature', 'Nozzle temperature'), ('nozzle_temperature_initial_layer', 'First layer nozzle temperature'),
                  ('textured_plate_temp', 'Bed temperature (textured plate)'), ('hot_plate_temp', 'Bed temperature (smooth plate)'),
                  ('filament_flow_ratio', 'Flow ratio'), ('filament_max_volumetric_speed', 'Max volumetric speed'), ('fan_max_speed', 'Max fan speed')]),
]
# Practical limits where OrcaSlicer sets none (or a very wide one), so an obvious typo is caught before a job is queued.
LIMITS = {'layer_height': (0.01, 1.0), 'initial_layer_print_height': (0.01, 1.5), 'top_shell_layers': (0, 100), 'bottom_shell_layers': (0, 100),
          'wall_loops': (0, 100), 'outer_wall_speed': (1, 1500), 'inner_wall_speed': (1, 1500), 'sparse_infill_speed': (1, 1500),
          'initial_layer_speed': (1, 1000), 'travel_speed': (1, 2000), 'default_acceleration': (0, 100000),
          'nozzle_temperature': (0, 500), 'nozzle_temperature_initial_layer': (0, 500), 'filament_max_volumetric_speed': (0, 200)}
SCHEMA = [dict(entry) for entry in OPTIONS]
BY_KEY = {entry['key']: entry for entry in SCHEMA}
for _key, (_low, _high) in LIMITS.items():
    BY_KEY[_key].update(min=_low, max=_high)
for _group, _entries in QUICK:
    for _key, _label in _entries:
        BY_KEY[_key].update(quick=_group, quick_label=_label)


def check(changes):
    """{'process': {...}, 'filament': {...}}: the changed settings, each checked against SCHEMA, as the values to put in
    the presets (numbers, booleans, choices, or '15%' for a percentage)."""
    if changes in (None, {}):
        return {PROCESS: {}, FILAMENT: {}}
    if not isinstance(changes, dict) or len(changes) > len(SCHEMA):
        raise ValueError('Settings must be a list of setting names and values.')
    out = {PROCESS: {}, FILAMENT: {}}
    for key, value in changes.items():
        entry = BY_KEY.get(key)
        if not entry:
            raise ValueError(f'"{str(key)[:60]}" is not a setting that can be changed here.')
        out[entry['preset']][key] = value_for(entry, value)
    return out


def _number(entry, value, label, percent=False):
    if type(value) is bool or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a number.')
    low, high = entry.get('min', -NO_LIMIT), entry.get('max', NO_LIMIT)
    if percent and 'max' not in entry:
        high = 1000
    if not low <= value <= high:
        unit = '%' if percent else entry.get('unit') or ''
        raise ValueError(f'{label} must be between {low:g} and {high:g}{(" " + unit) if unit and unit != "%" else unit}.')
    return value


def value_for(entry, value):
    label, kind = entry.get('quick_label') or entry['label'], entry['type']
    if not entry.get('quick_label') and entry.get('group') not in (None, label) and len(label.split()) < 3:
        label = f"{entry['group']}: {label}"   # OrcaSlicer's short labels, such as "Default" under Line width
    if kind == 'bool':
        if type(value) is not bool:
            raise ValueError(f'{label} must be on or off.')
        return value
    if kind == 'enum':
        choices = [v for v, _ in entry['options']]
        if value not in choices:
            raise ValueError(f'{label} must be one of: {", ".join(choices)}.')
        return value
    if kind == 'float_or_percent':   # a length (or speed) in its unit, or a percentage of another setting: 0.45 or "110%"
        if isinstance(value, str):
            text = value.strip()
            try:
                number = float(text[:-1]) if text.endswith('%') else None
            except ValueError:
                number = None
            if number is None:
                raise ValueError(f'{label} must be a number, or a percentage like 50%.')
            return f'{_number(entry, number, label, percent=True):g}%'
        return float(_number(entry, value, label))
    if kind == 'percent':
        return f'{_number(entry, value, label, percent=True):g}%'
    value = _number(entry, value, label)
    if kind == 'int':
        if value != int(value):
            raise ValueError(f'{label} must be a whole number.')
        return int(value)
    return float(value)


def client_schema():
    """SCHEMA for the Slicer tab, with the quick panel's groups."""
    return dict(settings=SCHEMA, quick=[dict(group=g, keys=[k for k, _ in entries]) for g, entries in QUICK], orca_version=ORCA_VERSION)
