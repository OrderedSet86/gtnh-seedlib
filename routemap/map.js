/* Route map controller: loads a world bundle and wires the filter panel to the layers. */

// Deep-linkable view. ?dim=7&base=topo&item=Bronze+Ingot&x=152&z=144&zoom=1 reproduces a
// specific state, so a route can be shared as a URL.
const QS = new URLSearchParams(location.search);
const BUNDLE = QS.get('world') || '-1636594104014467454';
const ROOT = `../worlds/${BUNDLE}`;

const DIM_NAMES = { '-1': 'Nether', 0: 'Overworld', 7: 'Twilight Forest' };

// Tab order. A plain .sort() on the keys is a STRING sort, which puts '-1' before '0' and opens
// the map on the Nether; travel order is what a reader expects.
const DIM_ORDER = ['0', '-1', '7'];
const dimKeys = (m) =>
  Object.keys(m).sort((a, b) => {
    const ia = DIM_ORDER.indexOf(a), ib = DIM_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });

const state = {
  meta: null,
  palette: null,
  dim: null,
  data: {}, // per-dim { veins, loot, pois, features }
  base: null, // resolved to the first available layer, which is the block render
  baseOpacity: 1,
  // `unstable` off by default: those veins differed between two walks of the same seed, so
  // showing them by default puts coordinates on the map that may not be there.
  veins: { on: true, ores: new Set(), unstable: false, maxDist: 1600 },
  loot: { mode: 'chest', sources: new Set(), minVal: 0, items: new Set(), search: '' },
  pois: new Set(),
  userMoved: false, // set once the view is the user's, not ours
  climate: false,
  rings: false,
  grid: 'off', // 'off' | '16' | '256' | '512'
};

const layers = {}; // live L.Layer handles by role
let map;

// The opening view is re-fitted when the window or the panel changes size, but only until the
// user takes over: refitting under someone who has zoomed into a dungeon is worse than a
// letterboxed map. Our own setView calls must not count as the user moving.
let programmaticMove = false;

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ boot */

async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function boot() {
  map = L.map('map', {
    crs: L.CRS.Simple,
    preferCanvas: true, // ~2500 vectors; SVG chokes, canvas does not
    // Zoom 0 is one screen pixel per block. The range runs from the whole 2560-block world in
    // a laptop window (-5) to a single block filling 256 px (8), which is enough to pick out
    // one chest in a dungeon room.
    minZoom: -5,
    maxZoom: 8,
    // No snapping. There are no tiles to align to -- the bases are single images -- and any
    // snap rounds the opening fit DOWN, which is what left a border round the map: the exact
    // fill zoom of -1.4 floored to -2 and lost a quarter of the window. zoomDelta below still
    // gives quarter-level wheel and button steps.
    zoomSnap: 0,
    // Quarter-level wheel and button steps, so the extra range does not mean flying past the
    // scale you wanted.
    zoomDelta: 0.25,
    wheelPxPerZoomLevel: 100,
    zoomControl: true,
    attributionControl: false,
  });

  state.meta = await getJSON(`${ROOT}/meta.json`);
  try {
    state.palette = await getJSON(`${ROOT}/palette.json`);
  } catch (e) {
    state.palette = null;
  }

  $('worldline').textContent = `seed ${state.meta.seed} · ${state.meta.pack}`;
  document.title = `Route map · ${state.meta.seed}`;

  map.on('movestart zoomstart', () => {
    if (!programmaticMove) state.userMoved = true;
  });
  // Keep filling the window as it changes shape, until the user has moved the view.
  map.on('resize', () => {
    if (!state.userMoved) fitDim();
  });

  renderCaveats();
  buildDimButtons();
  bindChrome();

  const dims = dimKeys(state.meta.dims);
  const want = QS.get('dim');
  await setDim(dims.includes(want) ? Number(want) : dims.includes('0') ? 0 : Number(dims[0]));
}

