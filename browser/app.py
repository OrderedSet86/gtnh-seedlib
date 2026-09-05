#!/usr/bin/env python3
"""Streamlit browser for gtnh-seedlib corpora.

Reads the per-version tarballs in this repo directly (no manual extraction) and
provides:
  - a sortable per-seed overview table (click column headers to sort),
  - a cluster query tab: ">= N of thing A, B, C within Y blocks of spawn and
    within Z blocks of each other" over chest loot and GT ores,
  - a coke% category tab ranking seeds by summed quota distances over the
    criteria of the coke-oven speedrun route,
  - a per-seed detail view (biomes, veins, chests, villages, witchery).

Launch: browser/run.sh  (or: uv run --with-requirements browser/requirements.txt \
        streamlit run browser/app.py)
"""
import json
import math
import pickle
import re
import tarfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import lootscore

try:  # ~6x faster tarball parse; stdlib json is a fine fallback
    import orjson
    _loads = orjson.loads
except ImportError:
    _loads = json.loads

# Parsed-corpus disk cache: canonical data stays in the LFS tarballs; the parsed
# form is pickled here keyed by (tarball name, mtime), so only the first load of
# a new corpus pays the JSON parse. Safe to delete anytime.
CACHE_DIR = Path.home() / ".cache" / "gtnh-seedlib"

REPO = Path(__file__).resolve().parent.parent
PIECE_RE = re.compile(r'(\w+)@(-?\d+),(-?\d+),(-?\d+)\.\.(-?\d+),(-?\d+),(-?\d+)')
COORD_RE = re.compile(r'-?\d+')
# GT m-value encoding (GT5u 1.7.10): m % 1000 = material id; >=16000 = small ore.
TIC_PIECES = {"ComponentToolWorkshop", "ComponentSmeltery"}


# ---------------------------------------------------------------- data loading

def find_versions():
    """{version folder: [tarball paths]} — one entry per pack version; a version
    may hold several corpus runs (100 seeds, 500 seeds, …) that get merged."""
    out = {}
    for tar in sorted(REPO.glob("gtnh-*/*.tar.gz")):
        out.setdefault(tar.parent.name, []).append(str(tar))
    return out


def load_version(tar_paths):
    """Merge every corpus run of ONE pack version (never merge across versions —
    reports don't transfer). Per-tarball parses stay cached; on duplicate seeds
    the newest file (mtime) wins, so a superseding rerun replaces its precursor."""
    mats, by_seed = {}, {}
    for p in sorted(tar_paths, key=lambda p: Path(p).stat().st_mtime):
        m, seeds = load_corpus(p, Path(p).stat().st_mtime)
        mats = m or mats
        for s in seeds:
            by_seed[s["seed"]] = s
    return mats, list(by_seed.values())


def _parse_villages(villages):
    out = []
    for v in villages:
        pieces = PIECE_RE.findall(str(v))
        if not pieces:
            continue
        names = [p[0] for p in pieces]
        boxes = [[int(g) for g in p[1:]] for p in pieces]
        cx = sum((b[0] + b[3]) / 2 for b in boxes) / len(boxes)
        cz = sum((b[2] + b[5]) / 2 for b in boxes) / len(boxes)
        out.append({"pieces": len(pieces), "cx": cx, "cz": cz,
                    "names": Counter(names),
                    "parts": [(n, (b[0] + b[3]) / 2, (b[2] + b[5]) / 2)
                              for n, b in zip(names, boxes)]})
    return out


def _parse_witchery(witchery):
    out = []
    for w in witchery:
        nums = [int(n) for n in COORD_RE.findall(str(w))]
        if len(nums) >= 2:
            out.append((nums[0], nums[1]))
    return out


