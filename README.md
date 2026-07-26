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
gtnh-2.7.4/
  README.md                              # provenance: pack, jar md5s, run mode, seeds
  seedlib-0.4-60seeds.tar.gz             # 60-seed corpus
gtnh-2.8.4/
  README.md
  seedlib-0.4-gtnh2.8.4-100seeds-r2.tar.gz  # 100-seed corpus (all loaded chunks)
  seedlib-0.4-gtnh2.8.4-500seeds.tar.gz     # 500-seed corpus, disjoint seeds
  seedlib-0.4-gtnh2.8.4-100seeds-b3.tar.gz  # 100-seed corpus (batch 3), disjoint seeds
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
- **Seed detail** — full biome/ore/chest/village/witchery breakdown of one
  seed, everything sorted by distance from spawn.

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