/** Apply ?base / ?item / ?x,z,zoom once the dimension's data is loaded. */
function applyQuery() {
  const base = QS.get('base');
  if (base) {
    state.base = base;
    buildBaseButtons();
    drawBase();
  }
  const ores = QS.get('ores');
  if (ores && state.data[state.dim].veins) {
    state.veins.ores = new Set(ores.split(',').map((s) => s.trim()).filter(Boolean));
    buildOreList();
    drawVeins();
  }
  const items = QS.get('item');
  if (items && state.data[state.dim].loot) {
    state.loot.mode = 'item';
    state.loot.items = new Set(items.split(',').map((s) => s.trim()).filter(Boolean));
    state.loot.search = '';
    buildLootControls();
    drawLoot();
  }
  if (QS.get('climate') === '1') {
    $('climate').checked = true;
    $('climate').onchange({ target: $('climate') });
  }
  if (QS.get('rings') === '1') {
    $('rings').checked = true;
    $('rings').onchange({ target: $('rings') });
  }
  const grid = QS.get('grid');
  if (grid) {
    $('grid').value = grid;
    $('grid').onchange({ target: $('grid') });
  }
  const x = QS.get('x');
  const z = QS.get('z');
  if (x != null && z != null) {
    map.setView(pt(Number(x), Number(z)), Number(QS.get('zoom') ?? 0), { animate: false });
    state.userMoved = true;
  }
}

function renderCaveats() {
  const el = $('caveats');
  el.innerHTML = (state.meta.caveats || []).map((c) => `<p>${esc(c)}</p>`).join('');
  if (!el.innerHTML) el.remove();
}

/* ------------------------------------------------------------------ dimension */

function buildDimButtons() {
  const el = $('dims');
  el.innerHTML = '';
  for (const d of dimKeys(state.meta.dims)) {
    const b = document.createElement('button');
    b.textContent = DIM_NAMES[d] || `DIM${d}`;
    b.dataset.dim = d;
    b.onclick = () => setDim(Number(d));
    el.appendChild(b);
  }
}

async function setDim(dim) {
  state.dim = dim;
  // Before anything reads a colour: the ore list, its legend swatches and the vein boxes all go
  // through oreColour(), and the Nether overrides the warm half of the palette.
  setOreDim(dim);
  for (const b of $('dims').children) b.classList.toggle('on', Number(b.dataset.dim) === dim);

  if (!state.data[dim]) {
    const d = { veins: null, loot: null, pois: null };
    // Each file is optional: a dimension may have veins but no loot export, or vice versa.
    await Promise.all([
      getJSON(`${ROOT}/dim${dim}/veins.json`).then((v) => (d.veins = v), () => {}),
      getJSON(`${ROOT}/dim${dim}/loot.json`).then((v) => (d.loot = v), () => {}),
      getJSON(`${ROOT}/dim${dim}/pois.json`).then((v) => (d.pois = v), () => {}),
    ]);
    d.features = d.pois ? poiFeatures(d.pois) : [];
    state.data[dim] = d;
  }

  // Spawn is only meaningful in the overworld, but every dimension anchors its rings, its
  // opening view and its "from spawn" numbers to the point that CORRESPONDS to spawn.
  //
  // That is not the same coordinate everywhere. The Twilight Forest is 1:1 with the overworld,
  // so reusing the overworld numbers is right there -- but the Nether is 1:8, and using them
  // literally put the distance rings ~130 blocks off centre, which is visible on the map. The
  // ratio is a pack config (hodgepodge netherPortalRatio), so the bundle carries it rather than
  // the viewer assuming 8.
  const sp = state.meta.spawn || { x: 0, y: 64, z: 0 };
  const scale = (state.meta.dims[String(dim)] || {}).coordScale || 1;
  state.spawn = { x: Math.round(sp.x / scale), y: sp.y, z: Math.round(sp.z / scale) };

  resetFilters();
  buildBaseButtons();
  buildOreList();
  buildLootControls();
  buildPoiList();
  buildClimate();
  // Set the view BEFORE adding any layer. Until the map has a view it is not "ready", and
  // L.Map.addLayer defers the real work through whenReady() -- so adds and removes queue up
  // and replay in call order, which makes restack() silently do nothing and leaves the POI
  // area boxes on top of the loot markers they cover.
  fitDim();
  drawAll();
  applyQuery();
  restack();
}

/** Width of map the panel is sitting on top of, in px. */
function panelOccludes() {
  const el = $('panel');
  if (!el || el.classList.contains('hidden')) return 0;
  const w = el.offsetWidth;
  // Below 640px the panel goes full width and covers the map entirely; there is no strip to
  // centre anything in, so treat it as not occluding and let the user hide it.
  return w > map.getSize().x * 0.6 ? 0 : w;
}

