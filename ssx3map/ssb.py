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

    def build(self, progress=None):
        """Return the new bam.ssb image (same size and layout as the original)."""
        out = bytearray(self.original)
        report = []
        max_decoded = max(b.decoded_size for b in self.blocks)
        for index in self.changed_chunks():
            data = bytes(self._decoded[index])
            chunk = self.chunks[index]
            if len(data) != chunk.decoded_size:
                raise StreamError(
                    f'chunk {index} changed size ({chunk.decoded_size} -> {len(data)} bytes); '
                    'only same-size edits are supported')
            bounds = [0]
            for b in chunk.blocks:
                bounds.append(bounds[-1] + b.decoded_size)
            k = 0
            while k < len(chunk.blocks):
                block = chunk.blocks[k]
                piece = data[bounds[k]:bounds[k + 1]]
                if piece == self.original_block_data(block):
                    k += 1
                    continue
                if progress:
                    progress(f'chunk {index} block {block.index}: re-encoding {len(piece)} bytes')
                payload = self.block_payload(block)
                try:
                    encoded, method, used_padding = self._reencode(index, block, payload, piece)
                except StreamError:
                    # Spread this block and the next ones' data over the same blocks again.
                    k, entries = self._rebalance(index, chunk, bounds, data, k, max_decoded, out, progress)
                    report.extend(entries)
                    continue
                assert len(encoded) == len(payload)
                out[block.payload_offset:block.offset + block.extent] = encoded
                report.append(dict(chunk=index, block=block.index, offset=block.offset,
                                   decoded=len(piece), method=method, used_padding=used_padding))
                k += 1
        if len(out) != len(self.original):
            raise AssertionError('world stream changed size')
        return bytes(out), report

    def _rebalance(self, index, chunk, bounds, data, k, max_decoded, out, progress):
        """Re-pack the decoded bytes of blocks k..k+w-1 into those same w blocks,
        moving the boundaries between them (records may straddle blocks anyway).
        Every block keeps its offset and extent; the window ends on an original
        boundary so the blocks after it are untouched. Returns (next k, report)."""
        blocks = chunk.blocks
        for w in range(2, len(blocks) - k + 1):
            window = blocks[k:k + w]
            span = data[bounds[k]:bounds[k + w]]
            if progress:
                progress(f'chunk {index}: re-packing blocks {window[0].index}..{window[-1].index}')
            payloads = _pack(span, [b.extent - 8 for b in window], max_decoded)
            if payloads is None:
                continue
            entries = []
            for b, (payload, decoded) in zip(window, payloads):
                out[b.payload_offset:b.offset + b.extent] = payload
                entries.append(dict(chunk=index, block=b.index, offset=b.offset, decoded=decoded,
                                    method='repack', used_padding=0))
            return k + w, entries
        raise StreamError(f'chunk {index}: the edited data does not fit in its blocks any more')

    def _reencode(self, index, block, payload, piece):
        """New payload bytes for `block`, exactly as long as the original payload.

        1. splice: keep the original commands around the edit, re-encode the rest;
        2. full: re-encode the whole stream into the bytes it used;
        3. padding: the stream grows into padding the original block carried.
        """
        try:
            return refpack.splice(payload, self.original_block_data(block), piece), 'splice', 0
        except refpack.ExactSizeError:
            pass
        header = refpack.parse_header(payload)
        _, consumed = refpack.decompress(payload, with_consumed=True)
        padding = payload[consumed:]
        try:
            return refpack.compress_exact(piece, consumed, header=header.raw) + padding, 'full', 0
        except refpack.ExactSizeError as err:
            if err.minimum is not None and consumed < err.minimum <= len(payload):
                encoded = refpack.compress_exact(piece, err.minimum, header=header.raw)
                used = err.minimum - consumed
                return encoded + padding[used:], 'padding', used
            raise StreamError(
                f'chunk {index} block {block.index}: the edited data needs {err.minimum} bytes '
                f'compressed but the block holds only {len(payload)}') from err

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


def _fits(data, capacity):
    return len(refpack.compress(data)) <= capacity


def _pack(span, capacities, max_decoded):
    """Split `span` over len(capacities) RefPack blocks: [(payload, decoded size)] or None.

    Each payload is a stream padded with zeros to its block's capacity (the
    retail blocks are padded the same way). Every block but the last takes as
    many bytes as fit.
    """
    out = []
    pos = 0
    for i, cap in enumerate(capacities):
        rest = span[pos:]
        last = i == len(capacities) - 1
        if last:
            if not rest or len(rest) > max_decoded or not _fits(rest, cap):
                return None
            take = len(rest)
        else:
            hi = min(len(rest) - (len(capacities) - 1 - i), max_decoded)     # leave >= 1 byte per later block
            if hi < 1:
                return None
            if _fits(rest[:hi], cap):
                take = hi
            else:
                good, bad = 0, hi
                probe = hi
                while True:             # step down quickly, then bisect
                    probe = max(1, int(probe * 0.97))
                    if _fits(rest[:probe], cap):
                        good = probe
                        break
                    bad = probe
                    if probe == 1:
                        return None
                while bad - good > 64:
                    mid = (good + bad) // 2
                    if _fits(rest[:mid], cap):
                        good = mid
                    else:
                        bad = mid
                take = good
        stream = refpack.compress(rest[:take])
        if len(stream) > cap:
            return None
        out.append((stream + bytes(cap - len(stream)), take))
        pos += take
    return out if pos == len(span) else None
