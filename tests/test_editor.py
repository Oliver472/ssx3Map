import json
import os
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


def Patch_c(flat):
    return [[tuple(flat[(j * 4 + i) * 3:(j * 4 + i) * 3 + 3]) for i in range(4)] for j in range(4)]


if __name__ == '__main__':
    unittest.main()