/**
 * Open on a view that fills the visible map area.
 *
 * fitBounds "contains" -- it shrinks the world until all of it is inside the viewport, which
 * on any non-square window leaves a border. This covers instead: scale by the LARGER of the
 * two ratios so the shorter axis is filled exactly and the longer one runs off screen. The
 * available width also excludes the panel, and the centre is shifted to match, or half the
 * world would open underneath it.
 */
function fitDim() {
  programmaticMove = true;
  try {
    fitDimInner();
  } finally {
    programmaticMove = false;
  }
}

function fitDimInner() {
  const b = (state.meta.dims[String(state.dim)] || {}).bounds;
  if (!b) {
    map.setView(pt(state.spawn.x, state.spawn.z), 0, { animate: false });
    return;
  }
  const size = map.getSize();
  const panel = panelOccludes();
  const availX = Math.max(50, size.x - panel);
  const zoom = Math.log2(Math.max(availX / b.w, size.y / b.h));

  // Not animated: there is no previous view to animate from, and an interrupted zoom
  // animation can leave the map pinned at minZoom.
  map.setView(pt(b.x0 + b.w / 2, b.z0 + b.h / 2), zoom, { animate: false });
  // Push the world out from behind the panel: panning the view left moves content right.
  if (panel) map.panBy([-panel / 2, 0], { animate: false });
}

// Ores selected on load. All 1256 vein boxes at once is a mesh you cannot read anything out
// of, so the map opens on the few worth routing to and the rest are one click away. Any of
// these the dimension does not have is skipped (the Twilight Forest has lapis but no mica);
// if it has none of them, fall back to showing everything rather than an empty map.
const DEFAULT_ORES = {
  0: ['lapis', 'mica'],
  7: ['terraaer', 'perditioordo', 'aquaignis'], // the Twilight Forest shard mixes
  // The Nether carries 11 mixes; these are the ones worth crossing a portal for. Molybdenum is
  // the rarest by an order of magnitude (9 regions in r60 on the reference seed vs 347 for iron).
  '-1': ['molybdenum', 'beryllium', 'sulfur'],
};

function resetFilters() {
  const d = state.data[state.dim];
  const all = d.veins ? Object.keys(d.veins.ores) : [];
  const wanted = (DEFAULT_ORES[state.dim] || []).filter((o) => d.veins && d.veins.ores[o]);
  state.veins.ores = new Set(wanted.length ? wanted : all);
  state.loot.sources = new Set(d.loot ? Object.keys(d.loot.sources) : []);
  state.loot.items = new Set();
  state.loot.mode = 'chest';
  // Default on: every kind except those flagged `off` -- piece-box outlines, which are noise
  // at world zoom, and the Witchery cell types that are not worth a detour.
  state.pois = new Set(
    [...new Set(d.features.map((f) => f.kind))].filter((k) => !POI_KIND[k]?.off)
  );
}

/* ------------------------------------------------------------------ base layer */

function baseOptions() {
  const m = state.meta.dims[String(state.dim)] || {};
  return (m.bases || []).concat([{ key: 'none', label: 'None', note: '' }]);
}

function buildBaseButtons() {
  const opts = baseOptions();
  const el = $('bases');
  el.innerHTML = '';
  for (const o of opts) {
    const b = document.createElement('button');
    b.textContent = o.label;
    b.dataset.base = o.key;
    b.onclick = () => {
      state.base = o.key;
      buildBaseButtons();
      drawBase();
    };
    el.appendChild(b);
  }
  if (!opts.some((o) => o.key === state.base)) state.base = opts[0].key;
  for (const b of el.children) b.classList.toggle('on', b.dataset.base === state.base);
}

function drawBase() {
  if (layers.base) {
    map.removeLayer(layers.base);
    layers.base = null;
  }
  const m = state.meta.dims[String(state.dim)] || {};
  const b = (m.bases || []).find((o) => o.key === state.base);
  if (b) {
    layers.base = L.imageOverlay(
      `${ROOT}/dim${state.dim}/${b.file}`,
      rect(b.x0, b.z0, b.x0 + b.w, b.z0 + b.h),
      { opacity: state.baseOpacity, className: 'pixelated', interactive: false }
    ).addTo(map);
    layers.base.setZIndex(1);
  }
}

