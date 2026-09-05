"""Item-value chest scoring, shared by the Loot score and Baseline rate tabs.

Port of gtnh-determinism/seedsearch/loot-score.py, which is the canonical CLI ranker — keep the two
in sync when the scoring changes, the same way PF_CRITERIA mirrors coke-rank.py. Vendored rather
than imported because the sibling repo is a convenience on dev machines, not a dependency, and
because its filename contains a hyphen.

Scoring, per seed: sum quantity per item across every chest inside the window, then
`score = sum(value * min(quantity, limit))`. A blank limit means uncapped.

Two chest sources are supported and normalised to the same shape:
  stage-0 prefilter JSONL — village_starts[].chests[].chest and dungeons[].chests[]
  full-generation corpus  — the browser's parsed `chests` list
"""
import json
import math
import re
import tarfile
from collections import Counter
from pathlib import Path

SECTION = re.compile("\xa7.")


def norm(name):
    """Display name to match key. Section-sign colour codes appear on both sides of the join."""
    return SECTION.sub("", name or "").strip().casefold()


def clean(name):
    return SECTION.sub("", name or "")


def parse_value_rows(rows):
    """[{Item, Value, Limit, Min}, …] -> (values, limits, mins, display, problems).

    Values may be floats: a rarity-normalised table spans eight orders of magnitude and no integer
    scale holds both Redstone and Alumite Large Plate. Duplicate items keep the highest value, and
    the collision is reported rather than silently resolved.

    `Limit` caps how much of an item counts. `Min` is the opposite and is a *filter*, not a score
    term: a seed holding fewer than Min of that item is dropped from the ranking entirely. Checked
    against the raw quantity, not the capped one — the question is what the seed has, not what the
    cap lets you count.
    """
    values, limits, mins, display, problems = {}, {}, {}, {}, []
    seen = {}

    def cell(v):
        """Editor cells arrive as float NaN for blanks, CSV cells as ''. Both mean 'unset'."""
        if v is None:
            return ""
        s = str(v).strip()
        return "" if s.lower() in ("", "nan", "none", "<na>") else s

    for row in rows:
        item = cell(row.get("Item"))
        if not item:
            continue
        raw = cell(row.get("Value"))
        try:
            value = float(raw)
        except ValueError:
            if raw:
                problems.append(f"{clean(item)}: value {raw!r} is not a number, row skipped")
            continue
        raw_limit = cell(row.get("Limit"))
        try:
            limit = int(float(raw_limit)) if raw_limit else None
        except ValueError:
            problems.append(f"{clean(item)}: limit {raw_limit!r} is not a number, "
                            "treated as uncapped")
            limit = None
        raw_min = cell(row.get("Min"))
        try:
            minimum = int(float(raw_min)) if raw_min else None
        except ValueError:
            problems.append(f"{clean(item)}: minimum {raw_min!r} is not a number, ignored")
            minimum = None
        if minimum is not None and minimum <= 0:
            minimum = None
        if minimum is not None and limit is not None and minimum > limit:
            problems.append(f"{clean(item)}: minimum {minimum} exceeds limit {limit} — the "
                            "requirement still applies to the raw quantity, but only "
                            f"{limit} can ever score")
        key = norm(item)
        if key in seen and seen[key] != value:
            problems.append(f"{clean(item)}: listed twice ({seen[key]:g} and {value:g}), "
                            f"keeping {max(seen[key], value):g}")
            if value < seen[key]:
                continue
        seen[key] = value
        values[key] = value
        limits[key] = limit
        mins[key] = minimum
        display[key] = clean(item)
    return values, limits, mins, display, problems


def unmet_minimums(qty, mins):
    """-> [(key, required, found)] for every minimum this seed fails. Empty means it qualifies."""
    out = []
    for key, need in mins.items():
        if need:
            got = qty.get(key, 0)
            if got < need:
                out.append((key, need, got))
    return out


def value_rows_from_csv(text):
    """Tolerant CSV read. The supplied tables are spreadsheet exports carrying trailing empty
    columns and an inline SETTINGS block, so only Item/Value/Limit are read and the rest ignored."""
    import csv
    import io
    return list(csv.DictReader(io.StringIO(text)))


class Chest:
    __slots__ = ("source", "category", "pos", "items", "y_nominal")

    def __init__(self, source, category, pos, items, y_nominal=False):
        self.source = source
        self.category = category
        self.pos = pos
        self.items = items          # [(display_name, count)]
        self.y_nominal = y_nominal


def chests_from_prefilter(record):
    """Stage-0 survivor -> [Chest]. Village chest Y is the piece's nominal ground level and is
    emitted but never predicted; Roguelike chest Y is real."""
    out = []
    for start in record.get("village_starts", []):
        for entry in start.get("chests", []):
            ch = entry["chest"]
            out.append(Chest("village", entry.get("category", ""), ch["pos"],
                             [(i.get("name") or f'{i["id"]}:{i["d"]}', i["n"])
                              for i in ch.get("items", [])],
                             y_nominal=True))
    for dungeon in record.get("dungeons", []):
        for ch in dungeon.get("chests", []):
            out.append(Chest("roguelike", "", ch["pos"],
                             [(i.get("name") or f'{i["id"]}:{i["d"]}', i["n"])
                              for i in ch.get("items", [])]))
    return out


