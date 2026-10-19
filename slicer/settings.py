"""The slicing settings people can change in the Slicer tab, on top of the chosen presets.

Each entry is an OrcaSlicer setting (its key in the preset files, checked against OrcaSlicer 2.4.2's PrintConfig.cpp),
which preset it belongs to (process or filament), its type and limits. The Slicer tab builds its settings panel from
this list (GET /api/slicer/catalog) and the Pi checks every value against it before a job is queued, so a worker only
ever receives known keys with values in range. To offer another setting, add it here.
"""

PROCESS, FILAMENT = 'process', 'filament'

SCHEMA = [
    # Quality
    dict(key='layer_height', label='Layer height', group='Quality', preset=PROCESS, type='float', min=0.04, max=0.8, step=0.02, unit='mm'),
    dict(key='initial_layer_print_height', label='First layer height', group='Quality', preset=PROCESS, type='float', min=0.05, max=1.0, step=0.02, unit='mm'),
    dict(key='line_width', label='Line width', group='Quality', preset=PROCESS, type='float', min=0.1, max=2.0, step=0.01, unit='mm'),
    dict(key='seam_position', label='Seam position', group='Quality', preset=PROCESS, type='enum', options=['aligned', 'nearest', 'back', 'random']),
    dict(key='ironing_type', label='Ironing', group='Quality', preset=PROCESS, type='enum', options=['no ironing', 'top', 'topmost', 'solid']),
    # Strength
    dict(key='wall_loops', label='Walls', group='Strength', preset=PROCESS, type='int', min=1, max=20),
    dict(key='top_shell_layers', label='Top layers', group='Strength', preset=PROCESS, type='int', min=0, max=50),
    dict(key='bottom_shell_layers', label='Bottom layers', group='Strength', preset=PROCESS, type='int', min=0, max=50),
    dict(key='sparse_infill_density', label='Infill density', group='Strength', preset=PROCESS, type='percent', min=0, max=100, step=1, unit='%'),
    dict(key='sparse_infill_pattern', label='Infill pattern', group='Strength', preset=PROCESS, type='enum',
         options=['crosshatch', 'grid', 'gyroid', 'cubic', 'adaptivecubic', 'triangles', 'tri-hexagon', 'honeycomb', '3dhoneycomb',
                  'lightning', 'rectilinear', 'alignedrectilinear', 'line', 'concentric', 'zigzag']),
    # Speed (mm/s; OrcaSlicer keeps one value per extruder)
    dict(key='outer_wall_speed', label='Outer wall speed', group='Speed', preset=PROCESS, type='float', min=1, max=1000, step=5, unit='mm/s'),
    dict(key='inner_wall_speed', label='Inner wall speed', group='Speed', preset=PROCESS, type='float', min=1, max=1000, step=5, unit='mm/s'),
    dict(key='sparse_infill_speed', label='Infill speed', group='Speed', preset=PROCESS, type='float', min=1, max=1000, step=5, unit='mm/s'),
    dict(key='initial_layer_speed', label='First layer speed', group='Speed', preset=PROCESS, type='float', min=1, max=500, step=5, unit='mm/s'),
    dict(key='travel_speed', label='Travel speed', group='Speed', preset=PROCESS, type='float', min=1, max=1000, step=10, unit='mm/s'),
    dict(key='default_acceleration', label='Acceleration', group='Speed', preset=PROCESS, type='float', min=100, max=50000, step=500, unit='mm/s²'),
    # Support and adhesion
    dict(key='enable_support', label='Supports', group='Support', preset=PROCESS, type='bool'),
    dict(key='support_type', label='Support type', group='Support', preset=PROCESS, type='enum',
         options=['normal(auto)', 'tree(auto)', 'normal(manual)', 'tree(manual)']),
    dict(key='support_threshold_angle', label='Overhang angle', group='Support', preset=PROCESS, type='int', min=0, max=90, unit='°'),
    dict(key='support_on_build_plate_only', label='Supports on the build plate only', group='Support', preset=PROCESS, type='bool'),
    dict(key='brim_type', label='Brim', group='Support', preset=PROCESS, type='enum',
         options=['auto_brim', 'no_brim', 'outer_only', 'inner_only', 'outer_and_inner', 'brim_ears']),
    # Material
    dict(key='nozzle_temperature', label='Nozzle temperature', group='Material', preset=FILAMENT, type='int', min=150, max=350, unit='°C'),
    dict(key='nozzle_temperature_initial_layer', label='First layer nozzle temperature', group='Material', preset=FILAMENT, type='int', min=150, max=350, unit='°C'),
    dict(key='textured_plate_temp', label='Bed temperature (textured plate)', group='Material', preset=FILAMENT, type='int', min=0, max=120, unit='°C'),
    dict(key='hot_plate_temp', label='Bed temperature (smooth plate)', group='Material', preset=FILAMENT, type='int', min=0, max=120, unit='°C'),
    dict(key='filament_flow_ratio', label='Flow ratio', group='Material', preset=FILAMENT, type='float', min=0.5, max=1.5, step=0.01),
    dict(key='filament_max_volumetric_speed', label='Max volumetric speed', group='Material', preset=FILAMENT, type='float', min=0.5, max=80, step=0.5, unit='mm³/s'),
    dict(key='fan_max_speed', label='Max fan speed', group='Material', preset=FILAMENT, type='int', min=0, max=100, unit='%'),
]
BY_KEY = {entry['key']: entry for entry in SCHEMA}


def check(changes):
    """{'process': {...}, 'filament': {...}}: the changed settings, each checked against SCHEMA, as the values to put in
    the presets (numbers, booleans, or '15%' for a percentage)."""
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


def value_for(entry, value):
    label, kind = entry['label'], entry['type']
    if kind == 'bool':
        if type(value) is not bool:
            raise ValueError(f'{label} must be on or off.')
        return value
    if kind == 'enum':
        if value not in entry['options']:
            raise ValueError(f'{label} must be one of: {", ".join(entry["options"])}.')
        return value
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a number.')
    if kind == 'int' and value != int(value):
        raise ValueError(f'{label} must be a whole number.')
    if not entry['min'] <= value <= entry['max']:
        raise ValueError(f"{label} must be between {entry['min']:g} and {entry['max']:g}{entry.get('unit', '')}.")
    if kind == 'int':
        return int(value)
    if kind == 'percent':
        return f'{value:g}%'
    return float(value)
