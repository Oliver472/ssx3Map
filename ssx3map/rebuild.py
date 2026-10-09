"""Wipe a course and build a plain slope with jumps on its route.

The terrain of the course's own location is replaced by a new piste that
follows the (smoothed) race line: a flat surface `width` wide with a gentle
constant fall, walls on both sides and jumps with step-down landings. Its
patches reuse the location's patch records (same count, same size); the
records left over become tiny patches far below the course. Scenery (trees,
buildings, rails, lights, particles, visibility curtains) is sunk 1 km under
the mountain. The race data follows the new ground: AI and race paths with
their events and remaining distance, start grid and reset points, the
progress meter, camera triggers, and the game's helper objects (start,
finish, triggers). The route stays inside the course's old area, so the
location's bounds in bam.sdb still hold.

Lighting is baked in the game: every new patch points its light rectangle at
one texel of the location's light pages whose brightness matches the
patch's slope towards the sun, so ramps and walls stay visible.
"""
from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass, field

from . import aip as aipmod
from . import instances, mapedit, terrain, texture, warp
from .terrain import BBOX_MAX, BBOX_MIN, COEFF, CORNERS, PATCH_SIZE, SPHERE

KEEP_MAPPED = ('trig', 'start', 'finish', 'load', 'nis_')    # helpers that follow the new ground
KEEP_IN_PLACE = ('reset', 'skybox')                           # helpers left where they are
SUN = (-0.35, 0.45, 0.82)
LIGHT_RECT = 0x10
UVS = 0x20
FLAGS = 0x0A
TEXTURE_CHUNK = 0x156
TEXTURE = 0x1A0
LIGHT_PAGE = 0x1A2


@dataclass
class Design:
    grade: float = 0.15         # fall per horizontal metre
    width: float = 60.0         # m, the flat piste
    bank: float = 20.0          # m, horizontal width of each wall
    bank_height: float = 12.0   # m
    jumps: list = None          # [(metres after the start, height m)]; None: every 350 m
    runup: float = 25.0         # m, concave ramp up to the lip
    lip: float = 8.0            # m, the lip falls back to the slope over this
    landing: float = 35.0       # m, the step-down landing after each jump
    step: float = 2.0           # m, how much deeper than the jump the landing drops
    fine: float = 4.0           # m, patch length around jumps


@dataclass
class Report:
    slots: int = 0
    used: int = 0
    rows: int = 0
    cols: int = 0
    fine: float = 0.0
    coarse: float = 0.0
    length_old: float = 0.0
    length_new: float = 0.0
    smoothing: float = 0.0
    shift: float = 0.0          # cm, largest distance of the new centre line from the old one
    drop: float = 0.0           # cm, fall from the start to the end of the line
    jumps: list = field(default_factory=list)
    sunk: int = 0
    helpers: int = 0
    rails: int = 0
    lights: int = 0
    particles: int = 0
    curtains: int = 0
    paths: int = 0
    points: int = 0
    gates: int = 0
    cameras: int = 0
    texture: int = -1
    chunks: dict = field(default_factory=dict)     # texture chunk -> new patches drawn with it
    layers: dict = field(default_factory=dict)     # layer type word (+0x0C) -> new patches
    untouched: list = field(default_factory=list)


def _smooth(t):
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return t * t * (3 - 2 * t)


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------