/* ------------------------------------------------------------------ list widgets */

/** Build a checkbox list. `items` is [{key, label, n, colour}]. */
function checkList(el, items, selected, onChange, opts = {}) {
  el.innerHTML = '';
  for (const it of items) {
    const row = document.createElement('label');
    row.className = 'row';
    if (it.title) row.title = it.title;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = selected.has(it.key);
    cb.onchange = () => {
      cb.checked ? selected.add(it.key) : selected.delete(it.key);
      row.classList.toggle('off', !cb.checked);
      onChange();
    };
    row.appendChild(cb);
    if (it.colour) {
      const sw = document.createElement('span');
      sw.className = 'sw';
      sw.style.background = it.colour;
      row.appendChild(sw);
    }
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = it.label;
    row.appendChild(nm);
    if (it.n != null) {
      const n = document.createElement('span');
      n.className = 'n';
      n.textContent = it.n;
      row.appendChild(n);
    }
    row.classList.toggle('off', !cb.checked);
    el.appendChild(row);
  }
  if (!items.length && opts.empty) {
    el.innerHTML = `<div class="note">${esc(opts.empty)}</div>`;
  }
}

function buildOreList() {
  const v = state.data[state.dim].veins;
  $('veincount').textContent = v ? `${v.veins.length}` : '';

  // Route instability was a property of the jar that generated the data, not of the map. Once
  // a bundle has none, the filter is dead UI -- hide it rather than offer a checkbox that
  // cannot change anything. The layer code still honours it for bundles that do.
  const unstable = v ? v.unstable : 0;
  $('veinunstable').parentElement.hidden = !unstable;
  if (!unstable) state.veins.unstable = true;
  if (!v) {
    checkList($('oreList'), [], state.veins.ores, drawVeins, { empty: 'No vein export for this dimension.' });
    return;
  }
  checkList(
    $('oreList'),
    Object.entries(v.ores).map(([ore, n]) => ({ key: ore, label: ore, n, colour: oreColour(ore) })),
    state.veins.ores,
    drawVeins
  );
}

function buildLootControls() {
  const l = state.data[state.dim].loot;
  $('lootcount').textContent = l ? `${l.chests.length} chests` : '';

  const el = $('lootmode');
  el.innerHTML = '';
  for (const [key, label] of [['chest', 'By chest'], ['item', 'By item']]) {
    const b = document.createElement('button');
    b.textContent = label;
    b.dataset.mode = key;
    b.disabled = !l;
    b.onclick = () => {
      state.loot.mode = key;
      buildLootControls();
      drawLoot();
    };
    el.appendChild(b);
  }
  for (const b of el.children) b.classList.toggle('on', b.dataset.mode === state.loot.mode);

  $('lootChestOpts').hidden = state.loot.mode !== 'chest';
  $('lootItemOpts').hidden = state.loot.mode !== 'item';

  if (!l) {
    checkList($('srcList'), [], state.loot.sources, drawLoot, {
      empty: 'No loot export for this dimension.',
    });
    return;
  }

  const maxVal = l.chests.length ? l.chests[0].val : 0;
  const slider = $('lootmin');
  slider.max = maxVal;
  slider.step = Math.max(1, Math.round(maxVal / 200));
  slider.value = state.loot.minVal;
  $('lootminv').textContent = Number(state.loot.minVal).toLocaleString();

  checkList(
    $('srcList'),
    Object.entries(l.sources).map(([s, n]) => ({ key: s, label: s, n })),
    state.loot.sources,
    drawLoot
  );
  buildItemList();
}

function buildItemList() {
  const l = state.data[state.dim].loot;
  if (!l) return;
  const q = state.loot.search.trim().toLowerCase();
  // Always show what is already selected, so a filter cannot hide an active choice.
  let names = Object.keys(l.items).filter(
    (n) => state.loot.items.has(n) || (q && n.toLowerCase().includes(q))
  );
  if (!q) names = names.concat(Object.keys(l.items).slice(0, 40));
  names = [...new Set(names)].slice(0, 200);

  // The number is the total quantity in the world; the hover says how many chests that is
  // spread across, which is what actually lights up on the map.
  checkList(
    $('itemList'),
    names.map((n) => ({
      key: n,
      label: n,
      n: l.items[n].toLocaleString(),
      title: `${l.items[n].toLocaleString()} in total, across ${(
        (l.item_chests || {})[n] || 0
      ).toLocaleString()} chests`,
    })),
    state.loot.items,
    drawLoot,
    { empty: 'No match.' }
  );
}

