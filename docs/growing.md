# Growing the world (experiments)

Every edit so far keeps the game data exactly the same size: the same number of terrain patches,
objects and path points, packed into the same 32 KB blocks. Bigger maps, more detail and new
models need the data to **grow**. Nothing stops that in principle; what is missing is knowing
which of the game's tables have to follow, and the only way to know is to try it in the game.

`probe` writes a set of test disc images, each one step further than the last, plus a report
of what the real data says about the fields it rewrites.

## Run it

```
python3 -m ssx3map probe
```

Without a path it finds the untouched game by itself (like the editor). The images go into a
folder `ssx3_probes` next to your disc image, with `probe_report.txt`. Each image is a full
copy of the game, so six of them need a lot of free space; make fewer with e.g.
`--only 1,2,3`. Reading the world and writing the images takes a few minutes.

## Try them in PCSX2

Start them **in order** and race **Single Event → Snow Jam** in each. For each one note:
does it start, does the course look normal, and from test 3 on: is the new ramp there (about
40 m after the start, on the race line), and can you ride up and jump off it?

| Test | What changed | What it tells |
|---|---|---|
| 1 `big_moved` | BAM.BIG moved to the end of the disc (contents identical; the old place is zeroed) | the game finds its files through the disc directory, so a bigger BAM.BIG can live at the end |
| 2 `one_more_block` | the first chunk one block longer (same contents); every other chunk shifted, offsets in `bam.sdb` rewritten | the game finds the chunks through `bam.sdb`, so a chunk can grow |
| 3 `new_ramp` | one new terrain patch on Snow Jam: a ramp up to 3 m high on the race line; counts and size in `bam.sdb` updated | new geometry is drawn and collides |
| 4 `patches_plus10` | the ramp and +10 % patches (tiny, hidden under the ground) | memory headroom |
| 5 `patches_plus50` | … +50 % | memory headroom |
| 6 `patches_plus100` | … +100 % (twice the patches) | memory headroom |

Stop at the first test that fails. Send back which tests worked and `probe_report.txt`.

## What each step rewrites

- **The disc:** `BAM.BIG` is written after the last sector of the image; its ISO 9660 directory
  record (sector and size, both byte orders) and the volume size in the primary volume
  descriptor follow. The PS2 reads ISO 9660; the UDF bridge entry is left stale.
- **`bam.ssb`:** a grown chunk keeps its blocks up to the first changed byte; the rest is packed
  again into 32 KB blocks (each at most 81 920 bytes decoded, like the largest retail block).
  Every later chunk moves by whole blocks.
- **`bam.sdb`, sub-chunk info of every chunk:** `+4` = the chunk's byte offset in `bam.ssb`.
  For the grown chunk: `+0` record count, `+12..` per-kind counts (kinds 0..12), `+8` size, by
  the rule that matches the original value (the report lists which rule matches how many chunks).
- **`bam.sdb`, location record:** the per-kind count of the added kind (shorts 0..22), when the
  original value matched the location's own-track or main-chunk count.
- **New patches** get the next free resource ids (record header and `+0x150`) and copy their
  texture, light and streaming fields from the patch they were made from, so they are loaded
  and drawn with it. They stay inside the course's existing terrain box, so the chunk boxes in
  `bam.sdb` need no change yet.

## If a test fails

- **1 fails:** the game does not look the file up on the disc; growing would have to use the
  free sectors after BAM.BIG (the report lists how many there are).
- **2 fails:** chunks are found another way; the report lists any other places in `bam.sdb`
  that hold chunk offsets.
- **3 fails or the ramp is missing / not solid:** a count or size the game uses was not updated,
  or new patches need more than the records (a collision or visibility table).
- **4–6 fail:** the memory limit lies between the last test that worked and the first that did
  not.