class Route:
    """The old race line resampled every `ds` cm (horizontal arc length) and a smoothed copy."""

    def __init__(self, points, ds=500.0):
        pts = [(p[0], p[1]) for p in points]
        acc = [0.0]
        for a, b in zip(pts, pts[1:]):
            acc.append(acc[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.length = acc[-1]
        n = max(2, round(self.length / ds) + 1)
        self.ds = ds = self.length / (n - 1) if self.length > 0 else ds
        self.old = []
        k = 0
        for i in range(n):
            s = min(i * ds, self.length)
            while k < len(acc) - 2 and acc[k + 1] < s:
                k += 1
            span = acc[k + 1] - acc[k]
            t = (s - acc[k]) / span if span else 0.0
            self.old.append((pts[k][0] + (pts[k + 1][0] - pts[k][0]) * t, pts[k][1] + (pts[k + 1][1] - pts[k][1]) * t))
        self.new = list(self.old)
        self._grid = None

    def smooth(self, half_width, max_sigma=30000.0):
        """Gaussian smoothing until no bend is tighter than 1.3 x half_width (or sigma reaches
        max_sigma). Returns sigma (cm)."""
        sigma = 3000.0
        while True:
            self.new = _gauss(self.old, sigma / self.ds)
            if self.min_radius() >= 1.3 * half_width or sigma >= max_sigma:
                return sigma
            sigma = min(max_sigma, sigma * 1.5)

    def min_radius(self):
        heads = self.headings(self.new)
        worst = float('inf')
        for a, b in zip(heads, heads[1:]):
            turn = abs(math.atan2(math.sin(b - a), math.cos(b - a)))
            if turn > 1e-9:
                worst = min(worst, self.ds / turn)
        return worst

    @staticmethod
    def headings(pts):
        out = []
        for i in range(len(pts)):
            a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
            out.append(math.atan2(b[1] - a[1], b[0] - a[0]))
        for i in range(1, len(out)):                    # unwrap
            while out[i] - out[i - 1] > math.pi:
                out[i] -= 2 * math.pi
            while out[i] - out[i - 1] < -math.pi:
                out[i] += 2 * math.pi
        return out

    def prepare(self):
        self.h_old = self.headings(self.old)
        self.h_new = self.headings(self.new)
        cell = 5000.0
        self._cell = cell
        self._grid = {}
        for i in range(len(self.old) - 1):
            (ax, ay), (bx, by) = self.old[i], self.old[i + 1]
            for gx in range(math.floor(min(ax, bx) / cell), math.floor(max(ax, bx) / cell) + 1):
                for gy in range(math.floor(min(ay, by) / cell), math.floor(max(ay, by) / cell) + 1):
                    self._grid.setdefault((gx, gy), []).append(i)

    def _interp(self, values, s):
        f = min(max(s / self.ds, 0.0), len(values) - 1.0)
        i = min(int(f), len(values) - 2)
        t = f - i
        return values[i] + (values[i + 1] - values[i]) * t

    def centre(self, s):
        """Centre of the new piste at s; straight on past either end."""
        f = s / self.ds
        i = min(max(int(math.floor(f)), 0), len(self.new) - 2)
        t = f - i
        a, b = self.new[i], self.new[i + 1]
        if 0.0 <= t <= 1.0:
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        h = self.heading(s)
        end, d = (a, f * self.ds) if t < 0 else (b, (f - (i + 1)) * self.ds)
        return end[0] + d * math.cos(h), end[1] + d * math.sin(h)

    def heading(self, s, new=True):
        return self._interp(self.h_new if new else self.h_old, s)

    def point(self, s, l):
        """(x, y) of the new piste at s along it and l to the left of its centre (cm)."""
        cx, cy = self.centre(s)
        h = self.heading(s)
        return cx - l * math.sin(h), cy + l * math.cos(h)

    def locate(self, x, y):
        """(s, l) of (x, y) against the old line: nearest point and signed distance (left +)."""
        g = (math.floor(x / self._cell), math.floor(y / self._cell))
        best = None
        for r in range(0, 40):
            cand = set()
            for gx in range(g[0] - r, g[0] + r + 1):
                for gy in range(g[1] - r, g[1] + r + 1):
                    if max(abs(gx - g[0]), abs(gy - g[1])) == r:
                        cand.update(self._grid.get((gx, gy), ()))
            last = len(self.old) - 2
            for i in cand:
                (ax, ay), (bx, by) = self.old[i], self.old[i + 1]
                dx, dy = bx - ax, by - ay
                n = dx * dx + dy * dy
                raw = 0.0 if not n else ((x - ax) * dx + (y - ay) * dy) / n
                t = min(1.0, max(0.0, raw))
                if (i == 0 and raw < 0) or (i == last and raw > 1):
                    t = raw                 # before the start or past the end: carry on straight
                px, py = ax + dx * t, ay + dy * t
                d = math.hypot(x - px, y - py)
                if best is None or d < best[0]:
                    side = dx * (y - ay) - dy * (x - ax)
                    best = (d, (i + t) * self.ds, d if side >= 0 else -d)
            if best is not None and best[0] < r * self._cell:
                break
        if best is None:
            return 0.0, 0.0
        return best[1], best[2]


def _gauss(pts, sigma):
    """Gaussian smoothing (sigma in samples). The ends are extended by point reflection
    through the end points, so the ends stay where they are and straight lines stay put."""
    if sigma <= 0 or len(pts) < 3:
        return list(pts)
    n = len(pts)
    radius = int(3 * sigma) + 1
    memo = {}

    def ext(j):
        if 0 <= j < n:
            return pts[j]
        if j in memo:
            return memo[j]
        if j < 0:
            q, e = ext(-j), pts[0]
        else:
            q, e = ext(2 * (n - 1) - j), pts[-1]
        memo[j] = (2 * e[0] - q[0], 2 * e[1] - q[1])
        return memo[j]

    weights = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-radius, radius + 1)]
    total = sum(weights)
    out = []
    for i in range(n):
        sx = sy = 0.0
        for k, w in zip(range(-radius, radius + 1), weights):
            p = ext(i + k)
            sx += w * p[0]
            sy += w * p[1]
        out.append((sx / total, sy / total))
    return out