function buildPoiList() {
  const feats = state.data[state.dim].features;
  const counts = {};
  for (const f of feats) counts[f.kind] = (counts[f.kind] || 0) + 1;

  // Known kinds in their curated order first, then anything else (TF features) alphabetically.
  const known = POI_KINDS.filter((k) => counts[k.key]).map((k) => ({
    key: k.key,
    label: k.label,
    n: counts[k.key],
    colour: k.colour,
    title: k.about,
  }));
  const rest = Object.keys(counts)
    .filter((k) => !POI_KIND[k])
    .sort()
    .map((k) => ({
      key: k,
      label: k.replace(/^tf-/, ''),
      n: counts[k],
      colour: poiColour(k),
    }));

  checkList($('poiList'), known.concat(rest), state.pois, drawPois, {
    empty: 'No POI export for this dimension.',
  });
}

/* ------------------------------------------------------------------ draw */

/**
 * Bottom-to-top order for every vector group.
 *
 * A canvas renderer resolves clicks to the LAST layer drawn, and that is decided by the order
 * layers were added to the map -- so rebuilding any one group (a filter change) would otherwise
 * float it to the top and let it swallow clicks meant for whatever it overlaps. Re-adding all
 * of them in this fixed order after any change keeps the priority stable. Re-adding is cheap:
 * the shapes already exist, only their registration with the renderer moves.
 *
 * Areas (biome squares, structure piece boxes) sit at the bottom because they are large and
 * mostly context; the point markers you actually click sit at the top.
 */
const VECTOR_STACK = ['poiAreas', 'grid', 'veins', 'loot', 'rings', 'poiMarkers'];

function restack() {
  for (const role of VECTOR_STACK) {
    const l = layers[role];
    if (l && map.hasLayer(l)) {
      map.removeLayer(l);
      l.addTo(map);
    }
  }
}

function swap(role, layer) {
  if (layers[role]) map.removeLayer(layers[role]);
  layers[role] = layer;
  if (layer) {
    // A layer rebuilt by a filter change is constructed at whatever VECTOR_SCALE was current;
    // re-apply for the live zoom so it matches the layers around it.
    restyleForZoom([layer], zoomScale(map.getZoom()));
    layer.addTo(map);
  }
  restack();
}

function drawVeins() {
  const v = state.data[state.dim].veins;
  swap('veins', v && state.veins.on ? veinLayer(v, state.spawn, state.veins) : null);
}

function drawLoot() {
  const l = state.data[state.dim].loot;
  swap('loot', l ? lootLayer(l, state.spawn, state.loot) : null);
}

function drawPois() {
  const { areas, markers } = poiLayers(
    state.data[state.dim].features,
    state.spawn,
    state.pois
  );
  swap('poiAreas', areas);
  swap('poiMarkers', markers);
}

function buildClimate() {
  const c = (state.meta.dims[String(state.dim)] || {}).climate;
  $('climatesec').hidden = !c;
  if (!c) return;
}

function drawClimate() {
  const c = (state.meta.dims[String(state.dim)] || {}).climate;
  if (layers.climate) {
    map.removeLayer(layers.climate);
    layers.climate = null;
  }
  if (!state.climate || !c) return;
  layers.climate = L.imageOverlay(
    `${ROOT}/dim${state.dim}/${c.file}`,
    rect(c.x0, c.z0, c.x0 + c.w, c.z0 + c.h),
    { opacity: 1, className: 'pixelated', interactive: false }
  ).addTo(map);
  layers.climate.setZIndex(2); // above the base image, below every vector
}

function drawRef() {
  swap('rings', state.rings ? ringLayer(state.spawn) : null);
  const b = (state.meta.dims[String(state.dim)] || {}).bounds;
  swap(
    'grid',
    state.grid !== 'off' && b
      ? gridLayer({ x0: b.x0, z0: b.z0, x1: b.x0 + b.w, z1: b.z0 + b.h }, Number(state.grid))
      : null
  );
}

function drawAll() {
  drawBase();
  drawClimate();
  drawVeins();
  drawLoot();
  drawPois();
  drawRef();
}

/* ------------------------------------------------------------------ chrome */

