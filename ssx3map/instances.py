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
