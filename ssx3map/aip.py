"""AI / race paths (world record kind 14, "AIP"; rid 0 = the course's own bank).

Layout after ssx-web's tools/race_event_assets.py (GPL-3.0):

    u32 magic
    u32 n; n x AI path:    7 x u32, u32 points, u32 events, 3f position, 3f low, 3f high,
                           points x 4f segment, events x {u32 type, u32 value, f start, f end}
    u32 n; n x track path: u32 kind, u32, u32, f, u32 points, u32 events, 3f position,
                           3f low, 3f high, segments, events (as above)
    u32 n; n x link {u32, u32}
    u32 n; n x region {u32 slot, u32 kind, 3f position, 3f direction, u32 node, u32 path}

Region kind 0 = start grid (slot 0 is the human), kind 1 = session/reset points.
A segment is (dx, dy, slope, w): the horizontal unit direction, the height change
per horizontal cm and the horizontal length w in cm (ssx-web, RACE_EVENT_RECOVERY:
the runtime path bank holds these floats verbatim), so a step moves by
(dx * w, dy * w, slope * w). Event start/end are horizontal distances from the
path origin. A track path's float header field is the remaining race distance at
its origin (ssx-web export_course_initial.py).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field


class AipError(ValueError):
    pass


@dataclass
class Path:
    index: int
    kind: str           # 'ai' or 'track'
    position: tuple
    low: tuple
    high: tuple
    segments: list
    header: tuple
    offset: int = 0
    body: int = 0               # byte offset of `position` inside the record
    events: list = field(default_factory=list)      # [(type, value, start, end)]

    @property
    def remaining_offset(self):
        """Byte offset of a track path's remaining distance (header float), or None."""
        return self.offset + 12 if self.kind == 'track' else None

    def points(self):
        """Polyline (cm). Segments are direction*length steps from `position`."""
        pts = [tuple(self.position)]
        for dx, dy, dz, length in self.segments:
            x, y, z = pts[-1]
            pts.append((x + dx * length, y + dy * length, z + dz * length))
        return pts

    def fits_bounds(self, tol=200.0):
        return all(self.low[k] - tol <= p[k] <= self.high[k] + tol for p in self.points() for k in range(3))

    @property
    def length(self):
        return sum(s[3] for s in self.segments)

    def at(self, distance):
        """(x, y, z), (dx, dy) at `distance` cm along the path (clamped)."""
        pts = self.points()
        remaining = max(0.0, distance)
        for (a, b), seg in zip(zip(pts, pts[1:]), self.segments):
            if remaining <= seg[3] or seg is self.segments[-1]:
                t = min(1.0, remaining / seg[3]) if seg[3] else 0.0
                p = tuple(a[k] + (b[k] - a[k]) * t for k in range(3))
                return p, (seg[0], seg[1])
            remaining -= seg[3]
        return pts[-1], (1.0, 0.0)


@dataclass
class Region:
    slot: int
    kind: int
    position: tuple
    direction: tuple
    node: int
    path: int
    offset: int         # byte offset of `position` inside the record


@dataclass
class Aip:
    magic: int
    ai_paths: list = field(default_factory=list)
    track_paths: list = field(default_factory=list)
    links: list = field(default_factory=list)
    regions: list = field(default_factory=list)

    def start(self):
        grid = sorted((r for r in self.regions if r.kind == 0), key=lambda r: r.slot)
        return grid[0] if grid else None


def decode(data):
    pos = 0

    def read(fmt):
        nonlocal pos
        size = struct.calcsize('<' + fmt)
        if pos + size > len(data):
            raise AipError('truncated AIP record')
        values = struct.unpack_from('<' + fmt, data, pos)
        pos += size
        return values

    def count():
        n, = read('I')
        if n > 100000:
            raise AipError('implausible AIP count')
        return n

    magic, = read('I')
    aip = Aip(magic)
    for index in range(count()):
        offset = pos
        header = read('7I')
        points, events = count(), count()
        body = pos
        position, low, high = read('3f'), read('3f'), read('3f')
        segments = [read('4f') for _ in range(points)]
        evs = [read('IIff') for _ in range(events)]
        aip.ai_paths.append(Path(index, 'ai', position, low, high, segments, header, offset, body, evs))
    for index in range(count()):
        offset = pos
        header = read('IIIf')
        points, events = count(), count()
        body = pos
        position, low, high = read('3f'), read('3f'), read('3f')
        segments = [read('4f') for _ in range(points)]
        evs = [read('IIff') for _ in range(events)]
        aip.track_paths.append(Path(index, 'track', position, low, high, segments, header, offset, body, evs))
    aip.links = [read('II') for _ in range(count())]
    for _ in range(count()):
        start = pos
        slot, kind = read('II')
        values = read('6f')
        node, path = read('II')
        aip.regions.append(Region(slot, kind, values[:3], values[3:], node, path, start + 8))
    if pos != len(data):
        raise AipError(f'{len(data) - pos} unparsed AIP bytes')
    return aip


