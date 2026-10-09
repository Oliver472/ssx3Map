# Command line

Everything the editor does can also be done from a terminal. Run the commands from the
project folder. On macOS and Linux type `python3`, on Windows `python`. Every command
explains its options with `--help`, for example `python3 -m ssx3map flat --help`.

The input is the disc image (`.iso`) or `BAM.BIG` on its own; the output is then the same
kind of file. The original is never changed. Several edits can be chained: use the output of
one command as the input of the next.

## Overview

| Command | What it does |
|---|---|
| `editor` | the map editor in the browser |
| `flat` | wipe a course and build a plain slope with jumps |
| `build` | build a new course layout from a recipe |
| `terrain` | add a jump, hill, dip, plateau or flatten an area |
| `warp` | move a piece of the course sideways with everything on it |
| `objects` | list, move or remove objects (trees, buildings…) |
| `map` | draw a course from above as SVG |
| `fog` | show or change the fog |
| `textures` / `tint` | export textures as PNG / recolour them |
| `info`, `list`, `inspect` | what is in the world data |

## Plain slope instead of a course (`flat`)

```
python3 -m ssx3map flat "SSX 3 (USA).iso" --map slope.svg -o SSX3_slope.iso
```

Wipes the course and builds a straight piste with a gentle fall, walls on both sides and jumps
with landings. It follows the (smoothed) route of the old course, so it has the same length and
stays in the game's original space.

Defaults: Snow Jam (`--location ARA1`), grade 15 %, width 60 m, walls 12 m high, a jump every
350 m (3, 4, 5 m). Change them with e.g. `--grade 10 --width 80 --jumps 300,700,1200
--jump-heights 4,6`, or `--no-jumps`.

What happens:
- **Terrain:** every patch of the course is reused for the new slope. Leftovers are hidden 1 km
  under the start.
- **Removed:** trees, buildings, rails, lights, particles and visibility curtains move 1 km under
  the mountain.
- **Carried onto the new slope:** start, finish, checkpoints, AI riders, reset points, the
  progress meter, cameras and the game's helper objects (start, finish, triggers).
- **Light:** the game's shadows are baked, so every patch gets a brightness from how it faces the
  sun; jumps and walls stay visible.

Connectors to other courses (e.g. `A_ARA1`) stay as they were.

## A new course from a recipe (`build`)

```
python3 -m ssx3map build "SSX 3 (USA).iso" snowjam_oliver --map new_course.svg -o SSX3_new_course.iso
```

A recipe is a list of edits in one JSON file. The built-in `snowjam_oliver` rebuilds Snow Jam
with six bends, eight jumps, rollers, a dip and a table-top. Start and finish stay; AI riders,
reset points and checkpoints move with the course. Open the `.svg` in a browser to see the new
course from above.

Every step is printed. A step that cannot be done at its place (the terrain would not hold it)
is skipped and the rest goes on. The edits must fit into the game's fixed-size blocks; the
command checks this before saving and, if needed, leaves out bends first (they rewrite the most
data), then the last jumps, and says which. It takes a few minutes and uses every CPU core.

Without bends (only jumps and rollers):

```
python3 -m ssx3map build "SSX 3 (USA).iso" snowjam_oliver --skip warp -o SSX3_jumps.iso
```

Your own recipe is plain JSON; `ssx3map/recipes/snowjam_oliver.json` is an example. Places are
given in metres from the start (`along`), optionally with `side`, `at`, `start` or `session`
as on the command line. Steps:

| `op` | what it does | parameters |
|---|---|---|
| `warp` | move a piece of the course | `right`, `ahead`, `lift` (m), `turn` (°), `radius`, `edge` |
| `kicker` | jump | `height`, optional `length`, `width`, `drop`, `edge`, `rotate` |
| `bump` | hill / dip | `height`, `radius` |
| `plateau` | table-top | `height`, `radius`, `edge` |
| `flatten` | flatten | `height`, `radius`, `edge` |
| `objects` | remove / raise objects | `radius`, `name`, `remove` or `raise` |

## Terrain shapes (`terrain`)

The terrain is also the collision: whatever you raise or lower is what riders ride on.

Draw the course from above first, and open the SVG in a browser. The red line is the course with
marks in metres from the start, green dots are objects; hovering shows coordinates.

```
python3 -m ssx3map map "SSX 3 (USA).iso" --location ARA1 -o snowjam.svg
```

Choose the place with one of:
- `--along 250`: 250 m from the start along the course,
- `--side 6`: 6 m to the right of that point (negative = left),
- `--at X Y`: coordinates in metres from the map,
- `--start`: at the start,
- `--session K`: at reset point K (numbers are on the map).

Shapes: `kicker` (jump facing down the course; `--height`, `--length`, `--width`, `--drop`),
`bump` (hill, a dip with a negative height; `--height`, `--radius`), `plateau` (raised or lowered
area with a flat top), `flatten` (levels the area to the height of the point; `--height` adds to it).

```
# a 3 m jump 250 m after the Snow Jam start (sizes from the terrain)
python3 -m ssx3map terrain "SSX 3 (USA).iso" --location ARA1 --along 250 \
    --shape kicker --height 3 -o SSX3_jump.iso
```

