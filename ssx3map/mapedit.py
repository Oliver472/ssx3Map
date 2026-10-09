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


def place(world, code, along=None, side=0.0, xy=None, start=False, session=None, heading=None):
    """Resolve a placement (metres in, centimetres out)."""
    course = course_aip(world, code)
    line = aipmod.course_line(course) if course else None
    if xy is not None:
        x, y = xy[0] * CM, xy[1] * CM
        h = heading
        if h is None and line is not None:
            _, h = line.nearest(x, y)
        frame = terrain.Frame(x, y, h or (1.0, 0.0))
        what = f'x {xy[0]:.1f} m, y {xy[1]:.1f} m'
    elif start or session is not None:
        kind, slot = (0, 0) if start else (1, session)
        row = next((r for r in (course.regions if course else []) if r.kind == kind and r.slot == slot), None)
        if row is None:
            raise ValueError(f'{code} has no {"start grid" if start else f"session point {session}"}')
        frame = terrain.Frame(row.position[0], row.position[1], aipmod.heading_xy(row.direction))
        what = 'start grid slot 0' if start else f'session point {session}'
    else:
        if line is None:
            raise ValueError(f'{code} has no race line; use --at X Y')
        p, h = line.at((along or 0.0) * CM)
        frame = terrain.Frame(p[0], p[1], h)
        what = f'{along:.0f} m after the start along the course line (of {line.length / CM:.0f} m)'
    if side:
        x, y = frame.world(0.0, side * CM)
        frame = terrain.Frame(x, y, (frame.fx, frame.fy))
        what += f', {side:+.1f} m to the side'
    z = terrain.surface_z((p for _, _, p in patches(world, course_locations(world, code))), frame.x, frame.y)
    return Placement(frame, z, what)


