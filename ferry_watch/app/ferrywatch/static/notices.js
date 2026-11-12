/* Warnings and waterway structures on the map (notices.py):
   - nautical warnings (Traficom): a diamond with "!" at each, its line or area dashed in
     the same amber; tap for what, where and until when;
   - sea weather warnings (FMI): the warned sea areas tinted yellow, orange or red, and a
     warning sign in the conditions pill when one covers your own waters;
   - opening bridges, locks and lock canals (Väylävirasto), with clearance and size;
   - the speed limits a planned route runs through, listed in the route panel and marked
     on the route, with the time they add at the planned speed.
   app.js makes one of these with FerryNotices.create and shows rows() in Layers. */
(() => {
  "use strict";

  const AMBER = "#b45309";
  const WARN_COLORS = { yellow: "#eab308", orange: "#f97316", red: "#dc2626" };
  const REFRESH = 10 * 60000;
  const STRUCT_MIN_Z = 9;
  const KIND_NAMES = { bridge: "Opening bridge", lock: "Lock", canal: "Lock canal" };
  // Väylävirasto's field names in plain words, for the ones seen; others show as they are.
  const FIELD_NAMES = {
    nimi: "Name", tyyppi: "Type", kayttotapa: "Operated", pituus: "Length (m)", leveys: "Width (m)",
    kynnyssyvyys: "Depth over the sill (m)", kulkusyvyys: "Navigable depth (m)", alikulkukorkeus: "Clearance (m)",
    alikulkukorkeus_suljettuna: "Clearance when closed (m)", puhelin: "Phone", vhf: "VHF channel", lisatieto: "More",
  };

  function create({ map, api, esc, store, fmtWhen, onChange }) {
    const on = {
      nautical: store.get("fw-nautical") !== "0",
      seawarn: store.get("fw-seawarn") === "1",
      structures: store.get("fw-structures") !== "0",
      limitAreas: store.get("fw-limitAreas") === "1",
    };
    const layers = { nautical: L.layerGroup(), seawarn: L.layerGroup(), structures: L.layerGroup(), limits: L.layerGroup(), limitAreas: L.layerGroup() };
    const data = { nautical: null, weather: null, structures: null };
    const errors = {};
    const loadedAt = {};
    const busy = {};

    const icon = (cls, html, size = 22) => L.divIcon({ className: `notice-icon ${cls}`, html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
    const ICONS = {
      nautical: () => icon("notice-nautical", '<svg viewBox="-12 -12 24 24" aria-hidden="true"><path d="M0 -10 L10 0 L0 10 L-10 0 Z"/><text y="4.5" text-anchor="middle">!</text></svg>', 24),
      bridge: () => icon("notice-structure", '<svg viewBox="-12 -12 24 24" aria-hidden="true"><circle r="10.5"/><path d="M-7 3 H7 M-7 3 V-1 M7 3 V-1 M-7 -1 Q0 -8 7 -1" /></svg>'),
      lock: () => icon("notice-structure", '<svg viewBox="-12 -12 24 24" aria-hidden="true"><circle r="10.5"/><path d="M-6 -5 V5 M6 -5 V5 M-6 0 L0 -3.5 L6 0" /></svg>'),
      canal: () => icon("notice-structure", '<svg viewBox="-12 -12 24 24" aria-hidden="true"><circle r="10.5"/><path d="M-4 -6 V6 M4 -6 V6" /></svg>'),
    };

    // --- nautical warnings ---
    function validity(w) {
      const parts = [];
      if (w.start && w.start * 1000 > Date.now()) parts.push(`from ${fmtWhen(w.start)}`);
      parts.push(w.end ? `until ${fmtWhen(w.end)}` : "until further notice");
      return parts.join(", ");
    }
    function nauticalPopup(w) {
      const where = [w.place, w.area].filter(Boolean).join(" · ");
      const rest = Object.entries(w.props || {});
      return `<div class="notice-pop"><strong>${esc(w.type)}${w.number ? ` ${esc(w.number)}` : ""}</strong>`
        + `${where ? `<div class="muted">${esc(where)}</div>` : ""}`
        + `${w.text ? `<p>${esc(w.text)}</p>` : ""}`
        + `<div class="small muted">In force ${esc(validity(w))}${w.published ? ` · published ${esc(fmtWhen(w.published))}` : ""}</div>`
        + `${rest.length ? `<details${w.text ? "" : " open"}><summary class="small">All details</summary><dl class="facts small">${rest.map(([k, v]) => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(v)}</dd>`).join("")}</dl></details>` : ""}`
        + `<div class="small muted">Navigational warning, Traficom</div></div>`;
    }
    function drawNautical() {
      layers.nautical.clearLayers();
      for (const w of data.nautical?.warnings || []) {
        const popup = nauticalPopup(w);
        // Only the sign takes the tap: an area that did would swallow taps meant for the map
        // (route points, ships, the chart's own marks).
        for (const s of w.shapes || []) {
          if (s.kind === "area") {
            L.polygon(s.points, { color: AMBER, weight: 2, dashArray: "6 5", fillColor: AMBER, fillOpacity: 0.1, interactive: false }).addTo(layers.nautical);
          } else if (s.kind === "line") {
            L.polyline(s.points, { color: AMBER, weight: 3, dashArray: "6 5", interactive: false }).addTo(layers.nautical);
          }
        }
        if (w.at) {
          L.marker(w.at, { icon: ICONS.nautical(), keyboard: false, zIndexOffset: 450, title: `${w.type}: ${w.place || w.area || ""}` })
            .bindPopup(popup, { className: "aid-popup", maxWidth: 300, offset: [0, -8] }).addTo(layers.nautical);
        }
      }
    }

    // --- sea weather warnings ---
    const hm = (ts) => fmtWhen(ts);
    function span(w) {
      if (w.onset && w.expires) return `${hm(w.onset)} to ${hm(w.expires)}`;
      return w.expires ? `until ${hm(w.expires)}` : w.onset ? `from ${hm(w.onset)}` : "";
    }
    function weatherPopup(w, area) {
      return `<div class="notice-pop"><strong><span class="warn-dot" style="background:${WARN_COLORS[w.color] || WARN_COLORS.yellow}"></span>${esc(w.event)}</strong>`
        + `${area ? `<div class="muted">${esc(area)}</div>` : ""}`
        + `${w.text ? `<p>${esc(w.text)}</p>` : ""}`
        + `<div class="small muted">${esc(span(w))}</div>`
        + `<div class="small muted">Warning, Finnish Meteorological Institute</div></div>`;
    }
    const warnIcon = (color) => icon("notice-warn", `<svg viewBox="-12 -12 24 24" aria-hidden="true"><path d="M0 -9.5 L10.5 8.5 H-10.5 Z" fill="${color}"/><path class="mark" d="M0 -3 V3 M0 6 V6.4"/></svg>`, 26);
    function drawWeather() {
      layers.seawarn.clearLayers();
      const view = map.getBounds();
      // Least severe first, so a red area lies on top of a yellow one. The tint itself
      // doesn't take taps (it covers whole sea areas); a sign in the part of each area
      // that is in view does.
      for (const w of [...(data.weather?.warnings || [])].reverse()) {
        const color = WARN_COLORS[w.color] || WARN_COLORS.yellow;
        for (const area of w.areas || []) {
          const poly = L.polygon(area.rings, { color, weight: 1.5, fillColor: color, fillOpacity: 0.16, interactive: false }).addTo(layers.seawarn);
          const b = poly.getBounds();
          if (!b.isValid() || !b.intersects(view)) continue;
          const at = L.latLngBounds(
            [Math.max(b.getSouth(), view.getSouth()), Math.max(b.getWest(), view.getWest())],
            [Math.min(b.getNorth(), view.getNorth()), Math.min(b.getEast(), view.getEast())]).getCenter();
          L.marker(at, { icon: warnIcon(color), keyboard: false, zIndexOffset: 200, title: `${w.event}: ${area.name || ""}` })
            .bindPopup(weatherPopup(w, area.name), { className: "aid-popup", maxWidth: 300, offset: [0, -8] }).addTo(layers.seawarn);
        }
      }
    }
    // The pill at the top of the map, and the list in the conditions sheet.
    function renderHere() {
      const here = data.weather?.here || [];
      const pill = document.getElementById("pill-warn");
      if (pill) {
        pill.hidden = !here.length;
        if (here.length) {
          const worst = here[0];
          pill.style.setProperty("--warn", WARN_COLORS[worst.color] || WARN_COLORS.yellow);
          pill.title = here.map((w) => `${w.event}: ${w.text || ""} (${span(w)})`).join("\n");
          pill.querySelector("span").textContent = worst.wind_ms ? `${Math.round(worst.wind_ms)} m/s` : "";
        }
      }
      const box = document.getElementById("sea-warnings");
      if (box) {
        box.hidden = !here.length;
        box.innerHTML = here.map((w) => `<div class="sea-warning" style="--warn:${WARN_COLORS[w.color] || WARN_COLORS.yellow}">`
          + `<strong>${esc(w.event)}</strong>${w.area ? ` <span class="muted">${esc(w.area)}</span>` : ""}`
          + `<div>${esc(w.text || "")}</div><div class="small muted">${esc(span(w))}</div></div>`).join("");
      }
    }

    // --- bridges, locks, canals ---
    const metres = (v) => `${String(Math.round(v * 10) / 10).replace(/\.0$/, "")} m`;
    function structurePopup(s) {
      const facts = [];
      if (s.height_m) facts.push(["Clearance", metres(s.height_m)]);
      if (s.width_m) facts.push(["Width", metres(s.width_m)]);
      if (s.length_m) facts.push(["Length", metres(s.length_m)]);
      if (s.depth_m) facts.push(["Depth", metres(s.depth_m)]);
      const rest = Object.entries(s.props || {}).filter(([k]) => !/nimi|name|namn/i.test(k));
      return `<div class="notice-pop"><strong>${esc(s.name || KIND_NAMES[s.kind])}</strong>`
        + `<div class="muted">${esc(KIND_NAMES[s.kind])}</div>`
        + `${facts.length ? `<dl class="facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : ""}`
        + `${rest.length ? `<details><summary class="small">All details</summary><dl class="facts small">${rest.map(([k, v]) => `<dt>${esc(FIELD_NAMES[k.toLowerCase()] || k.replace(/_/g, " "))}</dt><dd>${esc(v)}</dd>`).join("")}</dl></details>` : ""}`
        + `<div class="small muted">Väylävirasto. Check opening times and heights before you go.</div></div>`;
    }
    function drawStructures() {
      layers.structures.clearLayers();
      if (map.getZoom() < STRUCT_MIN_Z) return;
      const view = map.getBounds().pad(0.2);
      for (const s of data.structures?.items || []) {
        if (!s.at || !view.contains(s.at)) continue;
        const popup = structurePopup(s);
        for (const shape of s.shapes || []) {
          L.polyline(shape.points, { color: "#0e7490", weight: 3, opacity: 0.7 })
            .bindPopup(popup, { className: "aid-popup", maxWidth: 300 }).addTo(layers.structures);
        }
        const label = s.kind === "bridge" && s.height_m ? metres(s.height_m) : null;
        const m = L.marker(s.at, { icon: (ICONS[s.kind] || ICONS.canal)(), keyboard: false, zIndexOffset: 300, title: s.name || KIND_NAMES[s.kind] })
          .bindPopup(popup, { className: "aid-popup", maxWidth: 300, offset: [0, -8] }).addTo(layers.structures);
        if (label && map.getZoom() >= 12) m.bindTooltip(label, { permanent: true, direction: "right", offset: [10, 0], className: "notice-label" });
      }
    }

    // --- loading ---
    const SOURCES = {
      nautical: { url: "api/notices/nautical", key: "nautical", draw: drawNautical, every: REFRESH },
      seawarn: { url: "api/notices/weather", key: "weather", draw: () => { drawWeather(); renderHere(); }, every: REFRESH },
      structures: { url: "api/notices/structures", key: "structures", draw: drawStructures, every: 6 * 3600000 },
    };
    async function load(name, force) {
      const src = SOURCES[name];
      if (busy[name] || (!force && loadedAt[name] && Date.now() - loadedAt[name] < src.every)) return;
      busy[name] = true;
      try {
        data[src.key] = await api(src.url);
        errors[name] = data[src.key].error || null;
        loadedAt[name] = Date.now();
      } catch (err) {
        errors[name] = String(err.message || err).slice(0, 120);
        loadedAt[name] = Date.now() - src.every + 5 * 60000; // again in five minutes
      } finally {
        busy[name] = false;
      }
      src.draw();
      onChange?.();
    }
    function apply() {
      for (const name of ["nautical", "seawarn", "structures"]) { // (limitAreas: see loadAreas)
        if (on[name]) {
          if (!map.hasLayer(layers[name])) layers[name].addTo(map);
          load(name);
        } else {
          layers[name].remove();
        }
      }
      load("seawarn"); // the pill shows a warning for your waters whether or not the areas are drawn
    }
    function set(name, value) {
      on[name] = value;
      store.set(`fw-${name}`, value ? "1" : "0");
      apply();
    }
    map.on("moveend", () => {
      loadAreas();
      if (on.structures) drawStructures();
      if (on.seawarn && !map._popup) drawWeather(); // the signs follow the view, but not from under an open popup
    });
    setInterval(() => { if (!document.hidden) apply(); }, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) apply(); });

    // What Layers shows: one switch row each (app.js draws them).
    function rows() {
      const n = data.nautical?.warnings?.length;
      const w = data.weather?.warnings?.length;
      const s = data.structures?.items?.length;
      const fail = (name) => (errors[name] ? `Couldn't be read just now (${errors[name]}). ` : "");
      return [
        { title: "Nautical warnings", color: AMBER, checked: on.nautical, count: on.nautical && n != null ? `${n} in force` : "",
          note: `${fail("nautical")}The navigational warnings for Finnish waters, from Traficom: firing exercises, works in a fairway, marks missing or moved, things adrift. A diamond with "!" at each, its line or area dashed; tap for the text and how long it holds. A single faulty light or buoy is in "Faulty lights and buoys".`,
          onChange: async (v) => set("nautical", v) },
        { title: "Sea weather warnings", color: WARN_COLORS.orange, checked: on.seawarn, count: w != null ? `${w} in force` : "",
          note: `${fail("seawarn")}The Finnish Meteorological Institute's warnings for the sea areas (wind, waves, icing): the warned areas tinted yellow, orange or red; tap one for the text. A warning for your own waters always shows as a sign in the conditions pill, with or without this switch.`,
          onChange: async (v) => set("seawarn", v) },
        { title: "Bridges, locks and canals", color: "#0e7490", checked: on.structures, count: on.structures && s != null ? `${s}` : "",
          note: `${fail("structures")}Väylävirasto's opening bridges, locks and lock canals, from zoom ${STRUCT_MIN_Z}, with clearance and size where the data has them. Fixed bridges are on the chart itself.`,
          onChange: async (v) => set("structures", v) },
        { title: "Speed limit areas", color: "#dc2626", checked: on.limitAreas, count: on.limitAreas && la.areas ? `${la.areas.length} in view` : "",
          note: `${la.error ? `Couldn't be read just now (${la.error}). ` : ""}Väylävirasto's speed limit areas, from zoom ${AREAS_MIN_Z}: a red dashed outline, tinted, with the limit in km/h. They always show while you plan a route.`,
          onChange: async (v) => { set("limitAreas", v); loadAreas(); } },
      ];
    }

    // --- speed limit areas in view (with the Layers switch, and always while planning a route) ---
    const AREAS_MIN_Z = 10;
    const la = { areas: null, bounds: null, error: null, seq: 0, routeOpen: false };
    function drawAreas() {
      layers.limitAreas.clearLayers();
      const z = map.getZoom(), view = map.getBounds();
      for (const a of la.areas || []) {
        const poly = L.polygon(a.ring, { color: "#dc2626", weight: 1.5, dashArray: "5 5", fillColor: "#dc2626", fillOpacity: 0.07, interactive: false }).addTo(layers.limitAreas);
        const b = poly.getBounds();
        if (z < 11 || !view.intersects(b)) continue;
        // The sign where the area shows: in the middle of what is in view, if that is in it.
        const south = Math.max(b.getSouth(), view.getSouth()), north = Math.min(b.getNorth(), view.getNorth());
        const west = Math.max(b.getWest(), view.getWest()), east = Math.min(b.getEast(), view.getEast());
        let at = [(south + north) / 2, (west + east) / 2];
        if (!inRing(at, a.ring)) at = [b.getCenter().lat, b.getCenter().lng];
        if (!inRing(at, a.ring)) continue;
        const size = map.latLngToContainerPoint(b.getNorthWest()).distanceTo(map.latLngToContainerPoint(b.getSouthEast()));
        if (size < 60) continue;
        L.marker(at, { interactive: false, keyboard: false, icon: L.divIcon({ className: "limit-sign area", html: `<span>${Math.round(a.kmh)}</span>`, iconSize: [22, 22], iconAnchor: [11, 11] }) }).addTo(layers.limitAreas);
      }
    }
    function inRing(p, ring) {
      let inside = false;
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const a = ring[i], b = ring[j];
        if ((a[0] > p[0]) !== (b[0] > p[0]) && p[1] < ((b[1] - a[1]) * (p[0] - a[0])) / (b[0] - a[0]) + a[1]) inside = !inside;
      }
      return inside;
    }
    async function loadAreas() {
      const want = (on.limitAreas || la.routeOpen) && map.getZoom() >= AREAS_MIN_Z;
      if (!want) { layers.limitAreas.remove(); return; }
      if (!map.hasLayer(layers.limitAreas)) layers.limitAreas.addTo(map);
      const view = map.getBounds();
      if (la.bounds && la.bounds.contains(view)) { drawAreas(); return; }
      const b = view.pad(0.5), seq = ++la.seq;
      try {
        const got = await api(`api/speed-limits?bbox=${b.getSouth().toFixed(4)},${b.getWest().toFixed(4)},${b.getNorth().toFixed(4)},${b.getEast().toFixed(4)}`);
        if (seq !== la.seq) return;
        la.areas = got.areas || [];
        la.bounds = got.too_big ? null : b;
        la.error = null;
      } catch (err) {
        if (seq !== la.seq) return;
        la.error = String(err.message || err).slice(0, 120);
        la.bounds = null;
      }
      drawAreas();
      onChange?.();
    }
    function routeOpen(open) {
      if (open === la.routeOpen) return;
      la.routeOpen = open;
      loadAreas();
    }

    // --- speed limits on the planned route ---
    const rl = { key: "", seq: 0, timer: 0, data: null, error: null, busy: false };
    const nm = (m) => (m / 1852 < 10 ? (m / 1852).toFixed(1) : String(Math.round(m / 1852)));
    function renderLimits() {
      const box = document.getElementById("route-limits");
      if (!box) return;
      layers.limits.clearLayers();
      const d = rl.data;
      if (!rl.key || (!d && !rl.error)) { box.hidden = true; return; }
      box.hidden = false;
      if (rl.error) {
        box.innerHTML = `<span class="small muted">Speed limits couldn't be checked: ${esc(rl.error)}</span>`;
        return;
      }
      if (!d.stretches.length) {
        box.innerHTML = '<span class="small muted">No speed limits on this route.</span>';
        return;
      }
      if (!map.hasLayer(layers.limits)) layers.limits.addTo(map);
      const groups = new Map();
      for (const s of d.stretches) {
        L.polyline(s.line, { color: "#f59e0b", weight: 9, opacity: 0.55, interactive: false, lineCap: "butt" }).addTo(layers.limits);
        const mid = s.line[Math.floor(s.line.length / 2)];
        L.marker(mid, { interactive: false, keyboard: false, icon: L.divIcon({ className: "limit-sign", html: `<span>${Math.round(s.kmh)}</span>`, iconSize: [22, 22], iconAnchor: [11, 11] }) }).addTo(layers.limits);
        const g = groups.get(s.kmh) || { kmh: s.kmh, kn: s.kn, m: 0, n: 0 };
        g.m += s.to_m - s.from_m;
        g.n += 1;
        groups.set(s.kmh, g);
      }
      const parts = [...groups.values()].sort((a, b) => a.kmh - b.kmh)
        .map((g) => `<span class="limit-chip"><b>${Math.round(g.kmh)} km/h</b> (${g.kn} kn) for ${nm(g.m)} NM${g.n > 1 ? ` in ${g.n} places` : ""}</span>`);
      const extra = d.extra_min >= 1 ? ` At your speed they add about <b>${Math.round(d.extra_min)} min</b>.` : "";
      box.innerHTML = `<div class="small"><strong>Speed limits</strong> ${parts.join(" ")}${extra}</div>`;
    }
    /* points: the planned route ([[lat, lon], ...]) or null when there is none; ready: false
       while it is still being planned or dragged (the last answer stays up). */
    function route(points, speed, ready) {
      if (!points || points.length < 2) {
        clearTimeout(rl.timer);
        rl.key = ""; rl.data = null; rl.error = null; rl.seq++;
        layers.limits.remove();
        renderLimits();
        return;
      }
      if (!ready) return;
      const key = JSON.stringify([points.map((p) => [Math.round(p[0] * 1e4), Math.round(p[1] * 1e4)]), speed]);
      if (key === rl.key) return;
      clearTimeout(rl.timer);
      rl.timer = setTimeout(async () => {
        const seq = ++rl.seq;
        rl.key = key;
        try {
          const got = await api("api/route-limits", { method: "POST", body: { points, speed } });
          if (seq !== rl.seq) return;
          rl.data = got; rl.error = null;
        } catch (err) {
          if (seq !== rl.seq) return;
          rl.data = null; rl.error = String(err.message || err).slice(0, 120); rl.key = "";
        }
        renderLimits();
      }, 600);
    }

    apply();
    return { rows, route, routeOpen, apply, get here() { return data.weather?.here || []; }, get limits() { return rl.data; } };
  }

  window.FerryNotices = { create };
})();
