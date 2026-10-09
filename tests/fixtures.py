"""Synthetic stand-ins for the game data (no game files are used by the tests)."""
from __future__ import annotations

import random
import struct

from ssx3map import refpack


def record(kind, track, rid, payload):
    return bytes([kind]) + len(payload).to_bytes(3, 'little') + bytes([track]) + rid.to_bytes(3, 'little') + payload


def painter_record(fogs):
    """Kind-15 record with one fog section holding `fogs` (7-float tuples)."""
    count = len(fogs)
    header_size = 0x14 + 8 * (count - 1)
    tree = struct.pack('<3f', 0.01, -1000.0, -1000.0) + struct.pack('<I', 1) + bytes(4) + struct.pack('<H', 0) \
        + bytes(2) + struct.pack('<2I', 0, 0xFFFFFFFF) + bytes(8) + struct.pack('<4H', 0, 0, 0, 0)
    table_end = 12 + 8 * count
    payload_start = table_end + len(tree)
    section = struct.pack('<3I', header_size, count, 0xC)
    for i in range(count):
        section += struct.pack('<2I', 5, payload_start + 28 * i)
    section += tree
    for f in fogs:
        section += struct.pack('<7f', *f)
    offsets = [0xFFFFFFFF] * 13
    offsets[4] = 0x40
    head = struct.pack('<3I', 0x10, 0x0E, 0x40) + struct.pack('<13I', *offsets)
    return head + section


def texture_8bit(w, h, seed):
    rng = random.Random(seed)
    texels = bytes(rng.randrange(16) * 3 for _ in range(w * h))
    header = bytes([2]) + (0x80 + w * h).to_bytes(3, 'little') + struct.pack('<HH', w, h) + bytes(0x80 - 8)
    pal_header = bytes([0x21]) + (0x80 + 256 * 4).to_bytes(3, 'little') + struct.pack('<HHH', 16, 16, 256) \
        + bytes(0x80 - 10)
    palette = b''.join(bytes([i, 255 - i, (i * 7) & 255, 0x80]) for i in range(256))
    return header + texels + pal_header + palette


def texture_rgba(w, h):
    header = bytes([5]) + (0x80 + w * h * 4).to_bytes(3, 'little') + struct.pack('<HH', w, h) + bytes(0x80 - 8)
    return header + b''.join(bytes([x * 8 & 255, y * 8 & 255, 100, 0x80]) for y in range(h) for x in range(w))


def instance_record(x, y, z):
    m = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1)
    return (bytes(16) + struct.pack('<16f', *m) + struct.pack('<4f', x, y, z, 50)
            + struct.pack('<3f', x - 50, y - 50, z - 50) + struct.pack('<3f', x + 50, y + 50, z + 50)
            + bytes(0x40))


def filler(n, seed):
    rng = random.Random(seed)
    out = bytearray()
    while len(out) < n:
        out += struct.pack('<4f', *(rng.uniform(-1e5, 1e5) for _ in range(4))) if rng.random() < 0.5 \
            else bytes(rng.randrange(1, 64))
    return bytes(out[:n])


def encode_block(piece, slack=40, padding=0):
    """An 'original' block stream: `slack` bytes bigger than our best encoding."""
    best = len(refpack.compress(piece))
    stream = refpack.compress_exact(piece, best + slack)
    return stream + bytes(padding)


def build_ssb(chunks, block_decoded=6000, slack=40, padding=0):
    out = bytearray()
    for data in chunks:
        pieces = [data[i:i + block_decoded] for i in range(0, len(data), block_decoded)] or [b'']
        for k, piece in enumerate(pieces):
            payload = encode_block(piece, slack=slack, padding=padding)
            tag = b'CEND' if k == len(pieces) - 1 else b'CBXS'
            out += tag + struct.pack('<I', 8 + len(payload)) + payload
    return bytes(out)


def build_sdb(locations, textures=8, pages=2):
    """locations: [(name, chunk_end)]."""
    head = bytearray(80)
    struct.pack_into('<4s f 3I', head, 0, b'\x01\x00\x00\x00', 1.0, len(locations), 0, 0)
    struct.pack_into('<HH', head, 0x2A, textures, pages)
    body = bytearray()
    prev = -1
    for name, end in locations:
        rec = bytearray(88)
        rec[:len(name)] = name.encode()
        struct.pack_into('<4I', rec, 16, 0, end - prev, end, 0)
        body += rec
        prev = end
    data = head + body
    data += bytes(-len(data) % 16)
    return bytes(data)


