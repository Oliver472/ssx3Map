# ssx3map – SSX 3 map editor (PS2)

Edit the mountain of SSX 3 (NTSC-U, `SLUS_207.72`) in your browser and play the result in
PCSX2. Your own disc image stays untouched; the editor saves an edited copy next to it.

> **Experimental.** Tested on synthetic data and partly in PCSX2. No game files are included,
> and none belong in this repository.

## What you need

1. **A Mac** (Linux and Windows work too, see below).
2. **Python 3.9 or newer and git.** On a Mac open Terminal and run `xcode-select --install`;
   it installs both. Nothing else to install.
3. **Your SSX 3 (USA) disc image** as an `.iso` file (unpack it first if it is a `.7z`).
   Keep it in Documents, Downloads or on the Desktop and the editor finds it by itself.
4. **A web browser and internet** (the 3D view loads three.js from the internet; your game
   data never leaves your computer).
5. **PCSX2** to play the edited game (optional, in Applications).

## Start

1. Get the project (once), in Terminal:

   ```
   cd ~/Documents
   git clone https://github.com/Oliver472/ssx3Map.git
   ```

2. Open the `ssx3Map` folder and **double-click `SSX3 editor.command`**.
   The first time macOS may refuse to open it: right-click it → **Open** → **Open**.
   It updates the project and opens the editor in your browser. (In Terminal the same is
   `cd ~/Documents/ssx3Map && python3 -m ssx3map`.)
3. In the browser, **click your game** (the untouched one is marked *original game*) and press
   **Open**. If it is not listed, type the path to the `.iso`.
4. Make your course:
   - **New course in one go → Plain slope with jumps**: set the grade, width, walls and jumps,
     then **Build**. It wipes Snow Jam and builds a straight slope of the same length.
   - or pick a recipe (e.g. *Snow Jam – Oliver's course*) and **Build**,
   - or edit by hand with the tabs below (see *Editor tools*).
5. Press **Save the edited game**. A new `…_edited.iso` appears next to your original.
6. Press **Start in PCSX2** (or open the new `.iso` in PCSX2 yourself) and race
   **Single Event → Snow Jam**.

To stop the editor, close its Terminal window (or press Ctrl+C in it).
**Open another game** at the top of the page brings back the game list.

**Linux / Windows:** run `python3 -m ssx3map` (Windows: `python -m ssx3map`) in the project
folder. On Windows tick "Add python.exe to PATH" when installing Python from python.org.

## Editor tools

| Tab | What it does |
|---|---|
| **View** | left button turns, right button pans, wheel zooms; the slider flies along the course |
| **Brush** | drag over the terrain to raise, lower, flatten or smooth it |
| **Shapes** | click to place a jump, hill/dip, plateau or flat area |
| **Move** | grab the terrain and drag a piece of the course sideways; objects, rails, AI paths, start and reset points move with it |
| **Objects** | click a tree or building, then move, turn, remove it or place it elsewhere (P) |

**Undo** (Ctrl+Z) takes back the last edit. **Highlight changes** shows edited terrain in
orange. Edits that would break the terrain or not fit into the game data are refused with a
message in the log.

## If something goes wrong

| Problem | Fix |
|---|---|
| `python3: command not found` or `git: command not found` | run `xcode-select --install` |
| "No ISO found" | type the full path to your `.iso` on the start page |
| "PCSX2 not found" | open the saved `…_edited.iso` in PCSX2 yourself |
| the 3D view stays empty | check the internet connection and reload the page |
| the course looks wrong in the game | load the original `.iso` again; it is never changed |

## Limits

- Everything is a rebuild of an existing course: the same number of terrain patches, objects
  and path points. A brand-new mountain or a new menu entry is not possible yet.
- Terrain shadows are baked in the game, so new hills have no shadows of their own.
- Sound triggers and stage scripts do not move when a piece of the course is moved.

## More

- [docs/command-line.md](docs/command-line.md) – every command (`flat`, `build`, `terrain`,
  `warp`, `objects`, `fog`, `tint`…), recipes, location codes and how saving works.
- [docs/findings.md](docs/findings.md) – what is known about the game's data formats.
- Tests (they build their own synthetic game data): `python3 -m unittest`.
  The browser test is `tests/browser/editor_smoke.mjs` (needs Node and `playwright`).

## Credits and license

The knowledge of the formats comes from
[SSX-Library](https://github.com/GlitcherOG/SSX-Library) by GlitcherOG (GPL-3.0), the notes in
[ssxdecomp/ssx3](https://github.com/ssxdecomp/ssx3) (`docs/notes`) and
[ssx-web](https://github.com/owattenmaker/ssx-web) (GPL-3.0). Texture swizzling, painter
records, the MDR model decoder (`ssx3map/models.py`, adapted from `tools/world_models.py` in
ssx-web) and the editor's game-look formulas follow their descriptions and code. This tool is
therefore licensed under **GPL-3.0** (see `LICENSE`).
