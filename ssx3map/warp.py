"""Moving a piece of a course sideways (a "warp").

One smooth displacement field is applied to everything that lives in world
space, so the course keeps working where it now is:

  kind 1   terrain patches       refitted in x, y and z (terrain.displace)
  kind 3   object instances      turned and moved with the ground under their origin
  kind 5   particle instances    the same
  kind 6   local lights          position, spot axis, bounds
  kind 7   light glows           position, bounds
  kind 8   rails                 every cubic segment refitted; lengths, distances, bounds
  kind 11  visibility curtains   corners, plane, sphere, box
  kind 14  AI and race paths     points, bounds, event distances, remaining distance;
                                 start grid and reset points with their directions
  kind 17  camera triggers       volume, look and bound positions, turn
  kind 21  progress meter        gate origins and axes, distances, markers

Collision meshes (kind 12) are in their model's space and follow the instances.
Records we cannot read (sound triggers, stage scripts, ...) stay where they
are; the report lists them. Every record keeps its size.

The field: inside `radius` around the grabbed point everything moves rigidly
(by `move`, turned by `turn` about that point); over the next `edge` the
motion fades out with a smoothstep. The ground in that ring is stretched or
squeezed; the edit is refused when it would squeeze more than MIN_STRETCH.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from . import aip as aipmod
from . import instances, mapedit, terrain

MIN_STRETCH = 0.35          # smallest allowed area ratio (Jacobian) without force
FOLD = 0.05                 # never allowed: the ground would fold over itself


def _smooth(t):
    return t * t * (3 - 2 * t)


def _smooth_d(t):
    return 6 * t * (1 - t)


class Grab:
    """Move the area around `origin` (cm) by `move` (dx, dy, dz cm), turned by `turn` degrees."""

    def __init__(self, origin, move, radius, edge, turn=0.0):
        if radius < 0 or edge <= 0:
            raise ValueError('radius must be >= 0 and edge > 0')
        self.ox, self.oy = float(origin[0]), float(origin[1])
        self.mx, self.my, self.mz = (float(v) for v in move)
        self.radius, self.edge = float(radius), float(edge)
        self.turn = math.radians(turn)
        self.c, self.s = math.cos(self.turn), math.sin(self.turn)
        self.reach = self.radius + self.edge
        self.frame = terrain.Frame(self.ox, self.oy)

    def weight(self, x, y):
        d = math.hypot(x - self.ox, y - self.oy)
        if d <= self.radius:
            return 1.0
        if d >= self.reach:
            return 0.0
        return 1.0 - _smooth((d - self.radius) / self.edge)

    def __call__(self, x, y, z=None):
        w = self.weight(x, y)
        if not w:
            return (0.0, 0.0, 0.0)
        rx, ry = x - self.ox, y - self.oy
        tx = self.ox + rx * self.c - ry * self.s + self.mx
        ty = self.oy + rx * self.s + ry * self.c + self.my
        return (w * (tx - x), w * (ty - y), w * self.mz)

    def apply(self, p):
        d = self(*p)
        return (p[0] + d[0], p[1] + d[1], p[2] + d[2])

    def _jacobian(self, x, y):
        e = 1.0
        a, b, c = self(x, y), self(x + e, y), self(x, y + e)
        return (1 + (b[0] - a[0]) / e, (c[0] - a[0]) / e, (b[1] - a[1]) / e, 1 + (c[1] - a[1]) / e)

    def yaw(self, x, y):
        """How much the ground at (x, y) turns (radians)."""
        if not self.weight(x, y):
            return 0.0
        j00, j01, j10, j11 = self._jacobian(x, y)
        return math.atan2(j10 - j01, j00 + j11)

    def min_stretch(self):
        """Smallest area ratio of the map over the blend ring (1 = untouched, <= 0 = folded)."""
        worst = 1.0
        for k in range(72):
            a = 2 * math.pi * k / 72
            for i in range(1, 40):
                r = self.radius + self.edge * i / 40
                x, y = self.ox + r * math.cos(a), self.oy + r * math.sin(a)
                j00, j01, j10, j11 = self._jacobian(x, y)
                worst = min(worst, j00 * j11 - j01 * j10)
        return worst

    @property
    def size(self):
        return math.hypot(self.mx, self.my, self.mz)


def _same(a, b):
    return all(x == y for x, y in zip(a, b))


def _rebound(old_lo, old_hi, old_pts, new_pts):
    """New bounds around new_pts keeping the old margins around old_pts."""
    lo, hi = [], []
    for k in range(3):
        olo, ohi = min(p[k] for p in old_pts), max(p[k] for p in old_pts)
        lo.append(min(p[k] for p in new_pts) - max(0.0, olo - old_lo[k]))
        hi.append(max(p[k] for p in new_pts) + max(0.0, old_hi[k] - ohi))
    return lo, hi


def _turn_xy(v, yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c) + tuple(v[2:])


# --------------------------------------------------------------------------
# Rails (kind 8)
# --------------------------------------------------------------------------

RAIL_HEADER, RAIL_SEGMENT = 48, 144


def _cubic(rows, t):
    return tuple(rows[0][k] * t ** 3 + rows[1][k] * t * t + rows[2][k] * t + rows[3][k] for k in range(3))


def _arc(rows, n=48):
    pts = [_cubic(rows, i / n) for i in range(n + 1)]
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def warp_rail(buf, offset, size, grab):
    """Bend a rail record. Returns the worst fit error (cm), or None if it was not touched.

    Layout (ssx-web RAIL_RECOVERY): +0x04/+0x10 bounds; per 144-byte segment
    +0x0C arc length, +0x10.. rows for t^3, t^2, t, 1, +0x6C/+0x78 bounds,
    +0x84 distance from the rail start."""
    count = (size - RAIL_HEADER) // RAIL_SEGMENT
    segs = []
    for k in range(count):
        base = offset + RAIL_HEADER + RAIL_SEGMENT * k
        rows = [struct.unpack_from('<3f', buf, base + 0x10 + 16 * r) for r in range(4)]
        samples = [_cubic(rows, t) for t in terrain.GRID]
        segs.append((base, rows, samples, [grab(*p) for p in samples]))
    if not any(any(v for d in moves for v in d) for _, _, _, moves in segs):
        return None
    boxes_before = _segment_boxes(buf, segs)
    error = 0.0
    lengths = []
    for base, rows, samples, moves in segs:
        length, = struct.unpack_from('<f', buf, base + 0x0C)
        if not any(v for d in moves for v in d):
            lengths.append(length)
            continue
        target = [tuple(p[k] + d[k] for k in range(3)) for p, d in zip(samples, moves)]
        coef = [[sum(terrain._VINV[i][a] * target[a][k] for a in range(4)) for k in range(3)] for i in range(4)]
        new_rows = [coef[3], coef[2], coef[1], coef[0]]
        for r in range(4):
            struct.pack_into('<3f', buf, base + 0x10 + 16 * r, *new_rows[r])
        for t in (1 / 6, 0.5, 5 / 6):
            error = max(error, math.dist(_cubic(new_rows, t), grab.apply(_cubic(rows, t))))
        old_arc = _arc(rows)
        new_length = length * _arc(new_rows) / old_arc if old_arc > 0 else _arc(new_rows)
        struct.pack_into('<f', buf, base + 0x0C, new_length)
        lengths.append(new_length)
        old_pts = [_cubic(rows, i / 16) for i in range(17)]
        new_pts = [_cubic(new_rows, i / 16) for i in range(17)]
        lo = struct.unpack_from('<3f', buf, base + 0x6C)
        hi = struct.unpack_from('<3f', buf, base + 0x78)
        nlo, nhi = _rebound(lo, hi, old_pts, new_pts)
        struct.pack_into('<3f', buf, base + 0x6C, *nlo)
        struct.pack_into('<3f', buf, base + 0x78, *nhi)
    distance, = struct.unpack_from('<f', buf, segs[0][0] + 0x84)
    for (base, _, _, _), length in zip(segs, lengths):
        if abs(struct.unpack_from('<f', buf, base + 0x84)[0] - distance) > 1e-3:
            struct.pack_into('<f', buf, base + 0x84, distance)
        distance += length
    # Record bounds: around the segment boxes, with the record's own margin kept.
    lo = struct.unpack_from('<3f', buf, offset + 4)
    hi = struct.unpack_from('<3f', buf, offset + 16)
    nlo, nhi = _rebound(lo, hi, boxes_before, _segment_boxes(buf, segs))
    struct.pack_into('<3f', buf, offset + 4, *nlo)
    struct.pack_into('<3f', buf, offset + 16, *nhi)
    return error


def _segment_boxes(buf, segs):
    return [struct.unpack_from('<3f', buf, base + at) for base, _, _, _ in segs for at in (0x6C, 0x78)]


# --------------------------------------------------------------------------
# AI and race paths (kind 14)
# --------------------------------------------------------------------------

def _mapper(old_s, new_s):
    """Piecewise-linear map from old to new distance along a path."""
    def g(s):
        if s <= 0 or len(old_s) < 2:
            return s
        for k in range(len(old_s) - 1):
            if s <= old_s[k + 1] or k == len(old_s) - 2:
                span = old_s[k + 1] - old_s[k]
                t = (s - old_s[k]) / span if span else 0.0
                if k == len(old_s) - 2 and s > old_s[-1]:
                    return new_s[-1] + (s - old_s[-1])
                return new_s[k] + t * (new_s[k + 1] - new_s[k])
        return s
    return g


@dataclass
class AipChange:
    paths: int = 0
    events: int = 0
    regions: int = 0
    remaining: int = 0


def warp_aip(buf, base, data, grab):
    """Rewrite one AIP record at buf[base:] (its bytes before the edit: `data`)."""
    rec = aipmod.decode(data)
    out = AipChange()
    maps = {}
    for path in rec.ai_paths + rec.track_paths:
        pts = path.points()
        moved = [grab.apply(p) for p in pts]
        old_s, new_s = [0.0], [0.0]
        for seg in path.segments:
            old_s.append(old_s[-1] + seg[3])
        if all(_same(a, b) for a, b in zip(pts, moved)):
            maps[path.kind, path.index] = (old_s[-1], old_s[-1], lambda s: s)
            continue
        out.paths += 1
        at = base + path.body
        struct.pack_into('<3f', buf, at, *moved[0])
        lo, hi = _rebound(path.low, path.high, pts, moved)
        struct.pack_into('<3f', buf, at + 12, *lo)
        struct.pack_into('<3f', buf, at + 24, *hi)
        for k, (a, b) in enumerate(zip(moved, moved[1:])):
            if _same(a, pts[k]) and _same(b, pts[k + 1]):
                new_s.append(new_s[-1] + path.segments[k][3])      # untouched: keep its bytes
                continue
            dx, dy, dz = (b[i] - a[i] for i in range(3))
            w = math.hypot(dx, dy)
            seg = (dx / w, dy / w, dz / w, w) if w > 1e-3 else (path.segments[k][0], path.segments[k][1], 0.0, w)
            struct.pack_into('<4f', buf, at + 36 + 16 * k, *seg)
            new_s.append(new_s[-1] + w)
        g = _mapper(old_s, new_s)
        maps[path.kind, path.index] = (old_s[-1], new_s[-1], g)
        ev = at + 36 + 16 * len(path.segments)
        for e, (kind, value, start, end) in enumerate(path.events):
            ns, ne = g(start), g(end)
            if ns != start or ne != end:
                struct.pack_into('<2f', buf, ev + 16 * e + 8, ns, ne)
                out.events += 1
    out.remaining = _fix_remaining(buf, base, rec, maps)
    for r in rec.regions:
        d = grab(*r.position)
        yaw = grab.yaw(r.position[0], r.position[1])
        if not any(d) and not yaw:
            continue
        struct.pack_into('<3f', buf, base + r.offset, *(a + b for a, b in zip(r.position, d)))
        struct.pack_into('<3f', buf, base + r.offset + 12, *_turn_xy(r.direction, yaw))
        out.regions += 1
    return out


def _fix_remaining(buf, base, rec, maps):
    """Keep each race path's remaining distance (to the finish) true after lengths changed.

    Paths are visited from the finish backwards (ascending remaining). A path's
    successor is the path starting near its end whose remaining matches its
    remaining minus its length; then delta = own length change + successor's
    delta. Without a successor, the path holds the point where the remaining
    distance reaches zero and the delta is how far that point moved."""
    paths = rec.track_paths
    if not paths:
        return 0
    rem = {p.index: p.header[3] for p in paths}
    delta = {}
    changed = 0
    for p in sorted(paths, key=lambda q: rem[q.index]):
        old_len, new_len, g = maps['track', p.index]
        end = p.points()[-1]
        best = None
        for q in paths:
            if q.index not in delta or q is p:
                continue
            gap = math.dist(q.position, end)
            mismatch = abs(rem[p.index] - old_len - rem[q.index])
            if gap <= 5000.0 and mismatch <= 5000.0 and (best is None or mismatch < best[0]):
                best = (mismatch, q.index)
        if best is not None:
            delta[p.index] = (new_len - old_len) + delta[best[1]]
        elif 0.0 <= rem[p.index] <= old_len:
            delta[p.index] = g(rem[p.index]) - rem[p.index]
        else:
            delta[p.index] = new_len - old_len
        if abs(delta[p.index]) > 1e-3:
            struct.pack_into('<f', buf, base + p.remaining_offset, rem[p.index] + delta[p.index])
            changed += 1
    return changed


# --------------------------------------------------------------------------
# Course progress (kind 21), lights (6, 7), curtains (11), camera triggers (17)
# --------------------------------------------------------------------------

class Stretch:
    """How much longer the course line got before each of its points (cm, horizontal)."""

    def __init__(self, points, grab):
        self.points = [(p[0], p[1]) for p in points]
        moved = [grab.apply(p) for p in points]
        self.old, self.new = [0.0], [0.0]
        for a, b, c, d in zip(points, points[1:], moved, moved[1:]):
            self.old.append(self.old[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
            self.new.append(self.new[-1] + math.hypot(d[0] - c[0], d[1] - c[1]))

    def at(self, x, y):
        best = None
        for k in range(len(self.points) - 1):
            (ax, ay), (bx, by) = self.points[k], self.points[k + 1]
            dx, dy = bx - ax, by - ay
            n = dx * dx + dy * dy
            t = 0.0 if not n else min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / n))
            d = math.hypot(ax + dx * t - x, ay + dy * t - y)
            if best is None or d < best[0]:
                best = (d, k, t)
        if best is None:
            return 0.0
        _, k, t = best
        return (self.new[k] + t * (self.new[k + 1] - self.new[k])) - (self.old[k] + t * (self.old[k + 1] - self.old[k]))


def warp_spine(buf, offset, grab, stretch):
    """Progress meter path (ssx-web export_progress_meter): gates (axis xy, origin xy, distance), markers."""
    n, seg_off, m, mark_off = struct.unpack_from('<4I', buf, offset)
    gates = [list(struct.unpack_from('<5f', buf, offset + seg_off + 20 * k)) for k in range(n)]
    if not gates:
        return 0
    shift = [stretch.at(g[2], g[3]) if stretch else 0.0 for g in gates]
    base_shift = shift[0]
    if not any(grab.weight(g[2], g[3]) for g in gates) and all(abs(v - base_shift) < 1e-3 for v in shift):
        return 0
    moved = 0
    old_d = [g[4] for g in gates]
    for k, g in enumerate(gates):
        d = grab(g[2], g[3])
        yaw = grab.yaw(g[2], g[3])
        nx, ny = _turn_xy((g[0], g[1]), yaw)
        new = [nx, ny, g[2] + d[0], g[3] + d[1], g[4] + shift[k] - base_shift]
        if new != g:
            struct.pack_into('<5f', buf, offset + seg_off + 20 * k, *new)
            moved += 1

    def shift_at(dist):
        if dist <= old_d[0]:
            return shift[0] - base_shift
        for k in range(len(old_d) - 1):
            if dist <= old_d[k + 1]:
                span = old_d[k + 1] - old_d[k]
                t = (dist - old_d[k]) / span if span else 0.0
                return shift[k] + t * (shift[k + 1] - shift[k]) - base_shift
        return shift[-1] - base_shift

    for k in range(m):
        at = offset + mark_off + 8 * k + 4
        dist, = struct.unpack_from('<f', buf, at)
        struct.pack_into('<f', buf, at, dist + shift_at(dist))
    total, = struct.unpack_from('<f', buf, offset + 0x10)
    struct.pack_into('<f', buf, offset + 0x10, total + shift_at(total))
    return moved


def _move_box(buf, lo_at, hi_at, pivot, d, yaw):
    lo = struct.unpack_from('<3f', buf, lo_at)
    hi = struct.unpack_from('<3f', buf, hi_at)
    corners = [(x, y) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])]
    if yaw:
        corners = [_turn_xy((x - pivot[0], y - pivot[1]), yaw) for x, y in corners]
        corners = [(pivot[0] + x, pivot[1] + y) for x, y in corners]
    struct.pack_into('<3f', buf, lo_at, min(c[0] for c in corners) + d[0], min(c[1] for c in corners) + d[1],
                     lo[2] + d[2])
    struct.pack_into('<3f', buf, hi_at, max(c[0] for c in corners) + d[0], max(c[1] for c in corners) + d[1],
                     hi[2] + d[2])


# kind: (position, bounds min, bounds max, direction vectors to turn)
LIGHTS = {6: (56, 68, 80, (44,)), 7: (28, 40, 52, ())}


def warp_light(buf, offset, kind, grab):
    pos_at, lo_at, hi_at, axes = LIGHTS[kind]
    p = struct.unpack_from('<3f', buf, offset + pos_at)
    d = grab(*p)
    yaw = grab.yaw(p[0], p[1])
    if not any(d) and not yaw:
        return False
    struct.pack_into('<3f', buf, offset + pos_at, *(a + b for a, b in zip(p, d)))
    for a in axes:
        struct.pack_into('<3f', buf, offset + a, *_turn_xy(struct.unpack_from('<3f', buf, offset + a), yaw))
    _move_box(buf, offset + lo_at, offset + hi_at, p, d, yaw)
    return True


def warp_curtain(buf, offset, grab):
    """Visibility curtain (SSX-Library WorldVisCurtain): sphere +0, corners +0x10..+0x40,
    plane normal +0x50 and distance +0x5C (n.x + d = 0), box +0xA0/+0xAC."""
    corners = [struct.unpack_from('<3f', buf, offset + 0x10 + 16 * k) for k in range(4)]
    moved = [grab.apply(c) for c in corners]
    if all(_same(a, b) for a, b in zip(corners, moved)):
        return False
    for k, c in enumerate(moved):
        struct.pack_into('<3f', buf, offset + 0x10 + 16 * k, *c)
    old_n = struct.unpack_from('<3f', buf, offset + 0x50)
    # Newell's normal of the moved quad, oriented like the old one.
    n = [0.0, 0.0, 0.0]
    for a, b in zip(moved, moved[1:] + moved[:1]):
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    length = math.sqrt(sum(v * v for v in n))
    if length < 1e-6:
        n = list(_turn_xy(old_n, grab.yaw(*corners[0][:2])))
    else:
        n = [v / length for v in n]
        if sum(a * b for a, b in zip(n, old_n)) < 0:
            n = [-v for v in n]
    dist = -sum(sum(n[k] * c[k] for k in range(3)) for c in moved) / 4
    struct.pack_into('<4f', buf, offset + 0x50, *n, dist)
    sphere = struct.unpack_from('<3f', buf, offset)
    centre = grab.apply(sphere)
    off = sum(n[k] * centre[k] for k in range(3)) + dist
    centre = [centre[k] - off * n[k] for k in range(3)]
    radius = max(math.dist(centre, c) for c in moved)
    struct.pack_into('<4f', buf, offset, *centre, radius)
    lo = struct.unpack_from('<3f', buf, offset + 0xA0)
    hi = struct.unpack_from('<3f', buf, offset + 0xAC)
    nlo, nhi = _rebound(lo, hi, corners, moved)
    struct.pack_into('<3f', buf, offset + 0xA0, *nlo)
    struct.pack_into('<3f', buf, offset + 0xAC, *nhi)
    return True


def camera_trigger_fields(data):
    """Offsets of the positions and Z turns in a camera trigger record (ssx-web
    export_camera_triggers: version 7, triggers of a volume and two actions).
    Returns (positions, turns) or None when the record does not parse."""
    positions, turns = [], []
    pos = 0

    def u32():
        nonlocal pos
        if pos + 4 > len(data):
            raise ValueError
        v, = struct.unpack_from('<I', data, pos)
        pos += 4
        return v

    def skip(n):
        nonlocal pos
        if pos + n > len(data):
            raise ValueError
        pos += n

    def transform():
        positions.append(pos)
        turns.append(pos + 24)
        skip(36)

    def action():
        code = u32()
        if code == 0:
            skip(8)
        elif code == 1:
            skip(28)
            positions.append(pos)
            skip(12)
            bound = u32()
            if bound == 3:
                positions.append(pos)
                skip(12)
            elif bound in (0, 1, 2):
                transform()
                if bound == 2:
                    positions.extend((pos, pos + 12))
                    skip(24)
            else:
                raise ValueError
        elif code == 2:
            skip(20)
            transform()
            for _ in range(4):
                positions.append(pos)
                skip(12)
        elif code != 3:
            raise ValueError

    try:
        if u32() != 7:
            return None
        skip(4)
        count = u32()
        skip(4)
        if count > 10000:
            return None
        for _ in range(count):
            skip(8)
            if u32() not in (0, 1):
                return None
            transform()
            action()
            action()
    except ValueError:
        return None
    return positions, turns


def warp_cameras(buf, offset, size, grab):
    parsed = camera_trigger_fields(bytes(buf[offset:offset + size]))
    if parsed is None:
        return None
    positions, turns = parsed
    moved = 0
    yaws = {}
    for at in positions:
        p = struct.unpack_from('<3f', buf, offset + at)
        d = grab(*p)
        if any(d):
            struct.pack_into('<3f', buf, offset + at, *(a + b for a, b in zip(p, d)))
            moved += 1
        yaws[at + 24] = grab.yaw(p[0], p[1])
    for at in turns:
        yaw = yaws.get(at, 0.0)
        if yaw:
            r, = struct.unpack_from('<f', buf, offset + at)
            struct.pack_into('<f', buf, offset + at, r + yaw)
    return moved


# --------------------------------------------------------------------------
# The whole course
# --------------------------------------------------------------------------

UNTOUCHED = {13: 'zvukové spúšťače', 16: 'skripty scén', 19: 'misie', 22: 'lavíny / efekty',
             -14: 'nečitateľné AI trasy', -21: 'nečitateľný ukazovateľ postupu'}


@dataclass
class WarpReport:
    patches: int = 0
    max_move: float = 0.0
    shape_error: float = 0.0
    stretch: float = 1.0
    objects: int = 0
    objects_bent: list = field(default_factory=list)
    objects_skipped: list = field(default_factory=list)
    particles: int = 0
    lights: int = 0
    rails: int = 0
    rail_error: float = 0.0
    paths: int = 0
    events: int = 0
    remaining: int = 0
    points: int = 0
    curtains: int = 0
    cameras: int = 0
    cameras_unread: int = 0
    gates: int = 0
    length_change: float = 0.0
    untouched: list = field(default_factory=list)


def apply_warp(world, code, grab):
    report = WarpReport()
    frame, reach = grab.frame, grab.reach
    course = mapedit.course_aip(world, code)
    line = aipmod.course_line(course) if course else None
    stretch = Stretch(line.points, grab) if line else None
    if stretch:
        report.length_change = stretch.new[-1] - stretch.old[-1]
    present = set()
    for loc in mapedit.course_locations(world, code):
        c = mapedit.main_chunk(loc)
        original = world.stream.chunk_original(c)
        for rec in world.stream.records(c):
            data = world.stream.current(c)
            kind = rec.kind
            if kind in UNTOUCHED and rec.size:
                present.add(kind)
            if kind == 1 and rec.size == terrain.PATCH_SIZE:
                before = terrain.Patch(data[rec.offset:rec.offset + rec.size])
                if not mapedit._near(frame, reach, *before.bbox):
                    continue
                buf = world.stream.chunk(c)
                moved = terrain.displace(buf, rec.offset, grab, sideways='bilinear')
                if moved:
                    report.patches += 1
                    report.max_move = max(report.max_move, moved)
                    after = terrain.Patch(buf[rec.offset:rec.offset + rec.size])
                    for u in (0.1, 0.3, 0.5, 0.7, 0.9):
                        for v in (0.1, 0.3, 0.5, 0.7, 0.9):
                            want = grab.apply(before.point(u, v))
                            report.shape_error = max(report.shape_error, math.dist(after.point(u, v), want))
            elif kind in (3, 5) and rec.size >= instances.LAYOUTS[kind]['size']:
                lay = instances.LAYOUTS[kind]
                lo = struct.unpack_from('<3f', data, rec.offset + lay['lo'])
                hi = struct.unpack_from('<3f', data, rec.offset + lay['hi'])
                if not mapedit._near(frame, reach, lo, hi):
                    continue
                name = (world.name_of(1, rec.track, rec.rid) if kind == 3 else None) or f'{loc.name}#{rec.rid}'
                if max(hi[0] - lo[0], hi[1] - lo[1]) > 2 * reach:
                    report.objects_skipped.append(name)
                    continue
                t = struct.unpack_from('<3f', data, rec.offset + instances.TRANSLATION)
                d = grab(*t)
                yaw = grab.yaw(t[0], t[1])
                spread = max(math.dist(grab(x, y), d) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]))
                if spread > max(100.0, 0.2 * grab.size) and kind == 3:
                    report.objects_bent.append(name)
                if not any(d) and not yaw:
                    continue
                buf = world.stream.chunk(c)
                instances.turn_and_move(buf, rec.offset, yaw, d)
                instances.rebuild_bounds(buf, rec.offset, original[rec.offset:rec.offset + lay['size']], kind)
                if kind == 3:
                    report.objects += 1
                else:
                    report.particles += 1
            elif kind in LIGHTS and rec.size >= LIGHTS[kind][2] + 12:
                if warp_light(world.stream.chunk(c), rec.offset, kind, grab):
                    report.lights += 1
            elif kind == 8 and rec.size >= RAIL_HEADER and not (rec.size - RAIL_HEADER) % RAIL_SEGMENT:
                lo = struct.unpack_from('<3f', data, rec.offset + 4)
                hi = struct.unpack_from('<3f', data, rec.offset + 16)
                if not mapedit._near(frame, reach, lo, hi):
                    continue
                error = warp_rail(world.stream.chunk(c), rec.offset, rec.size, grab)
                if error is not None:
                    report.rails += 1
                    report.rail_error = max(report.rail_error, error)
            elif kind == 11 and rec.size >= 0xB8:
                if warp_curtain(world.stream.chunk(c), rec.offset, grab):
                    report.curtains += 1
            elif kind == 14 and rec.size:
                old = bytes(data[rec.offset:rec.offset + rec.size])
                try:
                    aipmod.decode(old)
                except aipmod.AipError:
                    present.add(-14)
                    continue
                change = warp_aip(world.stream.chunk(c), rec.offset, old, grab)
                report.paths += change.paths
                report.events += change.events
                report.remaining += change.remaining
                report.points += change.regions
            elif kind == 17 and rec.size:
                moved = warp_cameras(world.stream.chunk(c), rec.offset, rec.size, grab)
                if moved is None:
                    report.cameras_unread += 1
                else:
                    report.cameras += moved
            elif kind == 21 and rec.size >= 0x14:
                n, seg_off, m, mark_off = struct.unpack_from('<4I', data, rec.offset)
                if seg_off + 20 * n > rec.size or mark_off + 8 * m > rec.size:
                    present.add(-21)
                    continue
                report.gates += warp_spine(world.stream.chunk(c), rec.offset, grab, stretch)
    report.untouched = [UNTOUCHED[k] for k in sorted(present)]
    return report


def warp_edit(world, code, grab, force=False):
    """Apply one warp; on refusal the world is left exactly as it was."""
    if abs(math.degrees(grab.turn)) > 90:
        raise mapedit.EditRefused('turns beyond 90 degrees are refused')
    stretch = grab.min_stretch()
    if stretch < FOLD:
        raise mapedit.EditRefused(f'the ground would fold over itself (move {grab.size / 100:.0f} m over a '
                                  f'{grab.edge / 100:.0f} m edge); make the edge wider or the move shorter')
    if stretch < MIN_STRETCH and not force:
        raise mapedit.EditRefused(f'the edge would squeeze the ground to {stretch:.0%} of its size; make the edge '
                                  f'wider (about {grab.size / 0.4 / 100:.0f} m for this move) or force it')
    snap = world.stream.snapshot(mapedit.course_chunks(world, code))
    try:
        report = apply_warp(world, code, grab)
    except Exception:
        world.stream.restore(snap)
        raise
    report.stretch = stretch
    if not report.patches:
        world.stream.restore(snap)
        raise mapedit.EditRefused('no terrain patch was moved (the place is off the terrain)')
    allowed = max(50.0, 0.15 * grab.size)
    if report.shape_error > allowed and not force:
        world.stream.restore(snap)
        raise mapedit.EditRefused(f'the patches here cannot follow this bend: they would be off by up to '
                                  f'{report.shape_error / 100:.2f} m; make the edge wider or force it')
    return report