def build_big(members, align=2048):
    names = [n for n, _ in members]
    header_size = 16 + sum(8 + len(n) + 1 for n in names)
    pos = header_size
    layout = []
    for _, data in members:
        pos = (pos + align - 1) // align * align
        layout.append(pos)
        pos += len(data)
    out = bytearray(pos)
    out[:4] = b'BIGF'
    struct.pack_into('<I', out, 4, len(out))
    struct.pack_into('>II', out, 8, len(members), header_size)
    p = 16
    for (name, data), off in zip(members, layout):
        struct.pack_into('>II', out, p, off, len(data))
        out[p + 8:p + 8 + len(name)] = name.encode()
        p += 8 + len(name) + 1
        out[off:off + len(data)] = data
    return bytes(out)


def build_world(slack=40, padding=0):
    """A three-location world: AAA (chunks 0-1), A_AAA (2), ASKY (3)."""
    tex_a = texture_8bit(32, 32, 1)
    tex_b = texture_rgba(16, 16)
    fog_a = [(0.5, 0.001, 3000.0, 10000.0, 0.7, 0.8, 1.0), (0.5, 0.002, 2000.0, 9000.0, 0.6, 0.7, 0.9)]
    chunk0 = (record(9, 255, 7, tex_a) + record(3, 0, 1, instance_record(100, 200, 300))
              + record(0, 0, 0, struct.pack('<h', 7) + bytes(30)) + record(4, 0, 0, filler(9000, 1)))
    chunk1 = (record(9, 255, 7, tex_a) + record(9, 255, 3, tex_b) + record(15, 0, 0, painter_record(fog_a))
              + record(16, 0, 0, filler(7000, 2)))
    chunk2 = record(15, 1, 0, painter_record([(0.5, 0.001, 4000.0, 12000.0, 0.5, 0.5, 0.5)])) + record(
        16, 1, 0, filler(3000, 3))
    chunk3 = record(9, 255, 3, tex_b) + record(15, 2, 0, painter_record([(1.0, 0.0, 1.0, 2.0, 0, 0, 0)]))
    chunks = [chunk0, chunk1, chunk2, chunk3]
    ssb = build_ssb(chunks, slack=slack, padding=padding)
    sdb = build_sdb([('AAA', 1), ('A_AAA', 2), ('ASKY', 3)])
    big = build_big([('bam.sdb', sdb), ('bam.ssb', ssb), ('bam.phm', bytes(16)), ('bam.psm', bytes(16))])
    return big, chunks


def _dirrec(name, lba, size, is_dir):
    n = len(name)
    length = 33 + n + (1 - n % 2)
    rec = bytearray(length)
    rec[0] = length
    rec[2:10] = struct.pack('<I', lba) + struct.pack('>I', lba)
    rec[10:18] = struct.pack('<I', size) + struct.pack('>I', size)
    rec[25] = 2 if is_dir else 0
    rec[28:32] = struct.pack('<H', 1) + struct.pack('>H', 1)
    rec[32] = n
    rec[33:33 + n] = name
    return bytes(rec)


def build_iso(big, extra=b'SLUS_207.72 placeholder'):
    """Minimal ISO 9660 image with /SYSTEM.CNF and /DATA/WORLDS/BAM.BIG."""
    S = 2048
    root, data_dir, worlds, cnf = 18, 19, 20, 21
    big_lba = 22
    sectors = big_lba + (len(big) + S - 1) // S + 2
    img = bytearray(sectors * S)
    pvd = bytearray(S)
    pvd[0] = 1
    pvd[1:6] = b'CD001'
    pvd[6] = 1
    pvd[156:156 + 34] = _dirrec(b'\0', root, S, True)
    img[16 * S:17 * S] = pvd
    img[17 * S:17 * S + 6] = b'\xffCD001'

    def directory(lba, parent, children):
        body = _dirrec(b'\0', lba, S, True) + _dirrec(b'\1', parent, S, True) + b''.join(children)
        img[lba * S:lba * S + len(body)] = body
    directory(root, root, [_dirrec(b'DATA', data_dir, S, True), _dirrec(b'SYSTEM.CNF;1', cnf, len(extra), False)])
    directory(data_dir, root, [_dirrec(b'WORLDS', worlds, S, True)])
    directory(worlds, data_dir, [_dirrec(b'BAM.BIG;1', big_lba, len(big), False)])
    img[cnf * S:cnf * S + len(extra)] = extra
    img[big_lba * S:big_lba * S + len(big)] = big
    img[-S:] = b'\xAA' * S        # something after the file that must stay untouched
    return bytes(img)


