"""Terrain patches (world record kind 1) and same-size terrain deformation.

A PS2 terrain record is 432 bytes (layout checked against the retail disc, see
docs/zistenia.md):

    +0x00..0x0F  ids/flags (+0x0A authored flags; bit 0 = collidable)
    +0x10        lighting rectangle (4 floats)
    +0x20        base texture UV corners (4 x 2 floats)
    +0x40        16 x float4 bicubic coefficients, power basis, reversed:
                 raw[15 - (4*j + i)] multiplies u^i v^j (raw[15] = P(0,0))
    +0x140       bounding sphere (centre xyz, radius)
    +0x150       resource word (rid << 8 | track)
    +0x156       s16 texture chunk index
    +0x158/+0x164  bounding box min / max
    +0x170, +0x17C, +0x188, +0x194  the four corner points
    +0x1A0       s16 texture id, +0x1A2 s16 light page id

The surface is also the collision surface: changing it changes where riders
ride. Units are centimetres, Z up.

A deformation adds a height field dz(x, y). Each touched patch is sampled on a
4 x 4 grid (u, v in 0, 1/3, 2/3, 1), the samples are displaced and the bicubic
is refitted through them, so patches that share an edge also share the
displaced edge. Height edits change only Z (X/Y coefficients stay bit-identical);
`displace` moves patches sideways too (see warp.py).
"""
from __future__ import annotations

import math
import struct

PATCH_SIZE = 432
COEFF = 0x40
SPHERE = 0x140
RESOURCE = 0x150
BBOX_MIN = 0x158
BBOX_MAX = 0x164
CORNERS = (0x170, 0x17C, 0x188, 0x194)
TEXTURE = 0x1A0

GRID = (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0)


