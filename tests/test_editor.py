import json
import os
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ssx3map.editor import server
from ssx3map.world import World

from tests import fixtures


class EditorApiTest(unittest.TestCase):
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
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(req) as res:
                raw = res.read()
                return res.status, (json.loads(raw) if res.headers['Content-Type'].startswith('application/json')
                                    else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        status, page = self.call('/')
        self.assertEqual(status, 200)
        self.assertIn(b'importmap', page)
        status, info = self.call('/api/info')
        self.assertEqual(status, 200)
        self.assertIn('AAA', [c['code'] for c in info['courses']])
        status, course = self.call('/api/course?code=AAA')
        self.assertEqual(len(course['patches']), 84)
        self.assertEqual(len(course['patches'][0]['c']), 48)
        self.assertEqual(len(course['objects']), 2)
        self.assertIsNotNone(course['line'])
        status, png = self.call('/api/texture?id=7&code=AAA')
        self.assertEqual(status, 200)
        self.assertTrue(png.startswith(b'\x89PNG'))
        status, png = self.call('/api/lightpage?id=0&code=AAA')
        self.assertEqual(status, 200)
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertEqual(course['sky'], 'ASKY')
        self.assertEqual(course['fog']['far_cm'], 10000.0)
        self.assertEqual(course['patches'][0]['lp'], 0)
        self.assertEqual(course['objects'][0]['mod'], '0:5')
        self.assertEqual(len(course['objects'][0]['m']), 16)
        status, pack = self.call('/api/models?code=AAA')
        self.assertEqual(status, 200)
        self.assertEqual(pack[:4], b'SSXM')
        n, = struct.unpack_from('<I', pack, 4)
        head = json.loads(pack[8:8 + n])
        self.assertEqual(head['errors'], 0)
        self.assertEqual(head['models']['0:5'][0]['tex'], 7)
        self.assertEqual(len(pack), 8 + n + head['vertices'] * 20 + head['indices'] * 4 + head['colorCount'] * 4)
        self.assertEqual(len(head['colors']), 2)

        before = {p['k']: p['c'] for p in course['patches']}
        status, res = self.call('/api/terrain', dict(code='AAA', x=1500, y=2500, shape='bump', height=3))
        self.assertEqual(status, 200, res)
        self.assertIn('plátov', res['message'])
        _, course2 = self.call('/api/course?code=AAA')
        changed = [p['k'] for p in course2['patches'] if p['c'] != before[p['k']]]
        self.assertTrue(changed)
        self.assertEqual(len(course2['undo']), 1)

        # A shape the 5 m patches cannot hold is refused and changes nothing.
        status, res = self.call('/api/terrain', dict(code='AAA', x=1500, y=2500, shape='kicker', height=3,
                                                     length=2, width=2, drop=1, edge=1))
        self.assertEqual(status, 400)
        self.assertIn('cannot hold', res['error'])
        _, course3 = self.call('/api/course?code=AAA')
        self.assertEqual([p['c'] for p in course3['patches']], [p['c'] for p in course2['patches']])

        obj = next(o for o in course2['objects'] if o['n'].endswith('#2'))
        status, res = self.call('/api/objects', dict(code='AAA', action='move', keys=[obj['k']],
                                                     delta=[0, 0, 250]))
        self.assertEqual(status, 200, res)
        _, course4 = self.call('/api/course?code=AAA')
        moved = next(o for o in course4['objects'] if o['k'] == obj['k'])
        self.assertAlmostEqual(moved['lo'][2] - obj['lo'][2], 250, places=1)

        status, res = self.call('/api/undo', {})
        self.assertEqual(status, 200)
        _, course5 = self.call('/api/course?code=AAA')
        back = next(o for o in course5['objects'] if o['k'] == obj['k'])
        self.assertEqual(back['lo'], obj['lo'])

        out = os.path.join(self.tmp.name, 'out.iso')
        status, res = self.call('/api/save', dict(output=out))
        self.assertEqual(status, 200, res)
        self.assertEqual(os.path.getsize(out), os.path.getsize(self.iso))
        w = World(out)
        from ssx3map import mapedit
        saved = {f'{c}:{r.offset}': p.c for c, r, p in mapedit.patches(w, mapedit.course_locations(w, 'AAA'))}
        self.assertTrue(any(saved[k] != Patch_c(before[k]) for k in changed))

        status, res = self.call('/api/save', dict(output=self.iso))
        self.assertEqual(status, 400)           # never overwrite the input


    def test_warp(self):
        _, course = self.call('/api/course?code=AAA')
        session = next(r for r in course['regions'] if r['kind'] == 1)
        obj = next(o for o in course['objects'] if o['n'].endswith('#1'))
        rail = course['rails'][0]['pts']
        status, res = self.call('/api/warp', dict(code='AAA', x=1500, y=2500, tx=1800, ty=2500, radius=5, edge=15))
        self.assertEqual(status, 200, res)
        self.assertIn('AI trasy 2', res['message'])
        self.assertIn('zvukové spúšťače', res['message'])
        _, moved = self.call('/api/course?code=AAA')
        self.assertAlmostEqual(next(r for r in moved['regions'] if r['kind'] == 1)['p'][0] - session['p'][0], 300, 1)
        self.assertAlmostEqual(next(o for o in moved['objects'] if o['k'] == obj['k'])['lo'][0] - obj['lo'][0], 300, 1)
        self.assertNotEqual(moved['rails'][0]['pts'], rail)
        self.assertGreater(moved['line']['length'], course['line']['length'])
        # Too far for the edge: refused, nothing changes.
        status, res = self.call('/api/warp', dict(code='AAA', x=1500, y=2500, tx=3500, ty=2500, radius=5, edge=10))
        self.assertEqual(status, 400)
        self.assertIn('fold', res['error'])
        status, res = self.call('/api/undo', {})
        self.assertEqual(status, 200)
        _, back = self.call('/api/course?code=AAA')
        self.assertEqual(back['rails'][0]['pts'], rail)
        self.assertEqual(back['regions'], course['regions'])


    def test_recipe_marks_and_changes(self):
        status, recipes = self.call('/api/recipes')
        self.assertEqual(status, 200)
        self.assertIn('snowjam_oliver', [r['name'] for r in recipes])
        path = os.path.join(self.tmp.name, 'mini.json')
        with open(path, 'w') as f:
            json.dump({'name': 'mini', 'course': 'AAA', 'steps': [
                {'op': 'warp', 'along': 24, 'right': 3, 'radius': 5, 'edge': 15},
                {'op': 'bump', 'along': 10, 'height': 1.5}]}, f)
        _, before = self.call('/api/course?code=AAA')
        status, res = self.call('/api/recipe', dict(name=path))
        self.assertEqual(status, 200, res)
        self.assertEqual(res['code'], 'AAA')
        self.assertEqual([s['ok'] for s in res['steps']], [True, True])
        _, after = self.call('/api/course?code=AAA')
        self.assertEqual([m['t'] for m in after['marks']], ['1. ohyb 3 m doprava', '2. kopec +1.5 m'])
        self.assertGreater(after['changed'], before['changed'])
        self.assertEqual(after['changed'], sum(p['ch'] for p in after['patches']))
        self.assertTrue(any(o['ch'] for o in after['objects']))
        status, res = self.call('/api/undo', {})
        _, back = self.call('/api/course?code=AAA')
        self.assertEqual(back['marks'], [])
        self.assertEqual(back['changed'], before['changed'])

    def test_compare_with_the_original_game(self):
        from ssx3map import warp
        w = World(self.iso)
        warp.warp_edit(w, 'AAA', warp.Grab((1500.0, 2500.0), (300.0, 0.0, 0.0), 500.0, 1500.0))
        out = os.path.join(self.tmp.name, 'warped.iso')
        w.save(out)
        plain = server.Session(World(out)).course('AAA')
        compared = server.Session(World(out), reference=World(self.iso)).course('AAA')
        self.assertEqual(plain['changed'], 0)               # the edited disc against itself
        self.assertFalse(plain['compare'])
        self.assertTrue(compared['compare'])
        self.assertGreater(compared['changed'], 20)


def Patch_c(flat):
    return [[tuple(flat[(j * 4 + i) * 3:(j * 4 + i) * 3 + 3]) for i in range(4)] for j in range(4)]


if __name__ == '__main__':
    unittest.main()
