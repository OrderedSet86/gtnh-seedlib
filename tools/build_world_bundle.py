#!/usr/bin/env python3
"""Bake a per-seed data bundle for the routemap viewer.

Reads the analysis outputs that already exist for one world -- the stage-0 prefilter record,
the vein CSVs, the loot CSV, and (optionally) a search-mode probe report per dimension -- and
writes a self-contained directory of JSON and PNG that routemap/ fetches over HTTP.

    tools/build_world_bundle.py \
        --seed -1636594104014467454 --pack daily-707 \
        --prefilter ~/.cache/gtnh-determinism/sweep-final/one.jsonl \
        --loot-csv ~/Downloads/loot-1636594104014467454.csv \
        --veins-ow ~/.cache/gtnh-determinism/veins/out/veins-overworld.csv \
        --veins-tf ~/.cache/gtnh-determinism/veins/out/veins-tf.csv \
        --ow-search ~/.cache/gtnh-determinism/worldmap/ow-search.json \
        --tf-search ~/.cache/gtnh-determinism/worldmap/tf-search.json \
        --out worlds/-1636594104014467454

Every stage is independent: pass only the inputs you have and the rest are skipped, so the
vein/loot/POI layers can be built long before a probe run finishes.

Coordinates are block coordinates throughout, except where a field name says chunk. The
prefilter record stores village/dungeon/stronghold sites as chunk coords and witchery cells as
block coords; chunk->block uses cx*16+8, matching seedsearch/poi-export.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Sea level in these RWG worlds. Open water tops out at y 62, not the vanilla 63.
SEA_LEVEL = 62

# `surf` uses 0 as "no terrain found in this column". y=0 is bedrock-only in practice, so a
# zero byte means void rather than a genuine surface.
SURF_VOID = 0


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------------------
# prefilter record -> POIs
# --------------------------------------------------------------------------------------

# "78 pieces: ComponentSmeltery@204,64,-495..210,66,-487; ComponentToolWorkshop@175,64,-494..181,69,-488"
PIECE_RE = re.compile(
    r"([A-Za-z0-9_$.]+)@(-?\d+),(-?\d+),(-?\d+)\.\.(-?\d+),(-?\d+),(-?\d+)"
)

# Two different TiC pieces, tracked separately because they are not the same prize: the
# smeltery is the one worth routing to. ComponentWorkshop (no "Tool") is Railcraft, not TiC --
# see README.md's piece-name to source-mod table.
SMELTERY_PIECE = "ComponentSmeltery"
TIC_WORKSHOP_PIECE = "ComponentToolWorkshop"


def parse_pieces(s: str | None) -> list[dict]:
    if not s:
        return []
    out = []
    for m in PIECE_RE.finditer(s):
        name = m.group(1)
        out.append(
            {
                "name": name.rsplit(".", 1)[-1].rsplit("$", 1)[-1],
                "full": name,
                "b": [int(m.group(i)) for i in (2, 3, 4, 5, 6, 7)],
            }
        )
    return out


def load_prefilter(path: Path, seed: int) -> dict:
    for line in path.open():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("seed") == seed:
            return rec
    raise SystemExit(f"seed {seed} not found in {path}")


def pois_from_prefilter(rec: dict) -> dict:
    spawn = rec.get("spawn") or [0, 64, 0]
    out: dict = {"spawn": {"x": spawn[0], "y": spawn[1], "z": spawn[2]}}

    villages = []
    for i, v in enumerate(rec.get("village_starts") or [], 1):
        cx, cz = v["c"]
        pieces = parse_pieces(v.get("pieces"))
        villages.append(
            {
                "i": i,
                "name": f"Village {i}",
                "x": cx * 16 + 8,
                "z": cz * 16 + 8,
                "cx": cx,
                "cz": cz,
                "sizeable": bool(v.get("sizeable")),
                "smeltery": any(p["name"] == SMELTERY_PIECE for p in pieces),
                "tic_workshop": any(p["name"] == TIC_WORKSHOP_PIECE for p in pieces),
                "pieces": pieces,
                "n_chests": len(v.get("chests") or []),
                "n_unpredicted": len(v.get("chests_unpredicted") or []),
            }
        )
    out["villages"] = villages

    dungeons = []
    for i, d in enumerate(rec.get("dungeons") or [], 1):
        cx, cz = d["trigger"]
        dungeons.append(
            {
                "i": i,
                "tower": d.get("tower") or "?",
                # The trigger chunk is the actionable coordinate: the dungeon does not exist
                # until this chunk populates, and its chests sit a median 128 blocks away.
                "x": cx * 16 + 8,
                "z": cz * 16 + 8,
                "cx": cx,
                "cz": cz,
                "n_chests": len(d.get("chests") or []),
                "enchant_tables": d.get("enchant_tables") or [],
            }
        )
    out["dungeons"] = dungeons

    witchery = []
    for c in rec.get("witchery_cells") or []:
        x, z = c["cell"]
        # The cell itself is only (x, z). The only height anywhere in the record is a chest
        # position inside the structure, so use that when there is one and admit to none if not.
        chests = c.get("chests") or []
        y = chests[0]["pos"][1] if chests else None
        witchery.append(
            {
                "x": x,
                "z": z,
                "y": y,
                # null winner = the handler race did not resolve worldlessly; nothing is
                # promised to be there.
                "winner": (c.get("winner") or "").replace("WorldHandler", "") or None,
                "biome": c.get("biome"),
                "allowed": bool(c.get("allowed")),
                "n_chests": len(c.get("chests") or []),
            }
        )
    out["witchery"] = witchery

    strongholds = []
    for i, s in enumerate(rec.get("strongholds") or [], 1):
        cx, cz = s["c"]
        strongholds.append(
            {
                "i": i,
                "x": cx * 16 + 8,
                "z": cz * 16 + 8,
                "cx": cx,
                "cz": cz,
                "n_pieces": s.get("n"),
                "n_chests": len(s.get("chests") or []),
                "pieces": parse_pieces(s.get("pieces")),
            }
        )
    out["strongholds"] = strongholds

    # biomeregion stores each square as (corner chunk, side in chunks).
    squares = []
    br = rec.get("biomeregion") or {}
    for kind, side_key, corner_key in (
        ("no-rain", "n", "sq"),
        ("humid", "hn", "hsq"),
    ):
        side = br.get(side_key)
        corner = br.get(corner_key)
        if not side or not corner:
            continue
        cx, cz = corner
        squares.append(
            {
                "kind": kind,
                "side_chunks": side,
                "x1": cx * 16,
                "z1": cz * 16,
                "x2": (cx + side) * 16,
                "z2": (cz + side) * 16,
                "cx": cx + side // 2,
                "cz": cz + side // 2,
            }
        )
    out["squares"] = squares
    out["biomeregion"] = br
    return out


def pois_from_tffeatures(report: dict) -> dict:
    """TF structures from the probe's `tffeatures` section.

    One entry per 16-chunk grid region. Feature *centres* are seed-independent (jittered from
    the region coords alone); only which feature lands in a region varies with the seed.
    Regions with nothing in them are emitted as "nothing" and dropped here.
    """
    tf = report.get("tffeatures") or {}
    regions = tf.get("regions") or {}
    feats = []
    for key, f in sorted(regions.items()):
        name = f.get("feature")
        if not name or name == "nothing":
            continue
        x, z = f.get("center", [0, 0])
        feats.append(
            {
                "region": key,
                "name": name,
                "id": f.get("id"),
                "size": f.get("size"),
                "x": x,
                "z": z,
                "biome": f.get("biome"),
            }
        )
    return {
        "features": feats,
        "kinds": _counts(f["name"] for f in feats),
        "grid_chunks": tf.get("gridChunks", 16),
    }


# --------------------------------------------------------------------------------------
# vein CSV -> JSON
# --------------------------------------------------------------------------------------


def veins_from_csv(path: Path) -> dict:
    rows = list(csv.DictReader(path.open(newline="")))
    veins = []
    has_stability = "route_stable" in (rows[0] if rows else {})
    for r in rows:
        veins.append(
            {
                "ore": r["ore"],
                "layer": r["layer"],
                "x": int(r["centre_x"]),
                "z": int(r["centre_z"]),
                "y": int(r["min_y"]),
                "b": [
                    int(r["west_x"]),
                    int(r["north_z"]),
                    int(r["east_x"]),
                    int(r["south_z"]),
                ],
                "d": int(r["dist_from_spawn"]),
                # GT vein placement is not route-stable on this pack: an A/B of a rows walk
                # against a spiral walk disagreed on 113 of 1254 overworld veins. Anything
                # flagged here may simply not be there.
                "stable": (r.get("route_stable", "yes") != "NO"),
            }
        )
    ores: dict[str, int] = {}
    for v in veins:
        ores[v["ore"]] = ores.get(v["ore"], 0) + 1
    return {
        "veins": veins,
        "ores": dict(sorted(ores.items(), key=lambda kv: (-kv[1], kv[0]))),
        "has_stability": has_stability,
        "unstable": sum(1 for v in veins if not v["stable"]),
    }


# --------------------------------------------------------------------------------------
# loot CSV -> JSON
# --------------------------------------------------------------------------------------

# y_note is load-bearing: it says whether the Y in the row is somewhere you can teleport to.
Y_CONF = {
    "exact": "exact",
    "approx - may be one low": "approx",
    "nominal - fly down": "nominal",
    "nominal - fly down, Y is the piece box origin": "nominal",
    "sky - piece centre, not a chest position": "sky",
}


def tpCmd(r: dict) -> str:
    return f"/tp {r['x']} {int(r['y']) + 1} {r['z']}"


def loot_from_csv(path: Path) -> dict:
    chests: dict[str, dict] = {}
    items: dict[str, int] = {}
    for r in csv.DictReader(path.open(newline="")):
        cid = r["chest_id"]
        c = chests.get(cid)
        if c is None:
            note = r.get("y_note", "")
            c = chests[cid] = {
                "id": int(cid),
                "x": int(r["x"]),
                "y": int(r["y"]),
                "z": int(r["z"]),
                "src": r["source"],
                "struct": r.get("structure") or "",
                "cat": r.get("category") or "",
                "val": int(r["chest_value"] or 0),
                "d": int(r["dist_from_spawn"] or 0),
                # Carry the exporter's own /tp verbatim. It is NOT x,y,z: for an exact chest it
                # targets the block above (so you do not land inside the chest), for a nominal
                # or sky row it targets y 200 to fly down from, and for a surface-resolved row
                # it is a separately computed Y. Regenerating it from the columns is wrong.
                "tp": r.get("tp") or tpCmd(r),
                # Roguelike chests only exist once the dungeon's trigger chunk has populated,
                # so the trigger is a prerequisite, not a convenience.
                "trigger": r.get("structure_tp") or "",
                "yconf": Y_CONF.get(note, note.split(" ")[0] if note else "?"),
                "ynote": note,
                "predicted": r.get("predicted") == "yes",
                "conf": r.get("contents_confidence") or "",
                "items": [],
                "_names": set(),
            }
        name = r["item"]
        c["_names"].add(name)
        c["items"].append(
            {
                "n": name,
                "id": r.get("item_id") or "",
                "c": int(r["count"] or 0),
                "v": int(r["stack_value"] or 0),
                "gate": r.get("min_gate_item") == "yes",
            }
        )

    # Count chests per item, not stacks: on a map the useful number is how many points light
    # up when you select the item, and a chest can hold several stacks of the same thing.
    for c in chests.values():
        for name in c.pop("_names"):
            items[name] = items.get(name, 0) + 1

    out = sorted(chests.values(), key=lambda c: -c["val"])
    return {
        "chests": out,
        "items": dict(sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))),
        "sources": _counts(c["src"] for c in out),
        "yconf": _counts(c["yconf"] for c in out),
    }


def _counts(it) -> dict[str, int]:
    d: dict[str, int] = {}
    for v in it:
        d[v] = d.get(v, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------------------
# biome palette
# --------------------------------------------------------------------------------------

# Amidst's palette is the only source of real biome render colours in any of these repos --
# BiomeTable.java and biomes.json carry temperature/rainfall/types but no colour. It covers the
# vanilla and common modded biomes by display name and misses every RWG river/ocean variant, so
# anything it does not have is derived below and tagged as such.
AMIDST_REL = "AmidstGTNH/src/main/resources/assets/forgeamidst/biome/default.json"

# Fallback colours by BiomeDictionary type, most specific first. Picked to stay legible against
# the hillshade multiply and to keep water clearly distinct from land.
TYPE_COLOURS: list[tuple[str, tuple[int, int, int]]] = [
    ("END", (200, 200, 160)),
    ("NETHER", (110, 30, 30)),
    ("MUSHROOM", (150, 110, 160)),
    ("OCEAN", (30, 55, 120)),
    # A river biome's tint is only ever visible on its banks -- the water itself is painted
    # from the water mask -- so this is a damp-bank tone, not water blue.
    ("RIVER", (92, 130, 136)),
    ("BEACH", (222, 208, 150)),
    ("MESA", (180, 105, 60)),
    ("SNOWY", (235, 240, 245)),
    ("WASTELAND", (130, 120, 100)),
    ("DEAD", (120, 110, 95)),
    ("SWAMP", (85, 105, 75)),
    ("JUNGLE", (40, 110, 45)),
    ("SAVANNA", (170, 165, 90)),
    ("MAGICAL", (120, 90, 155)),
    ("CONIFEROUS", (55, 95, 80)),
    ("MOUNTAIN", (130, 135, 125)),
    ("SANDY", (225, 210, 150)),
    ("HILLS", (105, 130, 95)),
    ("FOREST", (70, 125, 70)),
    ("PLAINS", (135, 175, 105)),
]


def strip_json_comments(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_colormaps(mc_jar: Path | None):
    """Pull vanilla grass.png / foliage.png out of the Minecraft client jar.

    Grass and leaves are not painted with a biome's map colour -- that is a cartographic
    choice, unrelated to what the block looks like. The game derives their tint from the
    biome's temperature and rainfall through these two 256x256 colormaps. Using the real ones
    is the difference between a savanna reading olive and reading forest-green.
    """
    if not mc_jar or not mc_jar.exists():
        return None
    import io
    import zipfile

    from PIL import Image

    out = {}
    with zipfile.ZipFile(mc_jar) as z:
        for kind in ("grass", "foliage"):
            name = f"assets/minecraft/textures/colormap/{kind}.png"
            if name not in z.namelist():
                return None
            with z.open(name) as fh:
                out[kind] = np.asarray(Image.open(io.BytesIO(fh.read())).convert("RGB"))
    return out


def colormap_lookup(img, temperature: float, rainfall: float) -> list[int]:
    """The vanilla ColorizerGrass/ColorizerFoliage index, verbatim."""
    t = min(max(temperature, 0.0), 1.0)
    r = min(max(rainfall, 0.0), 1.0) * t
    i = int((1.0 - t) * 255.0)
    j = int((1.0 - r) * 255.0)
    return [int(v) for v in img[j, i]]


def build_palette(biomes_paths: list[Path], amidst_path: Path | None, mc_jar: Path | None = None) -> dict:
    # The biome list comes from the first sidecar; rwgRivers from the first one that has it.
    # A probe run only populates rwgRivers when the biome-region scan ran, so the sidecar
    # dropped next to a plain search report usually has it empty.
    cmaps = load_colormaps(mc_jar)
    data = json.loads(biomes_paths[0].read_text())
    biomes = data["biomes"]
    if not data.get("rwgRivers"):
        for extra in biomes_paths[1:]:
            rr = json.loads(extra.read_text()).get("rwgRivers")
            if rr:
                data["rwgRivers"] = rr
                log(f"palette: took rwgRivers ({len(rr)} entries) from {extra}")
                break

    amidst: dict[str, tuple[int, int, int]] = {}
    if amidst_path and amidst_path.exists():
        raw = json.loads(strip_json_comments(amidst_path.read_text()))
        for name, c in raw["colorMap"]:
            amidst[norm_name(name)] = (c["r"], c["g"], c["b"])

    # rwgRivers maps a base biome id to the river variants painted over it, so a variant with no
    # colour of its own can inherit the base's and shift toward water.
    river_base: dict[int, int] = {}
    for base, variants in (data.get("rwgRivers") or {}).items():
        for v in variants:
            river_base.setdefault(int(v), int(base))

    by_id = {b["id"]: b for b in biomes}
    pal: dict[str, dict] = {}

    def direct(b: dict) -> tuple[int, int, int] | None:
        return amidst.get(norm_name(b["name"]))

    def synth(b: dict) -> tuple[int, int, int]:
        types = set(b.get("types") or [])
        base = None
        for t, rgb in TYPE_COLOURS:
            if t in types:
                base = rgb
                break
        if base is None:
            # No usable type. Interpolate: cold biomes toward slate, hot toward ochre, and
            # damp ones toward green.
            t = max(0.0, min(1.0, (b.get("temperature", 0.5) + 0.5) / 2.5))
            rain = max(0.0, min(1.0, b.get("rainfall", 0.5)))
            base = (
                int(120 + 90 * t - 40 * rain),
                int(130 + 30 * rain),
                int(150 - 70 * t + 10 * rain),
            )
        # Nudge by temperature so variants that share a type stay distinguishable.
        if "OCEAN" not in types and "RIVER" not in types:
            warm = max(-1.0, min(1.0, (b.get("temperature", 0.5) - 0.8) / 1.2))
            base = tuple(int(max(0, min(255, c + warm * 18))) for c in base)
        return base  # type: ignore[return-value]

    for b in biomes:
        rgb = direct(b)
        src = "amidst"
        if rgb is None and b["id"] in river_base:
            parent = by_id.get(river_base[b["id"]])
            prgb = direct(parent) if parent else None
            if prgb:
                # Keep a trace of the host so a desert river still reads warmer than a taiga
                # one, but land mostly on the bank tone. Inheriting the host outright produces
                # saturated ribbons in the host's hue -- Temperate River's parent is pink --
                # which says nothing true about the riverbank.
                bank = dict(TYPE_COLOURS)["RIVER"]
                rgb = tuple(int(p * 0.35 + q * 0.65) for p, q in zip(prgb, bank))
                src = "rwg-river"
        if rgb is None:
            rgb = synth(b)
            src = "synth"
        e = {"name": b["name"], "rgb": list(rgb), "src": src}
        if cmaps:
            t, r = b.get("temperature", 0.5), b.get("rainfall", 0.5)
            e["grass"] = colormap_lookup(cmaps["grass"], t, r)
            e["foliage"] = colormap_lookup(cmaps["foliage"], t, r)
        pal[str(b["id"])] = e

    srcs = _counts(v["src"] for v in pal.values())
    log(f"palette: {len(pal)} biomes {srcs}{', vanilla grass/foliage tints' if cmaps else ''}")
    return {"biomes": pal, "sources": srcs, "tints": bool(cmaps)}


# --------------------------------------------------------------------------------------
# base map render
# --------------------------------------------------------------------------------------


def decode_report(report: dict) -> dict:
    """Pull the renderable arrays out of a search-mode report.

    `surf` is 512 hex chars = 256 bytes, row-major z*16+x, one absolute Y per column
    (WorldgenProbe.java:1876-1880). It is terrain only: vegetation is excluded and over water it
    is the seabed, so water has to be recovered separately.
    """
    import numpy as np

    chunks = (report.get("search") or {}).get("chunks") or {}
    if not chunks:
        raise SystemExit("report has no search.chunks -- was it run with PROBE_SEARCH=true?")

    keys = [tuple(int(v) for v in k.split(",")) for k in chunks]
    cx0 = min(k[0] for k in keys)
    cx1 = max(k[0] for k in keys)
    cz0 = min(k[1] for k in keys)
    cz1 = max(k[1] for k in keys)
    ncx, ncz = cx1 - cx0 + 1, cz1 - cz0 + 1
    w, h = ncx * 16, ncz * 16

    height = np.zeros((h, w), np.uint8)
    have = np.zeros((h, w), bool)
    water_lvl = np.zeros((ncz, ncx), np.int16)
    biome = np.full((ncz, ncx), -1, np.int32)
    bhave = np.zeros((ncz, ncx), bool)

    for key, c in chunks.items():
        cx, cz = (int(v) for v in key.split(","))
        ix, iz = cx - cx0, cz - cz0
        surf_hex = c.get("surf")
        if surf_hex:
            s = np.frombuffer(bytes.fromhex(surf_hex), np.uint8).reshape(16, 16)
            height[iz * 16 : iz * 16 + 16, ix * 16 : ix * 16 + 16] = s
            have[iz * 16 : iz * 16 + 16, ix * 16 : ix * 16 + 16] = s != SURF_VOID
            water_lvl[iz, ix] = _water_level(s, c.get("waterY"))
        bid = c.get("biomeId")
        if bid is not None:
            biome[iz, ix] = bid
            bhave[iz, ix] = True

    return {
        "height": height,
        "have": have,
        "water_lvl": water_lvl,
        "biome": biome,
        "bhave": bhave,
        "cx0": cx0,
        "cz0": cz0,
        "ncx": ncx,
        "ncz": ncz,
        "x0": cx0 * 16,
        "z0": cz0 * 16,
        "w": w,
        "h": h,
        "n_chunks": len(chunks),
    }


def _water_level(surf, water_y: dict | None) -> int:
    """Recover the open-water surface Y for one chunk.

    There is no per-block water field in any report format. But for a flat body of water,
    `waterY[y]` (the count of water blocks at that Y in the chunk) equals the number of columns
    whose terrain surface is below y. Cave water breaks that equality, which is exactly how we
    tell the two apart. Returns 0 when the chunk has no open water.
    """
    if not water_y:
        return 0
    import numpy as np

    flat = surf.reshape(-1)
    best = 0
    for y_s, count in water_y.items():
        y = int(y_s)
        if y <= 0:
            continue
        if int(np.count_nonzero(flat < y)) == count and count > 0 and y > best:
            best = y
    return best


def render_base(rep: dict, palette: dict, out_dir: Path, name: str) -> dict:
    """Write base-biome.png and base-topo.png. 1 px = 1 block, 1 chunk = 16 px."""
    import numpy as np
    from PIL import Image

    height = rep["height"].astype(np.float32)
    have = rep["have"]
    ncx, ncz, w, h = rep["ncx"], rep["ncz"], rep["w"], rep["h"]

    # --- water mask, per block, from the per-chunk water level
    lvl = np.repeat(np.repeat(rep["water_lvl"], 16, axis=0), 16, axis=1).astype(np.float32)
    water = have & (lvl > 0) & (height < lvl)
    depth = np.where(water, lvl - height, 0.0)

    # --- hillshade. Light from the north-west (top-left), so brightness rises with the
    # gradient: see the derivation in worldrender.colourise. Under water the seabed slope
    # would read as terrain relief, so flatten it there.
    shade_h = np.where(water, lvl, height)
    gz, gx = np.gradient(shade_h)
    shade = 1.0 + 0.16 * (gx + gz)
    shade = np.clip(shade, 0.55, 1.45)

    # --- biome tint. The report samples biome once per chunk, at the chunk centre column
    # (WorldgenProbe.java:1796-1797), so there is no per-block biome to draw. Upsampling
    # bilinearly puts each sample back at its true centre (+8, +8) and hides the 16 px blocking.
    pal = palette["biomes"]
    tint_small = np.zeros((ncz, ncx, 3), np.uint8)
    fallback = np.array([110, 110, 110], np.uint8)
    for iz in range(ncz):
        for ix in range(ncx):
            bid = rep["biome"][iz, ix]
            e = pal.get(str(int(bid))) if bid >= 0 else None
            tint_small[iz, ix] = e["rgb"] if e else fallback
    # Nearest-fill unsampled chunks first so their grey does not bleed into their neighbours.
    if not rep["bhave"].all():
        tint_small = _nearest_fill(tint_small, rep["bhave"])
    tint = np.asarray(
        Image.fromarray(tint_small).resize((w, h), Image.BILINEAR), np.float32
    )

    # --- compose
    rgb = tint * shade[..., None]
    if water.any():
        # Depth ramp: shallows keep some of the biome tint, deep water goes to open blue.
        t = np.clip(depth / 12.0, 0.0, 1.0)[..., None]
        deep = np.array([28, 58, 120], np.float32)
        shallow = np.array([70, 130, 190], np.float32)
        wcol = shallow * (1 - t) + deep * t
        rgb = np.where(water[..., None], wcol * (0.85 + 0.15 * shade[..., None]), rgb)
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    alpha = (have * 255).astype(np.uint8)
    cz0, cz1, cx0, cx1 = _crop_to_content(alpha)
    _save_png(rgb[cz0:cz1, cx0:cx1], alpha[cz0:cz1, cx0:cx1], out_dir / "base-biome.png")

    # --- topo: elevation ramp plus the same hillshade, so relief reads without biome colour
    lo, hi = _percentile_range(height[have])
    t = np.clip((height - lo) / max(1.0, hi - lo), 0, 1)
    topo = np.dstack(
        [
            40 + 200 * t,
            70 + 150 * t,
            90 + 60 * t,
        ]
    ).astype(np.float32)
    topo *= shade[..., None]
    if water.any():
        topo = np.where(water[..., None], np.array([35, 60, 110], np.float32), topo)
    # Contours every 16 blocks give a readable sense of steepness at a glance.
    band = (np.floor(height / 16.0) != np.floor(np.roll(height, 1, axis=0) / 16.0)) | (
        np.floor(height / 16.0) != np.floor(np.roll(height, 1, axis=1) / 16.0)
    )
    topo[band & ~water] *= 0.75
    _save_png(
        np.clip(topo, 0, 255).astype(np.uint8)[cz0:cz1, cx0:cx1],
        alpha[cz0:cz1, cx0:cx1],
        out_dir / "base-topo.png",
    )

    box = {
        "x0": rep["x0"] + cx0,
        "z0": rep["z0"] + cz0,
        "w": cx1 - cx0,
        "h": cz1 - cz0,
    }
    hr = [int(height[have].min()), int(height[have].max())]
    log(
        f"{name}: {w}x{h} px from {rep['n_chunks']} chunks, "
        f"origin ({rep['x0']},{rep['z0']}), y {hr}, {int(water.sum()):,} water px"
    )
    note = (
        f"Rendered from a radius-60 search probe: {rep['n_chunks']:,} chunks, y {hr[0]}-{hr[1]}. "
        f"Biome is sampled once per chunk, so colour is 16-block granular; height is per-block."
    )
    return [
        dict(key="biome", label="Biome", file="base-biome.png", note=note, **box),
        dict(
            key="topo",
            label="Topo",
            file="base-topo.png",
            note=note + " Contours every 16 blocks.",
            **box,
        ),
    ]


def _save_climate(cm: dict, path: Path, box) -> None:
    """Two-colour translucent overlay: amber for no-rain, teal for high humidity."""
    from PIL import Image

    z0, z1, x0, x1 = box
    nr, hu = cm["norain"][z0:z1, x0:x1], cm["humid"][z0:z1, x0:x1]
    rgba = np.zeros((z1 - z0, x1 - x0, 4), np.uint8)
    rgba[nr] = (255, 196, 82, 105)
    rgba[hu] = (96, 240, 190, 105)
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def render_blocks(region_dir: Path, level_dat: Path, palette: dict, biomes: list, out_dir: Path, name: str,
                  roofed: bool = False):
    """Render the true block surface from the world save the probe left behind.

    This is the closest thing to a JourneyMap capture that can be produced without a client:
    it reads the actual generated blocks, so it shows structures, roads, lakebeds and canopy
    rather than a per-chunk biome wash -- and unlike a JourneyMap capture it covers everything
    that was generated, not just where somebody walked.
    """
    import worldrender as W

    names = W.block_registry(level_dat)
    lookup = W.build_lookup(names, palette)
    # roofed: the Nether has a bedrock ceiling, so "topmost opaque block" is the roof and the
    # render comes out a flat slab. See worldrender.scan_world.
    scan = W.scan_world(region_dir, names, lookup, log=lambda m: None, roofed=roofed)
    rgb, alpha, _ = W.colourise(scan, lookup, palette)
    cov = W.coverage(scan, lookup)
    z0, z1, x0, x1 = _crop_to_content(alpha)
    _save_png(rgb[z0:z1, x0:x1], alpha[z0:z1, x0:x1], out_dir / "base-blocks.png")

    # Climate overlay, from the same per-block biome grid. Cropped to the same box as the
    # block render so the two line up when both are on.
    cm = W.climate_masks(scan, biomes)
    _save_climate(cm, out_dir / "climate.png", (z0, z1, x0, x1))
    st = cm["stats"]
    log(
        f"{name} climate: no-rain {st['norain_chunks_full']:,} full chunks "
        f"({st['norain_columns']:,} columns), humid {st['humid_chunks_full']:,} full chunks"
    )

    hs = scan["height"][scan["have"]]
    hr = [int(hs.min()), int(hs.max())] if hs.size else [0, 0]
    bx, bz = scan["x0"] + x0, scan["z0"] + z0
    log(
        f"{name} blocks: {x1 - x0}x{z1 - z0} px from {scan['n_chunks']:,} chunks, "
        f"origin ({bx},{bz}), {cov['known_pct']}% of surface from the palette"
    )
    if cov["top_unknown"]:
        log(f"  top unpalettised: {', '.join(n for n, _ in cov['top_unknown'][:5])}")
    bx, bz = scan["x0"] + x0, scan["z0"] + z0
    bw, bh = x1 - x0, z1 - z0
    return dict(
        key="blocks",
        label="Blocks",
        file="base-blocks.png",
        climate=dict(file="climate.png", x0=bx, z0=bz, w=bw, h=bh, **st),
        x0=bx,
        z0=bz,
        w=bw,
        h=bh,
        note=(
            f"True block surface, read from the world save the probe generated: "
            f"{scan['n_chunks']:,} chunks, {cov['known_pct']}% of visible blocks have a palette "
            f"colour, the rest fall back to their biome colour."
        ),
    )


def _crop_to_content(alpha):
    """Bounding box of the non-transparent pixels, as (z0, z1, x0, x1) slices.

    The renders are sized to whole 512-block region files, but generation only covers a
    radius-60 disc-ish patch inside that, so every image carries a transparent margin. Left in,
    that margin is part of the layer's bounds, so "fit the world to the window" fits the
    padding too and the visible map never reaches the edges. Cropping also takes a useful bite
    out of the file size.
    """
    rows = np.flatnonzero(alpha.any(axis=1))
    cols = np.flatnonzero(alpha.any(axis=0))
    if not rows.size or not cols.size:
        return 0, alpha.shape[0], 0, alpha.shape[1]
    return int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def _save_png(rgb, alpha, path: Path) -> None:
    """Write an 8-bit paletted PNG with one index reserved for the transparent gaps.

    These are continuous-tone shaded images, so truecolour PNG barely compresses (~96k unique
    colours, 3.3 MB). 255 colours plus dithering lands at a mean channel error under 2/255 --
    invisible against the hillshade -- for a third of the bytes.
    """
    import numpy as np
    from PIL import Image

    q = Image.fromarray(rgb, "RGB").quantize(
        colors=255, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG
    )
    idx = np.asarray(q).copy()
    idx[alpha == 0] = 255
    out = Image.fromarray(idx, "P")
    pal = q.getpalette()[: 255 * 3] + [0, 0, 0]
    out.putpalette(pal)
    out.save(path, optimize=True, transparency=255)


def _nearest_fill(arr, mask):
    """Fill masked-out chunk cells from their nearest sampled neighbour."""
    import numpy as np

    out = arr.copy()
    missing = np.argwhere(~mask)
    present = np.argwhere(mask)
    if not len(present) or not len(missing):
        return out
    for iz, ix in missing:
        d = np.abs(present[:, 0] - iz) + np.abs(present[:, 1] - ix)
        piz, pix = present[d.argmin()]
        out[iz, ix] = arr[piz, pix]
    return out


def _percentile_range(vals):
    import numpy as np

    if vals.size == 0:
        return 0.0, 1.0
    return float(np.percentile(vals, 1)), float(np.percentile(vals, 99))


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--pack", default="daily-707")
    ap.add_argument("--prefilter", type=Path)
    ap.add_argument("--loot-csv", type=Path)
    ap.add_argument("--veins-ow", type=Path)
    ap.add_argument("--veins-tf", type=Path)
    ap.add_argument("--veins-nether", type=Path)
    ap.add_argument("--ow-search", type=Path)
    ap.add_argument("--tf-search", type=Path)
    ap.add_argument("--nether-search", type=Path)
    ap.add_argument("--ow-region", type=Path, help="DIM0 region/ dir from the probe's World")
    ap.add_argument("--tf-region", type=Path, help="DIM7 region/ dir from the probe's World")
    ap.add_argument("--nether-region", type=Path, help="DIM-1 region/ dir from the probe's World")
    ap.add_argument(
        "--nether-portal-ratio",
        type=float,
        default=8.0,
        help="Overworld:Nether coordinate scale. Vanilla 8.0; GTNH exposes it as "
        "hodgepodge.cfg netherPortalRatio (range 0.125-64), so it is a per-pack value rather "
        "than a constant. Baked into meta so the viewer never has to guess it.",
    )
    ap.add_argument("--level-dat", type=Path, help="World/level.dat, for the block registry")
    ap.add_argument(
        "--biomes",
        type=Path,
        action="append",
        help="biomes.json sidecar for the palette; repeatable, first wins for the biome list "
        "and the first non-empty rwgRivers is used",
    )
    ap.add_argument("--amidst", type=Path, help="AmidstGTNH biome/default.json")
    ap.add_argument(
        "--mc-jar",
        type=Path,
        help="Minecraft 1.7.10 client jar, for the vanilla grass/foliage colormaps. Only the "
        "derived per-biome tints are written to the bundle, never the asset itself.",
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    out = args.out
    (out / "dim0").mkdir(parents=True, exist_ok=True)
    (out / "dim7").mkdir(parents=True, exist_ok=True)
    (out / "dim-1").mkdir(parents=True, exist_ok=True)

    meta: dict = {
        "seed": str(args.seed),  # JS numbers cannot hold a 64-bit seed exactly
        "pack": args.pack,
        "dims": {},
        "caveats": [],
    }

    rec = None
    if args.prefilter:
        rec = load_prefilter(args.prefilter, args.seed)
        pois = pois_from_prefilter(rec)
        _write(out / "dim0" / "pois.json", pois)
        meta["spawn"] = pois["spawn"]
        log(
            f"pois: {len(pois['villages'])} villages, {len(pois['dungeons'])} dungeons, "
            f"{len(pois['witchery'])} witchery cells, {len(pois['strongholds'])} strongholds, "
            f"{len(pois['squares'])} squares"
        )

    if args.loot_csv:
        loot = loot_from_csv(args.loot_csv)
        _write(out / "dim0" / "loot.json", loot)
        log(f"loot: {len(loot['chests'])} chests, {len(loot['items'])} distinct items")

    for dim, path in ((0, args.veins_ow), (7, args.veins_tf), (-1, args.veins_nether)):
        if not path:
            continue
        v = veins_from_csv(path)
        _write(out / f"dim{dim}" / "veins.json", v)
        log(f"veins dim{dim}: {len(v['veins'])} veins, {v['unstable']} route-unstable")
        if v["unstable"]:
            meta["caveats"].append(
                f"dim{dim}: {v['unstable']} of {len(v['veins'])} veins differed between a rows "
                f"walk and a spiral walk -- GT vein placement is not route-stable on this pack."
            )

    if args.tf_search and args.tf_search.exists():
        rep = json.loads(args.tf_search.read_text())
        tf = pois_from_tffeatures(rep)
        if tf["features"]:
            _write(out / "dim7" / "pois.json", tf)
            log(f"tf features: {len(tf['features'])}")

    # Base map render. Needs the palette, so it is built once and shared by both dimensions.
    searches = [(0, args.ow_search), (7, args.tf_search), (-1, args.nether_search)]
    regions = [(0, args.ow_region), (7, args.tf_region), (-1, args.nether_region)]
    if any(p and p.exists() for _, p in searches + regions):
        biomes_paths = list(args.biomes or [])
        biomes_paths.append(
            REPO.parent
            / "gtnh-determinism"
            / "results"
            / "2026-08-30-chest-loot-radius60"
            / "biomes.json"
        )
        biomes_paths = [p for p in biomes_paths if p.exists()]
        if not biomes_paths:
            raise SystemExit("no biomes.json found; pass --biomes")
        amidst_path = args.amidst or (REPO.parent / "all-gtnh" / AMIDST_REL)
        biomes_list = json.loads(biomes_paths[0].read_text())["biomes"]
        palette = build_palette(biomes_paths, amidst_path, args.mc_jar)
        _write(out / "palette.json", palette)

        # "Blocks" first: it is the most faithful layer, so it is the one the map opens on.
        for (dim, region), (_, search) in zip(regions, searches):
            d = meta["dims"].setdefault(str(dim), {})
            bases = d.setdefault("bases", [])
            if region and region.exists():
                if not args.level_dat or not args.level_dat.exists():
                    raise SystemExit("--level-dat is required alongside --ow-region/--tf-region")
                bases.append(
                    render_blocks(
                        region,
                        args.level_dat,
                        palette,
                        biomes_list,
                        out / f"dim{dim}",
                        f"dim{dim}",
                        roofed=(dim == -1),
                    )
                )
            if search and search.exists():
                rep = decode_report(json.loads(search.read_text()))
                bases.extend(render_base(rep, palette, out / f"dim{dim}", f"dim{dim}"))
                d["chunks"] = rep["n_chunks"]
            # The climate overlay rides along with the block render but is a layer of its own,
            # so lift it out of the base entry onto the dimension.
            for bb in bases:
                cl = bb.pop("climate", None)
                if cl:
                    d["climate"] = cl
            if bases:
                # One box covering every base, so fitBounds frames the whole world however
                # the layers happen to differ in extent.
                d["bounds"] = {
                    "x0": min(b["x0"] for b in bases),
                    "z0": min(b["z0"] for b in bases),
                    "w": max(b["x0"] + b["w"] for b in bases) - min(b["x0"] for b in bases),
                    "h": max(b["z0"] + b["h"] for b in bases) - min(b["z0"] for b in bases),
                }

    # How this dimension's coordinates relate to the overworld's. The viewer needs it to place
    # anything anchored to spawn: the overworld spawn sits at spawn/ratio in Nether space, and
    # using the overworld numbers directly puts the distance rings visibly off centre.
    #
    # setdefault, not a membership test: meta["dims"][d] is only created by the base-map block,
    # so a bundle built from vein data alone (no region, no search) would silently ship without
    # the scale and the rings would be wrong again. Gated on a Nether input actually being
    # passed, so an overworld-only bundle does not grow a phantom dimension.
    if args.veins_nether or args.nether_region or args.nether_search:
        meta["dims"].setdefault("-1", {})["coordScale"] = args.nether_portal_ratio

    _write(out / "meta.json", meta)
    log(f"bundle written to {out}")


def _dumps(obj, depth: int = 0) -> str:
    """Compact JSON, but one record per line.

    These files are committed, so they get read in diffs. Fully minified, a bundle is one
    enormous line and any change shows up as "the whole file changed"; pretty-printed, a vein
    sprawls over ten lines and triples the size. One entry per line is the useful middle: add
    a village and the diff is one line.
    """
    if isinstance(obj, dict):
        items = [f"{json.dumps(k)}:{_dumps(v, depth + 1)}" for k, v in obj.items()]
        return "{" + (",\n" if depth == 0 else ",").join(items) + "}"
    if isinstance(obj, list) and obj and isinstance(obj[0], (dict, list)) and depth <= 2:
        inner = ",\n".join(json.dumps(x, separators=(",", ":")) for x in obj)
        return "[\n" + inner + "\n]"
    return json.dumps(obj, separators=(",", ":"))


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(obj) + "\n")
    log(f"  {path.relative_to(REPO) if REPO in path.parents else path}  {path.stat().st_size:,} B")


if __name__ == "__main__":
    main()
