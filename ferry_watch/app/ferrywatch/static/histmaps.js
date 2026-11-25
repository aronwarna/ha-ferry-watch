/* Historic maps, map overlays and building ages (mapsources.py, histclean.py, buildings.py).

   - "Historic" map type: old aerial photos or old maps over today's picture. The bar's
     timeline marks only the years that have picture where you are looking (each layer's
     real coverage comes from the NAS as a coarse grid), with map series as spans; ‹ ›
     step through those. "Compare with" picks what lies underneath: today's satellite
     picture, map or terrain map, or another year of this place. "Side by side" puts a
     divider on the map (old left, the comparison right); Fade shows the one under
     through the old picture. A faint dashed line marks where the shown year ends.
     Nowhere to show: the nearest places that have some, one tap away.
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
  const BASE_PANE = "histBasePane";   // another year to compare with, under the shown one
  const EDGE_PANE = "histEdgePane";   // the outline of where the shown year has picture
  const OVERLAY_PANE = "mapOverlayPane";
  const BUILD_Z = 14;          // the NAS keeps building ages per zoom-14 tile
  const BUILD_MIN_Z = 13;      // shown from here in
  const BUILD_MAX_TILES = 30;
  const REFRESH = 6 * 3600e3;
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
    let entries = [];          // what there is here, oldest first: {key, year, to, rows}
    let others = { photo: 0, map: 0 };
    let current = null;
    let swipeX = 0.5;
    let coversRev = -1;
    let coversPending = 0;
    const covers = new Map();  // layer id -> {w, h, bits, mb}

    map.createPane(PANE).style.zIndex = 250;
    map.createPane(BASE_PANE).style.zIndex = 240;
    map.createPane(EDGE_PANE).style.zIndex = 260;
    map.createPane(OVERLAY_PANE).style.zIndex = 390;
    const pane = map.getPane(PANE);
    map.getPane(EDGE_PANE).style.pointerEvents = "none";

    // The map type: what to compare with underneath, the old picture on top.
    const group = L.layerGroup();
    const shown = L.layerGroup().addTo(group);
    const edges = L.layerGroup().addTo(group);
    let baseLayer = null;
    let baseKey = "";
    group.on("add", () => { active = true; bar.hidden = false; update(true); refreshBase(); renderSwipe(); pollCovers(); });
    group.on("remove", () => { active = false; bar.hidden = true; divider.hidden = true; pane.style.clip = ""; });

    // --- what to compare with ---------------------------------------------------------
    const TODAY = [
      { key: "satellite", label: "Satellite today", short: "Today", make: () => today.photo() },
      { key: "map", label: "Map today", short: "Today", make: () => today.map() },
      ...(today.terrain ? [{ key: "terrain", label: "Terrain map today", short: "Today", make: () => today.terrain() }] : []),
    ];
    function baseChoice() {
      const saved = store.get(`fw-hist-base-${kind}`) || (kind === "photo" ? "satellite" : "map");
      if (saved.startsWith("y:")) {
        const e = entries.find((x) => `y:${x.key}` === saved && x !== current);
        if (e) return { key: saved, entry: e, label: entryTitle(e), short: yearText(e) };
      }
      return TODAY.find((t) => t.key === saved) || TODAY[kind === "photo" ? 0 : 1];
    }
    function refreshBase() {
      if (!active) return;
      const choice = baseChoice();
      const key = choice.entry ? `${choice.key}|${choice.entry.rows.map((r) => r.id).join(",")}` : choice.key;
      divider.querySelector(".hd-now").textContent = choice.short;
      renderCompare(choice);
      if (key === baseKey) return;
      baseKey = key;
      if (baseLayer) group.removeLayer(baseLayer);
      if (choice.entry) {
        // Another year: today's satellite picture with that year's old one over it.
        baseLayer = L.layerGroup([today.photo(), ...tileLayers(choice.entry, BASE_PANE, 1)]);
      } else {
        baseLayer = choice.make();
      }
      group.addLayer(baseLayer);
    }

    // --- the bar --------------------------------------------------------------------
    const bar = document.createElement("div");
    bar.id = "hist-bar";
    bar.className = "histbar panel";
    bar.hidden = true;
    bar.innerHTML = `
      <div class="hb-top">
        <div class="segmented hb-kind" role="group" aria-label="What to show">
          <button type="button" data-kind="photo">Aerial photos<span class="hb-count"></span></button>
          <button type="button" data-kind="map">Old maps<span class="hb-count"></span></button>
        </div>
        <span class="hb-busy small muted" hidden>Finding edges…</span>
      </div>
      <div class="hb-main">
        <div class="hb-head">
          <button type="button" class="tb-step hb-prev" aria-label="Earlier">‹</button>
          <div class="hb-title">
            <strong class="hb-year">–</strong>
            <span class="hb-what small muted"></span>
          </div>
          <button type="button" class="tb-step hb-next" aria-label="Later">›</button>
        </div>
        <div class="hb-line" role="slider" tabindex="0" aria-label="Year">
          <div class="hb-track"></div>
          <div class="hb-marks"></div>
          <span class="hb-knob" aria-hidden="true"></span>
        </div>
        <div class="hb-scale small muted"></div>
      </div>
      <div class="hb-opts small">
        <label class="hb-cmp"><span class="muted">Compare with</span>
          <select class="hb-base" aria-label="Compare with"></select></label>
        <button type="button" class="hb-split" aria-pressed="false" title="Side by side: drag the divider">
          <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><rect x="2" y="3" width="16" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10 2v16" stroke="currentColor" stroke-width="1.6"/></svg>
          <span>Side by side</span></button>
        <label class="hb-op" title="See today's picture through the old one"><span class="muted">Fade</span><input type="range" class="player-range hb-opacity" min="15" max="100" step="5" aria-label="Opacity"></label>
        <button type="button" class="hb-savebtn" aria-expanded="false" title="Keep this area's old pictures on the NAS, to see them even if a service is down">
          <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M10 3v9m-4-4 4 4 4-4M4 15h12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span>Save area</span></button>
      </div>
      <div class="hb-save small" hidden>
        <div class="hb-save-opts">
          <label><span class="muted">Years</span><select class="hb-save-scope"></select></label>
          <label><span class="muted">Detail</span><select class="hb-save-z">
            <option value="15">Streets (zoom 15)</option>
            <option value="16">Houses (zoom 16)</option>
            <option value="17">Sharp (zoom 17)</option>
            <option value="19" selected>Best each has</option></select></label>
          <label title="How much of the NAS disk old photos and maps may take; when full, the longest unseen go first"><span class="muted">Space</span><select class="hb-save-space"></select></label>
        </div>
        <p class="hb-save-plan muted">Working out the size…</p>
        <div class="hb-save-bar" hidden><span></span></div>
        <div class="hb-save-actions">
          <button type="button" class="hb-save-go primary">Save</button>
          <button type="button" class="hb-save-close link-button">Close</button>
        </div>
      </div>
      <div class="hb-empty small" hidden></div>
      <p class="hb-credit small muted"></p>`;
    document.body.append(bar);
    const $b = (sel) => bar.querySelector(sel);
    bar.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => setKind(b.dataset.kind)));
    function setKind(k) {
      if (k === kind) return;
      kind = k;
      store.set("fw-hist-kind", kind);
      current = null;  // the same year of the other kind is a different picture
      update(true);
      refreshBase();
    }
    $b(".hb-prev").addEventListener("click", () => step(-1));
    $b(".hb-next").addEventListener("click", () => step(1));
    function step(d) {
      const i = entries.indexOf(current);
      if (i + d >= 0 && i + d < entries.length) pick(i + d);
    }
    $b(".hb-split").addEventListener("click", () => {
      swipe = !swipe;
      store.set("fw-hist-swipe", swipe ? "1" : "0");
      renderSwipe();
    });
    $b(".hb-base").addEventListener("change", (e) => {
      store.set(`fw-hist-base-${kind}`, e.target.value);
      refreshBase();
    });
    $b(".hb-opacity").value = String(opacity);
    $b(".hb-opacity").addEventListener("input", (e) => {
      opacity = Number(e.target.value);
      store.set("fw-hist-opacity", String(opacity));
      shown.eachLayer((l) => l.setOpacity?.(opacity / 100));
    });
    // --- saving the area ahead -----------------------------------------------------------
    // Everything kept stays on the NAS for good; this fetches a whole view's worth now.
    const save = $b(".hb-save");
    let savePoll = null;
    let planSeq = 0;
    function saveRows() {
      const scope = $b(".hb-save-scope").value;
      const pickRows = scope === "shown" ? (current ? current.rows : []) : here(kind);
      return pickRows.filter((r) => r.group === "photo" || r.group === "map");
    }
    function saveBox() {
      const b = map.getBounds();
      return [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((v) => Math.round(v * 1e5) / 1e5);
    }
    function saveBody() {
      return { box: saveBox(), layers: saveRows().map((r) => r.id), zmax: Number($b(".hb-save-z").value),
        title: current ? entryTitle(current) : "" };
    }
    async function post(url, body, method = "POST") {
      const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
      const text = await res.text();
      if (!res.ok) throw new Error(text || res.statusText);
      return JSON.parse(text);
    }
    function fillScope() {
      const n = new Set(here(kind).map((r) => `${r.year}-${r.year_to}`)).size;
      const sel = $b(".hb-save-scope");
      const was = sel.value || "all";
      sel.innerHTML = `<option value="shown">${current ? esc(yearText(current)) : "This year"} only</option>
        <option value="all">All ${n} year${n === 1 ? "" : "s"} here</option>`;
      sel.value = was;
    }
    const fmtN = (n) => n.toLocaleString("en-GB");
    function fillSpace(r) {
      const sel = $b(".hb-save-space");
      if (!r.kept_choices || sel.matches(":focus")) return;
      sel.innerHTML = r.kept_choices.map((g) => `<option value="${g}">${g} GB${r.kept_auto && g === r.kept_gb ? " (auto)" : ""}</option>`).join("");
      sel.value = String(r.kept_gb);
    }
    const fmtMB = (mb) => (mb >= 1000 ? `${(mb / 1000).toFixed(mb >= 10000 ? 0 : 1)} GB` : `${fmtN(Math.max(1, Math.round(mb)))} MB`);
    async function plan() {
      if (save.hidden) return;
      const seq = ++planSeq;
      const p = $b(".hb-save-plan");
      const body = saveBody();
      if (!body.layers.length) { p.textContent = "Nothing to save here."; $b(".hb-save-go").disabled = true; return; }
      p.textContent = "Working out the size…";
      try {
        const r = await post("api/mapsave/plan", body);
        if (seq !== planSeq) return;
        const left = r.tiles - r.have;
        fillSpace(r);
        $b(".hb-save-go").disabled = !left || r.too_big;
        const full = r.kept_mb + r.mb > r.kept_max_mb;
        p.innerHTML = r.too_big
          ? `${fmtN(left)}+ tiles is more than one save takes (${fmtN(r.max_tiles)}). Zoom in or pick less detail.`
          : !left ? `All ${fmtN(r.tiles)} tiles of this view are kept already. ${fmtMB(r.kept_mb)} of ${r.kept_gb} GB used.`
          : `${fmtN(left)} tiles, about ${fmtMB(r.mb)}${r.have ? ` (${fmtN(r.have)} kept already)` : ""}. ${fmtMB(r.kept_mb)} of ${r.kept_gb} GB used${
            full ? ": that's more than fits, so the longest unseen pictures would make room. Pick more space if the disk has it." : "."}`;
      } catch (err) {
        if (seq === planSeq) p.textContent = `Couldn't work out the size: ${err.message}`;
      }
    }
    function showStatus(st) {
      const barEl = $b(".hb-save-bar");
      const p = $b(".hb-save-plan");
      const go = $b(".hb-save-go");
      if (st.running) {
        barEl.hidden = false;
        barEl.firstElementChild.style.width = `${st.total ? (100 * st.done) / st.total : 0}%`;
        p.textContent = `${st.auto ? "Keeping in the background: " : "Saving "}${st.title ? `${st.title}, ` : ""}${fmtN(st.done)} of ${fmtN(st.total)} tiles…${st.auto ? " Saving this view instead pauses it." : ""}`;
        if (st.auto) {
          go.textContent = "Save this view";
          delete go.dataset.stop;
          return true;
        }
        go.textContent = "Stop";
        go.disabled = false;
        go.dataset.stop = "1";
        $b(".hb-savebtn span").textContent = `Saving ${st.total ? Math.floor((100 * st.done) / st.total) : 0}%`;
        return true;
      }
      barEl.hidden = true;
      go.textContent = "Save";
      delete go.dataset.stop;
      $b(".hb-savebtn span").textContent = "Save area";
      if (st.finished && Date.now() / 1000 - st.finished < 120) {
        p.textContent = st.error ? `Stopped: ${st.error}` : st.stopped ? `Stopped after ${fmtN(st.done)} tiles; what was saved stays.`
          : `Saved ${fmtN(st.done - st.failed)} tiles (${Math.round(st.bytes / 1e6)} MB)${st.failed ? `, ${fmtN(st.failed)} couldn't be fetched` : ""}. They now show even if the service is down.`;
        setTimeout(plan, 6000);
      } else {
        plan();
      }
      return false;
    }
    async function pollSave() {
      clearTimeout(savePoll);
      try {
        const st = await post("api/mapsave", null, "GET");
        if (showStatus(st)) savePoll = setTimeout(pollSave, 1500);
      } catch { /* the add-on restarting: try when the panel is opened again */ }
    }
    $b(".hb-savebtn").addEventListener("click", () => {
      save.hidden = !save.hidden;
      $b(".hb-savebtn").setAttribute("aria-expanded", String(!save.hidden));
      if (!save.hidden) { fillScope(); pollSave(); }
    });
    $b(".hb-save-close").addEventListener("click", () => { save.hidden = true; $b(".hb-savebtn").setAttribute("aria-expanded", "false"); });
    $b(".hb-save-scope").addEventListener("change", plan);
    $b(".hb-save-z").addEventListener("change", plan);
    $b(".hb-save-space").addEventListener("change", async (ev) => {
      try {
        await post("api/mapsave/limit", { gb: Number(ev.target.value) });
      } catch (err) {
        $b(".hb-save-plan").textContent = err.message;
      }
      ev.target.blur();
      plan();
    });
    $b(".hb-save-go").addEventListener("click", async () => {
      const go = $b(".hb-save-go");
      try {
        go.disabled = true;
        const st = go.dataset.stop ? await post("api/mapsave", null, "DELETE") : await post("api/mapsave", saveBody());
        showStatus(st);
        pollSave();
      } catch (err) {
        $b(".hb-save-plan").textContent = err.message;
        go.disabled = false;
      }
    });
    let planTimer = null;
    map.on("moveend", () => {
      if (save.hidden || $b(".hb-save-go").dataset.stop) return;
      clearTimeout(planTimer);
      planTimer = setTimeout(() => { fillScope(); plan(); }, 400);
    });

    // Keep map gestures off the bar.
    L.DomEvent.disableClickPropagation(bar);
    L.DomEvent.disableScrollPropagation(bar);

    // --- the timeline ------------------------------------------------------------------
    const line = $b(".hb-line");
    let span = [1900, 2026];
    const pos = (year) => ((year - span[0]) / Math.max(1, span[1] - span[0])) * 100;
    function nearestAt(clientX) {
      const r = line.getBoundingClientRect();
      const year = span[0] + ((clientX - r.left) / Math.max(1, r.width)) * (span[1] - span[0]);
      let best = 0;
      let bestD = Infinity;
      entries.forEach((e, i) => {
        const d = e.to && year >= e.year && year <= e.to ? 0 : Math.min(Math.abs(year - e.year), e.to ? Math.abs(year - e.to) : Infinity);
        if (d < bestD || (d === bestD && e === current)) { bestD = d; best = i; }
      });
      return best;
    }
    let lineDrag = false;
    line.addEventListener("pointerdown", (e) => {
      if (!entries.length) return;
      lineDrag = true;
      line.setPointerCapture(e.pointerId);
      pick(nearestAt(e.clientX));
      e.preventDefault();
    });
    line.addEventListener("pointermove", (e) => { if (lineDrag) pick(nearestAt(e.clientX)); });
    const endDrag = () => { lineDrag = false; };
    line.addEventListener("pointerup", endDrag);
    line.addEventListener("pointercancel", endDrag);
    line.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowDown") { step(-1); e.preventDefault(); }
      if (e.key === "ArrowRight" || e.key === "ArrowUp") { step(1); e.preventDefault(); }
      if (e.key === "Home") { pick(0); e.preventDefault(); }
      if (e.key === "End") { pick(entries.length - 1); e.preventDefault(); }
    });

    function renderLine() {
      const marks = $b(".hb-marks");
      if (!entries.length) { marks.innerHTML = ""; $b(".hb-scale").innerHTML = ""; return; }
      const first = Math.min(...entries.map((e) => e.year));
      const last = Math.max(...entries.map((e) => e.to || e.year));
      // Some air either side, ends on round years, and today at the right for photos.
      const lo = Math.floor((first - 2) / 10) * 10;
      const hi = Math.max(last + 2, kind === "photo" ? new Date().getFullYear() : last + 2);
      span = [lo, Math.max(lo + 10, hi)];
      // Spans (map series) in lanes so overlapping ones stay apart.
      const lanes = [];
      const lane = new Map();
      for (const e of entries.filter((x) => x.to)) {
        let i = lanes.findIndex((end) => end < e.year);
        if (i < 0) { i = lanes.length; lanes.push(0); }
        lanes[i] = e.to;
        lane.set(e, Math.min(i, 3));
      }
      line.style.setProperty("--lanes", String(Math.min(4, lanes.length)));
      marks.innerHTML = entries.map((e) => {
        const i = entries.indexOf(e);
        const cls = `hb-mark${e === current ? " on" : ""}`;
        if (e.to) {
          return `<span class="${cls} span" data-i="${i}" style="left:${pos(e.year)}%;width:${Math.max(0.8, pos(e.to) - pos(e.year))}%;--lane:${lane.get(e)}" title="${esc(yearText(e))}"></span>`;
        }
        return `<span class="${cls}" data-i="${i}" style="left:${pos(e.year)}%" title="${esc(yearText(e))}"></span>`;
      }).join("");
      // Round years under the line, spaced so they never crowd.
      const width = line.clientWidth || 300;
      const every = [10, 20, 25, 50, 100, 200].find((s) => ((span[1] - span[0]) / s) * 44 <= width) || 250;
      const ticks = [];
      for (let y = Math.ceil(span[0] / every) * every; y <= span[1]; y += every) ticks.push(y);
      $b(".hb-scale").innerHTML = ticks.map((y) => `<span style="left:${pos(y)}%">${y}</span>`).join("");
    }
    function placeKnob() {
      const knob = $b(".hb-knob");
      if (!current) { knob.hidden = true; return; }
      knob.hidden = false;
      knob.style.left = `${pos(current.to ? (current.year + current.to) / 2 : current.year)}%`;
      line.setAttribute("aria-valuetext", yearText(current));
      bar.querySelectorAll(".hb-mark").forEach((m) => m.classList.toggle("on", entries[Number(m.dataset.i)] === current));
    }

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
      if (!active || !swipe || !current) {
        pane.style.clip = "";
        map.getPane(EDGE_PANE).style.clip = "";
        divider.hidden = true;
        return;
      }
      const size = map.getSize();
      const x = Math.round(size.x * swipeX);
      const nw = map.containerPointToLayerPoint([0, 0]);
      const se = map.containerPointToLayerPoint(size);
      const cx = map.containerPointToLayerPoint([x, 0]).x;
      pane.style.clip = `rect(${nw.y}px, ${cx}px, ${se.y}px, ${nw.x}px)`;
      map.getPane(EDGE_PANE).style.clip = pane.style.clip;
      divider.hidden = false;
      divider.style.left = `${x}px`;
    }
    function renderSwipe() {
      $b(".hb-split").setAttribute("aria-pressed", String(swipe));
      clip();
    }
    map.on("move zoom resize", clip);

    // --- coverage --------------------------------------------------------------------
    // Each old photo and map comes with a coarse grid of where it really has picture
    // (mapsources.py works it out once): the bar offers only the years that show here.
    const HALF = 20037508.342789244;
    const merc = (lat, lon) => [lon / 180 * HALF, Math.log(Math.tan(Math.PI / 4 + Math.max(-85, Math.min(85, lat)) * Math.PI / 360)) * HALF / Math.PI];
    const unmerc = (x, y) => [(2 * Math.atan(Math.exp(y / HALF * Math.PI)) - Math.PI / 2) * 180 / Math.PI, x / HALF * 180];
    const boxMerc = (b) => [...merc(b[0], b[1]), ...merc(b[2], b[3])];
    function decode(row, [w, h, rle]) {
      const bits = new Uint8Array(w * h);
      let at = 0;
      let on = 0;
      for (const part of rle.split(",")) {
        const n = Number(part);
        if (on) bits.fill(1, at, Math.min(w * h, at + n));
        at += n;
        on ^= 1;
      }
      return { w, h, bits, mb: boxMerc(row.box) };
    }
    async function loadCovers(rev) {
      if (rev === coversRev) return;
      try {
        const res = await api(`api/mapcovers?v=${rev}`);
        const byId = new Map(catalogue.map((r) => [r.id, r]));
        for (const [id, c] of Object.entries(res.covers || {})) if (byId.has(id)) covers.set(id, decode(byId.get(id), c));
        coversRev = rev;
      } catch (err) {
        console.warn("map covers", err);
      }
    }
    // While the NAS is still working coverage out (the first hours after an update), look again now and then.
    let pollTimer = 0;
    function pollCovers() {
      clearTimeout(pollTimer);
      if (!active || coversPending <= 0) return;
      pollTimer = setTimeout(async () => { await load(); pollCovers(); }, 60000);
    }
    function cellsOf(c, mb) {
      const cw = (c.mb[2] - c.mb[0]) / c.w;
      const ch = (c.mb[3] - c.mb[1]) / c.h;
      return [Math.floor((mb[0] - c.mb[0]) / cw), Math.floor((c.mb[3] - mb[3]) / ch),
        Math.floor((mb[2] - c.mb[0]) / cw), Math.floor((c.mb[3] - mb[1]) / ch)];
    }
    function hasPicture(row, mb) {
      const c = covers.get(row.id);
      if (!c) {
        const b = boxMerc(row.box);
        return b[0] < mb[2] && b[2] > mb[0] && b[1] < mb[3] && b[3] > mb[1];
      }
      const [i0, j0, i1, j1] = cellsOf(c, mb);
      for (let j = Math.max(0, j0); j <= Math.min(c.h - 1, j1); j++) {
        for (let i = Math.max(0, i0); i <= Math.min(c.w - 1, i1); i++) if (c.bits[j * c.w + i]) return true;
      }
      return false;
    }
    // The part of the view that counts as "here": its middle, so a year that only
    // touches a corner isn't offered as if it showed this place.
    function focusBox() {
      const b = map.getBounds();
      const [x0, y0, x1, y1] = [...merc(b.getSouth(), b.getWest()), ...merc(b.getNorth(), b.getEast())];
      const mx = (x1 - x0) * 0.22;
      const my = (y1 - y0) * 0.22;
      return [x0 + mx, y0 + my, x1 - mx, y1 - my];
    }
    const zoomFits = (r, z) => z >= (r.min_zoom || 0) - 1 && z <= (r.max_zoom || 19) + 3;
    // Nearest place with picture of this kind: {where, at: [lat, lon], km, years}.
    function nearest(k) {
      const c0 = map.getCenter();
      const [cx, cy] = merc(c0.lat, c0.lng);
      const best = new Map();
      for (const r of catalogue) {
        if (r.group !== k || r.wide) continue;
        const c = covers.get(r.id);
        let at = null;
        let d = Infinity;
        if (c) {
          const cw = (c.mb[2] - c.mb[0]) / c.w;
          const ch = (c.mb[3] - c.mb[1]) / c.h;
          for (let j = 0; j < c.h; j += 1) {
            for (let i = 0; i < c.w; i += 1) {
              if (!c.bits[j * c.w + i]) continue;
              const x = c.mb[0] + (i + 0.5) * cw;
              const y = c.mb[3] - (j + 0.5) * ch;
              const dd = (x - cx) ** 2 + (y - cy) ** 2;
              if (dd < d) { d = dd; at = [x, y]; }
            }
          }
        } else {
          const b = boxMerc(r.box);
          at = [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
          d = (at[0] - cx) ** 2 + (at[1] - cy) ** 2;
        }
        if (!at) continue;
        const prev = best.get(r.where);
        const years = [r.year, r.to || r.year_to || r.year];
        if (!prev || d < prev.d) best.set(r.where, { where: r.where, d, at, years: prev ? [Math.min(prev.years[0], years[0]), Math.max(prev.years[1], years[1])] : years });
        else prev.years = [Math.min(prev.years[0], years[0]), Math.max(prev.years[1], years[1])];
      }
      const lat = c0.lat * Math.PI / 180;
      return [...best.values()].sort((a, b) => a.d - b.d).slice(0, 3).map((p) => ({
        ...p, at: unmerc(...p.at), km: Math.sqrt(p.d) * Math.cos(lat) / 1000,
      }));
    }

    // --- years here --------------------------------------------------------------------
    const yearText = (e) => (e.to ? `${e.year}–${e.to}` : String(e.year || e.key));
    function entryTitle(e) {
      const labels = [...new Set(e.rows.map((r) => r.label))];
      return `${yearText(e)} ${labels.length === 1 && kind === "map" ? labels[0].toLowerCase() : (kind === "photo" ? "photo" : "map")}`;
    }
    function here(k) {
      const z = map.getZoom();
      const focus = focusBox();
      return catalogue.filter((r) => r.group === k && zoomFits(r, z) && hasPicture(r, focus));
    }

    function update(force) {
      if (!active) return;
      const rows = here(kind);
      others = { photo: kind === "photo" ? rows.length : here("photo").length, map: kind === "map" ? rows.length : here("map").length };
      const byKey = new Map();
      for (const r of rows) {
        const key = r.year ? `${r.year}${r.year_to ? `-${r.year_to}` : ""}` : r.label;
        if (!byKey.has(key)) byKey.set(key, { key, year: r.year, to: r.year_to || null, rows: [] });
        byKey.get(key).rows.push(r);
      }
      const next = [...byKey.values()].sort((a, b) => (a.year || 9999) - (b.year || 9999) || (a.to || 0) - (b.to || 0) || a.key.localeCompare(b.key));
      const same = next.length === entries.length && next.every((e, i) => e.key === entries[i].key && e.rows.length === entries[i].rows.length);
      bar.querySelectorAll("[data-kind]").forEach((b) => {
        b.setAttribute("aria-pressed", String(b.dataset.kind === kind));
        const n = new Set(here(b.dataset.kind).map((r) => `${r.year}-${r.year_to}`)).size;
        b.querySelector(".hb-count").textContent = n ? ` ${n}` : "";
      });
      $b(".hb-busy").hidden = coversPending <= 0;
      if (same && !force) { drawEdges(); return; }
      entries = next;
      if (current) current = entries.find((e) => e.key === current.key) || null;
      let idx = current ? entries.indexOf(current) : entries.findIndex((e) => e.key === wanted);
      if (idx < 0 && entries.length) {
        // The nearest year to the one wanted, else the newest old one.
        const want = Number(String(current?.year || wanted).slice(0, 4));
        idx = want ? entries.reduce((best, e, i) => (Math.abs(e.year - want) < Math.abs(entries[best].year - want) ? i : best), 0) : 0;
      }
      const empty = $b(".hb-empty");
      empty.hidden = entries.length > 0;
      bar.classList.toggle("is-empty", !entries.length);
      renderLine();
      if (!entries.length) {
        current = null;
        shown.clearLayers();
        edges.clearLayers();
        placeKnob();
        clip();
        renderEmpty();
        $b(".hb-credit").textContent = "";
        return;
      }
      pick(idx, true);
      refreshBase();
    }

    function renderEmpty() {
      const empty = $b(".hb-empty");
      const what = kind === "photo" ? "old aerial photos" : "old maps";
      const z = map.getZoom();
      const focus = focusBox();
      const zoomIn = catalogue.filter((r) => r.group === kind && z < (r.min_zoom || 0) - 1 && hasPicture(r, focus));
      const other = kind === "photo" ? "map" : "photo";
      const places = nearest(kind).filter((p) => p.km > 0.5);
      empty.innerHTML = `
        <p class="hb-empty-title">${zoomIn.length ? `Zoom in to see ${what} of this area.` : `No ${what} of this spot.`}</p>
        ${others[other] ? `<p class="muted">${others[other]} old ${other === "map" ? "map" : "photo"}${others[other] === 1 ? "" : "s"} here. <button type="button" class="link-button" data-switch>Show ${other === "map" ? "old maps" : "aerial photos"}</button></p>` : ""}
        ${places.length && !zoomIn.length ? `<p class="muted">Nearest:</p><div class="hb-jump">${places.map((p, i) => `<button type="button" data-go="${i}"><b>${esc(p.where)}</b><span>${p.km < 1 ? "here" : `${p.km < 10 ? p.km.toFixed(1) : Math.round(p.km)} km`} · ${p.years[0]}${p.years[1] > p.years[0] ? `–${p.years[1]}` : ""}</span></button>`).join("")}</div>` : ""}`;
      empty.querySelector("[data-switch]")?.addEventListener("click", () => setKind(other));
      if (zoomIn.length) {
        empty.insertAdjacentHTML("beforeend", '<div class="hb-jump"><button type="button" data-zoom><b>Zoom in</b></button></div>');
        empty.querySelector("[data-zoom]").addEventListener("click", () => map.setZoom(Math.min(...zoomIn.map((r) => r.min_zoom || 0))));
      }
      empty.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => {
        const p = places[Number(b.dataset.go)];
        map.flyTo(p.at, Math.max(map.getZoom(), kind === "photo" ? 13 : 11), { duration: 0.8 });
      }));
    }

    function tileLayers(entry, paneName, alpha) {
      // Whole-country pictures go under the cities' own, sharper ones.
      const area = (r) => (r.box[2] - r.box[0]) * (r.box[3] - r.box[1]);
      return [...entry.rows].sort((a, b) => area(b) - area(a)).map((r, i) => L.tileLayer(
        `api/maptile/${encodeURIComponent(r.id)}/{z}/{x}/{y}.png`, {
          zIndex: i + 1, pane: paneName, minZoom: 0, maxZoom: 20, minNativeZoom: r.min_zoom, maxNativeZoom: r.max_zoom,
          bounds: boxBounds(r.box), opacity: alpha, attribution: r.attribution, className: "hist-tile",
        }));
    }

    function pick(i, quiet) {
      if (!entries.length) return;
      i = Math.max(0, Math.min(entries.length - 1, i));
      const entry = entries[i];
      $b(".hb-prev").disabled = i === 0;
      $b(".hb-next").disabled = i === entries.length - 1;
      if (current && current.key === entry.key && shown.getLayers().length && !quiet) return;
      const changed = !current || current.key !== entry.key || !shown.getLayers().length;
      current = entry;
      wanted = entry.key;
      store.set("fw-hist-year", wanted);
      $b(".hb-year").textContent = yearText(entry);
      const labels = [...new Set(entry.rows.map((r) => r.label))];
      const wheres = [...new Set(entry.rows.map((r) => r.where))];
      $b(".hb-what").textContent = kind === "map" ? `${labels.join(" · ")} · ${wheres.join(", ")}` : `Aerial photo · ${wheres.join(", ")}`;
      $b(".hb-what").title = $b(".hb-what").textContent;
      divider.querySelector(".hd-then").textContent = yearText(entry);
      $b(".hb-credit").textContent = [...new Set(entry.rows.map((r) => r.attribution))].join(" · ");
      placeKnob();
      if (changed) {
        // Swap without a blank: the new year's tiles go on top, the old ones leave once loaded.
        const old = shown.getLayers();
        const layers = tileLayers(entry, PANE, opacity / 100);
        let waiting = layers.length;
        const done = () => { if (--waiting <= 0) old.forEach((l) => shown.removeLayer(l)); };
        for (const layer of layers) {
          layer.once("load", done);
          shown.addLayer(layer);
        }
        setTimeout(() => old.forEach((l) => shown.removeLayer(l)), 4000);
        if (baseChoice().entry) refreshBase();
        renderCompare(baseChoice());
      }
      drawEdges();
      clip();
    }

    // The outline of where the shown year has picture: a faint line, so it's clear where
    // the old picture ends and today's begins.
    let edgesKey = "";
    function drawEdges() {
      const key = current ? `${current.key}|${current.rows.map((r) => r.id).join(",")}|${coversRev}|${map.getZoom() > 15}` : "";
      if (key === edgesKey) return;
      edgesKey = key;
      edges.clearLayers();
      if (!current) return;
      for (const r of current.rows) {
        const c = covers.get(r.id);
        if (!c || c.bits.every((b) => b)) continue;
        const cw = (c.mb[2] - c.mb[0]) / c.w;
        const ch = (c.mb[3] - c.mb[1]) / c.h;
        const ll = (i, j) => unmerc(c.mb[0] + i * cw, c.mb[3] - j * ch);
        const on = (i, j) => i >= 0 && j >= 0 && i < c.w && j < c.h && c.bits[j * c.w + i];
        const segs = [];
        // Horizontal edges, joined along each row; then vertical ones along each column.
        for (let j = 0; j <= c.h; j++) {
          let start = -1;
          for (let i = 0; i <= c.w; i++) {
            const edge = i < c.w && on(i, j - 1) !== on(i, j);
            if (edge && start < 0) start = i;
            if (!edge && start >= 0) { segs.push([ll(start, j), ll(i, j)]); start = -1; }
          }
        }
        for (let i = 0; i <= c.w; i++) {
          let start = -1;
          for (let j = 0; j <= c.h; j++) {
            const edge = j < c.h && on(i - 1, j) !== on(i, j);
            if (edge && start < 0) start = j;
            if (!edge && start >= 0) { segs.push([ll(i, start), ll(i, j)]); start = -1; }
          }
        }
        if (!segs.length) continue;
        edges.addLayer(L.polyline(segs, { pane: EDGE_PANE, color: "#0f172a", weight: 3, opacity: 0.25, interactive: false }));
        edges.addLayer(L.polyline(segs, { pane: EDGE_PANE, color: "#fff", weight: 1.25, opacity: 0.85, dashArray: "4 4", interactive: false }));
      }
    }

    function renderCompare(choice) {
      const sel = $b(".hb-base");
      const years = entries.filter((e) => e !== current);
      sel.innerHTML = `<optgroup label="Today">${TODAY.map((t) => `<option value="${t.key}">${esc(t.label)}</option>`).join("")}</optgroup>`
        + (years.length ? `<optgroup label="Another year here">${years.map((e) => `<option value="y:${esc(e.key)}">${esc(entryTitle(e))}</option>`).join("")}</optgroup>` : "");
      sel.value = choice.key;
    }

    map.on("moveend", () => update(false));

    // --- overlays (Layers switches) --------------------------------------------------
    const overlayOn = new Map();
    const overlayLayers = new Map();
    // Property ids are written by the panel from the Land Survey's id points (parcels.py):
    // sharp at every zoom, one size, never cut at a tile edge. Only if those can't be had do
    // the Land Survey's ready-made picture tiles stand in.
    const PARCEL_Z = 15;
    const PARCEL_MIN_Z = 15;
    const parcelLayer = L.layerGroup();
    const parcelTiles = new Map();
    let parcelRow = null;
    let parcelFallback = false;
    let parcelSeq = 0;
    async function loadParcels() {
      if (!parcelRow || !overlayOn.get(parcelRow.id) || parcelFallback) return;
      parcelLayer.clearLayers();
      if (map.getZoom() < PARCEL_MIN_Z) return;
      const my = ++parcelSeq;
      const b = map.getBounds();
      const nw = map.project(b.getNorthWest(), PARCEL_Z).divideBy(256).floor();
      const se = map.project(b.getSouthEast(), PARCEL_Z).divideBy(256).floor();
      const keys = [];
      for (let x = nw.x; x <= se.x; x++) for (let y = nw.y; y <= se.y; y++) keys.push(`${x}/${y}`);
      if (keys.length > 40) return;
      let failed = 0;
      await Promise.all(keys.map(async (k) => {
        if (Array.isArray(parcelTiles.get(k))) return;
        try {
          const r = await fetch(`api/parcels/${k}`);
          if (!r.ok) throw new Error(String(r.status));
          parcelTiles.set(k, (await r.json()).rows || []);
          if (parcelTiles.size > 200) parcelTiles.delete(parcelTiles.keys().next().value);
        } catch { failed += 1; }
      }));
      if (my !== parcelSeq || !overlayOn.get(parcelRow.id)) return;
      if (failed === keys.length && keys.length) {  // no id points to be had: the picture tiles
        parcelFallback = true;
        setOverlay(parcelRow, true);
        return;
      }
      // Largest-first isn't known, so first come first served; a label never covers another.
      const taken = [];
      const z = map.getZoom();
      const w = 112;
      for (const k of keys) {
        for (const [lat, lon, id] of parcelTiles.get(k) || []) {
          const pt = map.latLngToContainerPoint([lat, lon]);
          if (taken.some((t) => Math.abs(t.x - pt.x) < w && Math.abs(t.y - pt.y) < 24)) continue;
          taken.push(pt);
          const icon = L.divIcon({ className: "parcel-id", iconSize: [0, 0], html: `<span>${esc(id)}</span>` });
          const m = L.marker([lat, lon], { icon, keyboard: false, pane: OVERLAY_PANE });
          m.on("click", () => {
            const box = document.createElement("div");
            box.innerHTML = `<strong>${esc(id)}</strong><div class="muted small">Property id (kiinteistötunnus)</div>`
              + `<div style="margin-top:6px"><button type="button" class="chip-btn">Copy</button></div>`;
            box.querySelector("button").addEventListener("click", () => navigator.clipboard?.writeText(id).catch(() => {}));
            L.popup({ className: "aid-popup", offset: [0, -6] }).setLatLng([lat, lon]).setContent(box).openOn(map);
          });
          parcelLayer.addLayer(m);
        }
      }
    }
    map.on("moveend", loadParcels);

    function setOverlay(row, on) {
      overlayOn.set(row.id, on);
      store.set(`fw-mo-${row.id}`, on ? "1" : "0");
      if (row.id === "property-ids" && !parcelFallback) {
        parcelRow = row;
        if (on) { parcelLayer.addTo(map); loadParcels(); } else { map.removeLayer(parcelLayer); parcelLayer.clearLayers(); }
        return;
      }
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
        note: buildNote || `Every building as a dot coloured by the year it was finished, from zoom 13. Tap one for its year, use and floors. The capital region from HSY's register (with addresses), the rest of Finland from the national building register (Ryhti). ${failingNote("buildings")}`.trim(),
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
        coversPending = res.covers_pending || 0;
        if (res.covers_rev) await loadCovers(res.covers_rev);
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
