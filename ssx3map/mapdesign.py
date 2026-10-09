"""Oliver's Peak: a whole course designed for Snow Jam's route, as a height surface.

The course is drawn straight (the course frame of rebuild.SurfaceGround): y metres after
the start, x metres to the right, height in metres above the start. Importing it bends it
along Snow Jam's race line, so the start, the finish, the AI riders, checkpoints and reset
points keep working. It is 120 m wide: a piste in the middle, walls up to the edges.

Sections (positions for a 4 930 m course; a longer or shorter course stretches the layout,
not the features):

  Start          wide pad, gentle
  Drop-in        a steep chute
  Rollers        six rollers, 1.8 m high
  Kicker line    three kickers, 3, 4 and 5 m, with step-down landings
  Halfpipe       a U-shaped pipe, walls 8.5 m
  Table-top      a 4 m table with a 26 m deck
  Canyon         a narrow gully between 12 m walls, steep and fast
  Gaps           a gap jump over a pit, a step-up, a 4 m kicker
  Bank slalom    banked turns, left and right
  Big air        a long steep run-in, a 7 m kicker, a steep 55 m landing
  Whoops         twelve small rollers
  Finish         wide and flat
"""
from __future__ import annotations

import math

REFERENCE = 4930.0      # m: Snow Jam's race line after the route is smoothed
HALF = 60.0             # m: half the width of the band
COLUMNS = [-60.0, -32.0, -14.0, 0.0, 14.0, 32.0, 60.0]    # patch columns (x, m): finer in the middle

# name, start (m), grade, piste half width (m), wall height at the band's edge (m), colour
SECTIONS = [
    ('Start', -200.0, 0.04, 32.0, 8.0, (0.86, 0.90, 0.96)),
    ('Drop-in', 60.0, 0.32, 26.0, 10.0, (0.70, 0.80, 0.95)),
    ('Rollers', 170.0, 0.15, 26.0, 10.0, (0.75, 0.90, 0.85)),
    ('Kicker line', 600.0, 0.17, 28.0, 10.0, (0.95, 0.85, 0.70)),
    ('Halfpipe', 1150.0, 0.12, 26.0, 10.0, (0.80, 0.75, 0.95)),
    ('Table-top', 1700.0, 0.15, 26.0, 10.0, (0.95, 0.80, 0.80)),
    ('Canyon', 2000.0, 0.22, 26.0, 10.0, (0.70, 0.85, 0.80)),
    ('Gaps', 2450.0, 0.16, 26.0, 10.0, (0.95, 0.90, 0.70)),
    ('Bank slalom', 2950.0, 0.17, 24.0, 10.0, (0.75, 0.85, 0.95)),
    ('Big air', 3500.0, 0.26, 26.0, 11.0, (0.95, 0.75, 0.70)),
    ('Whoops', 4100.0, 0.12, 26.0, 9.0, (0.80, 0.92, 0.75)),
    ('Finish', 4600.0, 0.05, 34.0, 6.0, (0.90, 0.90, 0.90)),
]
GRADE_EXTRA = [(3735.0, 0.36), (3810.0, 0.12)]          # the big-air landing, then the run-out

FEATURES = [
    ('rollers', 220.0, dict(count=6, wavelength=55.0, amp=1.8)),
    ('kicker', 690.0, dict(height=3.0)),
    ('kicker', 870.0, dict(height=4.0)),
    ('kicker', 1050.0, dict(height=5.0)),
    ('table', 1800.0, dict(height=4.0)),
    ('gap', 2560.0, dict(height=3.5)),
    ('stepup', 2760.0, dict(height=3.0)),
    ('kicker', 2890.0, dict(height=4.0)),
    ('kicker', 3720.0, dict(height=7.0, runup=35.0, lip=7.0, landing=55.0, step=3.0)),
    ('whoops', 4160.0, dict(count=12, wavelength=32.0, amp=1.1)),
]
HALFPIPE = (1200.0, 1650.0)
CANYON = (2050.0, 2400.0)
BANKS = (3000.0, 3450.0, 180.0)     # from, to, period (m): one left and one right turn per period


def _smooth(t):
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return t * t * (3 - 2 * t)


