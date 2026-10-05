"""Where plastic should be: the printed geometry from a sliced plate's G-code, for AI failure detection (#79).

parse_gcode() reads the plate's G-code once (in a worker thread, cached on disk) and turns every extruding move into a
bed grid: for each GRID_MM x GRID_MM cell, the first layer that puts plastic there and the highest Z it reaches. From
that, footprint(layer) is "everything printed up to this layer, seen from above" and height(layer) how tall it is.

Camera calibration maps the bed into each camera's picture: the user clicks the printable area's four corners on a
snapshot (front-left, front-right, back-right, back-left) and homography() turns those into a bed <-> picture mapping.
That's exact on the bed surface; for parts that have grown taller, the footprint is widened by the part's height so
something seen above a tall part still counts as "on the part".

inside_fraction() then says how much of an AI detection box lies on (or right next to) the expected part. Material
where nothing should be printed is strong evidence of a failure (spaghetti, a part knocked loose); a detection on the
part itself is more likely to be the part's own geometry (supports, thin features), so it counts for less.

Standard library only (the service venv has no numpy).
"""
import base64
import math
import re
import zipfile
import zlib

GRID_MM = 2.0
MAX_GCODE = 600 * 1024 * 1024   # plate G-code larger than this isn't read
NO_LAYER = 0xFFFF
_WORD = re.compile(r'([GXYZEIJF])(-?\d*\.?\d+)')
_AREA = re.compile(r';\s*printable_area\s*=\s*(.+)')
_LAYER = re.compile(r';\s*(?:layer num/total_layer_count:\s*(\d+)\s*/|LAYER:\s*(\d+)|CHANGE_LAYER)', re.I)


class Geometry:
    """The parsed plate: grid cells with first layer / top height, and each layer's Z."""

    def __init__(self, width, depth, origin=(0.0, 0.0), grid=GRID_MM):
        self.width, self.depth, self.origin, self.grid = float(width), float(depth), origin, grid
        self.columns, self.rows = max(1, math.ceil(width / grid)), max(1, math.ceil(depth / grid))
        self.first = [NO_LAYER] * (self.columns * self.rows)   # first layer (1-based) with plastic in the cell
        self.top = [0.0] * (self.columns * self.rows)          # highest Z of plastic in the cell
        self.layer_z = {}                                      # layer number -> Z
        self.layers = 0
        self._distance = (None, None)                          # (layer, distance map) cache

    def cell(self, x, y):
        column, row = int((x - self.origin[0]) // self.grid), int((y - self.origin[1]) // self.grid)
        if 0 <= column < self.columns and 0 <= row < self.rows:
            return row * self.columns + column
        return None

    def mark(self, x0, y0, x1, y1, layer, z):
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) / (self.grid / 2)))
        for step in range(steps + 1):
            index = self.cell(x0 + (x1 - x0) * step / steps, y0 + (y1 - y0) * step / steps)
            if index is not None:
                if layer < self.first[index]:
                    self.first[index] = layer
                if z > self.top[index]:
                    self.top[index] = z

    def height(self, layer):
        """Z reached at this layer (what's been printed so far)."""
        known = [z for n, z in self.layer_z.items() if n <= layer]
        return max(known, default=0.0)

    def distance(self, layer):
        """Per cell, the distance (mm) to the nearest cell with plastic by this layer (two-pass chamfer transform)."""
        if self._distance[0] == layer:
            return self._distance[1]
        far, columns, rows = 1e9, self.columns, self.rows
        d = [0.0 if f <= layer else far for f in self.first]
        straight, diagonal = self.grid, self.grid * math.sqrt(2)
        for r in range(rows):
            for c in range(columns):
                i = r * columns + c
                if d[i]:
                    best = d[i]
                    if c: best = min(best, d[i - 1] + straight)
                    if r:
                        best = min(best, d[i - columns] + straight)
                        if c: best = min(best, d[i - columns - 1] + diagonal)
                        if c + 1 < columns: best = min(best, d[i - columns + 1] + diagonal)
                    d[i] = best
        for r in range(rows - 1, -1, -1):
            for c in range(columns - 1, -1, -1):
                i = r * columns + c
                if d[i]:
                    best = d[i]
                    if c + 1 < columns: best = min(best, d[i + 1] + straight)
                    if r + 1 < rows:
                        best = min(best, d[i + columns] + straight)
                        if c + 1 < columns: best = min(best, d[i + columns + 1] + diagonal)
                        if c: best = min(best, d[i + columns - 1] + diagonal)
                    d[i] = best
        self._distance = (layer, d)
        return d

    def covered(self, x, y, layer, margin=0.0):
        """Is there plastic within margin mm of bed point (x, y) by this layer? Off the bed counts as not covered."""
        index = self.cell(x, y)
        return index is not None and self.distance(layer)[index] <= margin

    def area(self, layer):
        """Bed area (mm²) with plastic by this layer."""
        return sum(1 for f in self.first if f <= layer) * self.grid * self.grid

    def to_json(self):
        packed = bytearray()
        for first, top in zip(self.first, self.top):
            packed += first.to_bytes(2, 'little') + min(65535, int(top * 100)).to_bytes(2, 'little')
        return dict(width=self.width, depth=self.depth, origin=list(self.origin), grid=self.grid, layers=self.layers,
                    layer_z={str(k): v for k, v in self.layer_z.items()},
                    cells=base64.b64encode(zlib.compress(bytes(packed), 6)).decode())

    @classmethod
    def from_json(cls, data):
        geometry = cls(data['width'], data['depth'], tuple(data['origin']), data['grid'])
        packed = zlib.decompress(base64.b64decode(data['cells']))
        if len(packed) != 4 * len(geometry.first):
            raise ValueError('Geometry cache does not match its grid.')
        for index in range(len(geometry.first)):
            geometry.first[index] = int.from_bytes(packed[index * 4:index * 4 + 2], 'little')
            geometry.top[index] = int.from_bytes(packed[index * 4 + 2:index * 4 + 4], 'little') / 100
        geometry.layer_z = {int(k): float(v) for k, v in data['layer_z'].items()}
        geometry.layers = int(data['layers'])
        return geometry


