import contextlib
import io
import os
import random
import struct
import tempfile
import unittest

from ssx3map import bigf, iso9660, painter, refpack, ssb, texture
from ssx3map.cli import main
from ssx3map.world import World

from tests import fixtures


def _sample(n, seed):
    rng = random.Random(seed)
    out = bytearray()
    while len(out) < n:
        r = rng.random()
        if r < 0.3:
            out += struct.pack('<4f', *(rng.uniform(-1e4, 1e4) for _ in range(4)))
        elif r < 0.5:
            out += bytes(rng.randrange(1, 300))
        elif r < 0.7 and len(out) > 64:
            s = rng.randrange(0, len(out) - 32)
            out += out[s:s + rng.randrange(3, 2000)]
        else:
            out += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
    return bytes(out[:n])


class RefPackTest(unittest.TestCase):
    def test_round_trip(self):
        for n in (0, 1, 3, 4, 5, 17, 1000, 20000, 140000):
            data = _sample(n, n)
            self.assertEqual(refpack.decompress(refpack.compress(data)), data, n)

    def test_long_distance_and_runs(self):
        block = bytes(random.Random(5).randrange(256) for _ in range(3000))
        data = block + bytes(120000) + block + b'\x07' * 5000
        self.assertEqual(refpack.decompress(refpack.compress(data)), data)

    def test_exact_size(self):
        data = _sample(30000, 9)
        best = len(refpack.compress(data))
        for extra in (0, 1, 2, 3, 5, 64, 1000, 5000):
            out = refpack.compress_exact(data, best + extra)
            self.assertEqual(len(out), best + extra)
            self.assertEqual(refpack.decompress(out), data)
        with self.assertRaises(refpack.ExactSizeError):
            refpack.compress_exact(data, best - 50)

    def test_header_preserved(self):
        data = _sample(5000, 3)
        best = len(refpack.compress(data)) + 3       # 0x11 header is 3 bytes longer
        header = refpack.make_header(len(data), flags=0x11, compressed_size=best)
        out = refpack.compress_exact(data, best, header=header)
        self.assertEqual(out[:8], header)
        self.assertEqual(refpack.parse_header(out).decompressed_size, len(data))
        self.assertEqual(refpack.decompress(out), data)

    def test_consumed_and_padding(self):
        data = _sample(4000, 4)
        stream = refpack.compress(data)
        out, consumed = refpack.decompress(stream + bytes(37), with_consumed=True)
        self.assertEqual(out, data)
        self.assertEqual(consumed, len(stream))

    def test_splice_small_edits(self):
        # Originals a little bigger than our best encoding (any real encoder leaves some slack).
        rng = random.Random(11)
        for seed in range(12):
            data = bytearray(_sample(30000, 100 + seed))
            best = len(refpack.compress(data))
            stream = refpack.compress_exact(data, best + 40 + best // 30)
            stream_padded = stream + bytes(seed % 3)
            new = bytearray(data)
            at = rng.randrange(0, len(new) - 40)
            for k in range(rng.randrange(1, 28)):
                new[at + k] = rng.randrange(256)
            out = refpack.splice(stream_padded, bytes(data), bytes(new))
            self.assertEqual(len(out), len(stream_padded))
            self.assertEqual(refpack.decompress(out), bytes(new))
            self.assertEqual(out[:5], stream[:5])      # header kept
            self.assertEqual(out[len(stream):], stream_padded[len(stream):])

    def test_splice_tight_literal_edit(self):
        # No slack at all, but the edit only touches bytes the stream stores as literals
        # (like fog floats or palette colours): the edited commands keep their size.
        rng = random.Random(4)
        noise = bytes(rng.randrange(256) for _ in range(2000))
        data = _sample(20000, 1) + noise + _sample(20000, 2)
        stream = refpack.compress(data, max_chain=512)
        new = bytearray(data)
        for k in range(20000 + 500, 20000 + 528):
            new[k] = rng.randrange(256)
        out = refpack.splice(stream, data, bytes(new))
        self.assertEqual(len(out), len(stream))
        self.assertEqual(refpack.decompress(out), bytes(new))
        same = sum(1 for x, y in zip(out, stream) if x == y)
        self.assertGreater(same, len(stream) - 200)

    def test_splice_follows_later_copies(self):
        part = bytes(random.Random(2).randrange(256) for _ in range(300))
        data = bytes(5000) + part + bytes(range(256)) * 8 + part + part
        best = len(refpack.compress(data))
        stream = refpack.compress_exact(data, best + 20)
        new = bytearray(data)
        new[5010] ^= 0x55               # inside the first copy of `part`, which later matches reference
        out = refpack.splice(stream, data, bytes(new))
        self.assertEqual(refpack.decompress(out), bytes(new))
        self.assertEqual(len(out), len(stream))

    def test_rejects_garbage(self):
        with self.assertRaises(refpack.RefPackError):
            refpack.decompress(b'\x10\xfb\x00\x00\x10\x05')


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.big, self.chunks = fixtures.build_world()
        self.big_path = os.path.join(self.dir, 'BAM.BIG')
        with open(self.big_path, 'wb') as f:
            f.write(self.big)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)

    def test_reads_structure(self):
        w = World(self.big_path)
        self.assertEqual([l.name for l in w.sdb.locations], ['AAA', 'A_AAA', 'ASKY'])
        self.assertEqual(list(w.location('aaa').chunks), [0, 1])
        self.assertEqual(len(w.stream), 4)
        for i, data in enumerate(self.chunks):
            self.assertEqual(w.stream.chunk_original(i), data)
        self.assertEqual(len(w.texture_records(7)), 2)

    def test_no_change_writes_identical(self):
        w = World(self.big_path)
        image, report = w.stream.build()
        self.assertEqual(report, [])
        self.assertEqual(image, w.stream.original)

    def test_fog_edit_round_trip(self):
        w = World(self.big_path)
        (c, r), = w.records(kind=15, location='AAA')
        changes = painter.edit_fog(w.stream.chunk(c), r.offset, r.size,
                                   dict(r=1.0, g=0.25, b=0.25, near_cm=500, far_cm=4000))
        self.assertEqual(len(changes), 2)
        out = self.path('mod.BIG')
        report = w.save(out)
        self.assertTrue(report)

        with open(out, 'rb') as f:
            new = f.read()
        self.assertEqual(len(new), len(self.big))
        old_big, new_big = bigf.BigArchive.parse(self.big), bigf.BigArchive.parse(new)
        self.assertEqual([(e.name, e.offset, e.size) for e in old_big.entries],
                         [(e.name, e.offset, e.size) for e in new_big.entries])
        # Only the edited chunk's blocks differ.
        old_blocks, _ = ssb.scan(old_big.read(old_big.find_suffix('.ssb')))
        new_ssb = new_big.read(new_big.find_suffix('.ssb'))
        new_blocks, _ = ssb.scan(new_ssb)
        self.assertEqual([(b.offset, b.extent, b.tag) for b in old_blocks],
                         [(b.offset, b.extent, b.tag) for b in new_blocks])
        changed_chunks = {b.chunk for b in new_blocks
                          if new_ssb[b.offset:b.offset + b.extent] != w.stream.original[b.offset:b.offset + b.extent]}
        self.assertEqual(changed_chunks, {c})

        w2 = World(out)
        (c2, r2), = w2.records(kind=15, location='AAA')
        fogs = painter.fog_entries(w2.record_bytes(c2, r2))
        self.assertAlmostEqual(fogs[0][1]['g'], 0.25)
        self.assertEqual(fogs[1][1]['far_cm'], 4000)
        self.assertEqual(fogs[0][1]['density'], struct.unpack('<f', struct.pack('<f', 0.001))[0])
        for i in range(4):
            if i != c:
                self.assertEqual(w2.stream.chunk_original(i), self.chunks[i])

    def test_fog_validation(self):
        w = World(self.big_path)
        (c, r), = w.records(kind=15, location='AAA')
        with self.assertRaises(painter.PainterError):
            painter.edit_fog(w.stream.chunk(c), r.offset, r.size, dict(near_cm=9000, far_cm=100))

    def test_tint_all_copies(self):
        w = World(self.big_path)
        copies = w.texture_records(7)
        before = [w.record_bytes(c, r) for c, r in copies]
        for c, r in copies:
            texture.tint(w.stream.chunk(c), r.offset, r.size, mul=(1.0, 0.5, 0.5))
        out = self.path('tint.BIG')
        w.save(out)
        w2 = World(out)
        after = [w2.record_bytes(c, r) for c, r in w2.texture_records(7)]
        self.assertEqual(after[0], after[1])
        shape = texture.parse(after[0])
        self.assertEqual(after[0][:shape.next_offset], before[0][:shape.next_offset])   # texels untouched
        r0, g0 = before[0][shape.palette_offset + 4 * 200], before[0][shape.palette_offset + 4 * 200 + 1]
        self.assertEqual(after[0][shape.palette_offset + 4 * 200], r0)
        self.assertEqual(after[0][shape.palette_offset + 4 * 200 + 1], round(g0 * 0.5))
        # The RGBA texture and everything else is unchanged.
        self.assertEqual(w2.record_bytes(*w2.texture_records(3)[0]), w.record_bytes(*w.texture_records(3)[0]))

    def test_texture_decode_and_png(self):
        w = World(self.big_path)
        for rid in (7, 3):
            c, r = w.texture_records(rid)[0]
            width, height, rgba = texture.decode_rgba(w.record_bytes(c, r))
            self.assertEqual(len(rgba), width * height * 4)
            png = self.path(f't{rid}.png')
            texture.write_png(png, width, height, rgba)
            with open(png, 'rb') as f:
                self.assertEqual(f.read(8), b'\x89PNG\r\n\x1a\n')

    def test_tight_block_uses_padding_or_fails(self):
        # Originals encoded as tightly as our encoder can: incompressible edits cannot fit...
        big, _ = fixtures.build_world(slack=0, padding=0)
        path = self.path('tight.BIG')
        with open(path, 'wb') as f:
            f.write(big)
        w = World(path)
        c, r = w.texture_records(3)[0]
        buf = w.stream.chunk(c)
        rng = random.Random(1)
        buf[r.offset + 0x80:r.offset + r.size] = bytes(rng.randrange(256) for _ in range(r.size - 0x80))
        with self.assertRaises(ssb.StreamError):
            w.stream.build()
        # ...unless the block carries padding after its stream.
        big, _ = fixtures.build_world(slack=0, padding=3000)
        with open(path, 'wb') as f:
            f.write(big)
        w = World(path)
        c, r = w.texture_records(3)[0]
        buf = w.stream.chunk(c)
        buf[r.offset + 0x80:r.offset + r.size] = bytes(rng.randrange(256) for _ in range(r.size - 0x80))
        image, report = w.stream.build()
        self.assertTrue(any(x['used_padding'] for x in report))
        self.assertTrue(w.stream.verify(image))

    def test_tight_original_small_edit_splices(self):
        # Original blocks as tight as our own best encoding: small edits still fit.
        big, _ = fixtures.build_world(slack=0, padding=0)
        path = self.path('tight.BIG')
        with open(path, 'wb') as f:
            f.write(big)
        w = World(path)
        (c, r), = w.records(kind=15, location='AAA')
        painter.edit_fog(w.stream.chunk(c), r.offset, r.size, dict(r=0.1, g=0.9, b=0.3, far_cm=5555))
        for c, r in w.texture_records(7):
            texture.tint(w.stream.chunk(c), r.offset, r.size, mul=(0.9, 0.7, 1.0))
        image, report = w.stream.build()
        self.assertTrue(report)
        self.assertEqual({x['method'] for x in report}, {'splice'})
        self.assertTrue(w.stream.verify(image))

    def test_size_change_rejected(self):
        w = World(self.big_path)
        w.stream.chunk(0).extend(b'xx')
        with self.assertRaises(ssb.StreamError):
            w.stream.build()

    def test_iso_patch(self):
        iso = fixtures.build_iso(self.big)
        iso_path = self.path('game.iso')
        with open(iso_path, 'wb') as f:
            f.write(iso)
        w = World(iso_path)
        self.assertTrue(w.is_iso)
        (c, r), = w.records(kind=15, location='A_AAA')
        painter.edit_fog(w.stream.chunk(c), r.offset, r.size, dict(r=0.0, g=1.0, b=0.0))
        out = self.path('mod.iso')
        w.save(out)
        with open(iso_path, 'rb') as f:
            self.assertEqual(f.read(), iso)          # input untouched
        with open(out, 'rb') as f:
            new = f.read()
        self.assertEqual(len(new), len(iso))
        entry = iso9660.find(out, 'DATA/WORLDS/BAM.BIG')
        self.assertEqual(new[:entry.offset], iso[:entry.offset])
        end = entry.offset + entry.size
        self.assertEqual(new[end:], iso[end:])
        self.assertNotEqual(new[entry.offset:end], iso[entry.offset:end])
        w2 = World(out)
        (c2, r2), = w2.records(kind=15, location='A_AAA')
        self.assertEqual(painter.fog_entries(w2.record_bytes(c2, r2))[0][1]['g'], 1.0)

    def test_refuses_to_overwrite_input(self):
        w = World(self.big_path)
        w.stream.chunk(0)[100] ^= 1
        with self.assertRaises(ValueError):
            w.save(self.big_path)

    def test_cli(self):
        out = self.path('cli.BIG')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            main(['fog', self.big_path, '--location', 'AAA', '--color', '1', '0', '0', '--far', '7000', '-o', out])
            main(['fog', out, '--all'])
            main(['tint', out, '--texture', '7', '--rgb', '1', '0.8', '0.8', '-o', self.path('cli2.BIG')])
            main(['textures', self.path('cli2.BIG'), '--export', self.path('png')])
            main(['list', out, '--kind', '15'])
            main(['info', out])
            main(['inspect', out, '--sample-blocks', '3', '--json', self.path('r.json')])
        text = buf.getvalue()
        self.assertIn('AAA        chunk 1: blend_rate 0.5, density 0.001, near_cm 3000, far_cm 7000, r 1', text)
        self.assertIn('tinted 1 textures (2 copies', text)
        self.assertEqual(sorted(os.listdir(self.path('png'))), ['tex_0003_16x16.png', 'tex_0007_32x32.png'])
        self.assertNotIn('ERROR in this section', text)


if __name__ == '__main__':
    unittest.main()
