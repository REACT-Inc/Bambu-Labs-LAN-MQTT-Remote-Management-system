"""STL meshes: read and check an uploaded STL (untrusted), and write one placed on the bed.

Standard library only, so the worker can use it too. Both binary and ASCII STL are read. A binary STL's size must match
its triangle count exactly, and files are capped (MAX_BYTES, MAX_TRIANGLES) before they are read. The Pi only streams
through a file (summary()); a worker keeps one as 4-byte floats (triangles()), about 36 bytes per triangle.
Coordinates are millimetres, Z up, like OrcaSlicer's bed.
"""
import math
import re
import struct
from array import array

MAX_BYTES = 200 * 1024 * 1024
MAX_TRIANGLES = 3_000_000
_FACET = struct.Struct('<12fH')
_NUMBER = rb'([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)'
_VERTEX = re.compile(rb'vertex\s+' + _NUMBER + rb'\s+' + _NUMBER + rb'\s+' + _NUMBER)


class MeshError(ValueError):
    pass


def kind(data):
    """'binary' or 'ascii' for STL bytes; raises MeshError for anything else."""
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < 15:
        raise MeshError('This is not an STL file (it is empty or too short).')
    if len(data) > MAX_BYTES:
        raise MeshError(f'The STL is larger than {MAX_BYTES // 1048576} MB.')
    if len(data) >= 84:
        count = struct.unpack_from('<I', data, 80)[0]
        if 84 + 50 * count == len(data):
            if count == 0:
                raise MeshError('The STL has no triangles.')
            if count > MAX_TRIANGLES:
                raise MeshError(f'The STL has {count:,} triangles; at most {MAX_TRIANGLES:,} can be sliced here.')
            return 'binary'
    head = bytes(data[:4096]).lstrip().lower()
    if head.startswith(b'solid') and b'facet' in head:
        return 'ascii'
    raise MeshError("This is not a valid STL file: its size doesn't match a binary STL and it isn't ASCII STL.")


def corners(data):
    """Every triangle corner as (x, y, z), checked: finite, and not too many triangles."""
    if kind(data) == 'binary':
        for values in _FACET.iter_unpack(memoryview(data)[84:]):
            for i in (3, 6, 9):
                point = values[i:i + 3]
                if not (math.isfinite(point[0]) and math.isfinite(point[1]) and math.isfinite(point[2])):
                    raise MeshError('The STL contains invalid coordinates (NaN or infinity).')
                yield point
        return
    count = 0
    for match in _VERTEX.finditer(data):
        point = tuple(float(v) for v in match.groups())
        if not all(math.isfinite(v) for v in point):
            raise MeshError('The STL contains invalid coordinates (NaN or infinity).')
        count += 1
        if count > MAX_TRIANGLES * 3:
            raise MeshError(f'The STL has more than {MAX_TRIANGLES:,} triangles.')
        yield point
    if count == 0 or count % 3:
        raise MeshError('The ASCII STL has no complete triangles.')


def summary(data):
    """What the dashboard needs to know about an uploaded STL: triangle count and size in millimetres."""
    low, high, count = [math.inf] * 3, [-math.inf] * 3, 0
    for point in corners(data):
        count += 1
        for i in range(3):
            if point[i] < low[i]:
                low[i] = point[i]
            if point[i] > high[i]:
                high[i] = point[i]
    return dict(triangles=count // 3, size=[round(high[i] - low[i], 3) for i in range(3)],
                min=[round(v, 3) for v in low], max=[round(v, 3) for v in high])


def triangles(data):
    """The mesh as a flat array of 4-byte floats, 9 per triangle (three corners, x y z)."""
    out = array('f')
    for point in corners(data):
        out.extend(point)
    return out


def check_matrix(matrix):
    """A 4x4 transform as 16 numbers in column-major order (three.js Matrix4.elements): affine, finite, not flat."""
    if not isinstance(matrix, (list, tuple)) or len(matrix) != 16:
        raise MeshError('Each object needs a 4x4 transform (16 numbers).')
    try:
        m = [float(v) for v in matrix]
    except (TypeError, ValueError):
        raise MeshError('An object transform contains something that is not a number.') from None
    if not all(math.isfinite(v) for v in m) or any(abs(v) > 1e6 for v in m):
        raise MeshError('An object transform is out of range.')
    if abs(m[3]) > 1e-9 or abs(m[7]) > 1e-9 or abs(m[11]) > 1e-9 or abs(m[15] - 1) > 1e-9:
        raise MeshError('Object transforms must be affine (move, rotate and scale only).')
    if abs(determinant(m)) < 1e-9:
        raise MeshError('An object is scaled to zero size.')
    return m


def determinant(m):
    a, b, c = m[0:3], m[4:7], m[8:11]
    return a[0] * (b[1] * c[2] - b[2] * c[1]) - b[0] * (a[1] * c[2] - a[2] * c[1]) + c[0] * (a[1] * b[2] - a[2] * b[1])


def transform(values, matrix):
    """The coordinates moved by a column-major 4x4 matrix (three.js order). A mirroring transform swaps two corners of
    each triangle so its faces still point outwards."""
    m = check_matrix(matrix)
    out = array('f', bytes(len(values) * 4))
    for i in range(0, len(values), 3):
        x, y, z = values[i], values[i + 1], values[i + 2]
        out[i] = m[0] * x + m[4] * y + m[8] * z + m[12]
        out[i + 1] = m[1] * x + m[5] * y + m[9] * z + m[13]
        out[i + 2] = m[2] * x + m[6] * y + m[10] * z + m[14]
    if determinant(m) < 0:
        for i in range(0, len(out), 9):
            out[i + 3:i + 6], out[i + 6:i + 9] = out[i + 6:i + 9], out[i + 3:i + 6]
    return out


def bounds(values):
    """((min x, y, z), (max x, y, z)) of a flat coordinate array."""
    xs, ys, zs = values[0::3], values[1::3], values[2::3]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def write_binary_stl(values, stream, name='object'):
    """Write a flat coordinate array as a binary STL, with each triangle's normal worked out."""
    count = len(values) // 9
    stream.write(f'binary {name}'[:80].encode('ascii', 'replace').ljust(80, b' '))   # never starts with "solid" (ASCII STL)
    stream.write(struct.pack('<I', count))
    for i in range(0, count * 9, 9):
        ax, ay, az, bx, by, bz, cx, cy, cz = values[i:i + 9]
        ux, uy, uz, vx, vy, vz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        stream.write(_FACET.pack(nx / length, ny / length, nz / length, ax, ay, az, bx, by, bz, cx, cy, cz, 0))