def printable_area(text):
    """'0x0,256x0,256x256,0x256' -> (x0, y0, x1, y1)."""
    points = [tuple(float(v) for v in p.split('x')) for p in text.replace(' ', '').split(',') if 'x' in p]
    if len(points) < 3:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def find_area(lines):
    """The bed's printable area from Bambu Studio's config block ('; printable_area = 0x0,256x0,...')."""
    found = None
    for line in lines:
        if line.startswith(';') and 'printable_area' in line:
            match = _AREA.match(line.strip())
            if match:
                found = printable_area(match.group(1)) or found
    return found


def parse_gcode(lines, area=None):
    """Build a Geometry from G-code lines (str), streamed. area=(x0, y0, x1, y1) of the bed; when it's None the
    lines are read into memory to find '; printable_area' first (fine for tests; from_3mf streams twice instead).

    Extrusion is detected from E (relative M83, as Bambu Studio writes it, or absolute M82); travel, retractions and
    wipes don't mark anything. Layers follow Bambu Studio's '; layer num/total_layer_count: N/M' (or ';LAYER:N' /
    '; CHANGE_LAYER'), so they match the printer's reported layer_num.
    """
    if area is None:
        lines = list(lines)
        area = find_area(lines)
    if not area:
        area = (0.0, 0.0, 256.0, 256.0)
    geometry = Geometry(area[2] - area[0], area[3] - area[1], (area[0], area[1]))
    x = y = z = e = 0.0
    absolute, absolute_e, layer, counted = True, False, 0, False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line[0] == ';':
            match = _LAYER.match(line)
            if match:
                number = match.group(1) or match.group(2)
                if number is not None:
                    layer, counted = int(number) + (1 if match.group(2) is not None else 0), True
                elif not counted:
                    layer += 1
                geometry.layers = max(geometry.layers, layer)
            continue
        code = line.split(';', 1)[0].upper()
        if code.startswith(('G90', 'G91', 'M82', 'M83', 'G92')):
            command = code.split()[0]
            if command == 'G90':
                absolute = True
            elif command == 'G91':
                absolute = False
            elif command == 'M82':
                absolute_e = True
            elif command == 'M83':
                absolute_e = False
            elif command == 'G92':
                words = dict(_WORD.findall(code))
                if 'E' in words:
                    e = float(words['E'])
            continue
        if not code.startswith(('G0', 'G1', 'G2', 'G3')) or code[:3] in ('G10', 'G11', 'G28', 'G29') or code.startswith(('G17', 'G18', 'G19')):
            continue
        words = dict(_WORD.findall(code))
        nx = (float(words['X']) if absolute else x + float(words['X'])) if 'X' in words else x
        ny = (float(words['Y']) if absolute else y + float(words['Y'])) if 'Y' in words else y
        nz = (float(words['Z']) if absolute else z + float(words['Z'])) if 'Z' in words else z
        extruded = 0.0
        if 'E' in words:
            value = float(words['E'])
            extruded, e = (value - e, value) if absolute_e else (value, e)
        if extruded > 0 and (nx != x or ny != y):
            current = max(layer, 1)
            geometry.mark(x, y, nx, ny, current, nz)
            geometry.layer_z[current] = max(geometry.layer_z.get(current, 0.0), nz)
            geometry.layers = max(geometry.layers, current)
        x, y, z = nx, ny, nz
    return geometry