def chests_from_corpus(seed_record):
    """Browser corpus seed -> [Chest]. Items are already (name, count) tuples; the chest carries a
    tile-entity type rather than a loot category, so that is what is shown."""
    return [Chest("corpus", c.get("type", ""), c["pos"], list(c.get("items", [])))
            for c in seed_record.get("chests", [])]


def in_scope(chests, spawn, radius):
    """Chebyshev distance in chunks from the spawn chunk — matches searchlib.SeedReport._near."""
    scx, scz = spawn[0] >> 4, spawn[2] >> 4
    return [c for c in chests
            if max(abs((c.pos[0] >> 4) - scx), abs((c.pos[2] >> 4) - scz)) <= radius]


def counted(key, n, limits):
    lim = limits.get(key)
    return n if lim is None else min(n, lim)


def score_seed(chests, values, limits):
    """-> (score, uncapped, marginals, quantities).

    The cap is allocated greedily over chests ranked by their own uncapped value, so each chest's
    marginal is what it actually earns given everything richer was taken first. Ranking on raw chest
    value would promote a chest whose every item was capped away at seed level. Marginals sum to the
    seed score by construction: each item's takes total min(quantity, limit).
    """
    def raw(chest):
        return sum(values.get(norm(n), 0) * q for n, q in chest.items)

    qty = Counter()
    for chest in chests:
        for name, q in chest.items:
            key = norm(name)
            if key in values:
                qty[key] += q

    uncapped = sum(values[k] * n for k, n in qty.items())
    remaining = {k: (math.inf if limits.get(k) is None else limits[k]) for k in qty}

    marginals = []
    for chest in sorted(chests, key=raw, reverse=True):
        earned = 0.0
        for name, q in chest.items:
            key = norm(name)
            if key not in values:
                continue
            take = min(q, remaining[key])
            if take > 0:
                earned += values[key] * take
                remaining[key] -= take
        marginals.append((earned, raw(chest), chest))
    return sum(m[0] for m in marginals), uncapped, marginals, qty


def chest_contents(chest, values, display, limit=6):
    """Valued contents of one chest, richest first, stacks of the same item summed."""
    totals = Counter()
    for name, q in chest.items:
        key = norm(name)
        if key in values:
            totals[key] += q
    ranked = sorted(totals.items(), key=lambda kv: values[kv[0]] * kv[1], reverse=True)
    parts = [f"{display.get(k, k)} x{n} ({values[k] * n:,.0f})" for k, n in ranked[:limit]]
    if len(ranked) > limit:
        parts.append(f"+{len(ranked) - limit} more")
    return ", ".join(parts) if parts else "(nothing valued)"


def read_jsonl_records(src, limit):
    """First `limit` records of a stage-0 sweep (0 = all). src is a path or "<tarball>::<member>".

    Line-limited on purpose: a radius-60 sweep runs about 1 MB per seed, so a 5000-seed file is
    several GB and parsing all of it to look at 100 seeds is the exact wait this limit exists to
    avoid. Kill lines carry no chests and do not count against the limit.
    """
    tar_path, _, member = str(src).partition("::")

    def parse(fh):
        out, kills = [], Counter()
        for line in fh:
            if limit and len(out) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "kill" in d:
                kills[d["kill"]] += 1
                continue
            out.append(d)
        return out, kills

    if member:
        with tarfile.open(tar_path, "r:gz") as tf:
            return parse(tf.extractfile(member))
    with open(tar_path, "rb") as fh:
        return parse(fh)


def sweep_has_chests(src):
    """Does this sweep carry chest data at all? A sweep run without
    -Dprobe.prefilter.villagechests / -Dprobe.prefilter.dungeon has none, and scoring it would
    report 0 for every seed — which reads as 'bad seeds' rather than 'wrong input file'."""
    try:
        records, _ = read_jsonl_records(src, 5)
    except Exception:
        return False
    return any(chests_from_prefilter(r) for r in records)


def occurrence_rates(per_seed_qty, n_seeds):
    """-> {key: (total, mean_per_seed, seeds_containing)} from a list of per-seed Counters."""
    total, present = Counter(), Counter()
    for qty in per_seed_qty:
        for k, n in qty.items():
            total[k] += n
            present[k] += 1
    return {k: (total[k], total[k] / n_seeds, present[k]) for k in total}


def normalised_value(value, total, n_seeds, k_smooth):
    """value / smoothed rate. k_smooth is pseudo-sightings; it bounds the multiplier at
    n_seeds / k_smooth, which is what stops an item seen twice from topping the table."""
    rate = (total + k_smooth) / n_seeds
    return value / rate if rate > 0 else 0.0
