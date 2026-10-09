"""Geometry edits on a course: placement along the race path, terrain fields
that carry objects and start/reset points with them, object moves, and an SVG
top-down map to pick places from."""
from __future__ import annotations

import html
import math
import struct
from dataclasses import dataclass, field

from . import aip as aipmod
from . import instances, terrain

CM = 100.0      # the CLI talks metres; the data is centimetres


def course_locations(world, code):
    """The course and the connectors that touch it (its event residency)."""
    target = world.location(code)
    out = [target]
    for loc in world.sdb.locations:
        if loc is not target and '_' in loc.name and target.name.upper() in loc.name.upper().split('_'):
            out.append(loc)
    return out


def main_chunk(loc):
    return loc.chunk_end


def course_aip(world, code):
    loc = world.location(code)
    c = main_chunk(loc)
    for rec in world.stream.records(c):
        if rec.kind == 14 and rec.rid == 0 and rec.size:
            return aipmod.decode(world.record_bytes(c, rec))
    return None


def patches(world, locations):
    """[(chunk, record, Patch)] for the main chunks of `locations`."""
    out = []
    for loc in locations:
        c = main_chunk(loc)
        data = world.stream.current(c)
        for rec in world.stream.records(c):
            if rec.kind == 1 and rec.size == terrain.PATCH_SIZE:
                out.append((c, rec, terrain.Patch(data[rec.offset:rec.offset + rec.size])))
    return out


@dataclass
class Placement:
    frame: terrain.Frame
    z: float
    description: str


def place(world, code, along=None, side=0.0, xy=None, start=False, heading=None):
    """Resolve a placement (metres in, centimetres out)."""
    course = course_aip(world, code)
    path = course.main_path() if course else None
    if xy is not None:
        x, y = xy[0] * CM, xy[1] * CM
        h = heading
        if h is None and path is not None:
            # Heading of the nearest path vertex.
            pts = path.points()
            k = min(range(len(path.segments)), key=lambda i: math.hypot(pts[i][0] - x, pts[i][1] - y))
            h = path.segments[k][:2]
        frame = terrain.Frame(x, y, h or (1.0, 0.0))
        what = f'x {xy[0]:.1f} m, y {xy[1]:.1f} m'
    elif start:
        grid = course.start() if course else None
        if grid is None:
            raise ValueError(f'{code} has no start grid')
        frame = terrain.Frame(grid.position[0], grid.position[1], aipmod.heading_xy(grid.direction))
        what = 'start grid slot 0'
    else:
        if path is None:
            raise ValueError(f'{code} has no race path; use --at X Y')
        p, h = path.at((along or 0.0) * CM)
        frame = terrain.Frame(p[0], p[1], h)
        what = f'{along:.0f} m along the course line (of {path.length / CM:.0f} m)'
    if side:
        x, y = frame.world(0.0, side * CM)
        frame = terrain.Frame(x, y, (frame.fx, frame.fy))
        what += f', {side:+.1f} m to the side'
    z = terrain.surface_z((p for _, _, p in patches(world, course_locations(world, code))), frame.x, frame.y)
    return Placement(frame, z, what)


@dataclass
class EditReport:
    patches: int = 0
    max_dz: float = 0.0
    objects: int = 0
    objects_skipped: int = 0
    points: int = 0
    rails_in_area: list = field(default_factory=list)


def _near(frame, reach, lo, hi):
    """Does the XY box (lo, hi) come within `reach` of the frame origin?"""
    dx = max(lo[0] - frame.x, 0.0, frame.x - hi[0])
    dy = max(lo[1] - frame.y, 0.0, frame.y - hi[1])
    return math.hypot(dx, dy) <= reach


