"""`ssx3map inspect`: a structural report of the real world data.

Several layout questions cannot be answered without the disc (and the editor
was written without it): whether blocks carry padding, how the original encoder
compares with ours, which SDB fields locate chunks, how the terrain patch fields
relate to each other. This report answers them from the user's own copy. Each
section is independent; a failing section prints its error and the report goes on.
"""
from __future__ import annotations

import collections
import hashlib
import math
import statistics
import struct
import time
import traceback

from . import mapedit, painter, refpack, ssb, terrain, texture
from .world import KIND_NAMES, TRACK_SHARED


def _hexdump(data, width=16):
    return [f'  {i:04X}: ' + ' '.join(f'{b:02X}' for b in data[i:i + width]) for i in range(0, len(data), width)]


def _dist(values):
    if not values:
        return 'none'
    values = list(values)
    common = collections.Counter(values).most_common(3)
    return (f'n={len(values)} min={min(values)} median={int(statistics.median(values))} max={max(values)} '
            f'common={common}')


class Report:
    def __init__(self, world, sample_blocks=24, out=print):
        self.w = world
        self.sample_blocks = sample_blocks
        self.out = out
        self.data = {}

    def section(self, title, fn):
        self.out('')
        self.out(f'== {title} ==')
        t = time.time()
        try:
            fn()
        except Exception:     # noqa: BLE001 - keep reporting
            self.out('  ERROR in this section:')
            for line in traceback.format_exc().splitlines():
                self.out('  ' + line)
        self.out(f'  ({time.time() - t:.1f}s)')

    def run(self):
        self.out('ssx3map inspect report')
        self.section('source', self.source)
        self.section('BIG archive', self.big)
        self.section('SDB directory', self.sdb)
        self.section('SSB blocks (full decode)', self.blocks)
        self.section('chunks and locations', self.chunks)
        self.section('SDB fields that look like SSB positions', self.sdb_offsets)
        self.section('records', self.records)
        self.section('terrain patches (kind 1)', self.patches)
        self.section('instances (kind 3)', self.instances)
        self.section('painters / fog (kind 15)', self.painters)
        self.section('textures (kind 9)', self.textures)
        self.section('re-encode test (our encoder vs the original)', self.encoder)
        self.section('dry run: fog and texture edits (nothing is written)', self.dry_run)
        self.section('geometry: SDB sizes and counts (for adding new geometry)', self.geometry)
        self.section('courses: race lines, start, terrain dry run', self.courses)
        return self.data

    # ------------------------------------------------------------------------
    def source(self):
        w = self.w
        self.out(f'  input: {w.source}')
        if w.is_iso:
            e = w.iso_entry
            self.out(f'  disc image; {e.path} at LBA {e.lba} (offset 0x{e.offset:X}), {e.size} bytes')
        self.out(f'  BAM.BIG sha1 {hashlib.sha1(w.big.data).hexdigest()}')

    def big(self):
        b = self.w.big
        self.out(f'  magic {b.magic!r} size field {b.size_field} actual {len(b.data)} '
                 f'header size {b.header_size} entries {len(b.entries)} alignment {b.alignment()}')
        for e in b.entries:
            head = bytes(b.data[e.offset:e.offset + 4])
            self.out(f'  {e.name:24s} offset 0x{e.offset:08X} size {e.size:10d} first bytes {head.hex()}')

    def sdb(self):
        d = self.w.sdb
        self.out(f'  size {d.size}, locations {d.location_count}, chunk infos {d.chunk_info_count}, '
                 f'sub-chunks {d.sub_chunk_count}, textures {d.texture_count}, light pages {d.light_page_count}')
        loc_end = 80 + 88 * d.location_count
        aligned = (loc_end + 15) & ~15
        expect = aligned + 96 * d.chunk_info_count + 68 * d.sub_chunk_count
        self.out(f'  expected size with 96-byte chunk infos and 68-byte sub-chunk infos: {expect} '
                 f'({"matches" if expect == d.size else "DIFFERS by %d" % (d.size - expect)})')
        self.out('  header:')
        for line in _hexdump(d.header):
            self.out(line)
        self.out('  idx name             subs chunks  end  subStart firstChunk  shorts')
        for loc in d.locations:
            self.out(f'  {loc.index:3d} {loc.name:16s} {loc.sub_count:4d} {loc.chunk_count:6d} {loc.chunk_end:4d} '
                     f'{loc.sub_start:9d} {loc.first_chunk:10d}  {list(loc.shorts)}')
        self.data['locations'] = [dict(name=l.name, sub_count=l.sub_count, chunk_count=l.chunk_count,
                                       chunk_end=l.chunk_end, sub_start=l.sub_start, shorts=list(l.shorts))
                                  for l in d.locations]

    def blocks(self):
        s = self.w.stream
        self.out(f'  bam.ssb {len(s.original)} bytes, {len(s.blocks)} blocks, {len(s.chunks)} chunks (CEND)')
        extents = [b.extent for b in s.blocks]
        decoded = [b.decoded_size for b in s.blocks]
        self.out(f'  block extents: {_dist(extents)}')
        self.out(f'  decoded sizes: {_dist(decoded)}')
        for align in (16, 2048, 0x8000):
            ok = sum(1 for b in s.blocks if b.offset % align == 0)
            self.out(f'  block offsets aligned to {align}: {ok}/{len(s.blocks)}')
        flags = collections.Counter()
        padding = []
        pad_bytes = collections.Counter()
        totals = collections.Counter()
        no_stop = 0
        max_dist = 0
        for b in s.blocks:
            payload = s.block_payload(b)
            st = refpack.stream_stats(payload)
            flags[st['flags']] += 1
            pad = len(payload) - st['consumed']
            padding.append(pad)
            if pad:
                pad_bytes.update(payload[st['consumed']:st['consumed'] + 64])
            for k in ('short', 'medium', 'long', 'literal_cmds', 'overlapping', 'promotable_short',
                      'promotable_medium'):
                totals[k] += st[k]
            no_stop += not st['stop']
            max_dist = max(max_dist, st['max_distance'])
        self.out(f'  RefPack header flags: {dict(flags)}')
        self.out(f'  bytes after the stop command (padding) per block: {_dist(padding)}')
        self.out(f'  padding byte values: {pad_bytes.most_common(5)}')
        self.out(f'  streams without a stop command: {no_stop}')
        self.out(f'  commands: {dict(totals)}; max match distance {max_dist}')
        self.data['blocks'] = dict(count=len(s.blocks), padding=padding[:20], flags=dict(flags), totals=dict(totals))
        # Full decode check (this is also what every edit relies on).
        t = time.time()
        total = 0
        for c in range(len(s.chunks)):
            total += len(s.chunk_original(c, keep=False))
        self.out(f'  decoded all chunks: {total} bytes in {time.time() - t:.1f}s')

    def chunks(self):
        w, s = self.w, self.w.stream
        self.out('  chunk location   blocks  decoded  records  kinds')
        rows = []
        for c, chunk in enumerate(s.chunks):
            recs = s.records(c, keep=False)
            kinds = collections.Counter(r.kind for r in recs)
            tracks = collections.Counter(r.track for r in recs)
            rows.append(dict(chunk=c, location=w.chunk_label(c), blocks=len(chunk.blocks),
                             decoded=chunk.decoded_size, kinds=dict(kinds), tracks=dict(tracks)))
            self.out(f'  {c:5d} {w.chunk_label(c):10s} {len(chunk.blocks):6d} {chunk.decoded_size:8d} '
                     f'{len(recs):8d}  {dict(sorted(kinds.items()))} tracks {dict(sorted(tracks.items()))}')
        self.data['chunks'] = rows
        last = w.sdb.locations[-1].chunk_end if w.sdb.locations else -1
        self.out(f'  last SDB chunk_end {last}, SSB chunks {len(s.chunks)}')

    def sdb_offsets(self):
        w, s = self.w, self.w.stream
        raw = w._member('.sdb')
        start = (80 + 88 * w.sdb.location_count + 15) & ~15
        chunk_offsets = [ch.blocks[0].offset for ch in s.chunks]
        chunk_ends = [ch.blocks[-1].offset + ch.blocks[-1].extent for ch in s.chunks]
        candidates = {
            'chunk byte offset': set(chunk_offsets),
            'chunk end offset': set(chunk_ends),
            'chunk sector (/2048)': {o // 2048 for o in chunk_offsets if o % 2048 == 0},
            'block byte offset': {b.offset for b in s.blocks},
            'chunk first block index': {ch.blocks[0].index for ch in s.chunks},
            'chunk decoded size': {ch.decoded_size for ch in s.chunks},
            'chunk compressed size': {e - o for o, e in zip(chunk_offsets, chunk_ends)},
        }
        words = [(p, struct.unpack_from('<I', raw, p)[0]) for p in range(start, len(raw) - 3, 4)]
        for label, values in candidates.items():
            values = {v for v in values if v > 64}    # small numbers match everything
            hits = [(p, v) for p, v in words if v in values]
            rel = collections.Counter((p - start) % 96 for p, _ in hits if p - start < 96 * w.sdb.chunk_info_count)
            self.out(f'  {label:26s}: {len(hits):5d} hits; by position mod 96 in chunk infos {dict(rel.most_common(4))};'
                     f' first {[(hex(p), v) for p, v in hits[:4]]}')
        # Dump the first chunk-info rows for eyeballing.
        self.out('  first chunk infos (96 bytes each, 12 floats + 4 ints shown as ints):')
        for i in range(min(4, w.sdb.chunk_info_count)):
            p = start + 96 * i
            if p + 96 <= len(raw):
                f = struct.unpack_from('<20f', raw, p)
                ints = struct.unpack_from('<4I', raw, p + 80)
                self.out(f'   [{i}] floats {[round(x, 1) for x in f]} ints {ints}')
        sub_start = start + 96 * w.sdb.chunk_info_count
        self.out('  first sub-chunk infos (68 bytes, u16 x 20 + u32 x 7):')
        for i in range(min(6, w.sdb.sub_chunk_count)):
            p = sub_start + 68 * i
            if p + 68 <= len(raw):
                self.out(f'   [{i}] {struct.unpack_from("<20H", raw, p)} {struct.unpack_from("<7I", raw, p + 40)}')

    def records(self):
        s = self.w.stream
        sizes = collections.defaultdict(list)
        for c in range(len(s.chunks)):
            for r in s.records(c, keep=False):
                sizes[r.kind].append(r.size)
        self.out('  kind name                          count  sizes')
        for k in sorted(sizes):
            self.out(f'  {k:4d} {KIND_NAMES.get(k, "?"):28s} {len(sizes[k]):6d}  {_dist(sizes[k])}')
        self.data['record_kinds'] = {k: len(v) for k, v in sizes.items()}

    def _sample_records(self, kind, limit):
        s = self.w.stream
        out = []
        step = max(1, len(s.chunks) // 12)
        for c in list(range(0, len(s.chunks), step)):
            data = s.chunk_original(c, keep=False)
            for r in ssb.parse_records(data):
                if r.kind == kind:
                    out.append((c, r, bytes(data[r.offset:r.offset + r.size])))
                    if len(out) >= limit:
                        return out
        return out

    def patches(self):
        samples = self._sample_records(1, 400)
        if not samples:
            self.out('  none sampled')
            return
        self.out(f'  sampled {len(samples)}; sizes {collections.Counter(len(d) for _, _, d in samples)}')
        u = collections.defaultdict(collections.Counter)
        corner = collections.Counter()
        bbox = collections.Counter()
        chunk_field = collections.Counter()
        resource_ok = 0
        for c, r, d in samples:
            if len(d) < 432:
                continue
            i0, i1 = struct.unpack_from('<2i', d, 0)
            h = struct.unpack_from('<4h', d, 8)
            u['+0x00'][i0] += 1
            u['+0x04'][i1] += 1
            for k, v in enumerate(h):
                u[f'+0x{8 + 2 * k:02X}'][v] += 1
            word, = struct.unpack_from('<I', d, 0x150)
            resource_ok += word == r.resource
            s10, s11 = struct.unpack_from('<2h', d, 0x154)
            chunk_field['+0x156 == chunk' if s11 == c else '+0x156 != chunk'] += 1
            u['+0x154'][s10] += 1
            coeff = [struct.unpack_from('<4f', d, 0x40 + 16 * k) for k in range(16)]
            pts = [struct.unpack_from('<3f', d, 0x158 + 12 * k) for k in range(6)]

            def close(a, b, tol=0.5):
                return all(abs(x - y) <= tol + 1e-4 * abs(y) for x, y in zip(a, b))
            # Hypothesis: power basis, reverse order -> coeff[15] is the constant term.
            p00 = coeff[15][:3]
            p11 = tuple(sum(cf[k] for cf in coeff) for k in range(3))
            corner['P(0,0)=coeff[15] in points'] += any(close(p00, p) for p in pts[:4])
            corner['P(1,1)=sum(coeff) in points'] += any(close(p11, p) for p in pts[:4])
            corner['coeff[0] in points'] += any(close(coeff[0][:3], p) for p in pts[:4])
            lo = [min(p[k] for p in pts[:4]) for k in range(3)]
            hi = [max(p[k] for p in pts[:4]) for k in range(3)]
            bbox['+0x188/+0x194 contains points 1-4'] += all(pts[4][k] - 1 <= lo[k] and hi[k] <= pts[5][k] + 1
                                                             for k in range(3))
            bbox['+0x158/+0x164 = min/max (box)'] += all(pts[0][k] <= pts[1][k] for k in range(3))
        n = len(samples)
        self.out(f'  resource word +0x150 equals record (rid<<8|track): {resource_ok}/{n}')
        self.out(f'  {dict(chunk_field)}')
        for k, v in corner.items():
            self.out(f'  {k}: {v}/{n}')
        for k, v in bbox.items():
            self.out(f'  {k}: {v}/{n}')
        for field, counter in u.items():
            self.out(f'  field {field}: {counter.most_common(6)}')
        c, r, d = samples[0]
        self.out(f'  first sampled patch (chunk {c}, track {r.track}, rid {r.rid}):')
        for line in _hexdump(d[:0x40] + b'....' + d[0x140:]):
            self.out(line)

    def instances(self):
        samples = self._sample_records(3, 300)
        if not samples:
            self.out('  none sampled')
            return
        stats = collections.Counter()
        for c, r, d in samples:
            if len(d) < 0x90:
                stats['short'] += 1
                continue
            m = struct.unpack_from('<16f', d, 0x10)
            t = m[12:15]
            v0 = struct.unpack_from('<4f', d, 0x50)
            lo = struct.unpack_from('<3f', d, 0x60)
            hi = struct.unpack_from('<3f', d, 0x6C)
            word, = struct.unpack_from('<I', d, 0x78)
            stats['+0x78 == resource'] += word == r.resource
            centre = [(a + b) / 2 for a, b in zip(lo, hi)]
            stats['bbox contains translation'] += all(lo[k] - 1 <= t[k] <= hi[k] + 1 for k in range(3))
            stats['V0.xyz ~ bbox centre'] += all(abs(v0[k] - centre[k]) <= 1 + 1e-3 * abs(centre[k]) for k in range(3))
            stats['V0.xyz ~ translation'] += all(abs(v0[k] - t[k]) <= 1 + 1e-3 * abs(t[k]) for k in range(3))
            stats['matrix w column = (0,0,0,1)'] += (abs(m[3]) + abs(m[7]) + abs(m[11]) < 1e-4 and abs(m[15] - 1) < 1e-4)
        self.out(f'  sampled {len(samples)}: {dict(stats)}')
        c, r, d = samples[0]
        name = self.w.name_of(1, r.track, r.rid)
        self.out(f'  first sampled instance {name} (chunk {c}):')
        for line in _hexdump(d[:0x90]):
            self.out(line)

    def painters(self):
        w = self.w
        rows = []
        for loc in w.sdb.locations:
            for c, r in w.records(kind=15, location=loc):
                data = w.stream.chunk_original(c, keep=False)[r.offset:r.offset + r.size]
                try:
                    secs = painter.sections(data)
                    fog = painter.fog_entries(data)
                except painter.PainterError as e:
                    self.out(f'  {loc.name:10s} chunk {c}: {e}')
                    continue
                first = fog[0][1] if fog else None
                rows.append(dict(location=loc.name, chunk=c, sections=sorted(secs), fog=[f for _, f in fog]))
                txt = (f"near {first['near_cm']:.0f} far {first['far_cm']:.0f} colour "
                       f"({first['r']:.2f}, {first['g']:.2f}, {first['b']:.2f}) density {first['density']:.3g}"
                       if first else 'no fog')
                self.out(f'  {loc.name:10s} chunk {c:4d} sections {sorted(secs)} fog entries {len(fog)}: {txt}')
        self.data['painters'] = rows

    def textures(self):
        s = self.w.stream
        by_rid = collections.defaultdict(set)
        fmts = collections.Counter()
        dims = collections.Counter()
        errors = collections.Counter()
        tracks = collections.Counter()
        for c in range(len(s.chunks)):
            data = s.chunk_original(c, keep=False)
            for r in ssb.parse_records(data):
                if r.kind != 9:
                    continue
                tracks[r.track] += 1
                rec = bytes(data[r.offset:r.offset + r.size])
                by_rid[r.rid].add(hashlib.sha1(rec).hexdigest())
                try:
                    sh = texture.parse(rec)
                    fmts[sh.fmt] += 1
                    dims[(sh.width, sh.height)] += 1
                except texture.TextureError as e:
                    errors[str(e)] += 1
        differing = [rid for rid, hashes in by_rid.items() if len(hashes) > 1]
        self.out(f'  records {sum(tracks.values())}, distinct ids {len(by_rid)}, tracks {dict(tracks)} '
                 f'(expected all on {TRACK_SHARED})')
        self.out(f'  ids whose copies differ: {len(differing)} {sorted(differing)[:10]}')
        self.out(f'  formats {dict(fmts)}; sizes {dims.most_common(6)}; parse errors {dict(errors)}')

    def encoder(self):
        s = self.w.stream
        blocks = s.blocks
        if not blocks:
            return
        n = min(self.sample_blocks, len(blocks))
        picks = sorted({int(i * (len(blocks) - 1) / max(1, n - 1)) for i in range(n)})
        diffs = []
        failures = 0
        t = time.time()
        for i in picks:
            b = blocks[i]
            payload = s.block_payload(b)
            data, consumed = refpack.decompress(payload, with_consumed=True)
            header = refpack.parse_header(payload)
            ours = len(refpack.compress(data, header=header.raw))
            diffs.append(ours - consumed)
            try:
                exact = refpack.compress_exact(data, consumed, header=header.raw)
                assert len(exact) == consumed
            except refpack.ExactSizeError:
                failures += 1
        self.out(f'  {len(picks)} blocks: our size minus original (negative = we are smaller): {_dist(diffs)}')
        self.out(f'  exact-size re-encode impossible for {failures}/{len(picks)} sampled blocks '
                 f'({time.time() - t:.1f}s)')
        self.data['encoder_diffs'] = diffs

    def dry_run(self):
        w = self.w
        stream = ssb.WorldStream(w.stream.original)     # private copy; the report's world stays clean
        edits = 0
        for loc in w.sdb.locations[::max(1, len(w.sdb.locations) // 8)]:
            for c, r in w.records(kind=15, location=loc):
                try:
                    painter.edit_fog(stream.chunk(c), r.offset, r.size, dict(r=1.0, g=0.2, b=0.2))
                    edits += 1
                except painter.PainterError as e:
                    self.out(f'  fog {loc.name}: {e}')
        tex = 0
        for c in range(0, len(stream.chunks), max(1, len(stream.chunks) // 6)):
            for r in stream.records(c):
                if r.kind == 9 and tex < 6:
                    try:
                        texture.tint(stream.chunk(c), r.offset, r.size, mul=(1.0, 0.6, 0.8))
                        tex += 1
                    except texture.TextureError as e:
                        self.out(f'  texture {r.rid}: {e}')
                    break
        self.out(f'  edited {edits} painter records and {tex} texture records; re-encoding...')
        try:
            image, report = stream.build()
            stream.verify(image)
        except ssb.StreamError as e:
            self.out(f'  FAILED: {e}')
            return
        methods = collections.Counter(x['method'] for x in report)
        self.out(f'  OK: {len(report)} blocks re-encoded, methods {dict(methods)}, '
                 f'padding used {sum(x["used_padding"] for x in report)} bytes; output verified')
        self.data['dry_run'] = report

    def geometry(self):
        import struct as st
        w, s = self.w, self.w.stream
        raw = w._member('.sdb')
        start = (80 + 88 * w.sdb.location_count + 15) & ~15
        sub_start = start + 96 * w.sdb.chunk_info_count
        align_of = {1: 16, 2: 16, 3: 16, 11: 16, 12: 16, 9: 128, 10: 128}
        hyps = collections.Counter()
        rows = []
        count_ok = shorts_ok = 0
        for c in range(len(s.chunks)):
            p = sub_start + 68 * c
            if p + 68 > len(raw):
                break
            u16 = st.unpack_from('<20H', raw, p)
            nrec, cid = u16[0], u16[1]
            off, size = st.unpack_from('<2I', raw, p + 4)
            recs = s.records(c, keep=False)
            kinds = collections.Counter(r.kind for r in recs)
            count_ok += nrec == len(recs) and cid == c and off == s.chunks[c].blocks[0].offset
            counts = list(u16[6:19])
            shorts_ok += counts == [kinds.get(k, 0) for k in range(13)]
            dec = s.chunks[c].decoded_size
            variants = {}
            for hdr in (0, 8):
                for aligned in (False, True):
                    for skip_name, skip in (('none', ()), ('14', (14,)), ('20', (20,)), ('14+20', (14, 20)),
                                            ('9+10', (9, 10)), ('9+10+14+20', (9, 10, 14, 20))):
                        total = 0
                        for r in recs:
                            if r.kind in skip:
                                continue
                            n = r.size + hdr
                            if aligned:
                                a = align_of.get(r.kind, 4)
                                n = (n + a - 1) // a * a
                            total += n
                        variants[f'hdr{hdr} {"aligned" if aligned else "raw"} skip {skip_name}'] = total
            variants['kinds<=12 with headers'] = sum(r.size + 8 for r in recs if r.kind <= 12)
            for name, total in variants.items():
                hyps[name] += total == size
            rows.append((c, size, dec, variants))
        n = len(rows)
        self.out(f'  sub-chunk infos: record count, chunk id and SSB offset match for {count_ok}/{n}; '
                 f'per-kind counts (u16 6..18 = kinds 0..12) match for {shorts_ok}/{n}')
        best = hyps.most_common(3) + [('kinds<=12 with headers', hyps['kinds<=12 with headers'])]
        self.out(f'  size field (+8) hypotheses, chunks matching: {best}')
        diffs = [size - dec for _, size, dec, _ in rows]
        self.out(f'  size field minus decoded size: {_dist(diffs)}')
        for c, size, dec, variants in rows[:3] + [r for r in rows if r[0] in (33, 36)]:
            close = min(variants.items(), key=lambda kv: abs(kv[1] - size))
            self.out(f'   chunk {c}: field {size}, decoded {dec}, closest {close[0]} = {close[1]}')
        # Location shorts = record counts of the location's main chunk?
        match = 0
        for loc in w.sdb.locations:
            kinds = collections.Counter(r.kind for r in s.records(loc.chunk_end, keep=False))
            match += list(loc.shorts[:23]) == [kinds.get(k, 0) for k in range(23)]
        self.out(f'  location shorts 0..22 = record counts of the last chunk: {match}/{len(w.sdb.locations)}')
        own = 0
        for loc in w.sdb.locations:
            kinds = collections.Counter(r.kind for c in loc.chunks for r in s.records(c, keep=False)
                                        if r.track == loc.index)
            own += list(loc.shorts[:23]) == [kinds.get(k, 0) for k in range(23)]
        self.out(f'  location shorts 0..22 = counts of records on the location\'s own track: '
                 f'{own}/{len(w.sdb.locations)}; shorts 23..27 of ARA1: '
                 f'{[l.shorts[23:] for l in w.sdb.locations if l.name == "ARA1"]}')
        # Patch box rule.
        rules = collections.Counter()
        sampled = self._sample_records(1, 300)
        for _, _, d in sampled:
            p = terrain.Patch(d)
            lo, hi = p.bbox
            net = [q for row in p.control_net() for q in row]
            nlo = [min(q[k] for q in net) for k in range(3)]
            nhi = [max(q[k] for q in net) for k in range(3)]
            dense = [p.point(u / 8, v / 8) for u in range(9) for v in range(9)]
            dlo = [min(q[k] for q in dense) for k in range(3)]
            dhi = [max(q[k] for q in dense) for k in range(3)]
            close = lambda a, b: all(abs(x - y) <= 0.05 + 1e-5 * abs(y) for x, y in zip(a, b))
            rules['box = control net'] += close(lo, nlo) and close(hi, nhi)
            rules['box ~ surface (8x8)'] += all(abs(x - y) < 2 for x, y in zip(lo + hi, dlo + dhi))
            rules['box contains surface'] += all(lo[k] - 0.05 <= q[k] <= hi[k] + 0.05 for q in dense for k in range(3))
            sph = st.unpack_from('<4f', d, terrain.SPHERE)
            rules['sphere contains surface'] += all(math.dist(sph[:3], q) <= sph[3] + 0.05 for q in dense)
        self.out(f'  terrain box/sphere rules over {len(sampled)} patches: {dict(rules)}')
        # Rails: what is row50?
        rails = self._sample_records(8, 40)
        kinds = collections.Counter()
        for _, _, d in rails:
            for k in range((len(d) - 48) // 144):
                b = 48 + 144 * k
                row50 = st.unpack_from('<4f', d, b + 0x50)
                lo = st.unpack_from('<3f', d, b + 0x6C)
                hi = st.unpack_from('<3f', d, b + 0x78)
                inside = all(lo[i] - 1 <= row50[i] <= hi[i] + 1 for i in range(3))
                unit = abs(math.sqrt(sum(v * v for v in row50[:3])) - 1) < 1e-3
                kinds['row50 inside segment box' if inside else 'row50 unit vector' if unit else 'row50 other'] += 1
        self.out(f'  rail segment row50: {dict(kinds)}')
        if rails:
            d = rails[0][2]
            self.out(f'   first rail segment rows: {[round(v, 2) for v in st.unpack_from("<20f", d, 48 + 0x10)]}')

    def courses(self):
        w = self.w
        codes = ['ARA1', 'BRA2', 'CRA3', 'DRA4', 'ERA5', 'ASS1', 'ABA1', 'BHP1', 'ABC1']
        from . import aip as aipmod
        for code in codes:
            try:
                course = mapedit.course_aip(w, code)
            except KeyError:
                continue
            if course is None:
                self.out(f'  {code}: no AIP')
                continue
            line = aipmod.course_line(course)
            start = course.start()
            self.out(f'  {code}: {len(course.ai_paths)} AI paths, {len(course.track_paths)} track paths, '
                     f'regions {collections.Counter(r.kind for r in course.regions)}; start '
                     f'{tuple(round(v / 100, 1) for v in start.position) if start else None}')
            for p in course.track_paths:
                pts = p.points()
                self.out(f'    track {p.index}: header {p.header[:3]} {p.header[3]:.1f}; {len(p.segments)} segs, '
                         f'{p.length / 100:.0f} m, from {tuple(round(v / 100) for v in pts[0])} to '
                         f'{tuple(round(v / 100) for v in pts[-1])}, in bounds {p.fits_bounds()}')
            if line:
                self.out(f'    line: parts {line.parts}, gaps {[round(g / 100, 1) for g in line.gaps]} m, '
                         f'{line.length / 100:.0f} m after the start (start at {line.start_offset / 100:.0f} m)')
            sessions = sorted((r for r in course.regions if r.kind == 1), key=lambda r: r.slot)
            if line and sessions:
                self.out('    session points at ' + ', '.join(
                    f'{r.slot}: {line.nearest(r.position[0], r.position[1])[0] / 100:.0f} m' for r in sessions))
        # Terrain dry run: a 3 m kicker 200 m down Snow Jam, re-encoded in memory.
        try:
            from .world import World
            probe = World.__new__(World)
            probe.__dict__.update(w.__dict__)
            probe.stream = ssb.WorldStream(w.stream.original)
            pl = mapedit.place(probe, 'ARA1', along=200.0)
            dz = terrain.bump(pl.frame, 300.0, 2500.0)
            rep = mapedit.apply_field(probe, 'ARA1', pl.frame, dz)
            sizes = sorted(rep.patch_sizes) or [0]
            self.out(f'  ARA1 bump (3 m, radius 25 m) at 200 m: x {pl.frame.x / 100:.1f} y {pl.frame.y / 100:.1f} '
                     f'z {pl.z / 100 if pl.z is not None else None}; {rep.patches} patches '
                     f'(typical {sizes[len(sizes) // 2] / 100:.1f} m, largest {sizes[-1] / 100:.1f} m), shape error '
                     f'{rep.shape_error / 100:.2f} m, {rep.objects} objects, {rep.rails} rails moved, '
                     f'{len(rep.rails_in_area)} rails left, {rep.points} points')
            image, report = probe.stream.build()
            probe.stream.verify(image)
            self.out(f'  re-encoded and verified: {collections.Counter(x["method"] for x in report)}')
        except Exception as e:      # noqa: BLE001
            self.out(f'  terrain dry run failed: {type(e).__name__}: {e}')
