"""Models in other formats for the Slicer tab: OBJ and 3MF files (untrusted uploads), read into triangles.

Each object becomes a flat array of 4-byte floats, 9 per triangle (as meshes.triangles()), in millimetres, which the
Pi keeps as a binary STL. So the rest of the slicer (checks, placement, workers) only ever sees STL.

- **OBJ:** `v` vertices and `f` faces (polygons are split into triangles; `v/vt/vn` and negative indices are read).
  OBJ has no units: millimetres, as slicers assume. The whole file is one object.
- **3MF:** each build item is an object, with its components and transforms (3MF Core), also from the separate model
  files Bambu Studio and OrcaSlicer projects use (the production extension's p:path). Units are converted to
  millimetres. Painted colours, modifiers and plate settings aren't read: only the shapes.

Limits are checked as the file is read: meshes.MAX_TRIANGLES per upload, MAX_PARTS model files and 3MF members of
at most meshes.MAX_BYTES. XML with a DOCTYPE (entities) is refused, so nothing in it is expanded.
"""
import io
import math
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from array import array

from slicer import meshes

MAX_PARTS = 2000          # objects + components in one 3MF
MAX_DEPTH = 16            # components inside components
UNITS = {'micron': 0.001, 'millimeter': 1.0, 'centimeter': 10.0, 'inch': 25.4, 'foot': 304.8, 'meter': 1000.0}
CORE = '{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'
PRODUCTION = '{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}'
IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


class ModelError(meshes.MeshError):
    pass


def read(name, data):
    """[(object name, triangles)] for an uploaded model, by its extension."""
    extension = name.lower().rsplit('.', 1)[-1] if '.' in name else ''
    if extension == 'stl':
        return [(_stem(name), meshes.triangles(data))]
    if extension == 'obj':
        return [(_stem(name), obj(data))]
    if extension == '3mf':
        return threemf(data, _stem(name))
    raise ModelError('Add .stl, .obj or .3mf files.')


def _stem(name):
    base = posixpath.basename(str(name).replace('\\', '/'))
    return (base.rsplit('.', 1)[0] or 'object')[:100]


def _finite(values):
    if not all(math.isfinite(v) for v in values):
        raise ModelError('The model contains invalid coordinates (NaN or infinity).')
    return values


# ---- OBJ ----

def obj(data):
    if len(data) > meshes.MAX_BYTES:
        raise ModelError(f'The file is larger than {meshes.MAX_BYTES // 1048576} MB.')
    vertices, out = [], array('f')
    for raw in bytes(data).splitlines():
        line = raw.strip()
        if line.startswith(b'v '):
            parts = line.split()
            try:
                vertex = (float(parts[1]), float(parts[2]), float(parts[3]))
            except (IndexError, ValueError):
                raise ModelError('The OBJ file has a broken vertex line.') from None
            vertices.append(_finite(vertex))
        elif line.startswith(b'f '):
            corners = []
            for item in line.split()[1:]:
                try:
                    index = int(item.split(b'/')[0])
                    corners.append(vertices[index - 1 if index > 0 else index])
                except (ValueError, IndexError):
                    raise ModelError('The OBJ file has a face that points to a missing vertex.') from None
            for i in range(1, len(corners) - 1):   # a polygon as a fan of triangles
                out.extend(corners[0] + corners[i] + corners[i + 1])
            if len(out) > meshes.MAX_TRIANGLES * 9:
                raise ModelError(f'The model has more than {meshes.MAX_TRIANGLES:,} triangles.')
    if not out:
        raise ModelError('The OBJ file has no faces (only points or lines?).')
    return out


# ---- 3MF ----

def _xml(archive, path):
    try:
        info = archive.getinfo(path)
    except KeyError:
        raise ModelError(f'The 3MF file refers to {path}, which is missing.') from None
    if info.file_size > meshes.MAX_BYTES:
        raise ModelError(f'A part of the 3MF file is larger than {meshes.MAX_BYTES // 1048576} MB.')
    with archive.open(info) as stream:
        head = stream.read(4096)
        if b'<!DOCTYPE' in head or b'<!ENTITY' in head:
            raise ModelError('The 3MF file contains XML declarations that are not allowed.')
    return info


def _matrix(text):
    """A 3MF transform ("m00 m01 m02 m10 ... m32", points are row vectors) as 12 floats, or the identity."""
    if not text:
        return IDENTITY
    try:
        values = tuple(float(v) for v in text.split())
    except ValueError:
        raise ModelError('The 3MF file has a broken transform.') from None
    if len(values) != 12 or not all(math.isfinite(v) for v in values):
        raise ModelError('The 3MF file has a broken transform.')
    return values


def _combine(inner, outer):
    """The transform `inner` followed by `outer` (both 3MF row-vector matrices)."""
    a, b = inner, outer
    rot = [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3)]
    move = [sum(a[9 + k] * b[k * 3 + c] for k in range(3)) + b[9 + c] for c in range(3)]
    return tuple(rot + move)


def _apply(m, x, y, z):
    return (x * m[0] + y * m[3] + z * m[6] + m[9], x * m[1] + y * m[4] + z * m[7] + m[10], x * m[2] + y * m[5] + z * m[8] + m[11])


