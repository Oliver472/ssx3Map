"""Growing the world data: more blocks, more records, a bigger BAM.BIG.

Every other edit in ssx3map keeps every byte where it was. Adding geometry
needs the world to grow, and that touches three layers:

  bam.ssb   a grown chunk takes more 32 KB blocks; the chunks after it shift.
  bam.sdb   the sub-chunk info of every chunk holds its byte offset in bam.ssb
            (+4), so shifted chunks get new offsets; the grown chunk's record
            counts (+0, +12..) and size (+8), and its location's per-kind
            counts, follow its new records.
  the disc  BAM.BIG no longer fits its sectors and moves to the end of the image.

How much of this the game accepts is not known yet. `probe` writes one test
image per step (docs/growing.md), so each step can be tried in PCSX2 on its own,
and a report of what the real data says about the fields it rewrites.
"""
from __future__ import annotations

import collections
import os
import shutil
import struct
from dataclasses import dataclass, field

from . import aip as aipmod
from . import bigf, iso9660, mapedit, rebuild, refpack, sdb, ssb, terrain
from .world import BIG_PATH

INFO_SIZE = 96          # chunk info: box min/max (2 x vec4), 48 zero bytes, 4 ints
SUB_SIZE = 68           # sub-chunk info: u16 records, u16 chunk, u32 offset, u32 size, u16 x 13 counts, ...
COUNTED_KINDS = 13      # the sub-chunk info counts record kinds 0..12
SHORTS = 32             # the 28 shorts of a location record start here
DVD5 = 2295104 * iso9660.SECTOR
ALIGN = {1: 16, 2: 16, 3: 16, 11: 16, 12: 16, 9: 128, 10: 128}
SIZE_RULE = 'kinds 0-12, 8-byte headers'     # holds for all 159 retail chunks (docs/findings.md)


class GrowError(ValueError):
    pass


# --------------------------------------------------------------------------
# bam.sdb
# --------------------------------------------------------------------------

class Tables:
    """The parts of bam.sdb that growing rewrites (the raw bytes, edited in place)."""

    def __init__(self, raw):
        self.raw = bytearray(raw)
        self.dir = sdb.parse(self.raw)
        d = self.dir
        self.info_start = (sdb.HEADER_SIZE + sdb.LOCATION_SIZE * d.location_count + 15) & ~15
        self.sub_start = self.info_start + INFO_SIZE * d.chunk_info_count
        self.expected_size = self.sub_start + SUB_SIZE * d.sub_chunk_count
        self._sub = {}
        for i in range(d.sub_chunk_count):
            p = self.sub_start + SUB_SIZE * i
            if p + SUB_SIZE > len(self.raw):
                break
            chunk, = struct.unpack_from('<H', self.raw, p + 2)
            self._sub.setdefault(chunk, p)

    def has(self, chunk):
        return chunk in self._sub

    def sub_pos(self, chunk):
        try:
            return self._sub[chunk]
        except KeyError:
            raise GrowError(f'bam.sdb has no sub-chunk info for chunk {chunk}') from None

    def offset(self, chunk):
        return struct.unpack_from('<I', self.raw, self.sub_pos(chunk) + 4)[0]

    def set_offset(self, chunk, value):
        struct.pack_into('<I', self.raw, self.sub_pos(chunk) + 4, value)

    def size(self, chunk):
        return struct.unpack_from('<I', self.raw, self.sub_pos(chunk) + 8)[0]

    def set_size(self, chunk, value):
        struct.pack_into('<I', self.raw, self.sub_pos(chunk) + 8, value)

    def record_count(self, chunk):
        return struct.unpack_from('<H', self.raw, self.sub_pos(chunk))[0]

    def set_record_count(self, chunk, value):
        struct.pack_into('<H', self.raw, self.sub_pos(chunk), value)

    def kind_counts(self, chunk):
        return list(struct.unpack_from(f'<{COUNTED_KINDS}H', self.raw, self.sub_pos(chunk) + 12))

    def set_kind_counts(self, chunk, counts):
        struct.pack_into(f'<{COUNTED_KINDS}H', self.raw, self.sub_pos(chunk) + 12, *counts)

    def _shorts_pos(self, loc):
        return sdb.HEADER_SIZE + sdb.LOCATION_SIZE * loc.index + SHORTS

    def shorts(self, loc):
        return list(struct.unpack_from('<28h', self.raw, self._shorts_pos(loc)))

    def set_short(self, loc, k, value):
        struct.pack_into('<h', self.raw, self._shorts_pos(loc) + 2 * k, value)

    def chunk_infos(self):
        """[(box floats (8), ints (4))] of the chunk-info table."""
        out = []
        for i in range(self.dir.chunk_info_count):
            p = self.info_start + INFO_SIZE * i
            if p + INFO_SIZE > len(self.raw):
                break
            out.append((struct.unpack_from('<8f', self.raw, p), struct.unpack_from('<4i', self.raw, p + 80)))
        return out