def _invert4(m):
    """Inverse of a 4x4 matrix (Gauss-Jordan, doubles)."""
    a = [list(row) + [1.0 if i == j else 0.0 for j in range(4)] for i, row in enumerate(m)]
    for col in range(4):
        pivot = max(range(col, 4), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(4):
            if r != col:
                f = a[r][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [row[4:] for row in a]


# Monomial values at the grid: V[k][i] = GRID[k] ** i. Its inverse maps samples to coefficients.
_V = [[t ** i for i in range(4)] for t in GRID]
_VINV = _invert4(_V)


class Patch:
    """Read view of one 432-byte terrain record."""

    def __init__(self, data):
        if len(data) != PATCH_SIZE:
            raise ValueError(f'terrain record is {len(data)} bytes, expected {PATCH_SIZE}')
        self.data = bytes(data)
        raw = [struct.unpack_from('<4f', data, COEFF + 16 * k) for k in range(16)]
        # c[j][i] = coefficient (xyz) of u^i v^j
        self.c = [[raw[15 - (4 * j + i)][:3] for i in range(4)] for j in range(4)]

    def point(self, u, v):
        up = (1.0, u, u * u, u * u * u)
        vp = (1.0, v, v * v, v * v * v)
        return tuple(sum(self.c[j][i][k] * up[i] * vp[j] for j in range(4) for i in range(4)) for k in range(3))

    def corners(self):
        return {(u, v): self.point(u, v) for u in (0.0, 1.0) for v in (0.0, 1.0)}

    def control_net(self):
        """Bezier control points (4x4) of the patch; their hull contains the surface."""
        # Power -> Bernstein for a cubic: b = B * a, per parameter direction.
        conv = [[1, 0, 0, 0], [1, 1 / 3, 0, 0], [1, 2 / 3, 1 / 3, 0], [1, 1, 1, 1]]
        net = []
        for bj in range(4):
            row = []
            for bi in range(4):
                row.append(tuple(sum(conv[bj][j] * conv[bi][i] * self.c[j][i][k] for j in range(4) for i in range(4))
                                 for k in range(3)))
            net.append(row)
        return net

    @property
    def bbox(self):
        return struct.unpack_from('<3f', self.data, BBOX_MIN), struct.unpack_from('<3f', self.data, BBOX_MAX)

    @property
    def stored_corners(self):
        return [struct.unpack_from('<3f', self.data, o) for o in CORNERS]

    @property
    def texture(self):
        return struct.unpack_from('<h', self.data, TEXTURE)[0]

    @property
    def flags(self):
        return struct.unpack_from('<h', self.data, 0x0A)[0]


def _bounds(points):
    lo = tuple(min(p[k] for p in points) for k in range(3))
    hi = tuple(max(p[k] for p in points) for k in range(3))
    return lo, hi


def _f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def deform(buf, offset, dz):
    """Displace the patch at buf[offset:offset+432] by the height field dz(x, y, z).

    Returns the largest |dz| applied (0.0 if the patch was not touched).
    """
    return displace(buf, offset, lambda x, y, z: (0.0, 0.0, dz(x, y, z)))


def _fit(d):
    """Power-basis coefficients coef[j][i] of the 4 x 4 samples d[v][u]: Vinv * d * Vinv^T."""
    tmp = [[sum(_VINV[i][a] * d[b][a] for a in range(4)) for i in range(4)] for b in range(4)]   # tmp[b][i]
    return [[sum(_VINV[j][b] * tmp[b][i] for b in range(4)) for i in range(4)] for j in range(4)]


def displace(buf, offset, field):
    """Move the patch at buf[offset:offset+432] by the vector field field(x, y, z) -> (dx, dy, dz).

    The patch is sampled on the 4 x 4 grid, the samples are moved and the bicubic
    is refitted through them; corners, box and sphere are rebuilt. Components the
    field leaves at zero stay bit-identical. Returns the largest displacement length.
    """
    patch = Patch(buf[offset:offset + PATCH_SIZE])
    samples = [[patch.point(u, v) for u in GRID] for v in GRID]       # samples[b][a] at (u=GRID[a], v=GRID[b])
    d = [[field(*p) for p in row] for row in samples]
    biggest = max(math.sqrt(sum(c * c for c in x)) for row in d for x in row)
    if biggest == 0.0:
        return 0.0
    # Which stored corner slot holds which (u, v) corner; decided before editing.
    old_corners = patch.corners()
    slot_uv = []
    for p in patch.stored_corners:
        slot_uv.append(min(old_corners, key=lambda uv: sum((a - b) ** 2 for a, b in zip(old_corners[uv], p))))
    for k in range(3):
        dk = [[x[k] for x in row] for row in d]
        if not any(v for row in dk for v in row):
            continue
        coef = _fit(dk)
        for j in range(4):
            for i in range(4):
                if abs(coef[j][i]) < 1e-4:      # rounding noise: keep the stored bits (and zeros) as they are
                    continue
                at = offset + COEFF + 16 * (15 - (4 * j + i)) + 4 * k
                c, = struct.unpack_from('<f', buf, at)
                struct.pack_into('<f', buf, at, c + coef[j][i])
    new = Patch(buf[offset:offset + PATCH_SIZE])
    corners = new.corners()
    for o, uv in zip(CORNERS, slot_uv):
        struct.pack_into('<3f', buf, offset + o, *corners[uv])
    # Box: the Bezier hull (contains the whole surface), grown by a float ulp's worth.
    net = [p for row in new.control_net() for p in row]
    lo, hi = _bounds(net + list(corners.values()))
    lo = tuple(v - 0.01 - 1e-6 * abs(v) for v in lo)
    hi = tuple(v + 0.01 + 1e-6 * abs(v) for v in hi)
    struct.pack_into('<3f', buf, offset + BBOX_MIN, *lo)
    struct.pack_into('<3f', buf, offset + BBOX_MAX, *hi)
    centre = tuple((a + b) / 2 for a, b in zip(lo, hi))
    dense = [new.point(u / 6, v / 6) for u in range(7) for v in range(7)]
    radius = max(math.dist(centre, p) for p in dense + list(corners.values()))
    struct.pack_into('<4f', buf, offset + SPHERE, *centre, _f32(radius * 1.0001 + 0.01))
    return biggest


# --------------------------------------------------------------------------
# Height fields
# --------------------------------------------------------------------------

def smoothstep(x):
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return x * x * (3 - 2 * x)


class Frame:
    """A placement: origin (cm) and a forward direction in the XY plane."""

    def __init__(self, x, y, heading=(1.0, 0.0)):
        n = math.hypot(*heading) or 1.0
        self.x, self.y = x, y
        self.fx, self.fy = heading[0] / n, heading[1] / n
        self.rx, self.ry = self.fy, -self.fx           # right-hand side, Z up

    def local(self, x, y):
        dx, dy = x - self.x, y - self.y
        return dx * self.fx + dy * self.fy, dx * self.rx + dy * self.ry   # (along, side)

    def world(self, along, side):
        return (self.x + along * self.fx + side * self.rx, self.y + along * self.fy + side * self.ry)


def bump(frame, height, radius):
    """Round hill (or dip with negative height), cosine profile."""
    def dz(x, y, z=None):
        d = math.hypot(x - frame.x, y - frame.y)
        return height * (0.5 + 0.5 * math.cos(math.pi * d / radius)) if d < radius else 0.0
    dz.reach = radius
    return dz


def plateau(frame, height, radius, edge):
    """Raise or lower a round area with a flat top and a smooth rim `edge` wide."""
    def dz(x, y, z=None):
        d = math.hypot(x - frame.x, y - frame.y)
        return height * (1.0 - smoothstep((d - radius) / edge)) if d < radius + edge else 0.0
    dz.reach = radius + edge
    return dz


def kicker(frame, height, length, width, drop, edge):
    """A jump: rises over `length` up to the lip at the frame origin, then falls
    back over `drop`; full height across `width`, blended out over `edge` at
    the sides. Faces along the frame's forward direction."""
    def dz(x, y, z=None):
        along, side = frame.local(x, y)
        if along < -length or along > drop:
            return 0.0
        s = abs(side) - width / 2
        lateral = 1.0 if s <= 0 else 1.0 - smoothstep(s / edge)
        if lateral <= 0:
            return 0.0
        if along <= 0:
            t = (along + length) / length
            profile = t * t                        # concave take-off ramp
        else:
            profile = 1.0 - smoothstep(along / drop)
        return height * profile * lateral
    dz.reach = math.hypot(max(length, drop), width / 2 + edge)
    return dz


def flatten_to(frame, target_z, radius, edge):
    """Pull the surface towards height `target_z` inside `radius`, blending over `edge`."""
    def dz(x, y, z=None):
        d = math.hypot(x - frame.x, y - frame.y)
        if z is None or d >= radius + edge:
            return 0.0
        return (target_z - z) * (1.0 - smoothstep((d - radius) / edge))
    dz.reach = radius + edge
    return dz


def surface_z(patches, x, y):
    """Height of the terrain at (x, y): the highest patch surface over that point, or None.

    `patches` is an iterable of Patch. Solves P(u, v).xy = (x, y) by Newton steps.
    """
    best = None
    for patch in patches:
        lo, hi = patch.bbox
        if not (lo[0] - 1 <= x <= hi[0] + 1 and lo[1] - 1 <= y <= hi[1] + 1):
            continue
        u = v = 0.5
        for _ in range(20):
            p = patch.point(u, v)
            e = 1e-4
            pu = patch.point(u + e, v)
            pv = patch.point(u, v + e)
            a, b = (pu[0] - p[0]) / e, (pv[0] - p[0]) / e
            c, d = (pu[1] - p[1]) / e, (pv[1] - p[1]) / e
            det = a * d - b * c
            if abs(det) < 1e-12:
                break
            rx, ry = x - p[0], y - p[1]
            du, dv = (d * rx - b * ry) / det, (a * ry - c * rx) / det
            u, v = u + du, v + dv
            if abs(du) + abs(dv) < 1e-7:
                break
        if -1e-3 <= u <= 1 + 1e-3 and -1e-3 <= v <= 1 + 1e-3:
            p = patch.point(min(1, max(0, u)), min(1, max(0, v)))
            if abs(p[0] - x) < 1 and abs(p[1] - y) < 1 and (best is None or p[2] > best):
                best = p[2]
    return best


class PatchIndex:
    """Patches bucketed by XY so surface queries only look at nearby ones."""

    def __init__(self, patches, cell=3000.0):
        self.cell = cell
        self.patches = list(patches)
        self.cells = {}
        for n, p in enumerate(self.patches):
            lo, hi = p.bbox
            for ix in range(math.floor(lo[0] / cell), math.floor(hi[0] / cell) + 1):
                for iy in range(math.floor(lo[1] / cell), math.floor(hi[1] / cell) + 1):
                    self.cells.setdefault((ix, iy), []).append(n)

    def z(self, x, y):
        near = self.cells.get((math.floor(x / self.cell), math.floor(y / self.cell)), ())
        return surface_z((self.patches[n] for n in near), x, y)


def _distance_to_polyline(x, y, points):
    if len(points) == 1:
        return math.hypot(x - points[0][0], y - points[0][1])
    best = float('inf')
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dx, dy = bx - ax, by - ay
        n = dx * dx + dy * dy
        t = 0.0 if not n else min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / n))
        best = min(best, math.hypot(ax + dx * t - x, ay + dy * t - y))
    return best