def from_3mf(path, plate=1):
    """Parse Metadata/plate_N.gcode inside a sliced .3mf. Returns a Geometry, or raises ValueError."""
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(f'Metadata/plate_{int(plate)}.gcode')
            if info.file_size > MAX_GCODE:
                raise ValueError('Plate G-code too large to read.')
            # Two streaming passes: the bed size is in the config block at the end, and the grid needs it first.
            with archive.open(info) as stream:
                area = find_area(line.decode('utf-8', 'replace') for line in stream)
            with archive.open(info) as stream:
                return parse_gcode((line.decode('utf-8', 'replace') for line in stream), area or (0.0, 0.0, 256.0, 256.0))
    except KeyError:
        raise ValueError(f"The file has no G-code for plate {plate}.") from None
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f'Could not read the print file ({type(exc).__name__}).') from exc


# ---- Camera calibration: bed (mm) <-> picture (0-1 fractions) ----

def _solve(matrix, vector):
    """Gaussian elimination with partial pivoting for a small square system."""
    size = len(vector)
    rows = [list(matrix[i]) + [vector[i]] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot][column]) < 1e-12:
            raise ValueError('The four corners are on one line; click them again.')
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for r in range(size):
            if r != column:
                factor = rows[r][column] / rows[column][column]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[column])]
    return [rows[i][size] / rows[i][i] for i in range(size)]


def homography(source, target):
    """3x3 matrix (row-major list of 9) mapping four source points onto four target points."""
    matrix, vector = [], []
    for (x, y), (u, v) in zip(source, target):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y]);vector.append(u)
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y]);vector.append(v)
    return _solve(matrix, vector) + [1.0]


def apply(h, x, y):
    w = h[6] * x + h[7] * y + h[8]
    if abs(w) < 1e-12:
        raise ValueError('Point maps to infinity.')
    return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w


class Calibration:
    """corners: picture positions (0-1) of the printable area's front-left, front-right, back-right, back-left."""

    def __init__(self, corners, area):
        if len(corners) != 4 or not all(len(c) == 2 and all(0 <= float(v) <= 1 for v in c) for c in corners):
            raise ValueError('Click the four bed corners on the picture.')
        x0, y0, x1, y1 = area
        bed = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        picture = [(float(c[0]), float(c[1])) for c in corners]
        self.to_bed, self.to_picture = homography(picture, bed), homography(bed, picture)
        # Reject a twisted outline (corners clicked out of order): the polygon must not cross itself.
        signs = []
        for i in range(4):
            (ax, ay), (bx, by), (cx, cy) = picture[i], picture[(i + 1) % 4], picture[(i + 2) % 4]
            signs.append((bx - ax) * (cy - by) - (by - ay) * (cx - bx) > 0)
        if len(set(signs)) != 1:
            raise ValueError('The corners cross over; click front-left, front-right, back-right, back-left in order.')

    def bed_point(self, u, v):
        return apply(self.to_bed, u, v)


def inside_fraction(box, geometry, calibration, layer, margin=5.0, samples=8):
    """Share (0-1) of a detection box (picture fractions x0, y0, x1, y1) that lies on or next to plastic the file has
    printed by this layer. The margin grows with the part's height, since the calibration is exact only on the bed."""
    x0, y0, x1, y1 = box
    reach = margin + geometry.height(layer)
    hits = total = 0
    for i in range(samples):
        for j in range(samples):
            u = x0 + (x1 - x0) * (i + 0.5) / samples
            v = y0 + (y1 - y0) * (j + 0.5) / samples
            try:
                bx, by = calibration.bed_point(u, v)
            except ValueError:
                continue
            total += 1
            if geometry.covered(bx, by, layer, reach):
                hits += 1
    return hits / total if total else 0.0


def main(argv):
    """python3 print_geometry.py <file.3mf> <plate> <out.json>: parse in a separate (low-priority) process."""
    import json
    import os
    import tempfile
    path, plate, out = argv[1], int(argv[2]), argv[3]
    try:
        result = from_3mf(path, plate).to_json()
    except ValueError as exc:
        result = {'error': str(exc)}
    folder = os.path.dirname(os.path.abspath(out))
    fd, temporary = tempfile.mkstemp(dir=folder, prefix='.geometry-')
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream)
    os.replace(temporary, out)
    return 0 if 'error' not in result else 1


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))
