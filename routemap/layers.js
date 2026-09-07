/* Layer builders for the route map.
 *
 * Coordinate convention, applied everywhere: Leaflet's L.CRS.Simple treats a LatLng as
 * (y, x) in an unprojected plane with y growing upward. Minecraft's z grows *south*, so a
 * block (x, z) becomes LatLng(-z, x). That single negation is what makes north point up.
 * Use pt() / rect() below rather than writing coordinates by hand.
 */

const pt = (x, z) => L.latLng(-z, x);
const rect = (x1, z1, x2, z2) => L.latLngBounds(pt(x1, z1), pt(x2, z2));

/* Vector sizing across the zoom range.
 *
 * Marker radii and line widths are in screen pixels, which only looks right at one zoom. Over
 * the full range a fixed 5 px dot covers 80 blocks when zoomed out (a solid mat of overlapping
 * markers) and a third of a block when zoomed in (invisible against the terrain). Scaling by
 * 2^(0.4z) sits between constant-on-screen and constant-in-world: markers shrink as you pull
 * back and grow as you push in, without ever tracking the terrain exactly.
 *
 * Shapes record their unscaled size in `baseRadius` / `baseWeight` so restyleForZoom can
 * rescale them in place rather than rebuilding every layer on each zoom.
 */
let VECTOR_SCALE = 1;

const zoomScale = (z) => Math.min(6, Math.max(0.3, Math.pow(2, z * 0.4)));

function setVectorScale(s) {
  VECTOR_SCALE = s;
}

function restyleForZoom(groups, s) {
  VECTOR_SCALE = s;
  for (const g of groups) {
    if (!g || !g.eachLayer) continue;
    g.eachLayer((l) => {
      const o = l.options;
      if (o.baseRadius != null && l.setRadius) l.setRadius(o.baseRadius * s);
      if (o.baseWeight != null && l.setStyle) l.setStyle({ weight: o.baseWeight * s });
    });
  }
}

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const tpCmd = (x, y, z) => `/tp ${x} ${y} ${z}`;

/** A click-to-select /tp line. `label` marks what the coordinate is for. */
function tpLine(x, y, z, label) {
  return tpRaw(tpCmd(x, y, z), label);
}

/** Same, for a /tp string the exporter already computed -- do not recompute those.
 *
 * The label sits OUTSIDE the <code>, which is the whole point: `user-select: all` on the code
 * element means one click selects exactly the command and nothing else. Putting the label
 * inside and stripping it back off at copy time is what the previous version did, and it
 * quietly failed for every multi-word label -- "1. trigger /tp ..." pasted into chat verbatim.
 */
function tpRaw(cmd, label) {
  return (
    `<div class="tprow">${label ? `<span class="tplbl">${esc(label)}</span>` : ''}` +
    `<code class="tp">${esc(cmd)}</code></div>`
  );
}

const dist = (x, z, spawn) => Math.round(Math.hypot(x - spawn.x, z - spawn.z));

/* ------------------------------------------------------------------ ore veins */

/* Ore colours.
 *
 * These were hashed from the ore name, which was wrong twice over: the hash bunched 9 of 28
 * ores into green and 5 into cyan, so gold drew green and coal drew cyan, and it had no idea
 * what was underneath — lapis landed on hue 237, a mid blue drawn faintly over rivers and
 * lakes, which is exactly where you cannot see it.
 *
 * So: named, recognisable colours, all kept light enough to read against terrain, and every
 * vein outline gets a dark casing (see veinLayer) so contrast never depends on the hue.
 */
