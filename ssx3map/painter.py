"""World painter records (world record kind 15): fog, glare, screen tint, sun, lighting.

Layout (ssxdecomp notes, asset-formats.md "Painter records", and ssx-web's
tools/import_sky.py): three u32 {0x10, 0x0E, 0x40}, then thirteen u32 section
offsets for painter types 1..13 (0xFFFFFFFF = absent). A section is
{u32 header_size, u32 count, u32 0xC, (u32 type, u32 payload_offset) x count},
then a spatial quadtree, then the payloads; payload offsets are relative to the
section start.

Fog payload (type 5), 7 floats: blend rate, density, near (cm), far (cm), R, G, B
(colour 0..1). Every location has one painter record.
"""
from __future__ import annotations

import math
import struct

TYPES = {5: 'fog', 6: 'glare', 7: 'screen_tint', 8: 'skybox', 9: 'sun', 11: 'lighting', 12: 'weather'}
FOG = 5
FOG_FIELDS = ('blend_rate', 'density', 'near_cm', 'far_cm', 'r', 'g', 'b')


class PainterError(ValueError):
    pass


def sections(data):
    """{painter type: (start, end)} offsets within the record."""
    if len(data) < 0x40:
        return {}
    head = struct.unpack_from('<3I', data, 0)
    if head[0] != 0x10 or head[2] != 0x40:
        raise PainterError(f'unexpected painter header {head}')
    offsets = struct.unpack_from('<13I', data, 12)
    valid = sorted(o for o in offsets if o != 0xFFFFFFFF)
    if any(o >= len(data) for o in valid):
        raise PainterError('painter section outside the record')
    result = {}
    for i, o in enumerate(offsets):
        if o == 0xFFFFFFFF:
            continue
        end = min([v for v in valid if v > o] + [len(data)])
        result[i + 1] = (o, end)
    return result


def entries(data, painter_type, words):
    """[(absolute payload offset in the record, entry type, values)] of one section."""
    span = sections(data).get(painter_type)
    if span is None:
        return []
    start, end = span
    header_size, count, third = struct.unpack_from('<3I', data, start)
    if count > 4096 or third != 0xC or 12 + 8 * count > end - start:
        raise PainterError(f'unexpected painter section header ({header_size}, {count}, {third})')
    result = []
    for i in range(count):
        kind, offset = struct.unpack_from('<2I', data, start + 12 + 8 * i)
        at = start + offset
        if at + 4 * words > end:
            raise PainterError('painter payload outside its section')
        result.append((at, kind, list(struct.unpack_from(f'<{words}f', data, at))))
    return result


def fog_entries(data):
    out = []
    for at, kind, values in entries(data, FOG, 7):
        if kind != FOG:
            raise PainterError(f'fog section holds an entry of type {kind}')
        out.append((at, dict(zip(FOG_FIELDS, values))))
    return out


def edit_fog(buf, record_offset, record_size, changes):
    """Apply `changes` (a dict of FOG_FIELDS -> value or callable(old) -> new) in place.

    Returns [(before, after)] per fog entry.
    """
    data = bytes(buf[record_offset:record_offset + record_size])
    done = []
    for at, fog in fog_entries(data):
        new = dict(fog)
        for key, value in changes.items():
            if key not in FOG_FIELDS:
                raise KeyError(key)
            new[key] = value(fog[key]) if callable(value) else float(value)
        if new['far_cm'] <= new['near_cm']:
            raise PainterError(f"fog far ({new['far_cm']}) must be greater than near ({new['near_cm']})")
        if not all(math.isfinite(v) for v in new.values()):
            raise PainterError('fog values must be finite')
        struct.pack_into('<7f', buf, record_offset + at, *(new[k] for k in FOG_FIELDS))
        done.append((fog, new))
    return done
