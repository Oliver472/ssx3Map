"""World textures (record kind 9, PS2 SHAPE records) and a minimal PNG writer.

Shape record: u8 format (1 = 4-bit paletted, 2 = 8-bit paletted, 5 = 32-bit
RGBA), u24 offset of the next record (the palette for formats 1/2), u16 width
at +4, u16 height at +6; texels start at +0x80. The palette record has its own
128-byte header (u24 record size at +1, u16 used colour count at +8) and stores
RGBA quads, alpha 0..128, in GS CSM1 order. Layouts from SSX-Library's
WorldSSH/OldShapeHandler, the ssxdecomp notes and ssx-web's tools/world_assets.py.

Every copy of a texture id in bam.ssb is byte-identical, so an edit is applied
to every copy (`World.texture_records`).
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


class TextureError(ValueError):
    pass


@dataclass
class Shape:
    fmt: int
    next_offset: int
    width: int
    height: int
    palette_offset: int     # offset of the palette colours, or None
    palette_entries: int    # stored palette entries (may exceed the used count)
    palette_used: int
    texel_end: int

    @property
    def paletted(self):
        return self.fmt in (1, 2)


def parse(data):
    if len(data) < 0x80:
        raise TextureError('texture record shorter than its header')
    fmt = data[0]
    next_offset = int.from_bytes(data[1:4], 'little')
    width, height = struct.unpack_from('<HH', data, 4)
    if not width or not height or max(width, height) > 4096:
        raise TextureError(f'bad texture size {width}x{height}')
    if fmt == 5:
        end = next_offset if next_offset >= 0x80 else 0x80 + width * height * 4
        return Shape(fmt, next_offset, width, height, None, 0, 0, min(end, len(data)))
    if fmt not in (1, 2):
        raise TextureError(f'unsupported texture format {fmt}')
    if next_offset < 0x80 or next_offset + 0x80 > len(data):
        raise TextureError('palette outside the texture record')
    psize = int.from_bytes(data[next_offset + 1:next_offset + 4], 'little')
    used, = struct.unpack_from('<H', data, next_offset + 8)
    start = next_offset + 0x80
    available = (len(data) - start) // 4
    stored = (psize - 0x80) // 4 if psize >= 0x84 else used
    entries = max(0, min(stored, available))
    if not entries:
        raise TextureError('empty palette')
    return Shape(fmt, next_offset, width, height, start, entries, used, next_offset)


def _clamp(v):
    return 0 if v < 0 else 255 if v > 255 else int(round(v))


def tint(buf, offset, size, mul=(1.0, 1.0, 1.0), add=(0, 0, 0)):
    """Recolour a texture record in place: c' = c * mul + add, alpha untouched.

    Paletted textures are recoloured through the palette, which also covers every
    mip level. Returns the number of colours changed.
    """
    data = bytes(buf[offset:offset + size])
    shape = parse(data)
    if shape.paletted:
        start, count = shape.palette_offset, shape.palette_entries
    else:
        start, count = 0x80, (shape.texel_end - 0x80) // 4
    changed = 0
    for i in range(count):
        at = offset + start + 4 * i
        r, g, b = buf[at], buf[at + 1], buf[at + 2]
        nr = _clamp(r * mul[0] + add[0])
        ng = _clamp(g * mul[1] + add[1])
        nb = _clamp(b * mul[2] + add[2])
        if (nr, ng, nb) != (r, g, b):
            buf[at], buf[at + 1], buf[at + 2] = nr, ng, nb
            changed += 1
    return changed


def _csm1(i):
    return (i & 0xE7) | ((i & 8) << 1) | ((i & 16) >> 1)


def decode_rgba(data, raw_alpha=False):
    """Base mip level as (width, height, RGBA bytes).

    Alpha in the GS range 0..128 is rescaled to 0..255 unless `raw_alpha` (light
    pages keep their 0..255 alpha: it is a gain, not a coverage)."""
    shape = parse(data)
    w, h = shape.width, shape.height
    if shape.fmt == 5:
        raw = bytearray(data[0x80:0x80 + w * h * 4])
        if len(raw) != w * h * 4:
            raise TextureError('truncated RGBA texture')
        if not raw_alpha and max(raw[3::4], default=0) <= 128:
            raw[3::4] = bytes(min(255, a * 2) for a in raw[3::4])
        return w, h, bytes(raw)
    pal_count = 256 if shape.fmt == 2 else 16
    palette = []
    for i in range(pal_count):
        src = _csm1(i) if shape.fmt == 2 else i
        at = shape.palette_offset + 4 * src
        palette.append(bytes(data[at:at + 4]) if src < shape.palette_entries else b'\0\0\0\0')
    if max(p[3] for p in palette) <= 128:
        palette = [p[:3] + bytes([min(255, p[3] * 2)]) for p in palette]
    texels = data[0x80:shape.next_offset]
    out = bytearray(w * h * 4)
    if shape.fmt == 1 and texels and len(set(texels)) == 1:
        # Uniform tile: any texel order gives the same image.
        return w, h, palette[texels[0] & 15] * (w * h)
    pages_v = (h + 127) // 128
    pages_h = (w + 127) // 128
    for y in range(h):
        swap = (((y + 2) >> 2) & 1) * 4
        row = (((y & ~3) >> 1) + (y & 1)) & 7
        for x in range(w):
            # PS2 GS PSMT8 / PSMT4 page swizzle, as stored by the original tools.
            if shape.fmt == 2:
                address = ((y & ~15) * w + (x & ~15) * 2 + row * w * 2
                           + ((x + swap) & 7) * 4 + ((y >> 1) & 1) + ((x >> 2) & 2))
                shift = 0
                mask = 255
            else:
                page = (y // 128) * pages_h + x // 128
                address = ((page // pages_v) * 32 * h * 2 + (page % pages_v) * 64 * 4
                           + (((x & 127) & ~31) >> 1) * h + ((y & 127) & ~15) * 2
                           + row * h * 2 + ((x + swap) & 7) * 4 + ((x >> 3) & 3))
                shift = ((y >> 1) & 1) * 4
                mask = 15
            if address >= len(texels):
                raise TextureError('swizzled texel outside the image')
            index = (texels[address] >> shift) & mask
            at = (y * w + x) * 4
            out[at:at + 4] = palette[index]
    return w, h, bytes(out)


def encode_png(width, height, rgba, level=6):
    if len(rgba) != width * height * 4:
        raise ValueError('PNG payload size mismatch')
    raw = b''.join(b'\0' + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, level)) + chunk(b'IEND', b''))


def write_png(path, width, height, rgba):
    with open(path, 'wb') as f:
        f.write(encode_png(width, height, rgba, level=9))
