"""Command line: python -m ssx3map <command> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import aip as aipmod
from . import instances, mapedit, painter, terrain, texture
from .world import KIND_NAMES, World, resolve_input


def _open(path):
    t = time.time()
    try:
        resolved = resolve_input(path)
    except (OSError, ValueError) as e:
        raise SystemExit(f'error: {e}')
    w = World(resolved)
    print(f'loaded {w.source} ({"disc image" if w.is_iso else "BAM.BIG"}): {len(w.sdb.locations)} locations, '
          f'{len(w.stream)} chunks, {len(w.stream.blocks)} blocks ({time.time() - t:.1f}s)', file=sys.stderr)
    return w


def _check_output(args, w):
    if not args.output:
        return
    ext = os.path.splitext(args.output)[1].lower()
    if w.is_iso and ext != '.iso':
        raise SystemExit('the input is a disc image, so the output must be a .iso file')
    if not w.is_iso and ext == '.iso':
        raise SystemExit('the input is BAM.BIG, so the output must be a .BIG file')


def _save(args, w):
    changed = w.stream.changed_chunks()
    if not changed:
        print('nothing changed; no file written')
        return
    t = time.time()
    report = w.save(args.output, progress=(lambda m: print('  ' + m, file=sys.stderr)) if args.verbose else None)
    padded = sum(1 for r in report if r['used_padding'])
    print(f'wrote {args.output}: {len(changed)} chunks, {len(report)} blocks re-encoded'
          f'{f", {padded} using block padding" if padded else ""} ({time.time() - t:.1f}s); '
          'block layout identical to the original, all changed chunks verified')


def cmd_info(args):
    w = _open(args.input)
    d = w.sdb
    print(f'world textures {d.texture_count}, light pages {d.light_page_count}')
    print('track location   chunks')
    for loc in d.locations:
        print(f'{loc.index:5d} {loc.name:10s} {loc.first_chunk:4d}..{loc.chunk_end:<4d}')


def cmd_inspect(args):
    from .inspect import Report
    w = _open(args.input)
    lines = []

    def out(line):
        print(line, flush=True)
        lines.append(line)
    data = Report(w, sample_blocks=args.sample_blocks, out=out).run()
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(data, f, indent=1, default=str)
        print(f'details written to {args.json}')


def cmd_list(args):
    w = _open(args.input)
    kind = args.kind
    for c, r in w.records(kind=kind, location=args.location):
        group = {1: 0, 3: 1, 2: 2, 8: 3, 12: 4}.get(r.kind)
        name = w.name_of(group, r.track, r.rid) if group is not None else None
        print(f'chunk {c:4d} {w.chunk_label(c):10s} kind {r.kind:2d} {KIND_NAMES.get(r.kind, "?"):26s} '
              f'track {r.track:3d} rid {r.rid:6d} size {r.size:7d}{"  " + name if name else ""}')


def _painter_targets(w, args):
    if args.all:
        return list(w.sdb.locations)
    if not args.location:
        raise SystemExit('give --location NAME (repeatable) or --all')
    return [w.location(n) for n in args.location]


def cmd_fog(args):
    w = _open(args.input)
    _check_output(args, w)
    changes = {}
    if args.color:
        changes.update(r=args.color[0], g=args.color[1], b=args.color[2])
    if args.near is not None:
        changes['near_cm'] = args.near
    if args.far is not None:
        changes['far_cm'] = args.far
    if args.density is not None:
        changes['density'] = args.density
    if args.scale_distance is not None:
        f = args.scale_distance
        changes['near_cm'] = lambda v, f=f: v * f
        changes['far_cm'] = lambda v, f=f: v * f
    if changes and not args.output:
        raise SystemExit('give -o OUTPUT to write the change (without values the command only shows fog)')
    for loc in _painter_targets(w, args):
        found = False
        for c, r in w.records(kind=15, location=loc):
            found = True
            if changes:
                buf = w.stream.chunk(c)
                for before, after in painter.edit_fog(buf, r.offset, r.size, changes):
                    print(f'{loc.name:10s} chunk {c}: near {before["near_cm"]:.0f}->{after["near_cm"]:.0f} '
                          f'far {before["far_cm"]:.0f}->{after["far_cm"]:.0f} colour '
                          f'({before["r"]:.2f},{before["g"]:.2f},{before["b"]:.2f})->'
                          f'({after["r"]:.2f},{after["g"]:.2f},{after["b"]:.2f})')
            else:
                for _, fog in painter.fog_entries(w.record_bytes(c, r)):
                    print(f'{loc.name:10s} chunk {c}: ' + ', '.join(f'{k} {v:.4g}' for k, v in fog.items()))
        if not found:
            print(f'{loc.name}: no painter record', file=sys.stderr)
    if changes:
        _save(args, w)


def _texture_ids(w, args):
    ids = set(args.texture or [])
    if args.location:
        for name in args.location:
            loc = w.location(name)
            for c, r in w.records(kind=9, location=loc):
                ids.add(r.rid)
            # Textures are also named by the location's materials and terrain.
            for c, r in w.records(kind=0, location=loc):
                data = w.record_bytes(c, r)
                if len(data) >= 2:
                    ids.add(int.from_bytes(data[:2], 'little', signed=True))
            for c, r in w.records(kind=1, location=loc):
                data = w.record_bytes(c, r)
                if len(data) >= 0x1A2:
                    ids.add(int.from_bytes(data[0x1A0:0x1A2], 'little', signed=True))
    ids.discard(-1)
    if not ids:
        raise SystemExit('give --texture ID (repeatable) and/or --location NAME')
    return ids


def cmd_tint(args):
    w = _open(args.input)
    _check_output(args, w)
    if not args.output:
        raise SystemExit('give -o OUTPUT')
    ids = _texture_ids(w, args)
    mul = tuple(args.rgb) if args.rgb else (1.0, 1.0, 1.0)
    add = tuple(args.add) if args.add else (0, 0, 0)
    copies = colours = 0
    seen = set()
    for c, r in w.records(kind=9):
        if r.rid not in ids:
            continue
        try:
            colours += texture.tint(w.stream.chunk(c), r.offset, r.size, mul, add)
        except texture.TextureError as e:
            print(f'texture {r.rid} in chunk {c} skipped: {e}', file=sys.stderr)
            continue
        copies += 1
        seen.add(r.rid)
    missing = sorted(ids - seen)
    print(f'tinted {len(seen)} textures ({copies} copies in the stream, {colours} colours changed)'
          + (f'; not found: {missing[:20]}' if missing else ''))
    _save(args, w)


def cmd_textures(args):
    w = _open(args.input)
    os.makedirs(args.export, exist_ok=True)
    wanted = set(args.texture or [])
    done = set()
    failed = 0
    for c, r in w.records(kind=9, location=args.location[0] if args.location else None):
        if r.rid in done or (wanted and r.rid not in wanted):
            continue
        data = w.record_bytes(c, r)
        try:
            width, height, rgba = texture.decode_rgba(data)
        except texture.TextureError as e:
            print(f'texture {r.rid}: {e}', file=sys.stderr)
            failed += 1
            continue
        texture.write_png(os.path.join(args.export, f'tex_{r.rid:04d}_{width}x{height}.png'), width, height, rgba)
        done.add(r.rid)
    print(f'exported {len(done)} textures to {args.export}' + (f' ({failed} failed)' if failed else ''))


def _placement(w, args):
    return mapedit.place(w, args.location, along=args.along, side=args.side or 0.0,
                         xy=tuple(args.at) if args.at else None, start=args.start, session=args.session)


def _describe(pl):
    z = f'{pl.z / 100:.1f} m' if pl.z is not None else 'mimo terénu'
    return (f'{pl.description}: x {pl.frame.x / 100:.1f} m, y {pl.frame.y / 100:.1f} m, výška terénu {z}, '
            f'smer ({pl.frame.fx:.2f}, {pl.frame.fy:.2f})')


def cmd_map(args):
    w = _open(args.input)
    svg = mapedit.svg_map(w, args.location, objects=not args.no_objects)
    out = args.output or f'{args.location}.svg'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(svg)
    course = mapedit.course_aip(w, args.location)
    line = aipmod.course_line(course) if course else None
    print(f'wrote {out}' + (f'; trať {line.length / 100:.0f} m z úsekov {line.parts} '
                            f'(medzery {[round(g / 100, 1) for g in line.gaps]} m)' if line else ''))


SHAPES = ('bump', 'plateau', 'kicker', 'flatten')


def cmd_terrain(args):
    w = _open(args.input)
    _check_output(args, w)
    if not args.output:
        raise SystemExit('give -o OUTPUT')
    if abs(args.height) > 60 and not args.force:
        raise SystemExit('heights beyond 60 m are refused without --force')
    pl = _placement(w, args)
    print('miesto: ' + _describe(pl))
    f, cm = pl.frame, 100.0
    local = mapedit.local_patch_size(w, args.location, f)
    if local is None:
        raise SystemExit('no terrain near that place; nothing written')
    unit = local / cm
    # Defaults scale with the terrain resolution: features under ~1.5 patches come out smeared.
    radius = args.radius or max(12.0, 2.0 * unit)
    edge = args.edge or max(5.0, 1.5 * unit)
    length = args.length or max(20.0, 3.0 * unit)
    width = args.width or max(12.0, 2.0 * unit)
    drop = args.drop or max(8.0, 1.5 * unit)
    if args.shape == 'bump':
        dz = terrain.bump(f, args.height * cm, radius * cm)
        dims = f'polomer {radius:.0f} m'
    elif args.shape == 'plateau':
        dz = terrain.plateau(f, args.height * cm, radius * cm, edge * cm)
        dims = f'polomer {radius:.0f} m, okraj {edge:.0f} m'
    elif args.shape == 'kicker':
        dz = terrain.kicker(f, args.height * cm, length * cm, width * cm, drop * cm, edge * cm)
        dims = f'nájazd {length:.0f} m, šírka {width:.0f} m, dopad {drop:.0f} m, okraj {edge:.0f} m'
    else:
        if pl.z is None:
            raise SystemExit('flatten needs a point on the terrain')
        dz = terrain.flatten_to(f, pl.z + args.height * cm, radius * cm, edge * cm)
        dims = f'polomer {radius:.0f} m, okraj {edge:.0f} m'
    print(f'tvar: {args.shape} {args.height:+.1f} m, {dims} (pláty tu majú okolo {unit:.1f} m)')
    report = mapedit.apply_field(w, args.location, f, dz, carry_objects=not args.no_carry,
                                 move_points=not args.no_carry)
    if not report.patches:
        raise SystemExit('no terrain patch was changed (the place is off the terrain, or the shape falls '
                         'between the sample points of one patch); nothing written')
    sizes = sorted(report.patch_sizes)
    typical = sizes[len(sizes) // 2] / cm
    print(f'terén: {report.patches} plátov (typický plát {typical:.1f} m), najväčší posun '
          f'{report.max_dz / cm:.2f} m, odchýlka tvaru do {report.shape_error / cm:.2f} m; objekty posunuté '
          f'{report.objects} (ponechané veľké {report.objects_skipped}); zábradlia {report.rails}; '
          f'štart/reset body {report.points}')
    feature = {'bump': radius * 2, 'plateau': edge, 'kicker': min(length, drop, width), 'flatten': edge}[args.shape]
    if feature < 1.5 * typical and not args.force:
        raise SystemExit(f'the shape ({feature:.0f} m) is too small for patches of about {typical:.0f} m here: '
                         f'it would come out smeared. Make it at least {1.5 * typical:.0f} m '
                         f'(--length/--drop/--width/--radius/--edge) or pass --force; nothing written')
    if report.rails_in_area:
        print(f'POZOR: {len(report.rails_in_area)} zábradlí/rails v oblasti sa neposunulo (sú väčšie ako úprava): '
              + ', '.join(report.rails_in_area[:8]), file=sys.stderr)
    _save(args, w)


PROTECTED = ('trig', 'reset', 'fence', 'collide', 'nis_', 'transport', 'lodge', 'skybox', 'load', 'finish', 'start')


def cmd_objects(args):
    w = _open(args.input)
    _check_output(args, w)
    frame = None
    if args.along is not None or args.at or args.start:
        pl = _placement(w, args)
        frame = pl.frame
        print('okolie: ' + _describe(pl) + f', polomer {args.radius:.0f} m')
    found = mapedit.find_objects(w, args.location, name=args.name, frame=frame,
                                 radius=args.radius * 100 if frame else None)
    acting = args.remove or args.move or args.raise_by is not None
    for c, rec, inst, label in found:
        x, y, z = inst.centre
        sx, sy, sz = inst.size
        print(f'{label:48s} x {x / 100:9.1f} y {y / 100:9.1f} z {z / 100:8.1f}  rozmer {sx / 100:.1f}x{sy / 100:.1f}x{sz / 100:.1f} m')
    print(f'{len(found)} objektov')
    if not acting:
        return
    if not args.output:
        raise SystemExit('give -o OUTPUT')
    done = skipped = 0
    for c, rec, inst, label in found:
        if not args.force and any(k in label.lower() for k in PROTECTED):
            skipped += 1
            continue
        if args.remove:
            d = (0.0, 0.0, -100000.0)          # 1 km under the mountain, collision included
        elif args.move:
            d = tuple(v * 100 for v in args.move)
        else:
            d = (0.0, 0.0, args.raise_by * 100)
        instances.translate(w.stream.chunk(c), rec.offset, *d)
        done += 1
    print(f'{"odstránené" if args.remove else "posunuté"}: {done}'
          + (f'; vynechané herné pomocné objekty (štart, triggery, resety...): {skipped} (--force ich zahrnie)'
             if skipped else ''))
    if done:
        _save(args, w)


def main(argv=None):
    p = argparse.ArgumentParser(prog='ssx3map', description='SSX 3 (PS2) world editor: edits BAM.BIG in place.')
    sub = p.add_subparsers(dest='command', required=True)

    def add(name, fn, help_text):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.add_argument('input', help='the disc image (.iso), BAM.BIG, or a folder holding either')
        sp.set_defaults(fn=fn)
        return sp

    add('info', cmd_info, 'list locations and their chunks')

    sp = add('inspect', cmd_inspect, 'structural report of the world data (send it to the developer)')
    sp.add_argument('--json', help='also write details as JSON')
    sp.add_argument('--sample-blocks', type=int, default=24, help='blocks to re-encode in the encoder test')

    sp = add('list', cmd_list, 'list world records')
    sp.add_argument('--location', help='location code, e.g. ARA1')
    sp.add_argument('--kind', type=int, help='record kind (9 texture, 15 painter, ...)')

    sp = add('fog', cmd_fog, 'show or change painted fog')
    sp.add_argument('--location', action='append', help='location code (repeatable), e.g. ARA1')
    sp.add_argument('--all', action='store_true', help='every location')
    sp.add_argument('--color', type=float, nargs=3, metavar=('R', 'G', 'B'), help='fog colour, 0..1')
    sp.add_argument('--near', type=float, help='fog start in cm')
    sp.add_argument('--far', type=float, help='fog end in cm')
    sp.add_argument('--density', type=float)
    sp.add_argument('--scale-distance', type=float, help='multiply near and far')
    sp.add_argument('-o', '--output', help='output .iso or .BIG')
    sp.add_argument('-v', '--verbose', action='store_true')

    sp = add('tint', cmd_tint, 'recolour world textures (palette, so every mip level too)')
    sp.add_argument('--texture', type=int, action='append', help='texture id (repeatable)')
    sp.add_argument('--location', action='append', help='every texture used by this location')
    sp.add_argument('--rgb', type=float, nargs=3, metavar=('R', 'G', 'B'), help='multipliers, e.g. 1 0.6 0.8')
    sp.add_argument('--add', type=int, nargs=3, metavar=('R', 'G', 'B'), help='offsets, -255..255')
    sp.add_argument('-o', '--output', help='output .iso or .BIG')
    sp.add_argument('-v', '--verbose', action='store_true')

    sp = add('textures', cmd_textures, 'export world textures as PNG')
    sp.add_argument('--export', required=True, help='output folder')
    sp.add_argument('--texture', type=int, action='append')
    sp.add_argument('--location', action='append')

    def placement_args(sp):
        sp.add_argument('--location', required=True, help='course code, e.g. ARA1 (Snow Jam)')
        sp.add_argument('--along', type=float, help='metres along the course line from the start')
        sp.add_argument('--side', type=float, help='metres to the right (negative: left) of that point')
        sp.add_argument('--at', type=float, nargs=2, metavar=('X', 'Y'), help='world position in metres (from the map)')
        sp.add_argument('--start', action='store_true', help='at the start grid')
        sp.add_argument('--session', type=int, metavar='K', help='at session (reset) point K, see the map')

    sp = add('map', cmd_map, 'draw a course from above as SVG (open it in a browser)')
    sp.add_argument('--location', required=True)
    sp.add_argument('--no-objects', action='store_true')
    sp.add_argument('-o', '--output', help='output .svg')

    sp = add('terrain', cmd_terrain, 'reshape the terrain (riders collide with it)')
    placement_args(sp)
    sp.add_argument('--shape', choices=SHAPES, required=True)
    sp.add_argument('--height', type=float, required=True, help='metres (negative lowers)')
    sp.add_argument('--radius', type=float, help='metres (bump, plateau, flatten)')
    sp.add_argument('--length', type=float, help='kicker run-up, metres')
    sp.add_argument('--width', type=float, help='kicker width, metres')
    sp.add_argument('--drop', type=float, help='kicker landing side, metres')
    sp.add_argument('--edge', type=float, help='blend width at the borders, metres')
    sp.add_argument('--no-carry', action='store_true', help='do not move objects and start/reset points along')
    sp.add_argument('--force', action='store_true')
    sp.add_argument('-o', '--output', help='output .iso or .BIG')
    sp.add_argument('-v', '--verbose', action='store_true')

    sp = add('objects', cmd_objects, 'list, move or remove objects (their collision moves with them)')
    placement_args(sp)
    sp.add_argument('--radius', type=float, default=20.0, help='metres around the place (default 20)')
    sp.add_argument('--name', help='only objects whose name contains this')
    sp.add_argument('--remove', action='store_true', help='remove (sink 1 km under the mountain)')
    sp.add_argument('--move', type=float, nargs=3, metavar=('DX', 'DY', 'DZ'), help='move by metres')
    sp.add_argument('--raise', dest='raise_by', type=float, metavar='DZ', help='move up/down by metres')
    sp.add_argument('--force', action='store_true', help='also touch start/trigger/reset helpers')
    sp.add_argument('-o', '--output', help='output .iso or .BIG')
    sp.add_argument('-v', '--verbose', action='store_true')

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == '__main__':
    main()