const ORE_COLOURS = {
  lapis: '#5b8cff',
  sapphire: '#9a86ff',
  diamond: '#5ce0e0',
  aquaignis: '#6fd8e8',
  apatite: '#6fe0c0',
  olivine: '#7fe05a',
  terraaer: '#c8e07a',
  nickel: '#cfe0a0',
  gold: '#ffd23f',
  mica: '#f0e6b0',
  salts: '#f2f2e2',
  kaolinitezeolite: '#e8dcd0',
  mineralsand: '#dcc89a',
  iron: '#d8a06a',
  copper: '#e87a3c',
  coppertin: '#e0a060',
  lignite: '#a88a64',
  oilsand: '#a08a66',
  redstone: '#ff5555',
  garnettin: '#e86a8c',
  manganese: '#c98fd8',
  molybdenum: '#9fd0e0',
  perditioordo: '#d09ff0',
  tfgalena: '#bcaad8',
  cassiterite: '#d4dbe2',
  magnetite: '#b0b4bc',
  soapstone: '#a8c8b4',
  // Not black: a black outline is invisible on this map and indistinguishable from the casing.
  coal: '#9095a0',
};

// Golden-angle hues for anything not named above, so newly-seen ores spread out instead of
// clustering the way the old hash did.
const ORE_FALLBACK = {};
let oreFallbackN = 0;
function oreColour(ore) {
  if (ORE_COLOURS[ore]) return ORE_COLOURS[ore];
  if (!ORE_FALLBACK[ore]) {
    ORE_FALLBACK[ore] = `hsl(${(oreFallbackN++ * 137.508) % 360}, 70%, 68%)`;
  }
  return ORE_FALLBACK[ore];
}

function veinLayer(data, spawn, filter) {
  const g = L.layerGroup();
  // With all 28 ores on, 1254 overlapping boxes have to stay faint or they hide the terrain
  // the veins are meant to be located against. Once you have filtered down to a handful there
  // is no clutter budget to protect, so the survivors are drawn to be found.
  const focused = filter.ores.size <= 3;
  const wt = focused ? 1.8 : 0.8;

  for (const v of data.veins) {
    if (!filter.ores.has(v.ore)) continue;
    if (!v.stable && !filter.unstable) continue;
    if (v.d > filter.maxDist) continue;
    const col = oreColour(v.ore);
    const [wx, nz, ex, sz] = v.b;
    const bounds = rect(wx, nz, ex + 1, sz + 1);

    // Dark casing under the outline. This is what makes a vein findable regardless of its
    // colour or what it sits on -- a thin blue lapis box over a blue river is invisible
    // without it, and no choice of hue fixes that for every background at once.
    g.addLayer(
      L.rectangle(bounds, {
        color: '#0b0d11',
        weight: (wt + 1.6) * VECTOR_SCALE,
        baseWeight: wt + 1.6,
        opacity: v.stable ? 0.6 : 0.35,
        fill: false,
        interactive: false,
      })
    );

    const box = L.rectangle(bounds, {
      color: col,
      weight: wt * VECTOR_SCALE,
      baseWeight: wt,
      opacity: v.stable ? 0.95 : 0.55,
      fillColor: col,
      fillOpacity: v.stable ? (focused ? 0.22 : 0.12) : 0.04,
      dashArray: v.stable ? null : '3,3',
      interactive: true,
    });
    box.bindPopup(() => veinPopup(v, spawn));
    box.bindTooltip(`${v.ore} y${v.y}${v.stable ? '' : ' (route-unstable)'}`, { sticky: true });
    g.addLayer(box);
  }
  return g;
}

function veinPopup(v, spawn) {
  const [wx, nz, ex, sz] = v.b;
  let h = `<div class="pop"><h3 style="color:${oreColour(v.ore)}">${esc(v.ore)}</h3>`;
  h += `<div class="kv">${esc(v.layer)} &middot; ${ex - wx + 1}&times;${sz - nz + 1} blocks &middot; ${v.d} from spawn</div>`;
  h += tpLine(v.x, v.y, v.z, 'centre');
  h += `<div class="kv">x ${wx}&hellip;${ex} &nbsp; z ${nz}&hellip;${sz} &nbsp; top of vein y ${v.y}</div>`;
  if (!v.stable) {
    h +=
      `<div class="warn">Route-unstable: this vein differed between a rows walk and a spiral ` +
      `walk of the same seed. GT vein placement is not a pure function of the seed on this ` +
      `pack, so it may not be here.</div>`;
  }
  return h + '</div>';
}

