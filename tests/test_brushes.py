import math
import os
import struct
import tempfile
import unittest

from ssx3map import instances, mapedit, terrain
from ssx3map.world import World

from tests import fixtures


class BrushTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        big, _ = fixtures.build_course_world()
        cls.path = os.path.join(cls.tmp.name, 'BAM.BIG')
        with open(cls.path, 'wb') as f:
            f.write(big)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def world(self):
        return World(self.path)

    def z(self, w, x, y):
        return mapedit.course_index(w, 'AAA').z(x, y)

    def test_raise_and_lower_along_a_line(self):
        w = self.world()
        before = {(x, y): self.z(w, x, y) for x in (1500.0, 2700.0) for y in (1800.0, 2500.0, 3200.0)}
        line = [(1500.0, 1800.0), (1500.0, 3200.0)]
        report = mapedit.stroke_edit(w, 'AAA', line, 'raise', radius=10.0, height=2.0)
        self.assertGreater(report.patches, 4)
        for y in (1800.0, 2500.0, 3200.0):
            self.assertAlmostEqual(self.z(w, 1500.0, y) - before[1500.0, y], 200.0, delta=25.0)
            self.assertAlmostEqual(self.z(w, 2700.0, y) - before[2700.0, y], 0.0, delta=1.0)   # 12 m away
        mapedit.stroke_edit(w, 'AAA', line, 'lower', radius=10.0, height=2.0)
        for y in (1800.0, 2500.0, 3200.0):
            self.assertAlmostEqual(self.z(w, 1500.0, y), before[1500.0, y], delta=25.0)

    def test_flatten_levels_to_the_first_point(self):
        w = self.world()
        line = [(1500.0, 2000.0), (1500.0, 3000.0)]
        target = self.z(w, *line[0])
        mapedit.stroke_edit(w, 'AAA', line, 'flatten', radius=12.0)
        for y in (2200.0, 2500.0, 2800.0):
            self.assertAlmostEqual(self.z(w, 1500.0, y), target, delta=20.0)

    def test_smooth_flattens_a_bump(self):
        w = self.world()
        frame = terrain.Frame(1500.0, 2500.0)
        mapedit.apply_field(w, 'AAA', frame, terrain.bump(frame, 400.0, 1000.0))
        peak = self.z(w, 1500.0, 2500.0) - fixtures.height(1500.0, 2500.0)
        for _ in range(3):
            mapedit.stroke_edit(w, 'AAA', [(1500.0, 2500.0)], 'smooth', radius=16.0)
        after = self.z(w, 1500.0, 2500.0) - fixtures.height(1500.0, 2500.0)
        self.assertGreater(peak, 300.0)
        self.assertLess(after, peak * 0.85)

    def test_tiny_brush_is_refused_and_rolled_back(self):
        w = self.world()
        before = bytes(w.stream.current(1))
        with self.assertRaises(mapedit.EditRefused):
            mapedit.stroke_edit(w, 'AAA', [(1730.0, 2610.0), (1760.0, 2640.0)], 'raise', radius=1.5, height=3.0)
        self.assertEqual(bytes(w.stream.current(1)), before)

    def test_place_object_on_the_terrain(self):
        w = self.world()
        (c, rec, inst, _), = mapedit.find_objects(w, 'AAA', name='AAA#2')
        mapedit.place_object(w, 'AAA', c, rec.offset, 2200.0, 4100.0)
        moved = instances.Instance(w.record_bytes(c, rec))
        self.assertAlmostEqual(moved.centre[0], 2200.0, places=2)
        self.assertAlmostEqual(moved.centre[1], 4100.0, places=2)
        self.assertAlmostEqual(moved.bbox[0][2], self.z(w, 2200.0, 4100.0), places=1)
        with self.assertRaises(mapedit.EditRefused):
            mapedit.place_object(w, 'AAA', c, rec.offset, -90000.0, 4100.0)

    def test_rotation_turns_matrix_and_keeps_the_box(self):
        w = self.world()
        (c, rec, inst, _), = mapedit.find_objects(w, 'AAA', name='AAA#1')
        orig = w.record_bytes(c, rec)
        mapedit.rotate_object(w, c, rec.offset, 90.0)
        data = w.record_bytes(c, rec)
        row0 = struct.unpack_from('<3f', data, instances.MATRIX)
        self.assertAlmostEqual(row0[0], 0.0, places=5)
        self.assertAlmostEqual(row0[1], 1.0, places=5)
        for _ in range(7):
            mapedit.rotate_object(w, c, rec.offset, 45.0)          # 90 + 315 = 405 = 45 degrees
        for _ in range(7):
            mapedit.rotate_object(w, c, rec.offset, -45.0)
        mapedit.rotate_object(w, c, rec.offset, -90.0)             # back to the start
        data = w.record_bytes(c, rec)
        for a, b in zip(struct.unpack_from('<28f', data, 0x10), struct.unpack_from('<28f', orig, 0x10)):
            self.assertAlmostEqual(a, b, places=2)


if __name__ == '__main__':
    unittest.main()