def _window(y, a, b, ramp):
    """1 inside [a, b], 0 farther than `ramp` outside it, smooth in between."""
    if y < a:
        return _smooth(1 - (a - y) / ramp)
    if y > b:
        return _smooth(1 - (y - b) / ramp)
    return 1.0


def _steps(y, points, ramp=30.0):
    """A value that holds from each point's y until the next one, blended over `ramp` metres."""
    value = points[0][1]
    for at, v in points[1:]:
        value += (v - value) * _smooth((y - at + ramp / 2) / ramp)
    return value


class OliversPeak:
    name = "Oliver's Peak"
    columns = COLUMNS

    def __init__(self, length=REFERENCE):
        self.fit_length(length)

    def fit_length(self, length):
        """Lay the sections out over a course `length` metres long (features keep their size)."""
        self.length = float(length)
        k = self.length / REFERENCE
        self.k = k
        self.sections = [(n, a * k if a > 0 else a, g, w, h, c) for n, a, g, w, h, c in SECTIONS]
        grades = [(a, g) for _, a, g, _, _, _ in self.sections] + [(a * k, g) for a, g in GRADE_EXTRA]
        grades.sort()
        self.features = [(kind, y * k, p) for kind, y, p in FEATURES]
        self.halfpipe = (HALFPIPE[0] * k, HALFPIPE[1] * k)
        self.canyon = (CANYON[0] * k, CANYON[1] * k)
        self.banks = (BANKS[0] * k, BANKS[1] * k, BANKS[2])
        # The fall, integrated once on a 1 m grid from 200 m before the start to 200 m past the end.
        self.y0 = -200.0
        n = int(self.length + 400.0) + 1
        fall = [0.0]
        for i in range(1, n):
            fall.append(fall[-1] - _steps(self.y0 + i - 0.5, grades))
        zero = fall[200]
        self.fall = [v - zero for v in fall]
        widths = [(a, w) for _, a, _, w, _, _ in self.sections]
        walls = [(a, h) for _, a, _, _, h, _ in self.sections]
        self._widths, self._walls = widths, walls
        return self

    # -- the surface ---------------------------------------------------------------------
    def base(self, y):
        f = min(max(y - self.y0, 0.0), len(self.fall) - 1.001)
        i = int(f)
        return self.fall[i] + (self.fall[i + 1] - self.fall[i]) * (f - i)

    def grade(self, y):
        return (self.base(y - 1.0) - self.base(y + 1.0)) / 2.0

    def feature(self, y):
        out = 0.0
        for kind, y0, p in self.features:
            x = y - y0
            if kind in ('rollers', 'whoops'):
                span = p['count'] * p['wavelength']
                if 0.0 <= x <= span:
                    out += p['amp'] * math.sin(math.pi * x / p['wavelength']) ** 2
                continue
            h = p['height']
            runup = p.get('runup', 22.0 if kind != 'table' else 25.0)
            if x < -runup:
                continue
            if x <= 0.0:
                out += h * ((x + runup) / runup) ** 2
                continue
            if kind == 'kicker':
                lip, landing, step = p.get('lip', 6.0), p.get('landing', 30.0), p.get('step', 1.5)
                if x < lip:
                    out += h * (1 - _smooth(x / lip))
                out -= (h + step) * _smooth((x - lip) / landing)
            elif kind == 'table':
                deck, landing, step = 26.0, 28.0, 1.0
                g = self.grade(y0)
                top = h + 0.5 * g * deck
                if x <= deck:
                    out += h + 0.5 * g * x
                else:
                    out += (top + step) * (1 - _smooth((x - deck) / landing)) - step
            elif kind == 'gap':
                gap, depth, landing, step = 24.0, 4.0, 26.0, 1.5
                knuckle = h - 1.0
                if x <= gap / 2:
                    out += h + (-depth - h) * _smooth(x / (gap / 2))
                elif x <= gap:
                    out += -depth + (knuckle + depth) * _smooth((x - gap / 2) / (gap / 2))
                else:
                    out += (knuckle + step) * (1 - _smooth((x - gap) / landing)) - step
            elif kind == 'stepup':
                rise, drop_len, plateau, landing, step = 2.5, 8.0, 30.0, 25.0, 1.0
                if x <= drop_len:
                    out += h + (rise - h) * _smooth(x / drop_len)
                elif x <= drop_len + plateau:
                    out += rise
                else:
                    out += (rise + step) * (1 - _smooth((x - drop_len - plateau) / landing)) - step
        return out

    def cross(self, x, y):
        """Height across the band (walls, halfpipe, canyon, banks) above the piste's centre."""
        ax = abs(x)
        w = _steps(y, self._widths, ramp=60.0)
        wall = _steps(y, self._walls, ramp=60.0)
        plain = wall * _smooth((ax - w) / max(HALF - w, 1.0)) if ax > w else 0.0
        hp = _window(y, self.halfpipe[0], self.halfpipe[1], 40.0)
        if hp > 0:
            if ax < 9.0:
                pipe = 0.0
            elif ax < 24.0:
                pipe = 8.5 * ((ax - 9.0) / 15.0) ** 2
            else:
                pipe = 8.5 + 3.5 * _smooth((ax - 24.0) / 36.0)
            plain = plain * (1 - hp) + pipe * hp
        cn = _window(y, self.canyon[0], self.canyon[1], 35.0)
        if cn > 0:
            if ax < 8.0:
                gully = 0.0
            elif ax < 16.0:
                gully = 12.0 * _smooth((ax - 8.0) / 8.0)
            else:
                gully = 12.0 + 6.0 * _smooth((ax - 16.0) / 44.0)
            plain = plain * (1 - cn) + gully * cn
        a, b, period = self.banks
        bk = math.sin(2 * math.pi * (y - a) / period) * _window(y, a, b, 30.0) if a - 30 < y < b + 30 else 0.0
        if bk:
            plain += 3.0 * abs(bk) + 3.0 * bk * max(-1.0, min(1.0, x / w))
        return plain

    def __call__(self, x, y):
        return self.base(y) + self.feature(y) + self.cross(x, y)

    # -- for Blender ---------------------------------------------------------------------
    def section_spans(self):
        out = []
        for i, (name, a, _, _, _, colour) in enumerate(self.sections):
            b = self.sections[i + 1][1] if i + 1 < len(self.sections) else self.length + 50.0
            out.append((name, max(a, -100.0), b, colour))
        return out

    def markers(self):
        out = [dict(name='Start gate', at=(0.0, 0.0, self(0.0, 0.0))),
               dict(name='Finish line', at=(0.0, self.length, self(0.0, self.length)))]
        count = {}
        for kind, y0, p in self.features:
            label = {'kicker': 'Kicker', 'table': 'Table-top', 'gap': 'Gap jump', 'stepup': 'Step-up',
                     'rollers': 'Rollers', 'whoops': 'Whoops'}[kind]
            count[label] = count.get(label, 0) + 1
            size = f' {p["height"]:g} m' if 'height' in p else ''
            out.append(dict(name=f'{label} {count[label]}{size}', at=(0.0, y0, self(0.0, y0))))
        return out

    def meshes(self, step=2.0):
        """One mesh per section (2 m grid), the race line and nothing else: for Blender."""
        xs = [-HALF + step * i for i in range(int(2 * HALF / step) + 1)]
        out = []
        for name, a, b, colour in self.section_spans():
            n = max(1, round((b - a) / step))
            ys = [a + (b - a) * j / n for j in range(n + 1)]
            pts = [(x, y, self(x, y)) for y in ys for x in xs]
            m = len(xs)
            tris = []
            for j in range(n):
                for i in range(m - 1):
                    p = j * m + i
                    tris += [(p, p + 1, p + m + 1), (p, p + m + 1, p + m)]
            out.append(dict(name=name, points=pts, triangles=tris, color=(*colour, 1.0)))
        line = [(0.0, y, self(0.0, y) + 0.3) for y in [5.0 * j for j in range(int(self.length / 5.0) + 1)]]
        out.append(dict(name='Race line', points=line, lines=[(i, i + 1) for i in range(len(line) - 1)],
                        color=(0.9, 0.1, 0.1, 1.0)))
        return out


DESIGNS = {'olivers_peak': OliversPeak}


def write(path, design=None, length=REFERENCE):
    """Write a design as a .glb (Blender: File > Import > glTF 2.0)."""
    from . import gltf
    design = design or OliversPeak(length)
    gltf.write_glb(path, design.meshes(), design.markers(),
                   extras=dict(ssx3map='course frame: y metres after the start, x metres to the right, '
                                       'z metres above the start', name=design.name, length=design.length))
    return design
