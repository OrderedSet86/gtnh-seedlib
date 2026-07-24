#!/usr/bin/env python3
"""Streamlit browser for gtnh-seedlib corpora.

Reads the per-version tarballs in this repo directly (no manual extraction) and
provides:
  - a sortable per-seed overview table (click column headers to sort),
  - a cluster query tab: ">= N of thing A, B, C within Y blocks of spawn and
    within Z blocks of each other" over chest loot and GT ores,
  - a per-seed detail view (biomes, veins, chests, villages, witchery).

Launch: browser/run.sh  (or: uv run --with-requirements browser/requirements.txt \
        streamlit run browser/app.py)
"""
import json
import math
import re
import tarfile
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

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
                    "names": Counter(names)})
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
    """Parse a seedlib tarball into (mats, [seed records]). mtime busts the cache."""
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
            d = json.load(tf.extractfile(m))
            search = d.get("search", {})
            chests = []
            chunks = []
            for key, c in search.get("chunks", {}).items():
                cx, cz = map(int, key.split(","))
                pop = c.get("populated", True)
                chunks.append({"cx": cx, "cz": cz, "biome": c.get("biome", "?"),
                               "water": c.get("water", 0), "clay": c.get("clay", 0),
                               "populated": pop,
                               "ores": {int(k): v for k, v in c.get("ores", {}).items()}})
                for chest in c.get("chests", []):
                    items = [(it.get("name") or f'{it["id"]}:{it["d"]}', it["n"])
                             for it in chest.get("items", [])]
                    chests.append({"pos": chest["pos"], "type": chest.get("type", "?"),
                                   "populated": pop, "items": items})
            seeds.append({
                "seed": d["seed"],
                "spawn": search.get("spawn", [0, 0, 0]),
                "chunks": chunks,
                "chests": chests,
                "villages": _parse_villages(d.get("villages", [])),
                "witchery": _parse_witchery(d.get("witchery", [])),
            })
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
        for c in s["chunks"]:
            things.update(ore_thing(m, mats) for m in c["ores"])
    return sorted(things)


def dist2d(ax, az, bx, bz):
    return math.hypot(ax - bx, az - bz)


def has_tic_house(village):
    return bool(TIC_PIECES & village["names"].keys())


def tp(x, y, z):
    """1.7.10 teleport command. y=None keeps current height (~); chest callers
    pass y+1 so you land on top of the block, not inside it."""
    return f"/tp {round(x)} {'~' if y is None else round(y)} {round(z)}"


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
    for c in s["chunks"]:
        counts = Counter()
        for m, n in c["ores"].items():
            t = ore_thing(m, mats)
            if t in wanted:
                counts[t] += n
        if counts:
            sites.append((c["cx"] * 16 + 8, c["cz"] * 16 + 8, None, "ore chunk", counts))
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


# ------------------------------------------------------------------------ UI

