# Seed library: GTNH 2.8.4 — 700 seeds across three corpora

Probe `search=true` reports (radius 15, nohash, **all loaded chunks**: the 31x31
walk window plus every cascade-generated chunk, ~1100-1200 chunks/seed), generated
2026-07-24. Seed lists live in the gtnh-determinism repo's `seedlib/`; the two
corpora are disjoint — the browser and the query tools can use them together
(700 seeds total).

- `seedlib-0.4-gtnh2.8.4-100seeds-r2.tar.gz` — first batch, 100 seeds
  (`gtnh-2.8.4-seeds-100.txt`)
- `seedlib-0.4-gtnh2.8.4-500seeds.tar.gz` — second batch, 500 seeds
  (`gtnh-2.8.4-seeds-500.txt`); 500/500 clean, spot-check re-run byte-identical
- `seedlib-0.4-gtnh2.8.4-100seeds-b3.tar.gz` — third batch, 100 seeds
  (`gtnh-2.8.4-seeds-100b.txt`); 100/100 clean, spot-check re-run byte-identical

- pack: GT_New_Horizons_2.8.4_Server_Java_17-25.zip
- fix jar: gtnhdeterminism 0.4 (md5 044d86ca21f8596775be3250d0579add)
- probe jar: worldgenprobe v0.4-main.11+6056faa (md5 6bbb4899985277a9a3a24ed8898cc8d6)
- run mode: CRIU pool restores — certified cold-equivalent; every seed verified a
  strict superset of the r1 window-only corpus on stable fields.

**r2 supersedes r1** (same seeds, same worldgen): r1 reported only the fixed 31x31
window; r2 keeps every generated chunk — corpus-wide that recovered 18,494 extra
chunks and 1,512 additional chests. Chunks outside the walk window may carry
`"populated": false` = partial data (see the repo README's Report format section).

Routing notes for 2.8.4 (differ from 2.7.4 — reports do NOT transfer across pack
versions):
- Village chests DO contain GT ingots here (brass is village-only loot); bronze is
  ~10x richer than in 2.7.4.
- In every real 2.8.4 world, spawn-region dungeon chests roll the smaller
  pre-ServerStarting loot table (fewer GT ingots, no stainless/aluminium entries);
  chests generated outside the spawn preload use the full table.
- Run-noise between any two runs: TiC tool NBT, flowing-water counts, ore-TE
  histograms, and clay counts in swamp-type biomes. Compare chests on
  (id, damage, count) only.
- Known fix-jar 0.4 residual: rarely, one deep Roguelike-dungeon chest's EXISTENCE
  is launch-dependent (write race; in this corpus: one chest on seed
  7066592863814697627 at (101, 63, 196)). Treat deep-dungeon single chests as
  probable, not promised.
