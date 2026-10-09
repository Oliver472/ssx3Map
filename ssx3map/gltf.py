"""Minimal glTF 2.0 files: write meshes and markers for Blender, read triangles back.

glTF (and Blender's glTF importer and exporter) use +Y up; ssx3map uses +Z up, so a
point (x, y, z) is stored as (x, z, -y) and read back as (x, -z, y). Units are metres.
Reading follows the node hierarchy (matrix or translation/rotation/scale), takes
triangle primitives (lists, strips and fans) and skips lines and points.
"""
from __future__ import annotations

import base64
import json
import math
import os
import struct

TRIANGLES, LINES, POINTS = 4, 1, 0
FLOAT, U8, U16, U32 = 5126, 5121, 5123, 5125
_COMPONENT = {5120: 'b', 5121: 'B', 5122: 'h', 5123: 'H', 5125: 'I', 5126: 'f'}
_WIDTH = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}


class GltfError(ValueError):
    pass


def _to_gltf(p):
    return (p[0], p[2], -p[1])


def _from_gltf(p):
    return (p[0], -p[2], p[1])


def _normals(points, triangles):
    acc = [[0.0, 0.0, 0.0] for _ in points]
    for a, b, c in triangles:
        pa, pb, pc = points[a], points[b], points[c]
        u = [pb[k] - pa[k] for k in range(3)]
        v = [pc[k] - pa[k] for k in range(3)]
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        for i in (a, b, c):
            for k in range(3):
                acc[i][k] += n[k]
    out = []
    for n in acc:
        length = math.sqrt(sum(x * x for x in n)) or 1.0
        out.append(tuple(x / length for x in n))
    return out


