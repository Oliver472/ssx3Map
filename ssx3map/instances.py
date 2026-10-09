"""Object instances (world record kind 3) and particle instances (kind 5).

    +0x10  world matrix, 4 x 4 floats, row-major; translation row at +0x40
    +0x50  bounding sphere (centre xyz = bbox centre, radius)
    +0x60  bbox min, +0x6C bbox max
    +0x78  resource word (rid << 8 | track)
    +0x80  model resource word

Collision meshes (kind 12) are stored in the model's local space and placed by
this matrix at run time (ssxdecomp notes, collision.md), so moving an instance
moves its collision with it.

A particle instance (kind 5, 144 bytes, SSX-Library WorldParticleInstance) has
the same matrix and sphere, two resource words at +0x60/+0x64 and its box at
+0x68/+0x74.
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

# Where the matrix, sphere and box sit, per record kind.
LAYOUTS = {3: dict(lo=BBOX_MIN, hi=BBOX_MAX, size=0x90), 5: dict(lo=0x68, hi=0x74, size=0x80)}


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


def turn_and_move(buf, offset, yaw, d):
    """Turn the record about the vertical axis through its origin by `yaw` radians,
    then move it by d (cm). Matrix only; see rebuild_bounds for the box and sphere."""
    if yaw:
        c, s = math.cos(yaw), math.sin(yaw)
        for row in range(3):
            at = offset + MATRIX + 16 * row
            x, y, z = struct.unpack_from('<3f', buf, at)
            struct.pack_into('<3f', buf, at, x * c - y * s, x * s + y * c, z)
    t = struct.unpack_from('<3f', buf, offset + TRANSLATION)
    struct.pack_into('<3f', buf, offset + TRANSLATION, *(a + b for a, b in zip(t, d)))


def _yaw(matrix):
    row = 0 if math.hypot(matrix[0], matrix[1]) > 1e-3 else 1
    return math.atan2(matrix[4 * row + 1], matrix[4 * row])


def rebuild_bounds(buf, offset, original, kind=3):
    """Box and sphere of the record at buf[offset:] from the record on the disc (`original`).

    Only turns about Z and moves ever change a matrix here, so the record is the
    disc record turned by the difference in yaw about the disc origin and carried
    to the new origin. Rebuilding from the disc box keeps repeated turns from
    growing the box."""
    lay = LAYOUTS[kind]
    om = struct.unpack_from('<16f', original, MATRIX)
    nm = struct.unpack_from('<16f', buf, offset + MATRIX)
    yaw = _yaw(nm) - _yaw(om)
    c, s = math.cos(yaw), math.sin(yaw)
    ot, nt = om[12:15], nm[12:15]

    def place(x, y):
        dx, dy = x - ot[0], y - ot[1]
        return nt[0] + dx * c - dy * s, nt[1] + dx * s + dy * c

    lo = struct.unpack_from('<3f', original, lay['lo'])
    hi = struct.unpack_from('<3f', original, lay['hi'])
    corners = [place(x, y) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])]
    dz = nt[2] - ot[2]
    struct.pack_into('<3f', buf, offset + lay['lo'], min(p[0] for p in corners), min(p[1] for p in corners), lo[2] + dz)
    struct.pack_into('<3f', buf, offset + lay['hi'], max(p[0] for p in corners), max(p[1] for p in corners), hi[2] + dz)
    sx, sy, sz = struct.unpack_from('<3f', original, SPHERE)
    struct.pack_into('<3f', buf, offset + SPHERE, *place(sx, sy), sz + dz)