/* ------------------------------------------------------------------ loot */

// Y confidence decides what you do on arrival, so it drives the marker outline.
const YCONF_STYLE = {
  exact: { color: '#ffffff', weight: 1.2, dash: null },
  approx: { color: '#e0a33c', weight: 1.2, dash: '2,2' },
  nominal: { color: '#e0a33c', weight: 1.4, dash: '3,2' },
  sky: { color: '#e05c5c', weight: 1.4, dash: '2,3' },
};

// contents_confidence values that mean "nothing is wrong" and so are not worth a line.
const QUIET_CONFIDENCE = new Set(['cross-env verified', 'predicted', 'exact', '']);

const YCONF_TEXT = {
  exact: 'Y is exact.',
  approx: 'Y is approximate and may be one block low (mod runs after decoration).',
  nominal: 'Y is the piece box origin, not the chest. Fly down from it.',
  sky: 'No Y was predicted. This is a piece centre, not a chest position.',
};

function valueColour(v, max) {
  const t = Math.min(1, Math.sqrt(Math.max(0, v) / Math.max(1, max)));
  // cool -> hot: low-value chests recede, high-value ones pop
  const r = Math.round(70 + 185 * t);
  const g = Math.round(140 - 60 * t);
  const b = Math.round(210 - 170 * t);
  return `rgb(${r},${g},${b})`;
}

function lootLayer(loot, spawn, filter) {
  const g = L.layerGroup();
  const maxVal = loot.chests.length ? loot.chests[0].val : 1;

  for (const c of loot.chests) {
    let matched = null;
    if (filter.mode === 'item') {
      if (!filter.items.size) continue;
      matched = c.items.filter((i) => filter.items.has(i.n));
      if (!matched.length) continue;
    } else {
      if (!filter.sources.has(c.src)) continue;
      if (c.val < filter.minVal) continue;
    }

    const y = YCONF_STYLE[c.yconf] || YCONF_STYLE.exact;
    let radius, fill;
    if (matched) {
      const qty = matched.reduce((a, i) => a + i.c, 0);
      radius = 2.5 + Math.min(7, Math.sqrt(qty) * 1.4);
      fill = '#7fb2ff';
    } else {
      radius = 2 + 5 * Math.min(1, Math.sqrt(c.val / Math.max(1, maxVal)));
      fill = valueColour(c.val, maxVal);
    }

    const m = L.circleMarker(pt(c.x, c.z), {
      radius: radius * VECTOR_SCALE,
      baseRadius: radius,
      color: y.color,
      weight: y.weight * VECTOR_SCALE,
      baseWeight: y.weight,
      dashArray: y.dash,
      fillColor: fill,
      fillOpacity: 0.85,
    });
    m.bindPopup(() => lootPopup(c, matched, spawn), { maxWidth: 340 });
    m.bindTooltip(
      matched
        ? `${matched.map((i) => `${i.n} x${i.c}`).join(', ')}`
        : `${c.val.toLocaleString()} pts &middot; ${c.src}`,
      { sticky: true }
    );
    g.addLayer(m);
  }
  return g;
}

