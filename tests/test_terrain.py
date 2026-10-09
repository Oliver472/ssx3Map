import contextlib
import io
import math
import os
import struct
import tempfile
import unittest

from ssx3map import aip, instances, mapedit, terrain
from ssx3map.cli import main
from ssx3map.world import World

from tests import fixtures


class TerrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.big, _ = fixtures.build_course_world()
        self.path = os.path.join(self.tmp.name, 'BAM.BIG')
        with open(self.path, 'wb') as f:
            f.write(self.big)

    def tearDown(self):
        self.tmp.cleanup()

    def all_patches(self, w):
        return mapedit.patches(w, mapedit.course_locations(w, 'AAA'))

    def test_fixture_surface(self):
        w = World(self.path)
        _, _, p = self.all_patches(w)[7]
        for u, v in ((0, 0), (0.5, 0.25), (1, 1)):
            x, y, z = p.point(u, v)
            self.assertAlmostEqual(z, fixtures.height(x, y), delta=8.0)     # the fixture has +-3 cm noise
        self.assertAlmostEqual(terrain.surface_z([q for _, _, q in self.all_patches(w)], 1234.0, 2345.0),
                               fixtures.height(1234.0, 2345.0), delta=8.0)

    def test_bump_keeps_patches_consistent(self):
        w = World(self.path)
        before = {(c, r.rid): p for c, r, p in self.all_patches(w)}
        frame = terrain.Frame(1500.0, 2500.0, (0.0, 1.0))
        dz = terrain.bump(frame, 400.0, 1200.0)
        report = mapedit.apply_field(w, 'AAA', frame, dz)
        self.assertGreater(report.patches, 10)
        self.assertAlmostEqual(report.max_dz, 400.0, delta=40.0)
        after = {(c, r.rid): p for c, r, p in self.all_patches(w)}
        for key, new in after.items():
            old = before[key]
            # X/Y coefficients untouched, Z interpolates old + dz on the 4x4 grid.
            for j in range(4):
                for i in range(4):
                    self.assertEqual(new.c[j][i][:2], old.c[j][i][:2])
            for u in terrain.GRID:
                for v in terrain.GRID:
                    o, n = old.point(u, v), new.point(u, v)
                    self.assertAlmostEqual(n[2], o[2] + dz(*o), delta=0.5)
            # Stored corners, box and sphere agree with the new surface.
            corners = new.corners()
            for stored in new.stored_corners:
                self.assertTrue(any(math.dist(stored, q) < 0.1 for q in corners.values()))
            lo, hi = new.bbox
            sphere = struct.unpack_from('<4f', new.data, terrain.SPHERE)
            for u in range(9):
                for v in range(9):
                    q = new.point(u / 8, v / 8)
                    self.assertTrue(all(lo[k] <= q[k] <= hi[k] for k in range(3)))
                    self.assertLessEqual(math.dist(q, sphere[:3]), sphere[3])
        # Neighbouring patches still meet along their shared edge.
        grid = {}
        for (c, rid), p in after.items():
            x0, y0, _ = p.point(0, 0)
            grid[(round(x0), round(y0))] = p
        gaps = 0.0
        for (x0, y0), p in grid.items():
            right = grid.get((x0 + 500, y0))
            if right:
                for t in (0.1, 0.37, 0.5, 0.81):
                    gaps = max(gaps, math.dist(p.point(1, t), right.point(0, t)))
            up = grid.get((x0, y0 + 500))
            if up:
                for t in (0.1, 0.37, 0.5, 0.81):
                    gaps = max(gaps, math.dist(p.point(t, 1), up.point(t, 0)))
        self.assertLess(gaps, 0.5)

    def test_kicker_carries_objects_and_points(self):
        w = World(self.path)
        course = mapedit.course_aip(w, 'AAA')
        start_z = course.regions[1].position[2]
        frame = terrain.Frame(1500.0, 2600.0, (0.0, 1.0))
        dz = terrain.kicker(frame, 300.0, 1500.0, 1000.0, 500.0, 400.0)
        (c, rec, inst, _), = mapedit.find_objects(w, 'AAA', frame=terrain.Frame(1500.0, 2500.0), radius=10.0)
        expected = dz(*inst.centre[:2], inst.bbox[0][2])
        self.assertGreater(expected, 50)
        report = mapedit.apply_field(w, 'AAA', frame, dz)
        self.assertEqual(report.objects, 1)
        self.assertEqual(report.points, 1)          # the session point at y=25 m, not the start
        moved = instances.Instance(w.record_bytes(c, rec))
        self.assertAlmostEqual(moved.translation[2] - inst.translation[2], expected, places=2)
        self.assertAlmostEqual(moved.bbox[0][2] - inst.bbox[0][2], expected, places=2)
        course2 = mapedit.course_aip(w, 'AAA')
        self.assertAlmostEqual(course2.regions[1].position[2] - start_z,
                               dz(*course.regions[1].position), places=2)
        out = os.path.join(self.tmp.name, 'k.BIG')
        w.save(out)
        w2 = World(out)
        self.assertEqual([r.rid for _, r, _ in self.all_patches(w2)], [r.rid for _, r, _ in self.all_patches(w)])

    def test_placement_along_path(self):
        w = World(self.path)
        pl = mapedit.place(w, 'AAA', along=20.0)
        self.assertAlmostEqual(pl.frame.x, 1500.0, delta=1.0)
        # 20 m along the (sloped) 3D line: a little less than 20 m in plan view.
        self.assertTrue(100.0 + 1900.0 < pl.frame.y < 100.0 + 2000.0, pl.frame.y)
        self.assertAlmostEqual(pl.frame.fy, 1.0, places=2)
        self.assertAlmostEqual(pl.z, fixtures.height(pl.frame.x, pl.frame.y), delta=8.0)
        side = mapedit.place(w, 'AAA', along=20.0, side=5.0)
        self.assertAlmostEqual(side.frame.x, 2000.0, delta=1.0)     # Z up: right of +y is +x

    def test_flatten(self):
        w = World(self.path)
        frame = terrain.Frame(1500.0, 2500.0)
        target = fixtures.height(1500.0, 2500.0)
        dz = terrain.flatten_to(frame, target, 600.0, 300.0)
        mapedit.apply_field(w, 'AAA', frame, dz)
        pts = [p for _, _, p in self.all_patches(w)]
        for x, y in ((1500.0, 2500.0), (1800.0, 2300.0), (1200.0, 2800.0)):
            self.assertAlmostEqual(terrain.surface_z(pts, x, y), target, delta=15.0)

    def test_cli_map_terrain_objects(self):
        out = io.StringIO()
        svg = os.path.join(self.tmp.name, 'aaa.svg')
        iso = os.path.join(self.tmp.name, 'game.iso')
        with open(iso, 'wb') as f:
            f.write(fixtures.build_iso(self.big))
        mod = os.path.join(self.tmp.name, 'mod.iso')
        mod2 = os.path.join(self.tmp.name, 'mod2.iso')
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(['map', iso, '--location', 'AAA', '-o', svg])
            main(['terrain', iso, '--location', 'AAA', '--along', '25', '--shape', 'kicker', '--height', '3',
                  '--length', '12', '--width', '8', '-o', mod])
            main(['objects', mod, '--location', 'AAA', '--at', '2', '3', '--radius', '5'])
            main(['objects', mod, '--location', 'AAA', '--at', '2', '3', '--radius', '5', '--remove', '-o', mod2])
        text = out.getvalue()
        with open(svg, encoding='utf-8') as f:
            body = f.read()
        self.assertTrue(body.startswith('<svg') and body.count('<polygon') == 84)
        self.assertIn('20 m', body)
        self.assertRegex(text, r'terén: \d+ plátov')
        self.assertIn('odstránené: 1', text)
        w = World(mod2)
        (c, rec, inst, _), = mapedit.find_objects(w, 'AAA', frame=terrain.Frame(200.0, 300.0), radius=1e6,
                                                  name='AAA#2')
        self.assertLess(inst.translation[2], -90000)
        self.assertEqual(os.path.getsize(mod2), os.path.getsize(iso))


