/* Where am I, what's here, find a place, and your own pins.

   - Locate button: centres the map on you. The device's own GPS when the browser allows it
     (the panel served over HTTPS), else your phone's position from Home Assistant. Tap again
     to follow; moving the map yourself stops following.
   - Long press (right click on a computer) anywhere: What's here. Coordinates in sea
     style (tap to copy), distance and bearing from you, the chart's depth there, the seabed
     type, and the weather now and the next hours (here.py). Save it as a pin from there.
   - Search button: anything the app knows (find.py): ships by name, MMSI or IMO, places
     (OpenStreetMap), harbours, lights and marks, reserves, stations, bridges and locks, your
     pins and routes, layers and map types, or typed coordinates; each with its kind's icon.
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
  // Lower case without accents (Rönnskär -> ronnskar), as find.py folds names.
  const fold = (text) => String(text ?? "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  // 0 the same, 1 starts with it, 2 a word starts with it, 3 inside; null: not every word in it.
  function score(name, words) {
    const n = fold(name);
    if (!n || !words.length || !words.every((w) => n.includes(w))) return null;
    const whole = words.join(" ");
    if (n === whole) return 0;
    if (n.startsWith(whole)) return 1;
    return n.split(" ").some((p) => p.startsWith(words[0])) ? 2 : 3;
  }
  // "60.12 24.45", "60°07.2'N 24°27'E" and the like (pins.py parses the same on the NAS).
  function parseCoords(q) {
    const nums = (q.match(/-?\d+(?:[.,]\d+)?/g) || []).map((n) => n.replace(",", "."));
    if (!nums.length || /[a-df-mo-rt-vx-z]/i.test(q.replace(/[NSEWnsew]$|[NSEWnsew](?=\s)/g, ""))) return null;
    let lat, lon;
    if (nums.length === 2) [lat, lon] = nums.map(Number);
    else if (nums.length === 4) { lat = Number(nums[0]) + Number(nums[1]) / 60; lon = Number(nums[2]) + Number(nums[3]) / 60; }
    else return null;
    if (/S/.test(q)) lat = -Math.abs(lat);
    if (/W/.test(q)) lon = -Math.abs(lon);
    return Math.abs(lat) > 1 && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? [lat, lon] : null;
  }
  const svg = (d) => d.split("|").map((p) => `<path d="${p}"/>`).join("");
  // What each kind of result looks like in the list: icon, colour, label, and its rank
  // when two match equally well.
  const TYPES = {
    coords: { label: "Coordinates", color: "#475569", rank: 0, icon: svg("M12 3v4|M12 17v4|M3 12h4|M17 12h4|M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8z") },
    ship: { label: "Ship", color: "#dc2626", rank: 1, icon: svg("M3 14h18l-2.5 5.5h-13z|M6 14V9h12v5|M10 9V5.5h4V9") },
    pin: { label: "Pin", color: "#2563eb", rank: 2, icon: svg("M12 21s-6-6.5-6-11a6 6 0 0 1 12 0c0 4.5-6 11-6 11z|M12 8.5a1.5 1.5 0 1 0 0 3a1.5 1.5 0 1 0 0-3z") },
    route: { label: "Saved route", color: "#7c3aed", rank: 3, icon: svg("M6 16.5a2 2 0 1 0 0 4a2 2 0 1 0 0-4z|M18 3.5a2 2 0 1 0 0 4a2 2 0 1 0 0-4z|M7.5 17C12 15 7 10.5 12 10s5-2 5.5-3") },
    harbour: { label: "Harbour", color: "#b91c1c", rank: 4, icon: svg("M12 3.5a2 2 0 1 0 0 4a2 2 0 1 0 0-4z|M12 7.5V20|M5 13a7 7 0 0 0 14 0|M9 10.5h6") },
    vessel: { label: "Ship", color: "#0e7490", rank: 5, icon: svg("M3 14h18l-2.5 5.5h-13z|M6 14V9h12v5|M10 9V5.5h4V9") },
    light: { label: "Light", color: "#ca8a04", rank: 6, icon: svg("M10 21l1-11h2l1 11z|M9 10h6|M10.5 7h3v3h-3z|M5 4.5l3 2|M19 4.5l-3 2|M4 9h2.5|M20 9h-2.5") },
    aid: { label: "Mark", color: "#15803d", rank: 7, icon: svg("M12 3v5|M9 21l1.5-10h3L15 21|M7.5 21h9|M9.5 6l2.5-3 2.5 3") },
    reserve: { label: "Nature reserve", color: "#3f9a4f", rank: 8, icon: svg("M5 19C5 10 11 5 20 5c0 9-5 14-14 14|M5 19l8-8") },
    station: { label: "Weather station", color: "#0284c7", rank: 9, icon: svg("M3 8h10a3 3 0 1 0-3-3|M3 12h15a3 3 0 1 1-3 3|M3 16h7") },
    structure: { label: "Bridge or lock", color: "#9a3412", rank: 10, icon: svg("M3 10h18|M5 10v9|M19 10v9|M5 15.5a7 4.5 0 0 1 14 0") },
    ashore: { label: "Ashore", color: "#15803d", rank: 11, icon: svg("M3 20L12 5l9 15z|M12 20l-2.5-5h5z") },
    place: { label: "Place", color: "#64748b", rank: 12, icon: svg("M4 6.5l5-2 6 2 5-2v13l-5 2-6-2-5 2z|M9 4.5v13|M15 6.5v13") },
    layer: { label: "Layer", color: "#0f766e", rank: 13, icon: svg("M12 4l9 5-9 5-9-5z|M3 14l9 5 9-5") },
    weather: { label: "Weather layer", color: "#0369a1", rank: 13, icon: svg("M7 18h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 11.5 3.3 3.3 0 0 0 7 18z") },
    view: { label: "Map type", color: "#475569", rank: 14, icon: svg("M4 6.5l5-2 6 2 5-2v13l-5 2-6-2-5 2z|M9 4.5v13|M15 6.5v13") },
    menu: { label: "Menu", color: "#475569", rank: 15, icon: svg("M4 7h16|M4 12h16|M4 17h16") },
  };
  // Nominatim's kinds in plain words.
  function placeLabel(kind) {
    const k = String(kind || "").replace(/_/g, " ");
    const words = { islet: "Island", island: "Island", bay: "Bay", strait: "Strait", village: "Village", town: "Town", city: "City",
      hamlet: "Village", cape: "Cape", peninsula: "Peninsula", lake: "Lake", water: "Water", reef: "Reef", shoal: "Shoal",
      suburb: "District", locality: "Place", beach: "Beach", harbour: "Harbour", marina: "Marina", ferry_terminal: "Ferry terminal" };
    return words[String(kind || "").toLowerCase()] || (k ? k[0].toUpperCase() + k.slice(1) : "Place");
  }
  const fmt = (v, d = 0) => (v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(d));

  function toast(text, ms = 3500) {
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
    el._t = setTimeout(() => { el.hidden = true; }, ms);
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast(text); }
  }

  function create({ map, api, store, phoneFix, busy, onChange, localRows, onPick, canvas, harbourPoints }) {
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

    // --- search: everything the app knows ---------------------------------------------
    // Rows: { type, name, label?, detail?, lat?, lon?, bbox?, score, km?, pick? }. The NAS
    // answers for ships, harbours, lights and marks, reserves, stations and bridges
    // (/api/find), OpenStreetMap for place names (/api/places, slower); the panel adds its
    // own ships, pins, routes and layers (localRows). Each row shows its kind's icon and a
    // label, so a ship is never mistaken for an island of the same name.
    const sheet = document.createElement("div");
    sheet.className = "search-sheet panel";
    sheet.hidden = true;
    sheet.innerHTML = `<div class="search-row"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5.5 5.5"/></svg>
      <input type="text" inputmode="search" placeholder="Ships, places, lights, harbours, layers…" autocomplete="off" enterkeyhint="search" aria-label="Search">
      <button type="button" class="icon-button plain search-close" aria-label="Close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
      <ul class="search-results" role="listbox"></ul><div class="search-foot small muted" hidden></div>`;
    document.body.append(sheet);
    L.DomEvent.disableClickPropagation(sheet);
    L.DomEvent.disableScrollPropagation(sheet);
    const input = sheet.querySelector("input");
    const list = sheet.querySelector(".search-results");
    const foot = sheet.querySelector(".search-foot");
    let found = null;
    function openSearch(open) {
      sheet.hidden = !open;
      document.body.classList.toggle("search-open", open);
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
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 220); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { clearTimeout(timer); (list.children.length ? Promise.resolve() : run()).then(() => list.querySelector("button")?.click()); }
      if (e.key === "ArrowDown") { e.preventDefault(); list.querySelector("button")?.focus(); }
    });
    list.addEventListener("keydown", (e) => {
      const li = e.target.closest("li");
      if (!li) return;
      if (e.key === "ArrowDown") { e.preventDefault(); li.nextElementSibling?.querySelector("button")?.focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); (li.previousElementSibling?.querySelector("button") || input).focus(); }
    });

    async function run() {
      const q = input.value.trim();
      const my = ++seq;
      if (q.length < 2) { list.replaceChildren(); foot.hidden = true; return; }
      const words = fold(q).split(" ").filter(Boolean).slice(0, 5);
      const c = map.getCenter();
      const near = `${c.lat.toFixed(3)},${c.lng.toFixed(3)}`;
      const nearKm = (lat, lon) => (lat == null ? null : c.distanceTo([lat, lon]) / 1000);
      const local = [];
      for (const p of pins) {
        const sc = Math.min(score(p.name, words) ?? 9, (score(p.note, words) ?? 9) + 1);
        if (sc < 9) local.push({ type: "pin", name: p.name || PIN_KINDS[p.kind][0], label: `Pin: ${PIN_KINDS[p.kind][0]}`, lat: p.lat, lon: p.lon, score: sc, pick: () => { goTo(p); showPin(p); } });
      }
      for (const r of localRows?.(words, score) || []) local.push(r);
      for (const r of local) if (r.km == null && r.lat != null) r.km = nearKm(r.lat, r.lon);
      const coords = parseCoords(q);
      if (coords) local.unshift({ type: "coords", name: seaCoords(coords[0], coords[1]), label: "Coordinates", lat: coords[0], lon: coords[1], score: -1 });
      // What the panel knows shows at once; the NAS's answer and the place names join it.
      let server = [], places = [], indexing = null, placesError = null, placesDone = false;
      const show = () => {
        if (my !== seq) return;
        const seen = new Set(local.filter((r) => r.mmsi).map((r) => r.mmsi));
        const rows = [...local, ...server.filter((r) => !(r.mmsi && seen.has(r.mmsi))), ...places];
        rows.sort((a, b) => (a.score - b.score) || ((TYPES[a.type]?.rank ?? 50) - (TYPES[b.type]?.rank ?? 50))
          || ((a.km ?? 1e9) - (b.km ?? 1e9)));
        found = rows.slice(0, 40);
        render(found);
        const notes = [];
        if (indexing) notes.push(`Still indexing the chart's lights and marks (${indexing.done} of ${indexing.total} areas)…`);
        if (!placesDone) notes.push("Looking up place names…");
        else if (placesError && !rows.length) notes.push(`Couldn't search place names just now (${placesError}).`);
        foot.textContent = notes.join(" ");
        foot.hidden = !notes.length;
        if (!rows.length && placesDone) list.innerHTML = '<li class="muted small sr-empty">Nothing found.</li>';
      };
      show();
      const a = api(`api/find?q=${encodeURIComponent(q)}&near=${near}`).then((d) => {
        server = d.results || [];
        indexing = d.indexing;
        show();
      }).catch(() => {});
      const b = api(`api/places?q=${encodeURIComponent(q)}`).then((d) => {
        places = (d.places || []).map((p) => ({ type: "place", name: p.name, label: placeLabel(p.kind), detail: p.where,
          lat: p.lat, lon: p.lon, km: nearKm(p.lat, p.lon), score: (score(p.name, words) ?? 3) + 0.5 }));
        placesError = d.error;
      }).catch((err) => { placesError = err.message || String(err); }).finally(() => { placesDone = true; show(); });
      await Promise.all([a, b]);
    }

    function render(rows) {
      list.replaceChildren(...rows.map((r) => {
        const t = TYPES[r.type] || TYPES.place;
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        const color = r.color || (r.light ? TYPES.light.color : t.color);
        const icon = (r.light && TYPES.light.icon) || t.icon;
        const where = [r.detail, r.km != null && r.km >= 0.05 ? (r.km < 10 ? `${r.km.toFixed(1)} km` : `${Math.round(r.km)} km`) : ""].filter(Boolean).join(" · ");
        b.innerHTML = `<span class="sr-icon" style="--c:${esc(color)}"><svg viewBox="0 0 24 24" aria-hidden="true">${icon}</svg></span>`
          + `<span class="sr-text"><strong>${esc(r.name)}</strong><span class="small"><span class="sr-type" style="--c:${esc(color)}">${esc(r.label || t.label)}</span>${where ? ` <span class="muted">${esc(where)}</span>` : ""}</span></span>`;
        b.addEventListener("click", () => {
          openSearch(false);
          if (r.pick) r.pick();
          else if (!onPick?.(r)) {
            goTo(r);
            whatsHere(L.latLng(r.lat, r.lon), r.name);
          }
        });
        li.append(b);
        return li;
      }));
    }

    function goTo(r, zoom = 13) {
      if (r.bbox) map.fitBounds([[r.bbox[1], r.bbox[0]], [r.bbox[3], r.bbox[2]]], { padding: [30, 30], maxZoom: 14 });
      else if (r.lat != null) map.setView([r.lat, r.lon], Math.max(map.getZoom(), zoom));
    }

    // --- places ashore (ashore.py, OpenStreetMap) ------------------------------------------
    // Drawn on the shared marks canvas (under the ships and harbours), small and see-through
    // like the service symbols on a paper chart. Each kind has its own first zoom (toilets and
    // taps only close in), places that would touch on screen merge into one mark with little
    // dots for the other kinds there, and those right by a harbour fold into its card.
    const ASHORE_KINDS = [
      // key, OSM kind, label, colour, first zoom
      ["shelter", "Lean-to shelter", "Lean-to shelters", "#2f7d4a", 12],
      ["hut", "Wilderness hut", "Huts", "#4d6b2f", 12],
      ["hut", "Day hut", "Huts", "#4d6b2f", 13],
      ["camp", "Campsite", "Campsites", "#2f7d4a", 12],
      ["fire", "Campfire site", "Campfire sites", "#c2410c", 13],
      ["sauna", "Sauna", "Saunas", "#a16207", 13],
      ["water", "Drinking water", "Drinking water", "#0e7490", 14],
      ["wc", "Toilet", "Toilets", "#475569", 15],
    ];
    const KIND = new Map(ASHORE_KINDS.map(([key, kind, label, color, minZoom], rank) => [kind, { key, label, color, minZoom, rank }]));
    const CATS = [...new Map(ASHORE_KINDS.map(([key, , label, color]) => [key, { key, label, color }])).values()];
    const GLYPH = { // 24-unit drawings, white on the coloured disc
      shelter: { stroke: new Path2D("M4 18.5L10.5 6.5L20 18.5M10.5 6.5V18.5") },
      hut: { stroke: new Path2D("M4.5 11.5L12 5L19.5 11.5M7 10V19H17V10M10.5 19v-4.5h3V19") },
      camp: { stroke: new Path2D("M3.5 19L12 5L20.5 19ZM12 19L9.8 14.2H14.2Z") },
      fire: { fill: new Path2D("M12 3c.8 3.6 5.5 5.8 5.5 10.5a5.5 5.5 0 0 1-11 0c0-2.3 1.2-4 2.6-5 .1 2.1 1 3.3 2.3 3.6C10.9 9.4 10.6 6 12 3z") },
      sauna: { stroke: new Path2D("M8 3.5c-1.6 1.9 1.6 3.4 0 5.3M12 3.5c-1.6 1.9 1.6 3.4 0 5.3M16 3.5c-1.6 1.9 1.6 3.4 0 5.3M4.5 12.5h15M6.5 12.5V20M17.5 12.5V20M6.5 16.5h11") },
      water: { fill: new Path2D("M12 3.2c3.2 4.3 6.3 7.6 6.3 11.3a6.3 6.3 0 0 1-12.6 0C5.7 10.8 8.8 7.5 12 3.2z") },
      wc: { text: "WC" },
    };
    const AshoreMark = L.CircleMarker.extend({
      _updatePath() {
        const r = this._renderer;
        if (!r._drawing || this._empty()) return;
        const p = this._point, ctx = r._ctx, o = this.options, g = o.group;
        const rad = o.radius;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.globalAlpha = 0.8; // see-through, like the chart's own symbols
        ctx.beginPath();
        ctx.arc(0, 0, rad, 0, Math.PI * 2);
        ctx.fillStyle = g.top.color;
        ctx.fill();
        ctx.lineWidth = 1;
        ctx.strokeStyle = "rgba(255,255,255,0.85)";
        ctx.stroke();
        ctx.globalAlpha = 0.95;
        const glyph = GLYPH[g.top.key];
        const k = (rad * 1.5) / 24;
        if (glyph.text) {
          ctx.fillStyle = "#fff";
          ctx.font = `700 ${Math.round(rad * 0.95)}px system-ui, -apple-system, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(glyph.text, 0, 0.5);
        } else {
          ctx.save();
          ctx.scale(k, k);
          ctx.translate(-12, -12);
          if (glyph.fill) { ctx.fillStyle = "#fff"; ctx.fill(glyph.fill); }
          if (glyph.stroke) {
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 2.4;
            ctx.lineJoin = "round";
            ctx.lineCap = "round";
            ctx.stroke(glyph.stroke);
          }
          ctx.restore();
        }
        // The other kinds at this spot: a short row of dots under the mark.
        const more = g.cats.slice(1, 5);
        if (more.length) {
          ctx.globalAlpha = 0.9;
          const step = 5;
          let x = -((more.length - 1) * step) / 2;
          for (const c of more) {
            ctx.beginPath();
            ctx.arc(x, rad + 3.5, 2.1, 0, Math.PI * 2);
            ctx.fillStyle = c.color;
            ctx.fill();
            ctx.lineWidth = 0.8;
            ctx.strokeStyle = "rgba(255,255,255,0.9)";
            ctx.stroke();
            x += step;
          }
        }
        ctx.restore();
      },
    });

    const EXTRA = { fire: "fireplace", wood: "firewood", toilet: "toilet", water: "drinking water", free: "free", fee: "fee" };
    let ashoreOn = store.get("fw-ashore") === "1";
    const ashoreHidden = new Set((store.get("fw-ashore-hide") || "").split(",").filter(Boolean));
    let ashoreNote = "";
    let ashoreCount = 0;
    let ashoreRows = [];
    let ashoreFolded = []; // [harbour latlng, places] for harbour cards
    const ashoreLayer = L.layerGroup();
    let ashoreSeq = 0;
    const ASHORE_FETCH_ZOOM = 12;
    const ASHORE_MERGE_PX = 20; // marks closer than this on screen become one
    const ASHORE_HARBOUR_PX = 18; // ... and fold into a harbour this close (below zoom 16)

    async function loadAshore() {
      if (!ashoreOn || map.getZoom() < ASHORE_FETCH_ZOOM) { ashoreRows = []; drawAshore(); return; }
      const b = map.getBounds().pad(0.2);
      const f = (v) => v.toFixed(3);
      const my = ++ashoreSeq;
      let data;
      try { data = await api(`api/ashore?bbox=${f(b.getWest())},${f(b.getSouth())},${f(b.getEast())},${f(b.getNorth())}`); }
      catch { return; }
      if (my !== ashoreSeq) return;
      ashoreNote = data.pending ? "Downloading them from OpenStreetMap…"
        : data.error && !data.harbours.length ? `Couldn't get them from OpenStreetMap just now: ${data.error}. Trying again by itself.` : "";
      ashoreRows = data.harbours;
      drawAshore();
      if (data.pending) setTimeout(loadAshore, 10000);
      else if (data.error && !data.harbours.length) setTimeout(loadAshore, 120000);
    }

    // Which places show at this zoom, merged by how close they sit on screen.
    function ashoreGroups(rows, zoom) {
      const places = [];
      for (const [, lat, lon, name, kind, extras] of rows) {
        const info = KIND.get(kind);
        if (!info || zoom < info.minZoom || ashoreHidden.has(info.key)) continue;
        places.push({ lat, lon, name, kind, extras: extras || [], info, pt: map.project([lat, lon], zoom) });
      }
      places.sort((a, b) => a.info.rank - b.info.rank);
      const cell = ASHORE_MERGE_PX;
      const grid = new Map();
      const groups = [];
      for (const pl of places) {
        const gx = Math.floor(pl.pt.x / cell), gy = Math.floor(pl.pt.y / cell);
        let best = null, bestD = cell;
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
          for (const g of grid.get(`${gx + dx},${gy + dy}`) || []) {
            const d = g.pt.distanceTo(pl.pt);
            if (d < bestD) { best = g; bestD = d; }
          }
        }
        if (best) { best.places.push(pl); continue; }
        const g = { pt: pl.pt, lat: pl.lat, lon: pl.lon, places: [pl] };
        groups.push(g);
        const key = `${gx},${gy}`;
        (grid.get(key) || grid.set(key, []).get(key)).push(g);
      }
      for (const g of groups) {
        const cats = new Map();
        for (const pl of g.places) if (!cats.has(pl.info.key)) cats.set(pl.info.key, { key: pl.info.key, color: pl.info.color, label: pl.info.label });
        g.cats = [...cats.values()];
        g.top = g.cats[0];
      }
      return groups;
    }

    function drawAshore() {
      ashoreLayer.clearLayers();
      ashoreFolded = [];
      ashoreCount = 0;
      if (!ashoreOn || !ashoreRows.length || map.getZoom() < ASHORE_FETCH_ZOOM) return;
      const zoom = map.getZoom();
      const groups = ashoreGroups(ashoreRows, zoom);
      let harbours = [];
      try { harbours = (zoom < 16 && harbourPoints?.()) || []; } catch { harbours = []; }
      const hpts = harbours.map((ll) => [ll, map.project(ll, zoom)]);
      const renderer = canvas?.();
      const rad = zoom >= 16 ? 8.5 : zoom >= 14 ? 7.5 : 6.5;
      for (const g of groups) {
        ashoreCount += g.places.length;
        const near = hpts.find(([, hp]) => hp.distanceTo(g.pt) < ASHORE_HARBOUR_PX);
        if (near) { ashoreFolded.push([near[0], g.places]); continue; }
        const m = new AshoreMark([g.lat, g.lon], { renderer, radius: rad, group: g, stroke: false, fill: false,
          bubblingMouseEvents: false, pane: renderer ? undefined : "markerPane" });
        m.on("click", () => openAshore(g));
        ashoreLayer.addLayer(m);
      }
      // Under the ships, harbours and chart marks on the same canvas.
      ashoreLayer.eachLayer((m) => m.bringToBack?.());
    }

    function placeLine(pl) {
      return `<li><strong>${esc(pl.name || pl.kind)}</strong> <span class="muted">${pl.name ? esc(pl.kind) : ""}${pl.extras.length ? `${pl.name ? " · " : ""}${esc(pl.extras.map((x) => EXTRA[x] || x).join(", "))}` : ""}</span></li>`;
    }

    function openAshore(g) {
      const box = document.createElement("div");
      box.className = "here-box ashore-box";
      const many = g.places.length > 1;
      const spread = many && g.places.some((pl) => pl.lat !== g.lat || pl.lon !== g.lon);
      // Named places one by one; nameless ones of a kind as one line ("Toilet ×3").
      const lines = [];
      const unnamed = new Map();
      for (const pl of g.places) {
        if (pl.name) { lines.push(placeLine(pl)); continue; }
        const u = unnamed.get(pl.kind) || unnamed.set(pl.kind, { n: 0, extras: new Set() }).get(pl.kind);
        u.n++;
        pl.extras.forEach((x) => u.extras.add(EXTRA[x] || x));
      }
      for (const [kind, u] of unnamed) {
        lines.push(`<li><strong>${esc(kind)}${u.n > 1 ? ` ×${u.n}` : ""}</strong>${u.extras.size ? ` <span class="muted">${esc([...u.extras].join(", "))}</span>` : ""}</li>`);
      }
      const shown = lines.slice(0, 8);
      box.innerHTML = `${many ? `<strong>${g.places.length} places ashore here</strong>` : ""}
        <ul class="ashore-list">${shown.join("")}</ul>
        ${lines.length > shown.length ? `<div class="small muted">and more: zoom in to see them.</div>` : ""}
        <div class="small muted">From OpenStreetMap: check on site.</div>
        <div class="here-actions">${spread && map.getZoom() < 17 ? '<button type="button" class="chip-btn" data-a="zoom">Zoom in</button>' : ""}<button type="button" class="chip-btn" data-a="here">What's here</button></div>`;
      const first = g.places[0];
      box.querySelector('[data-a="here"]').addEventListener("click", () => whatsHere(L.latLng(g.lat, g.lon), many ? "" : (first.name || first.kind)));
      box.querySelector('[data-a="zoom"]')?.addEventListener("click", () => {
        map.closePopup();
        map.fitBounds(L.latLngBounds(g.places.map((pl) => [pl.lat, pl.lon])), { padding: [80, 80], maxZoom: Math.min(18, map.getZoom() + 3) });
      });
      L.popup({ className: "aid-popup here-popup", maxWidth: 280, offset: [0, -6], ...PAD }).setLatLng([g.lat, g.lon]).setContent(box).openOn(map);
    }

    // For a harbour's card: the places ashore folded into it (kinds and names).
    function ashoreNear(latlng) {
      const out = [];
      for (const [ll, places] of ashoreFolded) if (ll.equals(latlng)) out.push(...places);
      return out.map((pl) => ({ name: pl.name, kind: pl.kind, label: pl.info.label, color: pl.info.color, extras: pl.extras.map((x) => EXTRA[x] || x) }));
    }

    function setAshore(on) {
      ashoreOn = on;
      store.set("fw-ashore", on ? "1" : "0");
      if (on) { ashoreLayer.addTo(map); loadAshore(); } else { map.removeLayer(ashoreLayer); ashoreRows = []; drawAshore(); }
    }
    function setAshoreCat(key, show) {
      if (show) ashoreHidden.delete(key); else ashoreHidden.add(key);
      store.set("fw-ashore-hide", [...ashoreHidden].join(","));
      drawAshore();
    }
    if (ashoreOn) setAshore(true);
    map.on("moveend", () => { if (ashoreOn) loadAshore().then(() => onChange?.()); });

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
      }, {
        title: "Places to go ashore", color: "#2f7d4a", checked: ashoreOn,
        count: ashoreOn && ashoreCount ? `${ashoreCount} in view` : "",
        note: ashoreNote || "Lean-to shelters, huts and campsites from zoom 12, campfire sites and saunas from 13, drinking water from 14, toilets from 15, from OpenStreetMap. Places close together share one mark (dots show what else is there); those at a harbour are in the harbour's card. Tap one for what's there. Downloaded once a month.",
        extra: ashoreChips(),
        onChange: async (on) => setAshore(on),
      }];
    }

    // Show or hide each kind of place ashore.
    function ashoreChips() {
      const box = document.createElement("div");
      box.className = "ashore-cats";
      for (const c of CATS) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "ashore-cat";
        b.setAttribute("aria-pressed", String(!ashoreHidden.has(c.key)));
        b.style.setProperty("--cat", c.color);
        b.innerHTML = `<i></i>${esc(c.label)}`;
        b.addEventListener("click", () => {
          const show = ashoreHidden.has(c.key);
          b.setAttribute("aria-pressed", String(show));
          setAshoreCat(c.key, show);
          onChange?.();
        });
        box.append(b);
      }
      return box;
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

    return { rows, whatsHere, ashoreNear, redrawAshore: () => drawAshore(), myFix, fromHash, openSearch, TYPES, get found() { return found; } };
  }

  window.FerryHere = { create, seaCoords, fold, score, parseCoords, TYPES, toast };
})();