function lootPopup(c, matched, spawn) {
  let h = `<div class="pop"><h3>${c.val.toLocaleString()} pts &middot; ${esc(c.src)}${
    c.struct ? ' ' + esc(c.struct) : ''
  }</h3>`;
  h += `<div class="kv">${c.items.length} stacks &middot; ${c.d} from spawn${
    c.cat ? ' &middot; ' + esc(c.cat) : ''
  }</div>`;

  // A roguelike chest does not exist until its dungeon's trigger chunk populates. Teleporting
  // straight to the chest can generate the chunk with no dungeon in it.
  if (c.trigger) {
    h += `<div class="warn">Trigger the dungeon first, or the chest will not be there.</div>`;
    h += tpRaw(c.trigger, '1. trigger');
    h += tpRaw(c.tp, '2. chest');
  } else {
    h += tpRaw(c.tp, null);
  }
  h += `<div class="kv">chest block at ${c.x}, ${c.y}, ${c.z}</div>`;

  if (c.yconf !== 'exact') {
    h += `<div class="warn">${esc(YCONF_TEXT[c.yconf] || c.ynote)}</div>`;
  }
  // Only surface contents_confidence when it is actually a caveat. "cross-env verified" and
  // "predicted" are the normal, healthy states and cover 997 of 1044 chests here -- printing
  // them as a warning on almost every chest trains you to ignore the line, which is exactly
  // when the 47 that do mean something get missed.
  if (c.conf && !QUIET_CONFIDENCE.has(c.conf)) {
    h += `<div class="warn">${esc(c.conf)}${c.note ? ` &mdash; ${esc(c.note)}` : ''}</div>`;
  }

  const show = matched && matched.length ? matched : c.items;
  const head = matched && matched.length ? 'Matching stacks' : 'Contents';
  h += `<div class="kv" style="margin-top:6px">${head}</div><ul>`;
  for (const i of show.slice(0, 60)) {
    h += `<li>${esc(i.n)} <span class="q">&times;${i.c}${
      i.v ? ` &middot; ${i.v.toLocaleString()} pts` : ''
    }</span>${i.gate ? ' &starf;' : ''}</li>`;
  }
  h += '</ul>';
  if (show.length > 60) h += `<div class="kv">&hellip; and ${show.length - 60} more</div>`;
  return h + '</div>';
}

/* ------------------------------------------------------------------ POIs */

// `off` keeps a kind out of the default selection without hiding it from the list.
// `about` explains a kind whose name does not carry its own meaning.
const POI_KINDS = [
  { key: 'spawn', label: 'Spawn', colour: '#ffffff' },
  { key: 'village-smeltery', label: 'Village +smeltery', colour: '#ffd24d' },
  { key: 'village', label: 'Village', colour: '#c9b46a' },
  { key: 'dungeon-ROGUE', label: 'Dungeon ROGUE', colour: '#ff7a7a' },
  { key: 'dungeon-PYRAMID', label: 'Dungeon PYRAMID', colour: '#ff9d5c' },
  { key: 'dungeon-ENIKO', label: 'Dungeon ENIKO', colour: '#ff6ad5' },
  { key: 'enchant-table', label: 'Enchanting table', colour: '#b98cff' },
  { key: 'stronghold', label: 'Stronghold', colour: '#8fd4ff' },
  {
    key: 'witchery-Coven',
    label: 'Coven circle',
    colour: '#ff5cc8',
    about:
      'A witches’ coven stone circle. The one Witchery cell type worth routing to — ' +
      'it is the ritual circle, and it carries the loot.',
  },
  {
    key: 'witchery-Shack',
    label: 'Witchery shack',
    colour: '#b06fd0',
    off: true,
    about: 'A small witch’s hut. Minor loot.',
  },
  {
    key: 'witchery-WickerMan',
    label: 'Wicker man',
    colour: '#9a6fd0',
    off: true,
    about: 'A wicker effigy. Scenery — no container.',
  },
  {
    key: 'witchery-ClonedStructure',
    label: 'Witchery clone spot',
    colour: '#7a6fb0',
    off: true,
    about:
      'Witchery’s WorldHandlerClonedStructure won this cell: the mod copies a prebuilt ' +
      'structure in here rather than generating one of its own. Nothing distinctive to visit.',
  },
  {
    key: 'witchery-none',
    label: 'Witchery cell (undecided)',
    colour: '#5e5a72',
    off: true,
    about:
      'Witchery picks between four handlers per cell, in order. Stage 0 predicts the world ' +
      'without generating it, and for this cell it could not tell which handler wins — ' +
      'so something is likely here, but not what. Not "empty", just unknown.',
  },
  // Off by default. These are the largest axis-aligned squares the prefilter could inscribe,
  // not the regions themselves -- on this world the no-rain square is 100 chunks against 4408
  // that actually qualify. Two big dashed boxes implying otherwise are not what the map should
  // open on; the Climate overlay shows the real thing.
  {
    key: 'no-rain',
    label: 'No-rain square',
    colour: '#ffe08a',
    off: true,
    about:
      'Largest all-no-rain square the stage-0 prefilter could inscribe. The real no-rain ' +
      'region is much larger and ragged — see the Climate overlay.',
  },
  {
    key: 'humid',
    label: 'Humid square',
    colour: '#8affc8',
    off: true,
    about:
      'Largest all-humid square the stage-0 prefilter could inscribe. The real humid region ' +
      'is much larger and ragged — see the Climate overlay.',
  },
  { key: 'village-pieces', label: 'Village piece boxes', colour: '#c9b46a', off: true },
  // On by default: a stronghold's marker is one dot for a structure that sprawls over
  // hundreds of blocks underground, and the piece boxes are the only thing that shows its
  // actual footprint and which way the corridors run.
  { key: 'stronghold-pieces', label: 'Stronghold piece boxes', colour: '#8fd4ff' },
];

