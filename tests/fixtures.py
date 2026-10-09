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


def placed_instance(x, y, track, rid, size=200.0):
    z = height(x, y)
    m = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1)
    data = (bytes(16) + struct.pack('<16f', *m) + struct.pack('<4f', x, y, z + size / 2, size)
            + struct.pack('<3f', x - size / 2, y - size / 2, z) + struct.pack('<3f', x + size / 2, y + size / 2, z + size)
            + struct.pack('<I', (rid << 8) | track) + bytes(4) + struct.pack('<I', 5 << 8) + bytes(0x40))
    return record(3, track, rid, data)


def aip_record(points, regions):
    """One track path through `points` (cm) plus region rows [(slot, kind, (x, y, z), (dx, dy, dz))]."""
    import math
    segs = []
    for a, b in zip(points, points[1:]):
        d = [b[k] - a[k] for k in range(3)]
        n = math.sqrt(sum(v * v for v in d))
        segs.append((d[0] / n, d[1] / n, d[2] / n, n))
    lo = [min(p[k] for p in points) for k in range(3)]
    hi = [max(p[k] for p in points) for k in range(3)]
    out = struct.pack('<I', 0x41495031) + struct.pack('<I', 0)
    out += struct.pack('<I', 1) + struct.pack('<IIIf', 0, 0, 0, 0.0) + struct.pack('<II', len(segs), 0)
    out += struct.pack('<3f', *points[0]) + struct.pack('<3f', *lo) + struct.pack('<3f', *hi)
    out += b''.join(struct.pack('<4f', *s) for s in segs)
    out += struct.pack('<I', 0)
    out += struct.pack('<I', len(regions))
    for slot, kind, p, d in regions:
        out += struct.pack('<II', slot, kind) + struct.pack('<6f', *p, *d) + struct.pack('<II', 0, 0)
    return out


def build_course_world():
    """AAA: 6 x 10 patches (30 m x 50 m) with a path down the middle; A_AAA continues below it."""
    xs = 15 * PATCH_M / 5
    line = [(xs, y, height(xs, y)) for y in (100.0, 1500.0, 3000.0, 4900.0)]
    regions = [(0, 0, line[0], (0.0, 1.0, 0.0)), (1, 1, (xs, 2500.0, height(xs, 2500.0)), (0.0, 1.0, 0.0))]
    main = (terrain_grid(0.0, 0.0, 6, 10, 0, corner_order=((0, 0), (0, 1), (1, 0), (1, 1)))
            + placed_instance(1500.0, 2500.0, 0, 1) + placed_instance(200.0, 300.0, 0, 2)
            + record(14, 0, 0, aip_record(line, regions)) + record(14, 0, 1, b'')
            + record(15, 0, 0, painter_record([(0.5, 2.0, 3000.0, 10000.0, 0.7, 0.8, 1.0)])))
    connector = terrain_grid(0.0, 10 * PATCH_M, 6, 4, 1) + record(15, 1, 0, painter_record(
        [(0.5, 2.0, 3000.0, 8000.0, 0.6, 0.7, 0.9)]))
    chunks = [record(9, 255, 7, texture_8bit(32, 32, 1)), main, connector,
              record(15, 2, 0, painter_record([(1.0, 0.0, 1.0, 2.0, 0, 0, 0)]))]
    ssb = build_ssb_slots(chunks)
    sdb = build_sdb([('AAA', 1), ('A_AAA', 2), ('ASKY', 3)])
    big = build_big([('bam.sdb', sdb), ('bam.ssb', ssb), ('bam.phm', bytes(16)), ('bam.psm', bytes(16))])
    return big, chunks
