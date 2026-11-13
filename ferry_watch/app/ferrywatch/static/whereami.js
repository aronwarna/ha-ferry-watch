/* Where am I, what's here, find a place, and your own pins.

   - Locate button: centres the map on you. The device's own GPS when the browser allows it
     (the panel served over HTTPS), else your phone's position from Home Assistant. Tap again
     to follow; moving the map yourself stops following.
   - Long press (right click on a computer) anywhere: What's here. Coordinates in sea
     style (tap to copy), distance and bearing from you, the chart's depth there, the seabed
     type, and the weather now and the next hours (here.py). Save it as a pin from there.
   - Search button: places (islands, bays, villages; OpenStreetMap), harbours, your pins, or
     typed coordinates.
   - Pins: your own marks (fishing spot, anchorage, sauna, …) with a name and a note, kept on
     the NAS so the phone and the computer share them; download them all as GPX for the
     plotter. A switch in Layers.

   app.js makes one of these with FerryHere.create. */
(() => {
  "use strict";

  const esc = (text) => String(text ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const PIN_KINDS = {
    pin: ["Pin", "#2563eb", "●"], fish: ["Fishing spot", "#0891b2", "🐟"], anchor: ["Anchorage", "#1e40af", "⚓"],
    sauna: ["Sauna", "#b45309", "♨"], swim: ["Swimming", "#0ea5e9", "≈"], camp: ["Campfire, shore", "#15803d", "▲"],
    danger: ["Danger", "#dc2626", "!"], photo: ["View, photo", "#7c3aed", "★"], home: ["Home", "#0f766e", "⌂"],
  };
  // Room for the top bar and the conditions pill above, the ship cards below.
  const PAD = { autoPanPaddingTopLeft: [12, 130], autoPanPaddingBottomRight: [12, 110] };
  const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

  function dm(v, pos, neg) {
    const a = Math.abs(v);
    const d = Math.floor(a);
    const m = (a - d) * 60;
    return `${d}°${m.toFixed(3).padStart(6, "0")}′${v >= 0 ? pos : neg}`;
  }
  const seaCoords = (lat, lon) => `${dm(lat, "N", "S")} ${dm(lon, "E", "W")}`;
  function bearing(a, b) {
    const r = Math.PI / 180;
    const y = Math.sin((b.lng - a.lng) * r) * Math.cos(b.lat * r);
    const x = Math.cos(a.lat * r) * Math.sin(b.lat * r) - Math.sin(a.lat * r) * Math.cos(b.lat * r) * Math.cos((b.lng - a.lng) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }
  const dirName = (deg) => COMPASS[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
  const fmt = (v, d = 0) => (v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(d));

  function toast(text) {
    let el = document.getElementById("fw-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "fw-toast";
      el.className = "fw-toast panel";
      document.body.append(el);
    }
    el.textContent = text;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.hidden = true; }, 3500);
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast(text); }
  }

  function create({ map, api, store, phoneFix, busy, onChange }) {
    // --- buttons on the map ---------------------------------------------------------
    const fabs = document.createElement("div");
    fabs.className = "map-fabs";
    fabs.innerHTML = `
      <button type="button" class="panel icon-button fab-search" aria-label="Find a place" title="Find a place">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5.5 5.5"/></svg>
      </button>
      <button type="button" class="panel icon-button fab-locate" aria-label="Where am I" title="Where am I" aria-pressed="false">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v4M12 17.5v4M2.5 12h4M17.5 12h4"/></svg>
      </button>`;
    document.body.append(fabs);
    L.DomEvent.disableClickPropagation(fabs);

    // --- where am I -------------------------------------------------------------------
    let live = null;        // { lat, lon, accuracy, heading, speed, ts } from this device
    let watchId = null;
    let follow = false;
    let liveLayer = null;
    const locateBtn = fabs.querySelector(".fab-locate");

    function myFix() {
      if (live && Date.now() / 1000 - live.ts < 120) return { ...live, source: "this device" };
      const ph = phoneFix();
      return ph ? { lat: ph.lat, lon: ph.lon, accuracy: ph.accuracy, ts: ph.ts, source: "your phone (Home Assistant)" } : null;
    }

    function renderLive() {
      if (!live) { liveLayer?.remove(); liveLayer = null; return; }
      const ll = [live.lat, live.lon];
      if (!liveLayer) {
        const halo = L.circle(ll, { radius: 0, color: "#0ea5e9", weight: 1, opacity: 0.4, fillOpacity: 0.1, interactive: false });
        const icon = L.divIcon({ className: "live-dot", iconSize: [34, 34], iconAnchor: [17, 17],
          html: '<svg viewBox="-17 -17 34 34"><path class="cone" d="M0 0 L-9 -15 A17 17 0 0 1 9 -15 Z"/><circle r="7"/></svg>' });
        const dot = L.marker(ll, { icon, interactive: false, keyboard: false, zIndexOffset: 900 });
        liveLayer = L.layerGroup([halo, dot]).addTo(map);
        liveLayer.halo = halo;
        liveLayer.dot = dot;
      }
      liveLayer.halo.setLatLng(ll).setRadius(Math.min(live.accuracy || 0, 2000));
      liveLayer.dot.setLatLng(ll);
      const el = liveLayer.dot.getElement();
      if (el) {
        const cone = el.querySelector(".cone");
        cone.style.display = live.heading == null || Number.isNaN(live.heading) ? "none" : "";
        cone.setAttribute("transform", `rotate(${live.heading || 0})`);
      }
    }

    function startWatch() {
      if (watchId != null || !navigator.geolocation || !window.isSecureContext) return false;
      watchId = navigator.geolocation.watchPosition((p) => {
        live = { lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy,
          heading: p.coords.heading, speed: p.coords.speed, ts: p.timestamp / 1000 };
        renderLive();
        if (follow) map.panTo([live.lat, live.lon], { animate: true });
      }, (err) => {
        if (err.code === 1) toast("Using your phone's position from Home Assistant");
        navigator.geolocation.clearWatch(watchId);
        watchId = null;
      }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
      return true;
    }

    function setFollow(on) {
      follow = on;
      locateBtn.setAttribute("aria-pressed", String(on));
      locateBtn.classList.toggle("following", on);
    }

    locateBtn.addEventListener("click", () => {
      startWatch();
      const fix = myFix();
      if (!fix) {
        toast(watchId != null ? "Finding you…" : "No position yet: your phone hasn't told Home Assistant where it is.");
        if (watchId != null) setFollow(true);
        return;
      }
      const centred = map.getCenter().distanceTo([fix.lat, fix.lon]) < 30;
      if (centred) setFollow(!follow);
      else setFollow(true);
      map.setView([fix.lat, fix.lon], Math.max(map.getZoom(), 13));
    });
    map.on("dragstart", () => { if (follow) setFollow(false); });
    if (store.get("fw-live-gps") === "1") startWatch();
    // Keep following the Home Assistant position when that's all there is.
    setInterval(() => {
      if (!follow || (live && Date.now() / 1000 - live.ts < 120)) return;
      const ph = phoneFix();
      if (ph) map.panTo([ph.lat, ph.lon]);
    }, 15000);

    // --- what's here (long press) ------------------------------------------------------
    let herePopup = null;
    async function whatsHere(latlng, title) {
      const lat = latlng.lat;
      const lon = latlng.lng;
      const fix = myFix();
      let from = "";
      if (fix) {
        const me = L.latLng(fix.lat, fix.lon);
        const nm = me.distanceTo(latlng) / 1852;
        const brg = bearing(me, latlng);
        from = nm < 0.01 ? "You are here" : `${nm < 10 ? nm.toFixed(2) : nm.toFixed(1)} NM from you, ${Math.round(brg)}° (${dirName(brg)})`;
      }
      const head = `${title ? `<strong>${esc(title)}</strong><br>` : ""}<button type="button" class="link-btn here-copy" title="Copy">${esc(seaCoords(lat, lon))}</button>`
        + `<div class="muted small">${lat.toFixed(5)}, ${lon.toFixed(5)}${from ? ` · ${esc(from)}` : ""}</div>`;
      const box = document.createElement("div");
      box.className = "here-box";
      box.innerHTML = `${head}<div class="here-facts small muted">Looking it up…</div>
        <div class="here-actions"><button type="button" class="chip-btn here-pin">Save as pin</button>
        <button type="button" class="chip-btn here-copy2">Copy</button>
        <button type="button" class="chip-btn here-link" title="A link that opens the app here">Link</button>
        <a class="chip-btn" target="_blank" rel="noopener" href="https://www.google.com/maps/search/?api=1&query=${lat.toFixed(6)},${lon.toFixed(6)}">Google Maps</a></div>`;
      box.querySelector(".here-copy").addEventListener("click", () => copy(seaCoords(lat, lon)));
      box.querySelector(".here-copy2").addEventListener("click", () => copy(`${lat.toFixed(6)}, ${lon.toFixed(6)}`));
      box.querySelector(".here-link").addEventListener("click", () => {
        const url = `${location.origin}${location.pathname}${location.search}#at=${lat.toFixed(5)},${lon.toFixed(5)},${map.getZoom()}`;
        if (navigator.share && matchMedia("(pointer: coarse)").matches) navigator.share({ title: title || "Place", url }).catch(() => {});
        else copy(url);
      });
      box.querySelector(".here-pin").addEventListener("click", () => editPin({ lat, lon, kind: "pin", name: title || "" }));
      herePopup = L.popup({ className: "aid-popup here-popup", maxWidth: 320, offset: [0, -4], ...PAD }).setLatLng(latlng).setContent(box).openOn(map);
      try {
        const res = await api(`api/here?lat=${lat.toFixed(5)}&lon=${lon.toFixed(5)}`);
        box.querySelector(".here-facts").outerHTML = factsHtml(res);
        box.querySelectorAll(".here-near button").forEach((b) => b.addEventListener("click", () => {
          const ll = L.latLng(Number(b.dataset.lat), Number(b.dataset.lon));
          map.setView(ll, Math.max(map.getZoom(), 14));
          whatsHere(ll, b.dataset.name);
        }));
      } catch (err) {
        box.querySelector(".here-facts").textContent = "Couldn't look it up just now.";
      }
      herePopup.update();
    }

    function factsHtml(res) {
      const rows = [];
      const d = res.depth;
      if (d?.land) rows.push(["Chart", "Land, or not charted"]);
      else if (d?.drying) rows.push(["Depth", "Dries (shore, shoal)"]);
      else if (d && d.min != null) rows.push(["Depth", d.max == null ? `${fmt(d.min)} m and deeper` : `${fmt(d.min)}–${fmt(d.max)} m`]);
      if (res.seabed?.type) rows.push(["Seabed", res.seabed.type]);
      const w = res.weather || {};
      if (w.wind != null) {
        rows.push(["Wind", `${fmt(w.wind, 1)} m/s ${w.wind_dir != null ? dirName(w.wind_dir) : ""}${w.gust != null ? `, gusts ${fmt(w.gust)}` : ""}`]);
      }
      if (w.wave != null) rows.push(["Waves", `${fmt(w.wave, 1)} m${w.wave_dir != null ? ` from ${dirName(w.wave_dir)}` : ""}`]);
      const temps = [w.temp != null && `air ${fmt(w.temp)}°`, w.sea_temp != null && `sea ${fmt(w.sea_temp)}°`].filter(Boolean);
      if (temps.length) rows.push(["Temperature", temps.join(", ")]);
      let hours = "";
      if (w.hours?.length) {
        hours = `<div class="here-hours">${w.hours.slice(0, 6).map((h) => {
          const t = new Date(h.t * 1000).toLocaleTimeString([], { hour: "2-digit", hour12: false });
          return `<span><b>${esc(t)}</b><svg viewBox="-6 -6 12 12" style="transform:rotate(${(h.dir || 0) + 180}deg)"><path d="M0 -5 L3 4 L0 2 L-3 4 Z"/></svg>${fmt(h.wind)}<i>${fmt(h.gust)}</i></span>`;
        }).join("")}</div>`;
      }
      let near = "";
      if (res.harbours?.length) {
        near = `<div class="here-near"><span class="muted">Nearest harbours</span>${res.harbours.map((h) =>
          `<div><button type="button" class="link-btn" data-lat="${h.lat}" data-lon="${h.lon}" data-name="${esc(h.name)}">${esc(h.name)}</button> <i class="muted">${(h.m / 1852).toFixed(1)} NM</i></div>`).join("")}</div>`;
      }
      hours += near;
      if (!rows.length && !hours) return '<div class="here-facts small muted">Nothing more known here.</div>';
      return `<div class="here-facts small"><dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>${hours}</div>`;
    }

    map.on("contextmenu", (e) => {
      if (busy?.(e)) return;
      whatsHere(e.latlng);
    });

    // --- pins -------------------------------------------------------------------------
    let pins = [];
    let pinsOn = store.get("fw-pins") !== "0";
    const pinLayer = L.layerGroup();
    if (pinsOn) pinLayer.addTo(map);

    function pinIcon(kind) {
      const [, color, glyph] = PIN_KINDS[kind] || PIN_KINDS.pin;
      return L.divIcon({ className: "pin-icon", iconSize: [26, 34], iconAnchor: [13, 32], popupAnchor: [0, -30],
        html: `<svg viewBox="0 0 26 34"><path d="M13 33 C13 33 2 20 2 12 A11 11 0 0 1 24 12 C24 20 13 33 13 33Z" fill="${color}" stroke="#fff" stroke-width="2"/></svg><span>${glyph}</span>` });
    }
    function renderPins() {
      pinLayer.clearLayers();
      for (const p of pins) {
        const m = L.marker([p.lat, p.lon], { icon: pinIcon(p.kind), title: p.name || PIN_KINDS[p.kind]?.[0] });
        m.on("click", () => showPin(p, m));
        pinLayer.addLayer(m);
      }
    }
    function showPin(p, marker) {
      const box = document.createElement("div");
      box.className = "here-box";
      box.innerHTML = `<strong>${esc(p.name || PIN_KINDS[p.kind]?.[0] || "Pin")}</strong>
        <div class="muted small">${esc(PIN_KINDS[p.kind]?.[0] || "")} · ${esc(seaCoords(p.lat, p.lon))}</div>
        ${p.note ? `<p class="pin-note">${esc(p.note)}</p>` : ""}
        <div class="here-actions"><button type="button" class="chip-btn pin-edit">Edit</button>
        <button type="button" class="chip-btn pin-here">What's here</button>
        <button type="button" class="chip-btn pin-del">Delete</button></div>`;
      box.querySelector(".pin-edit").addEventListener("click", () => editPin(p));
      box.querySelector(".pin-here").addEventListener("click", () => whatsHere(L.latLng(p.lat, p.lon), p.name));
      box.querySelector(".pin-del").addEventListener("click", async () => {
        if (!confirm(`Delete ${p.name || "this pin"}?`)) return;
        await api(`api/pins/${encodeURIComponent(p.id)}`, { method: "DELETE" });
        map.closePopup();
        await loadPins();
      });
      L.popup({ className: "aid-popup here-popup", maxWidth: 300, offset: [0, -28], ...PAD }).setLatLng([p.lat, p.lon]).setContent(box).openOn(map);
    }
    function editPin(p) {
      const box = document.createElement("form");
      box.className = "here-box pin-form";
      box.innerHTML = `<strong>${p.id ? "Edit pin" : "New pin"}</strong>
        <input name="name" placeholder="Name" maxlength="80" value="${esc(p.name || "")}">
        <select name="kind">${Object.entries(PIN_KINDS).map(([k, [label, , g]]) => `<option value="${k}"${k === p.kind ? " selected" : ""}>${g} ${esc(label)}</option>`).join("")}</select>
        <textarea name="note" rows="3" maxlength="2000" placeholder="Note (depth, bottom, best wind, …)">${esc(p.note || "")}</textarea>
        <div class="here-actions"><button type="submit" class="chip-btn primary">Save</button><button type="button" class="chip-btn pin-cancel">Cancel</button></div>`;
      box.addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = new FormData(box);
        try {
          await api("api/pins", { method: "POST", body: { ...p, name: f.get("name"), kind: f.get("kind"), note: f.get("note") } });
          map.closePopup();
          if (!pinsOn) setPins(true);
          await loadPins();
          toast("Pin saved");
        } catch (err) { toast(`Couldn't save: ${err.message || err}`); }
      });
      box.querySelector(".pin-cancel").addEventListener("click", () => map.closePopup());
      L.popup({ className: "aid-popup here-popup", maxWidth: 300, offset: [0, -4], ...PAD }).setLatLng([p.lat, p.lon]).setContent(box).openOn(map);
      setTimeout(() => box.querySelector("input")?.focus(), 50);
    }
    async function loadPins() {
      try { pins = await api("api/pins"); } catch { pins = []; }
      renderPins();
      onChange?.();
    }
    function setPins(on) {
      pinsOn = on;
      store.set("fw-pins", on ? "1" : "0");
      if (on) pinLayer.addTo(map); else map.removeLayer(pinLayer);
    }
    loadPins();

    // --- search -----------------------------------------------------------------------
    const sheet = document.createElement("div");
    sheet.className = "search-sheet panel";
    sheet.hidden = true;
    sheet.innerHTML = `<div class="search-row"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5.5 5.5"/></svg>
      <input type="text" inputmode="search" placeholder="Island, bay, harbour, pin or 60.12 24.45" autocomplete="off" enterkeyhint="search" aria-label="Find a place">
      <button type="button" class="icon-button plain search-close" aria-label="Close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
      <ul class="search-results"></ul>`;
    document.body.append(sheet);
    L.DomEvent.disableClickPropagation(sheet);
    L.DomEvent.disableScrollPropagation(sheet);
    const input = sheet.querySelector("input");
    const list = sheet.querySelector(".search-results");
    let found = null;
    function openSearch(open) {
      sheet.hidden = !open;
      if (open) { input.focus(); input.select(); }
    }
    fabs.querySelector(".fab-search").addEventListener("click", () => openSearch(sheet.hidden));
    sheet.querySelector(".search-close").addEventListener("click", () => openSearch(false));
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "")) { e.preventDefault(); openSearch(true); }
      if (e.key === "Escape" && !sheet.hidden) openSearch(false);
    });
    let timer = null;
    let seq = 0;
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 350); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(timer); run().then(() => list.querySelector("button")?.click()); } });
    async function run() {
      const q = input.value.trim();
      const my = ++seq;
      if (q.length < 2) { list.replaceChildren(); return; }
      const ql = q.toLowerCase();
      const rows = pins.filter((p) => (p.name || "").toLowerCase().includes(ql) || (p.note || "").toLowerCase().includes(ql))
        .slice(0, 5).map((p) => ({ name: p.name || PIN_KINDS[p.kind][0], where: "Your pin", kind: PIN_KINDS[p.kind][2], lat: p.lat, lon: p.lon }));
      const c = map.getCenter();
      const [places, harbours] = await Promise.all([
        api(`api/places?q=${encodeURIComponent(q)}`).catch(() => ({ places: [] })),
        api(`api/harbours?q=${encodeURIComponent(q)}&near=${c.lat.toFixed(3)},${c.lng.toFixed(3)}`).catch(() => null),
      ]);
      if (my !== seq) return;
      if (places.coords) rows.unshift({ name: seaCoords(places.coords[0], places.coords[1]), where: "Coordinates", kind: "⌖", lat: places.coords[0], lon: places.coords[1] });
      const hb = Array.isArray(harbours) ? harbours : harbours?.harbours || harbours?.results || [];
      for (const h of hb.slice(0, 5)) {
        // Rows [id, lat, lon, name, kind, …] as harbours.py gives them.
        const lat = Array.isArray(h) ? h[1] : h.lat;
        const lon = Array.isArray(h) ? h[2] : h.lon;
        const name = Array.isArray(h) ? h[3] : h.name;
        if (lat != null && lon != null) rows.push({ name, where: "Harbour", kind: "⚓", lat, lon });
      }
      for (const p of places.places || []) rows.push({ ...p, kind: "", where: [p.kind, p.where].filter(Boolean).join(" · ") });
      found = rows;
      list.replaceChildren(...rows.map((r) => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        b.innerHTML = `<span class="sr-kind">${esc(r.kind || "◦")}</span><span class="sr-text"><strong>${esc(r.name)}</strong><span class="muted small">${esc(r.where || "")}</span></span>`;
        b.addEventListener("click", () => {
          openSearch(false);
          map.setView([r.lat, r.lon], Math.max(map.getZoom(), 13));
          whatsHere(L.latLng(r.lat, r.lon), r.name);
        });
        li.append(b);
        return li;
      }));
      if (!rows.length) list.innerHTML = `<li class="muted small sr-empty">${places.error ? `Couldn't search just now (${esc(places.error)}).` : "Nothing found."}</li>`;
    }

    // --- Layers row ---------------------------------------------------------------------
    function rows() {
      const a = document.createElement("div");
      a.className = "small";
      a.innerHTML = `${pins.length ? '<a href="api/pins.gpx" download>Download all as GPX</a> (for the plotter) · ' : ""}`
        + '<label class="link-like">Add waypoints from a GPX file<input type="file" accept=".gpx,application/gpx+xml" hidden></label>';
      a.querySelector("input").addEventListener("change", async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        try {
          const res = await fetch("api/pins/import", { method: "POST", body: await file.text() });
          if (!res.ok) throw new Error(await res.text());
          const out = await res.json();
          toast(`${out.added} pins added${out.skipped ? `, ${out.skipped} already there or not usable` : ""}`);
          if (!pinsOn) setPins(true);
          await loadPins();
        } catch (err) { toast(`Couldn't read it: ${err.message || err}`); }
      });
      return [{
        title: "My pins", color: "#2563eb", checked: pinsOn, count: pins.length ? String(pins.length) : "",
        note: "Your own marks: long-press the map (right-click on a computer), then Save as pin. Kept on the NAS, so the phone and the computer share them.",
        extra: a,
        onChange: async (on) => setPins(on),
      }];
    }

    // A link with #at=lat,lon,zoom opens the app there with What's here open.
    function fromHash() {
      const at = new URLSearchParams(location.hash.slice(1)).get("at");
      const m = at && at.match(/^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(\d+))?$/);
      if (!m) return false;
      const ll = L.latLng(Number(m[1]), Number(m[2]));
      map.setView(ll, m[3] ? Number(m[3]) : 13);
      setTimeout(() => whatsHere(ll), 300);
      return true;
    }

    return { rows, whatsHere, myFix, fromHash, get found() { return found; } };
  }

  window.FerryHere = { create, seaCoords };
})();