def write_glb(path, meshes, markers=(), extras=None):
    """Write a .glb file.

    meshes:  [dict(name, points=[(x, y, z)], triangles=[(a, b, c)] or lines=[(a, b)],
                   color=(r, g, b, a))], Z up, metres;
    markers: [dict(name, at=(x, y, z))]: empties (Blender shows them as axes);
    extras:  a dict kept as the scene's extras."""
    blob = bytearray()
    views, accessors, materials, gl_meshes, nodes = [], [], [], [], []

    def add_view(data, target=None):
        while len(blob) % 4:
            blob.append(0)
        view = dict(buffer=0, byteOffset=len(blob), byteLength=len(data))
        if target:
            view['target'] = target
        blob.extend(data)
        views.append(view)
        return len(views) - 1

    def add_accessor(values, kind, component):
        flat = [v for item in values for v in (item if isinstance(item, (tuple, list)) else (item,))]
        data = struct.pack(f'<{len(flat)}{_COMPONENT[component]}', *flat)
        target = 34963 if kind == 'SCALAR' else 34962
        acc = dict(bufferView=add_view(data, target), componentType=component, count=len(values), type=kind)
        if kind == 'VEC3' and component == FLOAT:
            acc['min'] = [min(v[k] for v in values) for k in range(3)]
            acc['max'] = [max(v[k] for v in values) for k in range(3)]
        accessors.append(acc)
        return len(accessors) - 1

    for mesh in meshes:
        pts = [_to_gltf(p) for p in mesh['points']]
        if not pts:
            continue
        attributes = dict(POSITION=add_accessor(pts, 'VEC3', FLOAT))
        if mesh.get('triangles'):
            mode, items = TRIANGLES, [i for t in mesh['triangles'] for i in t]
            attributes['NORMAL'] = add_accessor([_to_gltf(n) for n in _normals(mesh['points'], mesh['triangles'])],
                                                'VEC3', FLOAT)
        elif mesh.get('lines'):
            mode, items = LINES, [i for e in mesh['lines'] for i in e]
        else:
            mode, items = POINTS, list(range(len(pts)))
        index_type = U16 if len(pts) < 65536 else U32
        prim = dict(attributes=attributes, indices=add_accessor(items, 'SCALAR', index_type), mode=mode)
        color = list(mesh.get('color', (0.9, 0.92, 0.95, 1.0)))
        materials.append(dict(name=mesh['name'], doubleSided=True,
                              pbrMetallicRoughness=dict(baseColorFactor=color, metallicFactor=0.0,
                                                        roughnessFactor=0.9)))
        prim['material'] = len(materials) - 1
        gl_meshes.append(dict(name=mesh['name'], primitives=[prim]))
        nodes.append(dict(name=mesh['name'], mesh=len(gl_meshes) - 1))
    for m in markers:
        nodes.append(dict(name=m['name'], translation=list(_to_gltf(m['at']))))
    doc = dict(asset=dict(version='2.0', generator='ssx3map'), scene=0,
               scenes=[dict(name='Scene', nodes=list(range(len(nodes))))],
               nodes=nodes, meshes=gl_meshes, materials=materials, accessors=accessors, bufferViews=views,
               buffers=[dict(byteLength=len(blob))])
    if extras:
        doc['scenes'][0]['extras'] = extras
    text = json.dumps(doc, separators=(',', ':')).encode()
    text += b' ' * (-len(text) % 4)
    while len(blob) % 4:
        blob.append(0)
    total = 12 + 8 + len(text) + 8 + len(blob)
    with open(path, 'wb') as f:
        f.write(struct.pack('<4sII', b'glTF', 2, total))
        f.write(struct.pack('<I4s', len(text), b'JSON') + text)
        f.write(struct.pack('<I4s', len(blob), b'BIN\0') + bytes(blob))


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _load(path):
    with open(path, 'rb') as f:
        data = f.read()
    if data[:4] == b'glTF':
        _, _, total = struct.unpack_from('<4sII', data, 0)
        pos, doc, binary = 12, None, b''
        while pos + 8 <= min(total, len(data)):
            length, kind = struct.unpack_from('<I4s', data, pos)
            chunk = data[pos + 8:pos + 8 + length]
            if kind == b'JSON':
                doc = json.loads(chunk.decode('utf-8'))
            elif kind == b'BIN\0':
                binary = chunk
            pos += 8 + length
        if doc is None:
            raise GltfError(f'{path}: no JSON chunk')
    else:
        try:
            doc = json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise GltfError(f'{path} is neither a .glb nor a .gltf file') from None
        binary = b''
    buffers = []
    for i, b in enumerate(doc.get('buffers', [])):
        uri = b.get('uri')
        if uri is None:
            buffers.append(binary)
        elif uri.startswith('data:'):
            buffers.append(base64.b64decode(uri.split(',', 1)[1]))
        else:
            with open(os.path.join(os.path.dirname(os.path.abspath(path)), uri), 'rb') as f:
                buffers.append(f.read())
    return doc, buffers


def _accessor(doc, buffers, index):
    acc = doc['accessors'][index]
    if 'sparse' in acc:
        raise GltfError('sparse accessors are not supported; export without them')
    count, kind, comp = acc['count'], acc['type'], acc['componentType']
    width = _WIDTH[kind]
    if 'bufferView' not in acc:
        return [tuple([0] * width)] * count
    view = doc['bufferViews'][acc['bufferView']]
    data = buffers[view['buffer']]
    size = struct.calcsize(_COMPONENT[comp])
    stride = view.get('byteStride') or size * width
    start = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
    fmt = f'<{width}{_COMPONENT[comp]}'
    return [struct.unpack_from(fmt, data, start + i * stride) for i in range(count)]