const POI_KIND = Object.fromEntries(POI_KINDS.map((k) => [k.key, k]));

/** Flatten the prefilter-derived POI record into {kind, x, z, ...} features. */
function poiFeatures(pois) {
  const out = [];
  if (pois.spawn) {
    out.push({ kind: 'spawn', name: 'Spawn', x: pois.spawn.x, y: pois.spawn.y, z: pois.spawn.z });
  }
  for (const v of pois.villages || []) {
    out.push({
      kind: v.smeltery ? 'village-smeltery' : 'village',
      name: `${v.name}${v.smeltery ? ' +SMELTERY' : ''}`,
      x: v.x,
      z: v.z,
      y: null,
      village: v,
    });
    for (const p of v.pieces || []) {
      out.push({ kind: 'village-pieces', box: p.b, name: p.name, x: p.b[0], z: p.b[2] });
    }
  }
  for (const d of pois.dungeons || []) {
    out.push({
      kind: `dungeon-${d.tower}`,
      name: `Dungeon ${d.tower} trigger`,
      x: d.x,
      z: d.z,
      y: 100,
      dungeon: d,
    });
    for (const e of d.enchant_tables || []) {
      out.push({
        kind: 'enchant-table',
        name: `Enchanting table (${d.tower})`,
        x: e[0],
        y: e[1],
        z: e[2],
        dungeon: d,
      });
    }
  }
  for (const w of pois.witchery || []) {
    const kind = `witchery-${w.winner || 'none'}`;
    out.push({
      kind,
      name: POI_KIND[kind]?.label || `Witchery ${w.winner}`,
      x: w.x,
      z: w.z,
      y: null,
      witchery: w,
    });
  }
  for (const s of pois.strongholds || []) {
    out.push({ kind: 'stronghold', name: `Stronghold ${s.i}`, x: s.x, z: s.z, y: null, sh: s });
    for (const p of s.pieces || []) {
      out.push({ kind: 'stronghold-pieces', box: p.b, name: p.name, x: p.b[0], z: p.b[2] });
    }
  }
  for (const q of pois.squares || []) {
    out.push({
      kind: q.kind,
      name: `${q.kind === 'no-rain' ? 'No-rain' : 'Humid'} ${q.side_chunks}x${q.side_chunks} chunks`,
      x: q.cx * 16,
      z: q.cz * 16,
      y: null,
      square: q,
    });
  }
  // Twilight Forest structures, keyed by feature name.
  for (const f of pois.features || []) {
    out.push({ kind: `tf-${f.name}`, name: f.name, x: f.x, z: f.z, y: null, tf: f });
  }
  return out;
}

