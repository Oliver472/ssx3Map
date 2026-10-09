"""Find and overwrite a file inside a PS2 DVD image (ISO 9660, 2048-byte sectors).

Only same-size or shrinking replacements are supported: the file keeps its
sectors, so nothing else on the disc moves. PS2 DVDs also carry a UDF bridge
that points at the same sectors, which stays valid as long as the size is
unchanged (the world editor never changes the size of BAM.BIG).
"""
from __future__ import annotations

import os
import shutil
import struct
from dataclasses import dataclass

SECTOR = 2048


class IsoError(ValueError):
    pass


@dataclass
class IsoFile:
    path: str
    lba: int
    size: int
    record_pos: int     # absolute offset of the directory record in the image

    @property
    def offset(self):
        return self.lba * SECTOR


def _read_at(f, pos, n):
    f.seek(pos)
    data = f.read(n)
    if len(data) != n:
        raise IsoError('unexpected end of image')
    return data


def _records(f, lba, size):
    data = _read_at(f, lba * SECTOR, size)
    pos = 0
    while pos < len(data):
        length = data[pos]
        if length == 0:
            pos = (pos // SECTOR + 1) * SECTOR      # rest of this sector is padding
            continue
        rec = data[pos:pos + length]
        r_lba, = struct.unpack_from('<I', rec, 2)
        r_size, = struct.unpack_from('<I', rec, 10)
        flags = rec[25]
        name_len = rec[32]
        name = rec[33:33 + name_len]
        yield name, r_lba, r_size, flags, lba * SECTOR + pos
        pos += length


def find(image_path, inner_path):
    parts = [p for p in inner_path.replace('\\', '/').split('/') if p]
    with open(image_path, 'rb') as f:
        pvd = _read_at(f, 16 * SECTOR, SECTOR)
        if pvd[0] != 1 or pvd[1:6] != b'CD001':
            raise IsoError('no ISO 9660 primary volume descriptor (is this a .iso?)')
        root = pvd[156:156 + 34]
        lba, = struct.unpack_from('<I', root, 2)
        size, = struct.unpack_from('<I', root, 10)
        for depth, part in enumerate(parts):
            want = part.upper()
            for name, r_lba, r_size, flags, rec_pos in _records(f, lba, size):
                if name in (b'\0', b'\1'):
                    continue
                text = name.decode('latin-1').upper().split(';')[0]
                if text == want:
                    last = depth == len(parts) - 1
                    if last:
                        if flags & 2:
                            raise IsoError(f'{inner_path} is a directory')
                        return IsoFile(inner_path, r_lba, r_size, rec_pos)
                    if not flags & 2:
                        raise IsoError(f'{part} is not a directory')
                    lba, size = r_lba, r_size
                    break
            else:
                raise IsoError(f'{inner_path} not found in the image')
    raise IsoError(f'{inner_path} not found in the image')


def read_file(image_path, inner_path):
    entry = find(image_path, inner_path)
    with open(image_path, 'rb') as f:
        return entry, _read_at(f, entry.offset, entry.size)


def replace_file(image_path, inner_path, payload, output_path=None):
    """Write `payload` over `inner_path`. With `output_path`, patch a copy instead."""
    entry = find(image_path, inner_path)
    allocated = (entry.size + SECTOR - 1) // SECTOR * SECTOR
    if len(payload) > allocated:
        raise IsoError(f'new {inner_path} is {len(payload)} bytes; only {allocated} fit in its sectors')
    target = image_path
    if output_path and os.path.abspath(output_path) != os.path.abspath(image_path):
        shutil.copyfile(image_path, output_path)
        target = output_path
    with open(target, 'r+b') as f:
        f.seek(entry.offset)
        f.write(payload)
        if len(payload) != entry.size:
            f.write(bytes(allocated - len(payload)))
            f.seek(entry.record_pos + 10)
            f.write(struct.pack('<I', len(payload)) + struct.pack('>I', len(payload)))
    return target, entry