def _matrix(node):
    if 'matrix' in node:
        m = node['matrix']                       # column major
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    t = node.get('translation', [0.0, 0.0, 0.0])
    x, y, z, w = node.get('rotation', [0.0, 0.0, 0.0, 1.0])
    s = node.get('scale', [1.0, 1.0, 1.0])
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[r[i][0] * s[0], r[i][1] * s[1], r[i][2] * s[2], t[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def read_triangles(path):
    """[(node name, [(p0, p1, p2)])] of every triangle mesh in the file: Z up, metres, with the
    node transforms applied."""
    doc, buffers = _load(path)
    required = set(doc.get('extensionsRequired', []))
    if required & {'KHR_draco_mesh_compression', 'EXT_meshopt_compression'}:
        raise GltfError('the file uses mesh compression; export it again with compression off')
    nodes = doc.get('nodes', [])
    scene = doc.get('scenes', [dict(nodes=list(range(len(nodes))))])[doc.get('scene', 0)]
    out = []
    eye = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    todo = [(n, eye) for n in scene.get('nodes', [])]
    while todo:
        index, parent = todo.pop()
        node = nodes[index]
        m = _mul(parent, _matrix(node))
        todo += [(c, m) for c in node.get('children', [])]
        if 'mesh' not in node:
            continue
        tris = []
        for prim in doc['meshes'][node['mesh']].get('primitives', []):
            mode = prim.get('mode', TRIANGLES)
            if mode not in (4, 5, 6) or 'POSITION' not in prim.get('attributes', {}):
                continue
            pos_acc = doc['accessors'][prim['attributes']['POSITION']]
            if pos_acc['componentType'] != FLOAT:
                raise GltfError('positions must be floats; export without quantization')
            pts = _accessor(doc, buffers, prim['attributes']['POSITION'])
            world = [_from_gltf(tuple(sum(m[i][k] * v for k, v in enumerate((*p, 1.0))) for i in range(3)))
                     for p in pts]
            idx = [i[0] for i in _accessor(doc, buffers, prim['indices'])] if 'indices' in prim \
                else list(range(len(pts)))
            if mode == 4:
                faces = [idx[k:k + 3] for k in range(0, len(idx) - 2, 3)]
            elif mode == 5:
                faces = [(idx[k], idx[k + 1], idx[k + 2]) if k % 2 == 0 else (idx[k + 1], idx[k], idx[k + 2])
                         for k in range(len(idx) - 2)]
            else:
                faces = [(idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1)]
            tris += [(world[a], world[b], world[c]) for a, b, c in faces]
        if tris:
            out.append((node.get('name', f'node {index}'), tris))
    return out


def read_markers(path):
    """{name: (x, y, z)} of the nodes without a mesh (empties), Z up, metres (top-level only)."""
    doc, _ = _load(path)
    out = {}
    for node in doc.get('nodes', []):
        if 'mesh' not in node and 'name' in node:
            m = _matrix(node)
            out[node['name']] = _from_gltf((m[0][3], m[1][3], m[2][3]))
    return out


class MeshSurface:
    """Height of a set of triangles: the highest one over (x, y), or None."""

    def __init__(self, triangles, cell=4.0):
        self.cell = cell
        self.cells = {}
        self.count = 0
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        for t in triangles:
            (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
            det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(det) < 1e-12:
                continue                        # vertical or degenerate: no height to give
            self.count += 1
            entry = (ax, ay, az, bx, by, bz, cx, cy, cz, det)
            for k, v in enumerate((min(ax, bx, cx), min(ay, by, cy), min(az, bz, cz))):
                lo[k] = min(lo[k], v)
            for k, v in enumerate((max(ax, bx, cx), max(ay, by, cy), max(az, bz, cz))):
                hi[k] = max(hi[k], v)
            for gx in range(math.floor(min(ax, bx, cx) / cell), math.floor(max(ax, bx, cx) / cell) + 1):
                for gy in range(math.floor(min(ay, by, cy) / cell), math.floor(max(ay, by, cy) / cell) + 1):
                    self.cells.setdefault((gx, gy), []).append(entry)
        self.lo, self.hi = tuple(lo), tuple(hi)

    def __call__(self, x, y):
        best = None
        for ax, ay, az, bx, by, bz, cx, cy, cz, det in self.cells.get((math.floor(x / self.cell),
                                                                       math.floor(y / self.cell)), ()):
            l1 = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / det
            l2 = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / det
            l3 = 1.0 - l1 - l2
            if l1 < -1e-9 or l2 < -1e-9 or l3 < -1e-9:
                continue
            z = l1 * az + l2 * bz + l3 * cz
            if best is None or z > best:
                best = z
        return best


def load_surface(path):
    """MeshSurface of every triangle mesh in a .glb/.gltf file (course frame: metres, Z up)."""
    tris = [t for _, ts in read_triangles(path) for t in ts]
    if not tris:
        raise GltfError(f'{path} has no triangle meshes')
    return MeshSurface(tris)
