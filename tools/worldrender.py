#!/usr/bin/env python3
"""Render a top-down block map straight from the world save the probe leaves behind.

`run-probe.sh` deletes and regenerates `<server>/World` on every run, so after a radius-60 walk
the region files hold the real, fully populated world. Reading those gives per-block surface
material -- which the search report does not carry, it only has a height per column -- so this
produces a JourneyMap-grade image rather than a biome wash. No client, no player, no mod.

Region format is Anvil; chunk NBT is 1.7.10 plus two GTNH extensions:
  * `Biomes16v2` -- 512 bytes = 256 little-endian u16, per column. 16-bit because the pack has
    biome ids past 255. Vanilla's 8-bit `Biomes` is absent.
  * `Data1High` / `Data2` on each section -- extended block metadata. Not needed here; the low
    nibble in `Data` is enough for the handful of blocks whose colour depends on it.
"""

from __future__ import annotations

import gzip
import struct
import zlib
from pathlib import Path

import numpy as np

CHUNK_SIDE = 16
REGION_CHUNKS = 32
SEA_LEVEL = 62

# ---------------------------------------------------------------------------------------
# minimal NBT
# ---------------------------------------------------------------------------------------

_FIXED = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}


def _str(b, i):
    n = struct.unpack_from(">H", b, i)[0]
    i += 2
    return b[i : i + n].decode("utf-8", "replace"), i + n


def _skip(b, i, t):
    """Advance past a tag without building Python objects.

    Chunks carry Entities, TileEntities and TileTicks lists that dwarf the block data and are
    irrelevant here. Walking them with the full parser costs more than everything else put
    together, so unwanted subtrees get skipped structurally instead.
    """
    if t in _FIXED:
        return i + _FIXED[t]
    if t == 7:
        return i + 4 + struct.unpack_from(">i", b, i)[0]
    if t == 8:
        return i + 2 + struct.unpack_from(">H", b, i)[0]
    if t == 11:
        return i + 4 + 4 * struct.unpack_from(">i", b, i)[0]
    if t == 9:
        et = b[i]
        n = struct.unpack_from(">i", b, i + 1)[0]
        i += 5
        if et in _FIXED:
            return i + n * _FIXED[et]
        for _ in range(n):
            i = _skip(b, i, et)
        return i
    if t == 10:
        while True:
            tt = b[i]
            i += 1
            if tt == 0:
                return i
            i += 2 + struct.unpack_from(">H", b, i)[0]
            i = _skip(b, i, tt)
    raise ValueError(f"tag {t}")


def _parse(b, i, t, want=None):
    if t == 1:
        return b[i], i + 1
    if t in (2, 3, 4, 5, 6):
        fmt = {2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}[t]
        return struct.unpack_from(fmt, b, i)[0], i + _FIXED[t]
    if t == 7:
        n = struct.unpack_from(">i", b, i)[0]
        i += 4
        return b[i : i + n], i + n
    if t == 8:
        return _str(b, i)
    if t == 9:
        et = b[i]
        n = struct.unpack_from(">i", b, i + 1)[0]
        i += 5
        out = []
        for _ in range(n):
            v, i = _parse(b, i, et)
            out.append(v)
        return out, i
    if t == 10:
        out = {}
        while True:
            tt = b[i]
            i += 1
            if tt == 0:
                return out, i
            k, i = _str(b, i)
            if want is not None and k not in want:
                i = _skip(b, i, tt)
            else:
                out[k], i = _parse(b, i, tt)
        return out, i
    if t == 11:
        n = struct.unpack_from(">i", b, i)[0]
        i += 4
        return list(struct.unpack_from(f">{n}i", b, i)), i + 4 * n
    raise ValueError(f"tag {t}")


def nbt_load(raw: bytes, want=None):
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif raw[0] == 0x78:
        raw = zlib.decompress(raw)
    t = raw[0]
    _, i = _str(raw, 1)
    return _parse(raw, i, t, want)[0]


# Only these keys are materialised; everything else in the chunk is skipped structurally.
CHUNK_WANT = {"Level", "Sections", "Biomes16v2", "xPos", "zPos", "Blocks", "Add", "Data", "Y"}


