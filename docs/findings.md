# Findings from the real BAM.BIG (NTSC-U, `inspect` report 2026-10-09)

BAM.BIG sha1 `d28a53689d0d9ebab598da5265893f0bee368aa2`, on the disc from LBA 865271, 113 078 400 bytes.
Members: `bam.sdb` (32 780), `bam.ssb` (109 051 904), `bam.phm`, `bam.psm`, `serial.txt`.

## bam.ssb
- 3 328 blocks, 159 chunks. **Every block spans exactly 32 768 bytes and starts at a
  multiple of 32 768.** The RefPack stream is shorter and zeros follow it (padding 3 to 31 959 B,
  median 14). So the game tolerates padding after the stop command. An edited block may grow up to 32 760 B.
- A decoded block holds at most 81 920 B (331 blocks are exactly 81 920).
- The RefPack header is always `0x10 FB` + 3 B. Every stream ends with a stop command.
- On 24 samples our encoder is 313 B smaller than the original in the median (range −1285 to +34).
- 161 953 347 decoded bytes in total.

## bam.sdb
- 49 locations, 183 chunk infos (96 B), 159 sub-chunk infos (68 B); the file size matches exactly.
- The third u32 of a location record is the location's last chunk (inclusive). A location's chunks are
  consecutive; every location has texture chunks (kinds 9/10 only) and a last "main" chunk with everything else.
- **Sub-chunk info: the u32 at +4 = byte offset of the chunk in bam.ssb** (e.g. chunk 1 → 131072,
  chunk 2 → 688128); for texture chunks the u32 at +8 equals the decoded chunk size.
  First u16 = record count, second = chunk index, then record counts per kind.
- Chunk info: bbox min/max (2× vec4), 48 B of zeros, then 4 ints (−1, −1, chunk index, 0) – a tree.

