"""RefPack (EA "0x10FB") compression as used by the SSX 3 world stream.

Bitstream reference: http://wiki.niotso.org/RefPack and GlitcherOG's SSX-Library
(`Internal/Refpack.cs`, GPL-3.0).

Besides a plain decoder and a size-minimising encoder, this module can encode a
buffer into a stream of an *exact* compressed length. The world packer uses that
to replace a block of BAM.SSB without moving a single byte of anything after it,
so no offset table anywhere in the game data has to change.

Command forms (lit = 0..3 literal bytes that follow the command):

    0b0DDLLLPP dddddddd                     length 3..10,   distance 1..1024
    0b10LLLLLL PPDDDDDD dddddddd            length 4..67,   distance 1..16384
    0b110DLLPP dddddddd dddddddd llllllll   length 5..1028, distance 1..131072
    0b111LLLLL                              4..112 literals (multiple of 4)
    0b111111PP                              stop, 0..3 trailing literals
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    'RefPackError', 'Header', 'parse_header', 'decompress', 'compress',
    'compress_exact', 'ExactSizeError', 'stream_stats', 'prefix_sizes',
]

SHORT, MEDIUM, LONG = 0, 1, 2
FORM_SIZE = (2, 3, 4)
FORM_MIN_LEN = (3, 4, 5)
FORM_MAX_LEN = (10, 67, 1028)
FORM_MAX_DIST = (1024, 16384, 131072)
MAX_LITERAL_CMD = 112


class RefPackError(ValueError):
    pass


class ExactSizeError(RefPackError):
    """The data cannot be encoded in the requested number of bytes."""

    def __init__(self, message, minimum):
        super().__init__(message)
        self.minimum = minimum


@dataclass(frozen=True)
class Header:
    flags: int
    decompressed_size: int
    size: int            # header length in bytes
    raw: bytes

    @property
    def width(self):
        return 4 if self.flags & 0x80 else 3


def parse_header(data) -> Header:
    if len(data) < 5 or data[1] != 0xFB or (data[0] & 0x3E) != 0x10:
        raise RefPackError('not a RefPack stream (expected xx FB header)')
    flags = data[0]
    width = 4 if flags & 0x80 else 3
    size = 2 + width * (2 if flags & 0x01 else 1)
    if len(data) < size:
        raise RefPackError('truncated RefPack header')
    decompressed = int.from_bytes(data[2:2 + width], 'big')
    return Header(flags, decompressed, size, bytes(data[:size]))


def decompress(data, limit=256 * 1024 * 1024, with_consumed=False):
    """Decode one RefPack stream.

    Returns the output bytes, or (output, consumed) when `with_consumed` is set,
    where `consumed` is the offset just past the stop command (anything after it
    in `data` is padding the decoder never reads).
    """
    header = parse_header(data)
    size = header.decompressed_size
    if size > limit:
        raise RefPackError(f'RefPack output of {size} bytes exceeds limit')
    out = bytearray()
    pos = header.size
    end = len(data)
    stopped = False
    while True:
        if len(out) == size:
            # Output complete: a bare stop command may follow; anything else is
            # padding (some encoders omit the stop command).
            if pos < end and data[pos] == 0xFC:
                pos += 1
            break
        if pos >= end:
            raise RefPackError('truncated RefPack stream')
        c = data[pos]
        if c < 0x80:
            if pos + 2 > end:
                raise RefPackError('truncated RefPack command')
            b = data[pos + 1]
            lit = c & 3
            count = ((c >> 2) & 7) + 3
            dist = ((c & 0x60) << 3) + b + 1
            pos += 2
        elif c < 0xC0:
            if pos + 3 > end:
                raise RefPackError('truncated RefPack command')
            b, d = data[pos + 1], data[pos + 2]
            lit = b >> 6
            count = (c & 0x3F) + 4
            dist = ((b & 0x3F) << 8) + d + 1
            pos += 3
        elif c < 0xE0:
            if pos + 4 > end:
                raise RefPackError('truncated RefPack command')
            b, d, e = data[pos + 1], data[pos + 2], data[pos + 3]
            lit = c & 3
            count = ((c & 0x0C) << 6) + e + 5
            dist = ((c & 0x10) << 12) + (b << 8) + d + 1
            pos += 4
        elif c < 0xFC:
            lit = ((c & 0x1F) + 1) * 4
            count = dist = 0
            pos += 1
        else:
            lit = c & 3
            count = dist = 0
            pos += 1
            stopped = True
        if pos + lit > end:
            raise RefPackError('truncated RefPack literals')
        if len(out) + lit + count > size:
            raise RefPackError('RefPack command overruns the declared size')
        out += data[pos:pos + lit]
        pos += lit
        if count:
            if dist > len(out):
                raise RefPackError('RefPack match reaches before the output start')
            start = len(out) - dist
            if dist >= count:
                out += out[start:start + count]
            else:
                # Overlapping copy: repeat the `dist`-byte pattern.
                pattern = bytes(out[start:])
                reps, rest = divmod(count, dist)
                out += pattern * reps + pattern[:rest]
        if stopped:
            break
    if len(out) != size:
        raise RefPackError(f'RefPack produced {len(out)} bytes, header says {size}')
    if with_consumed:
        return bytes(out), pos
    return bytes(out)


def stream_stats(data):
    """Command statistics of an existing stream (used by `inspect`)."""
    header = parse_header(data)
    pos = header.size
    stats = dict(flags=header.flags, decompressed=header.decompressed_size, short=0, medium=0,
                 long=0, literal_cmds=0, overlapping=0, max_distance=0, stop=False, consumed=0,
                 promotable_short=0, promotable_medium=0)
    produced = 0
    while pos < len(data):
        if produced >= header.decompressed_size:
            if data[pos] == 0xFC:
                pos += 1
                stats['stop'] = True
            break
        c = data[pos]
        if c < 0x80:
            lit, count, dist, n = c & 3, ((c >> 2) & 7) + 3, ((c & 0x60) << 3) + data[pos + 1] + 1, 2
            stats['short'] += 1
        elif c < 0xC0:
            b = data[pos + 1]
            lit, count, dist, n = b >> 6, (c & 0x3F) + 4, ((b & 0x3F) << 8) + data[pos + 2] + 1, 3
            stats['medium'] += 1
        elif c < 0xE0:
            lit = c & 3
            count = ((c & 0x0C) << 6) + data[pos + 3] + 5
            dist = ((c & 0x10) << 12) + (data[pos + 1] << 8) + data[pos + 2] + 1
            n = 4
            stats['long'] += 1
        elif c < 0xFC:
            lit, count, dist, n = ((c & 0x1F) + 1) * 4, 0, 0, 1
            stats['literal_cmds'] += 1
        else:
            pos += 1 + (c & 3)
            produced += c & 3
            stats['stop'] = True
            break
        if count:
            stats['max_distance'] = max(stats['max_distance'], dist)
            if dist < count:
                stats['overlapping'] += 1
            # Would a longer encoding of the same match have been legal?
            if c < 0x80 and count >= 4:
                stats['promotable_short'] += 1
            elif 0x80 <= c < 0xC0 and count >= 5:
                stats['promotable_medium'] += 1
        pos += n + lit
        produced += lit + count
    stats['consumed'] = pos
    return stats


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------
#
# A parse is a list of tokens [lits, length, dist, form, lcmds]: `lits` literal
# bytes, then a match of `length` bytes at `dist` (length 0 = the final stop
# token, END = the end of a spliced range, which carries no command). `lcmds` is the number of 0xE0..0xFB commands that carry the multiple-
# of-4 part of the literals; the remaining lits % 4 ride on the match/stop
# command itself.

def _min_lcmds(lits):
    four = lits & ~3
    return (four + MAX_LITERAL_CMD - 1) // MAX_LITERAL_CMD


def _max_lcmds(lits):
    return lits >> 2


def _best_form(length, dist):
    for form in (SHORT, MEDIUM, LONG):
        if FORM_MIN_LEN[form] <= length <= FORM_MAX_LEN[form] and dist <= FORM_MAX_DIST[form]:
            return form
    return None


END = -1    # token length of the last token of a range that ends without a stop command


def _token_size(tok):
    lits, length, _dist, form, lcmds = tok
    return lcmds + lits + (FORM_SIZE[form] if length > 0 else 1 if length == 0 else 0)


def _match_len(data, i, j, limit):
    """Length of the common prefix of data[i:] and data[j:], at most `limit`."""
    if data[i:i + limit] == data[j:j + limit]:
        return limit
    lo, hi = 0, limit       # data[i:i+lo] == data[j:j+lo], data[i:i+hi] != ...
    while hi - lo > 1:
        mid = (lo + hi) >> 1
        if data[i + lo:i + mid] == data[j + lo:j + mid]:
            lo = mid
        else:
            hi = mid
    return lo


def _find_matches(data, start, end, max_chain=64, nice=192):
    """For positions start..end-1: a staircase of (length, dist) with growing lengths.

    Matches may reach back before `start` (up to the 128 KiB window) but never
    past `end`.
    """
    table = {}
    found = {}
    skip_until = 0
    carry = None            # (dist, end) of a long match whose interior we skip
    window = FORM_MAX_DIST[LONG]
    for i in range(max(0, start - window), end - 2):
        key = data[i:i + 3]
        chain = table.get(key)
        if i >= start:
            if i < skip_until and carry is not None:
                dist, stop = carry
                length = min(stop - i, FORM_MAX_LEN[LONG])
                if length >= 3:
                    found[i] = [(length, dist)]
            else:
                carry = None
                if chain:
                    limit_total = min(FORM_MAX_LEN[LONG], end - i)
                    best = 2
                    stairs = []
                    checked = 0
                    for j in reversed(chain):
                        dist = i - j
                        if dist > window or best >= limit_total:
                            break
                        checked += 1
                        if data[j + best] == data[i + best]:
                            length = _match_len(data, i, j, limit_total)
                            if length > best and (_best_form(length, dist) is not None or length >= 5):
                                # Closer matches already cover every length <= best.
                                stairs.append((length, dist))
                                best = length
                        if checked >= max_chain:
                            break
                    if stairs:
                        found[i] = stairs
                        if best >= nice:
                            carry = (stairs[-1][1], i + best)
                            skip_until = i + best
        if chain is None:
            table[key] = [i]
        else:
            chain.append(i)
            if len(chain) > 4 * max_chain:
                del chain[:len(chain) - 2 * max_chain]
    return found


_LIT_COST = 1.0 + 1.0 / 28.0    # one byte, plus a literal command per 112 bytes


def _optimal_parse(data, max_chain, start=0, end=None, final=True):
    """Cheapest token list for data[start:end] (approximate literal costs)."""
    end = len(data) if end is None else end
    matches = _find_matches(data, start, end, max_chain=max_chain)
    INF = float('inf')
    span = end - start
    cost = [INF] * (span + 1)
    step = [None] * (span + 1)          # None = literal, else (length, dist)
    cost[span] = 1.0 if final else 0.0
    for k in range(span - 1, -1, -1):
        best = cost[k + 1] + _LIT_COST
        choice = None
        stairs = matches.get(start + k)
        if stairs:
            prev = 2
            for length, dist in stairs:
                lo = prev + 1
                if length - lo <= 48:
                    candidates = range(lo, length + 1)
                else:
                    candidates = set(range(lo, lo + 16)) | set(range(length - 16, length + 1))
                    candidates |= {b for b in (10, 11, 67, 68) if lo <= b <= length}
                for ln in candidates:
                    form = _best_form(ln, dist)
                    if form is None:
                        continue
                    c = FORM_SIZE[form] + cost[k + ln]
                    if c < best:
                        best = c
                        choice = (ln, dist)
                prev = length
        cost[k] = best
        step[k] = choice
    tokens = []
    k = lits = 0
    while k < span:
        choice = step[k]
        if choice is None:
            lits += 1
            k += 1
        else:
            length, dist = choice
            tokens.append([lits, length, dist, _best_form(length, dist), _min_lcmds(lits)])
            lits = 0
            k += length
    tokens.append([lits, 0 if final else END, 0, SHORT, _min_lcmds(lits)])
    if not final:
        _fix_tail(tokens)
    return tokens


def _fix_tail(tokens):
    """A range without a stop must end on whole literal commands (lits % 4 == 0)."""
    while tokens[-1][0] % 4:
        if len(tokens) < 2:
            raise ExactSizeError('range cannot end on a command boundary', None)
        need = 4 - tokens[-1][0] % 4
        prev = tokens[-2]
        new_len = prev[1] - need
        form = _best_form(new_len, prev[2]) if new_len >= 3 else None
        if form is not None:
            prev[1], prev[3] = new_len, form
            tokens[-1][0] += need
        else:
            # Drop the match entirely; its bytes become literals of the tail.
            merged = prev[0] + prev[1] + tokens[-1][0]
            tokens[-1][0] = merged
            del tokens[-2]
        tokens[-1][4] = _min_lcmds(tokens[-1][0])


def _emit(data, tokens, header=b'', start=0, end=None):
    end = len(data) if end is None else end
    out = bytearray(header)
    pos = start
    for lits, length, dist, form, lcmds in tokens:
        four = lits & ~3
        if lcmds:
            # Split `four` literals over `lcmds` commands, each 4..112 and a multiple of 4.
            groups = four >> 2
            base, extra = divmod(groups, lcmds)
            for k in range(lcmds):
                g = base + (1 if k < extra else 0)
                if not 1 <= g <= 28:
                    raise AssertionError('bad literal split')
                out.append(0xE0 | (g - 1))
                out += data[pos:pos + 4 * g]
                pos += 4 * g
        elif four:
            raise AssertionError('literals without a literal command')
        rest = lits & 3
        if length == END:
            if rest:
                raise AssertionError('range ends inside a literal group')
            continue
        if length == 0:
            out.append(0xFC | rest)
            out += data[pos:pos + rest]
            pos += rest
            continue
        d = dist - 1
        if form == SHORT:
            out.append(((d >> 3) & 0x60) | ((length - 3) << 2) | rest)
            out.append(d & 0xFF)
        elif form == MEDIUM:
            out.append(0x80 | (length - 4))
            out.append((rest << 6) | (d >> 8))
            out.append(d & 0xFF)
        else:
            out.append(0xC0 | ((d >> 12) & 0x10) | (((length - 5) >> 8) << 2) | rest)
            out.append((d >> 8) & 0xFF)
            out.append(d & 0xFF)
            out.append((length - 5) & 0xFF)
        out += data[pos:pos + rest]
        pos += rest + length
    if pos != end:
        raise AssertionError('parse does not cover the input')
    return bytes(out)


def make_header(decompressed_size, flags=None, compressed_size=None):
    if flags is None:
        flags = 0x10 if decompressed_size <= 0xFFFFFF else 0x90
    width = 4 if flags & 0x80 else 3
    if decompressed_size >= 1 << (8 * width):
        raise RefPackError('decompressed size does not fit the header')
    raw = bytes([flags, 0xFB]) + decompressed_size.to_bytes(width, 'big')
    if flags & 0x01:
        raw += (compressed_size or 0).to_bytes(width, 'big')
    return raw


def compress(data, header=None, max_chain=64):
    """Compress `data` as small as this encoder can manage."""
    data = bytes(data)
    header = header if header is not None else make_header(len(data))
    tokens = _optimal_parse(data, max_chain)
    return _emit(data, tokens, header)


def prefix_sizes(data, max_chain=64):
    """Upper estimates of the stream size (header and stop included) of every prefix data[:n].

    A forward pass of the optimal parse; one call replaces a bisection of
    compress() calls when cutting a buffer into blocks of a given capacity.
    """
    n = len(data)
    matches = _find_matches(data, 0, n, max_chain=max_chain)
    INF = float('inf')
    cost = [INF] * (n + 1)
    cost[0] = 0.0
    for k in range(n):
        c = cost[k]
        if c + _LIT_COST < cost[k + 1]:
            cost[k + 1] = c + _LIT_COST
        stairs = matches.get(k)
        if not stairs:
            continue
        prev = 2
        for length, dist in stairs:
            for ln in range(prev + 1, length + 1):
                form = _best_form(ln, dist)
                if form is not None:
                    v = c + FORM_SIZE[form]
                    if v < cost[k + ln]:
                        cost[k + ln] = v
            prev = length
    head = len(make_header(max(n, 1)))
    return [head + 4 + c * 1.002 for c in cost]


def _promo_room(length, dist, form):
    """How many +1 promotions (short->medium->long) this match allows."""
    room = 0
    while form < LONG:
        nxt = form + 1
        if not (FORM_MIN_LEN[nxt] <= length <= FORM_MAX_LEN[nxt] and dist <= FORM_MAX_DIST[nxt]):
            break
        form = nxt
        room += 1
    return room


def _grow(tokens, deficit):
    """Make the encoded token list exactly `deficit` bytes longer.

    Uses, in order of preference: turning whole matches into literals (large
    steps), then promoting a match to a longer command form or splitting a
    literal command in two (+1 byte each). Returns False if it cannot.
    """
    def fine_capacity():
        return sum(_max_lcmds(t[0]) - t[4] + (_promo_room(t[1], t[2], t[3]) if t[1] > 0 else 0)
                   for t in tokens)

    # Large steps first: replace matches by literals while the gain still fits
    # and enough fine-grained capacity remains to land exactly.
    while deficit > 0 and fine_capacity() < deficit:
        best_k = None
        best_gain = 0
        for k in range(len(tokens) - 1):
            a, b = tokens[k], tokens[k + 1]
            merged_lits = a[0] + a[1] + b[0]
            if b[1] == END and merged_lits % 4:
                continue
            before = _token_size(a) + _token_size(b)
            after = _token_size([merged_lits, b[1], b[2], b[3], _min_lcmds(merged_lits)])
            gain = after - before
            if 0 < gain <= deficit and gain > best_gain:
                best_gain, best_k = gain, k
        if best_k is None:
            break
        a, b = tokens[best_k], tokens[best_k + 1]
        merged = a[0] + a[1] + b[0]
        tokens[best_k + 1] = [merged, b[1], b[2], b[3], _min_lcmds(merged)]
        del tokens[best_k]
        deficit -= best_gain

    # +1 steps: promote short->medium->long, then split literal commands.
    for tok in tokens:
        if deficit <= 0:
            break
        if tok[1] > 0:
            take = min(_promo_room(tok[1], tok[2], tok[3]), deficit)
            tok[3] += take
            deficit -= take
    for tok in tokens:
        if deficit <= 0:
            break
        take = min(_max_lcmds(tok[0]) - tok[4], deficit)
        tok[4] += take
        deficit -= take
    return deficit == 0


def _encode_exact(data, target_size, header, start, end, final, max_chain):
    tokens = _optimal_parse(data, max_chain, start, end, final)
    size = len(header) + sum(_token_size(t) for t in tokens)
    # A deeper match search saves a little; only worth its time when that could be enough.
    if target_size < size <= target_size + max(64, (end or len(data)) // 200) and max_chain < 512:
        tokens = _optimal_parse(data, 512, start, end, final)
        size = len(header) + sum(_token_size(t) for t in tokens)
    if size > target_size:
        raise ExactSizeError(f'needs at least {size} bytes, only {target_size} available', size)
    if size < target_size and not _grow(tokens, target_size - size):
        raise ExactSizeError(f'cannot pad the encoding from {size} to {target_size} bytes', size)
    out = _emit(data, tokens, header, start, end)
    if len(out) != target_size:
        raise AssertionError(f'exact encoder produced {len(out)} bytes, wanted {target_size}')
    return out


def compress_exact(data, target_size, header=None, max_chain=64):
    """Encode `data` into exactly `target_size` bytes (header included).

    Raises ExactSizeError (with `.minimum`) if the encoder cannot get that small,
    or cannot pad up to that size.
    """
    data = bytes(data)
    header = header if header is not None else make_header(len(data))
    out = _encode_exact(data, target_size, header, 0, len(data), True, max_chain)
    if decompress(out) != data:
        raise AssertionError('exact encoder round trip failed')
    return out


# --------------------------------------------------------------------------
# Splicing: re-encode only the commands around an edit
# --------------------------------------------------------------------------

def parse_commands(stream):
    """[(stream offset, output offset)] for every command, plus the end markers.

    The last element is (offset just past the stop command, decoded size).
    """
    header = parse_header(stream)
    size = header.decompressed_size
    pos, produced = header.size, 0
    cmds = []
    while True:
        if produced >= size:
            if pos < len(stream) and stream[pos] == 0xFC:
                cmds.append((pos, produced))
                pos += 1
            break
        c = stream[pos]
        cmds.append((pos, produced))
        if c < 0x80:
            lit, count, n = c & 3, ((c >> 2) & 7) + 3, 2
        elif c < 0xC0:
            lit, count, n = stream[pos + 1] >> 6, (c & 0x3F) + 4, 3
        elif c < 0xE0:
            lit, count, n = c & 3, ((c & 0x0C) << 6) + stream[pos + 3] + 5, 4
        elif c < 0xFC:
            lit, count, n = ((c & 0x1F) + 1) * 4, 0, 1
        else:
            pos += 1 + (c & 3)
            produced += c & 3
            break
        pos += n + lit
        produced += lit + count
    cmds.append((pos, produced))
    return cmds


def splice(stream, old, new, max_chain=64):
    """Re-encode `stream` (which decodes to `old`) so that it decodes to `new`,
    same length, keeping every command before and after the edited range.

    Only the commands that cover the changed bytes (plus any later command that
    copied from them) are re-encoded, into exactly the bytes they occupied.
    Returns the new stream, or raises ExactSizeError.
    """
    stream, old, new = bytes(stream), bytes(old), bytes(new)
    if len(old) != len(new):
        raise RefPackError('splice needs same-size data')
    if old == new:
        return stream
    first = next(i for i in range(len(new)) if old[i] != new[i])
    last = next(i for i in range(len(new) - 1, -1, -1) if old[i] != new[i])
    cmds = parse_commands(stream)
    consumed = cmds[-1][0]
    starts = [o for _, o in cmds[:-1]]
    # a: last command starting at or before the first change.
    a = max(i for i, o in enumerate(starts) if o <= first)
    # b: first command starting after the last change (None: re-encode to the end).
    b = next((i for i, o in enumerate(starts) if o > last and i > a), None)
    extra = 0
    for _ in range(64):
        if b is not None and b >= len(starts):
            b = None
        lo_pos, lo_out = cmds[a]
        if b is None:
            hi_pos, hi_out = consumed, len(new)
        else:
            hi_pos, hi_out = cmds[b]
        try:
            middle = _encode_exact(new, hi_pos - lo_pos, b'', lo_out, hi_out, b is None, max_chain)
        except ExactSizeError:
            if b is None and a == 0:
                raise
            # Widen the range: more original bytes to spend.
            extra = max(1, extra * 2)
            a = max(0, a - extra)
            b = None if b is None else b + extra
            continue
        candidate = stream[:lo_pos] + middle + stream[hi_pos:]
        try:
            got = decompress(candidate)
        except RefPackError:
            got = None
        if got == new:
            return candidate
        if got is None or b is None:
            b = None if b is None else b + max(1, extra)
            continue
        # A later, kept command copied bytes we changed: re-encode up to it.
        bad = next(i for i in range(hi_out, len(new)) if got[i] != new[i])
        b = next((i for i, o in enumerate(starts) if o > bad), None)
    raise ExactSizeError('splice did not converge', None)