def region_chunks(path: Path):
    """Yield each stored chunk's Level compound from one .mca file."""
    d = path.read_bytes()
    for i in range(REGION_CHUNKS * REGION_CHUNKS):
        off = struct.unpack(">I", b"\x00" + d[i * 4 : i * 4 + 3])[0]
        if not off:
            continue
        o = off * 4096
        ln = struct.unpack(">I", d[o : o + 4])[0]
        comp = d[o + 4]
        raw = d[o + 5 : o + 5 + ln - 1]
        raw = zlib.decompress(raw) if comp == 2 else gzip.decompress(raw)
        yield nbt_load(raw, CHUNK_WANT)["Level"]


def block_registry(level_dat: Path) -> dict[int, str]:
    """id -> registry name, from the FML ItemData table. Blocks are prefixed \\x01."""
    d = nbt_load(level_dat.read_bytes())
    return {e["V"]: e["K"][1:] for e in d["FML"]["ItemData"] if e["K"][:1] == "\x01"}


# ---------------------------------------------------------------------------------------
# block palette
# ---------------------------------------------------------------------------------------

# Blocks that are not the surface: air, vegetation you see through or past, and the odd
# decoration. The renderer walks down past these, the way a map view shows the ground under
# grass rather than the grass itself. Leaves are deliberately NOT here -- a forest canopy is
# what makes a forest legible from above.
# A biome-tinted block is not drawn in the tint colour. The game multiplies its greyscale
# texture by the tint, and those textures are dark: grass_top averages 147/255 and the leaf
# textures 135/255. Skipping that step makes every forest and meadow about twice as bright as
# it should be, which was the single largest error in this renderer against real JourneyMap
# captures. Measured from assets/minecraft/textures/blocks/{grass_top,leaves_oak}.png.
GRASS_TEXTURE = 147 / 255
FOLIAGE_TEXTURE = 135 / 255

# Likewise water: this is water_still.png's own colour, not an invented blue.
WATER_TEXTURE = (47, 67, 244)

SKIP = {
    "minecraft:air",
    "minecraft:tallgrass",
    "minecraft:double_plant",
    "minecraft:red_flower",
    "minecraft:yellow_flower",
    "minecraft:deadbush",
    "minecraft:reeds",
    "minecraft:torch",
    "minecraft:snow_layer",
    "minecraft:brown_mushroom",
    "minecraft:red_mushroom",
    "minecraft:vine",
    "minecraft:fire",
    "minecraft:web",
    "TwilightForest:tile.TFPlant",
    "TwilightForest:tile.TFTorchberries",
    "Thaumcraft:blockCustomPlant",
    "Natura:florasapling",
    "harvestcraft:pamCrop",
    "etfuturum:lily_of_the_valley",
    "etfuturum:cornflower",
    "witchery:plant",
    # BoP ground cover is the single most common surface block in this world (355k columns);
    # rendering it would paint most of the overworld one flat green.
    "BiomesOPlenty:foliage",
    "BiomesOPlenty:plants",
    # Lily pads hide the water they float on, and TF's wispy clouds sit above the canopy and
    # would white out whole regions of the forest.
    "minecraft:waterlily",
    "TwilightForest:tile.HugeLilyPad",
    "TwilightForest:tile.WispyCloud",
    "TwilightForest:tile.HugeGrassBlock",
}

