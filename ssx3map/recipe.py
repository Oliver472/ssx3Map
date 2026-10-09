"""Course recipes: a whole new course layout as one JSON file of edits.

    {"name": "...", "course": "ARA1",
     "steps": [{"op": "warp", "along": 900, "right": 20, "radius": 50},
               {"op": "kicker", "along": 650, "height": 4}, ...]}

Steps run in order, each on the course as the previous steps left it (so
`along` always means metres along the current race line). Placement keys are
those of the command line: along, side, at [x, y], start, session. Ops:

  warp      right, ahead, lift (m), turn (deg), radius, edge (m)
  kicker    height, length, width, drop, edge, rotate (deg)
  bump      height, radius
  plateau   height, radius, edge
  flatten   height, radius, edge
  objects   radius, name, and one of remove / raise (m)

A step the terrain cannot hold is skipped (its reason is reported) and the
rest goes on. Built-in recipes live in ssx3map/recipes/<name>.json.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

from . import instances, mapedit, terrain, warp

RECIPES = os.path.join(os.path.dirname(__file__), 'recipes')
SHAPES = ('kicker', 'bump', 'plateau', 'flatten')
OPS = ('warp',) + SHAPES + ('objects',)
PLACE = ('along', 'side', 'at', 'start', 'session')
KEYS = {
    'warp': {'right', 'ahead', 'lift', 'turn', 'radius', 'edge'},
    'kicker': {'height', 'length', 'width', 'drop', 'edge', 'rotate'},
    'bump': {'height', 'radius'},
    'plateau': {'height', 'radius', 'edge'},
    'flatten': {'height', 'radius', 'edge'},
    'objects': {'radius', 'name', 'remove', 'raise'},
}
COMMON = set(PLACE) | {'op', 'force', 'note'}


class RecipeError(ValueError):
    pass


def builtin_names():
    return sorted(n[:-5] for n in os.listdir(RECIPES) if n.endswith('.json'))


def load(source):
    """A recipe from a file path or a built-in name."""
    path = source if os.path.exists(source) else os.path.join(RECIPES, f'{source}.json')
    if not os.path.exists(path):
        raise RecipeError(f'no recipe {source!r}; built-in: {", ".join(builtin_names())}')
    with open(path, encoding='utf-8') as f:
        recipe = json.load(f)
    problems = validate(recipe)
    if problems:
        raise RecipeError('recipe problems:\n  ' + '\n  '.join(problems))
    return recipe


def validate(recipe):
    out = []
    if not isinstance(recipe, dict) or not isinstance(recipe.get('steps'), list):
        return ['a recipe is an object with a "steps" list']
    if not isinstance(recipe.get('course'), str):
        out.append('"course" must be a location code such as ARA1')
    for n, step in enumerate(recipe['steps'], 1):
        op = step.get('op') if isinstance(step, dict) else None
        if op not in OPS:
            out.append(f'step {n}: unknown op {op!r} (use {", ".join(OPS)})')
            continue
        extra = set(step) - COMMON - KEYS[op]
        if extra:
            out.append(f'step {n} ({op}): unknown keys {sorted(extra)}')
        if not any(k in step for k in PLACE):
            out.append(f'step {n} ({op}): needs a place (along, at, start or session)')
        if op in SHAPES and 'height' not in step:
            out.append(f'step {n} ({op}): needs a height')
        if op == 'warp' and not any(step.get(k) for k in ('right', 'ahead', 'lift', 'turn')):
            out.append(f'step {n} (warp): moves nothing (give right, ahead, lift or turn)')
    return out


@dataclass
class StepResult:
    number: int
    op: str
    where: str
    ok: bool
    message: str


def _place(world, code, step):
    at = step.get('at')
    return mapedit.place(world, code, along=step.get('along'), side=step.get('side') or 0.0,
                         xy=tuple(at) if at else None, start=bool(step.get('start')), session=step.get('session'))


def _warp(world, code, pl, step):
    f = pl.frame
    right, ahead = step.get('right', 0.0) * 100, step.get('ahead', 0.0) * 100
    move = (right * f.rx + ahead * f.fx, right * f.ry + ahead * f.fy, step.get('lift', 0.0) * 100)
    shift = math.hypot(move[0], move[1])
    edge = step.get('edge') or max(40.0, 2.5 * shift / 100)
    grab = warp.Grab((f.x, f.y), move, step.get('radius', 30.0) * 100, edge * 100, turn=step.get('turn', 0.0))
    r = warp.warp_edit(world, code, grab, force=bool(step.get('force')))
    return (f'posun {shift / 100:.0f} m, {r.patches} plátov, objekty {r.objects}, zábradlia {r.rails}, '
            f'AI trasy {r.paths}, trať {r.length_change / 100:+.1f} m')


def _shape(world, code, pl, step):
    op = step['op']
    frame = pl.frame
    if step.get('rotate'):
        a = math.radians(step['rotate'])
        h = (frame.fx * math.cos(a) - frame.fy * math.sin(a), frame.fx * math.sin(a) + frame.fy * math.cos(a))
        frame = terrain.Frame(frame.x, frame.y, h)
    sizes = {k: step.get(k) for k in ('radius', 'edge', 'length', 'width', 'drop')}
    r, dims, unit = mapedit.terrain_edit(world, code, frame, pl.z, op, step['height'],
                                         force=bool(step.get('force')), **sizes)
    return f'{step["height"]:+.1f} m, {r.patches} plátov (~{unit:.0f} m), objekty {r.objects}, zábradlia {r.rails}'


def _objects(world, code, pl, step):
    found = mapedit.find_objects(world, code, name=step.get('name'), frame=pl.frame,
                                 radius=step.get('radius', 20.0) * 100)
    done = 0
    for c, rec, _, label in found:
        if mapedit.is_protected(label) and not step.get('force'):
            continue
        if step.get('remove'):
            d = (0.0, 0.0, mapedit.SINK)
        elif step.get('raise') is not None:
            d = (0.0, 0.0, step['raise'] * 100)
        else:
            raise mapedit.EditRefused('objects: say remove or raise')
        instances.translate(world.stream.chunk(c), rec.offset, *d)
        done += 1
    if not done:
        raise mapedit.EditRefused('no matching objects there')
    return f'{"odstránené" if step.get("remove") else "posunuté"} {done} objektov'


def run(world, recipe, skip=(), leave_out=(), log=None):
    """Apply every step; returns [StepResult]. Steps whose op is in `skip`, or whose
    number (from 1) is in `leave_out`, are left out."""
    code = recipe['course']
    results = []
    for n, step in enumerate(recipe['steps'], 1):
        op = step['op']
        if op in skip or n in leave_out:
            continue
        where = ''
        try:
            pl = _place(world, code, step)
            where = f'{pl.frame.x / 100:.0f}, {pl.frame.y / 100:.0f}'
            if op == 'warp':
                msg = _warp(world, code, pl, step)
            elif op in SHAPES:
                msg = _shape(world, code, pl, step)
            else:
                msg = _objects(world, code, pl, step)
            res = StepResult(n, op, where, True, msg)
        except (mapedit.EditRefused, ValueError) as e:
            res = StepResult(n, op, where, False, str(e))
        results.append(res)
        if log:
            place = f'{step["along"]:.0f} m' if 'along' in step else where
            log(f'  {n:2d}. {op:8s} {place:>8s}: {"ok, " if res.ok else "PRESKOČENÉ: "}{res.message}')
    return results


def fit(world, open_world, recipe, results, skip=(), log=None):
    """Make the edited world fit the game data: while a changed chunk would not fit its
    blocks, build again without one more step - the last warp first (warps rewrite the
    most bytes), then the last shape. Returns (world, results, left-out step numbers)."""
    left_out = []
    while True:
        over = {}
        for c in world.stream.changed_chunks():
            short = world.stream.shortfall(c)
            if short > 0:
                over[c] = short
        if not over:
            return world, results, left_out
        applied = [r for r in results if r.ok]
        warps = [r for r in applied if r.op == 'warp']
        victim = (warps or applied or [None])[-1]
        if victim is None:
            raise RecipeError(f'the edits do not fit the game data (chunk {", ".join(map(str, over))})')
        left_out.append(victim.number)
        if log:
            log(f'  úpravy sa nezmestia do herných dát (chýba ~{max(over.values())} B); '
                f'vynechávam krok {victim.number} ({victim.op})')
        world = open_world()
        results = run(world, recipe, skip=skip, leave_out=left_out)
