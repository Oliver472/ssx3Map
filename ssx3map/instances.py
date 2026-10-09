"""Object instances (world record kind 3).

    +0x10  world matrix, 4 x 4 floats, row-major; translation row at +0x40
    +0x50  bounding sphere (centre xyz = bbox centre, radius)
    +0x60  bbox min, +0x6C bbox max
    +0x78  resource word (rid << 8 | track)
    +0x80  model resource word

Collision meshes (kind 12) are stored in the model's local space and placed by
this matrix at run time (ssxdecomp notes, collision.md), so moving an instance
moves its collision with it.
"""
from __future__ import annotations

import math
import struct

MATRIX = 0x10
TRANSLATION = 0x40
SPHERE = 0x50
BBOX_MIN = 0x60
BBOX_MAX = 0x6C
RESOURCE = 0x78
MODEL = 0x80


class Instance:
    def __init__(self, data):
        if len(data) < 0x90:
            raise ValueError('instance record too short')
        self.translation = struct.unpack_from('<3f', data, TRANSLATION)
        self.sphere = struct.unpack_from('<4f', data, SPHERE)
        self.bbox = (struct.unpack_from('<3f', data, BBOX_MIN), struct.unpack_from('<3f', data, BBOX_MAX))
        self.resource, = struct.unpack_from('<I', data, RESOURCE)
        self.model, = struct.unpack_from('<I', data, MODEL)

    @property
    def centre(self):
        lo, hi = self.bbox
        return tuple((a + b) / 2 for a, b in zip(lo, hi))

    @property
    def size(self):
        lo, hi = self.bbox
        return tuple(b - a for a, b in zip(lo, hi))


def translate(buf, offset, dx, dy, dz):
    """Move the instance at buf[offset:] by (dx, dy, dz) cm: matrix, sphere and box."""
    d = (dx, dy, dz)
    for base in (TRANSLATION, SPHERE, BBOX_MIN, BBOX_MAX):
        values = struct.unpack_from('<3f', buf, offset + base)
        struct.pack_into('<3f', buf, offset + base, *(v + e for v, e in zip(values, d)))


def rotate_z(buf, offset, degrees):
    """Turn the instance about the vertical axis through its origin (collision turns with it).

    The matrix is row-vector style (world = v * M), so each rotation row r becomes
    r * Rz. The box is rebuilt from its rotated corners (conservative)."""
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    for row in range(3):
        at = offset + MATRIX + 16 * row
        x, y, z = struct.unpack_from('<3f', buf, at)
        struct.pack_into('<3f', buf, at, x * c - y * s, x * s + y * c, z)
    tx, ty, _ = struct.unpack_from('<3f', buf, offset + TRANSLATION)

    def turn(x, y):
        dx, dy = x - tx, y - ty
        return tx + dx * c - dy * s, ty + dx * s + dy * c

    lo = struct.unpack_from('<3f', buf, offset + BBOX_MIN)
    hi = struct.unpack_from('<3f', buf, offset + BBOX_MAX)
    corners = [turn(x, y) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])]
    struct.pack_into('<3f', buf, offset + BBOX_MIN, min(p[0] for p in corners), min(p[1] for p in corners), lo[2])
    struct.pack_into('<3f', buf, offset + BBOX_MAX, max(p[0] for p in corners), max(p[1] for p in corners), hi[2])
    sx, sy, sz = struct.unpack_from('<3f', buf, offset + SPHERE)
    struct.pack_into('<2f', buf, offset + SPHERE, *turn(sx, sy))
