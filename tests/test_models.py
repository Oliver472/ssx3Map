import struct
import unittest

from ssx3map import models, texture

from tests import fixtures


class ModelTest(unittest.TestCase):
    def test_decode_fixture_mdr(self):
        meshes = models.decode(fixtures.mdr_model(material=(3, 9)))
        self.assertEqual(len(meshes), 1)
        m = meshes[0]
        self.assertEqual(m['material'], (3, 9))
        self.assertEqual(len(m['positions']), 8 * 3)
        self.assertEqual(len(m['indices']), 4 * 3)              # two strips of 4 -> 4 triangles
        xs = m['positions'][0::3]
        zs = m['positions'][2::3]
        self.assertAlmostEqual(min(xs), -100, delta=0.1)       # +-0.5 * scale 200
        self.assertAlmostEqual(max(zs), 200, delta=0.1)
        self.assertTrue(all(0 <= i < 8 for i in m['indices']))
        # Strip winding alternates: the second triangle is reversed.
        self.assertEqual(m['indices'][:6], [0, 1, 2, 3, 2, 1])

    def test_rejects_garbage(self):
        self.assertEqual(models.decode(bytes(64)), [])          # no nodes: an empty model
        with self.assertRaises(models.ModelError):
            models.decode(bytes(8))                              # header out of range
        bad = bytearray(fixtures.mdr_model())
        struct.pack_into('<I', bad, 4, 10 ** 6)                 # absurd node count
        with self.assertRaises(models.ModelError):
            models.decode(bytes(bad))

    def test_instance_placement_and_colours(self):
        rec = fixtures.placed_instance(1000.0, 2000.0, 0, 7, size=400.0)[8:]
        matrix, model, scale = models.instance_placement(rec)
        self.assertEqual(model, (0, 5))
        self.assertEqual(scale, 2.0)
        for got, want in zip(matrix[12:15], (1000.0, 2000.0, fixtures.height(1000.0, 2000.0))):
            self.assertAlmostEqual(got, want, places=3)
        colours = models.instance_colors(rec)
        self.assertEqual(len(colours), 8)
        self.assertEqual(colours[0] & 31, 16)                   # unity
        self.assertTrue(colours[0] & 0x8000)

    def test_light_page_keeps_raw_alpha(self):
        w, h, rgba = texture.decode_rgba(fixtures.light_page(), raw_alpha=True)
        self.assertEqual(rgba[3], 160)
        w, h, rgba = texture.decode_rgba(fixtures.light_page())
        self.assertEqual(rgba[3], 160)                          # > 128 anyway: never rescaled


if __name__ == '__main__':
    unittest.main()