def apply_field(world, code, frame, dz, carry_objects=True, move_points=True):
    """Deform the terrain of the course `code` (and its connectors) by dz."""
    report = EditReport()
    reach = dz.reach
    for loc in course_locations(world, code):
        c = main_chunk(loc)
        buf = None
        for rec in world.stream.records(c):
            data = world.stream.current(c)
            if rec.kind == 1 and rec.size == terrain.PATCH_SIZE:
                lo, hi = terrain.Patch(data[rec.offset:rec.offset + rec.size]).bbox
                if not _near(frame, reach, lo, hi):
                    continue
                buf = world.stream.chunk(c)
                moved = terrain.deform(buf, rec.offset, dz)
                if moved:
                    report.patches += 1
                    report.max_dz = max(report.max_dz, moved)
            elif rec.kind == 3 and carry_objects and rec.size >= 0x90:
                inst = instances.Instance(data[rec.offset:rec.offset + rec.size])
                cx, cy, _ = inst.centre
                lo, hi = inst.bbox
                if not _near(frame, reach, lo, hi):
                    continue
                size = inst.size
                if max(size[0], size[1]) > 2 * reach:
                    report.objects_skipped += 1     # bigger than the edit: leave it
                    continue
                d = dz(cx, cy, lo[2])
                if d:
                    buf = world.stream.chunk(c)
                    instances.translate(buf, rec.offset, 0.0, 0.0, d)
                    report.objects += 1
            elif rec.kind == 8 and rec.size >= 48:
                lo = struct.unpack_from('<3f', data, rec.offset + 4)
                hi = struct.unpack_from('<3f', data, rec.offset + 16)
                if _near(frame, reach, lo, hi):
                    name = world.name_of(3, rec.track, rec.rid) or f'{loc.name} rail {rec.rid}'
                    report.rails_in_area.append(name)
            elif rec.kind == 14 and move_points and rec.size:
                record = aipmod.decode(data[rec.offset:rec.offset + rec.size])
                buf = world.stream.chunk(c)
                report.points += aipmod.shift_regions(buf, rec.offset, record, dz)
    return report


def find_objects(world, code, name=None, frame=None, radius=None):
    """[(chunk, record, Instance, name)] of the course's objects matching the filters."""
    out = []
    for loc in course_locations(world, code):
        c = main_chunk(loc)
        data = world.stream.current(c)
        for rec in world.stream.records(c):
            if rec.kind != 3 or rec.size < 0x90:
                continue
            inst = instances.Instance(data[rec.offset:rec.offset + rec.size])
            label = world.name_of(1, rec.track, rec.rid) or f'{loc.name}#{rec.rid}'
            if name and name.lower() not in label.lower():
                continue
            if frame is not None and not _near(frame, radius, *inst.bbox):
                continue
            out.append((c, rec, inst, label))
    return out


# --------------------------------------------------------------------------
# SVG map
# --------------------------------------------------------------------------

