# Reference data, GTNH daily-707

Static lookup tables for the browser's **Loot reference** tab. These describe the pack's loot
*tables*, not any world, so they are independent of every corpus here and do not change per seed.

Not under `gtnh-*/` on purpose: that glob is how `find_versions()` discovers seed corpora, and a
folder of CSVs is not one.

| file | what | produced by |
| --- | --- | --- |
| `nbt-items.csv` | every loot-table entry carrying NBT, resolved to a readable description, with heart damage for weapons | `gtnh-determinism/seedsearch/loot-nbt-report.py` |
| `enchantments.csv` | enchantment id → name, max level, weight, type | `ChestLootExport.writeEnchantments`, dumped from a running server |

Regenerate both by re-running the probe with `-Dprobe.lootcsv=<dir>` and then
`loot-nbt-report.py <dir> --materials <tic materials.csv> --csv nbt-items.csv`. See
`gtnh-determinism/results/2026-08-30-chest-loot-nbt/README.md`.
