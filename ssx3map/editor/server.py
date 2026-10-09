"""Local HTTP server for the browser map editor.

The browser never touches game files: it asks this server for a course (terrain
patches, objects, the race line...), sends edit requests, and asks it to save.
All edits go through the same functions as the command line, so the same
guarantees hold (same-size records, verified re-encoding, untouched offsets).
The server only listens on 127.0.0.1.
"""
from __future__ import annotations

import json
import mimetypes
import os
import struct
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import aip as aipmod
from .. import mapedit, ssb, terrain, texture

STATIC = os.path.join(os.path.dirname(__file__), 'static')
UNDO_LIMIT = 25
COURSES = ['ARA1', 'BRA2', 'CRA3', 'DRA4', 'ERA5', 'ASS1', 'DSS2', 'ESS3', 'ABA1', 'CBA2', 'EBA3',
           'BHP1', 'CHP2', 'EHP3', 'ABC1', 'DBC2', 'EBC3']
NAMES = {'ARA1': 'Snow Jam', 'BRA2': 'Metro-City', 'CRA3': 'Ruthless Ridge', 'DRA4': 'Intimidator',
         'ERA5': 'Gravitude', 'ASS1': 'R&B', 'DSS2': 'Style Mile', 'ESS3': 'Kick Doubt', 'ABA1': "Crow's Nest",
         'CBA2': 'Launch Time', 'EBA3': 'Much-2-Much', 'BHP1': 'The Junction', 'CHP2': 'Schizophrenia',
         'EHP3': 'Perpendiculous', 'ABC1': 'Happiness', 'DBC2': 'Ruthless', 'EBC3': 'The Throne'}


def _r(values, nd=2):
    return [round(v, nd) for v in values]


def _key(chunk, rec):
    return f'{chunk}:{rec.offset}'