def _size_rules():
    """Candidate formulas for the sub-chunk size field (+8): name -> fn(records)."""
    rules = {}
    sets = (('all kinds', None), ('kinds 0-12', set(range(13))),
            ('all but 14', set(range(256)) - {14}), ('all but 14, 20', set(range(256)) - {14, 20}),
            ('all but 9, 10', set(range(256)) - {9, 10}))
    for header in (8, 0):
        for aligned in (False, True):
            for label, kinds in sets:
                def fn(records, header=header, aligned=aligned, kinds=kinds):
                    total = 0
                    for r in records:
                        if kinds is not None and r.kind not in kinds:
                            continue
                        n = r.size + header
                        if aligned:
                            a = ALIGN.get(r.kind, 4)
                            n = (n + a - 1) // a * a
                        total += n
                    return total
                rules[f'{label}, {header}-byte headers{", aligned" if aligned else ""}'] = fn
    return rules


SIZE_RULES = _size_rules()


def _own_counts(stream, loc, kind):
    """(records of `kind` on the location's own track in its chunks, records of `kind` in its main chunk)."""
    own = main = 0
    for c in loc.chunks:
        if c >= len(stream):
            continue
        for r in stream.records(c, keep=c == loc.chunk_end):
            if r.kind == kind:
                own += r.track == loc.index
                main += c == loc.chunk_end
    return own, main


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------

def _block(tag, stream, extent):
    cap = extent - 8
    if len(stream) > cap:
        raise GrowError(f'a block stream of {len(stream)} bytes does not fit {cap}')
    return tag + struct.pack('<I', extent) + stream + bytes(cap - len(stream))