# --------------------------------------------------------------------------
# A small course with terrain, objects and a race path
# --------------------------------------------------------------------------

PATCH_M = 500.0     # 5 m patches


def height(x, y):
    """A slope falling towards +y with some waviness and a little noise (cm)."""
    import math
    noise = math.sin(x * 12.9898 + y * 78.233) * 43758.5453
    noise = (noise - math.floor(noise) - 0.5) * 6.0
    return -0.3 * y + 150.0 * math.sin(x / 1500.0) + 80.0 * math.cos(y / 2100.0) + noise


def build_ssb_slots(chunks, slot=8192, worse=0.015):
    """Like the retail bam.ssb: fixed-size blocks, each filled with as much data as the
    (here: emulated, `worse` bigger than ours) original encoder could fit, zero padded."""
    cap = slot - 8

    def ea_size(piece):
        return int(len(refpack.compress(piece)) * (1 + worse)) + 4

    out = bytearray()
    for data in chunks:
        pos = 0
        while pos < len(data):
            rest = data[pos:]
            if ea_size(rest) <= cap:
                take = len(rest)
            else:
                good, bad = 1, len(rest)
                while bad - good > 32:
                    mid = (good + bad) // 2
                    if ea_size(rest[:mid]) <= cap:
                        good = mid
                    else:
                        bad = mid
                take = good
            piece = rest[:take]
            stream = refpack.compress_exact(piece, ea_size(piece))
            pos += take
            tag = b'CEND' if pos >= len(data) else b'CBXS'
            out += tag + struct.pack('<I', slot) + stream + bytes(cap - len(stream))
    return bytes(out)


def patch_record(x0, y0, track, rid, corner_order=((0, 0), (1, 0), (0, 1), (1, 1))):
    from ssx3map import terrain
    grid = terrain.GRID
    zs = [[height(x0 + u * PATCH_M, y0 + v * PATCH_M) for u in grid] for v in grid]
    vinv = terrain._VINV
    tmp = [[sum(vinv[i][a] * zs[b][a] for a in range(4)) for i in range(4)] for b in range(4)]
    cz = [[sum(vinv[j][b] * tmp[b][i] for b in range(4)) for i in range(4)] for j in range(4)]
    rec = bytearray(432)
    struct.pack_into('<hh', rec, 8, 2, 9)
    struct.pack_into('<4f', rec, 0x10, 0.05, 0.05, 0.9, 0.9)      # lighting rectangle in the light page
    for j in range(4):
        for i in range(4):
            cx = x0 if (i, j) == (0, 0) else PATCH_M if (i, j) == (1, 0) else 0.0
            cy = y0 if (i, j) == (0, 0) else PATCH_M if (i, j) == (0, 1) else 0.0
            struct.pack_into('<4f', rec, 0x40 + 16 * (15 - (4 * j + i)), cx, cy, cz[j][i], 0.0)
    p = terrain.Patch(bytes(rec))
    corners = p.corners()
    for o, uv in zip(terrain.CORNERS, corner_order):
        struct.pack_into('<3f', rec, o, *corners[uv])
    net = [q for row in p.control_net() for q in row]
    lo = [min(q[k] for q in net) - 0.5 for k in range(3)]
    hi = [max(q[k] for q in net) + 0.5 for k in range(3)]
    struct.pack_into('<3f', rec, terrain.BBOX_MIN, *lo)
    struct.pack_into('<3f', rec, terrain.BBOX_MAX, *hi)
    centre = [(a + b) / 2 for a, b in zip(lo, hi)]
    struct.pack_into('<4f', rec, terrain.SPHERE, *centre, 400.0)
    struct.pack_into('<I', rec, terrain.RESOURCE, (rid << 8) | track)
    struct.pack_into('<hh', rec, terrain.TEXTURE, 7, 0)
    return bytes(rec)