# name -> (r, g, b) or (r, g, b, tint). tint "g" takes the biome colour (grass, ground cover),
# "f" takes a darkened biome colour (leaves/canopy). Everything unlisted falls back to the
# biome colour, so an unknown modded block still reads as terrain rather than as a hole.
BLOCKS: dict[str, tuple] = {
    # --- ground
    "minecraft:grass": (0, 0, 0, "g"),
    "minecraft:mycelium": (126, 108, 121),
    "minecraft:dirt": (134, 96, 67),
    "minecraft:farmland": (110, 78, 52),
    "minecraft:sand": (219, 207, 163),
    "minecraft:gravel": (136, 130, 127),
    "minecraft:clay": (160, 166, 179),
    "minecraft:stone": (127, 127, 127),
    "minecraft:cobblestone": (122, 122, 122),
    "minecraft:bedrock": (85, 85, 85),
    "minecraft:sandstone": (216, 203, 155),
    "minecraft:hardened_clay": (150, 92, 66),
    "minecraft:obsidian": (21, 18, 30),
    "minecraft:netherrack": (111, 54, 52),
    "minecraft:soul_sand": (84, 64, 51),
    "minecraft:end_stone": (221, 223, 165),
    "minecraft:mossy_cobblestone": (110, 118, 100),
    "minecraft:stonebrick": (122, 122, 122),
    "minecraft:coal_ore": (105, 105, 105),
    "minecraft:iron_ore": (150, 134, 120),
    # --- liquid / frozen
    "minecraft:water": (56, 92, 168),
    "minecraft:flowing_water": (56, 92, 168),
    "minecraft:ice": (145, 183, 235),
    "minecraft:packed_ice": (140, 178, 232),
    "minecraft:snow": (238, 245, 250),
    "minecraft:lava": (207, 92, 26),
    "minecraft:flowing_lava": (207, 92, 26),
    # --- wood / canopy
    "minecraft:leaves": (0, 0, 0, "f"),
    "minecraft:leaves2": (0, 0, 0, "f"),
    "minecraft:log": (105, 84, 51),
    "minecraft:log2": (98, 78, 47),
    "minecraft:planks": (161, 131, 82),
    "minecraft:cactus": (85, 127, 45),
    "minecraft:pumpkin": (196, 133, 32),
    "minecraft:melon_block": (110, 148, 40),
    "minecraft:brown_mushroom_block": (140, 108, 88),
    "minecraft:red_mushroom_block": (183, 62, 56),
    # --- structures you want to spot from above
    "minecraft:stone_slab": (140, 140, 140),
    "minecraft:stone_brick_stairs": (128, 128, 128),
    "minecraft:cobblestone_wall": (118, 118, 118),
    "minecraft:mob_spawner": (44, 54, 64),
    "minecraft:chest": (176, 129, 56),
    "minecraft:glass": (208, 226, 232),
    "minecraft:glowstone": (219, 191, 122),
    "minecraft:wool": (222, 222, 222),
    "minecraft:gravel_road": (130, 124, 120),
    # --- Twilight Forest
    "TwilightForest:tile.TFLeaves": (0, 0, 0, "f"),
    "TwilightForest:tile.TFLeaves2": (0, 0, 0, "f"),
    "TwilightForest:tile.TFLeaves3": (0, 0, 0, "f"),
    "TwilightForest:tile.TFHedge": (52, 92, 44),
    "TwilightForest:tile.TFLog": (98, 76, 46),
    "TwilightForest:tile.TFMagicLog": (120, 92, 130),
    "TwilightForest:tile.TFRoots": (98, 76, 46),
    "TwilightForest:tile.TFTowerStone": (128, 122, 116),
    "TwilightForest:tile.TFMazestone": (132, 132, 140),
    "TwilightForest:tile.TFUnderBrick": (96, 100, 96),
    "TwilightForest:tile.TFNagastoneEtched": (118, 122, 110),
    "TwilightForest:tile.TFNagastoneEtchedMossy": (100, 116, 92),
    "TwilightForest:tile.TFNagastoneEtchedWeathered": (128, 128, 120),
    "TwilightForest:tile.TFNagastoneStairsLeft": (118, 122, 110),
    "TwilightForest:tile.TFNagastoneStairsRight": (118, 122, 110),
    "TwilightForest:tile.TFNagastoneStairsMossyLeft": (100, 116, 92),
    "TwilightForest:tile.TFNagastoneStairsMossyRight": (100, 116, 92),
    "TwilightForest:tile.TFNagastoneStairsWeatheredLeft": (128, 128, 120),
    "TwilightForest:tile.TFNagastoneStairsWeatheredRight": (128, 128, 120),
    "TwilightForest:tile.TFAuroraBrick": (110, 168, 200),
    "TwilightForest:tile.TFDeadrock": (86, 86, 90),
    "TwilightForest:tile.TFTrollSteinn": (96, 108, 118),
    "TwilightForest:tile.TFCastleBrick": (196, 196, 190),
    "TwilightForest:tile.TFThorns": (86, 70, 52),
    # --- other mods that reach the surface
    "Thaumcraft:blockMagicalLeaves": (0, 0, 0, "f"),
    "Thaumcraft:blockMagicalLog": (104, 84, 60),
    "Thaumcraft:blockTaint": (150, 108, 160),
    "IC2:blockRubLeaves": (0, 0, 0, "f"),
    "IC2:blockRubWood": (98, 80, 50),
    "Natura:tree": (98, 80, 50),
    "Natura:floraLeaves": (0, 0, 0, "f"),
    "Natura:floraLeavesNoColor": (0, 0, 0, "f"),
    "BiomesOPlenty:leaves1": (0, 0, 0, "f"),
    "BiomesOPlenty:leaves2": (0, 0, 0, "f"),
    "BiomesOPlenty:leaves3": (0, 0, 0, "f"),
    "BiomesOPlenty:leaves4": (0, 0, 0, "f"),
    "BiomesOPlenty:colorizedLeaves1": (0, 0, 0, "f"),
    "BiomesOPlenty:colorizedLeaves2": (0, 0, 0, "f"),
    "BiomesOPlenty:hardDirt": (120, 88, 62),
    "BiomesOPlenty:hardSand": (206, 190, 148),
    "BiomesOPlenty:mud": (76, 60, 48),
    "BiomesOPlenty:driedDirt": (150, 116, 82),
    "BiomesOPlenty:ash": (70, 68, 68),
    "BiomesOPlenty:overgrownNetherrack": (96, 108, 62),
    "BiomesOPlenty:logs1": (105, 84, 51),
    "BiomesOPlenty:logs2": (105, 84, 51),
    "BiomesOPlenty:logs3": (105, 84, 51),
    "BiomesOPlenty:logs4": (105, 84, 51),
    "TwilightForest:tile.DarkLeaves": (0, 0, 0, "f"),
    "TwilightForest:tile.GiantLeaves": (0, 0, 0, "f"),
    "TwilightForest:tile.CastleBrick": (196, 196, 190),
    "TwilightForest:tile.GiantCobble": (120, 120, 120),
    "etfuturum:coarse_dirt": (122, 88, 62),
    "witchery:leaves": (0, 0, 0, "f"),
    "erebus:leaves": (0, 0, 0, "f"),
    # Metadata-coloured blocks still need a base entry or they count as unpalettised; the
    # per-meta table below overrides the value.
    "minecraft:stained_hardened_clay": (150, 92, 66),
    "minecraft:wool": (222, 222, 222),
    "minecraft:wooden_slab": (161, 131, 82),
    "minecraft:fence": (150, 122, 76),
    "minecraft:birch_stairs": (192, 175, 121),
    "minecraft:oak_stairs": (161, 131, 82),
    "minecraft:spruce_stairs": (114, 84, 48),
}

