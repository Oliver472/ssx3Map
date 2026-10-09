"""Growing the world data (the probe test images), on synthetic data."""
import contextlib
import io
import os
import random
import struct
import tempfile
import unittest

from ssx3map import grow, iso9660, mapedit, refpack, ssb, terrain
from ssx3map.cli import main
from ssx3map.world import BIG_PATH, World

from . import fixtures


class GrowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        big, cls.chunks = fixtures.build_course_world()
        cls.big = os.path.join(cls.tmp, 'BAM.BIG')
        with open(cls.big, 'wb') as f:
            f.write(big)
        cls.iso = os.path.join(cls.tmp, 'game.iso')
        with open(cls.iso, 'wb') as f:
            f.write(fixtures.build_iso(big))

    def grown(self, number, name):
        w = World(self.big)
        big, notes = grow.experiment(w, number, 'AAA')
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(big)
        return w, World(path), notes

    def test_relocate_file(self):
        out = os.path.join(self.tmp, 'moved.iso')
        entry, big = iso9660.read_file(self.iso, BIG_PATH)
        payload = big + b'x' * 5000
        before = os.path.getsize(self.iso)
        lba = iso9660.relocate_file(self.iso, BIG_PATH, payload, out)
        self.assertEqual(lba, before // iso9660.SECTOR)
        moved, got = iso9660.read_file(out, BIG_PATH)
        self.assertEqual((moved.lba, got), (lba, payload))
        with open(out, 'rb') as f:
            data = f.read()
        self.assertEqual(data[entry.offset:entry.offset + entry.size], bytes(entry.size))     # old place cleared
        self.assertEqual(data[before - 2048:before], b'\xAA' * 2048)                          # what followed stays
        total, = struct.unpack_from('<I', data, 16 * 2048 + 80)
        self.assertEqual(struct.unpack_from('>I', data, 16 * 2048 + 84)[0], total)
        self.assertEqual(total * iso9660.SECTOR, len(data))
        self.assertEqual([p for p, _, _ in iso9660.list_files(out)], ['SYSTEM.CNF', 'DATA/WORLDS/BAM.BIG'])

    def test_survey(self):
        s = grow.survey(World(self.iso), 'AAA')
        self.assertTrue(s.ok)
        self.assertEqual(s.size_rule, 'kinds 0-12, 8-byte headers')
        text = '\n'.join(s.lines)
        self.assertIn('= chunk offset in bam.ssb: 4/4', text)
        self.assertIn('per-kind counts 0..12 right: 4/4', text)
        self.assertIn('rids 0..59, 60 distinct, contiguous', text)
        self.assertIn('BAM.BIG at sector 22', text)

    def test_one_more_block_shifts_every_chunk(self):
        w, w2, _ = self.grown(2, 'block.BIG')
        self.assertEqual(len(w2.stream.chunks[0].blocks), len(w.stream.chunks[0].blocks) + 1)
        tables = grow.Tables(w2._member('.sdb'))
        for c, ch in enumerate(w2.stream.chunks):
            self.assertEqual(w2.stream.chunk_original(c), w.stream.chunk_original(c))
            self.assertEqual(tables.offset(c), ch.blocks[0].offset)
            if c:
                self.assertGreater(ch.blocks[0].offset, w.stream.chunks[c].blocks[0].offset)
        for name in ('.phm', '.psm'):
            self.assertEqual(w2.big.read(w2.big.find_suffix(name)), w.big.read(w.big.find_suffix(name)))

    def test_new_ramp(self):
        w, w2, notes = self.grown(3, 'ramp.BIG')
        before = w.stream.records(1)
        after = w2.stream.records(1)
        self.assertEqual(len(after), len(before) + 1)
        patches = [r for r in after if r.kind == 1]
        new = patches[-1]
        self.assertEqual((len(patches), new.rid, new.track), (61, 60, 0))
        # Everything else in the chunk is as it was, in the same order.
        data0, data1 = w.stream.chunk_original(1), w2.stream.chunk_original(1)
        self.assertEqual([data0[r.header_offset:r.offset + r.size] for r in before],
                         [data1[r.header_offset:r.offset + r.size] for r in after if r is not new])
        tables = grow.Tables(w2._member('.sdb'))
        self.assertEqual(tables.record_count(1), len(after))
        self.assertEqual(tables.kind_counts(1)[1], 61)
        self.assertEqual(tables.size(1), sum(r.size + 8 for r in after if r.kind <= 12))
        self.assertEqual(tables.shorts(w2.location('AAA'))[1], 61)
        # A ramp on the race line: on the ground upstream, 3 m above it downstream, collidable.
        ramp = terrain.Patch(data1[new.offset:new.offset + new.size])
        metres, raw = grow.ramp(w, 'AAA', grow.course_terrain(w, 'AAA'))
        self.assertEqual(ramp.data[:terrain.RESOURCE], raw[:terrain.RESOURCE])
        source = mapedit.course_index(w, 'AAA')
        lifts = [ramp.point(u / 4, v / 4)[2] - source.z(*ramp.point(u / 4, v / 4)[:2])
                 for u in range(5) for v in range(5)]
        self.assertAlmostEqual(min(lifts), 0.0, delta=5.0)
        self.assertAlmostEqual(max(lifts), 300.0, delta=5.0)
        self.assertEqual(ramp.flags & 1, 1)
        self.assertEqual(struct.unpack_from('<I', ramp.data, terrain.RESOURCE)[0], (60 << 8) | 0)
        self.assertIn(f'ramp {metres} m after the start', notes[0])

    def test_many_patches_take_more_blocks_and_stay_editable(self):
        w, w2, _ = self.grown(6, 'plus100.BIG')
        self.assertGreater(len(w2.stream.chunks[1].blocks), len(w.stream.chunks[1].blocks))
        for c in (0, 2, 3):
            self.assertEqual(w2.stream.chunk_original(c), w.stream.chunk_original(c))
        data = w2.stream.chunk_original(1)
        patches = [terrain.Patch(data[r.offset:r.offset + r.size]) for r in w2.stream.records(1) if r.kind == 1]
        self.assertEqual(len(patches), 121)
        ground = mapedit.course_index(w, 'AAA')
        for p in patches[61:]:
            lo, hi = p.bbox
            self.assertLess(hi[0] - lo[0], 20)
            self.assertEqual(p.flags & 1, 0)
            z = ground.z((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2)
            if z is not None:
                self.assertLess(hi[2], z - 100)
        # The usual same-size edits still work on the grown world.
        out = os.path.join(self.tmp, 'plus100_jump.BIG')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(['terrain', w2.source, '--location', 'AAA', '--along', '20', '--shape', 'kicker',
                  '--height', '2', '--length', '12', '--width', '10', '-o', out])
        w3 = World(out)
        self.assertNotEqual(w3.stream.chunk_original(1), data)
        self.assertEqual(len(w3.stream.chunks[1].blocks), len(w2.stream.chunks[1].blocks))

    def test_refuses_when_offsets_are_not_understood(self):
        g = grow.Growth(World(self.big))
        g.tables.set_offset(2, 12345)
        g.split_last_block(0)
        with self.assertRaises(grow.GrowError):
            g.build()

    def test_size_without_a_known_rule_grows_by_the_added_bytes(self):
        w = World(self.big)
        g = grow.Growth(w)
        g.tables.set_size(1, 12345)
        ground = grow.course_terrain(w, 'AAA')
        recs = grow.new_terrain_records(ground, [ground.records[0][1].data])
        g.add_records(1, recs, size_rule='kinds 0-12, 8-byte headers')
        self.assertEqual(g.tables.size(1), 12345 + 432 + 8)

    def test_pack_fresh(self):
        rng = random.Random(5)
        data = bytes(rng.randrange(256) for _ in range(30000)) + bytes(20000)
        pieces = grow.pack_fresh(data, 20000, 8000)
        self.assertTrue(all(len(s) <= 8000 and len(p) <= 20000 for p, s in pieces))
        self.assertEqual(b''.join(refpack.decompress(s) for _, s in pieces), data)
        self.assertEqual(b''.join(p for p, _ in pieces), data)

    def test_probe_command(self):
        out = os.path.join(self.tmp, 'probes')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(['probe', self.iso, '--location', 'AAA', '-o', out, '--only', '1,3'])
        names = sorted(os.listdir(out))
        self.assertEqual(names, ['probe1_big_moved.iso', 'probe3_new_ramp.iso', 'probe_report.txt'])
        _, original = iso9660.read_file(self.iso, BIG_PATH)
        entry, moved = iso9660.read_file(os.path.join(out, names[0]), BIG_PATH)
        self.assertEqual(moved, original)
        self.assertGreater(entry.lba, iso9660.find(self.iso, BIG_PATH).lba)
        w = World(os.path.join(out, names[1]))
        self.assertEqual(sum(1 for r in w.stream.records(1) if r.kind == 1), 61)
        with open(os.path.join(out, 'probe_report.txt'), encoding='utf-8') as f:
            report = f.read()
        self.assertIn('size rule used for chunk 1: kinds 0-12, 8-byte headers', report)
        self.assertIn('3. probe3_new_ramp.iso', report)


if __name__ == '__main__':
    unittest.main()
