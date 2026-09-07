# GTNH seed libraries

Worldgen report corpora for GT New Horizons seed searching, generated with the
[gtnh-determinism](https://github.com/OrderedSet86/gtnh-determinism) headless probe
on worlds running the determinism fix jar. Each report describes the spawn window of
one seed: biomes, water/clay, GT ores, complete chest inventories, village layouts,
and Witchery structure sites — everything needed to rank seeds for speedrun routing
without launching the game.

Corpora are **one folder per pack version** — reports do NOT transfer across pack
versions (mod updates change worldgen RNG consumption, structure templates, and loot
tables). Tarballs are stored via **git LFS** (`*.tar.gz`), so history stays small as
corpora get updated; a normal `git clone` fetches only the current versions (make
sure `git lfs` is installed, otherwise you get 3-line pointer files).

## Repository layout

```
reference/
  daily-707/                             # static loot-table lookups for the Loot reference tab
    nbt-items.csv                        #   entries with NBT, resolved + heart damage
    enchantments.csv                     #   enchantment id -> name for this pack
gtnh-2.7.4/
  README.md                              # provenance: pack, jar md5s, run mode, seeds
  seedlib-0.4-60seeds.tar.gz             # 60-seed corpus
gtnh-2.8.4/
  README.md
  seedlib-0.4-gtnh2.8.4-100seeds-r2.tar.gz  # 100-seed corpus (all loaded chunks)
  seedlib-0.4-gtnh2.8.4-500seeds.tar.gz     # 500-seed corpus, disjoint seeds
  seedlib-0.4-gtnh2.8.4-100seeds-b3.tar.gz  # 100-seed corpus (batch 3), disjoint seeds
  seedlib-0.5pre-gtnh2.8.4-99seeds-fmt2.tar.gz       # format-2 corpus, 0.5pre jar
  seedlib-0.5pre-gtnh2.8.4-300seeds-coke-funnel.tar.gz  # FUNNELED top-300 (not random!)
  prefilter-0.5pre-gtnh2.8.4-650k-coke-sweep.tar.gz  # stage-0 sweep data behind the funnel
worlds/
  -1636594104014467454/                  # one baked bundle per seed per pack version
    meta.json                            #   bounds, layer manifest, provenance, caveats
    palette.json                         #   biome id -> map colour + vanilla grass/foliage tints
    dim0/  dim7/                         #   *.png + loot.json are LFS; veins.json and
                                         #   pois.json are text, one record per line
routemap/                                # static Leaflet world map (see "Route map")
tools/
  build_world_bundle.py                  # bakes a worlds/<seed>/ bundle from analysis outputs
  worldrender.py                         #   reads Anvil region files -> true block-surface PNG
```

Each tarball extracts flat:

```
gtmats.json          # GT material id -> name (shared by all reports in the corpus)
seed-<seed>.json     # one report per seed, e.g. seed-5584831682639266804.json
```

## Report format

**Format versions.** Each report carries a top-level `"format": N`; reports
without the field are format 1. Corpora are never regenerated in place — a new
probe format means a new tarball alongside the old ones, and the browser reads
both (feature-detecting per seed, falling back where a field is missing, e.g.
surface detection degrades to the y ≥ 64 sea-level guess on format 1). History:

- **1** — water/clay totals, ores, chests, villages, witchery, `populated` flag.
- **2** — adds `sand`/`gravel` totals, `waterY`/`clayY`/`sandY`/`gravelY`
  per-height histograms (sparse `{y: count}`), `hardenedclay` +
  `stainedclay{meta: count}`, and `surf`: a 16×16 terrain heightmap per chunk
  (512 hex chars, row-major `z*16+x`, one byte per column) that ignores
  vegetation and floating slime islands — chest burial depth = `surf` at the
  chest column minus chest y.

Each `seed-<seed>.json` is one generated world, walked out to `radius` chunks
around spawn (currently 15, a 31x31-chunk window) — plus every chunk that worldgen
cascade-generated beyond the walk (nothing generated is discarded), typically ~1100
chunks total:

```jsonc
{
  "seed": 5584831682639266804,
  "order": "rows",            // chunk generation order used for the walk
  "radius": 15,               // window radius in chunks around spawn
  "search": {
    "spawn": [-164, 64, 121], // world spawn [x, y, z]
    "chunks": { ... }         // per-chunk data, keyed "chunkX,chunkZ" (see below)
  },
  "villages": [ ... ],        // one string per village (see below)
  "witchery": ["[-288, 320]"],// Witchery surface-structure sites: [x, z] block
                              // coords of the structure's chunk corner
  "popseq": [ ... ],          // diagnostic: chunk population sequence trace
  "chunks": {}, "spawnextra": {}  // block-hash fields, empty in these corpora
                                  // (generated with -Dprobe.nohash)
}
```

### Per-chunk data (`search.chunks`)

Keyed by chunk coordinates (`blockX >> 4`):

```jsonc
"-14,7": {
  "biome": "Magical Forest",
  "biomeId": 192,
  "water": 0,                 // surface water block count in this chunk
  "clay": 0,                  // clay block count
  "ores": {                   // GT ore m-value -> block count
    "870": 285, "16032": 5, "3086": 1, ...
  },
  "chests": [ ... ]           // present only if the chunk has loot chests
}
```

Chunks outside the walk window additionally carry `"populated": false` when their
own decoration pass had not run: they hold terrain plus whatever decoration spilled
over from populated neighbors (1.7.10 decorates at a +8,+8 offset), so their
ore/chest data is *partial*. Treat them as "at least this much", never as a
definitive zero. Fully-walked window chunks are complete; in rare cases a window
chunk also carries the flag — that is a real, deterministic 1.7.10 decoration hole
(cascade re-entrancy skipped that chunk's decoration pass), faithfully reported.

**Decoding ore m-values** (GT5u 1.7.10 metadata):

- `m % 1000` = GT material id — look the name up in `gtmats.json`
  (e.g. `"86"` → material 86 = look up `gtmats["86"]`)
- `m >= 16000` = small-ore variant; otherwise big ore (vein material)
- `m // 1000` (big ores) = host stone: 0 stone, 3 black granite, 4 red granite, …

So `"870": 285` = 285 big-ore blocks of material 870 in stone, and `"16870": 69` =
69 small ores of the same material.

### Chests

Every loot chest in the window, with full inventory:

```jsonc
{
  "pos": [-414, 28, 333],     // [x, y, z]
  "type": "TileEntityChest",
  "items": [
    { "s": 8,                       // slot
      "id": "gregtech:gt.metaitem.01",
      "d": 11300,                   // item damage/meta
      "n": 3,                       // stack count
      "name": "Bronze Ingot" }      // resolved display name
  ]
}
```

GT ingots are `gregtech:gt.metaitem.01` with damage `11000 + materialId` — e.g.
Steel (305) = damage 11305, Bronze (300) = 11300. Compare chests across runs on
`(id, d, n)` only; item NBT (TiC tools), flowing-water counts, and ore-TE histograms
are run-to-run noise.

### Villages

One string per village: piece count, then every structure piece with its bounding
box `Name@x1,y1,z1..x2,y2,z2` —

```
"65 pieces: ComponentSmeltery@-308,68,-254..-302,70,-246;
 ComponentToolWorkshop@-276,72,-234..-270,77,-228; Church@...; House1@...; ..."
```

Piece names identify the source mod: `ComponentToolWorkshop`/`ComponentSmeltery` =
Tinker's Construct houses, `ComponentVillageApothecary`/`ComponentVillageWitchHut` =
Witchery, `ComponentVillageBeeHouse` = Forestry, `ComponentWorkshop` = Railcraft
(not TiC!), `House1`/`Church`/`Field1`/… = vanilla.

## Browser

A Streamlit app in [`browser/`](browser/) reads the tarballs in place (no
extraction) and serves the corpora at `http://localhost:8501`:

```
browser/run.sh        # needs uv; or: pip install -r browser/requirements.txt
                      #              && (cd browser && streamlit run app.py)
                      # launch from browser/ so .streamlit/config.toml applies
```

- **Seed overview** — one row per seed (water, clay, chests, villages, TiC
  house, witchery, top biomes, plus per-item total columns); click a column
  header to sort.
- **Cluster query** — "at least N of thing A, B, C within Y blocks of spawn
  and within Z blocks of each other", over both chest loot (by display name)
  and GT ores (`Ore: <material>`, counted per chunk); chest y-range filter
  for surface/dungeon splits.
- **coke%** — category ranking for the coke-oven speedrun: each route
  criterion (village paper chest, water, clay, TiC shovel/axe heads, furnaces,
  Dezil's Marshmallows, chest fuel) gets a nearest-first *quota distance*;
  seeds are ranked by the summed distances, with per-seed breakdowns and tp
  commands. Sand is proxied by Desert/Beach biome chunks (the reports don't
  count sand/gravel blocks).
- **Loot score** — rank seeds by chest contents against an editable item value
  table: `score = Σ value × min(quantity, limit)` over every chest inside the
  window. The editor is the single copy of the metric; presets load from any
  `*/value-table*.csv` beside the corpora or in the sibling repo's `results/`,
  and a CSV can be uploaded. `Limit` caps how much of an item counts; **`Min`
  is the opposite** — a requirement rather than a score term, dropping any seed
  holding fewer than `Min` of that item from the ranking, checked against the
  raw quantity rather than the capped one. Failures are counted per requirement,
  so an empty ranking says which bar was too high instead of looking like a bad
  corpus. Below the ranking: the **top scoring chests**
  across the sample, ranked by *marginal* contribution — what a chest earns
  once every richer chest has already spent the per-item caps — with `/tp`; and
  **everything that fed the score**, one row per item with quantity found,
  quantity counted after the cap, value, contribution and share of the total.
  It defaults to `(top scorer)`, a sentinel that re-resolves each run and so
  follows the ranking as you edit the metric; selecting a specific seed pins it
  across reruns instead. Unvalued items are listed separately as
  candidates to add. Scoring mirrors
  `gtnh-determinism/seedsearch/loot-score.py`, the canonical CLI ranker — keep
  the two in sync, as with `PF_CRITERIA` and `coke-rank.py`.
- **Baseline rates** — how often each item actually appears: total, mean per
  seed, and the share of seeds containing it, over whatever is loaded. This is
  the input to rarity normalisation (`value / mean appearances per seed`),
  which makes each item's *expected* contribution equal its stated value and so
  stops bulk items like Redstone dominating. A smoothing slider `k` bounds the
  largest multiplier any item can earn at `seeds / k`; without it an item seen
  twice outranks everything and the score becomes a lottery on it. The
  normalised table can be downloaded or promoted straight into the Loot score
  editor. `k` defaults to 4% of the loaded seed count, because it is a count of
  pseudo-sightings and a value tuned for 5000 seeds swamps a 100-seed sample.
- **Loot reference** — static lookup, independent of every corpus: what the
  pack's loot *tables* contain, resolved from each entry's NBT. Enchantments by
  name, Tinkers' and GT tool materials and stats, and **weapon damage in
  hearts** — health points halved, matching the tooltip. Base damage is the
  item's `attackDamage` attribute (or `InfiTool.Attack` for Tinkers' tools);
  Sharpness adds 1.25 HP per level against everything, while Smite and Bane of
  Arthropods add 2.5 per level against undead and arthropods only, so those get
  their own columns rather than being folded into the headline number.
  **Armour** is ranked by **damage reduction**, not by armour points: 1.7.10
  applies `damage × (25 − armour)/25` and then `damage × (25 − i)/25`, so the
  two mitigations multiply and points alone gets the order wrong — 6 armour with
  Protection beats 8 armour without. `i` is not the raw EPF either; it is
  `ceil(EPF/2) + rand(0 … floor(EPF/2))` clamped to 20, so the enchantment half
  is random per hit and the column is its exact expectation. Both inputs are set
  totals in game, so scoring one piece as if it were the whole set is a stated
  convention: monotonic in both inputs, but the absolute percentage is only
  reached with nothing else worn. Forge `ISpecialArmor` items compute protection
  at damage time and would be understated; none of this pack's loot armour is
  one. Forestry
  genomes, knowledge notes, vis amulets and the odds-and-ends bucket are hidden
  as gear-irrelevant; the counts are stated and the rows stay in the CSV. Data
  lives in `reference/<pack>/`, generated by
  `gtnh-determinism/seedsearch/loot-nbt-report.py`.
- **Seed detail** — full biome/ore/chest/village/witchery breakdown of one
  seed, everything sorted by distance from spawn.

Sweeps are named
`prefilter-<determinism version>-<pack>-<seeds>-<what>-r<radius>.jsonl`, e.g.
`prefilter-0.5-d17a685-gtnhdaily707-5000-chest-loot-r60.jsonl`. The picker shows
only the filename, so the version, pack and radius have to be *in* it — two
sweeps of the same seed count against different packs are not comparable, and a
sweep run without the chest modules scores 0 for every seed. Do not also expose
the per-batch parts a combined sweep was concatenated from: they are the same
data, and offering both is a way to pick the slow one by accident.

The sidebar carries a **seed limit**, default 100, capping how many seeds every
tab looks at so that tinkering with a metric costs seconds rather than minutes.
It applies to corpus seeds and to how much of a stage-0 sweep is parsed — a
radius-60 sweep runs about 1 MB per seed, so reading all of a 5000-seed file to
look at 100 of them is exactly the wait it exists to avoid. Set it to 0 for no
limit. A limited run is a fair sample but it is a *sample*: best-of-100 is not
best-of-500, and the caption says which you are looking at.

## Route map

A static Leaflet map of one world, for routing a run rather than comparing
seeds. Where the browser answers "which seed", this answers "where, in this
seed". It is a plain page — no Streamlit, no server-side state, no network:
Leaflet is vendored into `routemap/vendor/`, and the baked bundles under
`worlds/` are committed, so a clone is all you need.

Three commands, and the only requirements are `git-lfs` and `python3`:

```
git lfs install            # once per machine, BEFORE cloning
git clone <repo> && cd gtnh-seedlib
routemap/run.sh            # then open http://localhost:8502/routemap/
```

`run.sh` serves the **repo root** (not `routemap/`), because the page fetches its
bundle from `/worlds/<seed>/`. `$PORT` overrides 8502. Any static server will do;
`fetch()` is blocked on `file://`, so opening the HTML directly will not work.

If you cloned before running `git lfs install`, the rasters arrive as pointer
stubs and the map is blank — `git lfs pull` fixes it without re-cloning.

**No uv, no pip, no npm, no packages of any kind.** Unlike the Streamlit browser
above, viewing a bundle installs nothing: `run.sh` is `python3 -m http.server`
from the standard library, Leaflet is vendored in `routemap/vendor/`, and the
page makes no external requests. Verified by serving it from a venv created with
`--without-pip` and no site-packages — it renders identically. Any Python 3 will
do, and any other static file server works just as well (`npx serve`, `caddy
file-server`, nginx) as long as it is rooted at the repo.

The only build-time dependencies are for *rebuilding* a bundle with
`tools/build_world_bundle.py`, which needs `numpy` and `Pillow`. A fresh user who
just wants the map never runs it.

Seed `-1636594104014467454` (daily-707) is bundled and opens by default. Other
worlds go in `worlds/<seed>/` and are selected with `?world=<seed>` — worldgen
changes between pack versions, so expect one bundle per seed per version. The
`.gitattributes` rules are glob-based over `worlds/**`, so a new bundle is
routed to LFS correctly without touching them.

### Publishing to GitHub Pages

`.github/workflows/pages.yml` publishes `routemap/` and `worlds/` on every push
to `main` that touches them. To turn it on: **Settings → Pages → Source →
GitHub Actions**, then push (or run the workflow manually from the Actions tab).
The site lands at `https://<user>.github.io/<repo>/`, which redirects to
`/routemap/`. Pages on a private repo needs a paid plan.

It deploys through an *artifact* rather than by publishing a branch, and that is
load-bearing: branch-published Pages sites are built from a `git archive`, which
contains LFS **pointer files** rather than their contents, so the base rasters
and `loot.json` would be served as ~130 bytes of text and the map would come up
blank. Uploading the checked-out tree ships real bytes. It also keeps visitor
traffic off Git LFS entirely, so the site cannot exhaust the LFS bandwidth quota.

The workflow checks out with `lfs: false` and then runs
`git lfs pull --include="worlds/**"`, because `lfs: true` would fetch every LFS
object in the repo — about 264 MB, nearly all of it seed-corpus tarballs the site
never serves — instead of the ~11 MB it publishes. It then hard-fails if the
rasters still look like pointer files, rather than publishing a blank map.

Layers, all toggleable, for both the Overworld and the Twilight Forest:

- **Base** — three renders, all at 1 px per block. *Blocks* is the default and
  the most faithful: the true surface material of every generated block, read
  out of the world save. *Biome* and *Topo* are cheaper derivations from the
  search report.
- **Ore veins** — one box per vein at its true bounding box, filtered by ore and
  by distance. 1256 in the Overworld, 1220 in the Twilight Forest. If a bundle
  contains veins that differed between a rows walk and a spiral walk of the same
  seed, they are hidden by default and drawn dashed when switched on, with the
  reason in the popup; the control only appears when there are such veins. The
  current bundle has none — route instability was a property of the generating
  jar and has been fixed upstream — but the mechanism stays, because it is a
  claim about a *bundle*, not about the map.
- **Loot** — by chest (sized and coloured by value) or by item, where picking
  an item lights up every chest containing it. Chest popups carry the
  exporter's own `/tp`, which is not the chest's raw coordinates: for an exact
  chest it targets the block above, and for a nominal or sky chest it targets
  y 200 to fly down from.
- **POIs** — villages with their piece boxes, roguelike dungeon *triggers*,
  enchanting tables, strongholds, witchery cells by winning handler, and the
  no-rain and humid biome squares. A roguelike dungeon does not exist until its
  trigger chunk populates, so the trigger is a prerequisite and the popup says
  so.
- **Climate** — the *actual* no-rain and high-humidity regions, per block, from
  the same biome grid as the Blocks layer. Worth turning on before trusting the
  dashed POI squares: those are the largest **axis-aligned squares** the stage-0
  prefilter could inscribe, which is much smaller than the region. On this world
  the no-rain square is 10×10 = 100 chunks while 4,408 chunks are fully no-rain,
  because the square is broken by a single non-qualifying column — and every
  no-rain base biome here paints its rivers with either Hot River, which is also
  no-rain, or River Oasis, which rains. One river through a desert halves the
  square without touching the region.

It opens filling the visible map area — covering it, so the shorter axis fits
exactly and the longer one runs off screen, rather than shrinking the world
until all of it fits and leaving a border. The available width excludes the
panel and the centre is offset to match, and the fit is redone on window resize
and on hiding the panel, but only until you move the view yourself. Two things
this depends on: zoom snapping is off (any snap rounds the opening fit *down* —
that alone cost a quarter of the window), and the renders are cropped to their
content, because the images are sized to whole 512-block region files while
generation only fills a patch inside that, and the transparent margin would
otherwise be fitted as if it were map.

Zoom 0 is one screen pixel per block; the range runs from −5 (the whole world in
a laptop window) to 8 (one block filling 256 px, enough to pick a single chest
out of a dungeon room), in quarter-level wheel steps. The corner readout gives
the current scale in px/block. Marker and line widths are in screen pixels, so
they are rescaled by 2^(0.4·zoom) as you move through that range — between
constant-on-screen and constant-in-world — otherwise a 5 px dot covers 80 blocks
at the far end and a third of a block at the near one.

Ore colours are named rather than hashed, and every vein outline is drawn over a
dark casing. Both matter: a hash bunched 9 of 28 ores into green and 5 into
cyan — gold drew green, coal drew cyan — and it had no idea what was underneath,
so lapis landed on a mid blue drawn faintly over rivers and lakes. No choice of
hue is legible against every background, which is what the casing is for.
Filtering down to three ores or fewer also thickens what is left, since at that
point there is no clutter to protect against.

The view is deep-linkable: `?dim=7`, `?base=topo`, `?ores=lapis,gold`,
`?item=Bronze+Ingot`, `?climate=1`, `?rings=1`, `?grid=512`, `?x=&z=&zoom=`,
`?world=<seed>`.

Bundles are baked, not read live, so the map does not depend on a multi-GB
cache that is safe to delete:

```
tools/build_world_bundle.py --seed <seed> --pack <pack> \
  --prefilter <sweep.jsonl> --loot-csv <loot.csv> \
  --veins-ow <veins-overworld.csv> --veins-tf <veins-tf.csv> \
  --ow-search <search-report.json> --tf-search <search-report.json> \
  --ow-region <World/region> --tf-region <World/DIM7/region> \
  --level-dat <World/level.dat> \
  --biomes <biomes.json> --jm-dir <journeymap/data/sp/WORLD> \
  --out worlds/<seed>
```

Every input is optional — pass what exists and the rest of the layers are
skipped.

**Getting the inputs.** `gtnh-determinism/scripts/run-probe.sh` with
`PROBE_SEARCH=true` (add `PROBE_DIM=7 PROBE_TFFEATURES=4` for the Twilight
Forest) writes the search report; about three minutes each at radius 60. The
same run also leaves a real, fully populated world in `<server>/World`, which
is where the Blocks layer comes from — copy `region/` and `level.dat` out
before the next run, because every run starts with `rm -rf World`. Since one
run only populates one dimension properly, the Overworld and Twilight Forest
saves have to be captured from separate runs.

Ore veins do *not* come from the search report — on GT 5.09.54.x worldgen ores
have no tile entities, so the report's ore census reads empty rather than zero,
and veins have to come from the `.veincache.json` sidecar via
`seedsearch/vein-csv.py`.

**How the Blocks layer works** (`tools/worldrender.py`). It reads the Anvil
region files directly: no client, no player, no mod. Chunk NBT is 1.7.10 plus
two GTNH extensions — `Biomes16v2`, 256 little-endian u16 per chunk because the
pack has biome ids past 255 (vanilla's 8-bit `Biomes` is absent), and
`Data1High`/`Data2` for extended block metadata. Block ids are resolved to
registry names through the FML table in `level.dat`, then coloured. Coverage is
99.1% (Overworld) and 99.2% (Twilight Forest) of visible surface blocks — the
build prints the figure and names the top misses, so extending `BLOCKS` is a
matter of reading that line. Vegetation, lily pads and Twilight Forest's wispy
clouds are in `SKIP` and are seen past, the way a map shows the ground under
grass; leaves are deliberately not, since a canopy is what makes a forest
legible.

Four things it has to model, each of which was wrong at first and each of
which is worth knowing before touching the palette:

- **Block metadata is part of the colour.** Over half the sand in this world is
  `minecraft:sand` *meta 1* — Et Futurum's backport of 1.8 red sand, sharing the
  block id with ordinary sand. Colouring by block id alone turns an orange-red
  desert into a beach. `BLOCK_META` carries the per-meta colours; the same
  applies to `leaves2` (acacia vs dark oak) and stained clay.
- **A biome-tinted block is texture × tint, not tint.** Grass and leaves are
  painted with the vanilla `grass.png` / `foliage.png` colormaps indexed by the
  biome's temperature and rainfall — *not* with the biome's cartographic map
  colour, which is a different thing. That tint then multiplies a greyscale
  texture: `grass_top` averages 147/255 and the leaf textures 135/255. Omitting
  that factor makes every forest and meadow about twice as bright. Pass
  `--mc-jar` to use the real colormaps; only the derived per-biome tints are
  written to the bundle, never the Mojang asset.
- **Water only counts above the ground.** A column's highest water block is
  often a flooded cave or dungeon room far below the surface — 180k columns
  here. Treating any water as open water draws the underground straight through
  the terrain, which is how flooded roguelike corridors first showed up as
  "buildings" on a surface map.
- **The hillshade sign.** Light comes from the north-west, the top-left of the
  screen. For a surface normal `(-dh/dx, -dh/dz, 1)` and a light vector
  `(-1, -1, 1)` the dot product is `1 + dh/dx + dh/dz`, so brightness rises
  *with* the gradient. Getting that sign backwards lights the scene from the
  south-east, and since people read shading as lit-from-above, every raised
  thing — trees, village roofs, slime islands — inverts into a pit. Regressing
  JourneyMap's own luminance over 91k single-colour red-sand pixels gives
  `+18.0*dh/dx +17.5*dh/dz` on a base of 94.8: near-equal coefficients, so NW at
  45°, and a relative gain of 0.19 against the 0.18 used here.

All four were found by measuring this renderer against real JourneyMap captures
of the same world, which after the fixes agrees to a mean absolute channel error
of 16/255 over 367k pixels — 80% of pixels within 40/255, 95% within 70. Those
captures were a *diagnostic* for finding structural errors, not a colour source:
nothing in the palette is copied from them. They are not shipped here, being
personal play data and only covering the few regions that were walked; the
Blocks layer renders the whole probe radius and supersedes them.

Two limits on the cheaper layers. Biome is sampled once per chunk, at the chunk
centre column, so the RWG river ribbons painted over base biomes are not
recoverable and biome colour is painted at 16-block granularity (height is
per-block). And there is no per-block water field in any report format — the
water mask is derived per chunk from the identity `waterY[y] == count(surf < y)`,
which holds for open water and fails for cave water, which is how the two are
told apart. The Blocks layer has neither limit: it reads both from the save.

## Querying

Quick start, no tooling — total surface steel in one seed:

```python
import json
mats = json.load(open("gtmats.json"))
r = json.load(open("seed-5584831682639266804.json"))
steel = sum(
    it["n"]
    for c in r["search"]["chunks"].values()
    for chest in c.get("chests", [])
    if chest["pos"][1] >= 64                      # surface = above sea level
    for it in chest["items"]
    if it["id"] == "gregtech:gt.metaitem.01" and it["d"] == 11305)
print(steel)  # 23
```

The [gtnh-determinism](https://github.com/OrderedSet86/gtnh-determinism) repo ships
ready-made tools (extract a tarball to a directory first):

```
scripts/searchlib.py summary <dir>       # per-seed one-liners + aggregate stats
scripts/searchlib.py filter <dir> 'r.water(6) > 2000 and r.village_count() > 0'
seedsearch/ingot-hunt.py totals <dir> --items steel --y-min 64   # rank seeds
seedsearch/ingot-hunt.py clusters <dir> --items steel,bronze     # best chest cluster
seedsearch/village-hunt.py <dir> --max-dist 200 --min-pieces 2   # TiC-house villages
```

Example — top surface-steel seeds in the 2.8.4 corpus:

```
$ seedsearch/ingot-hunt.py totals . --items steel --y-min 64 --top 3
  23     5584831682639266804  spawn [-164, 64, 121]  [steel 23]
  19     8113350524946093879  spawn [-7, 64, 17]  [steel 19]
  18     6228139464914664611  spawn [76, 64, 3]  [steel 18]
```

## Caveats

- Reports describe worlds generated **with the determinism fix jar** (exact md5 in
  each folder's README). Stock-jar worlds differ in village layout, Witchery
  structure types, Roguelike loot, and more — that nondeterminism is what the jar
  fixes.
- The window is finite (radius 15 ≈ 240 blocks); a "0" total means none *in the
  window*, not none in the world. Chunks flagged `"populated": false` have partial
  data (see Report format) — filter them out of anything that needs exact counts.
- Version-specific loot facts (e.g. 2.8.4 village chests holding GT ingots, the
  spawn-window dungeon-chest loot-table quirk) live in each version folder's README.
