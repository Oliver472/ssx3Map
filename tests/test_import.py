"""Designed maps: glTF files, the course-frame surface import, the world growing on save."""
import contextlib
import io
import json
import math
import os
import struct
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from ssx3map import aip, gltf, grow, iso9660, mapdesign, mapedit, rebuild, terrain
from ssx3map.cli import main
from ssx3map.editor import server
from ssx3map.world import BIG_PATH, World

from . import fixtures


def bumpy(x, y):
    """A test surface (metres): 10 % fall, a 1.5 m bump every 6 m, a bowl across."""
    return -0.1 * y + 1.5 * math.sin(math.pi * y / 6.0) ** 2 + 0.004 * x * x


class GltfTest(unittest.TestCase):
    def test_write_and_read_back(self):
        path = os.path.join(tempfile.mkdtemp(), 'm.glb')
        pts = [(0.0, 0.0, 1.0), (10.0, 0.0, 2.0), (0.0, 20.0, 3.0), (10.0, 20.0, 5.0)]
        gltf.write_glb(path, [dict(name='ground', points=pts, triangles=[(0, 1, 3), (0, 3, 2)]),
                              dict(name='line', points=pts[:2], lines=[(0, 1)])],
                       [dict(name='Start gate', at=(1.0, 2.0, 3.0))])
        (name, tris), = gltf.read_triangles(path)
        self.assertEqual(name, 'ground')
        self.assertEqual([tuple(round(v, 5) for v in p) for p in tris[0]], [pts[0], pts[1], pts[3]])
        self.assertEqual(gltf.read_markers(path)['Start gate'], (1.0, 2.0, 3.0))
        surf = gltf.load_surface(path)
        self.assertAlmostEqual(surf(5.0, 0.0), 1.5)
        self.assertAlmostEqual(surf(10.0, 20.0), 5.0)
        self.assertIsNone(surf(11.0, 5.0))

    def test_node_transforms_and_highest_surface(self):
        # A unit triangle, twice: one moved up 2 m and scaled x10 by its node (glTF is Y up).
        tri = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, -1.0]
        blob = struct.pack('<9f', *tri)
        doc = dict(asset=dict(version='2.0'), scene=0, scenes=[dict(nodes=[0, 1])],
                   nodes=[dict(mesh=0, scale=[10, 10, 10]), dict(mesh=0, translation=[0, 2, 0], scale=[10, 10, 10])],
                   meshes=[dict(primitives=[dict(attributes=dict(POSITION=0))])],
                   accessors=[dict(bufferView=0, componentType=5126, count=3, type='VEC3')],
                   bufferViews=[dict(buffer=0, byteLength=len(blob))],
                   buffers=[dict(byteLength=len(blob), uri='data:application/octet-stream;base64,'
                                 + __import__('base64').b64encode(blob).decode())])
        path = os.path.join(tempfile.mkdtemp(), 'm.gltf')
        with open(path, 'w') as f:
            json.dump(doc, f)
        surf = gltf.load_surface(path)
        self.assertEqual(surf.count, 2)
        self.assertAlmostEqual(surf(2.0, 3.0), 2.0)          # (x, -z, y): the triangle spans x, y 0..10
        self.assertIsNone(surf(8.0, 8.0))


class DesignTest(unittest.TestCase):
    def test_olivers_peak(self):
        d = mapdesign.OliversPeak()
        self.assertAlmostEqual(d(0.0, 0.0), 0.0, places=6)
        self.assertTrue(700 < -d(0.0, d.length) < 950)
        steps = [abs(d(0.0, y / 2 + 0.5) - d(0.0, y / 2)) for y in range(0, 2 * int(d.length))]
        self.assertLess(max(steps), 1.0)                       # no cliffs, even behind the lips
        self.assertGreater(d(50.0, 1400.0) - d(0.0, 1400.0), 8.0)     # halfpipe walls
        self.assertGreater(d(20.0, 2200.0) - d(0.0, 2200.0), 11.0)    # canyon walls
        names = [m['name'] for m in d.markers()]
        self.assertIn('Kicker 5 7 m', names)
        self.assertEqual(len(d.section_spans()), len(mapdesign.SECTIONS))
        short = mapdesign.OliversPeak(2465.0)
        self.assertAlmostEqual(short.features[1][1], d.features[1][1] / 2)

    def test_design_command_writes_a_glb(self):
        out = os.path.join(tempfile.mkdtemp(), 'peak.glb')
        with contextlib.redirect_stdout(io.StringIO()):
            main(['design', '--length', '300', '-o', out])
        names = [n for n, _ in gltf.read_triangles(out)]
        self.assertEqual(names[:2], ['Start', 'Drop-in'])
        surf = gltf.load_surface(out)
        d = mapdesign.OliversPeak(300.0)
        for y in (10.0, 75.3, 151.0):
            self.assertAlmostEqual(surf(3.0, y), d(3.0, y), delta=0.15)


class SurfaceImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        big, _ = fixtures.build_course_world()
        cls.big = os.path.join(cls.tmp, 'BAM.BIG')
        with open(cls.big, 'wb') as f:
            f.write(big)
        cls.iso = os.path.join(cls.tmp, 'game.iso')
        with open(cls.iso, 'wb') as f:
            f.write(fixtures.build_iso(big))

    def build(self, surface, extra=0.4, cols=4):
        w = World(self.big)
        r = rebuild.build_from_surface(w, 'AAA', surface, width=16.0, cols=cols, extra=extra, lane=3.0)
        return w, r

    def test_the_ground_follows_the_surface(self):
        w, r = self.build(lambda x, y: -0.1 * y + 0.01 * x * x, extra=0.0)
        self.assertEqual(r.added, 0)
        line = aip.course_line(mapedit.course_aip(w, 'AAA'))
        index = mapedit.course_index(w, 'AAA')
        start = line.at(0.0)[0]
        for m in (5.0, 20.0, 35.0):
            p, _ = line.at(m * 100.0)
            # the race line is on the new ground, which falls 10 % from the start
            self.assertAlmostEqual(index.z(p[0], p[1]), p[2], delta=30.0)
            self.assertAlmostEqual(index.z(p[0], p[1]) - index.z(start[0], start[1]), -10.0 * m, delta=40.0)

    def test_a_detailed_surface_adds_patches_and_the_world_grows(self):
        w, r = self.build(bumpy)
        self.assertGreater(r.added, 0)
        self.assertEqual(r.rows * r.cols, r.slots + r.added)
        recs = [x for x in w.stream.records(1) if x.kind == 1]
        self.assertEqual(len(recs), 60 + r.added)
        self.assertEqual([x.rid for x in recs[60:]], list(range(60, 60 + r.added)))
        out = os.path.join(self.tmp, 'bumpy.BIG')
        report = w.save(out)
        self.assertTrue(all(e['method'] == 'grow' for e in report))
        w2 = World(out)
        self.assertEqual(w2.stream.chunk_original(1), bytes(w.stream.current(1)))
        tables = grow.Tables(w2._member('.sdb'))
        self.assertEqual(tables.kind_counts(1)[1], 60 + r.added)
        self.assertEqual(tables.record_count(1), len(w2.stream.records(1)))
        self.assertEqual(tables.shorts(w2.location('AAA'))[1], 60 + r.added)
        for c, ch in enumerate(w2.stream.chunks):
            self.assertEqual(tables.offset(c), ch.blocks[0].offset)
        # bumps 6 m apart need short rows
        self.assertLessEqual(r.fine, 300.0 + 1e-6)

    def test_too_many_extra_patches_are_refused(self):
        with self.assertRaises(mapedit.EditRefused):
            self.build(bumpy, extra=0.6)

    def test_import_command_with_a_glb(self):
        mesh = os.path.join(self.tmp, 'mine.glb')
        xs = [-10.0 + i for i in range(21)]
        ys = [-10.0 + 0.5 * j for j in range(161)]
        pts = [(x, y, bumpy(x, y)) for y in ys for x in xs]
        tris = []
        for j in range(len(ys) - 1):
            for i in range(len(xs) - 1):
                p = j * len(xs) + i
                tris += [(p, p + 1, p + len(xs) + 1), (p, p + len(xs) + 1, p + len(xs))]
        gltf.write_glb(mesh, [dict(name='mine', points=pts, triangles=tris)])
        out = os.path.join(self.tmp, 'mine.iso')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            main(['import', self.iso, mesh, '--location', 'AAA', '--width', '16', '-o', out])
        self.assertIn('the world data grew', buf.getvalue())
        w = World(out)
        self.assertGreater(sum(1 for x in w.stream.records(1) if x.kind == 1), 60)
        self.assertGreater(iso9660.find(out, BIG_PATH).lba, iso9660.find(self.iso, BIG_PATH).lba)

    def test_store_file(self):
        _, big = iso9660.read_file(self.iso, BIG_PATH)
        same = os.path.join(self.tmp, 'same.iso')
        self.assertEqual(iso9660.store_file(self.iso, BIG_PATH, big, same), 'in place')
        moved = os.path.join(self.tmp, 'moved.iso')
        self.assertEqual(iso9660.store_file(self.iso, BIG_PATH, big + bytes(5000), moved), 'moved')
        longer = os.path.join(self.tmp, 'longer.iso')
        self.assertEqual(iso9660.store_file(moved, BIG_PATH, big + bytes(9000), longer), 'extended')
        self.assertEqual(os.path.getsize(longer), os.path.getsize(moved) + 2048 * 2)
        self.assertEqual(iso9660.read_file(longer, BIG_PATH)[1], big + bytes(9000))


class EditorDesignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        big, _ = fixtures.build_course_world()
        cls.iso = os.path.join(cls.tmp.name, 'game.iso')
        with open(cls.iso, 'wb') as f:
            f.write(fixtures.build_iso(big))
        server.Handler.session = server.Session(World(cls.iso))
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read())

    def test_design_undo_and_save(self):
        res = self.call('/api/design', dict(code='AAA', name='olivers_peak'))
        self.assertIn("Oliver's Peak", res['message'])
        course = self.call('/api/course?code=AAA')
        self.assertIn('Kicker 5 7 m', [m['t'] for m in course['marks']])
        self.assertGreater(course['changed'], 0)
        out = os.path.join(self.tmp.name, 'peak.iso')
        res = self.call('/api/save', dict(output=out))
        self.assertIn('saved', res['message'])
        World(out)
        self.call('/api/undo', {})
        self.assertEqual(self.call('/api/course?code=AAA')['marks'], [])


if __name__ == '__main__':
    unittest.main()