def _ramp(t):
    """Height colour: deep blue (low) -> pale cyan -> white (high)."""
    stops = [(0.0, (30, 60, 120)), (0.5, (120, 170, 200)), (1.0, (245, 248, 250))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            f = (t - a) / (b - a) if b > a else 0
            return tuple(int(ca[k] + (cb[k] - ca[k]) * f) for k in range(3))
    return stops[-1][1]


def svg_map(world, code, objects=True, size=1800):
    locs = course_locations(world, code)
    pts = patches(world, locs)
    if not pts:
        raise ValueError(f'{code} has no terrain')
    polys = []
    for _, rec, p in pts:
        corner = [p.point(0, 0), p.point(1, 0), p.point(1, 1), p.point(0, 1)]
        polys.append((rec, corner))
    xs = [q[0] for _, c in polys for q in c]
    ys = [q[1] for _, c in polys for q in c]
    zs = [q[2] for _, c in polys for q in c]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    minz, maxz = min(zs), max(zs)
    margin = 60
    scale = (size - 2 * margin) / max(maxx - minx, maxy - miny)
    width = int((maxx - minx) * scale) + 2 * margin
    height = int((maxy - miny) * scale) + 2 * margin

    def X(x):
        return margin + (x - minx) * scale

    def Y(y):
        return margin + (maxy - y) * scale

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="sans-serif">',
           f'<rect width="{width}" height="{height}" fill="#1b1f24"/>',
           f'<text x="{margin}" y="28" fill="#fff" font-size="20">{html.escape(code)}: '
           f'{", ".join(l.name for l in locs)} · výška {minz / CM:.0f}..{maxz / CM:.0f} m · '
           f'súradnice v metroch</text>']
    # 100 m grid with labels.
    step = 10000.0
    gx = math.floor(minx / step) * step
    while gx <= maxx:
        out.append(f'<line x1="{X(gx):.1f}" y1="{margin}" x2="{X(gx):.1f}" y2="{height - margin}" '
                   f'stroke="#3a414a" stroke-width="1"/>')
        out.append(f'<text x="{X(gx):.1f}" y="{height - margin + 18}" fill="#9aa4ae" font-size="12" '
                   f'text-anchor="middle">{gx / CM:.0f}</text>')
        gx += step
    gy = math.floor(miny / step) * step
    while gy <= maxy:
        out.append(f'<line x1="{margin}" y1="{Y(gy):.1f}" x2="{width - margin}" y2="{Y(gy):.1f}" '
                   f'stroke="#3a414a" stroke-width="1"/>')
        out.append(f'<text x="{margin - 6}" y="{Y(gy) + 4:.1f}" fill="#9aa4ae" font-size="12" '
                   f'text-anchor="end">{gy / CM:.0f}</text>')
        gy += step
    out.append('<g stroke="#000" stroke-opacity="0.25" stroke-width="0.5">')
    for rec, c in polys:
        z = sum(q[2] for q in c) / 4
        col = _ramp((z - minz) / ((maxz - minz) or 1))
        cx, cy = sum(q[0] for q in c) / 4, sum(q[1] for q in c) / 4
        out.append(f'<polygon points="{" ".join(f"{X(q[0]):.1f},{Y(q[1]):.1f}" for q in c)}" '
                   f'fill="rgb{col}"><title>x {cx / CM:.1f}  y {cy / CM:.1f}  výška {z / CM:.1f} m</title></polygon>')
    out.append('</g>')
    if objects:
        out.append('<g fill="#2e7d32" fill-opacity="0.8">')
        for c, rec, inst, label in find_objects(world, code):
            x, y, z = inst.centre
            r = max(1.5, min(6.0, max(inst.size[0], inst.size[1]) * scale / 2))
            out.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="{r:.1f}">'
                       f'<title>{html.escape(label)}  x {x / CM:.1f}  y {y / CM:.1f}</title></circle>')
        out.append('</g>')
    course = course_aip(world, code)
    path = course.main_path() if course else None
    if path is not None:
        line = path.points()
        out.append(f'<polyline points="{" ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in line)}" '
                   f'fill="none" stroke="#ff5252" stroke-width="3"/>')
        mark = 10000.0 if path.length > 60000.0 else 1000.0      # every 100 m (10 m on short lines)
        d = 0.0
        while d <= path.length:
            p, _ = path.at(d)
            out.append(f'<circle cx="{X(p[0]):.1f}" cy="{Y(p[1]):.1f}" r="5" fill="#ff5252"/>')
            out.append(f'<text x="{X(p[0]) + 8:.1f}" y="{Y(p[1]) - 6:.1f}" fill="#ffd54f" font-size="14" '
                       f'font-weight="bold">{d / CM:.0f} m</text>')
            d += mark
    if course is not None:
        for r in course.regions:
            colour = '#00e676' if r.kind == 0 else '#40c4ff'
            out.append(f'<circle cx="{X(r.position[0]):.1f}" cy="{Y(r.position[1]):.1f}" r="4" fill="{colour}">'
                       f'<title>{"štart" if r.kind == 0 else "reset/session"} {r.slot}</title></circle>')
    out.append(f'<text x="{margin}" y="{height - 12}" fill="#9aa4ae" font-size="13">červená: trať s '
               f'metrami od štartu · zelené body: objekty · zelené/modré krúžky: štart a reset body · '
               f'najeď myšou na plochu pre súradnice</text>')
    out.append('</svg>')
    return '\n'.join(out)
