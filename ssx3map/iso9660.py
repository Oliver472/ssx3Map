"""Find and overwrite a file inside a PS2 DVD image (ISO 9660, 2048-byte sectors).

replace_file keeps the file in its sectors (same size or smaller), so nothing
else on the disc moves. relocate_file is for a file that grew: it goes to the
end of the image and its ISO 9660 directory record and the volume size follow.
PS2 DVDs also carry a UDF bridge that points at the same sectors; the PS2 reads
ISO 9660, so a relocated file's UDF entry is left stale.
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


def list_files(image_path):
    """[(path, lba, size)] of every file in the image."""
    out = []
    with open(image_path, 'rb') as f:
        pvd = _read_at(f, 16 * SECTOR, SECTOR)
        if pvd[0] != 1 or pvd[1:6] != b'CD001':
            raise IsoError('no ISO 9660 primary volume descriptor (is this a .iso?)')
        root = pvd[156:156 + 34]
        todo = [('', *struct.unpack_from('<I', root, 2), *struct.unpack_from('<I', root, 10))]
        seen = set()
        while todo:
            prefix, lba, size = todo.pop()
            if lba in seen:
                continue
            seen.add(lba)
            for name, r_lba, r_size, flags, _ in _records(f, lba, size):
                if name in (b'\0', b'\1'):
                    continue
                path = prefix + '/' + name.decode('latin-1').split(';')[0]
                if flags & 2:
                    todo.append((path, r_lba, r_size))
                else:
                    out.append((path.lstrip('/'), r_lba, r_size))
    return sorted(out, key=lambda e: e[1])


def relocate_file(image_path, inner_path, payload, output_path, clear_old=True):
    """Copy the image to `output_path` with `inner_path` replaced by `payload` at the end of the disc.

    The old sectors are zeroed (clear_old), so a game that still read them would
    fail at once instead of quietly using the old file."""
    entry = find(image_path, inner_path)
    if os.path.abspath(output_path) == os.path.abspath(image_path):
        raise IsoError('refusing to overwrite the input image')
    shutil.copyfile(image_path, output_path)
    with open(output_path, 'r+b') as f:
        f.seek(0, os.SEEK_END)
        lba = (f.tell() + SECTOR - 1) // SECTOR
        f.seek(lba * SECTOR)
        f.write(payload)
        f.write(bytes(-len(payload) % SECTOR))
        total = lba + (len(payload) + SECTOR - 1) // SECTOR
        if clear_old:
            f.seek(entry.offset)
            left = (entry.size + SECTOR - 1) // SECTOR * SECTOR
            while left > 0:
                n = min(left, 1 << 22)
                f.write(bytes(n))
                left -= n
        f.seek(entry.record_pos + 2)
        f.write(struct.pack('<I', lba) + struct.pack('>I', lba)
                + struct.pack('<I', len(payload)) + struct.pack('>I', len(payload)))
        f.seek(16 * SECTOR + 80)                     # volume space size, both byte orders
        f.write(struct.pack('<I', total) + struct.pack('>I', total))
    return lba