@st.cache_data(show_spinner="Loading corpus…")
def load_corpus(tar_path, mtime):
    """Parse a seedlib tarball into (mats, [seed records]). mtime busts both the
    in-session cache and the on-disk pickle of the parsed form."""
    disk = CACHE_DIR / f"{Path(tar_path).name}-{int(mtime)}.pkl"
    if disk.exists():
        with open(disk, "rb") as f:
            return pickle.load(f)
    mats, seeds = {}, []
    with tarfile.open(tar_path, "r:gz") as tf:
        members = {m.name: m for m in tf.getmembers() if m.isfile()}
        for name, m in members.items():
            if Path(name).name == "gtmats.json":
                mats = json.load(tf.extractfile(m))
                break
        for name, m in sorted(members.items()):
            base = Path(name).name
            if not (base.startswith("seed-") and base.endswith(".json")):
                continue
            d = _loads(tf.extractfile(m).read())
            fmt = d.get("format", 1)  # pre-versioning corpora carry no field
            search = d.get("search", {})
            chests = []
            surf_by_chunk = {}
            # Chunk data is columnar (numpy) — per-chunk dicts made the parsed
            # pickle reconstruct millions of objects and dominated load time.
            cols = {k: [] for k in ("cx", "cz", "water", "clay", "sand",
                                    "gravel", "hclay", "populated", "code")}
            biome_names = []
            biome_idx = {}
            surf_list = []
            ores = []  # (chunk idx, m-value, count) triplets -> parallel arrays
            for key, c in search.get("chunks", {}).items():
                cx, cz = map(int, key.split(","))
                pop = c.get("populated", True)
                surf = bytes.fromhex(c["surf"]) if "surf" in c else None
                if surf is not None:
                    surf_by_chunk[(cx, cz)] = surf
                b = c.get("biome", "?")
                if b not in biome_idx:
                    biome_idx[b] = len(biome_names)
                    biome_names.append(b)
                idx = len(cols["cx"])
                cols["cx"].append(cx)
                cols["cz"].append(cz)
                cols["water"].append(c.get("water", 0))
                cols["clay"].append(c.get("clay", 0))
                cols["sand"].append(c.get("sand", 0))
                cols["gravel"].append(c.get("gravel", 0))
                cols["hclay"].append(c.get("hardenedclay", 0)
                                     + sum(c.get("stainedclay", {}).values()))
                cols["populated"].append(pop)
                cols["code"].append(biome_idx[b])
                surf_list.append(surf)
                for k, v in c.get("ores", {}).items():
                    ores.append((idx, int(k), v))
                for chest in c.get("chests", []):
                    items = [(it.get("name") or f'{it["id"]}:{it["d"]}', it["n"])
                             for it in chest.get("items", [])]
                    chests.append({"pos": chest["pos"], "type": chest.get("type", "?"),
                                   "populated": pop, "items": items})
            chunks = {
                "n": len(cols["cx"]),
                "cx": np.array(cols["cx"], dtype=np.int32),
                "cz": np.array(cols["cz"], dtype=np.int32),
                "water": np.array(cols["water"], dtype=np.int32),
                "clay": np.array(cols["clay"], dtype=np.int32),
                "sand": np.array(cols["sand"], dtype=np.int32),
                "gravel": np.array(cols["gravel"], dtype=np.int32),
                "hclay": np.array(cols["hclay"], dtype=np.int32),
                "populated": np.array(cols["populated"], dtype=bool),
                "code": np.array(cols["code"], dtype=np.uint16),
                "biome_names": biome_names,
                # one blob, 256 bytes/chunk (zeros = no heightmap for that chunk)
                "surf": (b"".join(s or bytes(256) for s in surf_list)
                         if any(s is not None for s in surf_list) else b""),
                "ores_idx": np.array([t[0] for t in ores], dtype=np.int32),
                "ores_m": np.array([t[1] for t in ores], dtype=np.int32),
                "ores_n": np.array([t[2] for t in ores], dtype=np.int32),
            }
            # burial depth = local terrain height minus chest y (format >= 2 only)
            for chest in chests:
                x, y, z = chest["pos"]
                surf = surf_by_chunk.get((x >> 4, z >> 4))
                chest["depth"] = surf[(z & 15) * 16 + (x & 15)] - y if surf else None
            seeds.append({
                "seed": d["seed"],
                "format": fmt,
                "spawn": search.get("spawn", [0, 0, 0]),
                "chunks": chunks,
                "chests": chests,
                "villages": _parse_villages(d.get("villages", [])),
                "witchery": _parse_witchery(d.get("witchery", [])),
            })
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in CACHE_DIR.glob(f"{Path(tar_path).name}-*.pkl"):
        stale.unlink()
    tmp = disk.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump((mats, seeds), f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.rename(disk)
    return mats, seeds


def ore_thing(m, mats):
    """m-value -> display name used in the query UI."""
    mat = mats.get(str(m % 1000), f"mat{m % 1000}")
    return f"Ore (small): {mat}" if m >= 16000 else f"Ore: {mat}"


@st.cache_data
def all_things(tar_key):
    """Every queryable 'thing': chest item names + ore materials. tar_key is a
    tuple of (path, mtime) pairs — the mtimes bust the cache on corpus updates."""
    mats, seeds = load_version([p for p, _ in tar_key])
    things = set()
    for s in seeds:
        for chest in s["chests"]:
            things.update(name for name, _ in chest["items"])
        things.update(ore_thing(int(m), mats)
                      for m in np.unique(s["chunks"]["ores_m"]))
    return sorted(things)


def dist2d(ax, az, bx, bz):
    return math.hypot(ax - bx, az - bz)


def has_tic_house(village):
    return bool(TIC_PIECES & village["names"].keys())


def tp(x, y, z):
    """1.7.10 teleport command. y=None keeps current height (~); chest callers
    pass y+1 so you land on top of the block, not inside it."""
    return f"/tp {round(x)} {'~' if y is None else round(y)} {round(z)}"


def chest_is_surface(chest, max_depth=2, y_min=64):
    """Format >= 2 reports carry real burial depth (terrain height - chest y,
    slime-island aware); older corpora fall back to the y >= 64 sea-level guess,
    which miscalls buried chests under hills — regenerate for exact answers."""
    if chest.get("depth") is not None:
        return chest["depth"] <= max_depth
    return chest["pos"][1] >= y_min


# ---------------------------------------------------------------- query engine

def seed_sites(s, mats, wanted, y_min, y_max):
    """[(x, z, y_or_None, kind, Counter)] of sites holding any wanted thing.

    Chests contribute at their exact position (y-filtered); ore chunks contribute
    at the chunk center (no y — GT veins span many layers).
    """
    sites = []
    for chest in s["chests"]:
        x, y, z = chest["pos"]
        if not (y_min <= y <= y_max):
            continue
        counts = Counter()
        for name, n in chest["items"]:
            if name in wanted:
                counts[name] += n
        if counts:
            sites.append((x, z, y, "chest", counts))
    ch = s["chunks"]
    by_chunk = {}
    # ~18k ore rows per seed but only a handful of distinct m-values: resolve
    # names once per m, and keep only the wanted ones (the whole script reruns
    # on every widget change, so this loop is on the interactive path).
    keep = {}
    for m in np.unique(ch["ores_m"]):
        t = ore_thing(int(m), mats)
        if t in wanted:
            keep[int(m)] = t
    for idx, m, n in zip(ch["ores_idx"], ch["ores_m"], ch["ores_n"]):
        t = keep.get(int(m))
        if t is not None:
            by_chunk.setdefault(int(idx), Counter())[t] += int(n)
    for idx, counts in by_chunk.items():
        sites.append((int(ch["cx"][idx]) * 16 + 8, int(ch["cz"][idx]) * 16 + 8,
                      None, "ore chunk", counts))
    return sites


def best_cluster(sites, reqs, spawn, max_spawn_dist, cluster_radius):
    """Anchor-ball search: for each site, gather sites within cluster_radius of it
    and test the summed counts against reqs. Returns the best qualifying cluster
    (smallest span, then most total), or None.
    """
    sx, sz = spawn[0], spawn[2]
    near = [t for t in sites if dist2d(t[0], t[1], sx, sz) <= max_spawn_dist]
    best = None
    for anchor in near:
        mem = [t for t in near
               if dist2d(t[0], t[1], anchor[0], anchor[1]) <= cluster_radius]
        tot = sum((t[4] for t in mem), Counter())
        if not all(tot.get(thing, 0) >= n for thing, n in reqs.items()):
            continue
        span = max((dist2d(a[0], a[1], b[0], b[1]) for a in mem for b in mem),
                   default=0.0)
        total = sum(tot[t] for t in reqs)
        key = (span, -total)
        if best is None or key < best[0]:
            best = (key, mem, tot, span)
    if best is None:
        return None
    _, mem, tot, span = best
    cx = sum(t[0] for t in mem) / len(mem)
    cz = sum(t[1] for t in mem) / len(mem)
    return {"members": mem, "totals": tot, "span": span,
            "spawn_dist": dist2d(cx, cz, sx, sz)}


# -------------------------------------------------------------- category engine

# What the reports can and can't see for coke%: water/clay are per-chunk BLOCK
# counts; sand/gravel blocks are NOT recorded (sand is proxied by Desert/Beach
# biome chunks, gravel not at all — but chest Flint is counted, and flint is
# what the GTNH furnace recipe needs). Furnaces are TileEntityFurnace TEs.
MARSH_ITEM = "Dezil's Marshmallow"
FURNACE_TYPES = {"TileEntityFurnace"}
SMITHY_PIECE = "House2"          # vanilla blacksmith
# Smelt value in coal units (coal smelts 8 items). Any "... Planks" item also
# counts via PLANK_FUEL.
FUEL_VALUES = {"Coal": 1.0, "Charcoal": 1.0, "Coal Coke": 2.0, "Block of Coal": 9.0}
PLANK_FUEL = 0.1875
# TiC head materials considered run-viable by default — editable in the UI.
# Corpus fact (2.8.4): metal heads (Bronze/Iron/Obsidian/Electrum/Pig Iron/
# Queen's Gold) appear ONLY in dungeon loot below y 50; surface chests hold
# Flint/Bone/Stone (and Cactus/Wooden at y 50-63). With the surface filter on,
# Flint is the best head a route can pick up.
GOOD_HEAD_DEFAULT = ["Flint", "Cactus", "Bone",
                     "Bronze", "Obsidian", "Electrum", "Queen's Gold",
                     "Iron", "Pig Iron"]


def is_sandy(biome):
    return "Desert" in biome or "Beach" in biome


def head_material(item_name, kind):
    """'Bronze Shovel Head' -> 'Bronze' (kind = 'Shovel' | 'Axe'), else None."""
    suffix = f" {kind} Head"
    return item_name[: -len(suffix)] if item_name.endswith(suffix) else None


def all_head_materials(seeds):
    mats = set()
    for s in seeds:
        for c in s["chests"]:
            for name, _ in c["items"]:
                for kind in ("Shovel", "Axe"):
                    m = head_material(name, kind)
                    if m:
                        mats.add(m)
    return sorted(mats)


def quota_dist(sites, quota):
    """sites = [(dist, qty, x, y_or_None, z, label)]. Take nearest-first until
    qty sums to quota; return (dist of last site needed, used sites) or None."""
    if quota <= 0:
        return (0.0, [])
    got = 0
    used = []
    for site in sorted(sites, key=lambda t: t[0]):
        got += site[1]
        used.append(site)
        if got >= quota:
            return (site[0], used)
    return None


def coke_criteria(s, params):
    """Per-criterion (quota, qty within radius, quota_dist result) for one seed.

    Returns {name: {"quota", "qty", "hit": (dist, used)|None}} — 'qty' is the
    total available inside the radius, 'hit' the nearest-first quota solution.
    """
    sx, _, sz = s["spawn"]
    R = params["radius"]

    def d(x, z):
        return dist2d(x, z, sx, sz)

    chest_sites = {"paper": [], "shovel": [], "axe": [], "marsh": [], "fuel": []}
    furnaces = []
    for c in s["chests"]:
        x, y, z = c["pos"]
        dist = d(x, z)
        if dist > R:
            continue
        if params["surface_only"] and not chest_is_surface(c, params["max_depth"]):
            continue
        if c["type"] in FURNACE_TYPES:
            furnaces.append((dist, 1, x, y, z, "furnace"))
            continue
        paper = shovel = axe = 0
        marsh = 0
        fuel = 0.0
        for name, n in c["items"]:
            if name == "Paper":
                paper += n
            elif name == MARSH_ITEM:
                marsh += n
            elif name in FUEL_VALUES:
                fuel += FUEL_VALUES[name] * n
            elif name.endswith("Planks"):
                fuel += PLANK_FUEL * n
            sm = head_material(name, "Shovel")
            am = head_material(name, "Axe")
            if sm in params["good_mats"]:
                shovel += n
            if am in params["good_mats"]:
                axe += n
        # paper quota must be met by ONE chest (a village house with >=4 paper)
        if paper >= params["paper_per_chest"]:
            chest_sites["paper"].append((dist, paper, x, y, z, f"{paper} paper"))
        if shovel:
            chest_sites["shovel"].append((dist, shovel, x, y, z,
                                          f"{shovel} good shovel head(s)"))
        if axe:
            chest_sites["axe"].append((dist, axe, x, y, z,
                                       f"{axe} good axe head(s)"))
        if marsh:
            chest_sites["marsh"].append((dist, marsh, x, y, z,
                                         f"{marsh} marshmallow(s)"))
        if fuel:
            chest_sites["fuel"].append((dist, fuel, x, y, z,
                                        f"{fuel:g} coal-equiv fuel"))

    ch = s["chunks"]
    xs = ch["cx"].astype(np.float64) * 16 + 8
    zs = ch["cz"].astype(np.float64) * 16 + 8
    dists = np.hypot(xs - sx, zs - sz)
    near = dists <= R
    sandy_code = np.array([is_sandy(b) for b in ch["biome_names"]], dtype=bool)
    sandy = int(np.count_nonzero(near & sandy_code[ch["code"]]))

    def chunk_sites(counts_arr, label):
        out = []
        for i in np.nonzero(near & (counts_arr > 0))[0]:
            n = int(counts_arr[i])
            out.append((float(dists[i]), n, int(xs[i]), None, int(zs[i]),
                        f"{n} {label}"))
        return out

    water_sites = chunk_sites(ch["water"], "water")
    clay_sites = chunk_sites(ch["clay"], "clay")

    smithies = sum(1 for v in s["villages"] for name, px, pz in v["parts"]
                   if name == SMITHY_PIECE and d(px, pz) <= R)

    crit = {}

    def add(name, sites, quota):
        crit[name] = {"quota": quota, "qty": sum(t[1] for t in sites),
                      "hit": quota_dist(sites, quota)}

    # paper: quota = 1 chest that individually holds >= paper_per_chest
    crit["paper"] = {"quota": params["paper_per_chest"],
                     "qty": sum(t[1] for t in chest_sites["paper"]),
                     "hit": quota_dist([(t[0], 1, *t[2:])
                                        for t in chest_sites["paper"]], 1)}
    add("water", water_sites, params["min_water"])
    add("clay", clay_sites, params["min_clay"])
    add("shovel", chest_sites["shovel"], params["min_shovel"])
    add("axe", chest_sites["axe"], params["min_axe"])
    add("furnaces", furnaces, params["min_furnaces"])
    add("marsh", chest_sites["marsh"], params["min_marsh"])
    add("fuel", chest_sites["fuel"], params["min_fuel"])
    crit["_sandy_chunks"] = sandy
    crit["_smithies"] = smithies
    return crit


CRIT_LABELS = {"paper": "paper", "water": "water", "clay": "clay",
               "shovel": "shovel heads", "axe": "axe heads",
               "furnaces": "furnaces", "marsh": "marshmallows", "fuel": "fuel"}


def coke_rows(seeds, params):
    """One summary row per seed + breakdown data, sorted best-first."""
    out = []
    for s in seeds:
        crit = coke_criteria(s, params)
        unmet = [CRIT_LABELS[k] for k in CRIT_LABELS if crit[k]["hit"] is None]
        score = sum(crit[k]["hit"][0] for k in CRIT_LABELS if crit[k]["hit"])
        row = {"seed": str(s["seed"]),
               "spawn x": s["spawn"][0], "spawn z": s["spawn"][2],
               "unmet": ", ".join(unmet) if unmet else "—",
               "score": round(score)}
        for k in CRIT_LABELS:
            c = crit[k]
            row[CRIT_LABELS[k]] = round(c["qty"], 1) if isinstance(c["qty"], float) else c["qty"]
            row[f"{CRIT_LABELS[k]} d"] = round(c["hit"][0]) if c["hit"] else None
        row["sandy chunks"] = crit["_sandy_chunks"]
        row["smithies"] = crit["_smithies"]
        out.append((len(unmet), score, row, crit, s))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def _filter_chunks(ch, mask=None):
    """Columnar chunk table filtered to `mask` (default: populated chunks)."""
    if mask is None:
        mask = ch["populated"]
    keep = np.nonzero(mask)[0]
    out = {k: v for k, v in ch.items()}
    for k in ("cx", "cz", "water", "clay", "sand", "gravel", "hclay",
              "populated", "code"):
        out[k] = ch[k][mask]
    out["n"] = len(keep)
    if ch["surf"]:
        out["surf"] = b"".join(ch["surf"][i * 256:(i + 1) * 256] for i in keep)
    ore_keep = mask[ch["ores_idx"]]
    new_idx = np.cumsum(mask) - 1  # old chunk idx -> new position
    out["ores_idx"] = new_idx[ch["ores_idx"][ore_keep]].astype(np.int32)
    out["ores_m"] = ch["ores_m"][ore_keep]
    out["ores_n"] = ch["ores_n"][ore_keep]
    return out


def biome_counts(ch):
    counts = np.bincount(ch["code"], minlength=len(ch["biome_names"]))
    return Counter({b: int(n) for b, n in zip(ch["biome_names"], counts) if n})


# ---------------------------------------------------------------- prefilter sweeps

# Stage-0 prefilter JSONL sweeps (gtnh-determinism scripts/prefilter.sh). Published
# sweeps ship in THIS repo as gtnh-*/prefilter-*.tar.gz (git LFS) and are read
# straight from the tarball; the sibling repo's results/ is only scanned as a
# convenience for local, not-yet-published sweeps, plus a free-path box.
PREFILTER_RESULTS = REPO.parent / "gtnh-determinism" / "results"
PREFILTER_CAP = 512.0
# Chest-bearing sweeps are large (a radius-60 run is ~1 MB/seed) and are staged outside the
# Dropbox-synced tree, so the loot tabs scan the working cache too. Missing dirs are skipped.
SWEEP_ROOTS = [PREFILTER_RESULTS, Path.home() / ".cache" / "gtnh-determinism"]
# The Prefilter tab's ranker parses an entire sweep — it has to, it ranks every survivor. Anything
# past this never appears in its picker, because selecting one is an unbounded wait with a spinner
# that looks identical to normal work. The loot tabs read a bounded number of lines and use
# find_chest_sweeps() instead, which has no such limit.
PREFILTER_MAX_BYTES = 512 * 1024 * 1024
# scoring mirrors gtnh-determinism/seedsearch/coke-rank.py (the canonical CLI ranker);
# keep the two in sync when criteria change
PF_CRITERIA = {
    "paper": ("VillageComponentPhotoshop",),
    "tic": ("ComponentToolWorkshop", "ComponentSmeltery"),
    "furnace": ("House2",),
}


@st.cache_data
def _tar_jsonl_members(tar_str, mtime):
    """.jsonl member names inside a prefilter tarball (mtime busts the cache).
    Listing a .tar.gz decompresses the whole stream — hence the cache."""
    with tarfile.open(tar_str, "r:gz") as tf:
        return [m.name for m in tf.getmembers()
                if m.isfile() and m.name.endswith(".jsonl")]


def find_prefilter_sweeps():
    """{label: source}. Source is either "<tarball>::<member>" for published
    sweeps in this repo (the normal case) or a plain path for local, not-yet-
    published sweeps in the sibling gtnh-determinism results/ (dev machines)."""
    out = {}
    for t in sorted(REPO.glob("gtnh-*/prefilter-*.tar.gz")):
        try:
            for name in _tar_jsonl_members(str(t), t.stat().st_mtime):
                out[f"{t.parent.name}/{t.name} :: {name}"] = f"{t}::{name}"
        except tarfile.ReadError:
            pass  # LFS pointer file — surfaced by the corpus loader already
    if PREFILTER_RESULTS.is_dir():
        for p in sorted(PREFILTER_RESULTS.glob("*/*.jsonl")):
            if p.stat().st_size <= PREFILTER_MAX_BYTES:
                out[f"local: {p.parent.name}/{p.name}"] = str(p)
    return out


def find_chest_sweeps():
    """Sweeps for the loot tabs, which read a bounded number of lines and so can afford the big
    chest-bearing runs the Prefilter tab must not touch.

    Kept separate from find_prefilter_sweeps deliberately. That tab's ranker parses a whole sweep to
    rank every survivor, so listing a multi-GB file there is a guaranteed hang; here the seed limit
    caps the read. Files are deduplicated by content size+line-count-free heuristic: a combined
    sweep and the per-batch parts it was concatenated from are the same data, and offering both is
    just a way to pick the slow one by accident.
    """
    out = {}
    for root in SWEEP_ROOTS:
        if not root.is_dir():
            continue
        for p in sorted(root.glob("*/*.jsonl")):
            out[f"{p.parent.name}/{p.name}"] = str(p)
    return out


def find_value_tables():
    """{label: path} for item-value CSVs shipped beside the corpora, plus any in the sibling
    repo's results/ on a dev machine. These seed the editor; the edited copy is what scores."""
    out = {}
    for root in [REPO] + SWEEP_ROOTS:
        if not root.is_dir():
            continue
        for p in sorted(root.glob("*/value-table*.csv")):
            out[f"{p.parent.name}/{p.name}"] = str(p)
    return out


def _sweep_mtime(src):
    return Path(str(src).split("::", 1)[0]).stat().st_mtime


def _sweep_exists(src):
    return Path(str(src).split("::", 1)[0]).is_file() if src else False


@st.cache_data(show_spinner="Parsing prefilter sweep…")
def load_prefilter(src, mtime):
    """src = plain JSONL path, or "<tarball>::<member>" for in-repo sweeps."""
    tar_path, _, member = str(src).partition("::")
    path = Path(tar_path)
    cache = CACHE_DIR / (f"prefilter-{path.parent.name}-{path.name}-"
                         f"{Path(member).name or 'file'}-{int(mtime)}.pkl")
    if cache.exists():
        with open(cache, "rb") as f:
            return pickle.load(f)

    def parse(f):
        kills, survivors = Counter(), []
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = _loads(line)
            if "kill" in d:
                kills[d["kill"]] += 1
                continue
            survivors.append(d)
        return kills, survivors

    if member:
        with tarfile.open(tar_path, "r:gz") as tf:
            kills, survivors = parse(tf.extractfile(member))
    else:
        with open(path, "rb") as f:
            kills, survivors = parse(f)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump((kills, survivors), f, protocol=pickle.HIGHEST_PROTOCOL)
    return kills, survivors


def prefilter_row(d, max_village_dist, furnace_bonus, water_cols, sand_cols, clay_cols):
    """Best single village + terrain distances for one survivor (coke-rank port)."""
    spawn = d.get("spawn")
    if not spawn:
        return None
    px, pz = spawn[0], spawn[2]
    best = None
    for stv in d.get("village_starts", []):
        vd = {crit: PREFILTER_CAP for crit in PF_CRITERIA}
        edge, houses = None, 0
        for m in PIECE_RE.finditer(stv.get("pieces", "")):
            x1, _, z1, x2, _, z2 = (int(g) for g in m.groups()[1:])
            dx = max(min(x1, x2) - px, 0, px - max(x1, x2))
            dz = max(min(z1, z2) - pz, 0, pz - max(z1, z2))
            dist = math.hypot(dx, dz)
            edge = dist if edge is None else min(edge, dist)
            if m.group(1) == "House2":
                houses += 1
            for crit, names in PF_CRITERIA.items():
                if m.group(1) in names:
                    vd[crit] = min(vd[crit], dist)
        if edge is None or edge > max_village_dist:
            continue
        vscore = sum(vd.values()) - furnace_bonus * max(0, houses - 1)
        cx, cz = stv.get("c", [0, 0])
        if best is None or vscore < best[0]:
            best = (vscore, (cx * 16 + 2, cz * 16 + 2), vd, houses)
    if best is None:
        return None
    vscore, well, vd, houses = best
    t = {"water": PREFILTER_CAP, "sand": PREFILTER_CAP, "clay": PREFILTER_CAP}
    for row in d.get("terrain", []):
        center = math.hypot(row[0] * 16 + 8 - px, row[1] * 16 + 8 - pz)
        if row[2] >= water_cols:
            t["water"] = min(t["water"], center)
        if len(row) >= 7 and row[5] >= sand_cols:
            t["sand"] = min(t["sand"], center)
        if len(row) >= 9 and row[7] >= clay_cols:
            t["clay"] = min(t["clay"], center)
    return {
        "score": round(vscore + t["water"] + t["sand"] + t["clay"]),
        "seed": d["seed"],
        "spawn": f"{px},{pz}",
        "village": f"{well[0]},{well[1]}",
        "paper": round(vd["paper"]), "tic": round(vd["tic"]),
        "furnace": round(vd["furnace"]), "furn_houses": houses,
        "water": round(t["water"]), "sand": round(t["sand"]), "clay": round(t["clay"]),
        "tp spawn": tp(px, None, pz), "tp village": tp(well[0], None, well[1]),
    }


@st.cache_data(show_spinner="Ranking survivors…", max_entries=8)
def prefilter_rank(src, mtime, max_vd, fbonus, wcols, scols, ccols, require_all):
    """Score every survivor. Cached: a sweep has tens of thousands of them, and
    Streamlit reruns the whole script (all tabs) on any widget change — without
    this, picking a seed in the detail tab pays for the prefilter tab."""
    _, survivors = load_prefilter(src, mtime)
    rows = []
    for d in survivors:
        r = prefilter_row(d, max_vd, fbonus, wcols, scols, ccols)
        if r is None:
            continue
        if require_all and any(r[k] >= PREFILTER_CAP for k in ("paper", "tic", "furnace")):
            continue
        rows.append(r)
    rows.sort(key=lambda r: r["score"])
    return pd.DataFrame(rows)


def render_prefilter():
    sweeps = find_prefilter_sweeps()
    st.caption("Stage-0 worldless prefilter sweeps (gtnh-determinism "
               "`scripts/prefilter.sh`). Layout/terrain predictions only — chest "
               "loot, marshmallows and heads need the stage-1 reports.")
    col_a, col_b = st.columns([2, 2])
    with col_a:
        choice = st.selectbox("Sweep", list(sweeps) or ["(none found)"])
    with col_b:
        custom = st.text_input("…or JSONL path", "")
    src = str(Path(custom).expanduser()) if custom else sweeps.get(choice)
    if not _sweep_exists(src):
        st.info("No sweep selected. Expected prefilter-*.tar.gz in this repo's "
                f"version folders (run `git lfs pull`), *.jsonl under "
                f"{PREFILTER_RESULTS}, or an explicit path above.")
        return
    kills, survivors = load_prefilter(src, _sweep_mtime(src))

    total = sum(kills.values()) + len(survivors)
    cols = st.columns(len(kills) + 2)
    cols[0].metric("Seeds", f"{total:,}")
    for i, (k, v) in enumerate(sorted(kills.items()), start=1):
        cols[i].metric(f"killed: {k}", f"{v:,}")
    cols[-1].metric("Survivors", f"{len(survivors):,}")

    with st.expander("Scoring parameters (mirrors seedsearch/coke-rank.py)"):
        c1, c2, c3, c4, c5 = st.columns(5)
        max_vd = c1.slider("Max village dist", 25, 512, 100,
                           help="village eligible only if its nearest piece is "
                                "within this many blocks of spawn")
        fbonus = c2.slider("Furnace-house bonus", 0, 100, 25)
        wcols = c3.slider("Water cols/chunk", 1, 64, 16)
        scols = c4.slider("Deep-sand cols/chunk", 1, 32, 4)
        ccols = c5.slider("Clay-cand cols/chunk", 1, 64, 8)
        require_all = st.checkbox("Require paper+tic+furnace in the village", True)

    df = prefilter_rank(src, _sweep_mtime(src), max_vd, fbonus, wcols, scols,
                        ccols, require_all)
    st.write(f"**{len(df)}** seeds pass the current village rules")
    if len(df):
        st.dataframe(df, width="stretch", height=560, hide_index=True)
        st.download_button("Download CSV", df.to_csv(index=False),
                           file_name=f"{Path(src).parent.name}-ranked.csv")


# ------------------------------------------------------------------------ UI

def render_sidebar(versions):
    with st.sidebar:
        st.title("gtnh-seedlib")
        label = st.selectbox("Pack version", list(versions),
                             index=len(versions) - 1)
        tars = versions[label]
        try:
            mats, seeds = load_version(tars)
        except tarfile.ReadError:
            st.error("Not a valid tarball — likely a git-LFS pointer file. "
                     "Run `git lfs pull` in the repo.")
            st.stop()
        fmts = Counter(s.get("format", 1) for s in seeds)
        fmt_txt = ", ".join(f"format {f}: {n}" for f, n in sorted(fmts.items()))
        st.caption(f"{len(seeds)} seeds merged from {len(tars)} corpus "
                   f"file{'s' if len(tars) != 1 else ''} ({fmt_txt}) · window "
                   "radius 15 chunks (~240 blocks) around spawn, plus the "
                   "generated fringe beyond it · distances are horizontal (x, z)")
        # format-1 seeds lack surf/sand/gravel/per-y data, so every analysis on
        # them silently degrades to heuristics — keep them out unless asked.
        n_fmt2 = sum(n for f, n in fmts.items() if f >= 2)
        if min(fmts) < 2:
            include_fmt1 = st.checkbox(
                f"Include format 1 seeds ({fmts.get(1, 0)})",
                value=(n_fmt2 == 0),
                help="Format 1 corpora predate the terrain heightmap, "
                     "sand/gravel, and per-height data: surface detection falls "
                     "back to the y ≥ 64 sea-level guess and sand columns are "
                     "biome proxies. Off by default so results only reflect "
                     "full-fidelity data.")
            if n_fmt2 == 0 and not include_fmt1:
                st.info("This version has no format 2 corpora yet — nothing to "
                        "show with format 1 excluded.")
            if not include_fmt1:
                seeds = [s for s in seeds if s.get("format", 1) >= 2]
            else:
                st.caption("⚠ format 1 seeds included — surface detection uses "
                           "the y ≥ 64 sea-level guess for them.")
        complete_only = st.checkbox("Exclude partially-generated chunks", value=False)
        st.caption("Fringe chunks marked `populated: false` never ran their own "
                   "decoration pass — they only hold ores/chests spilled over from "
                   "finished neighbors. Their data is real but incomplete: fine "
                   "when hunting for things, misleading when the *absence* of "
                   "something matters (e.g. \"no copper near spawn\"). Tick this "
                   "to count complete chunks only.")
        if complete_only:
            seeds = [{**s, "chunks": _filter_chunks(s["chunks"]),
                      "chests": [c for c in s["chests"] if c["populated"]]}
                     for s in seeds]
        st.divider()
        n_all = len(seeds)
        limit = st.number_input(
            "Seed limit", min_value=0, value=100, step=50,
            help="Cap how many seeds every tab looks at, so tinkering with a metric costs seconds "
                 "instead of minutes. 0 = no limit. Seeds are taken in corpus order, which is "
                 "arbitrary but stable, so a limited run is a fair sample — but it is a SAMPLE: "
                 "'best of 100' is not 'best of 500'. Raise it before trusting a ranking.")
        if limit and limit < n_all:
            seeds = seeds[:limit]
            st.caption(f"⚠ using {len(seeds)} of {n_all} seeds — rankings are best-of-"
                       f"{len(seeds)}, not best-of-{n_all}.")
        else:
            st.caption(f"using all {n_all} seeds")
    tar_key = tuple(sorted((p, Path(p).stat().st_mtime) for p in tars))
    return tar_key, mats, seeds, int(limit)


def render_overview(seeds, mats, things):
    st.caption("Click a column header to sort. Add per-thing total columns below "
               "(chest items count items in the whole window; ores count blocks).")
    default_cols = [t for t in ("Steel Ingot", "Bronze Ingot") if t in things]
    thing_cols = st.multiselect("Total columns", things, default=default_cols)
    surface_only = st.checkbox(
        "Surface chests only for thing columns", value=False,
        help="Real burial depth on format ≥ 2 corpora; y ≥ 64 fallback on "
             "older ones (Roguelike dungeon loot is mostly deep).")

    rows = []
    for s in seeds:
        ch = s["chunks"]
        biomes = biome_counts(ch)
        row = {
            "seed": str(s["seed"]),
            "spawn x": s["spawn"][0], "spawn z": s["spawn"][2],
            "water": int(ch["water"].sum()),
            "clay": int(ch["clay"].sum()),
            "chests": len(s["chests"]),
            "villages": len(s["villages"]),
            "TiC house": any(has_tic_house(v) for v in s["villages"]),
            "witchery": len(s["witchery"]),
            "top biomes": ", ".join(b for b, _ in biomes.most_common(3)),
        }
        for t in thing_cols:
            total = 0
            if t.startswith("Ore"):
                ms = [int(m) for m in np.unique(ch["ores_m"])
                      if ore_thing(int(m), mats) == t]
                if ms:
                    total = int(ch["ores_n"][np.isin(ch["ores_m"], ms)].sum())
            else:
                for chest in s["chests"]:
                    if surface_only and not chest_is_surface(chest):
                        continue
                    total += sum(n for name, n in chest["items"] if name == t)
            row[t] = total
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), width="stretch", height=600, hide_index=True)


