"""Course recipes: many edits from one JSON file, skipped steps reported, the rest applied."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from ssx3map import aip, mapedit, recipe
from ssx3map.cli import main
from ssx3map.world import World

from . import fixtures

MINI = {
    'name': 'test', 'course': 'AAA',
    'steps': [
        {'op': 'warp', 'along': 24, 'right': 3, 'radius': 5, 'edge': 15},
        {'op': 'bump', 'along': 10, 'height': 1.5},
        {'op': 'kicker', 'along': 35, 'height': 3, 'length': 2, 'width': 2, 'drop': 1, 'edge': 1},
        {'op': 'objects', 'at': [2, 3], 'radius': 3, 'remove': True},
    ],
}


class RecipeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.big = os.path.join(cls.tmp, 'BAM.BIG')
        with open(cls.big, 'wb') as f:
            f.write(fixtures.build_course_world()[0])

    def test_validate(self):
        bad = {'course': 'AAA', 'steps': [{'op': 'jump', 'along': 1}, {'op': 'kicker', 'along': 5},
                                          {'op': 'bump', 'height': 2}, {'op': 'warp', 'along': 3},
                                          {'op': 'bump', 'along': 3, 'height': 1, 'hieght': 2}]}
        problems = recipe.validate(bad)
        self.assertEqual(len(problems), 5, problems)
        self.assertEqual(recipe.validate(MINI), [])

    def test_builtin_recipes_are_valid(self):
        for name in recipe.builtin_names():
            rc = recipe.load(name)
            self.assertTrue(rc['steps'], name)

    def test_run_skips_what_does_not_fit(self):
        w = World(self.big)
        line0 = aip.course_line(mapedit.course_aip(w, 'AAA'))
        results = recipe.run(w, MINI)
        self.assertEqual([r.ok for r in results], [True, True, False, True], [r.message for r in results])
        self.assertIn('cannot hold', results[2].message)
        line1 = aip.course_line(mapedit.course_aip(w, 'AAA'))
        self.assertGreater(line1.length, line0.length)
        gone = [i for _, _, i, _ in mapedit.find_objects(w, 'AAA') if i.bbox[0][2] < -50000]
        self.assertEqual(len(gone), 1)
        # Leaving out the warp leaves the race line as it was.
        w2 = World(self.big)
        results = recipe.run(w2, MINI, skip=('warp',))
        self.assertEqual(len(results), 3)
        self.assertAlmostEqual(aip.course_line(mapedit.course_aip(w2, 'AAA')).length, line0.length, delta=1.0)

    def test_cli_build(self):
        path = os.path.join(self.tmp, 'mini.json')
        with open(path, 'w') as f:
            json.dump(MINI, f)
        out, svg = os.path.join(self.tmp, 'built.BIG'), os.path.join(self.tmp, 'built.svg')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            main(['build', self.big, path, '--map', svg, '-o', out])
        text = buf.getvalue()
        self.assertIn('done: 3 of 4 steps', text)
        self.assertIn('SKIPPED', text)
        self.assertTrue(os.path.getsize(out) == os.path.getsize(self.big))
        with open(svg, encoding='utf-8') as f:
            self.assertIn('<svg', f.read())
        self.assertNotEqual(World(out).stream.current(1), World(self.big).stream.current(1))


if __name__ == '__main__':
    unittest.main()
