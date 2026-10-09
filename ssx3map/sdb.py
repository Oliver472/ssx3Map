"""bam.sdb: the directory of world locations.

80-byte header (u32 location count at +8, chunk-info count at +12, sub-chunk
count at +16, u16 world texture count at +0x2A, u16 light page count at +0x2C),
then 88-byte location records: name[16], u32 sub-chunk count, u32 chunk count,
u32 last chunk index (inclusive), u32 first sub-chunk, 28 x s16 (the first 23:
the location's record counts per kind on its own track). Then 96-byte chunk
infos (a box tree) and 68-byte sub-chunk infos (record counts, the chunk's
offset in bam.ssb, its size); grow.Tables reads and rewrites those. Layout from
GlitcherOG's SSX-Library SDBHandler, the ssxdecomp notes
(docs/notes/subsystems/asset-formats.md) and the probe report (docs/findings.md).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER_SIZE = 80
LOCATION_SIZE = 88


@dataclass
class Location:
    index: int          # also the resource "track" of the location's records
    name: str
    sub_count: int
    chunk_count: int
    chunk_end: int      # inclusive
    sub_start: int
    shorts: tuple
    first_chunk: int = 0

    @property
    def chunks(self):
        return range(self.first_chunk, self.chunk_end + 1)


@dataclass
class Directory:
    header: bytes
    location_count: int
    chunk_info_count: int
    sub_chunk_count: int
    texture_count: int
    light_page_count: int
    locations: list
    size: int

    def by_name(self, name):
        for loc in self.locations:
            if loc.name.lower() == name.lower():
                return loc
        names = ', '.join(l.name for l in self.locations)
        raise KeyError(f'no location {name!r}; known: {names}')

    def location_of_chunk(self, chunk):
        for loc in self.locations:
            if loc.first_chunk <= chunk <= loc.chunk_end:
                return loc
        return None


def parse(data):
    if len(data) < HEADER_SIZE:
        raise ValueError('bam.sdb is too short')
    count, chunk_infos, subs = struct.unpack_from('<3I', data, 8)
    if count > 1000 or len(data) < HEADER_SIZE + count * LOCATION_SIZE:
        raise ValueError('implausible bam.sdb location table')
    textures, pages = struct.unpack_from('<HH', data, 0x2A)
    locations = []
    previous_end = -1
    for i in range(count):
        p = HEADER_SIZE + i * LOCATION_SIZE
        name = data[p:p + 16].split(b'\0')[0].decode('latin-1')
        sub_count, chunk_count, chunk_end, sub_start = struct.unpack_from('<4I', data, p + 16)
        shorts = struct.unpack_from('<28h', data, p + 32)
        loc = Location(i, name, sub_count, chunk_count, chunk_end, sub_start, shorts, previous_end + 1)
        previous_end = chunk_end
        locations.append(loc)
    return Directory(bytes(data[:HEADER_SIZE]), count, chunk_infos, subs, textures, pages, locations, len(data))