def shift_regions(buf, record_offset, aip, dz):
    """Move start-grid and session points with the terrain: z += dz(x, y, z). Returns count."""
    moved = 0
    for r in aip.regions:
        x, y, z = r.position
        d = dz(x, y, z)
        if d:
            struct.pack_into('<f', buf, record_offset + r.offset + 8, z + d)
            moved += 1
    return moved


def heading_xy(direction):
    n = math.hypot(direction[0], direction[1]) or 1.0
    return direction[0] / n, direction[1] / n


@dataclass
class Line:
    """The course line: track paths chained end to start, measured from the start grid."""
    points: list
    parts: list             # path indices, in order
    gaps: list              # cm between consecutive parts
    start_offset: float     # distance along `points` of the start grid's projection

    def _cumulative(self):
        acc = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            acc.append(acc[-1] + math.dist(a, b))
        return acc

    @property
    def length(self):
        return self._cumulative()[-1] - self.start_offset

    def at(self, distance):
        """(x, y, z), (dx, dy) at `distance` cm after the start grid (clamped to the line)."""
        target = self.start_offset + max(0.0, distance)
        acc = self._cumulative()
        for k in range(len(self.points) - 1):
            if target <= acc[k + 1] or k == len(self.points) - 2:
                a, b = self.points[k], self.points[k + 1]
                seg = acc[k + 1] - acc[k]
                t = min(1.0, max(0.0, (target - acc[k]) / seg)) if seg else 0.0
                return tuple(a[i] + (b[i] - a[i]) * t for i in range(3)), heading_xy((b[0] - a[0], b[1] - a[1]))
        return self.points[-1], (1.0, 0.0)

    def nearest(self, x, y):
        """(distance after the start, heading) of the line point closest to (x, y)."""
        acc = self._cumulative()
        best = None
        for k in range(len(self.points) - 1):
            a, b = self.points[k], self.points[k + 1]
            dx, dy = b[0] - a[0], b[1] - a[1]
            n = dx * dx + dy * dy
            t = 0.0 if not n else min(1.0, max(0.0, ((x - a[0]) * dx + (y - a[1]) * dy) / n))
            px, py = a[0] + dx * t, a[1] + dy * t
            d = math.hypot(px - x, py - y)
            if best is None or d < best[0]:
                best = (d, acc[k] + t * (acc[k + 1] - acc[k]), heading_xy((dx, dy)))
        return best[1] - self.start_offset, best[2]


def course_line(aip, max_gap=5000.0):
    """Chain the track paths (else the AI paths) into one line from the start grid on.

    The first part is the path passing closest to start-grid slot 0; each next
    part is the unused path whose first point is closest to the current end,
    while that gap stays under `max_gap` cm.
    """
    pool = [p for p in aip.track_paths if p.segments] or [p for p in aip.ai_paths if p.segments]
    if not pool:
        return None
    grid = aip.start()
    anchor = grid.position if grid else pool[0].position
    first = min(pool, key=lambda p: min(math.dist(q, anchor) for q in p.points()))
    chain, gaps, used = [first], [], {first.index}
    while True:
        end = chain[-1].points()[-1]
        options = [(math.dist(p.points()[0], end), p) for p in pool if p.index not in used]
        if not options:
            break
        gap, nxt = min(options, key=lambda o: o[0])
        if gap > max_gap:
            break
        chain.append(nxt)
        gaps.append(gap)
        used.add(nxt.index)
    points = []
    for p in chain:
        pts = p.points()
        if points and math.dist(points[-1], pts[0]) < 1.0:
            pts = pts[1:]
        points.extend(pts)
    line = Line(points, [p.index for p in chain], gaps, 0.0)
    if grid is not None:
        offset, _ = line.nearest(grid.position[0], grid.position[1])
        line.start_offset = offset
    return line
