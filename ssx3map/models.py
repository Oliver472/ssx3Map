"""World models (record kind 2, PS2 "MDR") and the per-instance baked colours.

Adapted from ssx-web's tools/world_models.py (owattenmaker/ssx-web, GPL-3.0),
which follows GlitcherOG's SSX-Library WorldMDR/WorldInstance. Every access is
bounds-checked; a model that does not decode raises ModelError and is skipped.

Model layout (little endian):
    +4  u32 node count, +8 u32 node table offset, +24 3f position scale,
    +36 u32 data base, +40 u32 material count, +44 material object ids
    node: {u32 parent, u32 mesh header, u32 -, u32 matrix offset}
    mesh header +28: u32 group count, u32 group table offset
    group: u16 material, u16 flags, u32 DMA chain address (+ data base)
The DMA chain points at VIF blocks: a vertex block (strip lengths, s16 UVs /4096,
s16 positions /32768 * scale) and a matching normal block. Vertices are in the
model's space after the node hierarchy; an instance places them with its own
matrix and uniform scale.

Instance colours: one 16-bit colour per model vertex (5:5:5 + bit 15), carried
by VIF UNPACK V4-5 commands in the instance record from +0xA0. The PS2 draws
static models as texture * (c5 << 3) >> 7, so 16 is the unity value.
"""
from __future__ import annotations

import struct

IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
NONE = 0xFFFFFFFF


class ModelError(ValueError):
    pass


def _align16(v):
    return (v + 15) & ~15


def _mul(a, b):
    return tuple(sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4))


def _apply(p, m):
    return [p[0] * m[j] + p[1] * m[4 + j] + p[2] * m[8 + j] + m[12 + j] for j in range(3)]


class _Reader:
    def __init__(self, data):
        self.data = data

    def read(self, fmt, at):
        if at < 0 or at + struct.calcsize('<' + fmt) > len(self.data):
            raise ModelError(f'model data out of range at 0x{at:X}')
        return struct.unpack_from('<' + fmt, self.data, at)

    def u32(self, at):
        return self.read('I', at)[0]


def decode(data):
    """[{'material': (track, rid), 'flags': int, 'positions': [x, y, z, ...],
    'uvs': [u, v, ...], 'indices': [...], 'color_offset': int}] in model space (cm)."""
    r = _Reader(data)
    node_count = r.u32(4)
    node_table = r.u32(8)
    scale = r.read('3f', 24)
    base = r.u32(36)
    material_count = r.u32(40)
    if node_count > 4096 or material_count > 4096:
        raise ModelError('implausible model header')
    materials = []
    for i in range(material_count):
        v = r.u32(44 + 4 * i)
        materials.append((v & 0xFF, v >> 8))
    nodes = []
    for i in range(node_count):
        parent, mesh, _, matrix_at = r.read('4I', node_table + 16 * i)
        matrix = r.read('16f', matrix_at) if matrix_at not in (0, NONE) else IDENTITY
        nodes.append((parent, mesh, matrix))
    resolved = {}

    def world_of(i, seen=()):
        if i in resolved:
            return resolved[i]
        if i >= len(nodes) or i in seen:
            raise ModelError('bad node hierarchy')
        parent, _, m = nodes[i]
        if parent != NONE:
            m = _mul(m, world_of(parent, seen + (i,)))
        resolved[i] = m
        return m

    meshes = []
    color_offset = 0
    for n, (_, mesh, _) in enumerate(nodes):
        if mesh in (0, NONE):
            continue
        m = world_of(n)
        group_count, groups_at = r.read('2I', mesh + 28)
        if group_count > 4096:
            raise ModelError('implausible group count')
        for g in range(group_count):
            group_at = r.u32(groups_at + 4 * g)
            material, flags = r.read('2H', group_at)
            chain = (r.u32(group_at + 4) & 0xFFFFFF) + base
            blocks = []
            for _ in range(4096):
                tag, address = r.read('2I', chain)
                if address >> 24 not in (0, 0x80):
                    raise ModelError('unsupported DMA address flags')
                chain = _align16(chain + 8)
                if tag >> 24 == 0x60:
                    break
                if address & 0xFFFFFF:
                    blocks.append(base + (address & 0xFFFFFF))
            else:
                raise ModelError('unterminated DMA chain')
            pairs, pending, normals_seen = [], None, set()
            for at in blocks:
                if r.read('2I', at) == (0x20000000, 0x40404040):          # a normal block
                    if pending is not None:
                        pairs.append((pending, at))
                        pending = None
                    elif at not in normals_seen:
                        raise ModelError('normal block without vertices')
                    normals_seen.add(at)
                else:
                    if pending is not None:
                        raise ModelError('vertex block without normals')
                    pending = at
            if pending is not None:
                raise ModelError('unpaired vertex block')
            if material >= len(materials):
                raise ModelError('material index out of range')
            first = True
            for vertex_at, _normal_at in pairs:
                at = vertex_at + (32 if first else 0)
                first = False
                strip_count = r.u32(at + 32)
                count = r.u32(at + 40)
                if not 1 <= count <= 256 or strip_count > 256:
                    raise ModelError('unexpected vertex block')
                strips = r.read(f'{strip_count}H', at + 64)
                if sum(strips) != count:
                    raise ModelError('strip lengths do not add up')
                uv_at = _align16(at + 64 + 2 * strip_count) + 16
                pos_at = _align16(uv_at + 4 * count) + 16
                uv_raw = r.read(f'{2 * count}h', uv_at)
                pos_raw = r.read(f'{3 * count}h', pos_at)
                positions, uvs = [], []
                for v in range(count):
                    p = [pos_raw[3 * v + k] / 32768.0 * scale[k] for k in range(3)]
                    positions.extend(_apply(p, m))
                    uvs.extend((uv_raw[2 * v] / 4096.0, uv_raw[2 * v + 1] / 4096.0))
                indices = []
                start = 0
                for length in strips:
                    for v in range(2, length):
                        tri = (start + v - 2, start + v - 1, start + v)
                        indices.extend(tri[::-1] if v & 1 else tri)
                    start += length
                meshes.append(dict(material=materials[material], flags=flags, positions=positions, uvs=uvs,
                                   indices=indices, color_offset=color_offset))
                color_offset += count
    return meshes


def instance_placement(data):
    """(matrix 16f, model (track, rid), uniform scale) of an instance record."""
    if len(data) < 0x88:
        raise ModelError('instance record too short')
    matrix = struct.unpack_from('<16f', data, 0x10)
    v, = struct.unpack_from('<I', data, 0x80)
    scale, = struct.unpack_from('<f', data, 0x84)
    return matrix, (v & 0xFF, v >> 8), scale


def instance_colors(data):
    """Raw 16-bit baked colours of an instance, one per model vertex."""
    colors = []
    at = 0xA0
    while at + 4 <= len(data):
        word, = struct.unpack_from('<I', data, at)
        at += 4
        if word >> 24 not in (0x6F, 0x7F):
            continue
        count = ((word >> 16) & 0xFF) or 256
        if at + 2 * count > len(data):
            break
        colors.extend(struct.unpack_from(f'<{count}H', data, at))
        at = (at + 2 * count + 3) & ~3
    return colors