# Blocks whose colour depends on the low metadata nibble. Ignoring metadata is a whole class
# of error, not a missing table row: over half the sand in this world is meta 1, Et Futurum's
# backport of 1.8 red sand, which shares the block id with ordinary sand. Rendering it pale
# yellow turns an orange-red desert into a beach.
#
# The `f` tint marker works here too, for leaf variants that are biome-tinted.
BLOCK_META: dict[str, dict[int, tuple]] = {
    "minecraft:sand": {0: (219, 207, 163), 1: (171, 89, 34)},
    "etfuturum:red_sand": {0: (171, 89, 34)},
    "minecraft:stained_hardened_clay": {
        0: (209, 178, 161), 1: (161, 83, 37), 2: (149, 88, 108), 3: (113, 108, 137),
        4: (186, 133, 35), 5: (103, 117, 52), 6: (161, 78, 78), 7: (57, 42, 35),
        8: (135, 107, 98), 9: (87, 91, 91), 10: (118, 70, 86), 11: (74, 59, 91),
        12: (76, 50, 35), 13: (76, 82, 42), 14: (143, 61, 46), 15: (37, 22, 16),
    },
    # leaves2 is acacia (0) and dark oak (1); bit 3 is the no-decay flag, so 8/9 are the same
    # two woods. Acacia is not foliage-tinted the way the others are -- it keeps its own dry
    # olive -- which is why one colour for the whole block id reads wrong in savanna.
    "minecraft:leaves2": {
        0: (0, 0, 0, "f"), 8: (0, 0, 0, "f"),
        1: (104, 100, 66), 9: (104, 100, 66),
    },
    "minecraft:log": {
        0: (105, 84, 51), 1: (60, 41, 22), 2: (200, 195, 180), 3: (105, 84, 51),
    },
    "minecraft:wool": {
        0: (233, 236, 236), 1: (240, 118, 19), 2: (189, 68, 179), 3: (58, 175, 217),
        4: (248, 198, 39), 5: (112, 185, 25), 6: (237, 141, 172), 7: (62, 68, 71),
        8: (142, 142, 134), 9: (21, 137, 145), 10: (121, 42, 172), 11: (53, 57, 157),
        12: (114, 71, 40), 13: (84, 109, 27), 14: (161, 39, 34), 15: (20, 21, 25),
    },
}