## Records
- Terrain (kind 1, 30 644 records of 432 B):
  - `+0x40` 16× vec4 coefficients; `coeff[15]` is the point P(0,0)
  - `+0x140` bounding sphere (centre xyz, radius)
  - `+0x150` resource (rid<<8 | track)
  - `+0x156` s16 = index of the texture chunk (not the patch's own chunk)
  - `+0x158` bbox min, `+0x164` bbox max
  - `+0x170`, `+0x17C`, `+0x188`, `+0x194` four corner points (SSX-Library has bbox and points in the opposite order)
  - `+0x1A0` texture, `+0x1A2` light page
- Instances (kind 3): `+0x78` = resource, `+0x50` xyz = centre of the bbox (`+0x60`/`+0x6C`), `+0x10` 4×4 matrix (rows).
- Textures: 6 203 records, 788 IDs, all on track 255; copies of the same ID are identical.
  Formats: 4-bit 4 258×, 8-bit 1 771×, RGBA 174×.
- Painter (kind 15): one per location; the skies and TRANSP have an empty one (8 B).
  ARA1 has 9 fog payloads, the first: near 3000, far 10000, colour (0.70, 0.82, 1.00), density 2.

# Second report (inspect with geometry)

## bam.sdb, sub-chunk info (68 B, one per chunk)
`u16 record count, u16 chunk index, u32 chunk offset in bam.ssb, u32 size,
u16 × 13 record counts of kinds 0..12, …, 7 × u32 zeros`. Checked on 159/159 chunks.
- The size equals the decoded size for the 110 texture chunks. For main chunks it is
  smaller. For ASKY the difference is exactly 268 B, the sum of (8 + size) of the records of kinds
  13, 14, 15, 16, 18, 20 and 22. Hypothesis: **size = Σ (8 + size) of the records of kinds 0..12**.
  The next report will check it.
- 28 shorts in the location record: the first 23 are record counts per kind. They match the
  records of the last chunk for 43 of 49 locations (not for TRANSP and the 5 skies, which keep
  textures in the main chunk). Hypothesis: only records on the location's own track are counted.

## Terrain, rails
- A patch's bbox = the bbox of the Bézier control net (300/300 exact). The sphere holds the whole surface.
- Rail segment: the row at +0x50 is zero (65/65 in the sample), so a rail can be moved by
  adding to +0x48 (M3.z) and to the bboxes.
- On Snow Jam a typical patch is larger than the 4.8 m of the first ABA1 sample. A 15 m jump hit only
  4 patches, so shapes must be at least 1.5× larger than a patch.

## AIP (kind 14, rid 0)
- Snow Jam: 129 AI paths, 8 track paths, 14 regions (6 start positions and 8 session points).
- The course is split into several track paths (sections). The longest Snow Jam section is 882 m; the whole
  course is their chain. Segment = (direction xyz, length): the polyline fits the bounds on every course.
- Snow Jam start: (-1318.8, 138.6, -2287.7) m, the same as in ssx-web.

# Formats for moving the course (`warp`)

Record layouts the move rewrites. Sources: ssx-web (RAIL_RECOVERY.md,
RACE_EVENT_RECOVERY.md, export_progress_meter.py, export_local_lights.py,
export_light_glow.py, export_camera_triggers.py, export_course_initial.py)
and SSX-Library (WorldParticleInstance, WorldVisCurtain). Checked on the disc
in those projects; here so far only on synthetic data.

- **Rail (kind 8):** 48 B header (`+0x04`/`+0x10` bbox, `+0x20` segment
  count), 144 B segment: `+0x0C` arc length, `+0x10..+0x4F` rows for t³, t², t, 1
  (`+0x40` = start, w = 1), `+0x50` small values (the game does not read them),
  `+0x60/+0x64` previous/next segment, `+0x68` rid, `+0x6C/+0x78` bbox,
  `+0x84` distance from the rail start (= previous + length), `+0x8C` = 15.
- **AIP (kind 14):** a segment is (horizontal direction x, y, climb per cm, horizontal
  length); events (`type, value, start, end`) are horizontal distances
  from the path start. The float in a race path's header is the remaining
  distance to the finish at its start. Type 18 = checkpoint, 0 = finish.
- **Progress meter (kind 21):** `u32 N, u32 gate offset, u32 M, u32 marker
  offset, f32 total length`; a 20 B gate = cross axis (x, y), centre (x, y),
  distance from the start; an 8 B marker = type (0 start, 1 checkpoint, 2 finish), distance.
- **Local light (kind 6, 112 B):** `+56` position, `+44` cone axis, `+68/+80` bbox.
- **Light glow (kind 7, 80 B):** `+28` position, `+40/+52` bbox.
- **Particle (kind 5, 144 B):** matrix `+0x10` (translation `+0x40`), sphere `+0x50`,
  bbox `+0x68/+0x74`.
- **Visibility curtain (kind 11, 208 B):** sphere `+0`, four corners `+0x10..+0x40`,
  plane `+0x50` (normal, d), bbox `+0xA0/+0xAC`.
- **Camera triggers (kind 17):** version 7, triggers with a volume (position,
  scale, rotation Z/X/Y) and two actions (switch, bounded camera
  with a look-at point and a bounding object, spline, none).

The move computes how much the course length changes along the chain of race paths. Distances
past the bend shift by that much (events, remaining distance, gates and markers
of the progress meter). The game should then count the finish, checkpoints and rider order
correctly. This still has to be checked in PCSX2.

# Drawing new terrain (`flat`)

Learned from the first plain slope in PCSX2: riders rode on the new ground (collision works)
but the surface was invisible.

- `+0x0C` is a layer word: `0x29` = base texture layer 1 + light layer 5.
- `+0x155` (track) and `+0x156` (texture chunk) decide when a patch is drawn: the game draws it
  only while that chunk is loaded, and texture chunks are streamed in by race progress.
  Collision does not depend on this.
- So every new patch copies the streaming chunk, texture and layer word from the old patch
  that was at the same distance along the course. Whether this fixes the invisible surface
  still has to be confirmed in PCSX2.

# Growing the world (`probe`, first round in PCSX2)

Tests 1 to 5 worked in PCSX2; test 6 did not.

- **BAM.BIG can move.** With the file at the end of the disc and its old sectors zeroed, the
  game still runs: it finds BAM.BIG through the ISO 9660 directory, so a bigger BAM.BIG can
  live at the end of the image.
- **Chunks can move.** With the first chunk one block longer and every other chunk shifted, the
  game still runs: it finds chunks through the sub-chunk offsets (`+4`) in `bam.sdb`. A chunk
  can therefore grow by whole blocks.
- **Records can be added.** One new terrain patch on Snow Jam worked: a ramp with the next free
  rid, inserted after the last terrain record, with the record count (`+0`), the kind-1 count
  and the size (`+8`) of its sub-chunk info and the location's kind-1 count updated.
- **Memory.** Snow Jam's main chunk (ARA1, 3 675 patches) worked with +10 % and +50 % more
  patches (tiny ones, hidden under the ground) but not with +100 %. The limit is between about
  +1 840 and +3 675 patches (+0.8 to +1.6 MB decoded). Until it is narrowed down, +50 % is the
  tested headroom.