def render_query(seeds, mats, things):
    st.caption("Find seeds with at least N of each thing, where all contributing "
               "sites sit within the spawn radius and within the cluster radius "
               "of one anchor site (max pairwise spread shown as *span*). "
               "Chest items match by display name; 'Ore:' entries count ore "
               "blocks per chunk at the chunk center.")

    default_reqs = pd.DataFrame(
        [{"thing": t, "min count": 10} for t in ("Steel Ingot",) if t in things]
        or [{"thing": things[0], "min count": 1}])
    reqs_df = st.data_editor(
        default_reqs, num_rows="dynamic", hide_index=True, width="stretch",
        column_config={
            "thing": st.column_config.SelectboxColumn(
                "thing", options=things, required=True, width="large"),
            "min count": st.column_config.NumberColumn(
                "min count", min_value=1, step=1, required=True),
        },
        key="reqs")

    c1, c2, c3, c4 = st.columns(4)
    max_spawn_dist = c1.number_input("Within … blocks of spawn", 1, 2000, 240,
                                     help="Window edge is ~240 blocks out.")
    cluster_radius = c2.number_input("Within … blocks of each other", 1, 2000, 100,
                                     help="Radius around an anchor site; check "
                                          "the span column for actual spread.")
    y_min = c3.number_input("Chest y ≥", 0, 255, 0)
    y_max = c4.number_input("Chest y ≤", 0, 255, 255)

    reqs = {}
    for _, r in reqs_df.iterrows():
        if pd.notna(r["thing"]) and pd.notna(r["min count"]):
            reqs[r["thing"]] = reqs.get(r["thing"], 0) + int(r["min count"])

    if not reqs:
        st.info("Add at least one requirement row.")
        return

    results = []
    for s in seeds:
        sites = seed_sites(s, mats, set(reqs), y_min, y_max)
        hit = best_cluster(sites, reqs, s["spawn"], max_spawn_dist, cluster_radius)
        if hit:
            results.append((s, hit))
    results.sort(key=lambda t: (t[1]["span"], t[1]["spawn_dist"]))

    st.markdown(f"**{len(results)}** / {len(seeds)} seeds match")
    if not results:
        return
    table = []
    for s, hit in results:
        row = {"seed": str(s["seed"]),
               "spawn x": s["spawn"][0], "spawn z": s["spawn"][2],
               "sites": len(hit["members"]),
               "span": round(hit["span"]),
               "dist from spawn": round(hit["spawn_dist"])}
        for t in reqs:
            row[t] = hit["totals"].get(t, 0)
        table.append(row)
    st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True)

    with st.expander("Cluster members per seed"):
        for s, hit in results:
            st.markdown(f"**{s['seed']}** — spawn {s['spawn'][0]}, {s['spawn'][2]}")
            mem_rows = [{
                "kind": kind, "x": x, "z": z,
                "y": y if y is not None else "—",
                "tp": tp(x, y + 1 if y is not None else None, z),
                "contents": ", ".join(f"{n}× {t}" for t, n in counts.items()),
            } for x, z, y, kind, counts in hit["members"]]
            st.dataframe(pd.DataFrame(mem_rows), width="stretch", hide_index=True)