def terrain_grid(x0, y0, nx, ny, track, first_rid=0, corner_order=((0, 0), (1, 0), (0, 1), (1, 1))):
    out = b''
    rid = first_rid
    for j in range(ny):
        for i in range(nx):
            out += record(1, track, rid, patch_record(x0 + i * PATCH_M, y0 + j * PATCH_M, track, rid, corner_order))
            rid += 1
    return out


def placed_instance(x, y, track, rid, size=200.0, model=(0, 5), vertices=8):
    """An instance of `model` with its own baked colours (one per model vertex)."""
    z = height(x, y)
    m = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1)
    data = (bytes(16) + struct.pack('<16f', *m) + struct.pack('<4f', x, y, z + size / 2, size)
            + struct.pack('<3f', x - size / 2, y - size / 2, z) + struct.pack('<3f', x + size / 2, y + size / 2, z + size)
            + struct.pack('<I', (rid << 8) | track) + bytes(4) + struct.pack('<I', (model[1] << 8) | model[0])
            + struct.pack('<f', size / 200.0))
    data += bytes(0xA0 - len(data))
    colours = [(16 + (v % 8)) | (16 << 5) | ((16 - (v % 8)) << 10) | 0x8000 for v in range(vertices)]
    data += struct.pack('<I', 0x6F000000 | (vertices << 16)) + struct.pack(f'<{vertices}H', *colours)
    data += bytes(-len(data) % 16 + 16)
    return record(3, track, rid, data)


def mdr_model(material=(0, 0)):
    """A real-format MDR: one node, one group, two strips forming a 2 m tall cross of quads."""
    scale = (200.0, 200.0, 200.0)
    quads = [[(-0.5, 0.0, 0.0), (0.5, 0.0, 0.0), (-0.5, 0.0, 1.0), (0.5, 0.0, 1.0)],
             [(0.0, -0.5, 0.0), (0.0, 0.5, 0.0), (0.0, -0.5, 1.0), (0.0, 0.5, 1.0)]]
    count = 8
    strips = [4, 4]
    out = bytearray(0x200)
    struct.pack_into('<I', out, 4, 1)                 # nodes
    struct.pack_into('<I', out, 8, 0x60)              # node table
    struct.pack_into('<3f', out, 24, *scale)
    struct.pack_into('<I', out, 36, 0x100)            # data base
    struct.pack_into('<I', out, 40, 1)                # materials
    struct.pack_into('<I', out, 44, (material[1] << 8) | material[0])
    struct.pack_into('<4I', out, 0x60, 0xFFFFFFFF, 0x80, 0, 0xFFFFFFFF)
    struct.pack_into('<2I', out, 0x80 + 28, 1, 0xC0)  # mesh header: 1 group at 0xC0
    struct.pack_into('<I', out, 0xC0, 0xD0)
    struct.pack_into('<HHI', out, 0xD0, 0, 0, 0x00)   # group: material 0, chain at base + 0
    base = 0x100
    # DMA chain: vertex block at base+0x40, normal block at base+0x180, terminator.
    chain = bytearray(48)
    struct.pack_into('<2I', chain, 0, 0x10000000, 0x40)
    struct.pack_into('<2I', chain, 16, 0x10000000, 0x180)
    struct.pack_into('<2I', chain, 32, 0x60000000, 0)
    vertex = bytearray(0x140)
    at = 32                                            # first block starts 32 bytes in
    struct.pack_into('<I', vertex, 0, 0x01000101)
    struct.pack_into('<I', vertex, at + 32, len(strips))
    struct.pack_into('<I', vertex, at + 40, count)
    struct.pack_into(f'<{len(strips)}H', vertex, at + 64, *strips)
    uv_at = ((at + 64 + 2 * len(strips) + 15) & ~15) + 16
    pos_at = ((uv_at + 4 * count + 15) & ~15) + 16
    pts = [p for q in quads for p in q]
    for v, (x, y, z) in enumerate(pts):
        struct.pack_into('<2h', vertex, uv_at + 4 * v, int((x + y + 0.5) * 4096), int((1 - z) * 4096))
        struct.pack_into('<3h', vertex, pos_at + 6 * v, int(x * 32767), int(y * 32767), int(z * 32767))
    normal = bytearray(16 + 6 * count + 8)
    struct.pack_into('<2I', normal, 0, 0x20000000, 0x40404040)
    normal[14] = count
    data = bytes(out[:base]) + bytes(chain) + bytes(0x40 - len(chain)) + bytes(vertex) + bytes(normal)
    return data