/**
 * Build the POI features as TWO groups: area shapes and point markers.
 *
 * Hit-testing on a canvas renderer resolves to the last thing drawn, so draw order is click
 * priority -- and that ordering is global, not per layer group. Areas and markers therefore
 * cannot live in one group: the caller has to be able to put every area shape underneath the
 * ore veins and loot markers, which are separate groups entirely. All 24 stronghold chests sit
 * inside a stronghold piece box, so with the areas on top not one of them was clickable.
 *
 * See VECTOR_STACK in map.js for the order they are put back in.
 */
function poiLayers(features, spawn, enabled) {
  const areas = L.layerGroup();
  const markers = L.layerGroup();
  const shown = features.filter((f) => enabled.has(f.kind));
  // Squares below piece boxes: a biome square is far larger than any structure piece.
  shown.sort((a, b) => (a.square ? 0 : 1) - (b.square ? 0 : 1));

  for (const f of shown) {
    const g = f.square || f.box ? areas : markers;
    const colour = poiColour(f.kind);

    if (f.box) {
      const [x1, y1, z1, x2, y2, z2] = f.box;
      const r = L.rectangle(rect(x1, z1, x2 + 1, z2 + 1), {
        color: colour,
        weight: 1 * VECTOR_SCALE,
        baseWeight: 1,
        opacity: 0.7,
        fillOpacity: 0.08,
      });
      r.bindTooltip(`${esc(f.name)} y${y1}&ndash;${y2}`, { sticky: true });
      r.bindPopup(
        `<div class="pop"><h3>${esc(f.name)}</h3>` +
          `<div class="kv">x ${x1}&hellip;${x2} &nbsp; y ${y1}&hellip;${y2} &nbsp; z ${z1}&hellip;${z2}</div>` +
          tpLine(Math.round((x1 + x2) / 2), y2 + 1, Math.round((z1 + z2) / 2), null) +
          '</div>'
      );
      g.addLayer(r);
      continue;
    }

    if (f.square) {
      const q = f.square;
      // Drawn as a non-interactive fill plus a closed POLYLINE border, not a polygon. A
      // polygon hit-tests its whole interior, so a 352-block square would answer every hover
      // inside it and bury the ore veins and chests it contains. A polyline only hit-tests
      // its stroke, so the outline stays clickable and the interior is see-through to
      // everything underneath.
      g.addLayer(
        L.rectangle(rect(q.x1, q.z1, q.x2, q.z2), {
          stroke: false,
          fillColor: colour,
          fillOpacity: 0.07,
          interactive: false,
        })
      );
      const ring = L.polyline(
        [pt(q.x1, q.z1), pt(q.x2, q.z1), pt(q.x2, q.z2), pt(q.x1, q.z2), pt(q.x1, q.z1)],
        { color: colour, weight: 2 * VECTOR_SCALE, baseWeight: 2, opacity: 0.9, dashArray: '6,4' }
      );
      ring.bindTooltip(esc(f.name), { sticky: true });
      ring.bindPopup(
        `<div class="pop"><h3>${esc(f.name)}</h3>` +
          `<div class="kv">x ${q.x1}&hellip;${q.x2} &nbsp; z ${q.z1}&hellip;${q.z2}</div>` +
          tpLine(f.x, 200, f.z, 'centre') +
          `<div class="kv">This is the largest all-${
            q.kind === 'no-rain' ? 'no-rain' : 'humid'
          } <em>square</em> the stage-0 prefilter could inscribe — axis-aligned, and broken by ` +
          `a single qualifying-failing column. The real region is larger and ragged: turn on ` +
          `the Climate overlay to see it.</div>` +
          `<div class="warn">No height was predicted for this square. Fly down.</div></div>`
      );
      g.addLayer(ring);
      continue;
    }

    const baseR = f.kind === 'spawn' ? 7 : 5;
    const m = L.circleMarker(pt(f.x, f.z), {
      radius: baseR * VECTOR_SCALE,
      baseRadius: baseR,
      color: '#0d0f13',
      weight: 1.5 * VECTOR_SCALE,
      baseWeight: 1.5,
      fillColor: colour,
      fillOpacity: 0.95,
    });
    m.bindTooltip(esc(f.name), { sticky: true });
    m.bindPopup(() => poiPopup(f, spawn), { maxWidth: 340 });
    g.addLayer(m);
  }
  return { areas, markers };
}

