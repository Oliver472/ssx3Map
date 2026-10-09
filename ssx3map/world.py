"""High-level access to the SSX 3 world: open BAM.BIG (or the whole disc image),
edit records, write a new BAM.BIG or a patched copy of the disc image."""
from __future__ import annotations

import os
import struct

from . import bigf, iso9660, refpack, sdb, ssb

BIG_PATH = 'DATA/WORLDS/BAM.BIG'

KIND_NAMES = {
    0: 'material', 1: 'terrain patch', 2: 'model', 3: 'instance', 4: 'particle model',
    5: 'particle instance', 6: 'local lights', 7: 'light glows', 8: 'rail splines',
    9: 'texture', 10: 'light page', 11: 'vis curtain', 12: 'collision', 13: 'sound triggers',
    14: 'AI paths (AIP)', 15: 'painter (fog/sun/lighting)', 16: 'stage scripts',
    17: 'camera triggers', 18: 'cutscene locators', 19: 'missions', 20: 'sound bank',
    21: 'course spine', 22: 'avalanche / visual fx',
}
TRACK_SHARED = 255


def resource_names(phm, psm):
    """{group: {(track, rid): name}}: terrain 0, instances 1, models 2, splines 3, collision 4."""
    count, = struct.unpack_from('<I', phm, 8)
    if count != struct.unpack_from('<I', psm, 8)[0] or not 1 <= count <= 32:
        raise ValueError('PHM/PSM group counts differ')
    hp = sp = 12
    groups = []
    for _ in range(count):
        _, entries = struct.unpack_from('<2I', phm, hp)
        hp += 8
        _, strings = struct.unpack_from('<2I', psm, sp)
        sp += 8
        if entries != strings or entries > 1000000:
            raise ValueError('PHM/PSM entry counts differ')
        group = {}
        for _ in range(entries):
            _, _, resource, _ = struct.unpack_from('<4I', phm, hp)
            hp += 16
            end = psm.index(b'\0', sp)
            group[(resource & 0xFF, resource >> 8)] = psm[sp:end].decode('latin-1')
            sp = end + 1
        sp = (sp + 3) & ~3
        groups.append(group)
    return groups


class World:
    def __init__(self, source):
        self.source = source
        self.iso_entry = None
        with open(source, 'rb') as f:
            magic = f.read(4)
        if magic in (b'BIGF', b'BIG4'):
            with open(source, 'rb') as f:
                big_bytes = f.read()
        else:
            self.iso_entry, big_bytes = iso9660.read_file(source, BIG_PATH)
        self.big = bigf.BigArchive.parse(big_bytes)
        self.ssb_entry = self.big.find_suffix('.ssb')
        self.sdb = sdb.parse(self._member('.sdb'))
        self.stream = ssb.WorldStream(self.big.read(self.ssb_entry))
        self._names = None

    @property
    def is_iso(self):
        return self.iso_entry is not None

    def _member(self, suffix):
        data = self.big.read(self.big.find_suffix(suffix))
        if len(data) > 5 and data[1] == 0xFB and data[0] in (0x10, 0x11, 0x90, 0x91):
            data = refpack.decompress(data)
        return data

    @property
    def names(self):
        if self._names is None:
            try:
                self._names = resource_names(self._member('.phm'), self._member('.psm'))
            except (KeyError, ValueError, struct.error, IndexError):
                self._names = []
        return self._names

    def name_of(self, group, track, rid):
        if group < len(self.names):
            return self.names[group].get((track, rid))
        return None

    # -- navigation -----------------------------------------------------------
    def location(self, name):
        return self.sdb.by_name(name)

    def chunk_label(self, chunk):
        loc = self.sdb.location_of_chunk(chunk)
        return loc.name if loc else '?'

    def chunks_for(self, location=None):
        if location is None:
            return range(len(self.stream))
        loc = self.location(location) if isinstance(location, str) else location
        return [c for c in loc.chunks if c < len(self.stream)]

    def records(self, kind=None, location=None, track=None):
        """Yield (chunk index, Record) for matching records."""
        for c in self.chunks_for(location):
            for rec in self.stream.records(c, keep=False):
                if kind is not None and rec.kind != kind:
                    continue
                if track is not None and rec.track != track:
                    continue
                yield c, rec

    def record_bytes(self, chunk, rec):
        return bytes(self.stream.current(chunk)[rec.offset:rec.offset + rec.size])

    def texture_records(self, rid):
        """Every copy of world texture `rid` (kind 9, shared track 255)."""
        return [(c, r) for c, r in self.records(kind=9) if r.rid == rid]

    # -- saving ---------------------------------------------------------------
    def save(self, output, progress=None):
        """Write the edited world. Returns the per-block re-encode report."""
        image, report = self.stream.build(progress=progress)
        self.stream.verify(image)
        self.big.replace(self.ssb_entry, image)
        payload = self.big.to_bytes()
        if os.path.abspath(output) == os.path.abspath(self.source):
            raise ValueError('refusing to overwrite the input; choose another output path')
        if self.is_iso:
            iso9660.replace_file(self.source, BIG_PATH, payload, output_path=output)
        else:
            with open(output, 'wb') as f:
                f.write(payload)
        return report