Sizes do not have to be given: they follow the size of the terrain patches at that place (about
20 m on Snow Jam). The result is compared with the intended shape; if it is off by more than 15 %
of the height, the edit is refused without `--force` (the shape is too small for the patches).
Objects, start and reset points on the edited terrain move with it, and rails are raised or
lowered too (only a rail longer than the whole edit stays, with a warning).

## Moving a piece of the course (`warp`)

```
# 250 m after the Snow Jam start, move 40 m of the course 12 m to the right
python3 -m ssx3map warp "SSX 3 (USA).iso" --location ARA1 --along 250 \
    --right 12 --radius 20 -o SSX3_moved.iso
```

Everything within `--radius` moves as a whole; over the width `--edge` the move fades out and
the terrain stretches or squeezes. Moved together with the terrain:
- objects and their collision, particles, lights and their glow,
- rails: every segment bends and the lengths are recomputed,
- AI and race paths: points, events (checkpoints, finish) and the distance to the finish,
- the start grid and reset points, including their direction,
- camera triggers, visibility curtains and the progress meter.

The course length changes and the command says by how much. The edge must be wide enough: a move
that would squeeze the terrain below 35 % is refused without `--force`, and one that would fold
the terrain over itself is never done. The default edge is 2.5× the move (at least 40 m).
Sound triggers and stage scripts cannot be read yet, so they stay where they were (the command
lists them).

## Objects (`objects`)

```
python3 -m ssx3map objects "SSX 3 (USA).iso" --location ARA1 --along 400 --radius 25
python3 -m ssx3map objects "SSX 3 (USA).iso" --location ARA1 --along 400 --radius 25 --name tree --remove -o SSX3_no_trees.iso
```

Removing moves an object 1 km under the mountain together with its collision. The game's helper
objects (start, triggers, resets, mode fences) are left alone without `--force`.

## Fog (`fog`)

```
python3 -m ssx3map fog "SSX 3 (USA).iso" --all                       # show the current fog
python3 -m ssx3map fog "SSX 3 (USA).iso" --location ARA1 --location A_ARA1 --location ARA1_B \
    --color 1 0.2 0.2 --near 1000 --far 8000 -o SSX3_red_fog.iso   # red fog on Snow Jam
```

`--color R G B` (0 to 1), `--near` / `--far` (centimetres), `--density`, `--scale-distance X`
(multiplies near and far). Every location has its own fog and the game switches it by the terrain
the rider stands on; a race also loads the connectors (for Snow Jam `A_ARA1` and `ARA1_B`), so
edit them together.

## Textures (`textures`, `tint`)

```
python3 -m ssx3map textures "SSX 3 (USA).iso" --location ARA1 --export textures
python3 -m ssx3map tint "SSX 3 (USA).iso" --texture 17 --rgb 1 0.6 0.8 -o SSX3_pink.iso
```

The export names files `tex_<ID>_<width>x<height>.png`. Textures are shared by the whole
mountain, so a change shows everywhere the texture is used. `--location ARA1` instead of
`--texture` recolours every texture the location uses. For palette textures the palette changes,
so every mip level changes too.

## Looking into the data

```
python3 -m ssx3map info  "SSX 3 (USA).iso"                 # locations and their chunks
python3 -m ssx3map list  "SSX 3 (USA).iso" --location ARA1 # records (kind, rid, size, name)
python3 -m ssx3map list  "SSX 3 (USA).iso" --kind 15       # painter records only (fog, sun…)
python3 -m ssx3map inspect "SSX 3 (USA).iso" > report.txt  # structural report (a few minutes)
```

What the reports showed is in [findings.md](findings.md).

## Location codes

| Code | Course | Code | Course |
|---|---|---|---|
| ARA1 | Snow Jam | ABC1 | Happiness |
| BRA2 | Metro-City | DBC2 | Ruthless |
| CRA3 | Ruthless Ridge | EBC3 | The Throne |
| DRA4 | Intimidator | ASS1 | R&B |
| ERA5 | Gravitude | DSS2 | Style Mile |
| ABA1 | Crow's Nest | ESS3 | Kick Doubt |
| CBA2 | Launch Time | BHP1 | The Junction |
| EBA3 | Much-2-Much | CHP2 | Schizophrenia |
| A–E | lodges | EHP3 | Perpendiculous |

Connectors are named after the locations they join (e.g. `A_ARA1`). The skies are `ASKY` to
`ESKY`.

## How saving works

The whole mountain is one stream, `bam.ssb`: blocks compressed with RefPack. The game finds
each piece through `bam.sdb` and other tables. Those tables are not fully understood yet, so
the tool never changes them:

- Edited blocks are encoded to **exactly their original byte count**. No block moves, and
  `BAM.BIG` and the ISO keep their size.
- First it tries **splicing**: the original RefPack commands before and after the change stay
  byte for byte and only a short stretch around the edit is re-encoded. If that is not enough the
  whole block is re-encoded, using the padding after the stream if the block has some.
- Every written block is decoded again and compared with the wanted data.

That is why only edits that **keep the record sizes** are possible: moving and reshaping what
exists, colours, values, palettes.
