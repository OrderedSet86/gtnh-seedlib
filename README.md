# GTNH seed libraries

Probe `search=true` report corpora for [gtnh-determinism](https://github.com/OrderedSet86/gtnh-determinism)
seed searching, **one folder per pack version** — seed reports do NOT transfer across
pack versions (mod updates change worldgen RNG consumption, structure templates, and
loot tables). Each folder's README records the pack version, jar md5s, run mode, and
seed provenance.

Tarballs are stored via **git LFS** (`*.tar.gz`) so corpus updates don't bloat the
history; `git clone` fetches only the current versions.

- `gtnh-2.7.4/` — 60-seed balance corpus, 0.4 jar, cold runs. Canonical for 2.7.4;
  doubles as the fixed arm of the 0.4 balance report.
- `gtnh-2.8.4/` — 100-seed corpus, 0.4 jar, CRIU-pool runs (certified cold-equivalent).

Query with the gtnh-determinism repo's `scripts/searchlib.py` (generic),
`seedsearch/ingot-hunt.py` (chest-ingot rankings), or `seedsearch/village-hunt.py`
(village piece filters). Extract a tarball to a temp dir first. Seed lists live in
that repo's `seedlib/` (they are harness inputs).