def stroke(points, radius, mode, height=0.0, strength=1.0, index=None, target=None):
    """Height field of one brush stroke along `points` [(x, y)] (cm).

    raise/lower: a ridge or trench of `height` with a cosine profile `radius` wide;
    flatten:     pull towards height `target` (cm);
    smooth:      pull towards a blurred copy of the terrain (needs `index`).
    The returned field carries .frame and .reach like the shape fields.
    """
    if not points:
        raise ValueError('empty stroke')
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    reach = max(math.hypot(x - cx, y - cy) for x, y in points) + radius

    def weight(x, y):
        if abs(x - cx) > reach or abs(y - cy) > reach:
            return 0.0
        d = _distance_to_polyline(x, y, points)
        return 0.5 + 0.5 * math.cos(math.pi * d / radius) if d < radius else 0.0

    if mode in ('raise', 'lower'):
        h = abs(height) * (1 if mode == 'raise' else -1)

        def dz(x, y, z=None):
            return h * weight(x, y)
    elif mode == 'flatten':
        if target is None:
            raise ValueError('flatten needs a target height')

        def dz(x, y, z=None):
            w = weight(x, y)
            return 0.0 if not w or z is None else (target - z) * w * strength
    elif mode == 'smooth':
        if index is None:
            raise ValueError('smooth needs the terrain index')
        blurred = _blurred_grid(index, cx, cy, reach + radius, radius)

        def dz(x, y, z=None):
            w = weight(x, y)
            if not w or z is None:
                return 0.0
            b = blurred(x, y)
            return 0.0 if b is None else (b - z) * w * strength
    else:
        raise ValueError(f'unknown brush {mode!r}')
    dz.frame = Frame(cx, cy)
    dz.reach = reach
    return dz


