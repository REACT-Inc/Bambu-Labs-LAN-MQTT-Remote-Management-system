"""Write slicer/orca_options.py: every process and filament setting the Slicer tab offers, from OrcaSlicer's source.

    python3 tools/make_slicer_options.py ORCASLICER_SOURCE_FOLDER

ORCASLICER_SOURCE_FOLDER is a checkout of the OrcaSlicer release the workers use (slicer/installer.py ORCA_VERSION),
for example `git clone --depth 1 --branch v2.4.2 https://github.com/OrcaSlicer/OrcaSlicer`. Two files are read:

- src/slic3r/GUI/Tab.cpp: the pages and groups of OrcaSlicer's Process and Filament tabs, and the settings in each,
  in OrcaSlicer's order (TabPrint::build, TabFilament::build).
- src/libslic3r/PrintConfig.cpp: each setting's label, tooltip, type, unit, limits, choices, default and
  Simple/Advanced level.

Settings that hold text are left out: G-code, post-processing scripts (which a worker would run), notes, file name
formats and preset links. Everything left is a number, a percentage, on/off or a fixed choice, which the Pi checks
before a job reaches a worker. Run it again after changing ORCA_VERSION, and commit the result.
"""
import ast
import pprint
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPES = {'coFloat': 'float', 'coFloats': 'float', 'coInt': 'int', 'coInts': 'int', 'coBool': 'bool', 'coBools': 'bool',
         'coPercent': 'percent', 'coPercents': 'percent', 'coFloatOrPercent': 'float_or_percent',
         'coFloatsOrPercents': 'float_or_percent', 'coEnum': 'enum', 'coEnums': 'enum'}
LISTS = {'coFloats', 'coInts', 'coBools', 'coPercents', 'coFloatsOrPercents', 'coEnums'}
LEVELS = {'Simple': 'simple', 'Advanced': 'advanced', 'Expert': 'advanced', 'Develop': 'develop'}
# Never offered: text (G-code, scripts a worker would run, notes), preset links, and per-printer identity.
SKIP = re.compile(r'gcode|post_process|filename|notes|compatible|inherits|settings_id|_ids?$|printer|^filament_vendor$|'
                  r'^filament_type$|^default_filament_colour$|^filament_colour')
# Settings shown as one row in OrcaSlicer (a "Line") but without a group of their own: kept with the row's group.
TAB_PAGES = (('TabPrint::build', 'process'), ('TabFilament::build', 'filament'), ('TabFilament::add_filament_overrides_page', 'filament'))


def literals(text):
    """The C++ string literals in an expression ("a" "b" L("c") u8"\\u2103"), joined."""
    parts = re.findall(r'(?:u8)?"((?:[^"\\]|\\.)*)"', text)
    return ''.join(parts).encode('latin-1', 'backslashreplace').decode('unicode_escape') if parts else ''


def assignment(block, name):
    match = re.search(rf'->\s*{name}\s*=\s*((?:[^;"]|"(?:[^"\\]|\\.)*")*);', block)   # up to the ; outside strings
    return literals(match.group(1)) if match else ''


def number(text, constants):
    text = text.strip().rstrip('f')
    if text in constants:
        return constants[text]
    try:
        value = ast.literal_eval(text.replace('.f', '.0'))
        return value if isinstance(value, (int, float)) else None
    except (ValueError, SyntaxError):
        return None


def definitions(source):
    constants = {m.group(1): float(m.group(2)) for m in re.finditer(r'(?:const|constexpr)\s+(?:int|float|double)\s+(\w+)\s*=\s*([-\d.]+)f?\s*;', source)}
    options, names = {}, {}   # names: `auto def_top_fill_pattern = def = this->add("top_surface_pattern"...` -> its key
    blocks = re.split(r'\n[^\n;]*?\bdef\s*=\s*this->add(?:_nullable)?\(', source)
    heads = re.findall(r'\n[^\n;]*?\bdef\s*=\s*this->add(?:_nullable)?\(\s*"([^"]+)"', source)
    for line in re.finditer(r'auto\s+(\w+)\s*=\s*def\s*=\s*this->add(?:_nullable)?\(\s*"([^"]+)"', source):
        names[line.group(1)] = line.group(2)
    for block in blocks[1:]:
        head = re.match(r'\s*"([^"]+)"\s*,\s*(co\w+)\s*\)', block)
        if not head or head.group(2) not in TYPES:
            continue
        key, cotype = head.groups()
        entry = dict(key=key, type=TYPES[cotype], list=cotype in LISTS,
                     label=assignment(block, 'full_label') or assignment(block, 'label') or key,
                     tooltip=assignment(block, 'tooltip'), unit=assignment(block, 'sidetext'))
        for limit in ('min', 'max'):
            match = re.search(rf'->\s*{limit}\s*=\s*([^;]+);', block)
            value = number(match.group(1), constants) if match else None
            if value is not None:
                entry[limit] = value
        mode = re.search(r'->\s*mode\s*=\s*com(\w+)', block)
        entry['level'] = LEVELS.get(mode.group(1), 'advanced') if mode else 'advanced'
        if entry['type'] == 'enum':
            values = re.findall(r'enum_values\.(?:push|emplace)_back\(\s*"([^"]*)"\s*\)', block)
            labels = [literals(x) for x in re.findall(r'enum_labels\.(?:push|emplace)_back\(\s*((?:L\()?\s*(?:u8)?"(?:[^"\\]|\\.)*"\s*\)?)\s*\)', block)]
            if values:
                entry['options'] = [[v, labels[i] if i < len(labels) and labels[i] else v] for i, v in enumerate(values)]
            else:   # def->enum_values = def_top_fill_pattern->enum_values; (another setting's choices)
                same = re.search(r'enum_values\s*=\s*(\w+)->enum_values', block)
                other = options.get(names.get(same.group(1))) if same else None
                if not other or 'options' not in other:
                    continue
                entry['options'] = [list(o) for o in other['options']]
        default = re.search(r'set_default_value\(\s*new\s+ConfigOption\w*(?:<\w+>)?\s*[({]\s*(.*?)\s*[)}]\s*\)', block, re.S)
        if default:
            text = default.group(1).strip().strip('{} ')
            first = re.split(r',(?![^(]*\))', text)[0].strip() if text else ''
            if entry['type'] == 'bool':
                entry['default'] = {'true': True, 'false': False, '1': True, '0': False}.get(first.lower())
            elif entry['type'] in ('float', 'int', 'percent'):
                entry['default'] = number(first, constants)
            elif entry['type'] == 'float_or_percent':
                parts = [p.strip() for p in first.split(',')] if ',' in text else [first]
                value = number(parts[0], constants)
                entry['default'] = f'{value:g}%' if value is not None and 'true' in text.lower() else value
            if entry.get('default') is None:
                entry.pop('default', None)
        options[key] = entry
    # A filament's overrides of the printer's retraction settings (filament_retraction_length...): copies of those
    # settings, made in a loop (PrintConfigDef::init_fff_params, filament_extruder_override_keys). Blank uses the printer's.
    listing = re.search(r'filament_extruder_override_keys\s*=\s*\{(.*?)\};', source, re.S)
    for key in re.findall(r'"([^"]+)"', re.sub(r'//[^\n]*', '', listing.group(1))) if listing else []:
        printer = options.get(key[len('filament_'):]) if key.startswith('filament_') else None
        if printer and key not in options:
            options[key] = {k: v for k, v in printer.items() if k != 'default'} | dict(key=key, list=True, level='advanced')
    return options