def render_coke(seeds):
    st.caption(
        "Rank seeds for coke% (complete a coke oven). Each criterion gets a "
        "**quota distance**: its sites are taken nearest-to-spawn-first until the "
        "quota is met, and the cost is the distance of the last site needed. "
        "**score = sum of quota distances** — lower is a tighter run. Seeds "
        "missing a criterion sort below, with the misses named. Qty columns show "
        "the total available inside the radius. Caveats: the probe records "
        "water/clay *blocks* but not sand/gravel blocks — *sandy chunks* counts "
        "Desert/Beach biome chunks as a sand proxy, and gravel isn't measurable "
        "at all. *furnaces* are real TileEntityFurnace tile entities; *smithies* "
        "counts House2 blacksmith pieces. Chest criteria only count SURFACE "
        "chests by default (real burial depth on format ≥ 2 corpora, y ≥ 64 "
        "fallback on older ones). Note: metal heads (Bronze/Iron/Obsidian/…) "
        "only exist in dungeon loot below y 50; surface chests hold "
        "Flint/Bone/Stone heads, so Flint is the best surface pickup.")

    head_mats = all_head_materials(seeds)
    good_mats = st.multiselect(
        "Head materials that count as good (shovel/axe)", head_mats,
        default=[m for m in GOOD_HEAD_DEFAULT if m in head_mats])

    r1 = st.columns(5)
    r2 = st.columns(5)
    params = {
        "radius": r1[0].number_input("Radius", 50, 2000, 240, step=10),
        "paper_per_chest": r1[1].number_input("Paper (one chest)", 1, 64, 4),
        "min_water": r1[2].number_input("Water blocks", 0, 10000, 300, step=50),
        "min_clay": r1[3].number_input("Clay blocks", 0, 1000, 30, step=5,
                                       help="104 unfired bricks need ~105 clay "
                                            "balls ≈ 27 clay blocks."),
        "min_furnaces": r1[4].number_input(
            "Furnaces", 0, 50, 1,
            help="Pre-built SURFACE furnaces are rare (only 7/700 seeds have "
                 "4+): the run usually crafts them from cobble + flint. Sort "
                 "by the furnaces column to find village-furnace seeds."),
        "min_marsh": r2[0].number_input("Marshmallows", 0, 256, 16),
        "min_fuel": r2[1].number_input("Fuel (coal-equiv)", 0, 500, 32, step=8,
                                       help="Coal=1, Coke=2, Coal Block=9, "
                                            "planks=0.1875 each."),
        "min_shovel": r2[2].number_input("Shovel heads", 0, 20, 1),
        "min_axe": r2[3].number_input("Axe heads", 0, 20, 1),
        "max_depth": r2[4].number_input(
            "Max burial depth", 0, 64, 2,
            help="Blocks of terrain above the chest. Needs format ≥ 2 corpora "
                 "(real slime-island-aware heightmap); format-1 seeds fall back "
                 "to the y ≥ 64 sea-level guess, which miscalls chests buried "
                 "under hills."),
        "surface_only": st.checkbox(
            "Surface chests only", value=True,
            help="Dungeon detours are too slow for coke%. Applies to every "
                 "chest-based criterion (paper, heads, marshmallows, fuel, "
                 "furnaces)."),
        "good_mats": set(good_mats),
    }

    ranked = coke_rows(seeds, params)
    n_ok = sum(1 for u, _, _, _, _ in ranked if u == 0)
    st.markdown(f"**{n_ok}** / {len(ranked)} seeds meet every quota")

    st.dataframe(pd.DataFrame([r for _, _, r, _, _ in ranked]),
                 width="stretch", height=500, hide_index=True)

    st.subheader("Seed breakdown")
    options = [r["seed"] for _, _, r, _, _ in ranked]
    pick = st.selectbox("Seed", options, key="coke_seed")
    _, _, row, crit, s = next(t for t in ranked if t[2]["seed"] == pick)
    sx, sy, sz = s["spawn"]
    st.markdown(f"Spawn **{sx}, {sy}, {sz}** · `/tp {sx} {sy} {sz}` · "
                f"score **{row['score']}** · unmet: {row['unmet']}")
    bd = []
    for k, label in CRIT_LABELS.items():
        cinfo = crit[k]
        if cinfo["hit"]:
            dist, used = cinfo["hit"]
            for site in used:
                sdist, qty, x, y, z, what = site
                bd.append({"criterion": label, "quota": cinfo["quota"],
                           "in radius": round(cinfo["qty"], 1),
                           "site": what, "dist": round(sdist),
                           "tp": tp(x, y + 1 if y is not None else None, z)})
        else:
            bd.append({"criterion": label, "quota": cinfo["quota"],
                       "in radius": round(cinfo["qty"], 1),
                       "site": "NOT MET", "dist": None, "tp": ""})
    st.dataframe(pd.DataFrame(bd), width="stretch", hide_index=True, height=450)