class _Model:
    """The objects in one .model file: meshes (vertices + triangles) and components."""

    def __init__(self, archive, path, budget):
        self.path, self.objects, self.items = path, {}, []
        info = _xml(archive, path)
        scale, current, vertices, triangles = 1.0, None, None, None
        with archive.open(info) as stream:
            try:
                for event, element in ET.iterparse(stream, events=('start', 'end')):
                    tag = element.tag.rsplit('}', 1)[-1]
                    if event == 'start':
                        if tag == 'model':
                            unit = element.get('unit', 'millimeter')
                            if unit not in UNITS:
                                raise ModelError(f'The 3MF file uses an unknown unit: {unit[:20]}.')
                            scale = UNITS[unit]
                        elif tag == 'object':
                            current = dict(id=element.get('id'), name=element.get('name') or '', mesh=None, components=[])
                            budget[0] += 1
                        elif tag == 'vertices':
                            vertices = array('f')
                        elif tag == 'triangles':
                            triangles = array('i')
                        continue
                    if tag == 'vertex' and vertices is not None:
                        try:
                            vertex = (float(element.get('x')) * scale, float(element.get('y')) * scale, float(element.get('z')) * scale)
                        except (TypeError, ValueError):
                            raise ModelError('The 3MF file has a broken vertex.') from None
                        vertices.extend(_finite(vertex))
                    elif tag == 'triangle' and triangles is not None:
                        try:
                            triangles.extend((int(element.get('v1')), int(element.get('v2')), int(element.get('v3'))))
                        except (TypeError, ValueError):
                            raise ModelError('The 3MF file has a broken triangle.') from None
                        budget[1] += 1
                        if budget[1] > meshes.MAX_TRIANGLES:
                            raise ModelError(f'The model has more than {meshes.MAX_TRIANGLES:,} triangles.')
                    elif tag == 'component' and current is not None:
                        budget[0] += 1
                        current['components'].append((element.get('objectid'), _matrix(element.get('transform')),
                                                      element.get(PRODUCTION + 'path') or element.get('path')))
                    elif tag == 'mesh' and current is not None:
                        current['mesh'] = (vertices or array('f'), triangles or array('i'))
                        vertices = triangles = None
                    elif tag == 'object' and current is not None:
                        self.objects[current['id']] = current
                        current = None
                    elif tag == 'item':
                        self.items.append((element.get('objectid'), _matrix(element.get('transform')),
                                           element.get(PRODUCTION + 'path') or element.get('path')))
                    if budget[0] > MAX_PARTS:
                        raise ModelError(f'The 3MF file has more than {MAX_PARTS} objects and parts.')
                    if tag in ('vertex', 'triangle', 'component', 'item'):
                        element.clear()
            except ET.ParseError:
                raise ModelError('The 3MF file contains broken XML.') from None


def _model_path(archive):
    """The main .model file, from _rels/.rels (the 3MF start part), else 3D/3dmodel.model."""
    try:
        rels = archive.read('_rels/.rels') if archive.getinfo('_rels/.rels').file_size < 1_000_000 else b''
        if b'<!DOCTYPE' not in rels and b'<!ENTITY' not in rels:
            for rel in ET.fromstring(rels):
                if rel.get('Type', '').endswith('/3dmodel'):
                    return rel.get('Target', '').lstrip('/')
    except (KeyError, ET.ParseError):
        pass
    return '3D/3dmodel.model'


def threemf(data, default_name='object'):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ModelError("That isn't a readable .3mf file.") from None
    budget, models = [0, 0], {}
    with archive:
        def model(path):
            path = path.lstrip('/')
            if path not in models:
                if len(models) > 200:
                    raise ModelError('The 3MF file has too many model parts.')
                models[path] = _Model(archive, path, budget)
            return models[path]

        main = model(_model_path(archive))

        def collect(path, object_id, transform, out, depth):
            if depth > MAX_DEPTH:
                raise ModelError('The 3MF file nests its parts too deeply (or in a loop).')
            obj = model(path).objects.get(object_id)
            if obj is None:
                raise ModelError(f'The 3MF file refers to object {str(object_id)[:20]}, which is missing.')
            if obj['mesh']:
                vertices, triangles = obj['mesh']
                count = len(vertices) // 3
                for i in range(0, len(triangles), 3):
                    for v in triangles[i:i + 3]:
                        if not 0 <= v < count:
                            raise ModelError('The 3MF file has a triangle that points to a missing vertex.')
                        out.extend(_apply(transform, vertices[v * 3], vertices[v * 3 + 1], vertices[v * 3 + 2]))
            for child, matrix, child_path in obj['components']:
                collect(child_path or path, child, _combine(matrix, transform), out, depth + 1)
            return obj['name']

        objects = []
        for object_id, transform, path in main.items:
            out = array('f')
            name = collect(path or main.path, object_id, transform, out, 0)
            if out:
                objects.append(((name or f'{default_name} {len(objects) + 1}')[:100], out))
    if not objects:
        raise ModelError('The 3MF file has no objects to print.')
    if len(objects) == 1 and not objects[0][0].strip():
        objects = [(default_name, objects[0][1])]
    return objects


def summary(values):
    """What the Slicer tab needs about an imported object (as meshes.summary() for an STL)."""
    low, high = meshes.bounds(values)
    return dict(triangles=len(values) // 9, size=[round(high[i] - low[i], 3) for i in range(3)],
                min=[round(v, 3) for v in low], max=[round(v, 3) for v in high])