def tab_layout(source, function):
    """[(page, group, [keys])] of one Tab::build function, in OrcaSlicer's order."""
    if f'void {function}()' not in source:
        return []
    start = source.index(f'void {function}()')
    end = source.index('\nvoid ', start + 10)
    body = re.sub(r'//[^\n]*', '', source[start:end])
    # for (const std::string opt_key : {"a", "b"}) append_x_option(opt_key...): the same as a line per key
    body = re.sub(r'for\s*\(\s*const\s+std::string\s+\w+\s*:\s*\{(.*?)\}\s*\)',
                  lambda m: ' '.join(f'append_single_option_line("{k}")' for k in re.findall(r'"([^"]+)"', m.group(1))), body, flags=re.S)
    layout, page, group = [], 'General', 'General'
    pattern = re.compile(r'add_options_page\(\s*L\("([^"]+)"\)|new_optgroup\(\s*L\("([^"]+)"\)|'
                         r'(?:append_single_option_line|get_option|get_option_)\(\s*"([^"#]+)')
    for match in pattern.finditer(body):
        if match.group(1):
            page, group = match.group(1), match.group(1)
        elif match.group(2):
            group = match.group(2)
        else:
            if not layout or layout[-1][:2] != (page, group):
                layout.append((page, group, []))
            if match.group(3) not in layout[-1][2]:
                layout[-1][2].append(match.group(3))
    return layout


def build(orca):
    definitions_source = (orca / 'src/libslic3r/PrintConfig.cpp').read_text(encoding='utf-8')
    tab_source = (orca / 'src/slic3r/GUI/Tab.cpp').read_text(encoding='utf-8')
    defs = definitions(definitions_source)
    out, seen = [], set()
    for function, preset in TAB_PAGES:
        for page, group, keys in tab_layout(tab_source, function):
            for key in keys:
                entry = defs.get(key)
                if not entry or key in seen or SKIP.search(key) or entry['level'] == 'develop':
                    continue
                seen.add(key)
                out.append(dict(entry, preset=preset, page=page, group=group))
    # The plate type is a plate setting in OrcaSlicer's window (not on these tabs); it picks the bed temperature.
    if 'curr_bed_type' in defs and 'curr_bed_type' not in seen:
        out.append(dict(defs['curr_bed_type'], preset='process', page='Others', group='Plate', level='simple'))
    return out


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    options = build(Path(argv[1]))
    version = re.search(r"ORCA_VERSION = '([^']+)'", (ROOT / 'slicer/installer.py').read_text()).group(1)
    text = (f'"""Every OrcaSlicer {version} process and filament setting the Slicer tab offers (docs/slicer.md).\n\n'
            'Generated by tools/make_slicer_options.py from OrcaSlicer\'s Tab.cpp (pages, groups, order) and PrintConfig.cpp\n'
            '(labels, tooltips, types, units, limits, choices, defaults). Do not edit by hand: run the tool again.\n"""\n\n'
            f'ORCA_VERSION = {version!r}\n\nOPTIONS = ' + pprint.pformat(options, width=120, sort_dicts=False) + '\n')
    (ROOT / 'slicer/orca_options.py').write_text(text, encoding='utf-8')
    print(f'{len(options)} settings: ' + ', '.join(f"{sum(1 for o in options if o['preset'] == p)} {p}" for p in ('process', 'filament')))


if __name__ == '__main__':
    main(sys.argv)