def build_lookup(names: dict[int, str], palette: dict) -> tuple:
    """Compile the palette into flat arrays indexed by block id (1.7.10 ids fit in 12 bits)."""
    n = 4096
    rgb = np.zeros((n, 3), np.float32)
    tint = np.zeros(n, np.uint8)  # 0 none, 1 grass, 2 foliage
    known = np.zeros(n, bool)
    skip = np.zeros(n, bool)
    water = np.zeros(n, bool)
    meta_tables: dict[int, dict[int, tuple]] = {}

    for bid, name in names.items():
        if not 0 <= bid < n:
            continue
        if name in SKIP:
            skip[bid] = True
            continue
        if name in ("minecraft:water", "minecraft:flowing_water"):
            water[bid] = True
        e = BLOCKS.get(name)
        if e is None:
            continue
        known[bid] = True
        if len(e) == 4:
            tint[bid] = 1 if e[3] == "g" else 2
        else:
            rgb[bid] = e[:3]
        if name in BLOCK_META:
            meta_tables[bid] = BLOCK_META[name]
    skip[0] = True  # air
    return rgb, tint, known, skip, water, meta_tables


# ---------------------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------------------


def _sections(level) -> np.ndarray | None:
    """Decode one chunk's sections into (256, 16, 16) block ids, indexed [y][z][x]."""
    secs = level.get("Sections")
    if not secs:
        return None
    out = np.zeros((256, CHUNK_SIDE, CHUNK_SIDE), np.uint16)
    for s in secs:
        y = s["Y"] * 16
        if not 0 <= y < 256:
            continue
        b = np.frombuffer(s["Blocks"], np.uint8).astype(np.uint16).reshape(16, 16, 16)
        add = s.get("Add")
        if add:
            a = np.frombuffer(add, np.uint8)
            hi = np.empty(4096, np.uint16)
            hi[0::2] = a & 0x0F
            hi[1::2] = a >> 4
            b = b | (hi.reshape(16, 16, 16) << 8)
        out[y : y + 16] = b
    return out


def _meta(level) -> np.ndarray:
    out = np.zeros((256, CHUNK_SIDE, CHUNK_SIDE), np.uint8)
    for s in level.get("Sections") or []:
        y = s["Y"] * 16
        if not 0 <= y < 256 or "Data" not in s:
            continue
        a = np.frombuffer(s["Data"], np.uint8)
        d = np.empty(4096, np.uint8)
        d[0::2] = a & 0x0F
        d[1::2] = a >> 4
        out[y : y + 16] = d.reshape(16, 16, 16)
    return out


def scan_world(region_dir: Path, names: dict[int, str], lookup, log=print) -> dict:
    """Walk every region file and build per-block surface, height, water and biome grids."""
    rgb_tbl, tint_tbl, known_tbl, skip_tbl, water_tbl, meta_tables = lookup

    files = sorted(region_dir.glob("r.*.mca"))
    if not files:
        raise SystemExit(f"no region files in {region_dir}")
    coords = []
    for f in files:
        _, rx, rz, _ = f.name.split(".")
        coords.append((int(rx), int(rz)))
    rx0, rx1 = min(c[0] for c in coords), max(c[0] for c in coords)
    rz0, rz1 = min(c[1] for c in coords), max(c[1] for c in coords)
    cx0, cz0 = rx0 * REGION_CHUNKS, rz0 * REGION_CHUNKS
    ncx = (rx1 - rx0 + 1) * REGION_CHUNKS
    ncz = (rz1 - rz0 + 1) * REGION_CHUNKS
    w, h = ncx * CHUNK_SIDE, ncz * CHUNK_SIDE

    surf_id = np.zeros((h, w), np.uint16)
    surf_meta = np.zeros((h, w), np.uint8)
    height = np.zeros((h, w), np.uint8)
    water_top = np.zeros((h, w), np.uint8)
    biome = np.zeros((h, w), np.uint16)
    have = np.zeros((h, w), bool)

    n_chunks = 0
    for f, (rx, rz) in zip(files, coords):
        for level in region_chunks(f):
            blocks = _sections(level)
            if blocks is None:
                continue
            cx, cz = level["xPos"], level["zPos"]
            ix, iz = (cx - cx0) * CHUNK_SIDE, (cz - cz0) * CHUNK_SIDE
            if not (0 <= ix < w and 0 <= iz < h):
                continue

            # Topmost water, then topmost non-skipped block at or below it. Doing water first
            # means a lake bed is recorded as the surface material with the water depth on top,
            # which is what makes shallows read differently from open sea.
            is_water = water_tbl[blocks]
            wcol = is_water.any(axis=0)
            wtop = np.where(wcol, 255 - np.argmax(is_water[::-1], axis=0), 0)

            opaque = ~skip_tbl[blocks] & ~is_water
            ocol = opaque.any(axis=0)
            otop = np.where(ocol, 255 - np.argmax(opaque[::-1], axis=0), 0)

            zz, xx = np.meshgrid(np.arange(16), np.arange(16), indexing="ij")
            ids = blocks[otop, zz, xx]
            ids[~ocol] = 0

            metas = _meta(level)[otop, zz, xx] if meta_tables else np.zeros((16, 16), np.uint8)

            surf_id[iz : iz + 16, ix : ix + 16] = ids
            surf_meta[iz : iz + 16, ix : ix + 16] = metas
            height[iz : iz + 16, ix : ix + 16] = otop
            water_top[iz : iz + 16, ix : ix + 16] = wtop
            have[iz : iz + 16, ix : ix + 16] = ocol | wcol

            b16 = level.get("Biomes16v2")
            if b16:
                biome[iz : iz + 16, ix : ix + 16] = np.frombuffer(b16, "<u2").reshape(16, 16)

            n_chunks += 1
        log(f"  {f.name}: {n_chunks} chunks so far")

    return {
        "surf_id": surf_id,
        "surf_meta": surf_meta,
        "height": height,
        "water_top": water_top,
        "biome": biome,
        "have": have,
        "x0": cx0 * CHUNK_SIDE,
        "z0": cz0 * CHUNK_SIDE,
        "w": w,
        "h": h,
        "n_chunks": n_chunks,
        "names": names,
    }