def light_page(w=16, h=16):
    header = bytes([5]) + (0x80 + w * h * 4).to_bytes(3, 'little') + struct.pack('<HH', w, h) + bytes(0x80 - 8)
    texels = b''.join(bytes([20 + x * 4, 20 + y * 4, 40, 160]) for y in range(h) for x in range(w))
    return header + texels


def _segments(points):
    """AIP segments as on the disc: horizontal unit direction, slope, horizontal length."""
    import math
    segs = []
    for a, b in zip(points, points[1:]):
        d = [b[k] - a[k] for k in range(3)]
        w = math.hypot(d[0], d[1])
        segs.append((d[0] / w, d[1] / w, d[2] / w, w))
    return segs


def _path_body(points, events):
    lo = [min(p[k] for p in points) - 100 for k in range(3)]
    hi = [max(p[k] for p in points) + 100 for k in range(3)]
    segs = _segments(points)
    out = struct.pack('<II', len(segs), len(events))
    out += struct.pack('<3f', *points[0]) + struct.pack('<3f', *lo) + struct.pack('<3f', *hi)
    out += b''.join(struct.pack('<4f', *s) for s in segs)
    out += b''.join(struct.pack('<IIff', *e) for e in events)
    return out


def aip_record(points, regions, ai=(), events=(), remaining=None):
    """One track path through `points` (cm) plus region rows [(slot, kind, (x, y, z), (dx, dy, dz))].

    `ai`: AI paths [(points, events)]; `events`: the track path's [(type, value, start, end)];
    `remaining`: the track path's remaining race distance at its origin (default: its length)."""
    if remaining is None:
        remaining = sum(s[3] for s in _segments(points))
    out = struct.pack('<I', 0x41495031) + struct.pack('<I', len(ai))
    for pts, evs in ai:
        out += struct.pack('<7I', 0, 0, 0, 1, 0, 0, 7) + _path_body(pts, evs)
    out += struct.pack('<I', 1) + struct.pack('<IIIf', 0, 0, 0, remaining) + _path_body(points, events)
    out += struct.pack('<I', 0)
    out += struct.pack('<I', len(regions))
    for slot, kind, p, d in regions:
        out += struct.pack('<II', slot, kind) + struct.pack('<6f', *p, *d) + struct.pack('<II', 0, 0)
    return out


def rail_record(points, track, rid, bulge=150.0):
    """A rail through `points` (cm): one cubic segment per pair, bowed sideways by `bulge`."""
    import math
    n = len(points) - 1
    out = bytearray(48 + 144 * n)
    struct.pack_into('<I', out, 0, (rid << 8) | track)
    struct.pack_into('<IIIiI', out, 0x1C, 0, n, 0x412D0000, -1, 0)
    dist = 0.0
    allpts = []
    for k, (a, b) in enumerate(zip(points, points[1:])):
        base = 48 + 144 * k
        dx, dy = b[0] - a[0], b[1] - a[1]
        h = math.hypot(dx, dy)
        side = (dy / h * bulge, -dx / h * bulge, 0.0)
        # p(t) = a + (b - a) t + 4 side t (1 - t)
        rows = [(0.0, 0.0, 0.0), tuple(-4 * v for v in side), tuple(b[i] - a[i] + 4 * side[i] for i in range(3)), a]
        for r, row in enumerate(rows):
            struct.pack_into('<4f', out, base + 0x10 + 16 * r, *row, 1.0 if r == 3 else 0.0)
        pts = [tuple(rows[1][i] * t * t + rows[2][i] * t + rows[3][i] for i in range(3))
               for t in (j / 64 for j in range(65))]
        length = sum(math.dist(p, q) for p, q in zip(pts, pts[1:]))
        allpts += pts
        struct.pack_into('<f', out, base + 0x0C, length)
        struct.pack_into('<3i', out, base + 0x60, k - 1 if k else -1, k + 1 if k < n - 1 else -1, rid)
        struct.pack_into('<3f', out, base + 0x6C, *(min(p[i] for p in pts) - 30 for i in range(3)))
        struct.pack_into('<3f', out, base + 0x78, *(max(p[i] for p in pts) + 30 for i in range(3)))
        struct.pack_into('<f', out, base + 0x84, dist)
        struct.pack_into('<I', out, base + 0x8C, 15)
        dist += length
    struct.pack_into('<3f', out, 4, *(min(p[i] for p in allpts) - 50 for i in range(3)))
    struct.pack_into('<3f', out, 16, *(max(p[i] for p in allpts) + 50 for i in range(3)))
    return record(8, track, rid, bytes(out))


