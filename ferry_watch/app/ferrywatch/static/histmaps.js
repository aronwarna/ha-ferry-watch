/* Old aerial photos, old maps, map overlays and building ages (mapsources.py, buildings.py).

   - "Then & now" map type: old aerial photos or old maps of the year picked on a slider,
     over today's picture (satellite for photos, the map for old maps). The slider only
     holds the years there is something for in view. "Compare with today" puts a divider
     on the map: the old picture left of it, today right of it. An opacity slider fades
     the old one over today's.
   - Overlays from the catalogue (property boundaries, seabed type, …) as switches in
     Layers, on top of any map type.
   - Building ages: every building in the Helsinki region as a dot coloured by the year
     it was built; tap one for its year, use, floors and address.

   The panel lists whatever /api/mapsources gives, so a layer added to mapsources.py's
   SOURCES shows up here without any change. app.js makes one of these with
   FerryHist.create and asks it for rows() in Layers. */
(() => {
  "use strict";

  const PANE = "histPane";
  const OVERLAY_PANE = "mapOverlayPane";
  const BUILD_Z = 14;          // the NAS keeps building ages per zoom-14 tile
  const BUILD_MIN_Z = 13;      // shown from here in
  const BUILD_MAX_TILES = 30;
  const REFRESH = 6 * 3600e3;
  const PLACES = [
    { name: "Espoo", at: [60.168, 24.66], z: 13 },
    { name: "Helsinki", at: [60.168, 24.945], z: 13 },
  ];
  // Building age colours, oldest to newest.
  const AGE_STOPS = [
    [1700, "#5b1a0f"], [1900, "#9a3412"], [1930, "#d97706"], [1950, "#eab308"], [1970, "#84cc16"],
    [1990, "#22c55e"], [2005, "#06b6d4"], [2025, "#2563eb"],
  ];

  function mix(a, b, t) {
    const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
    const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
    return `rgb(${pa.map((v, i) => Math.round(v + (pb[i] - v) * t)).join(",")})`;
  }
  function ageColor(year) {
    if (!year) return "#94a3b8";
    if (year <= AGE_STOPS[0][0]) return AGE_STOPS[0][1];
    for (let i = 1; i < AGE_STOPS.length; i++) {
      const [y1, c1] = AGE_STOPS[i];
      const [y0, c0] = AGE_STOPS[i - 1];
      if (year <= y1) return mix(c0, c1, (year - y0) / (y1 - y0));
    }
    return AGE_STOPS[AGE_STOPS.length - 1][1];
  }

  function boxBounds(box) { return L.latLngBounds([box[0], box[1]], [box[2], box[3]]); }

  const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  function create({ map, api, store, today, onChange }) {
    let catalogue = [];
    let sources = [];
    let active = false;
    let kind = store.get("fw-hist-kind") === "map" ? "map" : "photo";
    let wanted = store.get("fw-hist-year") || "";
    let opacity = Number(store.get("fw-hist-opacity") || 100);
    let swipe = store.get("fw-hist-swipe") === "1";
    let entries = [];
    let current = null;
    let swipeX = 0.5;

    map.createPane(PANE).style.zIndex = 250;
    map.createPane(OVERLAY_PANE).style.zIndex = 390;
    const pane = map.getPane(PANE);

    // The map type: today's picture underneath, the old one on top.
    const group = L.layerGroup();
    const base = { photo: today.photo(), map: today.map() };
    const shown = L.layerGroup().addTo(group);
    group.on("add", () => { active = true; refreshBase(); update(true); bar.hidden = false; renderSwipe(); });
    group.on("remove", () => { active = false; bar.hidden = true; divider.hidden = true; pane.style.clip = ""; });

    function refreshBase() {
      for (const k of Object.keys(base)) {
        if (k === kind) group.addLayer(base[k]);
        else group.removeLayer(base[k]);
      }
    }

    // --- the bar --------------------------------------------------------------------
    const bar = document.createElement("div");
    bar.id = "hist-bar";
    bar.className = "histbar panel";
    bar.hidden = true;
    bar.innerHTML = `
      <div class="hb-top">
        <div class="segmented hb-kind" role="group" aria-label="What to show">
          <button type="button" data-kind="photo">Aerial photos</button>
          <button type="button" data-kind="map">Old maps</button>
        </div>
        <span class="hb-where small muted"></span>
      </div>
      <div class="hb-year-row">
        <button type="button" class="tb-step hb-prev" aria-label="Earlier">‹</button>
        <strong class="hb-year">–</strong>
        <input type="range" class="player-range hb-range" min="0" max="0" step="1" aria-label="Year">
        <button type="button" class="tb-step hb-next" aria-label="Later">›</button>
      </div>
      <div class="hb-ticks small muted"></div>
      <div class="hb-opts small">
        <label class="hb-check"><input type="checkbox" class="switch hb-swipe"> Compare with today</label>
        <label class="hb-op"><span class="muted">Fade</span><input type="range" class="player-range hb-opacity" min="15" max="100" step="5" aria-label="Opacity"></label>
      </div>
      <div class="hb-empty small" hidden></div>
      <p class="hb-credit small muted"></p>`;
    document.body.append(bar);
    const $b = (sel) => bar.querySelector(sel);
    bar.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => {
      kind = b.dataset.kind;
      store.set("fw-hist-kind", kind);
      current = null;  // the same year of the other kind is a different picture
      refreshBase();
      update(true);
    }));
    $b(".hb-range").addEventListener("input", (e) => pick(Number(e.target.value)));
    $b(".hb-prev").addEventListener("click", () => pick(entries.indexOf(current) - 1));
    $b(".hb-next").addEventListener("click", () => pick(entries.indexOf(current) + 1));
    $b(".hb-swipe").checked = swipe;
    $b(".hb-swipe").addEventListener("change", (e) => {
      swipe = e.target.checked;
      store.set("fw-hist-swipe", swipe ? "1" : "0");
      renderSwipe();
    });
    $b(".hb-opacity").value = String(opacity);
    $b(".hb-opacity").addEventListener("input", (e) => {
      opacity = Number(e.target.value);
      store.set("fw-hist-opacity", String(opacity));
      shown.eachLayer((l) => l.setOpacity(opacity / 100));
    });
    // Keep map gestures off the bar.
    L.DomEvent.disableClickPropagation(bar);
    L.DomEvent.disableScrollPropagation(bar);

    // --- the divider for comparing ---------------------------------------------------
    const divider = document.createElement("div");
    divider.className = "hist-divider";
    divider.hidden = true;
    divider.innerHTML = '<span class="hd-label hd-then"></span><span class="hd-handle" aria-hidden="true">‹ ›</span><span class="hd-label hd-now">Today</span>';
    map.getContainer().append(divider);
    L.DomEvent.disableClickPropagation(divider);
    let dragging = false;
    divider.addEventListener("pointerdown", (e) => {
      dragging = true;
      divider.setPointerCapture(e.pointerId);
      map.dragging.disable();
      e.preventDefault();
    });
    divider.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const rect = map.getContainer().getBoundingClientRect();
      swipeX = Math.min(0.98, Math.max(0.02, (e.clientX - rect.left) / rect.width));
      clip();
    });
    const stop = () => { if (dragging) { dragging = false; map.dragging.enable(); } };
    divider.addEventListener("pointerup", stop);
    divider.addEventListener("pointercancel", stop);

    function clip() {
      if (!active || !swipe) { pane.style.clip = ""; divider.hidden = true; return; }
      const size = map.getSize();
      const x = Math.round(size.x * swipeX);
      const nw = map.containerPointToLayerPoint([0, 0]);
      const se = map.containerPointToLayerPoint(size);
      const cx = map.containerPointToLayerPoint([x, 0]).x;
      pane.style.clip = `rect(${nw.y}px, ${cx}px, ${se.y}px, ${nw.x}px)`;
      divider.hidden = false;
      divider.style.left = `${x}px`;
    }
    function renderSwipe() { clip(); }
    map.on("move zoom resize", clip);

    // --- years in view ---------------------------------------------------------------
    function inView() {
      const view = map.getBounds().pad(0.1);
      const z = map.getZoom();
      return catalogue.filter((r) => r.group === kind && z >= (r.min_zoom || 0) - 3 && boxBounds(r.box).intersects(view));
    }

    function update(force) {
      if (!active) return;
      const rows = inView();
      const byKey = new Map();
      for (const r of rows) {
        const key = r.year ? String(r.year) : r.label;
        if (!byKey.has(key)) byKey.set(key, { key, year: r.year, rows: [] });
        byKey.get(key).rows.push(r);
      }
      const next = [...byKey.values()].sort((a, b) => (a.year || 9999) - (b.year || 9999) || a.key.localeCompare(b.key));
      const same = next.length === entries.length && next.every((e, i) => e.key === entries[i].key && e.rows.length === entries[i].rows.length);
      if (same && !force) return;
      entries = next;
      bar.querySelectorAll("[data-kind]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.kind === kind)));
      const range = $b(".hb-range");
      range.max = String(Math.max(0, entries.length - 1));
      let idx = entries.findIndex((e) => e.key === (current?.key || wanted));
      if (idx < 0 && entries.length) {
        // The nearest year to the one wanted, else the oldest.
        const want = Number(current?.year || wanted);
        idx = want ? entries.reduce((best, e, i) => (Math.abs((e.year || 9999) - want) < Math.abs((entries[best].year || 9999) - want) ? i : best), 0) : 0;
      }
      renderTicks();
      const empty = $b(".hb-empty");
      empty.hidden = entries.length > 0;
      bar.classList.toggle("is-empty", !entries.length);
      if (!entries.length) {
        current = null;
        shown.clearLayers();
        $b(".hb-year").textContent = "–";
        $b(".hb-where").textContent = "";
        const all = [...new Set(catalogue.filter((r) => r.group === kind).map((r) => r.where))];
        empty.innerHTML = `<p>${kind === "photo" ? "No old aerial photos here." : "No old maps here."} `
          + `${all.length ? `They cover ${esc(all.join(", "))}.` : "The services haven't answered yet."}</p>`
          + `<div class="hb-jump">${PLACES.map((p, i) => `<button type="button" data-go="${i}">Go to ${esc(p.name)}</button>`).join("")}</div>`;
        empty.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => {
          const p = PLACES[Number(b.dataset.go)];
          map.setView(p.at, Math.max(map.getZoom(), p.z));
        }));
        $b(".hb-credit").textContent = "";
        return;
      }
      pick(idx, true);
    }

    function renderTicks() {
      const ticks = $b(".hb-ticks");
      if (entries.length < 2) { ticks.textContent = ""; return; }
      const first = entries[0];
      const last = entries[entries.length - 1];
      ticks.innerHTML = `<span>${esc(first.key)}</span><span>${entries.length} ${kind === "photo" ? "years" : "maps"}</span><span>${esc(last.key)}</span>`;
    }

    function pick(i, quiet) {
      if (!entries.length) return;
      i = Math.max(0, Math.min(entries.length - 1, i));
      const entry = entries[i];
      $b(".hb-range").value = String(i);
      $b(".hb-prev").disabled = i === 0;
      $b(".hb-next").disabled = i === entries.length - 1;
      if (current && current.key === entry.key && !quiet) return;
      if (current && current.key === entry.key && shown.getLayers().length) {
        current = entry;
        return;
      }
      current = entry;
      wanted = entry.key;
      store.set("fw-hist-year", wanted);
      $b(".hb-year").textContent = entry.year ? String(entry.year) : entry.key;
      const labels = [...new Set(entry.rows.map((r) => (kind === "map" ? `${r.label}, ${r.where}` : r.where)))];
      $b(".hb-where").textContent = labels.join(" · ");
      divider.querySelector(".hd-then").textContent = entry.year ? String(entry.year) : entry.key;
      $b(".hb-credit").textContent = [...new Set(entry.rows.map((r) => r.attribution))].join(" · ");
      // Swap without a blank: the new year's tiles go on top, the old ones leave once loaded.
      const old = shown.getLayers();
      let waiting = entry.rows.length;
      const done = () => { if (--waiting <= 0) old.forEach((l) => shown.removeLayer(l)); };
      for (const r of entry.rows) {
        const layer = L.tileLayer(`api/maptile/${encodeURIComponent(r.id)}/{z}/{x}/{y}.png`, {
          pane: PANE, minZoom: 0, maxZoom: 20, minNativeZoom: r.min_zoom, maxNativeZoom: r.max_zoom,
          bounds: boxBounds(r.box), opacity: opacity / 100, attribution: r.attribution,
        });
        layer.once("load", done);
        shown.addLayer(layer);
      }
      setTimeout(() => old.forEach((l) => shown.removeLayer(l)), 4000);
      clip();
    }

    map.on("moveend", () => update(false));

    // --- overlays (Layers switches) --------------------------------------------------
    const overlayOn = new Map();
    const overlayLayers = new Map();
    function setOverlay(row, on) {
      overlayOn.set(row.id, on);
      store.set(`fw-mo-${row.id}`, on ? "1" : "0");
      let layer = overlayLayers.get(row.id);
      if (on && !layer) {
        // A daily picture's tiles are asked for by date, so yesterday's never come from the browser's cache.
        layer = L.tileLayer(`api/maptile/${encodeURIComponent(row.id)}/{z}/{x}/{y}.png${row.daily ? `?d=${row.daily}` : ""}`, {
          pane: OVERLAY_PANE, minZoom: row.z_shown ?? row.min_zoom, maxZoom: 20,
          minNativeZoom: row.min_zoom, maxNativeZoom: row.max_zoom, bounds: boxBounds(row.box),
          opacity: row.opacity ?? 1, attribution: row.attribution,
        });
        overlayLayers.set(row.id, layer);
      }
      if (layer) {
        if (on) layer.addTo(map);
        else map.removeLayer(layer);
      }
    }

    // --- building ages ---------------------------------------------------------------
    let buildingsOn = store.get("fw-buildings") === "1";
    const buildRenderer = L.canvas({ padding: 0.2, pane: OVERLAY_PANE });
    const buildLayer = L.layerGroup();
    const buildTiles = new Map();  // "x/y" -> rows | Promise
    let buildRows = [];
    let buildNote = "";
    function tileKeys() {
      const b = map.getBounds();
      const nw = map.project(b.getNorthWest(), BUILD_Z).divideBy(256).floor();
      const se = map.project(b.getSouthEast(), BUILD_Z).divideBy(256).floor();
      const out = [];
      for (let x = nw.x; x <= se.x; x++) for (let y = nw.y; y <= se.y; y++) out.push(`${x}/${y}`);
      return out;
    }
    async function loadBuildings() {
      if (!buildingsOn || map.getZoom() < BUILD_MIN_Z) {
        buildLayer.clearLayers();
        buildRows = [];
        buildNote = buildingsOn ? "Zoom in closer to see the buildings." : "";
        return;
      }
      const keys = tileKeys();
      if (keys.length > BUILD_MAX_TILES) { buildNote = "Zoom in closer to see the buildings."; buildLayer.clearLayers(); return; }
      buildNote = "";
      await Promise.all(keys.map(async (k) => {
        if (buildTiles.has(k)) return buildTiles.get(k);
        const p = fetch(`api/buildings/${k}`).then((r) => (r.ok ? r.json() : { rows: [] })).then((j) => j.rows || [])
          .catch(() => []);
        buildTiles.set(k, p);
        const rows = await p;
        buildTiles.set(k, rows);
        if (buildTiles.size > 300) buildTiles.delete(buildTiles.keys().next().value);
        return rows;
      }));
      if (!buildingsOn) return;
      buildRows = keys.flatMap((k) => (Array.isArray(buildTiles.get(k)) ? buildTiles.get(k) : []));
      drawBuildings();
    }
    function drawBuildings() {
      buildLayer.clearLayers();
      const r = map.getZoom() >= 16 ? 5 : map.getZoom() >= 15 ? 3.5 : 2.4;
      for (const row of buildRows) {
        L.circleMarker([row[0], row[1]], {
          renderer: buildRenderer, radius: r, weight: map.getZoom() >= 16 ? 1 : 0, color: "#fff",
          fillColor: ageColor(row[2]), fillOpacity: 0.9, interactive: false,
        }).addTo(buildLayer);
      }
    }
    function setBuildings(on) {
      buildingsOn = on;
      store.set("fw-buildings", on ? "1" : "0");
      if (on) { buildLayer.addTo(map); loadBuildings(); } else { map.removeLayer(buildLayer); buildRows = []; }
    }
    if (buildingsOn) buildLayer.addTo(map);
    map.on("moveend", () => { if (buildingsOn) loadBuildings().then(() => onChange?.()); });
    map.on("zoomend", () => { if (buildingsOn && buildRows.length) drawBuildings(); });

    function buildingAt(latlng) {
      if (!buildingsOn || !buildRows.length) return null;
      const p = map.latLngToContainerPoint(latlng);
      let best = null;
      let bestD = 14 * 14;
      for (const row of buildRows) {
        const q = map.latLngToContainerPoint([row[0], row[1]]);
        const d = (q.x - p.x) ** 2 + (q.y - p.y) ** 2;
        if (d < bestD) { bestD = d; best = row; }
      }
      return best;
    }
    function tap(latlng) {
      const row = buildingAt(latlng);
      if (!row) return false;
      const [lat, lon, year, use, floors, street, facade, frame] = row;
      const lines = [
        `<strong>${year ? `Built ${year}` : "Year not known"}</strong>`,
        use && esc(use),
        [floors && `${floors} floor${floors === 1 ? "" : "s"}`, facade && `facade: ${esc(facade.toLowerCase())}`,
          frame && `frame: ${esc(frame.toLowerCase())}`].filter(Boolean).join(" · "),
        street && `<span class="muted">${esc(street)}</span>`,
        year && `<span class="muted">${new Date().getFullYear() - year} years old</span>`,
      ].filter(Boolean);
      L.popup({ className: "aid-popup", offset: [0, -4] }).setLatLng([lat, lon]).setContent(lines.join("<br>")).openOn(map);
      return true;
    }

    function ageLegend() {
      const div = document.createElement("div");
      div.className = "age-legend small";
      const stops = AGE_STOPS.slice(1);
      const grad = stops.map(([, c], i) => `${c} ${Math.round((i / (stops.length - 1)) * 100)}%`).join(",");
      const names = stops.map(([y], i) => (i === 0 ? `–${y}` : i === stops.length - 1 ? "now" : String(y)));
      div.innerHTML = `<i style="background:linear-gradient(90deg,${grad})"></i><span class="muted">${names.map((n) => `<span>${n}</span>`).join("")}</span>`;
      return div;
    }

    // --- Layers rows ------------------------------------------------------------------
    function rows() {
      const out = catalogue.filter((r) => r.group === "overlay").map((r) => ({
        title: r.id === r.source ? r.title : `${r.label}${r.year ? ` (${r.year})` : ""}`, color: r.id.startsWith("property") ? "#dc2626" : "#a16207", checked: Boolean(overlayOn.get(r.id)),
        note: `${r.info || ""} ${failingNote(r.source)}`.trim(),
        onChange: async (on) => { setOverlay(r, on); },
      }));
      out.push({
        title: "Building ages", color: "#d97706", checked: buildingsOn,
        count: buildingsOn && buildRows.length ? `${buildRows.length} in view` : "",
        note: buildNote || `Every building in Helsinki, Espoo, Vantaa and Kauniainen as a dot coloured by the year it was finished, from zoom 13. Tap one for its year, use, floors and address. From HSY's building register. ${failingNote("buildings")}`.trim(),
        extra: buildingsOn ? ageLegend() : null,
        onChange: async (on) => { setBuildings(on); },
      });
      return out;
    }
    function failingNote(id) {
      const s = sources.find((x) => x.id === id);
      return s?.failing ? `Couldn't reach it just now (${s.failing}).` : "";
    }

    // Settings › Data sources rows.
    function status() { return sources; }

    async function load() {
      try {
        const res = await api("api/mapsources");
        catalogue = res.layers || [];
        sources = res.sources || [];
      } catch (err) {
        console.warn("map sources", err);
      }
      for (const r of catalogue) {
        if (r.group === "overlay" && store.get(`fw-mo-${r.id}`) === "1" && !overlayOn.get(r.id)) setOverlay(r, true);
      }
      update(true);
      onChange?.();
    }
    load();
    setInterval(load, REFRESH);
    if (buildingsOn) loadBuildings();

    return {
      layer: group, rows, tap, status, reload: load,
      years: () => entries.map((e) => e.key),
      _pick: (i) => pick(i),
    };
  }

  // On a phone the map type buttons don't all fit: show the chosen one with a ▾ and open
  // the full list as a drop-down when it's tapped.
  function compactPicker(box) {
    if (!box) return;
    box.classList.add("type-picker");
    // A wrapper keeps the closed picker's place in the top bar while the list is open.
    const wrap = document.createElement("span");
    wrap.className = "type-wrap";
    box.before(wrap);
    wrap.append(box);
    const setOpen = (open) => {
      if (open) {
        wrap.style.width = `${box.offsetWidth}px`;
        wrap.style.height = `${box.offsetHeight}px`;
      }
      box.classList.toggle("open", open);
      if (!open) { wrap.style.width = ""; wrap.style.height = ""; }
    };
    box.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      if (!window.matchMedia("(max-width: 700px)").matches) return;
      if (!box.classList.contains("open")) {
        setOpen(true);
        e.stopImmediatePropagation();
        e.preventDefault();
        return;
      }
      setOpen(false);
    }, true);
    document.addEventListener("click", (e) => { if (!box.contains(e.target)) setOpen(false); });
  }

  window.FerryHist = { create, compactPicker, ageColor };
})();