def _blurred_grid(index, cx, cy, half, radius):
    """Bilinear lookup into the terrain heights around (cx, cy), box-blurred twice."""
    step = max(150.0, radius / 6)
    n = int(2 * half / step) + 1
    x0, y0 = cx - half, cy - half
    grid = [[index.z(x0 + i * step, y0 + j * step) for i in range(n)] for j in range(n)]
    k = max(1, round(radius / 2 / step))

    def blur_rows(rows):
        out = []
        for row in rows:
            new = []
            for i in range(len(row)):
                vals = [v for v in row[max(0, i - k):i + k + 1] if v is not None]
                new.append(sum(vals) / len(vals) if vals and row[i] is not None else None)
            out.append(new)
        return out

    def transpose(rows):
        return [list(col) for col in zip(*rows)]

    for _ in range(2):
        grid = transpose(blur_rows(transpose(blur_rows(grid))))

    def lookup(x, y):
        fx, fy = (x - x0) / step, (y - y0) / step
        i, j = int(math.floor(fx)), int(math.floor(fy))
        if not (0 <= i < n - 1 and 0 <= j < n - 1):
            return None
        tx, ty = fx - i, fy - j
        cells = (grid[j][i], grid[j][i + 1], grid[j + 1][i], grid[j + 1][i + 1])
        if any(v is None for v in cells):
            known = [v for v in cells if v is not None]
            return sum(known) / len(known) if known else None
        a = cells[0] + (cells[1] - cells[0]) * tx
        b = cells[2] + (cells[3] - cells[2]) * tx
        return a + (b - a) * ty
    return lookup