if __name__ == '__main__':
    unittest.main()


class CourseLineTest(unittest.TestCase):
    def aip_bytes(self, paths, regions):
        out = struct.pack('<I', 1) + struct.pack('<I', 0) + struct.pack('<I', len(paths))
        for pts in paths:
            segs = []
            for a, b in zip(pts, pts[1:]):
                d = [b[k] - a[k] for k in range(3)]
                n = math.sqrt(sum(v * v for v in d))
                segs.append((d[0] / n, d[1] / n, d[2] / n, n))
            lo = [min(p[k] for p in pts) for k in range(3)]
            hi = [max(p[k] for p in pts) for k in range(3)]
            out += struct.pack('<IIIf', 0, 0, 0, 0.0) + struct.pack('<II', len(segs), 0)
            out += struct.pack('<9f', *pts[0], *lo, *hi) + b''.join(struct.pack('<4f', *s) for s in segs)
        out += struct.pack('<I', 0) + struct.pack('<I', len(regions))
        for slot, kind, p in regions:
            out += struct.pack('<II6fII', slot, kind, *p, 0.0, 1.0, 0.0, 0, 0)
        return out

    def test_chains_sections_from_the_start(self):
        # Three sections stored out of order; the start grid sits 10 m into section "a".
        a = [(0.0, 0.0, 0.0), (0.0, 30000.0, -9000.0)]
        b = [(0.0, 30100.0, -9000.0), (20000.0, 50000.0, -15000.0)]     # 1 m gap after a
        c = [(20000.0, 50000.0, -15000.0), (20000.0, 90000.0, -27000.0)]
        stray = [(90000.0, 0.0, 0.0), (95000.0, 0.0, 0.0)]               # an unrelated path
        data = self.aip_bytes([c, stray, a, b], [(0, 0, (0.0, 1000.0, -300.0)), (3, 1, (0.0, 20000.0, 0.0))])
        record = aip.decode(data)
        line = aip.course_line(record)
        self.assertEqual(line.parts, [2, 3, 0])
        self.assertAlmostEqual(line.start_offset, math.dist(a[0], (0.0, 1000.0, -300.0)), delta=5)
        p, h = line.at(0.0)
        self.assertAlmostEqual(p[1], 1000.0, delta=1)
        expected = math.dist(*a) + math.dist(a[1], b[0]) + math.dist(*b) + math.dist(*c) - line.start_offset
        self.assertAlmostEqual(line.length, expected, delta=1)
        p, h = line.at(math.dist(*a) - line.start_offset + math.dist(a[1], b[0]) + 1.0)
        self.assertAlmostEqual(p[0], b[0][0], delta=5)
        self.assertGreater(h[0], 0.5)               # heading turns towards +x in section b
        d, _ = line.nearest(0.0, 20000.0)
        self.assertAlmostEqual(d, math.dist((0, 0, 0), (0, 20000.0, -6000.0)) - line.start_offset, delta=5)


class RailTest(unittest.TestCase):
    def test_rigid_move(self):
        rec = bytearray(48 + 144 * 2)
        struct.pack_into('<3f3f', rec, 4, 0, 0, 100, 10, 10, 200)
        for k in range(2):
            base = 48 + 144 * k
            struct.pack_into('<4f', rec, base + 0x40, 5.0 * k, 0.0, 150.0, 1.0)
            struct.pack_into('<3f3f', rec, base + 0x6C, 0, 0, 120, 10, 10, 180)
        self.assertTrue(mapedit._rail_rigid(rec, 0, len(rec)))
        mapedit.translate_rail(rec, 0, len(rec), 50.0)
        self.assertEqual(struct.unpack_from('<3f3f', rec, 4), (0, 0, 150, 10, 10, 250))
        for k in range(2):
            base = 48 + 144 * k
            self.assertEqual(struct.unpack_from('<4f', rec, base + 0x40), (5.0 * k, 0.0, 200.0, 1.0))
            self.assertEqual(struct.unpack_from('<3f3f', rec, base + 0x6C), (0, 0, 170, 10, 10, 230))
        struct.pack_into('<f', rec, 48 + 0x50, 1.0)
        self.assertFalse(mapedit._rail_rigid(rec, 0, len(rec)))