def pack_fresh(data, max_decoded, cap, workers=None, fill=0.92):
    """Cut `data` into [(piece, RefPack stream)], each stream at most `cap` bytes and each piece at
    most `max_decoded` bytes. A first pass measures how well each stretch compresses; the cut then
    aims at `fill` x cap per block, and a piece that still does not fit is cut in two."""
    data = bytes(data)
    if not data:
        return [(b'', refpack.compress(b''))]
    probes = [data[i:i + max_decoded] for i in range(0, len(data), max_decoded)]
    ratios = [len(z) / len(p) for p, z in zip(probes, ssb.parallel_map(refpack.compress, probes, workers))]
    pieces, pos = [], 0
    while pos < len(data):
        ratio = ratios[min(pos // max_decoded, len(ratios) - 1)]
        take = max(1, min(max_decoded, int(fill * cap / ratio)))
        pieces.append(data[pos:pos + take])
        pos += take
    streams = [None] * len(pieces)
    while True:
        todo = [i for i, z in enumerate(streams) if z is None]
        if not todo:
            return list(zip(pieces, streams))
        for i, z in zip(todo, ssb.parallel_map(refpack.compress, [pieces[i] for i in todo], workers)):
            streams[i] = z
        new_pieces, new_streams = [], []
        for p, z in zip(pieces, streams):
            if len(z) <= cap:
                new_pieces.append(p)
                new_streams.append(z)
            else:
                half = len(p) // 2
                new_pieces += [p[:half], p[half:]]
                new_streams += [None, None]
        pieces, streams = new_pieces, new_streams


def _first_difference(a, b):
    n = min(len(a), len(b))
    step = 1 << 14
    pos = 0
    while pos < n and a[pos:pos + step] == b[pos:pos + step]:
        pos += step
    while pos < n and a[pos] == b[pos]:
        pos += 1
    return min(pos, n)


class Growth:
    """A world being grown: chunks with new block lists, then new bam.ssb, bam.sdb and BAM.BIG."""

    def __init__(self, world):
        self.world = world
        self.stream = world.stream
        self.tables = Tables(world._member('.sdb'))
        self.blocks = {}        # chunk -> [block bytes]
        self.data = {}          # chunk -> its decoded contents after the change
        self.notes = []
        self.max_decoded = max(b.decoded_size for b in self.stream.blocks)

    def _raw(self, block):
        return self.stream.original[block.offset:block.offset + block.extent]

    def check_offsets(self):
        bad = [c for c, ch in enumerate(self.stream.chunks)
               if not self.tables.has(c) or self.tables.offset(c) != ch.blocks[0].offset]
        if bad:
            raise GrowError(f'bam.sdb does not hold the bam.ssb offset of {len(bad)} chunks '
                            f'(first: {bad[:5]}); growing would break them')

    def split_last_block(self, chunk):
        """Same contents, one block more: the chunk's last block is cut in two."""
        ch = self.stream.chunks[chunk]
        last = ch.blocks[-1]
        piece = self.stream.decode_block(last)
        if len(piece) < 2:
            raise GrowError(f'chunk {chunk}: its last block is too small to split')
        half = len(piece) // 2
        a, b = refpack.compress(piece[:half]), refpack.compress(piece[half:])
        self.blocks[chunk] = ([self._raw(x) for x in ch.blocks[:-1]]
                              + [_block(ssb.TAG_BLOCK, a, last.extent), _block(ssb.TAG_END, b, last.extent)])
        self.data[chunk] = self.stream.chunk_original(chunk)
        self.notes.append(f'chunk {chunk}: last block split in two ({len(ch.blocks)} -> {len(ch.blocks) + 1} blocks)')

    def set_chunk(self, chunk, data, workers=None):
        """New contents for a chunk: blocks before the first change stay, the rest is packed again."""
        ch = self.stream.chunks[chunk]
        old = self.stream.chunk_original(chunk)
        first = _first_difference(old, data)
        pos, keep = 0, 0
        for k, b in enumerate(ch.blocks[:-1]):
            if pos + b.decoded_size > first:
                break
            pos += b.decoded_size
            keep = k + 1
        extent = ch.blocks[-1].extent
        fresh = pack_fresh(bytes(data[pos:]), self.max_decoded, extent - 8, workers)
        tail = [_block(ssb.TAG_END if i == len(fresh) - 1 else ssb.TAG_BLOCK, s, extent)
                for i, (_, s) in enumerate(fresh)]
        self.blocks[chunk] = [self._raw(b) for b in ch.blocks[:keep]] + tail
        self.data[chunk] = bytes(data)
        self.notes.append(f'chunk {chunk}: {len(old)} -> {len(data)} bytes, {len(ch.blocks)} -> '
                          f'{len(self.blocks[chunk])} blocks ({keep} kept as they were)')

    def take_edits(self, workers=None, size_rule=SIZE_RULE):
        """Pack every edited chunk of the world again (it may have grown); counts follow."""
        for c in self.stream.changed_chunks():
            if c in self.blocks:
                continue
            old = ssb.parse_records(self.stream.chunk_original(c))
            data = bytes(self.stream.current(c))
            new = ssb.parse_records(data)
            self.set_chunk(c, data, workers)
            if [(r.kind, r.size) for r in old] != [(r.kind, r.size) for r in new]:
                self._recount(c, old, new, size_rule)

    def add_records(self, chunk, records, size_rule=SIZE_RULE, workers=None):
        """Insert raw records (headers included) after the last record of their kind; counts follow."""
        old = bytes(self.stream.current(chunk))
        old_recs = ssb.parse_records(old)
        groups = {}
        for raw in records:
            groups.setdefault(raw[0], []).append(raw)
        points = []
        for kind, raws in groups.items():
            same = [r for r in old_recs if r.kind == kind]
            points.append((same[-1].offset + same[-1].size if same else len(old), b''.join(raws)))
        data = bytearray(old)
        for at, blob in sorted(points, key=lambda p: -p[0]):
            data[at:at] = blob
        data = bytes(data)
        new_recs = ssb.parse_records(data)
        self.set_chunk(chunk, data, workers)
        self._recount(chunk, old_recs, new_recs, size_rule)

    def _recount(self, chunk, old_recs, new_recs, size_rule):
        t = self.tables
        if len(new_recs) > 0xFFFF:
            raise GrowError(f'chunk {chunk} would hold {len(new_recs)} records; bam.sdb counts them in 16 bits')
        if t.record_count(chunk) == len(old_recs):
            t.set_record_count(chunk, len(new_recs))
            self.notes.append(f'sub-chunk {chunk}: record count {len(old_recs)} -> {len(new_recs)}')
        else:
            self.notes.append(f'sub-chunk {chunk}: record count field {t.record_count(chunk)} is not the '
                              f'record count {len(old_recs)}; left alone')
        before = collections.Counter(r.kind for r in old_recs)
        after = collections.Counter(r.kind for r in new_recs)
        counts = t.kind_counts(chunk)
        for k in range(COUNTED_KINDS):
            if before[k] == after[k]:
                continue
            if counts[k] == before[k]:
                counts[k] = after[k]
                self.notes.append(f'sub-chunk {chunk}: kind {k} count {before[k]} -> {after[k]}')
            else:
                self.notes.append(f'sub-chunk {chunk}: kind {k} count field {counts[k]} is not {before[k]}; '
                                  'left alone')
        t.set_kind_counts(chunk, counts)
        field_ = t.size(chunk)
        rule = SIZE_RULES.get(size_rule)
        if rule is not None and rule(old_recs) == field_:
            value = rule(new_recs)
            how = size_rule
        else:
            value = field_ + sum(r.size + 8 for r in new_recs) - sum(r.size + 8 for r in old_recs)
            how = 'no known rule fits; grown by the added bytes'
        t.set_size(chunk, value)
        self.notes.append(f'sub-chunk {chunk}: size {field_} -> {value} ({how})')
        loc = t.dir.location_of_chunk(chunk)
        if loc is None:
            return
        shorts = t.shorts(loc)
        for k in sorted(set(after) | set(before)):
            if k >= 23 or before[k] == after[k]:
                continue
            own, main = _own_counts(self.stream, loc, k)
            added = after[k] - before[k]
            if shorts[k] in (own, main):
                if shorts[k] + added > 0x7FFF:
                    raise GrowError(f'{loc.name}: {shorts[k] + added} records of kind {k} do not fit its 16-bit count')
                t.set_short(loc, k, shorts[k] + added)
                self.notes.append(f'{loc.name}: kind {k} count {shorts[k]} -> {shorts[k] + added}')
            else:
                self.notes.append(f'{loc.name}: kind {k} count field {shorts[k]} matches neither {own} nor {main}; '
                                  'left alone')

    def ssb_image(self):
        out, offsets = bytearray(), []
        for c, ch in enumerate(self.stream.chunks):
            offsets.append(len(out))
            if c in self.blocks:
                out += b''.join(self.blocks[c])
            else:
                out += self.stream.original[ch.blocks[0].offset:ch.blocks[-1].offset + ch.blocks[-1].extent]
        return bytes(out), offsets

    def build(self):
        """The new BAM.BIG (bytes), checked: every chunk decodes to what it should."""
        if not self.blocks:
            return self.world.big.to_bytes()
        self.check_offsets()
        image, offsets = self.ssb_image()
        blocks, chunks = ssb.scan(image)
        if len(chunks) != len(self.stream.chunks):
            raise GrowError('the new bam.ssb does not have the same chunks')
        for c, ch in enumerate(chunks):
            if ch.blocks[0].offset != offsets[c]:
                raise GrowError(f'chunk {c} is not where it should be')
            if c in self.blocks:
                got = b''.join(refpack.decompress(image[b.payload_offset:b.offset + b.extent]) for b in ch.blocks)
                if got != self.data[c]:
                    raise GrowError(f'chunk {c} does not decode to its new contents')
        for c, off in enumerate(offsets):
            self.tables.set_offset(c, off)
        shifted = sum(1 for c, ch in enumerate(self.stream.chunks) if ch.blocks[0].offset != offsets[c])
        self.notes.append(f'bam.ssb {len(self.stream.original)} -> {len(image)} bytes; {shifted} chunks shifted'
                          + (' (their offsets in bam.sdb rewritten)' if shifted else ''))
        big = bigf.BigArchive.parse(self.world.big.to_bytes())
        entry = big.find_suffix('.sdb')
        stored = big.read(entry)
        new_sdb = bytes(self.tables.raw)
        if len(stored) > 5 and stored[1] == 0xFB and stored[0] in (0x10, 0x11, 0x90, 0x91):
            new_sdb = refpack.compress(new_sdb)
        big.replace(entry, new_sdb)
        big.replace(big.find_suffix('.ssb'), image)
        return big.to_bytes()


# --------------------------------------------------------------------------
# new terrain for the tests
# --------------------------------------------------------------------------

def record_bytes(kind, track, rid, payload):
    return bytes([kind]) + len(payload).to_bytes(3, 'little') + bytes([track]) + rid.to_bytes(3, 'little') + payload


def ramp_patch(data, heading, lift):
    """A copy of a terrain record that rises from the ground at its upstream edge to `lift` cm
    above it at its downstream edge (along `heading`, a unit XY vector): a jump made of one patch."""
    p = terrain.Patch(bytes(data))
    hx, hy = heading
    along = [q[0] * hx + q[1] * hy for q in (p.point(u, v) for u in terrain.GRID for v in terrain.GRID)]
    lo, span = min(along), (max(along) - min(along)) or 1.0

    def sample(u, v):
        q = p.point(u, v)
        return q[0], q[1], q[2] + lift * ((q[0] * hx + q[1] * hy) - lo) / span
    buf = bytearray(data)
    rebuild.write_patch(buf, 0, rebuild._fit_patch(sample), rebuild._corner_order(p))
    return bytes(buf)


def tiny_patch(data, x, y, z):
    """A copy of a terrain record shrunk to a 10 cm square at (x, y, z), not collidable."""
    buf = bytearray(data)
    old = terrain.Patch(bytes(data))
    rebuild.write_patch(buf, 0, rebuild._fit_patch(lambda u, v: (x + 10 * u, y + 10 * v, z)),
                        rebuild._corner_order(old))
    flags, = struct.unpack_from('<h', buf, rebuild.FLAGS)
    struct.pack_into('<h', buf, rebuild.FLAGS, flags & ~1)
    return bytes(buf)


def _set_resource(data, rid, track):
    buf = bytearray(data)
    struct.pack_into('<I', buf, terrain.RESOURCE, (rid << 8) | track)
    return bytes(buf)


@dataclass
class Terrain:
    """The terrain records of a course's main chunk."""
    chunk: int
    track: int
    records: list            # [(Record, Patch)]
    lo: tuple
    hi: tuple
    next_rid: int
    resource_word: bool       # +0x150 holds (rid << 8 | track) for every patch


def course_terrain(world, code):
    loc = world.location(code)
    c = mapedit.main_chunk(loc)
    data = world.stream.current(c)
    recs = [(r, terrain.Patch(data[r.offset:r.offset + r.size])) for r in world.stream.records(c)
            if r.kind == 1 and r.size == terrain.PATCH_SIZE]
    if not recs:
        raise GrowError(f'{code}: no terrain patches in chunk {c}')
    lo = tuple(min(p.bbox[0][k] for _, p in recs) for k in range(3))
    hi = tuple(max(p.bbox[1][k] for _, p in recs) for k in range(3))
    track = collections.Counter(r.track for r, _ in recs).most_common(1)[0][0]
    rids = [r.rid for r, _ in recs if r.track == track]
    word = all(struct.unpack_from('<I', p.data, terrain.RESOURCE)[0] == (r.rid << 8) | r.track for r, p in recs)
    return Terrain(c, track, recs, lo, hi, max(rids) + 1, word)


def ramp(world, code, ground, lift=300.0, metres=range(40, 401, 10)):
    """(metres after the start, record bytes): a copy of the patch under the race line turned into a
    ramp `lift` cm high, at the first place where it stays inside the course's terrain box."""
    line = aipmod.course_line(mapedit.course_aip(world, code))
    if line is None:
        raise GrowError(f'{code}: no race line to put the ramp on')
    for m in metres:
        (x, y, _), heading = line.at(m * 100.0)
        best = None
        for r, p in ground.records:
            lo, hi = p.bbox
            if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
                continue
            z = terrain.surface_z([p], x, y)
            if z is not None and (best is None or z > best[0]):
                best = (z, r, p)
        if best is None:
            continue
        _, r, p = best
        lo, hi = p.bbox
        if hi[2] + lift <= ground.hi[2]:
            return m, ramp_patch(p.data, heading, lift)
    raise GrowError(f'{code}: found no place for the ramp inside the terrain box')


def hidden_patches(ground, count, depth=2000.0, room=300.0):
    """`count` tiny patches under course patches (spread like the course itself), up to `depth` cm
    down but at least `room` cm under the patch's lowest point and inside the terrain box."""
    floor = ground.lo[2] + 50.0
    pool = [p for _, p in ground.records if p.bbox[0][2] - room >= floor] or [p for _, p in ground.records]
    out = []
    n = len(pool)
    for i in range(count):
        p = pool[i * n // count] if count <= n else pool[i % n]
        lo, hi = p.bbox
        out.append(tiny_patch(p.data, (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, max(lo[2] - depth, floor)))
    return out


def new_terrain_records(ground, patches):
    out = []
    for k, data in enumerate(patches):
        rid = ground.next_rid + k
        if ground.resource_word:
            data = _set_resource(data, rid, ground.track)
        out.append(record_bytes(1, ground.track, rid, data))
    return out


# --------------------------------------------------------------------------
# the survey: what the real data says about the fields growing rewrites
# --------------------------------------------------------------------------

@dataclass
class Survey:
    lines: list = field(default_factory=list)
    size_rule: str = None
    ok: bool = True

    def add(self, text):
        self.lines.append(text)


def survey(world, code, progress=None):
    s = Survey()
    stream = world.stream
    t = Tables(world._member('.sdb'))
    d = t.dir
    s.add(f'bam.sdb: {len(t.raw)} bytes, {d.location_count} locations, {d.chunk_info_count} chunk infos, '
          f'{d.sub_chunk_count} sub-chunk infos; layout size {t.expected_size} '
          f'({"matches" if t.expected_size == len(t.raw) else "DIFFERS"})')
    s.add(f'bam.ssb: {len(stream.original)} bytes, {len(stream.blocks)} blocks, {len(stream.chunks)} chunks; '
          f'block extents {sorted(collections.Counter(b.extent for b in stream.blocks).items())}; '
          f'largest decoded block {max(b.decoded_size for b in stream.blocks)}')
    offsets_ok = sum(1 for c, ch in enumerate(stream.chunks) if t.has(c) and t.offset(c) == ch.blocks[0].offset)
    s.add(f'sub-chunk offset (+4) = chunk offset in bam.ssb: {offsets_ok}/{len(stream.chunks)}')
    if offsets_ok != len(stream.chunks):
        s.ok = False
    fields = {t.sub_pos(c) + 4 for c in range(len(stream.chunks)) if t.has(c)}
    starts = {ch.blocks[0].offset for ch in stream.chunks if ch.blocks[0].offset > 64}
    stray = [p for p in range(0, len(t.raw) - 3, 4)
             if p not in fields and struct.unpack_from('<I', t.raw, p)[0] in starts]
    s.add(f'other words in bam.sdb equal to a chunk offset: {len(stray)} {[hex(p) for p in stray[:8]]}')
    # Record counts and the size field over every chunk (decodes the whole world once).
    counts_ok = kinds_ok = 0
    rule_hits = collections.Counter()
    target = mapedit.main_chunk(world.location(code))
    target_rules = []
    by_kind, by_kind_track = {}, {}
    for c in range(len(stream.chunks)):
        if progress and c % 20 == 0:
            progress(f'reading chunk {c} of {len(stream.chunks)}')
        recs = stream.records(c, keep=c == target)
        kinds = collections.Counter(r.kind for r in recs)
        by_kind[c] = kinds
        by_kind_track[c] = collections.Counter((r.kind, r.track) for r in recs)
        if not t.has(c):
            continue
        counts_ok += t.record_count(c) == len(recs)
        kinds_ok += t.kind_counts(c) == [kinds.get(k, 0) for k in range(COUNTED_KINDS)]
        size = t.size(c)
        for name, fn in SIZE_RULES.items():
            if fn(recs) == size:
                rule_hits[name] += 1
                if c == target:
                    target_rules.append(name)
    n = len(stream.chunks)
    s.add(f'sub-chunk record count (+0) right: {counts_ok}/{n}; per-kind counts 0..12 right: {kinds_ok}/{n}')
    s.add('sub-chunk size (+8) rules, chunks matching: '
          + '; '.join(f'{k}: {v}' for k, v in rule_hits.most_common(5)))
    if target_rules:
        s.size_rule = max(target_rules, key=lambda r: rule_hits[r])
    s.add(f'size rule used for chunk {target}: {s.size_rule or "none fits; the field grows by the added bytes"}')
    # Location shorts.
    own_ok = main_ok = 0
    for loc in d.locations:
        sh = t.shorts(loc)
        own = collections.Counter()
        for c in loc.chunks:
            for (kind, track), m in by_kind_track.get(c, {}).items():
                if track == loc.index:
                    own[kind] += m
        main = by_kind.get(loc.chunk_end, collections.Counter())
        own_ok += sh[:23] == [own.get(k, 0) for k in range(23)]
        main_ok += sh[:23] == [main.get(k, 0) for k in range(23)]
    s.add(f'location shorts 0..22 = per-kind counts on the own track: {own_ok}/{d.location_count}, '
          f'of the main chunk: {main_ok}/{d.location_count}')
    # The course itself.
    loc = world.location(code)
    recs = stream.records(target)
    runs = []
    for r in recs:
        if runs and runs[-1][0] == r.kind:
            runs[-1][1] += 1
        else:
            runs.append([r.kind, 1])
    s.add(f'{code} main chunk {target}: {len(recs)} records, {stream.chunks[target].decoded_size} bytes, '
          f'{len(stream.chunks[target].blocks)} blocks; kind order '
          + ' '.join(f'{k}x{m}' for k, m in runs[:40]) + (' ...' if len(runs) > 40 else ''))
    ground = course_terrain(world, code)
    rids = sorted(r.rid for r, _ in ground.records if r.track == ground.track)
    s.add(f'{code} terrain: {len(ground.records)} patches on track {ground.track} (location index {loc.index}); '
          f'rids {rids[0]}..{rids[-1]}, {len(set(rids))} distinct, '
          f'{"contiguous" if rids == list(range(rids[0], rids[0] + len(rids))) else "with gaps"}; '
          f'+0x150 = rid << 8 | track: {"yes" if ground.resource_word else "NO"}')
    s.add(f'{code} location record: {loc.sub_count} sub-chunks, {loc.chunk_count} chunks, last chunk '
          f'{loc.chunk_end}, first sub-chunk {loc.sub_start}; shorts {t.shorts(loc)}')
    s.add(f'{code} sub-chunk {target}: records {t.record_count(target)}, size {t.size(target)}, '
          f'kinds 0..12 {t.kind_counts(target)}')
    infos = t.chunk_infos()
    mine = [k for k, (_, ints) in enumerate(infos) if ints[2] == target]
    for k in mine[:3]:
        box, ints = infos[k]
        s.add(f'chunk info {k} (chunk {target}): box {[round(v) for v in box]} ints {list(ints)}')
    s.add(f'{code} terrain box: {[round(v) for v in ground.lo]} .. {[round(v) for v in ground.hi]}')
    kinds = collections.Counter(tuple(ints[:2]) == (-1, -1) for _, ints in infos)
    s.add(f'chunk infos with ints (-1, -1, ...): {kinds[True]}, others: {kinds[False]}; '
          f'first others: {[list(ints) for _, ints in infos if tuple(ints[:2]) != (-1, -1)][:6]}')
    # The archive and the disc.
    big = world.big
    s.add('BAM.BIG members: ' + ', '.join(f'{e.name} @{e.offset} ({e.size})' for e in big.entries)
          + f'; alignment {big.alignment()}')
    if world.is_iso:
        files = iso9660.list_files(world.source)
        image = os.path.getsize(world.source)
        entry = world.iso_entry
        after = [f for f in files if f[1] > entry.lba]
        nxt = min(after, key=lambda f: f[1]) if after else None
        end = entry.lba + (entry.size + iso9660.SECTOR - 1) // iso9660.SECTOR
        s.add(f'disc: {image} bytes ({len(files)} files); BAM.BIG at sector {entry.lba}, {entry.size} bytes; '
              + (f'next file {nxt[0]} at sector {nxt[1]} ({nxt[1] - end} free sectors between)'
                 if nxt else 'nothing after it'))
        s.add(f'room left on a single-layer DVD: {(DVD5 - image) // (1024 * 1024)} MB')
    return s


# --------------------------------------------------------------------------
# the test images
# --------------------------------------------------------------------------

EXPERIMENTS = (
    (1, 'big_moved', 'BAM.BIG moved to the end of the disc, contents unchanged'),
    (2, 'one_more_block', 'the first chunk one 32 KB block longer (same contents); every other chunk shifted'),
    (3, 'new_ramp', 'one new terrain patch: a ramp up to 3 m high on the race line'),
    (4, 'patches_plus10', 'the ramp and +10 % terrain patches (tiny, hidden under the ground)'),
    (5, 'patches_plus50', 'the ramp and +50 % terrain patches (tiny, hidden under the ground)'),
    (6, 'patches_plus100', 'the ramp and +100 % terrain patches (tiny, hidden under the ground)'),
)
EXTRA = {4: 10, 5: 50, 6: 100}


def experiment(world, number, code, rule=None, workers=None):
    """(BAM.BIG bytes, notes) for one test."""
    g = Growth(world)
    chunk = mapedit.main_chunk(world.location(code))
    if number == 2:
        g.split_last_block(0)
    elif number >= 3:
        ground = course_terrain(world, code)
        metres, new = ramp(world, code, ground)
        patches = [new]
        if number in EXTRA:
            patches += hidden_patches(ground, round(len(ground.records) * EXTRA[number] / 100))
        g.add_records(chunk, new_terrain_records(ground, patches), size_rule=rule, workers=workers)
        g.notes.insert(0, f'ramp {metres} m after the start; {len(patches) - 1} hidden patches; '
                          f'new rids {ground.next_rid}..{ground.next_rid + len(patches) - 1}')
    return g.build(), g.notes


def write_image(world, big_bytes, output):
    if world.is_iso:
        lba = iso9660.relocate_file(world.source, BIG_PATH, big_bytes, output)
        return f'BAM.BIG ({len(big_bytes)} bytes) now at sector {lba}'
    with open(output, 'wb') as f:
        f.write(big_bytes)
    return f'BAM.BIG {len(big_bytes)} bytes'


def probe(world, out_dir, code='ARA1', only=None, log=print, workers=None):
    """Write the test images into `out_dir` and a report; returns the report lines."""
    os.makedirs(out_dir, exist_ok=True)
    chosen = [e for e in EXPERIMENTS if not only or e[0] in only]
    ext = '.iso' if world.is_iso else '.BIG'
    size = os.path.getsize(world.source)
    need = len(chosen) * (size + len(world.big.data) * 2)
    free = shutil.disk_usage(out_dir).free
    if need > free:
        raise GrowError(f'the {len(chosen)} test images need about {need // 2 ** 30 + 1} GB, '
                        f'{free // 2 ** 30} GB are free; pick fewer with --only (e.g. --only 1,2,3)')
    log('reading the world data (a few minutes)')
    sv = survey(world, code, progress=log)
    report = [f'ssx3map probe of {world.source}, course {code}', ''] + sv.lines + ['']
    for line in sv.lines:
        log('  ' + line)
    if not sv.ok:
        report.append('bam.sdb does not hold the chunk offsets as expected; only test 1 was written.')
        chosen = [e for e in chosen if e[0] == 1]
    for number, name, text in chosen:
        out = os.path.join(out_dir, f'probe{number}_{name}{ext}')
        log(f'test {number}: {text}')
        big, notes = experiment(world, number, code, rule=sv.size_rule, workers=workers)
        notes.append(write_image(world, big, out))
        report.append(f'{number}. {os.path.basename(out)}: {text}')
        report += [f'     {n}' for n in notes]
        for n in notes:
            log('    ' + n)
    with open(os.path.join(out_dir, 'probe_report.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(report) + '\n')
    return report