def colourise(scan: dict, lookup, biome_palette: dict) -> tuple:
    """Turn the scan into an RGB image plus an alpha mask."""
    rgb_tbl, tint_tbl, known_tbl, skip_tbl, water_tbl, meta_tables = lookup

    ids = scan["surf_id"]
    have = scan["have"]
    height = scan["height"].astype(np.float32)
    wtop = scan["water_top"].astype(np.float32)
    # Water counts only where it sits ON the terrain. `water_top` is the highest water block
    # anywhere in the column, which in 180k columns of this world is a flooded cave or dungeon
    # room far below the ground -- testing `> 0` painted those as open water and drew the
    # underground straight through the surface. This is a surface map: if the ground is above
    # the water, you are looking at ground.
    water = wtop > height

    # Three colours per biome. `rgb` is the cartographic map colour, used only as a fallback
    # for blocks with no palette entry. `grass` and `foliage` are the real vanilla tints,
    # derived from the biome's temperature and rainfall through the game's own colormaps --
    # that is what the game actually paints grass and leaves with, and it is a different thing
    # from the map colour.
    maxb = int(scan["biome"].max()) + 1
    bpal = np.full((max(maxb, 1), 3), 110, np.float32)
    gpal = np.full((max(maxb, 1), 3), (120, 160, 90), np.float32)
    fpal = np.full((max(maxb, 1), 3), (90, 140, 60), np.float32)
    for k, v in biome_palette.get("biomes", {}).items():
        i = int(k)
        if i >= maxb:
            continue
        bpal[i] = v["rgb"]
        gpal[i] = v.get("grass", v["rgb"])
        fpal[i] = v.get("foliage", v.get("grass", v["rgb"]))
    b = scan["biome"]
    bcol = bpal[b]
    grass = gpal[b] * GRASS_TEXTURE
    fol = fpal[b] * FOLIAGE_TEXTURE

    col = rgb_tbl[ids].copy()
    t = tint_tbl[ids]
    col[t == 1] = grass[t == 1]
    col[t == 2] = fol[t == 2]
    unknown = ~known_tbl[ids] & have
    col[unknown] = bcol[unknown] * 0.92        # unlisted modded block: fall back to the biome

    # Metadata overrides last, so a per-meta entry beats both the block's base colour and any
    # tint flag it inherited.
    metas = scan["surf_meta"]
    for bid, table in meta_tables.items():
        m = ids == bid
        if not m.any():
            continue
        for mv, rgbv in table.items():
            sel = m & (metas == mv)
            if not sel.any():
                continue
            if len(rgbv) == 4:
                col[sel] = (fol if rgbv[3] == "f" else grass)[sel]
            else:
                col[sel] = rgbv

    # Relief from the visible surface: the water plane where there is water, the ground
    # otherwise, so a lake reads flat instead of showing its bed's contours.
    relief = np.where(water, wtop, height)
    gz, gx = np.gradient(relief)
    # Light from the north-west, i.e. the top-left of the screen. For a surface normal
    # (-dh/dx, -dh/dz, 1) and a light vector (-1, -1, 1), the dot product is 1 + dh/dx + dh/dz,
    # so brightness rises with the gradient. Getting this sign backwards lights the scene from
    # the south-east, and because people read shading as "lit from above", every raised thing
    # -- trees, buildings, slime islands -- then looks like a pit. Regressing JourneyMap's own
    # luminance against this terrain gives +18.0*dh/dx +17.5*dh/dz on a base of 94.8: the same
    # direction, and a relative gain of 0.19 against the 0.18 used here.
    shade = np.clip(1.0 + 0.18 * (gx + gz), 0.5, 1.5)
    col *= shade[..., None]

    if water.any():
        depth = wtop - height  # positive by construction now
        f = np.clip(depth / 10.0, 0.0, 1.0)[..., None]
        base = np.array(WATER_TEXTURE, np.float32)
        # Deeper water reads darker rather than a different hue: it is the same water with
        # less light coming back out of it.
        wcol = base * (0.82 - 0.30 * f)
        # Keep a little of the bed showing through the shallows.
        wcol = wcol * (0.75 + 0.25 * f) + col * (0.25 - 0.25 * f)
        col = np.where(water[..., None], wcol * (0.9 + 0.1 * shade[..., None]), col)

    return np.clip(col, 0, 255).astype(np.uint8), (have * 255).astype(np.uint8), unknown


