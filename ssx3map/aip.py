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
A segment is (dx, dy, dz, length): a unit direction and a length in cm (checked
against each path's bounds, see `Path.points`).
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

    def main_path(self):
        """The longest track path (the course line), else the longest AI path."""
        pool = self.track_paths or self.ai_paths
        if not pool:
            return None
        return max(pool, key=lambda p: p.length)

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
        position, low, high = read('3f'), read('3f'), read('3f')
        segments = [read('4f') for _ in range(points)]
        for _ in range(events):
            read('IIff')
        aip.ai_paths.append(Path(index, 'ai', position, low, high, segments, header, offset))
    for index in range(count()):
        offset = pos
        header = read('IIIf')
        points, events = count(), count()
        position, low, high = read('3f'), read('3f'), read('3f')
        segments = [read('4f') for _ in range(points)]
        for _ in range(events):
            read('IIff')
        aip.track_paths.append(Path(index, 'track', position, low, high, segments, header, offset))
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
