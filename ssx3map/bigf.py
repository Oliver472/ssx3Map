"""EA BIGF / BIG4 archives (DATA/WORLDS/BAM.BIG holds bam.sdb, bam.ssb, bam.phm, bam.psm).

Header: 'BIGF', u32 LE archive size, u32 BE entry count, u32 BE header size, then
entries of {u32 BE offset, u32 BE size, NUL-terminated path}; offsets are from
the archive start.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


class BigError(ValueError):
    pass


@dataclass
class BigEntry:
    name: str
    offset: int
    size: int
    entry_pos: int      # where this entry's {offset, size} pair sits in the header


@dataclass
class BigArchive:
    data: bytearray
    magic: bytes
    size_field: int
    header_size: int
    entries: list

    @classmethod
    def parse(cls, data):
        data = bytearray(data)
        if len(data) < 16 or bytes(data[:4]) not in (b'BIGF', b'BIG4'):
            raise BigError('not a BIGF/BIG4 archive')
        size_field, = struct.unpack_from('<I', data, 4)
        count, header_size = struct.unpack_from('>II', data, 8)
        if count > 100000:
            raise BigError('implausible BIG entry count')
        pos = 16
        entries = []
        for _ in range(count):
            if pos + 8 > len(data):
                raise BigError('truncated BIG directory')
            offset, size = struct.unpack_from('>II', data, pos)
            end = data.find(b'\0', pos + 8)
            if end < 0:
                raise BigError('unterminated BIG entry name')
            name = data[pos + 8:end].decode('latin-1')
            if offset + size > len(data):
                raise BigError(f'BIG entry {name} extends past the archive')
            entries.append(BigEntry(name, offset, size, pos))
            pos = end + 1
        return cls(data, bytes(data[:4]), size_field, header_size, entries)

    def find(self, name):
        want = name.replace('\\', '/').lower()
        for e in self.entries:
            if e.name.replace('\\', '/').lower() == want or e.name.replace('\\', '/').lower().endswith('/' + want):
                return e
        raise KeyError(name)

    def find_suffix(self, suffix):
        suffix = suffix.lower()
        hits = [e for e in self.entries if e.name.lower().endswith(suffix)]
        if len(hits) != 1:
            raise KeyError(f'expected one *{suffix} member, found {len(hits)}')
        return hits[0]

    def read(self, entry):
        return bytes(self.data[entry.offset:entry.offset + entry.size])

    def replace(self, entry, payload):
        """Replace a member. Same size: in place. Otherwise the archive is rebuilt."""
        payload = bytes(payload)
        if len(payload) == entry.size:
            self.data[entry.offset:entry.offset + entry.size] = payload
            return
        self._rebuild({entry.name: payload})

    def alignment(self):
        """Largest power of two (<= 2048) that every member offset is a multiple of."""
        align = 2048
        for e in self.entries:
            if e.size == 0:
                continue
            while align > 1 and e.offset % align:
                align //= 2
        return align

    def _rebuild(self, replacements):
        align = self.alignment()
        payloads = [replacements.get(e.name, self.read(e)) for e in self.entries]
        header = bytearray(self.data[:self.header_size])
        # Members keep their order; each starts on the original alignment.
        pos = len(header)
        layout = []
        for e, p in zip(self.entries, payloads):
            if e.size == 0 and e.offset == 0:
                layout.append((0, 0))
                continue
            pos = (pos + align - 1) // align * align
            layout.append((pos, len(p)))
            pos += len(p)
        out = bytearray(pos)
        out[:len(header)] = header
        for e, p, (off, size) in zip(self.entries, payloads, layout):
            struct.pack_into('>II', out, e.entry_pos, off, size)
            out[off:off + size] = p
            e.offset, e.size = off, size
        struct.pack_into('<I', out, 4, len(out))
        self.data = out
        self.size_field = len(out)

    def to_bytes(self):
        return bytes(self.data)