function poiColour(kind) {
  const k = POI_KIND[kind];
  if (k) return k.colour;
  // TF features get a hashed hue, same scheme as ores.
  let h = 0;
  for (let i = 0; i < kind.length; i++) h = (h * 31 + kind.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360}, 62%, 66%)`;
}

function poiPopup(f, spawn) {
  let h = `<div class="pop"><h3 style="color:${poiColour(f.kind)}">${esc(f.name)}</h3>`;
  h += `<div class="kv">${dist(f.x, f.z, spawn)} blocks from spawn</div>`;
  const about = POI_KIND[f.kind]?.about;
  if (about) h += `<div class="kv">${esc(about)}</div>`;

  if (f.dungeon && f.kind.startsWith('dungeon-')) {
    const d = f.dungeon;
    h += `<div class="warn">Go to the trigger first. The dungeon does not exist until this ` +
      `chunk populates, and its chests sit up to ~235 blocks away.</div>`;
    h += tpLine(f.x, 100, f.z, 'trigger');
    h += `<div class="kv">${d.n_chests} chests${
      d.enchant_tables.length ? `, ${d.enchant_tables.length} enchanting table(s)` : ''
    }</div>`;
  } else if (f.kind === 'enchant-table') {
    h += tpLine(f.x, f.y, f.z, null);
    h += `<div class="warn">Trigger the ${esc(f.dungeon.tower)} dungeon first.</div>`;
  } else if (f.village) {
    const v = f.village;
    h += tpLine(f.x, 200, f.z, 'centre');
    h += `<div class="warn">No village height is predicted. Fly down.</div>`;
    h += `<div class="kv">${v.pieces.length} pieces &middot; ${v.n_chests} predicted chests${
      v.n_unpredicted ? ` &middot; ${v.n_unpredicted} unpredicted` : ''
    }${v.sizeable ? ' &middot; sizeable' : ''}${
      v.tic_workshop ? ' &middot; TiC tool workshop' : ''
    }</div>`;
    const notable = v.pieces.filter((p) => /Smeltery|ToolWorkshop|Blacksmith|Tower/.test(p.name));
    if (notable.length) {
      h += '<ul>';
      for (const p of notable) h += `<li>${esc(p.name)} <span class="q">@ ${p.b[0]},${p.b[2]}</span></li>`;
      h += '</ul>';
    }
  } else if (f.witchery) {
    const w = f.witchery;
    h += tpLine(f.x, w.y == null ? 200 : w.y + 1, f.z, null);
    h += `<div class="warn">${
      w.y == null
        ? 'No height is known for this cell &mdash; nothing inside it has a position. Fly down.'
        : 'Height comes from a container inside the structure. Witchery generates after ' +
          'decoration, so it may be one block low.'
    }</div>`;
    h += `<div class="kv">biome ${w.biome} &middot; ${w.n_chests} container(s)${
      w.allowed ? '' : ' &middot; not allowed here'
    }</div>`;
    if (!w.winner) {
      h += `<div class="warn">No handler resolved worldlessly for this cell &mdash; nothing is ` +
        `promised to be here.</div>`;
    }
  } else if (f.sh) {
    h += tpLine(f.x, 200, f.z, 'centre');
    h += `<div class="warn">Stronghold height is not predicted. Fly down / dig.</div>`;
    h += `<div class="kv">${f.sh.n_pieces} pieces &middot; ${f.sh.n_chests} predicted chests</div>`;
  } else if (f.tf) {
    h += tpLine(f.x, 200, f.z, 'centre');
    h += `<div class="kv">region ${esc(f.tf.region)} &middot; size ${f.tf.size} &middot; ${esc(
      f.tf.biome || ''
    )}</div>`;
    h += `<div class="warn">Feature centres are seed-independent; only which feature lands in a ` +
      `region varies by seed.</div>`;
  } else {
    h += tpLine(f.x, f.y == null ? 200 : f.y, f.z, null);
  }
  return h + '</div>';
}

/* ------------------------------------------------------------------ reference */

// Drawn as a dark casing under a bright core so the line stays readable over pale desert and
// dark forest alike -- a single translucent stroke disappears against one or the other.
const RINGS = [
  { r: 250, colour: '#7fe0a0' },
  { r: 500, colour: '#ffd24d' },
  { r: 1000, colour: '#ff8c6a' },
];

function ringLayer(spawn) {
  const g = L.layerGroup();
  const c = pt(spawn.x, spawn.z);
  for (const { r, colour } of RINGS) {
    g.addLayer(
      L.circle(c, { radius: r, color: '#0d0f13', weight: 4.5, opacity: 0.5, fill: false, interactive: false })
    );
    g.addLayer(
      L.circle(c, {
        radius: r,
        color: colour,
        weight: 1.6,
        opacity: 0.95,
        fill: false,
        dashArray: '9,7',
        interactive: false,
      })
    );
    // Label on all four sides: at a normal zoom only part of a 1000-block ring is on screen.
    for (const [dx, dz] of [[0, -r], [0, r], [-r, 0], [r, 0]]) {
      g.addLayer(
        L.marker(pt(spawn.x + dx, spawn.z + dz), {
          icon: L.divIcon({
            className: '',
            html: `<span class="ringlbl" style="color:${colour}">${r}</span>`,
            iconSize: [46, 16],
            iconAnchor: [23, 8],
          }),
          interactive: false,
        })
      );
    }
  }
  return g;
}

/** Grid pitches that mean something in this game, rather than an arbitrary spacing. */
const GRIDS = {
  16: { label: 'Chunk (16)', major: 16 },
  256: { label: 'TF feature grid (256)', major: 4 },
  512: { label: 'Region / JourneyMap tile (512)', major: 2 },
};

function gridLayer(bounds, step) {
  const g = L.layerGroup();
  const cfg = GRIDS[step] || GRIDS[512];
  const { x0, z0, x1, z1 } = bounds;

  // Every Nth line is drawn heavier, so the grid still reads when the fine lines get too
  // dense to tell apart, and there is always an obvious anchor to count from.
  const line = (a, b, major) =>
    L.polyline([a, b], {
      color: major ? '#9fd0ff' : '#7fb2ff',
      weight: major ? 1.1 : 0.5,
      opacity: major ? 0.55 : 0.22,
      interactive: false,
    });

  for (let x = Math.ceil(x0 / step) * step; x <= x1; x += step) {
    g.addLayer(line(pt(x, z0), pt(x, z1), (x / step) % cfg.major === 0));
  }
  for (let z = Math.ceil(z0 / step) * step; z <= z1; z += step) {
    g.addLayer(line(pt(x0, z), pt(x1, z), (z / step) % cfg.major === 0));
  }
  // The axes through the world origin, which is what every grid coordinate is measured from.
  const axis = { color: '#ffffff', weight: 1.4, opacity: 0.4, interactive: false };
  g.addLayer(L.polyline([pt(0, z0), pt(0, z1)], axis));
  g.addLayer(L.polyline([pt(x0, 0), pt(x1, 0)], axis));
  return g;
}