def render_sidebar(versions):
    with st.sidebar:
        st.title("gtnh-seedlib")
        label = st.selectbox("Pack version", list(versions))
        tars = versions[label]
        try:
            mats, seeds = load_version(tars)
        except tarfile.ReadError:
            st.error("Not a valid tarball — likely a git-LFS pointer file. "
                     "Run `git lfs pull` in the repo.")
            st.stop()
        st.caption(f"{len(seeds)} seeds merged from {len(tars)} corpus "
                   f"file{'s' if len(tars) != 1 else ''} · window radius 15 "
                   "chunks (~240 blocks) around spawn, plus the generated fringe "
                   "beyond it · distances are horizontal (x, z)")
        complete_only = st.checkbox("Exclude partially-generated chunks", value=False)
        st.caption("Fringe chunks marked `populated: false` never ran their own "
                   "decoration pass — they only hold ores/chests spilled over from "
                   "finished neighbors. Their data is real but incomplete: fine "
                   "when hunting for things, misleading when the *absence* of "
                   "something matters (e.g. \"no copper near spawn\"). Tick this "
                   "to count complete chunks only.")
        if complete_only:
            seeds = [{**s,
                      "chunks": [c for c in s["chunks"] if c["populated"]],
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
        "Surface chests only (y ≥ 50) for thing columns", value=False,
        help="Roguelike dungeon loot is mostly y<50; surface ruins are y>50.")

    rows = []
    for s in seeds:
        biomes = Counter(c["biome"] for c in s["chunks"])
        row = {
            "seed": str(s["seed"]),
            "spawn x": s["spawn"][0], "spawn z": s["spawn"][2],
            "water": sum(c["water"] for c in s["chunks"]),
            "clay": sum(c["clay"] for c in s["chunks"]),
            "chests": len(s["chests"]),
            "villages": len(s["villages"]),
            "TiC house": any(has_tic_house(v) for v in s["villages"]),
            "witchery": len(s["witchery"]),
            "top biomes": ", ".join(b for b, _ in biomes.most_common(3)),
        }
        for t in thing_cols:
            total = 0
            if t.startswith("Ore"):
                for c in s["chunks"]:
                    total += sum(n for m, n in c["ores"].items()
                                 if ore_thing(m, mats) == t)
            else:
                for chest in s["chests"]:
                    if surface_only and chest["pos"][1] < 50:
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


def render_detail(seeds, mats):
    seed_pick = st.selectbox("Seed", [str(s["seed"]) for s in seeds])
    s = next(x for x in seeds if str(x["seed"]) == seed_pick)
    sx, sy, sz = s["spawn"]
    st.markdown(f"Spawn **{sx}, {sy}, {sz}** · `/tp {sx} {sy} {sz}`")
    st.caption("tp columns are 1.7.10 syntax (needs cheats/op) — click a cell and "
               "Ctrl+C to copy. Chest tps target one block above the chest; "
               "`~` keeps your current height where no y is known.")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Water", sum(c["water"] for c in s["chunks"]))
    m2.metric("Clay", sum(c["clay"] for c in s["chunks"]))
    m3.metric("Chests", len(s["chests"]))
    m4.metric("Villages", len(s["villages"]))
    m5.metric("Witchery sites", len(s["witchery"]))

    col_l, col_r = st.columns(2)
    with col_l:
        st.subheader("Biomes")
        biomes = Counter(c["biome"] for c in s["chunks"])
        st.dataframe(pd.DataFrame(
            [{"biome": b, "chunks": n} for b, n in biomes.most_common()]),
            width="stretch", hide_index=True)

        st.subheader("Ores (window total)")
        big, small = Counter(), Counter()
        for c in s["chunks"]:
            for m, n in c["ores"].items():
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
    chest_rows = [{
        "x": c["pos"][0], "y": c["pos"][1], "z": c["pos"][2],
        "dist from spawn": round(dist2d(c["pos"][0], c["pos"][2], sx, sz)),
        "tp": tp(c["pos"][0], c["pos"][1] + 1, c["pos"][2]),
        "type": c["type"], "stacks": len(c["items"]),
        "contents": ", ".join(f"{n}× {t}" for t, n in c["items"]),
    } for c in sorted(s["chests"],
                      key=lambda c: dist2d(c["pos"][0], c["pos"][2], sx, sz))]
    st.dataframe(pd.DataFrame(chest_rows), width="stretch", hide_index=True,
                 height=500)


def main():
    st.set_page_config(page_title="gtnh-seedlib browser", layout="wide",
                       initial_sidebar_state="expanded")
    versions = find_versions()
    if not versions:
        st.error("No corpora found — expected gtnh-*/**.tar.gz next to browser/. "
                 "If tarballs are 3-line pointer files, run `git lfs pull`.")
        st.stop()
    tar_key, mats, seeds = render_sidebar(versions)
    things = all_things(tar_key)

    tab_overview, tab_query, tab_detail = st.tabs(
        ["Seed overview", "Cluster query", "Seed detail"])
    with tab_overview:
        render_overview(seeds, mats, things)
    with tab_query:
        render_query(seeds, mats, things)
    with tab_detail:
        render_detail(seeds, mats)


if __name__ == "__main__":
    main()
