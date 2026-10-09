"""Moving a piece of the course sideways: every world-space record follows the ground."""
import math
import os
import struct
import tempfile
import unittest

from ssx3map import aip, instances, mapedit, ssb, terrain, warp
from ssx3map.world import World

from . import fixtures


def records(w, kind, chunk=1):
    return [r for r in w.stream.records(chunk) if r.kind == kind]


def rail_points(data, offset, size):
    out = []
    for k in range((size - 48) // 144):
        base = offset + 48 + 144 * k
        rows = [struct.unpack_from('<3f', data, base + 0x10 + 16 * r) for r in range(4)]
        out.append([warp._cubic(rows, t) for t in (0.0, 0.25, 0.5, 0.75, 1.0)])
    return out


class GrabTest(unittest.TestCase):
    def test_rigid_inside_and_still_outside(self):
        g = warp.Grab((1000.0, 2000.0), (300.0, -100.0, 50.0), 500.0, 1500.0)
        self.assertEqual(g(1200.0, 2100.0), (300.0, -100.0, 50.0))
        self.assertEqual(g(1000.0 + 2001.0, 2000.0), (0.0, 0.0, 0.0))
        d = g(1000.0 + 1250.0, 2000.0)                          # half way through the edge
        self.assertAlmostEqual(d[0], 150.0, delta=1e-6)
        self.assertEqual(g.yaw(1100.0, 2000.0), 0.0)
        self.assertAlmostEqual(g.min_stretch(), 1 - 1.5 * math.hypot(300, -100) / 1500, delta=0.02)

    def test_turn(self):
        g = warp.Grab((0.0, 0.0), (0.0, 0.0, 0.0), 1000.0, 3000.0, turn=30.0)
        x, y = 400.0, 0.0
        d = g(x, y)
        self.assertAlmostEqual(x + d[0], 400 * math.cos(math.radians(30)), delta=1e-6)
        self.assertAlmostEqual(y + d[1], 400 * math.sin(math.radians(30)), delta=1e-6)
        self.assertAlmostEqual(math.degrees(g.yaw(x, y)), 30.0, delta=0.01)
        self.assertEqual(g.yaw(5000.0, 0.0), 0.0)


class DisplacePatchTest(unittest.TestCase):
    def test_constant_move_translates_the_patch(self):
        rec = bytearray(fixtures.patch_record(0.0, 0.0, 0, 0))
        before = terrain.Patch(bytes(rec))
        moved = terrain.displace(rec, 0, lambda x, y, z: (120.0, -40.0, 15.0))
        self.assertAlmostEqual(moved, math.sqrt(120 ** 2 + 40 ** 2 + 15 ** 2), delta=1e-6)
        after = terrain.Patch(bytes(rec))
        for u in (0.0, 0.3, 1.0):
            for v in (0.0, 0.6, 1.0):
                p, q = before.point(u, v), after.point(u, v)
                for a, b, d in zip(p, q, (120.0, -40.0, 15.0)):
                    self.assertAlmostEqual(b - a, d, delta=0.05)
        for p, q in zip(before.stored_corners, after.stored_corners):
            self.assertAlmostEqual(q[0] - p[0], 120.0, delta=0.05)
        lo, hi = after.bbox
        for p in [after.point(u / 4, v / 4) for u in range(5) for v in range(5)]:
            self.assertTrue(all(lo[k] <= p[k] <= hi[k] for k in range(3)))

    def test_height_only_keeps_xy_bits(self):
        rec = bytearray(fixtures.patch_record(500.0, 500.0, 0, 0))
        xy = [rec[0x40 + 16 * k:0x40 + 16 * k + 8] for k in range(16)]
        terrain.deform(rec, 0, lambda x, y, z: 30.0)
        self.assertEqual(xy, [rec[0x40 + 16 * k:0x40 + 16 * k + 8] for k in range(16)])


class WarpCourseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.big, cls.chunks = fixtures.build_course_world()
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, 'BAM.BIG')
        with open(cls.path, 'wb') as f:
            f.write(cls.big)

    def world(self):
        return World(self.path)

    def grab(self, **kw):
        args = dict(origin=(1500.0, 2500.0), move=(300.0, 0.0, 0.0), radius=500.0, edge=1500.0)
        args.update(kw)
        return warp.Grab(**args)

    def test_everything_moves_with_the_ground(self):
        w = self.world()
        before = bytes(w.stream.current(1))
        g = self.grab()
        report = warp.warp_edit(w, 'AAA', g)
        after = bytes(w.stream.current(1))
        recs = ssb.parse_records(before)

        def pair(kind, n=0):
            r = [r for r in recs if r.kind == kind][n]
            return r, before[r.offset:r.offset + r.size], after[r.offset:r.offset + r.size]

        # Terrain: every patch follows the field (within the reported fit error).
        self.assertGreater(report.patches, 20)
        self.assertLess(report.shape_error, 25.0)
        self.assertEqual(report.sideways, 'bilinear')
        for r in recs:
            if r.kind != 1:
                continue
            p0 = terrain.Patch(before[r.offset:r.offset + r.size])
            p1 = terrain.Patch(after[r.offset:r.offset + r.size])
            for u, v in ((0.0, 0.0), (0.5, 0.5), (1.0, 0.3)):
                self.assertLess(math.dist(p1.point(u, v), g.apply(p0.point(u, v))), report.slip + 0.1)
        self.assertLessEqual(report.shape_error, report.slip)
        # The tree under the grab moves 3 m with its box; the far one stays.
        r, old, new = pair(3, 0)
        i0, i1 = instances.Instance(old), instances.Instance(new)
        self.assertAlmostEqual(i1.translation[0] - i0.translation[0], 300.0, delta=1e-3)
        self.assertAlmostEqual(i1.bbox[0][0] - i0.bbox[0][0], 300.0, delta=1e-3)
        self.assertEqual(pair(3, 1)[1], pair(3, 1)[2])
        self.assertEqual(report.objects, 1)
        # Particles, lights and glows.
        for kind, at in ((5, 0x40), (6, 56), (7, 28)):
            r, old, new = pair(kind)
            a, b = struct.unpack_from('<3f', old, at), struct.unpack_from('<3f', new, at)
            self.assertAlmostEqual(b[0] - a[0], 300.0, delta=1e-3, msg=f'kind {kind}')
        r, old, new = pair(6)
        self.assertEqual(struct.unpack_from('<3f', old, 44), struct.unpack_from('<3f', new, 44))   # no turn
        # Rail: bent, still chained, distances add up, boxes hold the curve.
        r, old, new = pair(8)
        old_pts, new_pts = rail_points(old, 0, len(old)), rail_points(new, 0, len(new))
        for seg0, seg1 in zip(old_pts, new_pts):
            for p, q in zip(seg0, seg1):
                self.assertLess(math.dist(q, g.apply(p)), report.rail_error + 2.0)
        for a, b in zip(new_pts, new_pts[1:]):
            self.assertLess(math.dist(a[-1], b[0]), 1.0)
        for k in range(len(new_pts) - 1):
            base = 48 + 144 * k
            length, = struct.unpack_from('<f', new, base + 0x0C)
            d0, = struct.unpack_from('<f', new, base + 0x84)
            d1, = struct.unpack_from('<f', new, base + 144 + 0x84)
            self.assertAlmostEqual(d0 + length, d1, delta=0.01)
        for k, seg in enumerate(new_pts):
            lo = struct.unpack_from('<3f', new, 48 + 144 * k + 0x6C)
            hi = struct.unpack_from('<3f', new, 48 + 144 * k + 0x78)
            self.assertTrue(all(lo[a] <= p[a] <= hi[a] for p in seg for a in range(3)))
        self.assertEqual(report.rails, 1)
        # Race paths: the points follow, the encoding stays (unit direction, slope, length).
        r = [r for r in recs if r.kind == 14 and r.rid == 0][0]
        a0 = aip.decode(before[r.offset:r.offset + r.size])
        a1 = aip.decode(after[r.offset:r.offset + r.size])
        for p0, p1 in zip(a0.track_paths + a0.ai_paths, a1.track_paths + a1.ai_paths):
            for p, q in zip(p0.points(), p1.points()):
                self.assertLess(math.dist(q, g.apply(p)), 0.5)
            for s in p1.segments:
                self.assertAlmostEqual(math.hypot(s[0], s[1]), 1.0, delta=1e-5)
            self.assertTrue(p1.fits_bounds(tol=0.0))
        t0, t1 = a0.track_paths[0], a1.track_paths[0]
        grown = t1.length - t0.length
        self.assertGreater(grown, 1.0)
        # Events keep their place on the stretched polyline: the same fraction of the same segment.
        def locate(path, s):
            acc = 0.0
            for k, seg in enumerate(path.segments):
                if s <= acc + seg[3]:
                    return k, (s - acc) / seg[3]
                acc += seg[3]

        def distance(path, k, t):
            return sum(seg[3] for seg in path.segments[:k]) + t * path.segments[k][3]

        for e0, e1 in zip(t0.events, t1.events):
            self.assertAlmostEqual(e1[2], distance(t1, *locate(t0, e0[2])), delta=0.05)
        # The remaining distance reached zero at the end of this path: it grows by what the path grew.
        self.assertAlmostEqual(t1.header[3] - t0.header[3], grown, delta=0.5)
        # The checkpoint sits in the rigid middle: it moves by what the path grew before it.
        self.assertLess(abs(t1.events[0][2] - t0.events[0][2]), grown)
        # Session point 1 sits under the grab, the start grid is outside it.
        start0, session0 = a0.regions
        start1, session1 = a1.regions
        self.assertEqual(start0.position, start1.position)
        self.assertAlmostEqual(session1.position[0] - session0.position[0], 300.0, delta=1e-3)
        self.assertEqual(report.points, 1)
        # Visibility curtain: still planar, box and sphere around it.
        r, old, new = pair(11)
        corners = [struct.unpack_from('<3f', new, 0x10 + 16 * k) for k in range(4)]
        n = struct.unpack_from('<4f', new, 0x50)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in n[:3])), 1.0, delta=1e-5)
        for c in corners:
            self.assertLess(abs(sum(n[k] * c[k] for k in range(3)) + n[3]), 30.0)
            self.assertLessEqual(math.dist(c, struct.unpack_from('<3f', new, 0)),
                                 struct.unpack_from('<f', new, 12)[0] + 1e-3)
        # Camera trigger: the volume moved with the ground.
        r, old, new = pair(17)
        self.assertAlmostEqual(struct.unpack_from('<f', new, 28)[0] - struct.unpack_from('<f', old, 28)[0], 300.0,
                               delta=1e-3)
        # Progress meter: gates under the grab moved; distances past the bend grew like the course.
        r, old, new = pair(21)
        n_gates, seg_off, n_marks, mark_off = struct.unpack_from('<4I', old, 0)
        g_old = [struct.unpack_from('<5f', old, seg_off + 20 * k) for k in range(n_gates)]
        g_new = [struct.unpack_from('<5f', new, seg_off + 20 * k) for k in range(n_gates)]
        self.assertEqual(g_old[0], g_new[0])
        self.assertAlmostEqual(g_new[3][2] - g_old[3][2], 300.0, delta=1e-3)       # (1500, 2200)
        self.assertAlmostEqual(g_new[-1][4] - g_old[-1][4], report.length_change, delta=1.0)
        total0, total1 = struct.unpack_from('<f', old, 0x10)[0], struct.unpack_from('<f', new, 0x10)[0]
        self.assertAlmostEqual(total1 - total0, report.length_change, delta=1.0)
        # The finish marker (4400 cm, at y = 4500) moves by how much the course grew before it.
        stretch = warp.Stretch(aip.course_line(a0).points, g)
        finish0 = struct.unpack_from('<f', old, mark_off + 16 + 4)[0]
        finish1 = struct.unpack_from('<f', new, mark_off + 16 + 4)[0]
        self.assertAlmostEqual(finish1 - finish0, stretch.at(1500.0, 4500.0) - stretch.at(1500.0, 100.0), delta=0.5)
        self.assertEqual(struct.unpack_from('<f', old, mark_off + 4), struct.unpack_from('<f', new, mark_off + 4))
        self.assertEqual(report.untouched, ['zvukové spúšťače'])
        # Records keep their sizes and places.
        self.assertEqual([(r.kind, r.offset, r.size) for r in recs],
                         [(r.kind, r.offset, r.size) for r in ssb.parse_records(after)])

    def test_course_line_still_chains(self):
        w = self.world()
        line0 = aip.course_line(mapedit.course_aip(w, 'AAA'))
        report = warp.warp_edit(w, 'AAA', self.grab())
        line1 = aip.course_line(mapedit.course_aip(w, 'AAA'))
        self.assertEqual(line0.parts, line1.parts)
        self.assertGreater(line1.length, line0.length)
        self.assertGreater(report.length_change, 0)

    def test_turn_turns_objects_and_directions(self):
        w = self.world()
        warp.warp_edit(w, 'AAA', self.grab(move=(0.0, 0.0, 0.0), turn=20.0, edge=3000.0))
        r = [r for r in w.stream.records(1) if r.kind == 3][0]
        m = struct.unpack_from('<16f', w.stream.current(1), r.offset + instances.MATRIX)
        self.assertAlmostEqual(math.degrees(math.atan2(m[1], m[0])), 20.0, delta=0.01)
        session = mapedit.course_aip(w, 'AAA').regions[1]
        self.assertAlmostEqual(math.degrees(math.atan2(session.direction[1], session.direction[0])), 110.0,
                               delta=0.01)

    def test_refuses_folding_and_squeezing(self):
        w = self.world()
        with self.assertRaises(mapedit.EditRefused):
            warp.warp_edit(w, 'AAA', self.grab(move=(1500.0, 0.0, 0.0), edge=1000.0), force=True)
        with self.assertRaises(mapedit.EditRefused):
            warp.warp_edit(w, 'AAA', self.grab(move=(700.0, 0.0, 0.0), edge=1500.0))
        self.assertEqual(w.stream.changed_chunks(), [])
        report = warp.warp_edit(w, 'AAA', self.grab(move=(700.0, 0.0, 0.0), edge=1500.0), force=True)
        self.assertLess(report.stretch, warp.MIN_STRETCH)

    def test_bends_too_sharp_for_bilinear_are_refitted(self):
        # A full refit follows a sharp turn at least as well as the bilinear move...
        g = warp.Grab((1500.0, 2500.0), (0.0, 0.0, 0.0), 300.0, 2000.0, turn=35.0)
        errors = {}
        for mode in ('bilinear', 'cubic'):
            w = self.world()
            errors[mode] = warp.apply_warp(w, 'AAA', g, sideways=mode).shape_error
        self.assertLessEqual(errors['cubic'], errors['bilinear'] + 1e-6)
        # ...and warp_edit falls back to it when the bilinear move is off by too much.
        real = warp.apply_warp

        def strict(world, code, grab, sideways='bilinear'):
            report = real(world, code, grab, sideways)
            if sideways == 'bilinear':
                report.shape_error = 1e9
            return report
        warp.apply_warp = strict
        try:
            w = self.world()
            report = warp.warp_edit(w, 'AAA', self.grab())
        finally:
            warp.apply_warp = real
        self.assertEqual(report.sideways, 'cubic')
        self.assertTrue(w.stream.changed_chunks())

    def test_refuses_off_the_terrain(self):
        w = self.world()
        with self.assertRaises(mapedit.EditRefused):
            warp.warp_edit(w, 'AAA', self.grab(origin=(90000.0, 90000.0)))
        self.assertEqual(w.stream.changed_chunks(), [])

    def test_saved_world_reads_back(self):
        w = self.world()
        warp.warp_edit(w, 'AAA', self.grab())
        edited = bytes(w.stream.current(1))
        out = os.path.join(self.tmp, 'warped.BIG')
        w.save(out)
        self.assertEqual(bytes(World(out).stream.current(1)), edited)


if __name__ == '__main__':
    unittest.main()
