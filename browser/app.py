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
    for idx, m, n in zip(ch["ores_idx"], ch["ores_m"], ch["ores_n"]):
        t = ore_thing(int(m), mats)
        if t in wanted:
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
            out[f"local: {p.parent.name}/{p.name}"] = str(p)
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

    rows = []
    for d in survivors:
        r = prefilter_row(d, max_vd, fbonus, wcols, scols, ccols)
        if r is None:
            continue
        if require_all and any(r[k] >= PREFILTER_CAP for k in ("paper", "tic", "furnace")):
            continue
        rows.append(r)
    rows.sort(key=lambda r: r["score"])
    st.write(f"**{len(rows)}** seeds pass the current village rules")
    if rows:
        df = pd.DataFrame(rows)
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
    tar_key = tuple(sorted((p, Path(p).stat().st_mtime) for p in tars))
    return tar_key, mats, seeds


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


def main():
    st.set_page_config(page_title="gtnh-seedlib browser", layout="wide",
                       initial_sidebar_state="expanded")
    disable_bare_hotkeys()
    versions = find_versions()
    if not versions:
        st.error("No corpora found — expected gtnh-*/**.tar.gz next to browser/. "
                 "If tarballs are 3-line pointer files, run `git lfs pull`.")
        st.stop()
    tar_key, mats, seeds = render_sidebar(versions)
    things = all_things(tar_key)

    tab_overview, tab_query, tab_coke, tab_prefilter, tab_detail = st.tabs(
        ["Seed overview", "Cluster query", "coke%", "Prefilter", "Seed detail"])
    with tab_overview:
        render_overview(seeds, mats, things)
    with tab_query:
        render_query(seeds, mats, things)
    with tab_coke:
        render_coke(seeds)
    with tab_prefilter:
        render_prefilter()
    with tab_detail:
        render_detail(seeds, mats)


if __name__ == "__main__":
    main()
