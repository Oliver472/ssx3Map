"""Wiping a course and building a plain slope with jumps on its route."""
import math
import os
import struct
import tempfile
import unittest

from ssx3map import aip, instances, mapedit, rebuild, ssb, terrain
from ssx3map.world import World

from . import fixtures

DESIGN = dict(grade=0.1, width=12, bank=4, bank_height=3, jumps=[(20, 1.5)], runup=6, lip=2, landing=8, step=0.5,
              fine=1.5)


class RouteTest(unittest.TestCase):
    def test_straight_line_stays_and_ends_extend(self):
        r = rebuild.Route([(0.0, 0.0, 0.0), (0.0, 10000.0, -2000.0)])
        r.smooth(3000.0)
        r.prepare()
        self.assertAlmostEqual(r.length, 10000.0)
        self.assertLess(max(math.dist(a, b) for a, b in zip(r.old, r.new)), 1e-6)
        self.assertEqual(r.locate(300.0, 5000.0), (5000.0, -300.0))           # right of the line: l < 0
        s, l = r.locate(0.0, 10500.0)                                          # past the end
        self.assertAlmostEqual(s, 10500.0)
        self.assertAlmostEqual(l, 0.0)
        x, y = r.point(10500.0, 200.0)
        self.assertAlmostEqual(x, -200.0, delta=1e-6)
        self.assertAlmostEqual(y, 10500.0, delta=1e-6)

    def test_tight_bends_are_smoothed(self):
        pts = [(5000 * math.cos(a / 10), 5000 * math.sin(a / 10), 0.0) for a in range(0, 30)]   # radius 50 m
        r = rebuild.Route(pts)
        r.smooth(6000.0)
        self.assertGreaterEqual(r.min_radius(), 1.3 * 6000.0 * 0.99)


class FlattenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, 'BAM.BIG')
        with open(cls.path, 'wb') as f:
            f.write(fixtures.build_course_world()[0])

    def build(self):
        w = World(self.path)
        before = bytes(w.stream.current(1))
        report = rebuild.flatten_course(w, 'AAA', rebuild.Design(**DESIGN))
        return w, before, report

    def test_new_ground(self):
        w, before, r = self.build()
        self.assertEqual(r.slots, 60)
        self.assertEqual(r.used, r.rows * r.cols)
        data = w.stream.current(1)
        recs = [x for x in ssb.parse_records(data) if x.kind == 1]
        live = [terrain.Patch(data[x.offset:x.offset + x.size]) for x in recs]
        on = [p for p in live if p.flags & 1]
        off = [p for p in live if not p.flags & 1]
        self.assertEqual(len(on), r.used)
        self.assertEqual(len(off), r.slots - r.used)
        for p in off:                                   # the leftovers: tiny, far below
            lo, hi = p.bbox
            self.assertLess(hi[2], -50000)
            self.assertLess(hi[0] - lo[0], 50)
        # Neighbouring patches share their edges (no cracks).
        edges = {}
        for p in on:
            for (a, b) in (((0, 0), (1, 0)), ((1, 0), (1, 1)), ((1, 1), (0, 1)), ((0, 1), (0, 0))):
                mid = p.point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                key = tuple(sorted((tuple(round(v) for v in p.point(*a)), tuple(round(v) for v in p.point(*b)))))
                edges.setdefault(key, []).append(mid)
        shared = [m for m in edges.values() if len(m) == 2]
        self.assertGreater(len(shared), 20)
        for a, b in shared:
            self.assertLess(math.dist(a, b), 1.0)
        # Along the old line: a plain fall of 10 %, the jump on top of it.
        index = terrain.PatchIndex(on)
        line = aip.course_line(mapedit.course_aip(w, 'AAA'))
        z = [index.z(*line.at(m * 100)[0][:2]) for m in (2, 6, 10)]
        self.assertAlmostEqual(z[0] - z[1], 40.0, delta=3.0)
        self.assertAlmostEqual(z[1] - z[2], 40.0, delta=3.0)
        lip = index.z(*line.at(2000.0)[0][:2])
        self.assertAlmostEqual(lip - (z[2] - 100.0), 150.0, delta=10.0)          # 1.5 m above the slope at 20 m
        # Across: flat piste, walls rising to 3 m.
        p, h = line.at(1000.0)
        right = (h[1], -h[0])
        mid = index.z(p[0], p[1])
        self.assertAlmostEqual(index.z(p[0] + 400 * right[0], p[1] + 400 * right[1]), mid, delta=2.0)
        self.assertAlmostEqual(index.z(p[0] + 980 * right[0], p[1] + 980 * right[1]) - mid, 300.0, delta=15.0)

    def test_everything_else(self):
        w, before, r = self.build()
        data = w.stream.current(1)
        recs = ssb.parse_records(before)
        # Scenery sunk 1 km.
        for x in recs:
            if x.kind == 3:
                self.assertAlmostEqual(instances.Instance(data[x.offset:x.offset + 0x90]).translation[2]
                                       - instances.Instance(before[x.offset:x.offset + 0x90]).translation[2],
                                       mapedit.SINK, delta=1.0)
        self.assertEqual((r.sunk, r.rails, r.particles, r.curtains), (2, 1, 1, 1))
        # Race data on the new ground: the course keeps its length across the ground.
        rec = [x for x in recs if x.kind == 14 and x.rid == 0][0]
        a0 = aip.decode(before[rec.offset:rec.offset + rec.size])
        a1 = mapedit.course_aip(w, 'AAA')
        self.assertAlmostEqual(a1.track_paths[0].length, a0.track_paths[0].length, delta=5.0)
        index = mapedit.course_index(w, 'AAA')
        for reg in a1.regions:
            g = index.z(reg.position[0], reg.position[1])
            self.assertIsNotNone(g)
            self.assertLess(abs(reg.position[2] - g), 160.0)
        self.assertEqual(r.points, 2)
        self.assertGreater(r.gates, 0)

    def test_patches_stream_with_the_old_piste_there(self):
        # The game draws a patch only while its texture chunk (+0x156) is loaded, and texture
        # chunks follow race progress: the far half of this course uses chunk 3 and texture 3.
        w = World(self.path)
        buf = w.stream.chunk(1)
        for rec in w.stream.records(1):
            if rec.kind == 1:
                p = terrain.Patch(buf[rec.offset:rec.offset + rec.size])
                if p.point(0.5, 0.5)[1] > 3000:
                    struct.pack_into('<h', buf, rec.offset + 0x156, 3)
                    struct.pack_into('<h', buf, rec.offset + 0x1A0, 3)
        r = rebuild.flatten_course(w, 'AAA', rebuild.Design(**DESIGN))
        data = w.stream.current(1)
        textures = {c: {x.rid for x in w.stream.records(c) if x.kind == 9} for c in range(len(w.stream))}
        seen = set()
        for rec in w.stream.records(1):
            if rec.kind != 1:
                continue
            p = terrain.Patch(data[rec.offset:rec.offset + rec.size])
            if not p.flags & 1:
                continue
            chunk, = struct.unpack_from('<h', p.data, 0x156)
            self.assertIn(p.texture, textures[chunk])
            far = p.point(0.5, 0.5)[1] > 3300
            near = p.point(0.5, 0.5)[1] < 2700
            if far:
                self.assertEqual((chunk, p.texture), (3, 3))
            if near:
                self.assertEqual((chunk, p.texture), (0, 7))
            seen.add(chunk)
        self.assertEqual(seen, {0, 3})

    def test_fits_and_saves(self):
        w, before, r = self.build()
        for c in w.stream.changed_chunks():
            self.assertLessEqual(w.stream.shortfall(c), 0)
        out = os.path.join(self.tmp, 'flat.BIG')
        w.save(out)
        self.assertEqual(bytes(World(out).stream.current(1)), bytes(w.stream.current(1)))

    def test_many_jumps_get_longer_patches(self):
        w = World(self.path)
        design = rebuild.Design(**dict(DESIGN, jumps=[(m, 1.0) for m in range(2, 48, 2)], fine=0.2))
        r = rebuild.flatten_course(w, 'AAA', design)
        self.assertGreater(r.fine, 20.0)                # 0.2 m was asked; the 60 patches cannot give that
        self.assertLessEqual(r.used, r.slots)


if __name__ == '__main__':
    unittest.main()