def local_patch_size(world, code, frame, radius=4000.0):
    """Median edge length (cm) of the terrain patches within `radius` of the frame."""
    edges = []
    for _, _, p in patches(world, course_locations(world, code)):
        lo, hi = p.bbox
        if _near(frame, radius, lo, hi):
            c = p.corners()
            edges += [math.dist(c[(0, 0)], c[(1, 0)]), math.dist(c[(0, 0)], c[(0, 1)])]
    edges.sort()
    return edges[len(edges) // 2] if edges else None


@dataclass
class EditReport:
    patches: int = 0
    max_dz: float = 0.0
    objects: int = 0
    objects_skipped: int = 0
    points: int = 0
    rails: int = 0
    rails_in_area: list = field(default_factory=list)      # rails that were not moved
    patch_sizes: list = field(default_factory=list)         # cm, edge lengths of touched patches
    shape_error: float = 0.0                                # cm, worst |achieved - wanted| inside patches


def _near(frame, reach, lo, hi):
    """Does the XY box (lo, hi) come within `reach` of the frame origin?"""
    dx = max(lo[0] - frame.x, 0.0, frame.x - hi[0])
    dy = max(lo[1] - frame.y, 0.0, frame.y - hi[1])
    return math.hypot(dx, dy) <= reach


def _rail_rigid(data, offset, size):
    """Can this rail be moved rigidly? (every segment's row at +0x50 is zero, as on the disc)"""
    if (size - 48) % 144:
        return False
    for k in range((size - 48) // 144):
        if any(struct.unpack_from('<4f', data, offset + 48 + 144 * k + 0x50)):
            return False
    return True


def translate_rail(buf, offset, size, dz):
    """Move a rail record (kind 8) up/down: constant row, segment boxes and record box."""
    for at in (offset + 12, offset + 24):                       # record box min/max z
        z, = struct.unpack_from('<f', buf, at)
        struct.pack_into('<f', buf, at, z + dz)
    for k in range((size - 48) // 144):
        base = offset + 48 + 144 * k
        for at in (base + 0x48, base + 0x74, base + 0x80):      # M3.z, segment box min/max z
            z, = struct.unpack_from('<f', buf, at)
            struct.pack_into('<f', buf, at, z + dz)


def apply_field(world, code, frame, dz, carry_objects=True, move_points=True):
    """Deform the terrain of the course `code` (and its connectors) by dz."""
    report = EditReport()
    reach = dz.reach
    for loc in course_locations(world, code):
        c = main_chunk(loc)
        for rec in world.stream.records(c):
            data = world.stream.current(c)
            if rec.kind == 1 and rec.size == terrain.PATCH_SIZE:
                before = terrain.Patch(data[rec.offset:rec.offset + rec.size])
                lo, hi = before.bbox
                if not _near(frame, reach, lo, hi):
                    continue
                buf = world.stream.chunk(c)
                moved = terrain.deform(buf, rec.offset, dz)
                if moved:
                    report.patches += 1
                    report.max_dz = max(report.max_dz, moved)
                    corners = before.corners()
                    report.patch_sizes += [math.dist(corners[(0, 0)], corners[(1, 0)]),
                                           math.dist(corners[(0, 0)], corners[(0, 1)])]
                    after = terrain.Patch(buf[rec.offset:rec.offset + rec.size])
                    for u in (0.1, 0.3, 0.5, 0.7, 0.9):
                        for v in (0.1, 0.3, 0.5, 0.7, 0.9):
                            p = before.point(u, v)
                            report.shape_error = max(report.shape_error,
                                                     abs(after.point(u, v)[2] - p[2] - dz(*p)))
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
                    instances.translate(world.stream.chunk(c), rec.offset, 0.0, 0.0, d)
                    report.objects += 1
            elif rec.kind == 8 and rec.size >= 48:
                lo = struct.unpack_from('<3f', data, rec.offset + 4)
                hi = struct.unpack_from('<3f', data, rec.offset + 16)
                if not _near(frame, reach, lo, hi):
                    continue
                name = world.name_of(3, rec.track, rec.rid) or f'{loc.name} rail {rec.rid}'
                small = max(hi[0] - lo[0], hi[1] - lo[1]) <= 2 * reach
                d = dz((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]) if carry_objects else 0.0
                if d and small and _rail_rigid(data, rec.offset, rec.size):
                    translate_rail(world.stream.chunk(c), rec.offset, rec.size, d)
                    report.rails += 1
                elif d or not carry_objects:
                    report.rails_in_area.append(name)
            elif rec.kind == 14 and move_points and rec.size:
                record = aipmod.decode(data[rec.offset:rec.offset + rec.size])
                report.points += aipmod.shift_regions(world.stream.chunk(c), rec.offset, record, dz)
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
    line = aipmod.course_line(course) if course else None
    if course is not None:
        palette = ['#ffab40', '#b388ff', '#64ffda', '#ff80ab', '#eeff41', '#80d8ff', '#ccff90', '#ffd180']
        for k, path in enumerate(course.track_paths):
            pts = path.points()
            colour = palette[k % len(palette)]
            out.append(f'<polyline points="{" ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in pts)}" '
                       f'fill="none" stroke="{colour}" stroke-width="1.5" stroke-dasharray="6 4"/>')
            out.append(f'<text x="{X(pts[0][0]) + 6:.1f}" y="{Y(pts[0][1]) + 14:.1f}" fill="{colour}" '
                       f'font-size="12">úsek {path.index} ({path.length / CM:.0f} m)</text>')
    if line is not None:
        out.append(f'<polyline points="{" ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in line.points)}" '
                   f'fill="none" stroke="#ff5252" stroke-width="3" stroke-opacity="0.85"/>')
        mark = 10000.0 if line.length > 60000.0 else 1000.0      # every 100 m (10 m on short lines)
        d = 0.0
        while d <= line.length:
            p, _ = line.at(d)
            out.append(f'<circle cx="{X(p[0]):.1f}" cy="{Y(p[1]):.1f}" r="5" fill="#ff5252"/>')
            out.append(f'<text x="{X(p[0]) + 8:.1f}" y="{Y(p[1]) - 6:.1f}" fill="#ffd54f" font-size="14" '
                       f'font-weight="bold">{d / CM:.0f} m</text>')
            d += mark
    if course is not None:
        for r in course.regions:
            colour = '#00e676' if r.kind == 0 else '#40c4ff'
            label = f'štart {r.slot}' if r.kind == 0 else f'session {r.slot}'
            out.append(f'<circle cx="{X(r.position[0]):.1f}" cy="{Y(r.position[1]):.1f}" r="5" fill="{colour}">'
                       f'<title>{label}  x {r.position[0] / CM:.1f}  y {r.position[1] / CM:.1f}</title></circle>')
            if r.kind == 1 or r.slot == 0:
                out.append(f'<text x="{X(r.position[0]) - 8:.1f}" y="{Y(r.position[1]) + 18:.1f}" fill="{colour}" '
                           f'font-size="13" text-anchor="end">{label}</text>')
    out.append(f'<text x="{margin}" y="{height - 12}" fill="#9aa4ae" font-size="13">červená: celá trať s '
               f'metrami od štartu · prerušované: úseky trasy · zelené bodky: objekty · zelený/modrý krúžok: '
               f'štart a session (reset) body · najeď myšou na plochu pre súradnice</text>')
    out.append('</svg>')
    return '\n'.join(out)
