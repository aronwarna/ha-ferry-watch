/* Nature reserves and national parks (OpenStreetMap, through the add-on's api/reserves).
   Tap inside one to fill it with green and see its card: names, kind, size, year protected,
   who manages it, access rules and links. Layers › "Highlight nature reserves" tints and
   names every one in view, on any map. The outlines are fetched for the part of the map
   in view, only while tapping can find something or the switch is on. The geometry maths
   is kept apart so the tests can run it under Node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FerryReserves = api;
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  // --- geometry -----------------------------------------------------------------------

  // Inside the reserve's rings ([[lat, lon], ...] each), even-odd: holes are rings too.
  function contains(reserve, lat, lon) {
    const b = reserve.bbox; // west, south, east, north
    if (lon < b[0] || lon > b[2] || lat < b[1] || lat > b[3]) return false;
    let inside = false;
    for (const ring of reserve.rings) {
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [yi, xi] = ring[i], [yj, xj] = ring[j];
        if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
      }
    }
    return inside;
  }

  // The reserves at a place, smallest first (a reserve inside a national park is the one
  // meant).
  function at(reserves, lat, lon) {
    return reserves.filter((r) => contains(r, lat, lon)).sort((a, b) => a.area_m2 - b.area_m2);
  }

  // A place for the name: inside the reserve, near the middle of its box.
  function labelPoint(reserve) {
    const [w, s, e, n] = reserve.bbox;
    const mid = [(s + n) / 2, (w + e) / 2];
    if (contains(reserve, mid[0], mid[1])) return mid;
    let best = null, bestD = Infinity;
    const steps = 9;
    for (let i = 1; i < steps; i++) {
      for (let j = 1; j < steps; j++) {
        const lat = s + ((n - s) * i) / steps, lon = w + ((e - w) * j) / steps;
        const d = (lat - mid[0]) ** 2 + ((lon - mid[1]) * 0.5) ** 2;
        if (d < bestD && contains(reserve, lat, lon)) { best = [lat, lon]; bestD = d; }
      }
    }
    return best || reserve.rings[0][0];
  }

  function areaText(m2) {
    if (!m2) return "";
    const ha = m2 / 10000;
    if (ha < 1) return `${Math.round(m2)} m²`;
    if (ha < 100) return `${ha < 10 ? ha.toFixed(1) : Math.round(ha)} ha`;
    const km2 = ha / 100;
    return `${km2 < 10 ? km2.toFixed(1) : Math.round(km2).toLocaleString("en-GB")} km²`;
  }

  // "1994-05-12" -> "1994"; anything else as it is.
  function sinceText(text) {
    const m = /^(\d{4})(-\d{2})?(-\d{2})?$/.exec(text || "");
    return m ? m[1] : text || "";
  }

  // The links on a reserve's card, as [label, href].
  function links(r) {
    const out = [];
    if (r.website) out.push(["Website", r.website]);
    const name = r.name || r.other_names?.[0];
    const state = /national park|strict nature/i.test(r.kind) || /mets[äa]hallitus|forststyrelsen/i.test(r.operator || "");
    if (name && state && !/luontoon\.fi/.test(r.website || "")) {
      out.push(["Find on luontoon.fi", `https://duckduckgo.com/?q=${encodeURIComponent(`site:luontoon.fi ${name}`)}`]);
    }
    if (r.wikipedia) out.push(["Wikipedia", r.wikipedia]);
    if (r.wikidata) out.push(["Wikidata", `https://www.wikidata.org/wiki/${r.wikidata}`]);
    const kind = { r: "relation", w: "way", n: "node" }[r.id[0]];
    if (kind) out.push(["OpenStreetMap", `https://www.openstreetmap.org/${kind}/${r.id.slice(1)}`]);
    return out;
  }

  // --- on the map ---------------------------------------------------------------------

  const COLOR = "#2f7d3d";
  const ALL_STYLE = { color: COLOR, weight: 1.5, opacity: 0.75, fillColor: "#3f9a4f", fillOpacity: 0.14, dashArray: null };
  const PICKED_STYLE = { color: "#1f6b2e", weight: 3, opacity: 0.95, fillColor: "#3f9a4f", fillOpacity: 0.3, dashArray: null };
  const ALL_MIN_ZOOM = 10;
  const LABEL_MIN_PX = 70; // reserves narrower than this on screen aren't named
  const MAX_LABELS = 40;

  /* map: the Leaflet map; api(url): JSON from the add-on; esc(text): HTML-escaped text;
     store: { get, set } for this browser; canTap(): whether a tap on the map may look for
     a reserve (the drawn chart at zoom 11 and in shows their borders); onChange(): the
     switch's count or note changed. */
  function create({ map, api, esc, store, canTap = () => false, onChange = () => {} }) {
    const pane = map.createPane("reserves");
    pane.style.zIndex = 360; // over the chart tiles and weather, under tracks, ships and popups
    pane.style.pointerEvents = "none";
    const renderer = L.canvas({ pane: "reserves", padding: 0.3 });
    const group = L.layerGroup().addTo(map);
    const labels = L.layerGroup().addTo(map);
    const known = new Map(); // id -> reserve
    let all = store.get("fw-reserves-all") === "1";
    let box = null, busy = false, timer = 0, note = "", inView = 0;
    let picked = null, pickedLayer = null, popup = null, pickedBefore = null;

    // A tap on a ship's track or an area's outline (a path that let the click go on to
    // the map) is that thing's, not the reserve's under it.
    let pathClicked = false;
    if (typeof map._fireDOMEvent === "function" && typeof map._findEventTargets === "function") {
      const fire = map._fireDOMEvent;
      map._fireDOMEvent = function (e, type, targets) {
        if (type === "click") {
          const found = (targets || []).concat(this._findEventTargets(e, type));
          pathClicked = found.length > 0 && found[0] !== this;
        }
        return fire.apply(this, arguments);
      };
    }

    function wanted() {
      return (all && map.getZoom() >= ALL_MIN_ZOOM) || canTap();
    }

    async function load() {
      clearTimeout(timer);
      if (!wanted() || document.hidden) { draw(); return; }
      const view = map.getBounds();
      if (box?.contains(view)) { draw(); return; }
      if (busy) return;
      const pad = view.pad(0.3);
      busy = true;
      try {
        const f = (v) => v.toFixed(4);
        const data = await api(`api/reserves?bbox=${f(pad.getWest())},${f(pad.getSouth())},${f(pad.getEast())},${f(pad.getNorth())}`);
        for (const r of data.reserves) known.set(r.id, r);
        if (known.size > 600) { // keep memory small: drop the ones far from here
          for (const [id, r] of known) {
            if (known.size <= 400) break;
            if (id !== picked?.id && !pad.intersects(L.latLngBounds([r.bbox[1], r.bbox[0]], [r.bbox[3], r.bbox[2]]))) known.delete(id);
          }
        }
        note = data.pending ? "Downloading the nature reserves here from OpenStreetMap…"
          : data.error === "zoom in" ? "Zoom in to see them." : data.error ? `Couldn't reach OpenStreetMap just now (${data.error}).` : "";
        if (data.pending) timer = setTimeout(load, 4000);
        else if (!data.error) box = pad;
      } catch (err) {
        console.error(err);
        note = `Couldn't load them: ${err.message || err}`;
      } finally {
        busy = false;
      }
      draw();
      // The view moved on while this was loading.
      if (!timer && !map.getBounds().equals(view) && !box?.contains(map.getBounds())) load();
    }

    function bounds(r) {
      return L.latLngBounds([r.bbox[1], r.bbox[0]], [r.bbox[3], r.bbox[2]]);
    }

    function draw() {
      group.clearLayers();
      labels.clearLayers();
      const view = map.getBounds();
      const showAll = all && map.getZoom() >= ALL_MIN_ZOOM;
      const before = inView;
      inView = 0;
      if (showAll) {
        const shown = [...known.values()].filter((r) => view.intersects(bounds(r)));
        inView = shown.length;
        let named = 0;
        // The biggest first, so a small reserve's fill lies over a park's.
        shown.sort((a, b) => b.area_m2 - a.area_m2);
        for (const r of shown) {
          if (r.id === picked?.id) continue;
          L.polygon(r.rings, { ...ALL_STYLE, renderer, interactive: false }).addTo(group);
          const b = bounds(r);
          const px = map.latLngToContainerPoint(b.getNorthEast()).x - map.latLngToContainerPoint(b.getSouthWest()).x;
          if (r.name && px >= LABEL_MIN_PX && named < MAX_LABELS) {
            named++;
            L.tooltip({ permanent: true, direction: "center", className: "reserve-label", interactive: false, pane: "reserves" })
              .setLatLng(labelPoint(r)).setContent(esc(r.name)).addTo(labels);
          }
        }
      }
      pickedLayer = null;
      if (picked) pickedLayer = L.polygon(picked.rings, { ...PICKED_STYLE, renderer, interactive: false }).addTo(group);
      if (before !== inView) onChange();
    }

    function card(r, others) {
      const facts = [r.kind, areaText(r.area_m2), r.since && `protected ${esc(sinceText(r.since))}`].filter(Boolean);
      const parts = [
        `<strong>${esc(r.name || "Unnamed nature reserve")}</strong>`,
        r.other_names?.length && `<span class="muted">${r.other_names.map(esc).join(" · ")}</span>`,
        esc(facts.join(" · ")),
        r.iucn && r.iucn !== r.kind && `<span class="muted">IUCN: ${esc(r.iucn)}</span>`,
        r.operator && `<span class="muted">Managed by ${esc(r.operator)}</span>`,
        ...(r.access || []).map((a) => `<span class="warn-text">${esc(a)}</span>`),
        r.description && `<span class="reserve-desc">${esc(r.description)}</span>`,
        r.ref && `<span class="muted">Register id ${esc(r.ref)}</span>`,
        `<span class="reserve-links">${links(r).map(([t, h]) => `<a href="${esc(h)}" target="_blank" rel="noopener">${esc(t)}</a>`).join(" · ")}`
          + ` · <a href="#" data-reserve-fit="${esc(r.id)}">Show all of it</a></span>`,
        others.length && `<span class="muted">Also here:</span> ${others.map((o) =>
          `<a href="#" data-reserve-pick="${esc(o.id)}">${esc(o.name || o.kind)}</a>`).join(" · ")}`,
      ].filter(Boolean);
      return parts.join("<br>");
    }

    function pick(r, latlng, here) {
      picked = r;
      draw();
      if (!popup) {
        popup = L.popup({ className: "aid-popup reserve-popup", offset: [0, -4], maxWidth: 300, autoPanPadding: [16, 16] });
        popup.on("remove", () => {
          if (popup.fwKeep) return;
          picked = null;
          draw();
        });
      }
      popup.fwKeep = true;
      popup.setLatLng(latlng).setContent(card(r, here.filter((o) => o.id !== r.id))).openOn(map);
      popup.fwKeep = false;
      const el = popup.getElement();
      el?.querySelectorAll("[data-reserve-pick]").forEach((a) => a.addEventListener("click", (ev) => {
        ev.preventDefault();
        const o = known.get(a.dataset.reservePick);
        if (o) pick(o, latlng, here);
      }));
      el?.querySelector("[data-reserve-fit]")?.addEventListener("click", (ev) => {
        ev.preventDefault();
        popup.fwKeep = true;
        map.closePopup(popup);
        popup.fwKeep = false;
        map.fitBounds(bounds(r), { padding: [24, 24] });
      });
    }

    function clear() {
      picked = null;
      if (popup && map.hasLayer(popup)) map.closePopup(popup);
      draw();
    }

    // The tap that closes the card (Leaflet closes popups first) mustn't open it again.
    map.on("preclick", () => { pickedBefore = picked?.id || null; });

    // A tap on the map: the reserve there, if any. True if it was one's.
    async function tap(latlng) {
      const was = pickedBefore;
      pickedBefore = null;
      if (pathClicked || !(canTap() || (all && map.getZoom() >= ALL_MIN_ZOOM))) return false;
      if (!box?.contains(latlng)) await load();
      const here = at([...known.values()], latlng.lat, latlng.lng);
      if (!here.length) {
        if (picked) clear();
        return false;
      }
      if (was && here.some((r) => r.id === was)) { clear(); return true; } // tapped again: off
      pick(here[0], latlng, here);
      return true;
    }

    function setAll(on) {
      all = on;
      store.set("fw-reserves-all", on ? "1" : "0");
      load();
      onChange();
    }

    map.on("moveend", () => load());
    document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
    load();

    return {
      tap, setAll, load, clear,
      get all() { return all; },
      get note() { return note; },
      get count() { return inView; },
      minZoom: ALL_MIN_ZOOM,
    };
  }

  return { contains, at, labelPoint, areaText, sinceText, links, create };
});