function bindChrome() {
  $('toggle').onclick = () => {
    $('panel').classList.toggle('hidden');
    // Wait out the slide, then reclaim (or give back) the width the panel occupied.
    setTimeout(() => {
      map.invalidateSize();
      if (!state.userMoved) fitDim();
    }, 180);
  };

  $('baseopacity').oninput = (e) => {
    state.baseOpacity = e.target.value / 100;
    if (layers.base) {
      if (layers.base.setOpacity) layers.base.setOpacity(state.baseOpacity);
      else layers.base.eachLayer((l) => l.setOpacity(state.baseOpacity));
    }
  };

  $('veinson').onchange = (e) => {
    state.veins.on = e.target.checked;
    drawVeins();
  };
  $('veinunstable').onchange = (e) => {
    state.veins.unstable = e.target.checked;
    drawVeins();
  };
  $('veindist').oninput = (e) => {
    state.veins.maxDist = Number(e.target.value);
    $('veindistv').textContent = e.target.value;
    drawVeins();
  };
  for (const b of document.querySelectorAll('[data-ores]')) {
    b.onclick = () => {
      const v = state.data[state.dim].veins;
      state.veins.ores = b.dataset.ores === 'all' && v ? new Set(Object.keys(v.ores)) : new Set();
      buildOreList();
      drawVeins();
    };
  }

  $('lootmin').oninput = (e) => {
    state.loot.minVal = Number(e.target.value);
    $('lootminv').textContent = state.loot.minVal.toLocaleString();
    drawLoot();
  };
  $('itemsearch').oninput = (e) => {
    state.loot.search = e.target.value;
    buildItemList();
  };
  $('itemclear').onclick = () => {
    state.loot.items.clear();
    buildItemList();
    drawLoot();
  };

  $('climate').onchange = (e) => {
    state.climate = e.target.checked;
    $('climatelegend').hidden = !state.climate;
    drawClimate();
  };
  $('rings').onchange = (e) => {
    state.rings = e.target.checked;
    $('ringlegend').hidden = !state.rings;
    $('ringlegend').innerHTML = RINGS.map(
      (r) => `<span><i style="background:${r.colour}"></i>${r.r}</span>`
    ).join('');
    drawRef();
  };
  $('grid').onchange = (e) => {
    state.grid = e.target.value;
    $('gridnote').textContent =
      state.grid === 'off' ? '' : `${GRIDS[state.grid].label}. Every ` +
      `${GRIDS[state.grid].major}th line is heavier; the white axes are x=0 and z=0.`;
    drawRef();
  };

  // Keep vectors legible as the zoom range is traversed, and say what the scale is: with
  // 1 px = 1 block at zoom 0, "px/block" is the only scale figure that means anything here.
  const onZoom = () => {
    const z = map.getZoom();
    restyleForZoom(
      [layers.poiAreas, layers.veins, layers.loot, layers.poiMarkers, layers.grid],
      zoomScale(z)
    );
    const s = Math.pow(2, z);
    $('zoomreadout').textContent =
      s >= 1 ? `${+s.toFixed(2)} px/block` : `${+(1 / s).toFixed(2)} blocks/px`;
  };
  map.on('zoomend', onZoom);
  map.whenReady(onZoom);

  // Live block-coordinate readout. Inverting pt(): x = lng, z = -lat.
  map.on('mousemove', (e) => {
    const x = Math.floor(e.latlng.lng);
    const z = Math.floor(-e.latlng.lat);
    $('posreadout').textContent = `  x ${x}  z ${z}   chunk ${x >> 4}, ${z >> 4}`;
  });
  map.on('mouseout', () => ($('posreadout').textContent = ''));

  // Click a /tp line to select and copy it. The element holds the bare command -- the label is
  // a sibling -- so there is nothing to strip off, and what gets selected on screen is exactly
  // what lands on the clipboard.
  document.addEventListener('click', (e) => {
    const el = e.target.closest('code.tp');
    if (!el) return;
    const r = document.createRange();
    r.selectNodeContents(el);
    const s = getSelection();
    s.removeAllRanges();
    s.addRange(r);
    navigator.clipboard?.writeText(el.textContent).catch(() => {});
  });
}

boot().catch((e) => {
  document.getElementById('worldline').textContent = `failed: ${e.message}`;
  console.error(e);
});