# --------------------------------------------------------------------------
# The new ground
# --------------------------------------------------------------------------

class Piste:
    """Height of the new ground: a constant fall, jumps with step-down landings, walls."""

    def __init__(self, route, design, s_start, z_start):
        self.route, self.d = route, design
        self.s_start, self.z_start = s_start, z_start
        self.half = design.width * 50.0
        self.bank = design.bank * 100.0
        self.jumps = []
        for metres, height in design.jumps:
            self.jumps.append((s_start + metres * 100.0, height * 100.0))

    def base(self, s):
        d = self.d
        z = self.z_start - d.grade * (s - self.s_start)
        for s_lip, h in self.jumps:
            start = s_lip + d.lip * 100
            z -= (h + d.step * 100) * _smooth((s - start) / (d.landing * 100))
        return z

    def feature(self, s):
        d = self.d
        out = 0.0
        for s_lip, h in self.jumps:
            x = s - s_lip
            a = d.runup * 100
            if -a <= x <= 0:
                out += h * ((x + a) / a) ** 2
            elif 0 < x < d.lip * 100:
                out += h * (1 - _smooth(x / (d.lip * 100)))
        return out

    def wall(self, l):
        over = abs(l) - self.half
        return self.d.bank_height * 100 * _smooth(over / self.bank) if over > 0 else 0.0

    def z(self, s, l):
        return self.base(s) + self.feature(s) + self.wall(l)

    def plain(self, s, l):
        return self.base(s) + self.wall(l)

    def zones(self):
        """[(s0, s1)] that need short patches (around jumps and landings)."""
        d = self.d
        out = []
        for s_lip, h in self.jumps:
            out.append((s_lip - (d.runup + 6) * 100, s_lip + (d.lip + d.landing + 6) * 100))
        out.sort()
        merged = []
        for a, b in out:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged


class CourseMap:
    """Where things go: the point at (s, l, height above the old ground) on the old course
    moves to the same (s, l) on the new piste, at the same height above the new ground."""

    def __init__(self, route, piste, old_ground, lmax):
        self.route, self.piste, self.old_ground, self.lmax = route, piste, old_ground, lmax
        self.cache = {}

    def weight(self, x, y):
        return 1.0

    def _target(self, x, y, z):
        key = (round(x, 1), round(y, 1), round(z, 1))
        hit = self.cache.get(key)
        if hit is None:
            s, l = self.route.locate(x, y)
            l = max(-self.lmax, min(self.lmax, l))
            nx, ny = self.route.point(s, l)
            g = self.old_ground(x, y)
            above = 0.0 if g is None else max(-200.0, min(600.0, z - g))
            hit = (nx, ny, self.piste.z(s, l) + above)
            self.cache[key] = hit
        return hit

    def apply(self, p):
        return self._target(*p)

    def __call__(self, x, y, z=None):
        z = 0.0 if z is None else z
        t = self._target(x, y, z)
        return (t[0] - x, t[1] - y, t[2] - z)

    def yaw(self, x, y):
        s, _ = self.route.locate(x, y)
        return self.route.heading(s) - self.route.heading(s, new=False)


class Shift:
    """A constant move (sinking scenery)."""

    def __init__(self, d):
        self.d = d

    def weight(self, x, y):
        return 1.0

    def __call__(self, x, y, z=None):
        return self.d

    def apply(self, p):
        return tuple(a + b for a, b in zip(p, self.d))

    def yaw(self, x, y):
        return 0.0


# --------------------------------------------------------------------------
# Patch records
# --------------------------------------------------------------------------

def _fit_patch(sample):
    """Coefficients c[k][j][i] (axis, v power, u power) of the bicubic through sample(u, v)."""
    pts = [[sample(u, v) for u in terrain.GRID] for v in terrain.GRID]
    return [terrain._fit([[p[k] for p in row] for row in pts]) for k in range(3)]


def write_patch(buf, offset, coef, corner_uv):
    """Write coefficients (keeping each vec4's w), corners in the slot's order, box and sphere."""
    for j in range(4):
        for i in range(4):
            at = offset + COEFF + 16 * (15 - (4 * j + i))
            struct.pack_into('<3f', buf, at, coef[0][j][i], coef[1][j][i], coef[2][j][i])
    p = terrain.Patch(bytes(buf[offset:offset + PATCH_SIZE]))
    corners = p.corners()
    for o, uv in zip(CORNERS, corner_uv):
        struct.pack_into('<3f', buf, offset + o, *corners[uv])
    net = [q for row in p.control_net() for q in row] + list(corners.values())
    lo = [min(q[k] for q in net) - 1.0 for k in range(3)]
    hi = [max(q[k] for q in net) + 1.0 for k in range(3)]
    struct.pack_into('<3f', buf, offset + BBOX_MIN, *lo)
    struct.pack_into('<3f', buf, offset + BBOX_MAX, *hi)
    centre = [(a + b) / 2 for a, b in zip(lo, hi)]
    dense = [p.point(u / 6, v / 6) for u in range(7) for v in range(7)]
    radius = max(math.dist(centre, q) for q in dense + list(corners.values())) * 1.0001 + 1.0
    struct.pack_into('<4f', buf, offset + SPHERE, *centre, radius)
    return p


def _corner_order(patch):
    """Which (u, v) corner each stored corner slot holds."""
    ev = patch.corners()
    return [min(ev, key=lambda uv: sum((a - b) ** 2 for a, b in zip(ev[uv], q))) for q in patch.stored_corners]


class LightPool:
    """Texels of the location's light pages, by how bright they leave a white texture."""

    def __init__(self, pages):
        self.items = []             # (brightness, page, u, v)
        for page, (w, h, rgba) in pages.items():
            def b(x, y):
                at = (y * w + x) * 4
                r, g, bl, a = rgba[at:at + 4]
                return (1 - (r + g + bl) / 765.0) * a / 128.0
            for y in range(1, h - 1, 2):
                for x in range(1, w - 1, 2):
                    c = b(x, y)
                    if all(abs(b(x + dx, y + dy) - c) < 0.03 for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                        self.items.append((c, page, (x + 0.5) / w, (y + 0.5) / h))
        self.items.sort()
        self.keys = [it[0] for it in self.items]

    def nearest(self, brightness):
        if not self.items:
            return None
        k = bisect.bisect_left(self.keys, brightness)
        best = min((i for i in (k - 1, k) if 0 <= i < len(self.items)),
                   key=lambda i: abs(self.keys[i] - brightness))
        return self.items[best]

    def brightness_at(self, pages, page, u, v):
        if page not in pages:
            return None
        w, h, rgba = pages[page]
        x, y = min(w - 1, int(u * w)), min(h - 1, int(v * h))
        at = (y * w + x) * 4
        r, g, b, a = rgba[at:at + 4]
        return (1 - (r + g + b) / 765.0) * a / 128.0


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / n for x in v)


