"""The SSX 3 world stream, bam.ssb.

The file is a run of blocks: 4-byte tag ('CBXS', or 'CEND' for the last block of
a chunk), u32 LE block extent (header included), then a RefPack stream. The
decoded blocks of one chunk concatenate into the chunk's record stream; a record
can straddle blocks. A record is {u8 kind, u24 LE size, u8 track, u24 LE rid}
followed by `size` bytes.

Writing never moves a block. A changed chunk is cut at the original block
boundaries (in decoded bytes) and only blocks whose decoded bytes changed are
re-encoded, each into exactly the number of bytes the original stream occupied.
The result has the same size and the same block offsets as the original, so
bam.sdb, the BIG directory and the disc image stay valid untouched.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from . import refpack

TAG_BLOCK = b'CBXS'
TAG_END = b'CEND'


class StreamError(ValueError):
    pass


@dataclass
class Block:
    index: int
    offset: int         # file offset of the 8-byte tag
    tag: bytes
    extent: int         # u32 field: header + payload (+ padding)
    decoded_size: int   # from the RefPack header
    chunk: int

    @property
    def payload_offset(self):
        return self.offset + 8


@dataclass
class Record:
    kind: int
    track: int
    rid: int
    offset: int         # offset of the record data inside the chunk
    size: int

    @property
    def header_offset(self):
        return self.offset - 8

    @property
    def resource(self):
        return (self.rid << 8) | self.track


@dataclass
class Chunk:
    index: int
    blocks: list = field(default_factory=list)

    @property
    def decoded_size(self):
        return sum(b.decoded_size for b in self.blocks)


def scan(data):
    """Index the blocks and chunks of a bam.ssb image without decoding anything."""
    blocks, chunks = [], []
    pos = 0
    current = Chunk(0)
    n = len(data)
    while pos < n:
        if pos + 8 > n:
            raise StreamError(f'truncated block header at 0x{pos:X}')
        tag = bytes(data[pos:pos + 4])
        if tag not in (TAG_BLOCK, TAG_END):
            raise StreamError(f'unexpected block tag {tag!r} at 0x{pos:X}')
        extent, = struct.unpack_from('<I', data, pos + 4)
        if extent <= 8 + 5 or pos + extent > n:
            raise StreamError(f'bad block extent {extent} at 0x{pos:X}')
        header = refpack.parse_header(data[pos + 8:pos + 8 + 16])
        block = Block(len(blocks), pos, tag, extent, header.decompressed_size, current.index)
        blocks.append(block)
        current.blocks.append(block)
        pos += extent
        if tag == TAG_END:
            chunks.append(current)
            current = Chunk(len(chunks))
    if current.blocks:
        raise StreamError('stream ends inside a chunk (no CEND)')
    return blocks, chunks


def parse_records(chunk_data):
    records = []
    pos = 0
    n = len(chunk_data)
    while pos < n:
        if pos + 8 > n:
            raise StreamError(f'truncated record header at chunk offset 0x{pos:X}')
        kind = chunk_data[pos]
        size = int.from_bytes(chunk_data[pos + 1:pos + 4], 'little')
        track = chunk_data[pos + 4]
        rid = int.from_bytes(chunk_data[pos + 5:pos + 8], 'little')
        pos += 8
        if pos + size > n:
            raise StreamError(f'record at chunk offset 0x{pos - 8:X} extends past the chunk')
        records.append(Record(kind, track, rid, pos, size))
        pos += size
    return records


class WorldStream:
    """bam.ssb with lazily decoded, editable chunks."""

    def __init__(self, data):
        self.original = bytes(data)
        self.blocks, self.chunks = scan(self.original)
        self._decoded = {}      # chunk index -> bytearray (contents being edited)
        self._pristine = {}     # chunk index -> bytes (decoded original)

    # -- reading -----------------------------------------------------------
    def block_payload(self, block):
        return self.original[block.payload_offset:block.offset + block.extent]

    def decode_block(self, block):
        data = refpack.decompress(self.block_payload(block))
        if len(data) != block.decoded_size:
            raise StreamError(f'block {block.index} decoded size mismatch')
        return data

    def chunk_original(self, index, keep=True):
        data = self._pristine.get(index)
        if data is None:
            data = b''.join(self.decode_block(b) for b in self.chunks[index].blocks)
            if keep:
                self._pristine[index] = data
        return data

    def original_block_data(self, block):
        chunk = self.chunks[block.chunk]
        start = 0
        for b in chunk.blocks:
            if b is block:
                break
            start += b.decoded_size
        return self.chunk_original(block.chunk)[start:start + block.decoded_size]

    def chunk(self, index):
        """Editable decoded contents of a chunk (a bytearray; edit in place, same size)."""
        data = self._decoded.get(index)
        if data is None:
            data = bytearray(self.chunk_original(index))
            self._decoded[index] = data
        return data

    def current(self, index, keep=True):
        """Current contents (edited or original) without forcing an editable copy."""
        edited = self._decoded.get(index)
        return edited if edited is not None else self.chunk_original(index, keep=keep)

    def records(self, index, keep=True):
        return parse_records(self.current(index, keep=keep))

    def __len__(self):
        return len(self.chunks)

    # -- writing -----------------------------------------------------------
    def snapshot(self, chunks):
        """State of `chunks` for restore(): their edited bytes, or None if unedited."""
        return {c: (bytes(self._decoded[c]) if c in self._decoded else None) for c in chunks}

    def restore(self, snap):
        for c, data in snap.items():
            if data is None:
                self._decoded.pop(c, None)
            else:
                self._decoded[c] = bytearray(data)

    def changed_chunks(self):
        return [i for i, d in sorted(self._decoded.items()) if bytes(d) != self.chunk_original(i)]

    def _block_jobs(self, index):
        """(k, block, payload, original piece, edited piece) for the changed blocks of a chunk."""
        data = bytes(self._decoded[index])
        chunk = self.chunks[index]
        if len(data) != chunk.decoded_size:
            raise StreamError(
                f'chunk {index} changed size ({chunk.decoded_size} -> {len(data)} bytes); '
                'only same-size edits are supported')
        jobs = []
        pos = 0
        for k, block in enumerate(chunk.blocks):
            piece = data[pos:pos + block.decoded_size]
            pos += block.decoded_size
            old = self.original_block_data(block)
            if piece != old:
                jobs.append((k, block, self.block_payload(block), old, piece))
        return data, jobs

    def build(self, progress=None, workers=None):
        """Return the new bam.ssb image (same size and layout as the original).

        Every changed block is re-encoded into exactly its own bytes (in parallel).
        If a block cannot hold its edited data, the whole chunk is packed again
        over the same blocks, moving the boundaries between them."""
        out = bytearray(self.original)
        report = []
        max_decoded = max(b.decoded_size for b in self.blocks)
        work = []
        for index in self.changed_chunks():
            data, jobs = self._block_jobs(index)
            work += [(index, job) for job in jobs]
        if progress and work:
            progress(f're-encoding {len(work)} blocks')
        results = parallel_map(_reencode_job, [(p, o, n) for _, (_, _, p, o, n) in work], workers)
        failed = []
        for (index, (k, block, payload, old, piece)), res in zip(work, results):
            if res is None:
                if index not in failed:
                    failed.append(index)
                continue
            encoded, method, used_padding = res
            assert len(encoded) == len(payload)
            out[block.payload_offset:block.offset + block.extent] = encoded
            report.append(dict(chunk=index, block=block.index, offset=block.offset,
                               decoded=len(piece), method=method, used_padding=used_padding))
        for index in failed:
            if progress:
                progress(f'chunk {index}: packing all {len(self.chunks[index].blocks)} blocks again')
            report = [r for r in report if r['chunk'] != index]
            report += self._repack_chunk(index, out, max_decoded)
        if len(out) != len(self.original):
            raise AssertionError('world stream changed size')
        return bytes(out), report

    def _repack_chunk(self, index, out, max_decoded):
        """Pack the edited chunk over all of its blocks again (each keeps its offset and extent)."""
        blocks = self.chunks[index].blocks
        data = bytes(self._decoded[index])
        payloads = _pack(data, [b.extent - 8 for b in blocks], max_decoded)
        if payloads is None:
            short = self.shortfall(index)
            raise StreamError(f'chunk {index}: the edited data does not fit in its blocks any more '
                              f'(about {max(short, 1)} bytes too many)')
        entries = []
        for b, (payload, decoded) in zip(blocks, payloads):
            out[b.payload_offset:b.offset + b.extent] = payload
            entries.append(dict(chunk=index, block=b.index, offset=b.offset, decoded=decoded,
                                method='repack', used_padding=0))
        return entries

    def shortfall(self, index, workers=None):
        """Estimated compressed bytes by which the edited chunk misses its blocks (<= 0: it fits).

        Each original block range is compressed on its own; packing over the
        blocks is then simulated with those densities, honouring both limits
        of a block: its compressed capacity and the decoded size the game takes
        (the largest block on the disc)."""
        data = bytes(self._decoded.get(index) or self.chunk_original(index))
        blocks = self.chunks[index].blocks
        max_decoded = max(b.decoded_size for b in self.blocks)
        pieces, pos = [], 0
        for b in blocks:
            pieces.append(data[pos:pos + b.decoded_size])
            pos += b.decoded_size
        changed = [k for k, (b, p) in enumerate(zip(blocks, pieces)) if p != self.original_block_data(b)]
        sizes = dict(zip(changed, parallel_map(_compressed_size, [pieces[k] for k in changed], workers)))
        slack = [blocks[k].extent - 8 - sizes[k] for k in changed]
        if all(v >= 0 for v in slack):
            return -min(slack, default=0)            # every block still holds its own edited bytes
        rest = [k for k in range(len(blocks)) if k not in sizes]
        sizes.update(zip(rest, parallel_map(_compressed_size, [pieces[k] for k in rest], workers)))
        sizes = [sizes[k] for k in range(len(blocks))]
        segments = [(len(p), s / len(p)) for p, s in zip(pieces, sizes) if p]
        seg, used = 0, 0                     # current segment, bytes of it already placed
        spare = 0.0
        for b in blocks:
            budget = (b.extent - 8) * 0.995 - 32
            room = max_decoded
            while seg < len(segments) and budget > 0 and room > 0:
                length, rho = segments[seg]
                take = min(length - used, room, int(budget / rho))
                if take <= 0:
                    break
                used += take
                room -= take
                budget -= take * rho
                if used == length:
                    seg, used = seg + 1, 0
            spare += max(0.0, budget)
        if seg >= len(segments):
            return -int(spare)
        left = (segments[seg][0] - used) * segments[seg][1]
        left += sum(length * rho for length, rho in segments[seg + 1:])
        return int(left) + 1

    def verify(self, image):
        """Decode every changed chunk of `image` and compare it with the edits."""
        blocks, chunks = scan(image)
        if [(b.offset, b.tag, b.extent) for b in blocks] != [(b.offset, b.tag, b.extent) for b in self.blocks]:
            raise StreamError('block layout differs from the original')
        for index in self.changed_chunks():
            got = b''.join(refpack.decompress(image[b.payload_offset:b.offset + b.extent])
                           for b in chunks[index].blocks)
            if got != bytes(self._decoded[index]):
                raise StreamError(f'chunk {index} does not decode to the edited data')
        return True


def _compressed_size(piece):
    return len(refpack.compress(piece))


def _reencode_job(job):
    """New payload for one block, exactly as long as the original payload, or None.

    1. splice: keep the original commands around the edit, re-encode the rest
       (only when the edit leaves a good part of the block alone);
    2. full: re-encode the whole stream into the bytes it used;
    3. padding: the stream grows into padding the original block carried.
    """
    payload, old, piece = job
    first = next(i for i in range(len(piece)) if old[i] != piece[i])
    last = next(i for i in range(len(piece) - 1, -1, -1) if old[i] != piece[i])
    if last - first < len(piece) // 2:
        try:
            return refpack.splice(payload, old, piece), 'splice', 0
        except refpack.ExactSizeError:
            pass
    header = refpack.parse_header(payload)
    _, consumed = refpack.decompress(payload, with_consumed=True)
    padding = payload[consumed:]
    try:
        return refpack.compress_exact(piece, consumed, header=header.raw) + padding, 'full', 0
    except refpack.ExactSizeError as err:
        if err.minimum is not None and consumed < err.minimum <= len(payload):
            try:
                encoded = refpack.compress_exact(piece, err.minimum, header=header.raw)
            except refpack.ExactSizeError:
                return None
            used = err.minimum - consumed
            return encoded + padding[used:], 'padding', used
        return None


def parallel_map(fn, items, workers=None):
    """map(fn, items) over worker processes (pure-Python RefPack is slow); serial for few items."""
    items = list(items)
    if workers is None:
        workers = min(len(items), os.cpu_count() or 1)
    if workers <= 1 or len(items) < 2:
        return [fn(x) for x in items]
    try:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fn, items))
    except (OSError, ImportError, RuntimeError):        # no process support here: do it serially
        return [fn(x) for x in items]


def _pack(span, capacities, max_decoded):
    """Split `span` over len(capacities) RefPack blocks: [(payload, decoded size)] or None.

    Each payload is a stream padded with zeros to its block's capacity (the
    retail blocks are padded the same way). Every block but the last takes as
    many bytes as fit, found from one pass of prefix size estimates.
    """
    out = []
    pos = 0
    for i, cap in enumerate(capacities):
        rest = span[pos:]
        later = len(capacities) - 1 - i
        if not later:
            if not rest or len(rest) > max_decoded:
                return None
            take = len(rest)
            stream = refpack.compress(rest)
        else:
            hi = min(len(rest) - later, max_decoded)          # leave >= 1 byte per later block
            if hi < 1:
                return None
            est = refpack.prefix_sizes(rest[:hi])
            for n in range(1, len(est)):          # make the estimates monotonic
                if est[n] < est[n - 1]:
                    est[n] = est[n - 1]

            def largest(limit, scale):
                lo, hi_ = 0, len(est) - 1
                while lo < hi_:
                    mid = (lo + hi_ + 1) // 2
                    if est[mid] * scale <= limit:
                        lo = mid
                    else:
                        hi_ = mid - 1
                return lo

            scale = 1.0
            take = largest(cap, scale)
            stream = None
            for _ in range(12):
                if take < 1:
                    return None
                trial = refpack.compress(rest[:take])
                scale = len(trial) / est[take]              # the real encoder vs the estimate here
                if len(trial) <= cap:
                    stream = trial
                    grow = largest(cap - 4, scale)
                    if grow <= take or cap - len(trial) < 16:
                        break
                    take = grow
                elif stream is not None:
                    take = len(refpack.decompress(stream))     # back to the last one that fitted
                    break
                else:
                    take = min(take - 16, largest(cap, scale * 1.001))
            if stream is None:
                return None
        if len(stream) > cap:
            return None
        out.append((stream + bytes(cap - len(stream)), take))
        pos += take
    return out if pos == len(span) else None
