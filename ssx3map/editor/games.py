"""Finding the game on this computer, remembering choices, starting PCSX2."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from .. import iso9660
from ..world import BIG_PATH

CONFIG = os.path.join(os.path.expanduser('~'), '.ssx3map.json')
ORIGINAL_BIG_SHA1 = 'd28a53689d0d9ebab598da5265893f0bee368aa2'     # NTSC-U BAM.BIG as on the disc
SKIP_DIRS = {'Library', 'Applications', 'node_modules', '.git', '.Trash', 'Pictures', 'Music', 'Movies'}


def load_config():
    try:
        with open(CONFIG, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data):
    try:
        with open(CONFIG, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


def remember(path, key='recent'):
    data = load_config()
    items = [p for p in data.get(key, []) if p != path]
    data[key] = [path] + items[:9]
    save_config(data)


def search_roots():
    home = os.path.expanduser('~')
    roots = [os.path.join(home, d) for d in ('Documents', 'Downloads', 'Desktop')]
    return [r for r in roots if os.path.isdir(r)] + [home]


def _is_game(path):
    try:
        iso9660.find(path, BIG_PATH)
        return True
    except (OSError, ValueError):
        return False


def _is_original(path, cache):
    """Does the disc hold the untouched BAM.BIG? (cached by path, size and time)"""
    st = os.stat(path)
    key = f'{path}|{st.st_size}|{int(st.st_mtime)}'
    if key in cache:
        return cache[key]
    try:
        entry = iso9660.find(path, BIG_PATH)
        h = hashlib.sha1()
        with open(path, 'rb') as f:
            f.seek(entry.lba * iso9660.SECTOR)
            left = entry.size
            while left > 0:
                block = f.read(min(left, 1 << 22))
                if not block:
                    break
                h.update(block)
                left -= len(block)
        cache[key] = h.hexdigest() == ORIGINAL_BIG_SHA1
    except (OSError, ValueError):
        cache[key] = False
    return cache[key]


def find_games(roots=None, depth=4, budget=4.0, min_size=64 * 1024):
    """SSX 3 disc images under the usual folders (and the remembered ones), newest first."""
    data = load_config()
    seen, found = set(), []
    deadline = time.time() + budget

    def consider(path):
        path = os.path.abspath(path)
        if path in seen or not os.path.isfile(path):
            return
        seen.add(path)
        try:
            if os.path.getsize(path) < min_size or not _is_game(path):
                return
        except OSError:
            return
        found.append(path)

    for p in data.get('recent', []):
        consider(p)
    for root in roots or search_roots():
        base = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if time.time() > deadline:
                break
            if dirpath.count(os.sep) - base >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in SKIP_DIRS]
            for name in filenames:
                if name.lower().endswith('.iso') and not name.startswith('._'):
                    consider(os.path.join(dirpath, name))
    cache = data.setdefault('original_cache', {})
    out = []
    for path in found:
        st = os.stat(path)
        out.append(dict(path=path, name=os.path.basename(path), folder=os.path.dirname(path),
                        size=st.st_size, modified=st.st_mtime, original=_is_original(path, cache)))
    save_config(data)
    out.sort(key=lambda g: (not g['original'], -g['modified']))
    return out


def find_pcsx2():
    """The PCSX2 program to start a disc with, or None."""
    if sys.platform == 'darwin':
        for base in ('/Applications', os.path.expanduser('~/Applications')):
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for name in names:
                if name.lower().startswith('pcsx2') and name.endswith('.app'):
                    return os.path.join(base, name)
        return None
    for exe in ('pcsx2-qt', 'pcsx2', 'PCSX2'):
        path = shutil.which(exe)
        if path:
            return path
    return None


def start_pcsx2(iso):
    app = find_pcsx2()
    if app is None:
        raise FileNotFoundError('PCSX2 not found (looked in Applications); start the game yourself')
    if sys.platform == 'darwin':
        cmd = ['open', '-n', '-a', app, '--args', iso]
    else:
        cmd = [app, iso]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return app