def particle_record(x, y, z, track, rid):
    m = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1)
    data = (bytes(16) + struct.pack('<16f', *m) + struct.pack('<4f', x, y, z + 100, 150)
            + struct.pack('<II', (rid << 8) | track, (rid << 8) | track)
            + struct.pack('<3f', x - 100, y - 100, z) + struct.pack('<3f', x + 100, y + 100, z + 200) + bytes(16))
    return record(5, track, rid, data)


def light_record(x, y, z, track, rid):
    """A spot light (kind 6, 112 bytes) pointing along +y and down."""
    data = bytearray(112)
    struct.pack_into('<I3f', data, 16, 1, 1.0, 1.0, 800.0)
    struct.pack_into('<3f', data, 32, 1.0, 0.9, 0.7)
    struct.pack_into('<3f', data, 44, 0.0, 0.8, -0.6)
    struct.pack_into('<3f', data, 56, x, y, z)
    struct.pack_into('<3f3f', data, 68, x - 800, y - 800, z - 800, x + 800, y + 800, z + 800)
    struct.pack_into('<2f', data, 92, 0.9, 0.7)
    return record(6, track, rid, bytes(data))


def glow_record(x, y, z, track, rid):
    data = bytearray(80)
    struct.pack_into('<I3f3f', data, 12, 1, 1.0, 1.0, 0.8, x, y, z)
    struct.pack_into('<3f3f', data, 40, x - 50, y - 50, z - 50, x + 50, y + 50, z + 50)
    return record(7, track, rid, bytes(data))


def curtain_record(x0, x1, y, z, height_, track, rid):
    """A vertical occluder quad across x0..x1 at y (kind 11, 208 bytes)."""
    corners = [(x0, y, z), (x1, y, z), (x1, y, z + height_), (x0, y, z + height_)]
    data = bytearray(208)
    cx, cz = (x0 + x1) / 2, z + height_ / 2
    import math
    struct.pack_into('<4f', data, 0, cx, y, cz, math.dist((cx, y, cz), corners[0]))
    for k, c in enumerate(corners):
        struct.pack_into('<4f', data, 0x10 + 16 * k, *c, 1.0)
    struct.pack_into('<4f', data, 0x50, 0.0, 1.0, 0.0, -y)
    struct.pack_into('<3f3f', data, 0xA0, x0, y, z, x1, y, z + height_)
    struct.pack_into('<I', data, 0xB8, 1)
    return record(11, track, rid, bytes(data))


