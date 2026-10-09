"""Starting the editor without arguments: finding the game, opening it from the page."""
import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from ssx3map.editor import games, server

from tests import fixtures


class GamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, 'config.json')
        self.big, _ = fixtures.build_course_world()
        os.makedirs(os.path.join(self.tmp, 'Documents', 'PS', 'SSX 3 (USA)'))
        self.iso = os.path.join(self.tmp, 'Documents', 'PS', 'SSX 3 (USA)', 'SSX 3 (USA).iso')
        with open(self.iso, 'wb') as f:
            f.write(fixtures.build_iso(self.big))
        with open(os.path.join(self.tmp, 'Documents', 'other.iso'), 'wb') as f:
            f.write(b'not a game' * 1000)

    def find(self):
        with mock.patch.object(games, 'CONFIG', self.config):
            return games.find_games(roots=[os.path.join(self.tmp, 'Documents')], min_size=0)

    def test_finds_the_game_only(self):
        found = self.find()
        self.assertEqual([g['path'] for g in found], [self.iso])
        self.assertFalse(found[0]['original'])

    def test_knows_the_untouched_game(self):
        with mock.patch.object(games, 'ORIGINAL_BIG_SHA1', hashlib.sha1(self.big).hexdigest()):
            found = self.find()
        self.assertTrue(found[0]['original'])

    def test_remembers(self):
        with mock.patch.object(games, 'CONFIG', self.config):
            games.remember('/a.iso')
            games.remember('/b.iso')
            games.remember('/a.iso')
            self.assertEqual(games.load_config()['recent'], ['/a.iso', '/b.iso'])


class OpenFromThePageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        big, _ = fixtures.build_course_world()
        cls.iso = os.path.join(cls.tmp.name, 'game.iso')
        with open(cls.iso, 'wb') as f:
            f.write(fixtures.build_iso(big))
        cls.patch = mock.patch.object(games, 'CONFIG', os.path.join(cls.tmp.name, 'config.json'))
        cls.patch.start()
        server.Handler.session = server.Session()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patch.stop()
        cls.tmp.cleanup()

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        status, info = self.call('/api/info')
        self.assertEqual((status, info['loaded']), (200, False))
        status, res = self.call('/api/course?code=AAA')
        self.assertEqual(status, 409)                       # nothing open yet
        status, res = self.call('/api/open', dict(path=self.iso))
        self.assertEqual(status, 200, res)
        status, info = self.call('/api/info')
        self.assertTrue(info['loaded'])
        self.assertEqual(info['source'], self.iso)
        with open(games.CONFIG) as f:
            self.assertEqual(json.load(f)['recent'], [self.iso])
        # The plain slope from the page, as one undo step.
        status, res = self.call('/api/flat', dict(code='AAA', grade=10, width=12, walls=3, every=0))
        self.assertEqual(status, 200, res)
        self.assertIn('plain slope 10 %', res['message'])
        _, course = self.call('/api/course?code=AAA')
        self.assertGreater(course['changed'], 30)
        # No PCSX2 here: a clear message, not a crash.
        status, res = self.call('/api/play', dict(path=self.iso))
        self.assertEqual(status, 400)
        self.assertIn('PCSX2', res['error'])
        out = os.path.join(self.tmp.name, 'flat.iso')
        status, res = self.call('/api/save', dict(output=out))
        self.assertEqual(status, 200, res)
        _, info = self.call('/api/info')
        self.assertEqual(info['saved'], out)
        status, res = self.call('/api/open', dict(path=os.path.join(self.tmp.name, 'missing.iso')))
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