# --------------------------------------------------------------------------
# The whole course
# --------------------------------------------------------------------------

def default_jumps(length_m):
    heights = (3.0, 4.0, 5.0)
    out = []
    m, k = 300.0, 0
    while m <= length_m - 350.0:
        out.append((m, heights[k % len(heights)]))
        m += 350.0
        k += 1
    return out


def flatten_course(world, code, design=None, log=None):
    """Replace the course `code` by a plain slope with jumps (see the module doc). Returns a Report."""
    design = design or Design()
    report = Report()
    locs = mapedit.course_locations(world, code)
    loc = locs[0]
    main = mapedit.main_chunk(loc)
    course = mapedit.course_aip(world, code)
    if course is None:
        raise mapedit.EditRefused(f'{code} has no race line')
    line = aipmod.course_line(course)
    report.length_old = line.length
    if design.jumps is None:
        design.jumps = default_jumps(line.length / 100.0)

    # Old ground (all of the course's locations), before anything changes.
    old_patches = [p for _, _, p in mapedit.patches(world, locs)]
    index = terrain.PatchIndex(old_patches)
    data = world.stream.current(main)
    slots = [(rec, terrain.Patch(data[rec.offset:rec.offset + rec.size]))
             for rec in world.stream.records(main) if rec.kind == 1 and rec.size == PATCH_SIZE]
    report.slots = len(slots)
    if len(slots) < 40:
        raise mapedit.EditRefused(f'{code} has only {len(slots)} terrain patches')

    # The route and the new ground.
    route = Route(line.points)
    half_total = design.width * 50.0 + design.bank * 100.0
    report.smoothing = route.smooth(half_total)
    route.prepare()
    report.shift = max(math.dist(a, b) for a, b in zip(route.old, route.new))
    report.length_new = sum(math.dist(a, b) for a, b in zip(route.new, route.new[1:]))
    s_start, _ = route.locate(*line.at(0.0)[0][:2])
    grid = course.start()
    z_start = None
    if grid is not None:
        z_start = index.z(grid.position[0], grid.position[1])
        if z_start is None:
            z_start = grid.position[2]
    if z_start is None:
        z_start = line.at(0.0)[0][2]
    piste = Piste(route, design, s_start, z_start)
    report.jumps = list(design.jumps)
    report.drop = piste.base(s_start) - piste.base(route.length)

    # Look and streaming of the old piste. The game draws a patch only while its texture chunk
    # (+0x155 track, +0x156 chunk) is resident, and texture chunks stream in by race progress, so
    # every new patch copies these from the old piste patch at the same place along the course,
    # and takes its texture and light page from that chunk.
    near = []
    for rec, p in slots:
        if struct.unpack_from('<h', p.data, TEXTURE_CHUNK)[0] < 0:
            continue
        c = p.point(0.5, 0.5)
        s_, l_ = route.locate(c[0], c[1])
        if abs(l_) < 2500:
            near.append((s_, p))
    if not near:
        near = [(route.locate(*p.point(0.5, 0.5)[:2])[0], p) for _, p in slots
                if struct.unpack_from('<h', p.data, TEXTURE_CHUNK)[0] >= 0]
    if not near:
        raise mapedit.EditRefused(f'{code} has no drawable terrain patches to copy the look from')
    near.sort(key=lambda t: t[0])
    near_s = [t[0] for t in near]
    counts = {}
    for _, p in near:
        counts[p.texture] = counts.get(p.texture, 0) + 1
    tex = max(counts, key=counts.get)
    report.texture = tex
    scales = {}
    for _, p in near:
        uv = struct.unpack_from('<8f', p.data, UVS)
        c = p.corners()
        for (a, b), (ia, ib) in ((((0, 0), (1, 0)), (0, 4)), (((0, 0), (0, 1)), (0, 2))):
            edge = math.dist(c[a], c[b])
            span = math.hypot(uv[ib] - uv[ia], uv[ib + 1] - uv[ia + 1])
            if edge > 1:
                scales.setdefault(p.texture, []).append(span / edge)
    k_uv = {t: sorted(v)[len(v) // 2] for t, v in scales.items()}
    up = sum(1 for _, p in slots if p.normal(0.5, 0.5)[2] >= 0)
    along_u = up >= len(slots) - up            # u along the course gives an upward normal

    chunk_textures, pages = {}, {}
    wanted_pages = {struct.unpack_from('<h', p.data, LIGHT_PAGE)[0] for _, p in near}
    for c in sorted({struct.unpack_from('<h', p.data, TEXTURE_CHUNK)[0] for _, p in near}):
        if c >= len(world.stream):
            continue
        cdata = world.stream.current(c)
        rids = set()
        for rec in world.stream.records(c):
            if rec.kind == 9:
                rids.add(rec.rid)
            elif rec.kind == 10 and rec.rid in wanted_pages and rec.rid not in pages:
                try:
                    pages[rec.rid] = texture.decode_rgba(bytes(cdata[rec.offset:rec.offset + rec.size]),
                                                         raw_alpha=True)
                except texture.TextureError:
                    pass
        chunk_textures[c] = rids
    pools = {page: LightPool({page: data}) for page, data in pages.items()}
    refs = []
    for _, p in near:
        page, = struct.unpack_from('<h', p.data, LIGHT_PAGE)
        u0, v0, du, dv = struct.unpack_from('<4f', p.data, LIGHT_RECT)
        if page in pools:
            b = pools[page].brightness_at(pages, page, u0 + du / 2, v0 + dv / 2)
            if b is not None:
                refs.append(b)
    refs.sort()
    b_ref = max(0.6, refs[len(refs) * 3 // 4]) if refs else 1.0
    sun = _unit(SUN)

    def reference(s_):
        k = bisect.bisect_left(near_s, s_)
        best = min((i for i in (k - 1, k) if 0 <= i < len(near)), key=lambda i: abs(near_s[i] - s_))
        return near[best][1]

    # Rows along the course: short around jumps, the rest of the budget spread over the plain parts.
    cols = [-half_total, -design.width * 50.0, 0.0, design.width * 50.0, half_total]
    ncols = len(cols) - 1
    rows_budget = len(slots) // ncols
    zones = [(max(0.0, a), min(route.length, b)) for a, b in piste.zones()]
    zones = [(a, b) for a, b in zones if b > a]
    fine = design.fine * 100.0
    fine_len = sum(b - a for a, b in zones)
    plain_len = route.length - fine_len
    pieces = len(zones) + 1                    # plain stretches between the zones
    while True:
        fine_rows = sum(max(1, math.ceil((b - a) / fine)) for a, b in zones)
        left = rows_budget - fine_rows - pieces  # every stretch may round up by one row
        if left > 0 and plain_len / left <= 4000.0:
            break
        fine *= 1.25
        if fine > 1500.0:
            raise mapedit.EditRefused(f'{len(slots)} patches are too few for a {route.length / 100:.0f} m piste')
    coarse = max(fine, plain_len / left) if plain_len > 0 else fine
    breaks = [0.0]
    pos = 0.0
    for a, b in zones + [(route.length, route.length)]:
        if a > pos:
            n = max(1, math.ceil((a - pos) / coarse))
            breaks += [pos + (a - pos) * i / n for i in range(1, n + 1)]
            pos = a
        if b > pos:
            n = max(1, math.ceil((b - pos) / fine))
            breaks += [pos + (b - pos) * i / n for i in range(1, n + 1)]
            pos = b
    report.rows, report.cols, report.fine, report.coarse = len(breaks) - 1, ncols, fine, coarse
    if report.rows * ncols > len(slots):
        raise mapedit.EditRefused('internal: more patches than slots')

    # Slots in order along the course, new patches likewise: neighbours stay in the same block.
    order = sorted(slots, key=lambda rp: route.locate(*rp[1].point(0.5, 0.5)[:2])[0])
    buf = world.stream.chunk(main)
    used = 0
    for r in range(report.rows):
        s0, s1 = breaks[r], breaks[r + 1]
        for c in range(ncols):
            l0, l1 = cols[c], cols[c + 1]
            rec, old = order[used]
            used += 1

            def at(u, v, s0=s0, s1=s1, l0=l0, l1=l1):
                if not along_u:
                    u, v = v, u
                s, l = s0 + (s1 - s0) * u, l0 + (l1 - l0) * v
                x, y = route.point(s, l)
                return x, y, piste.z(s, l)
            off = rec.offset
            new = write_patch(buf, off, _fit_patch(at), _corner_order(old))
            ref = reference((s0 + s1) / 2)
            chunk, = struct.unpack_from('<h', ref.data, TEXTURE_CHUNK)
            use_tex = tex if tex in chunk_textures.get(chunk, ()) else ref.texture
            k = k_uv.get(use_tex) or k_uv.get(tex) or 1 / 2000.0
            # texture: tiled along the course (u) and across it (v), in the order (0,0) (0,1) (1,0) (1,1)
            uvs = []
            for pu, pv in ((0, 0), (0, 1), (1, 0), (1, 1)):
                u, v = (pu, pv) if along_u else (pv, pu)
                s, l = s0 + (s1 - s0) * u, l0 + (l1 - l0) * v
                uvs += [s * k, l * k]
            struct.pack_into('<8f', buf, off + UVS, *uvs)
            # surface, flags, layer types (+0x08..+0x0F), streaming track and chunk (+0x155, +0x156)
            # and the extra layer's texture (+0x1A4) as on the old piste here; collidable
            buf[off + 0x08:off + 0x10] = ref.data[0x08:0x10]
            buf[off + 0x155:off + 0x158] = ref.data[0x155:0x158]
            buf[off + 0x1A4:off + 0x1A6] = ref.data[0x1A4:0x1A6]
            struct.pack_into('<h', buf, off + TEXTURE, use_tex)
            flags, = struct.unpack_from('<h', buf, off + FLAGS)
            struct.pack_into('<h', buf, off + FLAGS, flags | 1)
            report.chunks[chunk] = report.chunks.get(chunk, 0) + 1
            layer, = struct.unpack_from('<H', buf, off + 0x0C)
            report.layers[layer] = report.layers.get(layer, 0) + 1
            # light: a texel of this place's light page as bright as the slope towards the sun asks for
            sm, lm = (s0 + s1) / 2, (l0 + l1) / 2
            n = _unit(new.normal(0.5, 0.5))
            if n[2] < 0:
                n = tuple(-x for x in n)
            e = 50.0
            x0, y0 = route.point(sm, lm)
            x1, y1 = route.point(sm + e, lm)
            x2, y2 = route.point(sm, lm + e)
            t = (x1 - x0, y1 - y0, piste.plain(sm + e, lm) - piste.plain(sm, lm))
            b = (x2 - x0, y2 - y0, piste.plain(sm, lm + e) - piste.plain(sm, lm))
            n0 = _unit((t[1] * b[2] - t[2] * b[1], t[2] * b[0] - t[0] * b[2], t[0] * b[1] - t[1] * b[0]))
            if n0[2] < 0:
                n0 = tuple(-x for x in n0)
            ratio = sum(a * b for a, b in zip(n, sun)) / max(0.05, sum(a * b for a, b in zip(n0, sun)))
            page, = struct.unpack_from('<h', ref.data, LIGHT_PAGE)
            texel = pools[page].nearest(b_ref * max(0.5, min(1.3, ratio))) if page in pools else None
            if texel is not None:
                _, page, u, v = texel
                struct.pack_into('<4f', buf, off + LIGHT_RECT, u, v, 1e-5, 1e-5)
            else:
                buf[off + LIGHT_RECT:off + LIGHT_RECT + 16] = ref.data[LIGHT_RECT:LIGHT_RECT + 16]
            struct.pack_into('<h', buf, off + LIGHT_PAGE, page)
    report.used = used
    # Leftover slots: 10 cm patches 1 km under the start, not collidable.
    far = (*route.point(s_start, 0.0), z_start + mapedit.SINK)
    for rec, old in order[used:]:
        def tiny(u, v):
            return far[0] + 10 * u, far[1] + 10 * v, far[2]
        write_patch(buf, rec.offset, _fit_patch(tiny), _corner_order(old))
        flags, = struct.unpack_from('<h', buf, rec.offset + FLAGS)
        struct.pack_into('<h', buf, rec.offset + FLAGS, flags & ~1)

    # Everything else in the location.
    lmax = design.width * 50.0 - 300.0
    cmap = CourseMap(route, piste, index.z, lmax)
    sink = Shift((0.0, 0.0, mapedit.SINK))
    original = world.stream.chunk_original(main)
    stretch = warp.Stretch(line.points, cmap)
    for rec in world.stream.records(main):
        data = world.stream.current(main)
        kind = rec.kind
        if kind == 3 and rec.size >= 0x90:
            inst = instances.Instance(data[rec.offset:rec.offset + 0x90])
            model = world.name_of(2, inst.model & 0xFF, inst.model >> 8) or ''
            name = ((world.name_of(1, rec.track, rec.rid) or '') + ' ' + model).lower()
            if any(k in name for k in KEEP_IN_PLACE):
                report.helpers += 1
                continue
            if any(k in name for k in KEEP_MAPPED):
                t = inst.translation
                d = cmap(*t)
                instances.turn_and_move(buf, rec.offset, cmap.yaw(t[0], t[1]), d)
                instances.rebuild_bounds(buf, rec.offset, original[rec.offset:rec.offset + 0x90])
                report.helpers += 1
            else:
                instances.translate(buf, rec.offset, *sink.d)
                report.sunk += 1
        elif kind == 5 and rec.size >= 0x80:
            instances.turn_and_move(buf, rec.offset, 0.0, sink.d)
            instances.rebuild_bounds(buf, rec.offset, original[rec.offset:rec.offset + 0x80], 5)
            report.particles += 1
        elif kind in warp.LIGHTS and rec.size >= warp.LIGHTS[kind][2] + 12:
            report.lights += warp.warp_light(buf, rec.offset, kind, sink)
        elif kind == 8 and rec.size >= 48 and not (rec.size - 48) % 144:
            mapedit.translate_rail(buf, rec.offset, rec.size, mapedit.SINK)
            report.rails += 1
        elif kind == 11 and rec.size >= 0xB8:
            report.curtains += warp.warp_curtain(buf, rec.offset, sink)
        elif kind == 14 and rec.size:
            old = bytes(data[rec.offset:rec.offset + rec.size])
            try:
                aipmod.decode(old)
            except aipmod.AipError:
                report.untouched.append('nečitateľné AI trasy')
                continue
            change = warp.warp_aip(buf, rec.offset, old, cmap)
            report.paths += change.paths
            report.points += change.regions
        elif kind == 17 and rec.size:
            moved = warp.warp_cameras(buf, rec.offset, rec.size, cmap)
            report.cameras += moved or 0
        elif kind == 21 and rec.size >= 0x14:
            report.gates += warp.warp_spine(buf, rec.offset, cmap, stretch)
        elif kind in warp.UNTOUCHED and rec.size and warp.UNTOUCHED[kind] not in report.untouched:
            report.untouched.append(warp.UNTOUCHED[kind])
    if log:
        log(f'nová trať: {report.rows} x {report.cols} plátov z {report.slots}, dĺžka plátov '
            f'{report.fine / 100:.0f}..{report.coarse / 100:.0f} m')
    return report