def camera_record(volume_pos, look_point, bound_pos, track):
    """Camera triggers (kind 17): one box volume whose enter action is a bounded camera on a point."""
    out = struct.pack('<IfII', 7, 0.0, 1, 2)
    out += struct.pack('<III', 1, 2, 1) + struct.pack('<3f3f3f', *volume_pos, 500, 500, 300, 0.25, 0, 0)
    out += struct.pack('<I6fI3f', 1, 0.5, 400.0, 1.0, 100.0, 10.0, 0.0, 1, *look_point)
    out += struct.pack('<I3f', 3, *bound_pos)
    out += struct.pack('<I', 3)
    out += b'\xef\xbe\xad\xde' * (-len(out) // 4 % 4)
    return record(17, track, 0, out)


def spine_record(points, markers):
    """Progress meter (kind 21): a gate at each of `points` (cm) across the course, `markers` [(type, distance)]."""
    import math
    gates = []
    dist = 0.0
    for k, p in enumerate(points):
        if k:
            dist += math.hypot(p[0] - points[k - 1][0], p[1] - points[k - 1][1])
        q = points[min(k + 1, len(points) - 1)] if k < len(points) - 1 else p
        o = points[k - 1] if k == len(points) - 1 else p
        dx, dy = q[0] - o[0], q[1] - o[1]
        h = math.hypot(dx, dy) or 1.0
        gates.append((dy / h, -dx / h, p[0], p[1], dist))
    out = struct.pack('<4If', len(gates), 0x14, len(markers), 0x14 + 20 * len(gates), dist)
    out += b''.join(struct.pack('<5f', *g) for g in gates)
    out += b''.join(struct.pack('<If', *m) for m in markers)
    return record(21, 0, 0, out)


def build_course_world():
    """AAA: 6 x 10 patches (30 m x 50 m) with a path down the middle; A_AAA continues below it."""
    xs = 15 * PATCH_M / 5
    line = [(xs, y, height(xs, y)) for y in (100.0, 1500.0, 3000.0, 4900.0)]
    regions = [(0, 0, line[0], (0.0, 1.0, 0.0)), (1, 1, (xs, 2500.0, height(xs, 2500.0)), (0.0, 1.0, 0.0))]
    ai_line = [(xs - 300.0, y, height(xs - 300.0, y) + 20) for y in (100.0, 1000.0, 2000.0, 3000.0, 4000.0, 4900.0)]
    events = [(18, 0, 2400.0, 2450.0), (0, 0, 4500.0, 4550.0)]
    rail = [(2200.0, y, height(2200.0, y) + 60) for y in (1000.0, 2000.0, 3000.0, 4000.0)]
    spine = [(xs, y, 0.0) for y in (100.0, 800.0, 1500.0, 2200.0, 3000.0, 3800.0, 4900.0)]
    main = (record(0, 0, 0, struct.pack('<h', 7) + bytes(18)) + record(2, 0, 5, mdr_model())
            + terrain_grid(0.0, 0.0, 6, 10, 0, corner_order=((0, 0), (0, 1), (1, 0), (1, 1)))
            + placed_instance(1500.0, 2500.0, 0, 1) + placed_instance(200.0, 300.0, 0, 2)
            + particle_record(1300.0, 2600.0, height(1300.0, 2600.0), 0, 3)
            + light_record(1700.0, 2400.0, height(1700.0, 2400.0) + 600, 0, 0)
            + glow_record(1600.0, 2300.0, height(1600.0, 2300.0) + 400, 0, 0)
            + rail_record(rail, 0, 0)
            + curtain_record(600.0, 2400.0, 2700.0, height(1500.0, 2700.0) - 200, 1500.0, 0, 0)
            + camera_record((1500.0, 2500.0, height(1500.0, 2500.0)), (1500.0, 3000.0, 0.0),
                            (1800.0, 2500.0, height(1800.0, 2500.0) + 300), 0)
            + record(13, 0, 0, bytes(32))
            + spine_record(spine, [(0, 0.0), (1, 2400.0), (2, 4400.0)])
            + record(14, 0, 0, aip_record(line, regions, ai=[(ai_line, [(100, 1, 1000.0, 1200.0)])], events=events))
            + record(14, 0, 1, b'')
            + record(15, 0, 0, painter_record([(0.5, 2.0, 3000.0, 10000.0, 0.7, 0.8, 1.0)])))
    connector = terrain_grid(0.0, 10 * PATCH_M, 6, 4, 1) + record(15, 1, 0, painter_record(
        [(0.5, 2.0, 3000.0, 8000.0, 0.6, 0.7, 0.9)]))
    chunks = [record(9, 255, 7, texture_8bit(32, 32, 1)) + record(10, 255, 0, light_page()), main, connector,
              record(15, 2, 0, painter_record([(1.0, 0.0, 1.0, 2.0, 0, 0, 0)]))]
    ssb = build_ssb_slots(chunks)
    sdb = build_sdb([('AAA', 1), ('A_AAA', 2), ('ASKY', 3)])
    big = build_big([('bam.sdb', sdb), ('bam.ssb', ssb), ('bam.phm', bytes(16)), ('bam.psm', bytes(16))])
    return big, chunks