def render_detail(seeds, mats):
    seed_pick = st.selectbox("Seed", [str(s["seed"]) for s in seeds])
    s = next(x for x in seeds if str(x["seed"]) == seed_pick)
    sx, sy, sz = s["spawn"]
    st.markdown(f"Spawn **{sx}, {sy}, {sz}** · `/tp {sx} {sy} {sz}`")
    st.caption("tp columns are 1.7.10 syntax (needs cheats/op) — click a cell and "
               "Ctrl+C to copy. Chest tps target one block above the chest; "
               "`~` keeps your current height where no y is known.")

    ch = s["chunks"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Water", int(ch["water"].sum()))
    m2.metric("Clay", int(ch["clay"].sum()))
    m3.metric("Chests", len(s["chests"]))
    m4.metric("Villages", len(s["villages"]))
    m5.metric("Witchery sites", len(s["witchery"]))

    col_l, col_r = st.columns(2)
    with col_l:
        st.subheader("Biomes")
        biomes = biome_counts(ch)
        st.dataframe(pd.DataFrame(
            [{"biome": b, "chunks": n} for b, n in biomes.most_common()]),
            width="stretch", hide_index=True)

        st.subheader("Ores (window total)")
        big, small = Counter(), Counter()
        for m, n in zip(ch["ores_m"], ch["ores_n"]):
            m, n = int(m), int(n)
            mat = mats.get(str(m % 1000), f"mat{m % 1000}")
            (small if m >= 16000 else big)[mat] += n
        ore_rows = [{"material": mat, "big-ore blocks": big.get(mat, 0),
                     "small ores": small.get(mat, 0)}
                    for mat in sorted(set(big) | set(small),
                                      key=lambda k: -(big.get(k, 0)))]
        st.dataframe(pd.DataFrame(ore_rows), width="stretch", hide_index=True,
                     height=400)

    with col_r:
        st.subheader("Villages")
        if s["villages"]:
            st.dataframe(pd.DataFrame([{
                "pieces": v["pieces"],
                "dist from spawn": round(dist2d(v["cx"], v["cz"], sx, sz)),
                "tp": tp(v["cx"], None, v["cz"]),
                "TiC house": has_tic_house(v),
                "notable": ", ".join(sorted(n for n in v["names"]
                                            if n.startswith("Component"))) or "—",
            } for v in s["villages"]]), width="stretch", hide_index=True)
        else:
            st.caption("none in window")

        st.subheader("Witchery sites")
        if s["witchery"]:
            st.dataframe(pd.DataFrame([{
                "x": x, "z": z, "dist from spawn": round(dist2d(x, z, sx, sz)),
                "tp": tp(x, None, z),
            } for x, z in s["witchery"]]), width="stretch", hide_index=True)
        else:
            st.caption("none in window")

    st.subheader("Chests")
    has_depth = any(c.get("depth") is not None for c in s["chests"])
    chest_rows = [{
        "x": c["pos"][0], "y": c["pos"][1], "z": c["pos"][2],
        **({"buried": c["depth"]} if has_depth else {}),
        "dist from spawn": round(dist2d(c["pos"][0], c["pos"][2], sx, sz)),
        "tp": tp(c["pos"][0], c["pos"][1] + 1, c["pos"][2]),
        "type": c["type"], "stacks": len(c["items"]),
        "contents": ", ".join(f"{n}× {t}" for t, n in c["items"]),
    } for c in sorted(s["chests"],
                      key=lambda c: dist2d(c["pos"][0], c["pos"][2], sx, sz))]
    st.dataframe(pd.DataFrame(chest_rows), width="stretch", hide_index=True,
                 height=500)


def disable_bare_hotkeys():
    """Streamlit binds bare 'c' (clear cache) and 'r' (rerun) whenever focus is
    outside an input — 'c' collides with copying cells out of the tables.
    toolbarMode="viewer" in .streamlit/config.toml removes them the supported
    way; this capture-phase guard is the belt-and-suspenders for dev mode.
    Modified keys (Ctrl+C) and keys typed into widgets pass through untouched.
    """
    components.html(
        """
        <script>
        const doc = window.parent.document;
        if (!doc.__seedlibHotkeyGuard) {
            doc.__seedlibHotkeyGuard = true;
            doc.addEventListener("keydown", (e) => {
                if (e.key !== "c" && e.key !== "r") return;
                if (e.ctrlKey || e.metaKey || e.altKey) return;
                const t = e.target;
                if (t && t.closest && t.closest(
                        "input, textarea, [contenteditable='true'], " +
                        "[data-testid='stDataFrame'], [role='grid']")) return;
                e.stopImmediatePropagation();
            }, true);
        }
        </script>
        """,
        height=0)


# ------------------------------------------------------- loot scoring (item value table)

@st.cache_data(show_spinner="Reading sweep…", max_entries=4)
def load_sweep_chests(src, mtime, limit):
    """Stage-0 sweep -> [(seed, spawn, [(source, category, pos, items, y_nominal)])].

    Tuples rather than objects because this is pickled by the cache. Records with no `spawn` are
    dropped and counted: Prefilter only computes the spawn point when the terrain digest is on
    (PREFILTER_TERRAIN >= 0), and without it every window would be centred on the origin instead —
    a wrong answer rather than a missing one.
    """
    records, kills = lootscore.read_jsonl_records(src, limit)
    out, no_spawn = [], 0
    for d in records:
        if not d.get("spawn"):
            no_spawn += 1
            continue
        out.append((d["seed"], d["spawn"],
                    [(c.source, c.category, c.pos, c.items, c.y_nominal)
                     for c in lootscore.chests_from_prefilter(d)]))
    return out, dict(kills), no_spawn


def _rehydrate(rows):
    return [lootscore.Chest(*r) for r in rows]


TOP_SCORER = "(top scorer)"

DEFAULT_TABLE = pd.DataFrame(
    [{"Item": "Alumite Large Plate", "Value": 10000.0, "Limit": 2, "Min": None},
     {"Item": "Steel Ingot", "Value": 100.0, "Limit": None, "Min": None},
     {"Item": "Bronze Ingot", "Value": 30.0, "Limit": None, "Min": None}])


def tall_table(df, height):
    """A dataframe that actually uses the fullscreen canvas.

    st.dataframe with a pixel height keeps that height when expanded, so fullscreening a 420 px
    table just puts 11 rows on a large empty page. A fixed-height container gives normal flow its
    size, and height="stretch" lets the table fill whatever it is given — including the fullscreen
    viewport.
    """
    with st.container(height=height):
        st.dataframe(df, width="stretch", height="stretch", hide_index=True)


def set_value_base(df, origin):
    """Replace the BASE table — the imported CSV, not the edited view.

    Bumps a version counter that is part of the editor's widget key. st.data_editor keeps its own
    per-key state, so handing it a different frame under an unchanged key is ignored and a preset
    load appears to do nothing; a new key is what makes the swap stick, and it also discards the
    stale row-indexed edit deltas that would otherwise land on the wrong rows of the new table.
    """
    df = df.copy()
    for col in ("Value", "Limit", "Min"):
        if col not in df:
            df[col] = None
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce")
    df = df.sort_values("Value", ascending=False, na_position="last")
    df = df[["Item", "Value", "Limit", "Min"]]
    st.session_state["vt_base"] = df.reset_index(drop=True)
    st.session_state["vt_origin"] = origin
    st.session_state["_vt_ver"] = st.session_state.get("_vt_ver", 0) + 1


def _vt_key(df):
    """{normalised item: (value, limit)} for diffing base against the edited view."""
    out = {}
    for r in df.to_dict("records"):
        item = str(r.get("Item") or "").strip()
        if not item:
            continue
        out[lootscore.norm(item)] = (lootscore.clean(item), r.get("Value"),
                                     r.get("Limit"), r.get("Min"))
    return out


def _same(a, b):
    if a is None or b is None or (isinstance(a, float) and a != a) or (isinstance(b, float) and b != b):
        return (a is None or (isinstance(a, float) and a != a)) and \
               (b is None or (isinstance(b, float) and b != b))
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def value_table_diff(base, current):
    """Hand edits since import, as rows. Kept as a derived view rather than tracked incrementally so
    it cannot drift from what is actually being scored."""
    b, c = _vt_key(base), _vt_key(current)
    rows = []
    for k in c.keys() - b.keys():
        rows.append({"change": "added", "item": c[k][0], "field": "", "from": "", "to": ""})
    for k in b.keys() - c.keys():
        rows.append({"change": "removed", "item": b[k][0], "field": "",
                     "from": f"value {b[k][1]}", "to": ""})
    for k in b.keys() & c.keys():
        for i, field in ((1, "Value"), (2, "Limit"), (3, "Min")):
            if not _same(b[k][i], c[k][i]):
                rows.append({"change": "edited", "item": c[k][0], "field": field,
                             "from": b[k][i], "to": c[k][i]})
    rows.sort(key=lambda r: (r["change"], str(r["item"])))
    return rows


def value_table_editor():
    """The single editable copy of the scoring metric.

    Two frames, deliberately: `vt_base` is what was imported and is never written back to, and the
    editor's return value is the live view. Feeding the return value back into the editor's own
    input — the obvious thing — makes Streamlit re-apply its row-indexed deltas on top of already
    edited data, which reverts cells and duplicates added rows. `vt_current` is published for the
    Baseline tab, which runs later in the same script pass and so sees this run's edits, not the
    previous run's.
    """
    tables = find_value_tables()
    col_a, col_b = st.columns([3, 2])
    with col_a:
        preset = st.selectbox("Load a table", ["(keep current)"] + list(tables), index=0,
                              help="Loading replaces the table and discards hand edits.")
    with col_b:
        upload = st.file_uploader("…or upload a CSV", type="csv",
                                  help="Columns Item, Value, Limit. Extra columns are ignored.")

    def from_rows(rows, origin):
        set_value_base(pd.DataFrame(
            [{"Item": r.get("Item"), "Value": r.get("Value"), "Limit": r.get("Limit"),
               "Min": r.get("Min")}
             for r in rows if (r.get("Item") or "").strip()]), origin)

    if upload is not None and st.session_state.get("_vt_upload") != upload.name:
        from_rows(lootscore.value_rows_from_csv(upload.getvalue().decode("utf-8", "replace")),
                  f"upload: {upload.name}")
        st.session_state["_vt_upload"] = upload.name
    elif preset != "(keep current)" and st.session_state.get("_vt_preset") != preset:
        from_rows(lootscore.value_rows_from_csv(Path(tables[preset]).read_text(encoding="utf-8")),
                  preset)
        st.session_state["_vt_preset"] = preset
    if "vt_base" not in st.session_state:
        set_value_base(DEFAULT_TABLE.copy(), "built-in starter table")

    base = st.session_state["vt_base"]
    height = st.select_slider("Editor height", [280, 420, 560, 760, 1000], value=560,
                              help="Rows are sorted by value at import. The editor cannot re-sort "
                                   "as you type — use the button below to fold edits in and re-sort.")
    edited = st.data_editor(
        base, key=f"vt_editor_{st.session_state.get('_vt_ver', 0)}", num_rows="dynamic",
        width="stretch", height=height,
        column_config={
            "Item": st.column_config.TextColumn("Item", help="In-game display name, as the probe "
                                                             "records it. Colour codes are stripped "
                                                             "on both sides before matching."),
            "Value": st.column_config.NumberColumn("Value", format="%.4g"),
            "Limit": st.column_config.NumberColumn(
                "Limit", help="Hard cap on the quantity of this item that counts toward one seed. "
                              "Blank = uncapped."),
            "Min": st.column_config.NumberColumn(
                "Min", help="Requirement, not a score term: a seed holding fewer than this many is "
                            "dropped from the ranking entirely. Checked against the raw quantity, "
                            "not the capped one. Blank = no requirement."),
        })
    st.session_state["vt_current"] = edited

    values, limits, mins, display, problems = lootscore.parse_value_rows(edited.to_dict("records"))
    for p in problems:
        st.warning(p)

    diff = value_table_diff(base, edited)
    origin = st.session_state.get("vt_origin", "?")
    with st.expander(f"Hand edits since import — {len(diff)} "
                     f"({origin})", expanded=bool(diff)):
        if diff:
            st.dataframe(pd.DataFrame(diff), width="stretch", hide_index=True,
                         height=min(400, 40 + 35 * len(diff)))
        else:
            st.caption("None — the table is exactly as imported.")

    c1, c2, c3 = st.columns(3)
    if c1.button("Fold edits in and re-sort", disabled=not diff,
                 help="Makes the current view the new base, sorted by value. The edit list resets "
                      "to empty because there is no longer anything to compare against."):
        set_value_base(edited, f"{origin} + {len(diff)} hand edits")
        st.rerun()
    if c2.button("Discard hand edits", disabled=not diff):
        st.session_state["_vt_ver"] = st.session_state.get("_vt_ver", 0) + 1
        st.rerun()
    c3.download_button("Download as CSV", edited.to_csv(index=False), "value-table.csv", "text/csv")
    return values, limits, mins, display


def loot_source_picker(seeds, seed_limit, key):
    """-> (label, [(seed, spawn, [Chest])], default_radius, warning_or_None)."""
    sweeps = find_chest_sweeps()
    choices = ["Corpus (full generation)"] + [f"Sweep: {k}" for k in sweeps]
    pick = st.selectbox("Chest source", choices, key=f"{key}_src",
                        help="The corpus is full generation and sees every chest source. A stage-0 "
                             "sweep sees Roguelike dungeon and village chests only, but can cover a "
                             "far wider radius. Only the first N seeds of a sweep are read, where N "
                             "is the sidebar seed limit.")
    if pick.startswith("Corpus"):
        data = [(s["seed"], s["spawn"], lootscore.chests_from_corpus(s)) for s in seeds]
        return pick, data, 15, None
    name = pick[len("Sweep: "):]
    src = sweeps[name]
    # Scoring a sweep over a wider window than it was generated with quietly invents a different
    # answer: chests past its own radius are simply not in the file, so seeds look poorer for a
    # reason that has nothing to do with the seed. The generation radius is in the filename by
    # convention (…-r60.jsonl); honour it when present.
    m = re.search(r"-r(\d+)\b", name)
    gen_radius = int(m.group(1)) if m else 60
    if not _sweep_exists(src):
        return pick, [], gen_radius, "Sweep file is missing."
    rows, kills, no_spawn = load_sweep_chests(src, _sweep_mtime(src), seed_limit)
    size = Path(str(src).split("::", 1)[0]).stat().st_size
    st.caption(f"read {len(rows)} seeds"
               + (f" (sidebar limit {seed_limit})" if seed_limit else " (no limit)")
               + f" from a {size / 1e9:.2f} GB file · "
               + (f"{sum(kills.values())} killed by gates" if kills else "no gate kills")
               + (f" · generated at radius {gen_radius}" if m else
                  " · generation radius unknown (not in the filename) — defaulting to 60"))
    warn = None
    if no_spawn:
        warn = (f"{no_spawn} records carry no spawn point, so their window would be centred on the "
                f"origin. Re-run that sweep with PREFILTER_TERRAIN >= 0.")
    elif rows and not any(r[2] for r in rows):
        warn = ("This sweep contains no chests. It was run without "
                "-Dprobe.prefilter.villagechests / -Dprobe.prefilter.dungeon, so every seed will "
                "score 0 — that is the input file, not the seeds.")
    return pick, [(s, sp, _rehydrate(ch)) for s, sp, ch in rows], gen_radius, warn


def score_all(data, radius, values, limits, mins, display):
    """-> (rows, qty_by_seed, marginals, rejected). One pass, shared by the tables below.

    Seeds failing a Min requirement are held back rather than scored, and the failures are counted
    per requirement so an empty ranking says which bar was too high instead of looking like a bad
    corpus.
    """
    rows, qty_by_seed, marg, rejected = [], {}, [], Counter()
    for seed, spawn, chests in data:
        scoped = lootscore.in_scope(chests, spawn, radius)
        score, uncapped, marginals, qty = lootscore.score_seed(scoped, values, limits)
        unmet = lootscore.unmet_minimums(qty, mins)
        if unmet:
            for key, need, _got in unmet:
                rejected[f"{display.get(key, key)} ≥ {need}"] += 1
            continue
        by_src = Counter(c.source for c in scoped)
        rows.append({"score": round(score, 2), "seed": seed,
                     "uncapped": round(uncapped, 2),
                     "capped away": round(uncapped - score, 2),
                     "chests": len(scoped),
                     **{f"{k} chests": v for k, v in sorted(by_src.items())},
                     "spawn": f"{spawn[0]},{spawn[2]}",
                     "tp spawn": tp(spawn[0], None, spawn[2])})
        qty_by_seed[seed] = qty
        marg.extend((earned, seed, chest) for earned, _, chest in marginals if earned > 0)
    rows.sort(key=lambda r: -r["score"])
    marg.sort(key=lambda t: -t[0])
    return rows, qty_by_seed, marg, rejected


def render_loot(seeds, seed_limit):
    st.caption("Score seeds by chest contents against an item value table. "
               "`score = Σ value × min(quantity, limit)` over every chest inside the window. "
               "The table below is the single copy of the metric — the Baseline rates tab reads "
               "the same one.")
    label, data, default_r, warn = loot_source_picker(seeds, seed_limit, "loot")
    if warn:
        st.warning(warn)
    if not data:
        st.info("No seeds from this source.")
        return

    st.subheader("Scoring metric")
    values, limits, mins, display = value_table_editor()
    if not values:
        st.info("The value table is empty — add at least one item.")
        return

    radius = st.number_input("Window radius (chunks from the spawn chunk, chebyshev)",
                             min_value=1, max_value=200, value=default_r, step=1)
    rows, qty_by_seed, marg, rejected = score_all(data, radius, values, limits, mins, display)
    if rejected:
        st.warning("Held back by Min requirements: "
                   + ", ".join(f"{k} — {v} seed{'s' if v != 1 else ''}"
                               for k, v in rejected.most_common())
                   + f". {len(rows)} of {len(data)} seeds qualify.")
    if not rows:
        st.info("No seed meets every Min requirement." if rejected else "Nothing scored.")
        return

    st.subheader(f"Seeds ({len(rows)} scored)")
    scores = [r["score"] for r in rows]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("best", f"{scores[0]:,.0f}")
    c2.metric("median", f"{scores[len(scores) // 2]:,.0f}")
    c3.metric("worst", f"{scores[-1]:,.0f}")
    c4.metric("best / median",
              f"{scores[0] / scores[len(scores) // 2]:.2f}" if scores[len(scores) // 2] else "—")
    tall_table(pd.DataFrame(rows), 340)

    st.subheader("Top scoring chests")
    st.caption("Ranked by marginal contribution — what the chest earns once every richer chest has "
               "already spent the per-item caps. A chest whose items were all capped away scores 0 "
               "and is not listed.")
    n_chests = st.number_input("How many", min_value=5, max_value=500, value=25, step=5)
    tall_table(pd.DataFrame([{
        "earns": round(earned, 2), "seed": seed,
        "source": chest.source + (f" {chest.category}" if chest.category else ""),
        "x": chest.pos[0], "y": chest.pos[1], "z": chest.pos[2],
        "y nominal": chest.y_nominal,
        "tp": tp(chest.pos[0], chest.pos[1] + 1, chest.pos[2]),
        "contents": lootscore.chest_contents(chest, values, display),
    } for earned, seed, chest in marg[:int(n_chests)]]), 420)

    st.subheader("Everything that fed the score")
    # "(top scorer)" is a sentinel rather than a seed id, so it re-resolves every run and follows the
    # ranking as the metric is edited — which is what you want while tinkering. Picking an explicit
    # seed pins it, because the selectbox is keyed and Streamlit keeps a keyed widget's value across
    # reruns. Two behaviours, one widget, no extra checkbox to get out of sync.
    seed_opts = [TOP_SCORER, "(all seeds combined)"] + [r["seed"] for r in rows]
    if st.session_state.get("loot_breakdown") not in seed_opts:
        st.session_state["loot_breakdown"] = TOP_SCORER  # source or limit changed; the pin is stale
    which = st.selectbox(
        "Breakdown for", seed_opts, key="loot_breakdown",
        help="Defaults to whichever seed currently ranks first and follows it as you edit the "
             "metric. Select a specific seed to pin it across reruns.")
    if which == "(all seeds combined)":
        qty = Counter()
        for q in qty_by_seed.values():
            qty.update(q)
        denom = len(rows)
    else:
        seed = rows[0]["seed"] if which == TOP_SCORER else which
        qty, denom = qty_by_seed[seed], 1
        if which == TOP_SCORER:
            st.caption(f"top scorer: seed {seed}")
    breakdown = []
    for k, n in qty.items():
        c = lootscore.counted(k, n, limits) if denom == 1 else n
        breakdown.append({"item": display.get(k, k), "found": n,
                          "counted": c, "value": values[k],
                          "contribution": round(values[k] * c, 2),
                          "per seed": round(n / denom, 4),
                          "capped": bool(denom == 1 and c != n)})
    breakdown.sort(key=lambda r: -r["contribution"])
    tot = sum(r["contribution"] for r in breakdown) or 1
    for r in breakdown:
        r["% of score"] = round(100 * r["contribution"] / tot, 2)
    tall_table(pd.DataFrame(breakdown), 420)
    unvalued = Counter()
    for seed, spawn, chests in data:
        for chest in lootscore.in_scope(chests, spawn, radius):
            for name, q in chest.items:
                if lootscore.norm(name) not in values:
                    unvalued[lootscore.clean(name)] += q
    with st.expander(f"Items carrying no value ({len(unvalued)} distinct) — candidates to add"):
        tall_table(pd.DataFrame(
            [{"item": n, "total": q, "per seed": round(q / len(rows), 3)}
             for n, q in unvalued.most_common(200)]), 300)


def render_baseline(seeds, seed_limit):
    st.caption("How often each item actually turns up, measured on the seeds currently loaded. "
               "This is the input to rarity normalisation: dividing an item's value by its mean "
               "appearances per seed makes every item's *expected* contribution equal its stated "
               "value, which stops bulk items like Redstone dominating.")
    label, data, default_r, warn = loot_source_picker(seeds, seed_limit, "base")
    if warn:
        st.warning(warn)
    if not data:
        st.info("No seeds from this source.")
        return
    # No explicit key on purpose. A keyed widget's stored value outranks a changed `value=`
    # argument, so switching source from corpus (default 15) to a sweep (default 60) would silently
    # keep 15 and every rate below would be measured over the wrong window.
    radius = st.number_input("Window radius (chunks)", min_value=1, max_value=200,
                             value=default_r, step=1)

    counts, present = Counter(), Counter()
    for seed, spawn, chests in data:
        seen_here = set()
        for chest in lootscore.in_scope(chests, spawn, radius):
            for name, q in chest.items:
                k = lootscore.norm(name)
                counts[k] += q
                seen_here.add(k)
        for k in seen_here:
            present[k] += 1
    n = len(data)
    if not counts:
        st.info("No chest items in this window.")
        return

    # vt_current is this pass's edited view, published by the Loot score tab, which renders earlier
    # in the same script run — so this tab reflects the edit just made, not the previous one. It
    # falls back to the base if that tab returned early (no source, empty table).
    values, limits, display = ({}, {}, {})
    table = st.session_state.get("vt_current")
    if table is None:
        table = st.session_state.get("vt_base")
    if table is not None:
        values, limits, _mins, display, _ = lootscore.parse_value_rows(
            table.to_dict("records"))
    else:
        st.info("Open the Loot score tab first to load a value table; without one this tab shows "
                "rates only.")

    # k is pseudo-sightings, so a fixed default is wrong at a different sample size: k=200 against
    # 5000 seeds is a mild 4% prior, but against 100 seeds it is twice the data and the prior
    # decides the answer. Scale it and say so.
    k_default = max(1, round(0.04 * n))
    k_smooth = st.slider(
        "Smoothing k (pseudo-sightings)", 0, max(50, 4 * k_default), k_default,
        max(1, k_default // 8),
        help="Normalised value = value / ((total + k) / seeds). k bounds the largest multiplier an "
             "item can earn at seeds/k, which is what stops an item seen twice from topping the "
             "table. At k=0 the scale is a lottery on whichever rare item a seed happened to roll. "
             "The default is 4% of the loaded seed count.")
    if n < 500:
        st.caption(f"⚠ {n} seeds is a small sample for rate estimation. "
                   f"{sum(1 for c in counts.values() if c < 100)} of {len(counts)} items have "
                   "under 100 sightings, and their rates — hence their normalised values — are "
                   "wide guesses. Raise the sidebar seed limit before exporting a table.")

    rows = []
    for key, total in counts.items():
        name = display.get(key) or key
        v = values.get(key)
        rows.append({
            "item": name,
            "total": total,
            "per seed": round(total / n, 4),
            "seeds containing": present[key],
            "% of seeds": round(100 * present[key] / n, 1),
            "value": v,
            "normalised": (round(lootscore.normalised_value(v, total, n, k_smooth), 4)
                           if v is not None else None),
            "low sample": total < 100,
        })
    rows.sort(key=lambda r: -r["total"])
    st.caption(f"{n} seeds, radius {radius} chunks · {len(rows)} distinct items · "
               f"{sum(1 for r in rows if r['value'] is not None)} of them carry a value")
    tall_table(pd.DataFrame(rows), 460)

    valued = [r for r in rows if r["normalised"] is not None]
    if valued:
        never = [display.get(k, k) for k in values if k not in counts]
        if never:
            with st.expander(f"In the value table but never seen here ({len(never)})"):
                st.write(", ".join(sorted(never)))
        st.caption("A rate measured on a small sample is a wide estimate — that is what k is "
                   "compensating for, not fixing. `low sample` marks items under 100 sightings.")
        out = pd.DataFrame([{"Item": r["item"], "Value": r["normalised"],
                             "Limit": limits.get(lootscore.norm(r["item"])),
                             "Min": _mins.get(lootscore.norm(r["item"])),
                             "Notes": f"was {r['value']:g}; {r['total']} seen in {n} seeds; k={k_smooth}"}
                            for r in valued])
        st.download_button("Download normalised value table", out.to_csv(index=False),
                           f"value-table-normalised-k{k_smooth}.csv", "text/csv")
        if st.button("Use this normalised table as the scoring metric"):
            set_value_base(out[["Item", "Value", "Limit", "Min"]],
                           f"normalised from {n} seeds, k={k_smooth}")
            st.session_state.pop("_vt_preset", None)
            st.success("Loaded into the Loot score tab's editor.")


# ------------------------------------------------------------- loot reference (static tables)

REFERENCE = REPO / "reference"
# Sections the reference tab shows. Forestry genomes, knowledge notes, vis amulets and the
# odds-and-ends bucket are dropped: they are noise for routing, not gear.
REF_KINDS = ["enchanted", "enchanted book", "Tinkers' tool", "GT tool", "charged"]


@st.cache_data
def load_reference(path, mtime):
    return pd.read_csv(path, keep_default_na=False)


def render_reference():
    st.caption("What the pack's loot tables actually contain, resolved from each entry's NBT — "
               "enchantments by name, tool materials and stats, weapon damage in hearts. This is a "
               "static reference for the **tables**, not the chests of any seed: an entry says what "
               "the item looks like whenever that table rolls it.")
    packs = sorted(p.name for p in REFERENCE.glob("*") if (p / "nbt-items.csv").is_file()) \
        if REFERENCE.is_dir() else []
    if not packs:
        st.info(f"No reference data — expected {REFERENCE}/<pack>/nbt-items.csv. "
                "Generate it with gtnh-determinism/seedsearch/loot-nbt-report.py.")
        return
    pack = st.selectbox("Pack", packs, index=len(packs) - 1)
    base = REFERENCE / pack
    df = load_reference(str(base / "nbt-items.csv"), (base / "nbt-items.csv").stat().st_mtime)

    search = st.text_input("Filter", placeholder="item name, enchantment, material…",
                           help="Matches the item name and the resolved description.")
    if search:
        s = search.casefold()
        df = df[df.apply(lambda r: s in str(r["item"]).casefold()
                         or s in str(r["description"]).casefold(), axis=1)]

    weapons = df[df["hearts"] != ""].copy()
    if not weapons.empty:
        st.subheader(f"Weapons ({len(weapons)})")
        st.caption("Hearts are health points halved, matching the tooltip. Base damage is the item's "
                   "`attackDamage` attribute, or `InfiTool.Attack` for Tinkers' tools. Sharpness adds "
                   "1.25 HP per level against everything; Smite and Bane of Arthropods add 2.5 per "
                   "level but only against undead and arthropods, so those are separate columns "
                   "rather than folded into the headline number.")
        for c in ("hearts", "hearts_vs_undead", "hearts_vs_arthropod", "attack_hp", "base_attack_hp"):
            weapons[c] = pd.to_numeric(weapons[c], errors="coerce")
        # The report leads the description with the damage; here it has its own columns, so drop the
        # duplicate rather than printing the same number twice on one row.
        weapons["description"] = weapons["description"].str.replace(
            r"^[^·]*hearts[^·]*(· )?", "", regex=True)
        weapons = weapons.sort_values("hearts", ascending=False)
        tall_table(weapons[["item", "hearts", "hearts_vs_undead", "hearts_vs_arthropod",
                            "attack_hp", "base_attack_hp", "description", "found in"]], 420)

    armour = df[df["armor_points"] != ""].copy()
    if not armour.empty:
        st.subheader(f"Armour ({len(armour)})")
        st.caption("Ranked by **damage reduction**, not by armour points — the two mitigations "
                   "multiply, so points alone gets the order wrong. 1.7.10 applies "
                   "`damage × (25 − armour)/25` and then `damage × (25 − i)/25`, where `i` is not "
                   "the raw EPF: it is `ceil(EPF/2) + rand(0 … floor(EPF/2))`, clamped to 20, so the "
                   "enchantment half is random per hit and the column is its exact expectation. "
                   "Armour points come from `ItemArmor.damageReduceAmount` (2 points = 1 icon; a "
                   "full vanilla diamond set is 20, worth 80%). `EPF all` is Protection, the only "
                   "one that applies to every damage source; Fire, Fall, Blast and Projectile are "
                   "conditional and stay in their own column rather than being summed in.")
        st.caption("Both inputs are SET totals in game. Scoring one piece as if it were the whole "
                   "set is a convention — monotonic in both inputs, so the ranking holds, but the "
                   "absolute percentage is only reached when nothing else is worn. Forge "
                   "`ISpecialArmor` items compute protection at damage time and would be "
                   "understated; none of this pack's loot armour is one.")
        for c in ("damage_reduction_pct", "armor_points", "armor_icons", "epf_all", "durability"):
            armour[c] = pd.to_numeric(armour[c], errors="coerce")
        # Strip the leading stat clauses the report puts in the description — they have their own
        # columns here. Repeating group because a piece can carry several EPF clauses in a row.
        armour["description"] = armour["description"].str.replace(
            r"^[^·]*reduction[^·]*(· )?[^·]*armour[^·]*(· )?(\+[^·]*EPF[^·]*(· )?)*", "",
            regex=True)
        armour = armour.sort_values(["damage_reduction_pct", "armor_points"], ascending=False)
        tall_table(armour[["item", "damage_reduction_pct", "armor_points", "armor_icons", "slot",
                           "epf_all", "epf_conditional", "durability", "description",
                           "found in"]], 420)

    for kind in REF_KINDS:
        sub = df[df["kind"] == kind]
        if sub.empty:
            continue
        st.subheader(f"{kind} ({len(sub)})")
        cols = ["item", "description", "found in"]
        if (sub["hearts"] != "").any():
            cols.insert(2, "hearts")
        tall_table(sub[cols], 380)

    hidden = sorted(set(df["kind"]) - set(REF_KINDS))
    if hidden:
        st.caption("Not shown, as gear-irrelevant: " + ", ".join(
            f"{k} ({int((df['kind'] == k).sum())})" for k in hidden)
            + ". They are in the CSV.")
    ench_path = base / "enchantments.csv"
    if ench_path.is_file():
        with st.expander("Enchantment id → name (this pack)"):
            st.caption("Ids are assigned at registration and are pack-specific, so this map is "
                       "dumped from a running server rather than assumed. About half of the ids the "
                       "tables use are not vanilla.")
            tall_table(load_reference(str(ench_path), ench_path.stat().st_mtime), 320)


def main():
    st.set_page_config(page_title="gtnh-seedlib browser", layout="wide",
                       initial_sidebar_state="expanded")
    disable_bare_hotkeys()
    versions = find_versions()
    if not versions:
        st.error("No corpora found — expected gtnh-*/**.tar.gz next to browser/. "
                 "If tarballs are 3-line pointer files, run `git lfs pull`.")
        st.stop()
    tar_key, mats, seeds, seed_limit = render_sidebar(versions)
    things = all_things(tar_key)

    (tab_overview, tab_query, tab_coke, tab_loot, tab_baseline, tab_reference, tab_prefilter,
     tab_detail) = st.tabs(
        ["Seed overview", "Cluster query", "coke%", "Loot score", "Baseline rates",
         "Loot reference", "Prefilter", "Seed detail"])
    with tab_overview:
        render_overview(seeds, mats, things)
    with tab_query:
        render_query(seeds, mats, things)
    with tab_coke:
        render_coke(seeds)
    with tab_loot:
        render_loot(seeds, seed_limit)
    with tab_baseline:
        render_baseline(seeds, seed_limit)
    with tab_reference:
        render_reference()
    with tab_prefilter:
        render_prefilter()
    with tab_detail:
        render_detail(seeds, mats)


if __name__ == "__main__":
    main()