class Session:
    def __init__(self, world):
        self.world = world
        self.lock = threading.RLock()
        self.undo = []              # [(label, snapshot)]
        self.textures = {}          # rid -> record bytes
        self.pngs = {}              # rid -> png bytes (or None if undecodable)
        self.scanned = set()        # chunks already indexed for textures
        self.full_scan = False
        self.edits = 0

    # -- reading ----------------------------------------------------------------
    def info(self):
        w = self.world
        codes = {l.name for l in w.sdb.locations}
        base, _ = os.path.splitext(w.source)
        return dict(source=w.source, iso=w.is_iso,
                    courses=([dict(code=c, name=NAMES.get(c, c)) for c in COURSES if c in codes]
                             or [dict(code=l.name, name=l.name) for l in w.sdb.locations]),
                    locations=[l.name for l in w.sdb.locations],
                    undo=[label for label, _ in self.undo], edits=self.edits,
                    output=base + ('_upravene.iso' if w.is_iso else '_upravene.BIG'))

    def course(self, code):
        w = self.world
        with self.lock:
            locs = mapedit.course_locations(w, code)
            patches = []
            for c, rec, p in mapedit.patches(w, locs):
                coeff = [v for j in range(4) for i in range(4) for v in p.c[j][i]]
                patches.append(dict(k=_key(c, rec), c=_r(coeff, 3), t=p.texture,
                                    uv=_r(struct.unpack_from('<8f', p.data, 0x20), 4), f=p.flags))
            objects = []
            for c, rec, inst, name in mapedit.find_objects(w, code):
                lo, hi = inst.bbox
                objects.append(dict(k=_key(c, rec), n=name, lo=_r(lo), hi=_r(hi),
                                    p=mapedit.is_protected(name)))
            course = mapedit.course_aip(w, code)
            line = aipmod.course_line(course) if course else None
            regions = [dict(kind=r.kind, slot=r.slot, p=_r(r.position), d=_r(r.direction, 4))
                       for r in (course.regions if course else [])]
            rails = []
            for loc in locs:
                c = mapedit.main_chunk(loc)
                data = w.stream.current(c)
                for rec in w.stream.records(c):
                    if rec.kind != 8 or rec.size < 48 or (rec.size - 48) % 144:
                        continue
                    pts = []
                    for k in range((rec.size - 48) // 144):
                        rows = [struct.unpack_from('<3f', data, rec.offset + 48 + 144 * k + 0x10 + 16 * r)
                                for r in range(4)]
                        for t in ((0.0, 0.25, 0.5, 0.75, 1.0) if k == 0 else (0.25, 0.5, 0.75, 1.0)):
                            pts.append(_r([rows[0][a] * t ** 3 + rows[1][a] * t * t + rows[2][a] * t + rows[3][a]
                                           for a in range(3)]))
                    rails.append(dict(n=w.name_of(3, rec.track, rec.rid) or f'{loc.name} rail {rec.rid}', pts=pts))
            return dict(code=code, name=NAMES.get(code, code), locations=[l.name for l in locs],
                        patches=patches, objects=objects, regions=regions, rails=rails,
                        line=(dict(pts=[_r(p) for p in line.points], offset=round(line.start_offset, 2),
                                   length=round(line.length, 2)) if line else None),
                        undo=[label for label, _ in self.undo], edits=self.edits)

    def _index_textures(self, chunks):
        s = self.world.stream
        for c in chunks:
            if c in self.scanned:
                continue
            self.scanned.add(c)
            data = s.current(c, keep=False)
            for rec in ssb.parse_records(data):
                if rec.kind == 9 and rec.rid not in self.textures:
                    self.textures[rec.rid] = bytes(data[rec.offset:rec.offset + rec.size])

    def texture_png(self, rid, code=None):
        with self.lock:
            if rid in self.pngs:
                return self.pngs[rid]
            if rid not in self.textures and code:
                chunks = []
                for loc in mapedit.course_locations(self.world, code):
                    chunks += list(loc.chunks)
                self._index_textures(chunks)
            if rid not in self.textures and not self.full_scan:
                self.full_scan = True
                self._index_textures(range(len(self.world.stream)))
            data = self.textures.get(rid)
            png = None
            if data is not None:
                try:
                    png = texture.encode_png(*texture.decode_rgba(data), level=1)
                except texture.TextureError:
                    png = None
            self.pngs[rid] = png
            return png

    # -- editing ----------------------------------------------------------------
    def _push(self, label, snap):
        self.undo.append((label, snap))
        del self.undo[:-UNDO_LIMIT]
        self.edits += 1

    def terrain(self, req):
        w = self.world
        code = req['code']
        with self.lock:
            x, y = float(req['x']), float(req['y'])
            heading = req.get('heading')
            course = mapedit.course_aip(w, code)
            line = aipmod.course_line(course) if course else None
            if not heading:
                heading = line.nearest(x, y)[1] if line else (1.0, 0.0)
            frame = terrain.Frame(x, y, heading)
            pts = [p for _, _, p in mapedit.patches(w, mapedit.course_locations(w, code))]
            z = terrain.surface_z(pts, x, y)
            snap = w.stream.snapshot(mapedit.course_chunks(w, code))
            sizes = {k: (float(req[k]) if req.get(k) not in (None, '', 0) else None)
                     for k in ('radius', 'edge', 'length', 'width', 'drop')}
            report, dims, unit = mapedit.terrain_edit(w, code, frame, z, req['shape'], float(req['height']),
                                                      force=bool(req.get('force')), carry=req.get('carry', True),
                                                      **sizes)
            label = f"{req['shape']} {float(req['height']):+.1f} m @ ({x / 100:.0f}, {y / 100:.0f})"
            self._push(label, snap)
            return dict(message=(f'{label}: {report.patches} plátov (pláty ~{unit:.0f} m), odchýlka tvaru '
                                 f'{report.shape_error / 100:.2f} m, objekty {report.objects}, zábradlia '
                                 f'{report.rails}, štart/reset body {report.points}'
                                 + (f'; neposunuté zábradlia: {len(report.rails_in_area)}'
                                    if report.rails_in_area else '')),
                        dims={k: round(v, 1) for k, v in dims.items()}, unit=round(unit, 1))

    def objects(self, req):
        w = self.world
        with self.lock:
            keys = req['keys']
            action = req['action']
            force = bool(req.get('force'))
            names = {_key(c, rec): (c, rec, name) for c, rec, _, name in mapedit.find_objects(w, req['code'])}
            chunks = sorted({int(k.split(':')[0]) for k in keys})
            snap = w.stream.snapshot(chunks)
            done = skipped = 0
            for k in keys:
                if k not in names:
                    raise mapedit.EditRefused(f'unknown object {k}')
                c, rec, name = names[k]
                if mapedit.is_protected(name) and not force:
                    skipped += 1
                    continue
                if action == 'remove':
                    d = (0.0, 0.0, mapedit.SINK)
                elif action == 'move':
                    d = tuple(float(v) for v in req['delta'])
                else:
                    raise mapedit.EditRefused(f'unknown action {action!r}')
                mapedit.move_object(w, c, rec.offset, d)
                done += 1
            if not done:
                w.stream.restore(snap)
                raise mapedit.EditRefused('nothing changed' + (f' ({skipped} game helper objects need "force")'
                                                               if skipped else ''))
            label = f'{"odstránenie" if action == "remove" else "posun"} {done} objektov'
            self._push(label, snap)
            return dict(message=label + (f'; vynechané pomocné: {skipped}' if skipped else ''))

    def undo_last(self):
        with self.lock:
            if not self.undo:
                raise mapedit.EditRefused('nothing to undo')
            label, snap = self.undo.pop()
            self.world.stream.restore(snap)
            self.edits += 1
            return dict(message=f'späť: {label}')

    def save(self, output):
        with self.lock:
            output = os.path.expanduser(output)
            ext = os.path.splitext(output)[1].lower()
            if self.world.is_iso and ext != '.iso':
                raise mapedit.EditRefused('the output must be a .iso file')
            if not self.world.stream.changed_chunks():
                raise mapedit.EditRefused('nothing changed yet')
            t = time.time()
            report = self.world.save(output)
            return dict(message=f'uložené {output}: {len(report)} blokov prekódovaných, overené '
                                f'({time.time() - t:.0f} s)', output=output)


class Handler(BaseHTTPRequestHandler):
    session: Session = None

    def log_message(self, fmt, *args):     # keep the terminal quiet
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, fn):
        try:
            self._send(200, fn())
        except (mapedit.EditRefused, KeyError, ValueError) as e:
            self._send(400, dict(error=str(e)))
        except Exception as e:          # noqa: BLE001
            traceback.print_exc()
            self._send(500, dict(error=f'{type(e).__name__}: {e}'))

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        s = self.session
        if url.path in ('/', '/index.html'):
            return self._static('index.html')
        if url.path.startswith('/static/'):
            return self._static(url.path[len('/static/'):])
        if url.path == '/api/info':
            return self._guard(s.info)
        if url.path == '/api/course':
            return self._guard(lambda: s.course(q['code'][0]))
        if url.path == '/api/texture':
            png = s.texture_png(int(q['id'][0]), q.get('code', [None])[0])
            if png is None:
                return self._send(404, dict(error='texture not decodable'))
            return self._send(200, png, 'image/png')
        self._send(404, dict(error='not found'))

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        try:
            req = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return self._send(400, dict(error='bad JSON'))
        s = self.session
        routes = {'/api/terrain': lambda: s.terrain(req), '/api/objects': lambda: s.objects(req),
                  '/api/undo': s.undo_last, '/api/save': lambda: s.save(req['output'])}
        if url.path not in routes:
            return self._send(404, dict(error='not found'))
        self._guard(routes[url.path])

    def _static(self, name):
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            return self._send(404, dict(error='not found'))
        with open(path, 'rb') as f:
            body = f.read()
        self._send(200, body, mimetypes.guess_type(path)[0] or 'application/octet-stream')


def serve(world, port=8765, open_browser=True):
    Handler.session = Session(world)
    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{httpd.server_address[1]}/'
    print(f'editor beží na {url}  (ukončíš Ctrl+C)', flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return httpd