def climate_masks(scan: dict, biomes: list[dict]) -> dict:
    """Per-block no-rain and high-humidity masks, and the honest chunk counts.

    The prefilter reports the largest *inscribed square* in which every column is no-rain,
    which is a far smaller number than the no-rain area: it is axis-aligned, it is a square
    rather than a rectangle, and a single River Oasis column (which rains) cutting through a
    desert splits it. This computes the actual region so the square can be seen in context.
    """
    norain_ids = {b["id"] for b in biomes if not b.get("rainEnabled", True)}
    # hum 14 is CropsNH's HIGH_HUMIDITY_BONUS -- the "humid" the prefilter pairs with a dry
    # square, not merely damp.
    humid_ids = {b["id"] for b in biomes if b.get("hum", 0) >= 14}

    maxb = int(scan["biome"].max()) + 1
    nr = np.zeros(max(maxb, 1), bool)
    hu = np.zeros(max(maxb, 1), bool)
    for i in norain_ids:
        if i < maxb:
            nr[i] = True
    for i in humid_ids:
        if i < maxb:
            hu[i] = True

    b = scan["biome"]
    have = scan["have"]
    norain = nr[b] & have
    humid = hu[b] & have

    def chunk_all(mask):
        h, w = mask.shape
        c = mask[: h // 16 * 16, : w // 16 * 16].reshape(h // 16, 16, w // 16, 16)
        return c.all(axis=(1, 3))

    nrc, huc = chunk_all(norain), chunk_all(humid)
    return {
        "norain": norain,
        "humid": humid,
        "stats": {
            "norain_columns": int(norain.sum()),
            "humid_columns": int(humid.sum()),
            "norain_chunks_full": int(nrc.sum()),
            "humid_chunks_full": int(huc.sum()),
            "total_columns": int(have.sum()),
        },
    }


def coverage(scan: dict, lookup) -> dict:
    """What fraction of visible surface came from a real palette entry vs the biome fallback."""
    _, _, known_tbl, _, _, _ = lookup
    have = scan["have"]
    ids = scan["surf_id"]
    tot = int(have.sum())
    if not tot:
        return {"columns": 0, "known_pct": 0.0, "top_unknown": []}
    unk = ~known_tbl[ids] & have
    vals, counts = np.unique(ids[unk], return_counts=True)
    order = np.argsort(-counts)[:10]
    names = scan["names"]
    return {
        "columns": tot,
        "known_pct": round(100.0 * (tot - int(unk.sum())) / tot, 2),
        "top_unknown": [
            [names.get(int(vals[i]), f"#{int(vals[i])}"), int(counts[i])] for i in order
        ],
    }
