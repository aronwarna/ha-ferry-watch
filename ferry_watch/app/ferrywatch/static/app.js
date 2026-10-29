/* Ferry Watch panel. All URLs are relative because HA ingress serves this under a path prefix. */
(() => {
  "use strict";

  const TAIL_MINUTES = 60;
  const STALE_SECONDS = 30 * 60;
  const PORT_STALE_SECONDS = 24 * 3600;
  const FLEET_HIDE_SECONDS = 7 * 86400; // fleet ships not heard from in a week are left off the map
  const DEFAULT_VIEW = { center: [60.02, 24.45], zoom: 9 };

  const $ = (id) => document.getElementById(id);
  const ships = new Map(); // mmsi -> { info, marker, tail, tailPoints }
  let focused = null; // { mmsi, line, points }
  let chartInfo = undefined;
  let clockSkew = 0; // server time minus browser time, seconds

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  // Position reports arrive every few seconds; only touch the page where something changed.
  function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  // Faulty lights and buoys (see below); declared here because the chart view asks for them at once.
  let faultsOn = store.get("fw-faults") !== "0";
  let faultsData = null, faultsAt = 0, faultsLayer = null, faultsLoading = false;
  const FAULTS_REFRESH = 30 * 60 * 1000;
  // Harbours (see below) are drawn differently on the chart, so switching maps redraws them.
  let harboursOn = store.get("fw-harbours") !== "0";
  let harbourLayer = null;
  // One canvas for every canvas-drawn mark (other ships, weather stations, harbours). Each
  // Leaflet canvas covers the whole map, so a second one would swallow the taps meant for
  // the marks on the first.
  let markCanvas = null;
  const sharedCanvas = () => markCanvas || (markCanvas = L.canvas({ padding: 0.3, tolerance: 6 }));

  // --- map and layers -------------------------------------------------------

  // A fixed maxZoom: otherwise Leaflet clamps the zoom to the first layer added when the view
  // switches (the chart's ocean base stops at 10), and tapping Chart at zoom 12 zoomed out to 10.
  const map = L.map("map", { zoomControl: false, attributionControl: true, worldCopyJump: true, maxZoom: 19 })
    .setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  window.ferryMap = map; // handy from the browser console
  map.attributionControl.setPrefix(false);

  // Weather layers (overlays.js): ice, radar and wind over the map, with a shared time bar.
  // Made here, before anything can ask for the menu's rows or Settings.
  const overlays = FerryOverlays.create({
    map, store, switchRow, canvas: sharedCanvas, strip: $("strip"),
    onChange() {
      if (!$("ships-dialog").querySelector('[data-pane="layers"]').hidden) renderLayers();
      renderSettings();
    },
  });

  // HA serves the panel with a no-referrer policy; tile servers want to see where requests come from.
  const REFERRER = "strict-origin-when-cross-origin";
  // OpenStreetMap's own tile servers block requests from embedded apps like this one, and
  // CARTO's now need an API key, so the map uses Esri's free tiles (the same host as the
  // satellite view).
  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
  const streets = () => L.tileLayer(`${ESRI}/World_Topo_Map/MapServer/tile/{z}/{y}/{x}`, {
    maxZoom: 19, maxNativeZoom: 17, referrerPolicy: REFERRER, zIndex: 1,
    attribution: "© Esri, HERE, Garmin, OpenStreetMap",
  });
  // Chart and sea mark tiles come through the add-on, which keeps a copy of each on the NAS.
  const seamarks = () => L.tileLayer("api/tiles/seamark/{z}/{x}/{y}.png", {
    maxZoom: 19, maxNativeZoom: 18, zIndex: 10, attribution: "Sea marks © OpenSeaMap",
  });

  // Sweden and Estonia don't publish free charts, so the chart view fills in beyond
  // Finnish waters with Esri's ocean map (depth shading, toned to sit next to Traficom's
  // charts) and OpenSeaMap's sea marks, which the add-on leaves out where Traficom's
  // charts already draw their own.
  const oceanBase = () => L.layerGroup([
    L.tileLayer(`${ESRI}/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}`, {
      maxZoom: 10, referrerPolicy: REFERRER, zIndex: 1, className: "chart-under",
      attribution: "Ocean © Esri, GEBCO, NOAA, Garmin",
    }),
    // In the Baltic the ocean map has no detail past zoom 10 ("Map data not yet
    // available"); closer in, the ordinary map takes over, which also gives the
    // drawn chart's land its roads and towns.
    L.tileLayer(`${ESRI}/World_Topo_Map/MapServer/tile/{z}/{y}/{x}`, {
      minZoom: 11, maxZoom: 19, maxNativeZoom: 17, referrerPolicy: REFERRER, zIndex: 1,
      className: "chart-under", attribution: "© Esri, HERE, Garmin, OpenStreetMap",
    }),
  ]);
  const oceanLabels = (zIndex) => L.tileLayer(`${ESRI}/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}`, {
    maxZoom: 10, referrerPolicy: REFERRER, zIndex,
  });
  const seamarksAbroad = () => L.tileLayer("api/tiles/seamark-abroad/{z}/{x}/{y}.png", {
    maxZoom: 19, maxNativeZoom: 18, zIndex: 10, attribution: "Sea marks © OpenSeaMap",
  });

  const layers = {
    map: streets(),
    satellite: L.layerGroup([
      L.tileLayer(`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, {
        maxZoom: 19, referrerPolicy: REFERRER, zIndex: 1, attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
      }),
      L.tileLayer(`${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`, {
        maxZoom: 19, referrerPolicy: REFERRER, zIndex: 2,
      }),
    ]),
    chart: null, // built by buildChartLayer below
  };
  let activeLayer = null;

  // "drawn": the add-on's own seamless chart (vchart.js), with Traficom's overview chart
  // when zoomed far out. "official": Traficom's electronic chart (S-57 view service), drawn
  // by Traficom's server. "scanned": Traficom's scanned chart sheets.
  const CHART_STYLES = ["drawn", "official", "scanned"];
  let chartStyle = CHART_STYLES.includes(store.get("fw-chart-style")) ? store.get("fw-chart-style") : "drawn";

  function buildChartLayer(info) {
    if (chartStyle === "official") {
      return L.layerGroup([
        oceanBase(),
        // 512-pixel images one zoom out: the same scale on screen, a quarter of the requests.
        L.tileLayer("api/tiles/enc/{z}/{x}/{y}.png", {
          tileSize: 512, zoomOffset: -1, minZoom: 6, maxZoom: 19, maxNativeZoom: 18, zIndex: 3,
          attribution: "Electronic chart © Traficom (S-57 view service) · not for navigation",
        }),
        seamarksAbroad(),
      ]);
    }
    if (chartStyle === "drawn") {
      const overview = info?.layers?.find((l) => /250k/i.test(l.title || l.layer || ""));
      // The drawn chart first, so its credit leads the line at the bottom.
      return L.layerGroup([
        FerryChart.layer({
          attribution: "Chart © Traficom, Väylävirasto, Maanmittauslaitos · abroad: depths © EMODnet Bathymetry, "
            + "coast and sea marks © OpenStreetMap contributors · not for navigation",
          zIndex: 3,
        }),
        oceanBase(),
        ...(overview ? [FerryChart.rasterLayer(`api/tiles/chart-${overview.id}/{z}/{x}/{y}.png`, {
          minNativeZoom: overview.min_zoom, maxNativeZoom: overview.max_zoom, maxZoom: 9, zIndex: 2,
        })] : []),
        // No OpenSeaMap tiles here: the drawn chart draws the sea marks abroad itself.
        oceanLabels(6),
      ]);
    }
    if (!info?.layers?.length) return L.layerGroup([streets(), seamarks()]);
    // Overview charts first, then the detailed chart series on top of them.
    const charts = info.layers.map((layer, i) => FerryChart.rasterLayer(`api/tiles/chart-${layer.id}/{z}/{x}/{y}.png`, {
      minZoom: 0,
      minNativeZoom: layer.min_zoom,
      maxNativeZoom: layer.max_zoom,
      maxZoom: 19,
      zIndex: 3 + i,
      attribution: i === 0 ? info.attribution : undefined,
    }));
    return L.layerGroup([oceanBase(), oceanLabels(2), ...charts, seamarksAbroad()]);
  }

  layers.chart = buildChartLayer(null);

  function setChartStyle(style) {
    chartStyle = style;
    store.set("fw-chart-style", style);
    const wasActive = activeLayer === layers.chart;
    if (wasActive) map.removeLayer(layers.chart);
    layers.chart = buildChartLayer(chartInfo);
    if (wasActive) setLayer("chart");
    renderChartStyle();
  }

  let chartWaiting = 0;
  FerryChart.onWaiting((n) => { chartWaiting = n; renderChartStyle(); });

  function renderChartStyle() {
    const box = $("chart-style");
    box.hidden = activeLayer !== layers.chart;
    $("chart-loading").hidden = box.hidden || chartStyle !== "drawn" || chartWaiting === 0;
    box.querySelectorAll("[data-style]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.style === chartStyle)));
    $("chart-style-select").value = chartStyle;
    renderFaults();
  }

  // Tap a buoy, beacon, light, wreck or heritage site on the drawn chart to see what it is.
  map.on("click", (e) => {
    if (route.on || activeLayer !== layers.chart || chartStyle !== "drawn" || map.getZoom() < 11) return;
    const hit = FerryChart.aidAt(map, e.latlng);
    if (!hit) return;
    const link = hit.link && /^https:\/\//.test(hit.link.href || hit.link) &&
      `<a href="${esc(hit.link.href || hit.link)}" target="_blank" rel="noopener">${esc(hit.link.text || "Register entry")}</a>`;
    const lines = [hit.name && `<strong>${esc(hit.name)}</strong>`, hit.description && esc(hit.description),
      ...(hit.lines || []).filter(Boolean).map((l) => `<span class="muted">${esc(l)}</span>`),
      hit.sub && !(hit.lines || []).length && hit.sub !== hit.description && `<span class="muted">${esc(hit.sub)}</span>`,
      link].filter(Boolean);
    L.popup({ className: "aid-popup", offset: [0, -6] })
      .setLatLng(hit.latlng)
      .setContent(lines.join("<br>"))
      .openOn(map);
  });

  // Settings › Troubleshooting: a long press on the drawn chart saves what the chart has
  // there (tiles at zoom 8 to 12 and the block's depth areas) for sending in.
  let sampleOnPress = store.get("fw-chart-sample") === "1";
  map.on("contextmenu", async (e) => {
    if (!sampleOnPress || activeLayer !== layers.chart || chartStyle !== "drawn") return;
    const popup = L.popup({ className: "aid-popup", offset: [0, -6] })
      .setLatLng(e.latlng)
      .setContent("Saving the chart here (zoom 8 to 12)…")
      .openOn(map);
    try {
      const res = await api("api/vchart/sample", { method: "POST", body: { lat: e.latlng.lat, lon: e.latlng.lng } });
      popup.setContent(res.ok
        ? `<strong>Chart saved</strong><br>${esc(res.file)} (${res.mb} MB)<br>`
          + `<span class="muted">In Media › ferry_watch › chart-samples</span><br>`
          + `<a href="api/vchart/sample/${encodeURIComponent(res.file)}" download="${esc(res.file)}">Download</a>`
        : `Couldn't save it: ${esc(res.error)}`);
    } catch (err) {
      popup.setContent(`Couldn't save it: ${esc(String(err.message || err))}`);
    }
  });

  function setLayer(name) {
    if (!layers[name]) name = "map";
    if (activeLayer) map.removeLayer(activeLayer);
    activeLayer = layers[name];
    activeLayer.addTo(map);
    document.querySelectorAll("[data-layer]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.layer === name));
    });
    store.set("fw-layer", name);
    renderChartStyle();
    restyleHarbours();
  }

  document.querySelectorAll("[data-layer]").forEach((b) => {
    b.addEventListener("click", () => setLayer(b.dataset.layer));
  });
  document.querySelectorAll("#chart-style [data-style]").forEach((b) => {
    b.addEventListener("click", () => setChartStyle(b.dataset.style));
  });
  $("chart-style-select").addEventListener("change", (e) => setChartStyle(e.target.value));
  setLayer(store.get("fw-layer") || "map");

  function updateChart(info) {
    if (JSON.stringify(info) === JSON.stringify(chartInfo)) return;
    chartInfo = info;
    const wasActive = activeLayer === layers.chart;
    if (wasActive) map.removeLayer(layers.chart);
    layers.chart = buildChartLayer(info);
    if (wasActive) setLayer("chart");
  }

  // --- formatting -------------------------------------------------------------

  const now = () => Date.now() / 1000 + clockSkew;
  // One formatter for every clock time: toLocaleTimeString builds a new one each call, which
  // showed up as the panel's busiest function while ships moved on the map.
  const CLOCK = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const clock = (ts) => CLOCK.format(new Date(ts * 1000));
  const shortName = (name) => name.split(" ").slice(-1)[0];

  function ago(ts) {
    const s = Math.max(0, Math.round(now() - ts));
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min ago`;
    return new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short", hour12: false });
  }

  function duration(seconds) {
    const m = Math.round(seconds / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
  }

  function compass(deg) {
    if (deg == null) return "";
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
  }

  function motionText(info) {
    const p = info.position;
    if (!p) return "No position yet";
    if (info.in_port) {
      const since = info.stopped_since ? ` since ${clock(info.stopped_since)}` : "";
      return `In port${info.port ? ` in ${info.port}` : ""}${since}`;
    }
    const parts = [];
    if (p.sog != null) parts.push(`${p.sog.toFixed(1)} kn`);
    const dir = compass(p.heading ?? p.cog);
    if (dir && p.sog > 0.5) parts.push(`heading ${dir}`);
    if (info.destination) parts.push(destinationText(info.destination));
    return parts.join(" · ") || "Stopped";
  }

  function titleCase(text) {
    return text === text.toUpperCase()
      ? text.toLowerCase().replace(/(^|[\s-])\p{L}/gu, (c) => c.toUpperCase())
      : text;
  }

  // AIS destinations are often UN/LOCODEs, e.g. "SESTO<>FIMHQ<>FIHEL" for a route.
  const LOCODES = {
    SESTO: "Stockholm", FIHEL: "Helsinki", FIMHQ: "Mariehamn", EETLL: "Tallinn", FITKU: "Turku",
    SEKPS: "Kapellskär", FIHKO: "Hanko", FIVAA: "Vaasa", SEUME: "Umeå", DEROS: "Rostock",
    DETRV: "Travemünde", DEKEL: "Kiel", RULED: "St Petersburg", SENYN: "Nynäshamn", PLGDY: "Gdynia",
  };

  function placeName(text) {
    const code = text.replace(/\s+/g, "").toUpperCase();
    return LOCODES[code] || titleCase(text.trim());
  }

  function destinationText(raw) {
    if (raw.includes("<>")) return `route ${raw.split("<>").map(placeName).join(" – ")}`;
    const legs = raw.split(/\s*-?>\s*/).filter(Boolean);
    return `to ${placeName(legs[legs.length - 1] || raw)}`;
  }

  // Moored ships can go hours without a new AIS position, so a ship in port only counts
  // as out of contact after a day.
  const isStale = (info) => !info.position
    || now() - info.position.ts > (info.in_port ? PORT_STALE_SECONDS : STALE_SECONDS);

  // --- markers ----------------------------------------------------------------

  function shipSvg(color, rotation, size = 28) {
    return `<svg width="${size}" height="${size}" viewBox="-14 -14 28 28" style="transform: rotate(${rotation}deg)">
      <path d="M0 -12 L6 -2 L6 10 Q0 13 -6 10 L-6 -2 Z" fill="${color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>
    </svg>`;
  }

  const rotationOf = (info) => (info.position ? (info.position.heading ?? info.position.cog ?? 0) : 0);

  function markerIcon(info, dimmed) {
    const classes = ["ship-icon"];
    if (dimmed) classes.push("dimmed");
    if (isStale(info)) classes.push("stale");
    if (info.fleet) classes.push("fleet");
    const size = info.fleet ? 20 : 28;
    return L.divIcon({
      className: classes.join(" "),
      html: shipSvg(info.color, rotationOf(info), size),
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    });
  }

  function refreshIcon(entry, dimmed) {
    const info = entry.info;
    const key = `${info.color}|${dimmed}|${isStale(info)}|${Boolean(info.fleet)}`;
    if (entry.iconKey !== key) {
      entry.iconKey = key;
      entry.rotation = rotationOf(info);
      entry.marker.setIcon(markerIcon(info, dimmed));
      return;
    }
    const rotation = rotationOf(info);
    if (rotation !== entry.rotation) {
      entry.rotation = rotation;
      const svg = entry.marker.getElement()?.querySelector("svg");
      if (svg) svg.style.transform = `rotate(${rotation}deg)`;
    }
  }

  // --- aircraft (adsb.fi) ----------------------------------------------------------

  const AIRCRAFT_COLORS = { FinnHEMS: "#dc2626", "Border Guard": "#15803d", Police: "#1d4ed8" };
  const ROLE_NAMES = { FinnHEMS: "Medical helicopter (FinnHEMS)", "Border Guard": "Border Guard", Police: "Police" };
  const AIRCRAFT_CREDIT = '<a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a>';
  const aircraft = new Map(); // hex -> { marker, trail }
  let aircraftCredit = false;

  function aircraftSvg(ac, color) {
    const rotation = ac.track ?? 0;
    const body = ac.helicopter
      ? `<ellipse cx="0" cy="1" rx="4.5" ry="6" fill="${color}" stroke="#fff" stroke-width="1.5"/>
         <path d="M0 6 L0 12 M-3 12 L3 12" stroke="${color}" stroke-width="2.2" stroke-linecap="round"/>
         <path d="M-11 -9 L11 9 M11 -9 L-11 9" stroke="#fff" stroke-width="3.4" stroke-linecap="round"/>
         <path d="M-11 -9 L11 9 M11 -9 L-11 9" stroke="${color}" stroke-width="1.6" stroke-linecap="round"/>`
      : `<path d="M0 -12 L2 -4 L11 1 L11 3 L2 1 L1.5 8 L5 11 L5 12.5 L0 11 L-5 12.5 L-5 11 L-1.5 8 L-2 1 L-11 3 L-11 1 L-2 -4 Z"
           fill="${color}" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>`;
    return `<svg width="26" height="26" viewBox="-13 -13 26 26" style="transform: rotate(${rotation}deg)">${body}</svg>`;
  }

  function aircraftLabel(ac) {
    const name = ac.callsign || ac.reg;
    const squash = (t) => t.toLowerCase().replace(/\s/g, "");
    if (!ac.operator || squash(name).includes(squash(ac.operator))) return name;
    return `${ac.operator} ${name}`;
  }

  // Photos come from Planespotters.net, straight to the browser as their terms ask
  // (credited, linked, never stored by the add-on). Looked up once per aircraft.
  const PHOTO_TTL = 24 * 3600 * 1000;
  const acPhotos = new Map(); // hex -> photo | null | Promise

  function aircraftPhoto(hex) {
    if (acPhotos.has(hex)) return acPhotos.get(hex);
    try {
      const saved = JSON.parse(store.get(`fw-acphoto-${hex}`) || "null");
      if (saved && Date.now() - saved.at < PHOTO_TTL) {
        acPhotos.set(hex, saved.photo);
        return saved.photo;
      }
    } catch { /* ignore */ }
    const lookup = fetch(`https://api.planespotters.net/pub/photos/hex/${encodeURIComponent(hex)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const p = data?.photos?.[0];
        const photo = p ? { src: (p.thumbnail_large || p.thumbnail).src, link: p.link, by: p.photographer } : null;
        acPhotos.set(hex, photo);
        store.set(`fw-acphoto-${hex}`, JSON.stringify({ at: Date.now(), photo }));
        return photo;
      })
      .catch(() => { acPhotos.set(hex, null); return null; });
    acPhotos.set(hex, lookup);
    return lookup;
  }

  function aircraftPopupElement() {
    const el = document.createElement("div");
    el.className = "ac-popup";
    el.innerHTML = `<a class="ac-photo" target="_blank" rel="noopener" hidden><img alt=""><span class="credit"></span></a>
      <div class="ac-body"><strong class="ac-name"></strong><div class="ac-type"></div><div class="ac-role muted"></div>
      <div class="ac-flight"></div><div class="ac-alert" hidden></div><div class="ac-track" hidden></div>
      <div class="ac-foot muted"><span class="ac-seen"></span> · <a target="_blank" rel="noopener">adsb.fi</a>
      · <button type="button" class="link-button ac-hist">History</button></div></div>`;
    return el;
  }

  function showAircraftPhoto(el, photo) {
    const a = el.querySelector(".ac-photo");
    if (!photo || a.dataset.src === photo.src) { a.hidden = !photo; return; }
    a.dataset.src = photo.src;
    a.href = photo.link;
    a.querySelector("img").src = photo.src;
    a.querySelector(".credit").textContent = `© ${photo.by} · Planespotters.net`;
    a.hidden = false;
  }

  function fillAircraftPopup(entry, ac) {
    const el = entry.popupEl;
    el.querySelector(".ac-name").textContent = aircraftLabel(ac);
    const reg = ac.reg?.replace("-", "\u2011"); // keep OH-HMV on one line
    el.querySelector(".ac-type").textContent = [ac.type_name || ac.type, reg].filter(Boolean).join(" · ");
    const role = ROLE_NAMES[ac.operator];
    const owner = ac.owner && (!role || !ac.owner.toLowerCase().includes(ac.operator.toLowerCase())) ? ac.owner : null;
    el.querySelector(".ac-role").textContent = [role, owner, ac.year ? `built ${ac.year}` : null].filter(Boolean).join(" · ");
    const climb = Math.abs(ac.climb_fpm || 0) >= 300 ? (ac.climb_fpm > 0 ? "climbing" : "descending") : null;
    el.querySelector(".ac-flight").textContent = [
      ac.alt_ft != null ? (ac.alt_ft ? `${Math.round(ac.alt_ft * 0.3048).toLocaleString()} m` : "On the ground") : null,
      ac.gs_kn != null && ac.alt_ft !== 0 ? `${Math.round(ac.gs_kn)} kn (${Math.round(ac.gs_kn * 1.852)} km/h)` : null,
      climb,
    ].filter(Boolean).join(" · ");
    const alert = el.querySelector(".ac-alert");
    alert.hidden = !ac.emergency;
    alert.textContent = ac.emergency ? `Emergency: ${ac.emergency}${ac.squawk ? ` (squawk ${ac.squawk})` : ""}` : "";
    el.querySelector(".ac-seen").textContent = `Last report ${ago(ac.ts)}`;
    el.querySelector(".ac-foot a").href = `https://globe.adsb.fi/?icao=${encodeURIComponent(ac.hex)}`;
    const photo = aircraftPhoto(ac.hex);
    if (photo instanceof Promise) {
      photo.then((p) => { showAircraftPhoto(el, p); entry.marker.getPopup()?.update(); });
    } else {
      showAircraftPhoto(el, photo);
    }
  }

  function renderAircraft(list) {
    const seen = new Set();
    for (const ac of list || []) {
      seen.add(ac.hex);
      const color = AIRCRAFT_COLORS[ac.operator] || "#7c3aed";
      let entry = aircraft.get(ac.hex);
      const icon = L.divIcon({ className: "aircraft-icon", html: aircraftSvg(ac, color), iconSize: [26, 26], iconAnchor: [13, 13] });
      if (!entry) {
        const popupEl = aircraftPopupElement();
        entry = {
          hex: ac.hex,
          popupEl,
          flight: null, // the whole flight while the popup is open
          marker: L.marker([ac.lat, ac.lon], { icon, zIndexOffset: 1000, keyboard: false })
            .bindTooltip("", { permanent: true, direction: "right", offset: [12, 0], className: "ship-label aircraft-label" })
            .bindPopup(popupEl, { minWidth: 220, maxWidth: 260, className: "ac-popup-wrap", autoPanPaddingTopLeft: [12, 130] }),
          trail: L.polyline([], { color, weight: 2, opacity: 0.6, dashArray: "2 5", interactive: false }),
        };
        entry.trail.addTo(map);
        entry.marker.addTo(map);
        const e = entry;
        e.marker.on("popupopen", () => showFlight(e));
        e.marker.on("popupclose", () => hideFlight(e));
        popupEl.querySelector(".ac-hist").addEventListener("click", () => {
          map.closePopup();
          historyView.open({ icao: e.hex });
        });
        aircraft.set(ac.hex, entry);
      }
      entry.ac = ac;
      if (entry.flight) {
        if (ac.flight_id && ac.flight_id !== entry.flightId) showFlight(entry); // a new flight began
        else entry.flight.add(livePoint(ac));
      }
      entry.marker.setLatLng([ac.lat, ac.lon]).setIcon(icon);
      entry.marker.setTooltipContent(esc(aircraftLabel(ac)));
      fillAircraftPopup(entry, ac);
      entry.trail.setLatLngs(ac.trail || []);
    }
    for (const [hex, entry] of aircraft) {
      if (seen.has(hex)) continue;
      entry.marker.remove();
      entry.trail.remove();
      entry.flight?.remove();
      aircraft.delete(hex);
    }
    renderAirBadge(list || []);
    const info = lastState?.aircraft;
    $("air-history-button").hidden = !(info?.enabled && (aircraft.size || info.flights_stored));
    const on = Boolean(lastState?.aircraft?.enabled);
    if (on !== aircraftCredit) {
      if (on) map.attributionControl.addAttribution(AIRCRAFT_CREDIT);
      else map.attributionControl.removeAttribution(AIRCRAFT_CREDIT);
      aircraftCredit = on;
    }
  }

  // A badge under the top bar whenever a rescue aircraft is up; tap it to see them.
  function renderAirBadge(list) {
    const badge = $("air-badge");
    const flying = list.filter((ac) => ac.alt_ft !== 0);
    badge.hidden = !flying.length;
    if (!flying.length) return;
    const heli = flying.every((ac) => ac.helicopter);
    badge.classList.toggle("plane", !heli);
    $("air-badge-text").textContent = flying.length === 1
      ? `${aircraftLabel(flying[0])} in the air`
      : `${flying.length} ${heli ? "helicopters" : "aircraft"} in the air`;
  }

  // A selected aircraft: its whole flight so far, from the add-on's history, coloured by
  // altitude, in place of the short dashed trail.
  const livePoint = (ac) => [ac.lat, ac.lon, ac.ts, ac.alt_ft != null ? Math.round(ac.alt_ft * 0.3048) : null];

  async function showFlight(entry) {
    const id = entry.ac?.flight_id;
    entry.flightId = id;
    if (!id) return;
    let flight;
    try {
      flight = await api(`api/aircraft/flights/${id}`);
    } catch (err) {
      console.error(err);
      return;
    }
    if (!entry.marker.isPopupOpen() || entry.flightId !== id || aircraft.get(entry.hex) !== entry) return;
    entry.flight?.remove();
    entry.flight = FerryHistory.flightTrack(map, flight.track);
    entry.flight.add(livePoint(entry.ac));
    entry.trail.remove();
    const box = entry.popupEl.querySelector(".ac-track");
    box.innerHTML = `${FerryHistory.altLegend()}<span class="muted">This flight since ${clock(flight.start_ts)} · ${flight.dist_km} km</span>`;
    box.hidden = false;
    entry.marker.getPopup()?.update();
  }

  function hideFlight(entry) {
    entry.flight?.remove();
    entry.flight = null;
    entry.flightId = null;
    entry.popupEl.querySelector(".ac-track").hidden = true;
    if (aircraft.get(entry.hex) === entry) entry.trail.addTo(map);
  }

  // Photos already looked up, without asking Planespotters again.
  function knownAircraftPhoto(hex) {
    const p = acPhotos.get(hex);
    if (p !== undefined && !(p instanceof Promise)) return p;
    try {
      return JSON.parse(store.get(`fw-acphoto-${hex}`) || "null")?.photo || null;
    } catch {
      return null;
    }
  }

  const historyView = FerryHistory.view({
    map,
    api: (url) => api(url),
    esc: (text) => esc(text),
    knownPhoto: knownAircraftPhoto,
    enabled: () => Boolean(lastState?.aircraft?.enabled),
    onOpen() {
      if ($("ships-dialog").open) $("ships-dialog").close();
      if (focused) unfocus();
      clearReplay();
      if (route.on) setRouteMode(false);
      if (editing) setEditing(false);
      map.closePopup();
      $("strip").hidden = true;
    },
    onClose() { $("strip").hidden = Boolean(focused) || editing || Boolean(replayLayer); },
  });
  $("air-history-button").addEventListener("click", () => (historyView.isOpen() ? historyView.close() : historyView.open()));

  $("air-badge").addEventListener("click", () => {
    const entries = [...aircraft.values()];
    if (!entries.length) return;
    if (entries.length === 1) {
      map.setView(entries[0].marker.getLatLng(), Math.max(map.getZoom(), 10));
      entries[0].marker.openPopup();
    } else {
      map.fitBounds(L.latLngBounds(entries.map((e) => e.marker.getLatLng())).pad(0.3), { maxZoom: 11 });
    }
  });

  function upsertShip(info) {
    let entry = ships.get(info.mmsi);
    if (!entry) {
      entry = { info, marker: null, tail: null, tailPoints: [] };
      ships.set(info.mmsi, entry);
    }
    entry.info = info;
    const p = info.position;
    if (!p) return entry;
    if (info.fleet && now() - p.ts > FLEET_HIDE_SECONDS) {
      entry.marker?.remove();
      entry.tail?.remove();
      entry.marker = entry.tail = entry.iconKey = null;
      return entry;
    }
    const dimmed = focused && focused.mmsi !== info.mmsi;
    if (!entry.marker) {
      // Fleet ships are smaller, sit below the main ships and are named on hover only.
      entry.iconKey = `${info.color}|${dimmed}|${isStale(info)}|${Boolean(info.fleet)}`;
      entry.rotation = rotationOf(info);
      entry.label = info.fleet ? info.name : shortName(info.name);
      entry.marker = L.marker([p.lat, p.lon], {
        icon: markerIcon(info, dimmed), keyboard: true, title: info.name, zIndexOffset: info.fleet ? -500 : 0,
      })
        .on("click", () => focusShip(info.mmsi))
        .bindTooltip(info.fleet ? info.name : shortName(info.name), info.fleet
          ? { direction: "top", offset: [0, -8], className: "ship-label" }
          : { permanent: true, direction: "right", offset: [12, 0], className: "ship-label" })
        .addTo(map);
      entry.tail = L.polyline([], {
        color: info.color, weight: info.fleet ? 1.5 : 2.5, opacity: tailOpacity(info), interactive: false,
      }).addTo(map);
    } else {
      const ll = entry.marker.getLatLng();
      if (ll.lat !== p.lat || ll.lng !== p.lon) entry.marker.setLatLng([p.lat, p.lon]);
      refreshIcon(entry, dimmed);
      const label = info.fleet ? info.name : shortName(info.name);
      if (label !== entry.label) { entry.label = label; entry.marker.getTooltip()?.setContent(label); }
    }
    if (entry.tail.options.color !== info.color) entry.tail.setStyle({ color: info.color });
    entry.marker.getTooltip()?.getElement()?.classList.toggle("dimmed", Boolean(dimmed));
    return entry;
  }

  const tailOpacity = (info) => (info.fleet ? 0.4 : 0.55);

  function removeShip(mmsi) {
    const entry = ships.get(mmsi);
    if (!entry) return;
    entry.marker?.remove();
    entry.tail?.remove();
    ships.delete(mmsi);
    if (focused?.mmsi === mmsi) unfocus();
  }

  function setTail(entry, points) {
    entry.tailPoints = points;
    entry.tail?.setLatLngs(points.map((p) => [p[0], p[1]]));
  }

  function appendPoint(points, p) {
    const last = points[points.length - 1];
    if (!last || p[2] > last[2]) points.push(p);
  }

  function onPosition(info) {
    if (!ships.has(info.mmsi) && !info.name) return; // not loaded yet; the next state load brings it
    const entry = upsertShip({ ...ships.get(info.mmsi)?.info, ...info });
    const p = info.position;
    if (!p || !entry.tail) return;
    appendPoint(entry.tailPoints, [p.lat, p.lon, p.ts]);
    const cutoff = now() - TAIL_MINUTES * 60;
    while (entry.tailPoints.length && entry.tailPoints[0][2] < cutoff) entry.tailPoints.shift();
    entry.tail.setLatLngs(entry.tailPoints.map((q) => [q[0], q[1]]));
    if (focused?.mmsi === info.mmsi) {
      appendPoint(focused.points, [p.lat, p.lon, p.ts, p.sog]);
      focused.line?.set(focused.points);
      renderCard();
    }
    renderStrip();
  }

  // --- speed-coloured track -------------------------------------------------------

  // Slow to fast: purple, blue, teal, green, amber, red (knots).
  const SPEED_STOPS = [[0, [91, 42, 134]], [6, [37, 99, 235]], [12, [13, 148, 136]], [17, [101, 163, 13]],
    [21, [245, 158, 11]], [25, [220, 38, 38]]];
  const SPEED_MAX = SPEED_STOPS[SPEED_STOPS.length - 1][0];

  function speedColor(kn) {
    const v = Math.max(0, Math.min(SPEED_MAX, Math.round(kn ?? 0)));
    const i = SPEED_STOPS.findIndex(([k]) => k >= v);
    if (i <= 0) return `rgb(${SPEED_STOPS[0][1]})`;
    const [k0, c0] = SPEED_STOPS[i - 1], [k1, c1] = SPEED_STOPS[i];
    const t = (v - k0) / (k1 - k0);
    return `rgb(${c0.map((c, j) => Math.round(c + (c1[j] - c) * t))})`;
  }

  const SPEED_GRADIENT = `linear-gradient(90deg, ${SPEED_STOPS.map(([k]) => `${speedColor(k)} ${(k / SPEED_MAX) * 100}%`).join(", ")})`;

  // The focused ship's voyage: a dark outline with the line coloured by speed on top.
  // Tap anywhere on it to see the speed and time there.
  function speedTrack(points) {
    const group = L.layerGroup();
    const outline = L.polyline([], { color: "#111827", weight: 8, opacity: 0.35, lineCap: "round", lineJoin: "round" });
    outline.on("click", (e) => {
      let best = null, bestD = Infinity;
      for (const p of group.points) {
        const d = map.distance(e.latlng, [p[0], p[1]]);
        if (d < bestD) { best = p; bestD = d; }
      }
      if (!best) return;
      const kn = best[3];
      L.popup({ className: "speed-popup", closeButton: false, offset: [0, -4] })
        .setLatLng([best[0], best[1]])
        .setContent(`<strong>${kn != null ? `${kn.toFixed(1)} knots` : "Speed unknown"}</strong>`
          + `<br><span class="muted">${clock(best[2])}</span>`)
        .openOn(map);
    });
    group.addLayer(outline);
    const segments = L.layerGroup().addTo(group);
    group.set = (pts) => {
      group.points = pts;
      outline.setLatLngs(pts.map((p) => [p[0], p[1]]));
      segments.clearLayers();
      // One line per run of points with the same colour keeps the layer count low.
      let run = [], color = null;
      for (let i = 0; i < pts.length; i++) {
        const c = speedColor(pts[i][3]);
        if (c !== color && run.length) {
          run.push([pts[i][0], pts[i][1]]);
          segments.addLayer(L.polyline(run, { color, weight: 4.5, opacity: 1, interactive: false, lineCap: "round" }));
          run = [];
        }
        color = c;
        run.push([pts[i][0], pts[i][1]]);
      }
      if (run.length > 1) segments.addLayer(L.polyline(run, { color, weight: 4.5, opacity: 1, interactive: false, lineCap: "round" }));
    };
    group.set(points);
    return group;
  }

  // --- focus a ship -------------------------------------------------------------

  async function focusShip(mmsi, { fit = true } = {}) {
    const entry = ships.get(mmsi);
    if (!entry || editing) return;
    if (route.on) { // measuring: a tapped ship becomes a point, to see how far it is
      const p = entry.info.position;
      if (p) addRoutePoint(L.latLng(p.lat, p.lon));
      return;
    }
    clearReplay();
    stopPlayback();
    if (historyView.isOpen()) historyView.close();
    focused?.line?.remove();
    const mine = { mmsi, line: null, points: [] };
    focused = mine;
    applyDimming();
    setCardState("full", { animate: false });
    renderCard();
    $("strip").hidden = true;
    history.replaceState(null, "", `#ship=${mmsi}`);

    let data;
    try {
      data = await (await fetch(`api/track/${mmsi}?since=voyage`)).json();
    } catch (err) {
      console.error(err);
      return;
    }
    if (focused !== mine) return; // closed or another ship chosen meanwhile
    mine.points = data.points;
    mine.line = speedTrack(data.points).addTo(map);
    entry.marker?.setZIndexOffset(1000);
    renderCard();
    if (fit) zoomToTrack();
  }

  function zoomToTrack() {
    if (!focused) return;
    const entry = ships.get(focused.mmsi);
    const pts = focused.points.map((p) => [p[0], p[1]]);
    if (pts.length > 1) {
      map.fitBounds(L.latLngBounds(pts), { ...cardPadding(), maxZoom: 13 });
    } else if (entry?.info.position) {
      map.setView([entry.info.position.lat, entry.info.position.lon], 12);
    }
  }

  function cardPadding() {
    // Keep the track clear of the card: below it on phones, beside it on wider screens.
    const card = $("card");
    if (card.hidden) return { paddingTopLeft: [30, 70], paddingBottomRight: [30, 90] };
    if (window.innerWidth <= 560) {
      const h = card.classList.contains("sliding") ? cardTargetH : card.offsetHeight;
      return { paddingTopLeft: [30, 70], paddingBottomRight: [30, h + 40] };
    }
    return { paddingTopLeft: [card.offsetWidth + 40, 70], paddingBottomRight: [40, 40] };
  }

  function fitAllShips() {
    const pts = [...ships.values()].filter((e) => !e.info.fleet && e.info.position && !isStale(e.info))
      .map((e) => [e.info.position.lat, e.info.position.lon]);
    if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), { ...cardPadding(), maxZoom: 11 });
  }

  function unfocus() {
    stopPlayback();
    resetCardSheet();
    if (focused?.line) focused.line.remove();
    const entry = focused && ships.get(focused.mmsi);
    entry?.marker?.setZIndexOffset(0);
    focused = null;
    applyDimming();
    $("card").hidden = true;
    $("strip").hidden = editing;
    history.replaceState(null, "", location.pathname + location.search);
    renderStrip();
  }

  function applyDimming() {
    for (const entry of ships.values()) {
      const dimmed = Boolean(focused && focused.mmsi !== entry.info.mmsi);
      if (entry.marker) {
        refreshIcon(entry, dimmed);
        entry.marker.getTooltip()?.getElement()?.classList.toggle("dimmed", dimmed);
      }
      entry.tail?.setStyle({ opacity: dimmed ? 0.15 : tailOpacity(entry.info) });
      if (focused && focused.mmsi === entry.info.mmsi) entry.tail?.setStyle({ opacity: 0 });
    }
  }

  function showLine(el, text) {
    el.hidden = !text;
    setText(el, text || "");
  }

  // "Reaches the Porkkala lines ≈18:42 (in 23 min) · inner route 9 of last 14 sailings"
  const NEXT_HORIZON = 3 * 3600;
  function nextPassage(info) {
    const n = info.next_passage;
    if (!n || info.fleet) return null;
    const left = n.eta_ts - now();
    return left > -120 && left < NEXT_HORIZON ? { ...n, left } : null;
  }

  function nextPassageText(info) {
    const n = nextPassage(info);
    if (!n) return "";
    const when = n.left < 60 ? "now" : `≈${clock(n.eta_ts)} (in ${duration(n.left)})`;
    const habit = n.of ? ` · inner route ${n.inner} of last ${n.of} ${n.of === 1 ? "sailing" : "sailings"}` : "";
    return `Reaches the Porkkala lines ${when}${habit}`;
  }

  // The crew's own ETA from AIS, with the draught: "ETA Stockholm 09:45 (AIS) · draught 7.1 m"
  function etaText(info) {
    const parts = [];
    if (info.eta_ais) {
      const d = new Date(info.eta_ais * 1000);
      const sameDay = d.toDateString() === new Date(now() * 1000).toDateString();
      const day = sameDay ? "" : `${d.toLocaleDateString("en-GB", { weekday: "short" })} `;
      const legs = (info.destination || "").split(/<>|\s*-?>\s*/).filter(Boolean);
      const place = legs.length ? placeName(legs[legs.length - 1]) : "";
      parts.push(`ETA ${place ? `${place} ` : ""}${day}${clock(info.eta_ais)} (AIS)`);
    }
    if (info.draught) parts.push(`${parts.length ? "draught" : "Draught"} ${info.draught.toFixed(1)} m`);
    return parts.join(" · ");
  }

  // Distance from the phone, and how close the ship comes on its present course.
  function phoneFix() {
    const ph = lastState?.phone;
    return ph && now() - ph.ts <= (ph.max_age || 1800) ? ph : null;
  }

  function phoneText(info) {
    const ph = phoneFix(), p = info.position;
    if (!ph || !p || isStale(info)) return "";
    const kx = 111320 * Math.cos((ph.lat * Math.PI) / 180), ky = 110540;
    const rx = (p.lon - ph.lon) * kx, ry = (p.lat - ph.lat) * ky;
    const dist = Math.hypot(rx, ry);
    const km = (m) => (m < 1000 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`);
    let text = `${km(dist)} from you`;
    if (!info.in_port && p.sog > 2 && p.cog != null) {
      const v = p.sog * 0.5144, a = (p.cog * Math.PI) / 180;
      const vx = v * Math.sin(a), vy = v * Math.cos(a);
      const t = -(rx * vx + ry * vy) / (v * v);
      if (t <= 0) text += " · moving away";
      else if (t < 2 * 3600) text += ` · closest ${km(Math.hypot(rx + vx * t, ry + vy * t))} at ${clock(p.ts + t)}`;
    }
    return text;
  }

  function renderCard() {
    if (!focused) return;
    const entry = ships.get(focused.mmsi);
    if (!entry) return;
    const info = entry.info;
    $("card").hidden = false;
    setText($("card-name"), info.name);
    $("card-dot").style.background = info.color;
    setText($("card-motion"), motionText(info));
    showLine($("card-next"), nextPassageText(info));
    showLine($("card-eta"), etaText(info));
    showLine($("card-phone"), phoneText(info));

    let voyage = "";
    if (info.voyage_start) {
      const since = info.voyage_from ? `since ${info.voyage_from} ${clock(info.voyage_start)}` : `since ${clock(info.voyage_start)}`;
      voyage = `Track ${since} (${duration(now() - info.voyage_start)})`;
      if (now() - info.voyage_start > 86400) {
        voyage = `Track since ${new Date(info.voyage_start * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short", hour12: false })}`;
      }
    }
    setText($("card-voyage"), voyage);
    setText($("card-seen"), info.position ? `Last report ${ago(info.position.ts)}` : "Waiting for the first report");
    const speeds = (focused.points || []).map((p) => p[3]).filter((v) => v != null);
    $("card-speed").hidden = speeds.length < 2;
    if (speeds.length >= 2) {
      $("card-speed-bar").style.background = SPEED_GRADIENT;
      $("card-speed-max").textContent = `${SPEED_MAX}+ kn`;
      $("card-speed-top").textContent = `Top ${Math.max(...speeds).toFixed(1)} kn`;
    }
    const fleet = info.fleet && lastState?.fleets?.find((f) => f.id === info.fleet);
    $("card-fleet").hidden = !fleet;
    if (fleet) $("card-fleet").textContent = `${fleet.name} fleet`;
    $("card-follow").hidden = !info.fleet;
    $("card-unfollow").hidden = info.fleet !== "followed";
    $("card-play").hidden = (focused.points || []).length < 2;
    $("peek-play").hidden = (focused.points || []).length < 2;
    renderCardStats(info);
    renderPeek(info);

    const photo = info.photo;
    const box = $("card-photo");
    if (photo) {
      const src = `api/photo/${info.mmsi}?v=${photo.version}`;
      if ($("card-img").getAttribute("src") !== src) $("card-img").src = src;
      $("card-img").alt = `Photo of ${info.name}`;
      box.classList.remove("empty");
      const credit = $("card-credit");
      credit.hidden = !photo.credit_url;
      if (photo.credit_url) credit.href = photo.credit_url;
    } else {
      $("card-img").removeAttribute("src");
      box.classList.add("empty");
    }
  }

  // Inner-route numbers for one of my ships, from the same data as the Stats tab.
  let statsLoading = false;
  let statsLoadedAt = 0;
  function refreshStatsData() {
    if (statsLoading) return;
    statsLoading = true;
    api("api/stats")
      .then((data) => { statsData = data; statsLoadedAt = Date.now(); renderCard(); })
      .catch(console.error)
      .finally(() => { statsLoading = false; });
  }

  function renderCardStats(info) {
    const box = $("card-stats");
    if (info.fleet) { box.hidden = true; return; }
    if (!statsData || Date.now() - statsLoadedAt > 5 * 60000) refreshStatsData();
    if (!statsData) { box.hidden = true; return; }
    const today = new Date();
    const mine = statsData.passages.filter(([, mmsi]) => mmsi === info.mmsi).map(([ts, , route, direction]) => {
      const d = new Date(ts * 1000);
      return { ts, route, direction, year: d.getFullYear(), month: d.getMonth(), minute: d.getHours() * 60 + d.getMinutes() };
    });
    const inner = mine.filter((p) => p.route === "inner");
    const year = inner.filter((p) => p.year === today.getFullYear());
    const month = year.filter((p) => p.month === today.getMonth());
    const tiles = [
      [String(month.length), `in ${MONTHS[today.getMonth()]}`],
      [String(year.length), `in ${today.getFullYear()}`],
      [mine.length ? `${Math.round((100 * inner.length) / mine.length)}%` : "–", "inner route"],
    ];
    $("card-stat-tiles").replaceChildren(...tiles.map(([value, label]) => {
      const tile = document.createElement("div");
      tile.className = "tile";
      const v = document.createElement("strong");
      v.textContent = value;
      const l = document.createElement("span");
      l.textContent = label;
      tile.append(v, l);
      return tile;
    }));
    const notes = [];
    const last = inner.at(-1);
    if (last) notes.push(`Last inner passage ${ago(last.ts)}, ${DIRECTIONS[last.direction] || ""}`.replace(/, $/, ""));
    for (const [dir, label] of [["east", "towards Helsinki"], ["west", "towards Stockholm"]]) {
      const times = inner.filter((p) => p.direction === dir).map((p) => p.minute);
      if (times.length >= 3) notes.push(`Usually ${hhmm(median(times))} ${label}`);
    }
    $("card-stat-note").replaceChildren(...(notes.length ? notes : ["No inner-route passages recorded yet."]).map((text) => {
      const line = document.createElement("span");
      line.textContent = text;
      return line;
    }));
    box.hidden = false;
  }

  $("card-close").addEventListener("click", unfocus);
  $("card-unfollow").addEventListener("click", async () => {
    const info = focused && ships.get(focused.mmsi)?.info;
    if (!info) return;
    unfocus();
    await api(`api/follow/${info.mmsi}`, { method: "DELETE" });
    await loadState();
  });
  $("card-follow").addEventListener("click", async () => {
    // A fleet ship becomes one of my ships: alerts, stats and a photo.
    const info = focused && ships.get(focused.mmsi)?.info;
    if (!info?.fleet) return;
    await api("api/ships", { method: "POST", body: { mmsi: info.mmsi, name: info.name } });
    await loadState();
    await loadTails();
    focusShip(info.mmsi);
  });
  $("card-zoom").addEventListener("click", () => {
    if (phone()) setCardState("peek");
    zoomToTrack();
  });

  // --- the card as a sheet ----------------------------------------------------------
  // Full shows the whole card; peek leaves a 72 px banner (photo, name, what it's doing)
  // so the map is free, e.g. to watch a playback. On phones the card is a bottom sheet:
  // drag it between the two, or flick it down to close it; tap the banner to open it.

  let cardState = "full", cardTargetH = 0, slideTimer = 0, lastDragEnd = 0, drag = null;
  const phone = () => window.innerWidth <= 560;
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");

  function renderPeek(info) {
    setText($("peek-name"), info.name);
    const next = nextPassage(info);
    const soon = next ? `Porkkala ${next.left < 60 ? "now" : `in ${duration(next.left)}`} · ` : "";
    setText($("peek-status"), `${soon}${motionText(info)}`);
    const ring = $("peek-photo");
    ring.style.borderColor = info.color;
    const src = info.photo ? `api/photo/${info.mmsi}?v=${info.photo.version}` : "";
    if (ring.dataset.src !== src || (!src && ring.dataset.color !== info.color)) {
      ring.dataset.src = src;
      ring.dataset.color = info.color;
      ring.classList.toggle("empty", !src);
      ring.innerHTML = src ? `<img src="${esc(src)}" alt="">` : shipSvg(info.color, 45, 26);
    }
  }

  // The card's height in a state, measured without showing it.
  function cardHeightIn(state) {
    const card = $("card"), peek = card.classList.contains("peek"), h = card.style.height;
    card.style.height = "";
    card.classList.toggle("peek", state === "peek");
    const v = card.offsetHeight;
    card.classList.toggle("peek", peek);
    card.style.height = h;
    return v;
  }

  function slideCard(from, to, done) {
    const card = $("card");
    cardTargetH = to;
    clearTimeout(slideTimer);
    if (reduceMotion?.matches) { card.style.height = ""; done?.(); return; }
    card.classList.remove("sliding");
    card.style.height = `${from}px`;
    void card.offsetHeight; // start from there
    card.classList.add("sliding");
    card.style.height = `${to}px`;
    slideTimer = setTimeout(() => {
      card.classList.remove("sliding");
      card.style.height = "";
      done?.();
    }, 280);
  }

  function setCardState(state, { animate = true } = {}) {
    const card = $("card");
    const from = card.hidden ? 0 : card.offsetHeight;
    cardState = state;
    card.classList.toggle("peek", state === "peek");
    $("card-collapse").setAttribute("aria-expanded", String(state === "full"));
    if (!animate || card.hidden) {
      clearTimeout(slideTimer);
      card.classList.remove("sliding");
      card.style.height = "";
      return;
    }
    card.style.height = "";
    slideCard(from, card.offsetHeight);
  }

  function resetCardSheet() {
    clearTimeout(slideTimer);
    drag = null;
    const card = $("card");
    card.classList.remove("sliding", "peek", "closing");
    card.style.height = "";
    cardState = "full";
  }

  $("card-collapse").addEventListener("click", () => setCardState(cardState === "full" ? "peek" : "full"));
  $("peek-expand").addEventListener("click", () => setCardState("full"));
  $("peek-close").addEventListener("click", unfocus);
  $("card-peek").addEventListener("click", (e) => {
    // The play button swaps its icon while the click is under way, so check the button by the
    // path the click took rather than by where its target is now.
    if (e.composedPath().some((n) => n instanceof HTMLButtonElement) || Date.now() - lastDragEnd < 350) return;
    setCardState("full");
  });
  $("card-grab").addEventListener("click", () => {
    if (Date.now() - lastDragEnd >= 350) setCardState(cardState === "full" ? "peek" : "full");
  });

  // Dragging on phones: the card follows the finger; on release it settles on the nearest
  // state, or the next one in the direction of a flick.
  const DRAG_AREAS = "#card-grab, #card-peek, .card-photo, .card-head";
  $("card").addEventListener("pointerdown", (e) => {
    if (!phone() || (e.pointerType === "mouse" && e.button !== 0)) return;
    if (!e.target.closest(DRAG_AREAS) || e.target.closest("button, a, input, label")) return;
    if (e.pointerType === "mouse") e.preventDefault(); // no text selection while dragging
    drag = { id: e.pointerId, y0: e.clientY, h0: $("card").offsetHeight, moved: false, track: [[e.timeStamp, e.clientY]] };
  });
  $("card").addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const card = $("card"), dy = e.clientY - drag.y0;
    if (!drag.moved) {
      if (Math.abs(dy) < 8) return;
      drag.moved = true;
      drag.peekH = cardHeightIn("peek");
      drag.fullH = cardHeightIn("full");
      clearTimeout(slideTimer);
      card.classList.remove("sliding");
      card.setPointerCapture?.(e.pointerId);
    }
    let h = drag.h0 - dy;
    if (h > drag.fullH) h = drag.fullH + (h - drag.fullH) * 0.2; // a little give past the top
    h = Math.max(0, h);
    card.classList.toggle("peek", h <= drag.peekH + 16); // the banner near the bottom, the card above
    card.style.height = `${h}px`;
    drag.track.push([e.timeStamp, e.clientY]);
    if (drag.track.length > 5) drag.track.shift();
  });
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    if (!d.moved) return; // a tap: the click handlers take it
    lastDragEnd = Date.now();
    const card = $("card");
    const [ta, ya] = d.track[0], [tb, yb] = d.track[d.track.length - 1];
    const v = tb > ta ? (yb - ya) / (tb - ta) : 0; // px per ms, down is positive
    const h = card.offsetHeight;
    const levels = [["hidden", 0], ["peek", d.peekH], ["full", d.fullH]];
    let target;
    if (v > 0.45) target = levels.filter(([, lh]) => lh < h - 4).pop() || levels[0];
    else if (v < -0.45) target = levels.find(([, lh]) => lh > h + 4) || levels[2];
    else target = levels.reduce((a, b) => (Math.abs(b[1] - h) < Math.abs(a[1] - h) ? b : a));
    if (target[0] === "hidden") {
      card.classList.add("closing");
      slideCard(h, 0, unfocus);
      return;
    }
    cardState = target[0];
    card.classList.toggle("peek", cardState === "peek");
    $("card-collapse").setAttribute("aria-expanded", String(cardState === "full"));
    slideCard(h, target[1]);
  };
  $("card").addEventListener("pointerup", endDrag);
  $("card").addEventListener("pointercancel", endDrag);

  // --- track playback ------------------------------------------------------------------
  // One player at a time, for the focused ship's voyage or a passage from Stats.

  let player = null, playerOwner = null;

  function setPeekPlay(on) {
    const card = on && playerOwner === "card", replay = on && playerOwner === "replay";
    $("peek-play").innerHTML = card ? FerryPlayback.ICONS.pause : FerryPlayback.ICONS.play;
    $("peek-play").setAttribute("aria-label", card ? "Pause the playback" : "Play back the track");
    $("replay-play").innerHTML = replay ? FerryPlayback.ICONS.pause : FerryPlayback.ICONS.play;
    $("replay-play").setAttribute("aria-label", replay ? "Pause the playback" : "Play back the passage");
  }

  // Pixels of the map hidden under the panel at the bottom on phones.
  const coveredBy = (id) => () => (phone() ? Math.max(0, window.innerHeight - $(id).getBoundingClientRect().top) : 0);

  function startPlayback(owner, points, color, mount, cover) {
    stopPlayback();
    playerOwner = owner;
    player = FerryPlayback.create({
      map, points, color, mount, shipSvg, speedColor, cover,
      onState: setPeekPlay,
      onClose: stopPlayback,
    });
    if (!player) { playerOwner = null; return; }
    if (owner === "card") focused?.line?.remove();
    if (owner === "replay") replayLayer?.remove();
    player.play();
  }

  function stopPlayback() {
    if (!player) return;
    const owner = playerOwner;
    player.destroy();
    player = null;
    playerOwner = null;
    if (owner === "card" && focused?.line) focused.line.addTo(map);
    if (owner === "replay" && replayLayer) replayLayer.addTo(map);
    setPeekPlay(false);
  }

  function playCard() {
    if (player && playerOwner === "card") { player.toggle(); return; }
    if (!focused || focused.points.length < 2) return;
    setCardState("peek");
    startPlayback("card", focused.points, ships.get(focused.mmsi)?.info.color || "#2563eb", $("card-player"), coveredBy("card"));
  }

  $("card-play").addEventListener("click", playCard);
  $("peek-play").addEventListener("click", playCard);
  $("card-upload").addEventListener("change", async (ev) => {
    const file = ev.target.files[0];
    if (!file || !focused) return;
    const form = new FormData();
    form.append("photo", file);
    const res = await fetch(`api/ships/${focused.mmsi}/photo`, { method: "POST", body: form });
    if (!res.ok) alert(await res.text());
    ev.target.value = "";
    await loadState();
  });

  // --- bottom strip -------------------------------------------------------------

  // The chips are updated in place rather than rebuilt: rebuilt on every position
  // report, a click often landed on a chip that had already been replaced.
  const chips = new Map(); // mmsi -> { button, dot, name, sub }

  function renderStrip() {
    const strip = $("strip");
    const items = [...ships.values()].map((e) => e.info).filter((info) => !info.fleet);
    const wanted = new Set(items.map((info) => info.mmsi));
    for (const [mmsi, chip] of chips) {
      if (!wanted.has(mmsi)) { chip.button.remove(); chips.delete(mmsi); }
    }
    items.forEach((info, i) => {
      let chip = chips.get(info.mmsi);
      if (!chip) {
        const button = document.createElement("button");
        button.type = "button";
        const dot = document.createElement("span");
        dot.className = "dot";
        const text = document.createElement("span");
        const name = document.createElement("span");
        name.className = "name";
        const sub = document.createElement("span");
        sub.className = "sub";
        text.append(name, document.createElement("br"), sub);
        button.append(dot, text);
        button.addEventListener("click", () => focusShip(info.mmsi));
        chip = { button, dot, name, sub };
        chips.set(info.mmsi, chip);
      }
      const cls = "chip panel" + (isStale(info) ? " stale" : "");
      if (chip.button.className !== cls) chip.button.className = cls;
      if (chip.color !== info.color) { chip.color = info.color; chip.dot.style.background = info.color; }
      setText(chip.name, info.name);
      setText(chip.sub, chipText(info));
      if (strip.children[i] !== chip.button) strip.insertBefore(chip.button, strip.children[i] || null);
    });
  }

  // "Porkkala in 23 min · 17.5 kn" when a passage is coming up, otherwise speed and heading.
  function chipText(info) {
    if (!info.position) return "No position yet";
    if (isStale(info)) return `Last seen ${ago(info.position.ts)}`;
    const n = nextPassage(info);
    if (!n) return motionText(info);
    const speed = info.position.sog != null ? ` · ${info.position.sog.toFixed(1)} kn` : "";
    return `Porkkala ${n.left < 60 ? "now" : `in ${duration(n.left)}`}${speed}`;
  }

  // --- status -------------------------------------------------------------------

  let lastState = null;
  let socketOpen = false;

  let statusText = "Connecting";
  function renderStatus() {
    const el = $("status");
    let state = "connecting";
    let text = "Connecting";
    if (lastState?.simulated) {
      state = "simulated"; text = "Simulated data";
    } else if (lastState && socketOpen) {
      const feed = lastState.feed || {};
      if (feed.connected) { state = "live"; text = "Live"; }
      else { state = "offline"; text = "Feed offline"; }
    } else if (!socketOpen && lastState) {
      state = "offline"; text = "Reconnecting";
    }
    el.dataset.state = state;
    setText(el.lastElementChild, text);
    const pill = $("pill-status");
    pill.dataset.state = state;
    pill.title = text;
    statusText = text;
  }

  // --- data loading ---------------------------------------------------------------

  async function loadState() {
    const res = await fetch("api/state");
    const state = await res.json();
    lastState = state;
    clockSkew = state.now - Date.now() / 1000;
    const seen = new Set();
    for (const info of [...state.ships, ...(state.fleet_ships || [])]) {
      seen.add(info.mmsi);
      upsertShip(info);
    }
    for (const mmsi of [...ships.keys()]) if (!seen.has(mmsi)) removeShip(mmsi);
    updateChart(state.chart);
    renderAircraft(state.aircraft?.list);
    renderStrip();
    renderCard();
    renderStatus();
    renderShipsList();
    renderZones(state.zones || []);
    renderNotifyStatus();
    renderChartKeep();
    FerryChart.setRev(state.vchart?.rev, state.vchart?.min_zoom);
    if (!$("ships-dialog").querySelector('[data-pane="layers"]').hidden) renderLayers();
    renderConditions();
    renderPhone();
    renderSettings();
    const chartSource = state.chart?.layers?.length
      ? `Traficom (${state.chart.layers.map((l) => l.title).join(", ")})` : "OpenSeaMap sea marks";
    const vc = state.vchart;
    const drawn = vc ? ` · Drawn chart: ${vc.blocks} areas downloaded (${vc.mb} MB)${
      Object.keys(vc.errors || {}).length ? `, problems with ${Object.entries(vc.errors).map(([k, e]) => `${k} (${String(e).replace(/https?:\/\/([^/\s]+)\S*/g, "$1")})`).join(", ")}` : ""}` : "";
    $("version").textContent = `Ferry Watch ${state.version} · ${state.positions_stored.toLocaleString()} positions stored · Scanned chart: ${chartSource}${drawn}`;
  }

  async function loadTails() {
    const tails = await (await fetch(`api/tails?minutes=${TAIL_MINUTES}`)).json();
    for (const [mmsi, points] of Object.entries(tails)) {
      const entry = ships.get(Number(mmsi));
      if (entry) setTail(entry, points);
    }
  }

  // While the panel is hidden (phone locked, another app or tab) the live connection is
  // closed after a minute, so the NAS stops its panel-only work (weather, the phone's
  // position, aircraft); it's opened again, catching up, when the panel is seen again.
  const HIDDEN_CLOSE_MS = 60000;
  let socket = null, socketPaused = false, hiddenTimer = null;
  document.addEventListener("visibilitychange", () => {
    clearTimeout(hiddenTimer);
    if (document.hidden) {
      hiddenTimer = setTimeout(() => { socketPaused = true; socket?.close(); }, HIDDEN_CLOSE_MS);
      return;
    }
    if (socketPaused) {
      socketPaused = false;
      if (!socket || socket.readyState === WebSocket.CLOSED) connectSocket();
    }
    refreshState();
  });

  function connectSocket() {
    if (socketPaused) return;
    const url = new URL("api/ws", location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.hash = "";
    const ws = socket = new WebSocket(url);
    ws.onopen = () => {
      socketOpen = true;
      renderStatus();
      // Catch up on anything missed while disconnected.
      loadState().then(loadTails).catch(console.error);
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "position" || msg.type === "meta") onPosition(msg.ship);
      else if (msg.type === "reload") loadState().then(loadTails).catch(console.error);
      else if (msg.type === "aircraft") renderAircraft(msg.aircraft);
      else if (msg.type === "weather") {
        if (lastState) lastState.weather = msg.weather;
        renderConditions();
      } else if (msg.type === "phone") {
        if (lastState) lastState.phone = msg.phone;
        renderPhone();
        renderCard();
      }
    };
    ws.onclose = () => {
      socketOpen = false;
      if (socket === ws) socket = null;
      renderStatus();
      if (!socketPaused) setTimeout(connectSocket, 3000);
    };
  }

  // Refresh "x min ago" texts and feed status (not while hidden: nobody sees them).
  setInterval(() => { if (!document.hidden) refreshState(); }, 30000);
  function refreshState() {
    renderStrip();
    renderCard();
    fetch("api/state").then((r) => r.json()).then((s) => {
      lastState = s;
      renderStatus();
      renderConditions();
      renderPhone();
      renderSettings();
    }).catch(() => {});
  }

  // --- ships dialog -------------------------------------------------------------

  function renderShipsList() {
    const list = $("ships-list");
    list.replaceChildren(...[...ships.values()].filter(({ info }) => !info.fleet).map(({ info }) => {
      const li = document.createElement("li");
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = info.color;
      const grow = document.createElement("div");
      grow.className = "grow";
      const name = document.createElement("span");
      name.textContent = info.name;
      const sub = document.createElement("span");
      sub.className = "muted small";
      sub.textContent = `MMSI ${info.mmsi} · ${info.position ? `last report ${ago(info.position.ts)}` : "no data yet"}`;
      grow.append(name, sub);
      // Inner-route alerts per ship: on for the four ferries, off for ships added later.
      const alerts = document.createElement("label");
      alerts.className = "alert-switch small";
      alerts.title = "Send a phone alert when this ship takes the inner route";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.className = "switch";
      toggle.checked = info.alerts !== false;
      toggle.addEventListener("change", async () => {
        toggle.disabled = true;
        try {
          await api(`api/ships/${info.mmsi}/alerts`, { method: "POST", body: { enabled: toggle.checked } });
          info.alerts = toggle.checked;
        } finally {
          toggle.disabled = false;
        }
      });
      alerts.append("Alerts", toggle);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        if (!confirm(`Stop tracking ${info.name}?`)) return;
        await fetch(`api/ships/${info.mmsi}`, { method: "DELETE" });
        await loadState();
      });
      li.append(dot, grow, alerts, remove);
      return li;
    }));
  }

  let searchTimer = null;
  $("ship-search").addEventListener("input", (ev) => {
    clearTimeout(searchTimer);
    const q = ev.target.value.trim();
    searchTimer = setTimeout(() => search(q), 350);
  });

  async function search(q) {
    const list = $("search-results");
    if (q.length < 3) { list.replaceChildren(); return; }
    const results = await (await fetch(`api/search?q=${encodeURIComponent(q)}`)).json();
    const rows = results.filter((r) => !ships.has(r.mmsi)).map((r) => {
      const li = document.createElement("li");
      const grow = document.createElement("div");
      grow.className = "grow";
      const name = document.createElement("span");
      name.textContent = r.name || `MMSI ${r.mmsi}`;
      const sub = document.createElement("span");
      sub.className = "muted small";
      sub.textContent = `MMSI ${r.mmsi}${r.destination ? ` · ${destinationText(r.destination)}` : ""}`;
      grow.append(name, sub);
      const choose = async (url, button) => {
        li.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        button.textContent = "Adding…";
        await api(url, { method: "POST", body: { mmsi: r.mmsi, name: r.name } });
        $("ship-search").value = "";
        list.replaceChildren();
        await loadState();
        await loadTails();
      };
      const follow = document.createElement("button");
      follow.type = "button";
      follow.textContent = "Follow";
      follow.title = "Show on the map with its latest trip";
      follow.addEventListener("click", () => choose("api/follow", follow));
      const add = document.createElement("button");
      add.type = "button";
      add.className = "primary";
      add.textContent = "Add";
      add.title = "Alerts and stats, like the ferries";
      add.addEventListener("click", () => choose("api/ships", add));
      li.append(grow, follow, add);
      return li;
    });
    if (/^\d{9}$/.test(q) && !rows.length && !ships.has(Number(q))) {
      const li = document.createElement("li");
      li.innerHTML = `<div class="grow"><span>MMSI ${q}</span><span class="muted small">Not in the vessel list, add anyway</span></div>`;
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = "Add";
      add.addEventListener("click", async () => {
        await fetch("api/ships", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mmsi: Number(q) }) });
        await loadState();
      });
      li.append(add);
      rows.push(li);
    }
    list.replaceChildren(...rows);
  }

  $("ships-button").addEventListener("click", () => {
    renderShipsList();
    $("notify-result").textContent = "";
    const saved = store.get("fw-tab") === "fleets" ? "layers" : store.get("fw-tab");
    setTab(["stats", "layers", "settings"].includes(saved) ? saved : "ships");
    $("ships-dialog").showModal();
    $("ships-dialog").focus(); // not the first tab, which would show a focus ring
  });


  // --- areas (zones) ------------------------------------------------------------

  const ZONE_STYLE = {
    inner: { color: "#f97316", label: "Inner route" },
    outer: { color: "#8b5cf6", label: "Outer route" },
    phone: { color: "#16a34a", label: "My area" },
  };
  const zoneLayers = new Map(); // id -> { zone, layer }
  let editing = false;
  let drawing = null; // { role, shape }
  let zonesKey = "";

  map.pm.setGlobalOptions({ snappable: false, allowSelfIntersection: false, continueDrawing: false });
  map.pm.setLang("en");

  function zoneStyle(zone) {
    const color = ZONE_STYLE[zone.role]?.color || "#64748b";
    return editing
      ? { color, weight: zone.shape === "line" ? 5 : 3, opacity: 0.95, fillOpacity: 0.15, dashArray: null }
      : { color, weight: zone.shape === "line" ? 3 : 1.5, opacity: 0.55, fillOpacity: 0, dashArray: "6 6" }; // no fill: it would tint the chart
  }

  function zoneLatLngs(layer, shape) {
    const ll = layer.getLatLngs();
    const ring = shape === "polygon" ? ll[0] : ll;
    return ring.map((p) => [Number(p.lat.toFixed(6)), Number(p.lng.toFixed(6))]);
  }

  function renderZones(zones) {
    const key = JSON.stringify(zones);
    if (key === zonesKey) return;
    zonesKey = key;
    for (const { layer } of zoneLayers.values()) layer.remove();
    zoneLayers.clear();
    for (const zone of zones) {
      const make = zone.shape === "line" ? L.polyline : L.polygon;
      const layer = make(zone.points, { ...zoneStyle(zone), interactive: editing, pmIgnore: false }).addTo(map);
      layer.bindTooltip(zone.name, { sticky: true });
      layer.on("pm:edit", () => saveZoneShape(zone.id));
      zoneLayers.set(zone.id, { zone, layer });
      if (editing) layer.pm.enable({ allowSelfIntersection: false, snappable: false });
      layer.bringToBack();
    }
    renderZonesList();
  }

  const saveTimers = new Map();
  function saveZoneShape(id) {
    clearTimeout(saveTimers.get(id));
    saveTimers.set(id, setTimeout(async () => {
      const entry = zoneLayers.get(id);
      if (!entry) return;
      const points = zoneLatLngs(entry.layer, entry.zone.shape);
      entry.zone.points = points;
      zonesKey = ""; // the server's echo will redraw it
      await api(`api/zones/${id}`, { method: "PUT", body: { points } });
    }, 500));
  }

  async function api(url, { method = "GET", body } = {}) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }

  function renderZonesList() {
    const list = $("zones-list");
    const zones = [...zoneLayers.values()].map((e) => e.zone);
    if (!zones.length) {
      const li = document.createElement("li");
      li.className = "muted small";
      li.textContent = "No areas yet. Add an inner route line and your area below.";
      list.replaceChildren(li);
      return;
    }
    list.replaceChildren(...zones.map((zone) => {
      const li = document.createElement("li");
      const sw = document.createElement("span");
      sw.className = `swatch ${zone.shape}`;
      sw.style.background = ZONE_STYLE[zone.role]?.color;
      const grow = document.createElement("div");
      grow.className = "grow";
      const name = document.createElement("span");
      name.textContent = zone.name;
      const sub = document.createElement("span");
      sub.className = "muted small";
      sub.textContent = `${ZONE_STYLE[zone.role]?.label || zone.role} · ${zone.shape === "line" ? "line" : "area"}`;
      grow.append(name, sub);
      grow.style.cursor = "pointer";
      grow.addEventListener("click", () => {
        const layer = zoneLayers.get(zone.id)?.layer;
        if (layer) map.fitBounds(layer.getBounds(), { ...cardPadding(), maxZoom: 13 });
      });
      const actions = document.createElement("div");
      actions.className = "zone-actions";
      const rename = document.createElement("button");
      rename.type = "button";
      rename.textContent = "Rename";
      rename.addEventListener("click", async () => {
        const value = prompt("Name", zone.name);
        if (!value || value === zone.name) return;
        await api(`api/zones/${zone.id}`, { method: "PUT", body: { name: value } });
        await loadState();
      });
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete "${zone.name}"?`)) return;
        await api(`api/zones/${zone.id}`, { method: "DELETE" });
        await loadState();
      });
      actions.append(rename, del);
      li.append(sw, grow, actions);
      return li;
    }));
  }

  async function setEditing(on) {
    editing = on;
    if (!on) cancelDrawing();
    if (on && route.on) setRouteMode(false);
    if (on && focused) unfocus();
    if (on) clearReplay();
    $("areas").hidden = !on;
    $("strip").hidden = on || Boolean(focused);
    $("areas-button").setAttribute("aria-pressed", String(on));
    $("areas-button").hidden = !on;
    zonesKey = "";
    renderZones(lastState?.zones || []);
    // Longer tracks while editing, so lines can be placed on real routes.
    const minutes = on ? 24 * 60 : TAIL_MINUTES;
    try {
      const tails = await (await fetch(`api/tails?minutes=${minutes}`)).json();
      for (const [mmsi, points] of Object.entries(tails)) {
        const entry = ships.get(Number(mmsi));
        if (entry) setTail(entry, points);
      }
    } catch (err) { console.error(err); }
  }

  const DRAW_HINTS = {
    line: "Tap the map to place the line's points across the inner fairway at one end. Tap Finish when done.",
    polygon: "Tap the map to place the corners of the area. Tap Finish (or the first point) when done.",
  };

  function startDrawing(role, shape) {
    cancelDrawing();
    drawing = { role, shape };
    const color = ZONE_STYLE[role].color;
    map.pm.enableDraw(shape === "line" ? "Line" : "Polygon", {
      templineStyle: { color }, hintlineStyle: { color, dashArray: "5 5" }, pathOptions: { color },
      finishOn: null, tooltips: false,
    });
    $("areas").classList.add("is-drawing");
    $("drawing").hidden = false;
    $("drawing-text").textContent = `${ZONE_STYLE[role].label}: ${DRAW_HINTS[shape]}`;
  }

  function cancelDrawing() {
    drawing = null;
    map.pm.disableDraw();
    $("areas").classList.remove("is-drawing");
    $("drawing").hidden = true;
  }

  map.on("pm:create", async (ev) => {
    const current = drawing;
    ev.layer.remove();
    cancelDrawing();
    if (!current) return;
    const points = zoneLatLngs(ev.layer, current.shape);
    try {
      await api("api/zones", {
        method: "POST",
        body: { role: current.role, shape: current.shape, points, name: ZONE_STYLE[current.role].label },
      });
    } catch (err) {
      alert(err.message);
    }
    await loadState();
  });

  $("draw-finish").addEventListener("click", () => {
    const shape = drawing?.shape === "line" ? "Line" : "Polygon";
    const handler = map.pm.Draw[shape];
    const needed = shape === "Line" ? 2 : 3;
    if (handler?._layer && handler._layer.getLatLngs().length >= needed) handler._finishShape();
    else alert(`Place at least ${needed} points first.`);
  });
  $("draw-cancel").addEventListener("click", cancelDrawing);
  document.querySelectorAll("[data-add]").forEach((b) => {
    b.addEventListener("click", () => {
      const [role, shape] = b.dataset.add.split(":");
      startDrawing(role, shape);
    });
  });
  $("areas-button").addEventListener("click", () => setEditing(!editing));
  // The areas are set up once, so they're edited from Settings rather than the top bar;
  // the bar's button shows only while editing, to finish.
  $("areas-open").addEventListener("click", () => {
    $("ships-dialog").close();
    setEditing(true);
  });
  $("areas-done").addEventListener("click", () => setEditing(false));

  // --- notifications and passages ---------------------------------------------

  const DIRECTIONS = { east: "towards Helsinki", west: "towards Stockholm" };

  function renderNotifyStatus() {
    $("notify-status").textContent = lastState?.ha_connected
      ? "Alerts go to your phone when a ship takes the inner route while you're in your area."
      : "Not connected to Home Assistant, so no alerts can be sent.";
    $("notify-test").disabled = !lastState?.ha_connected;
  }

  function renderChartKeep() {
    const vc = lastState?.vchart;
    const keep = vc?.keep;
    $("chart-keep").checked = Boolean(keep);
    $("chart-foreign").checked = vc?.foreign !== false;
    $("chart-sample").checked = sampleOnPress;
    let text = "Downloads the drawn chart of the whole Gulf of Finland once, so it opens quickly everywhere. Off: only the parts you look at are downloaded.";
    if (keep && keep.running) {
      const last = keep.last_ok ? ` Last area finished at ${new Date(keep.last_ok * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.` : "";
      const failed = keep.failed ? ` ${keep.failed} areas failed and will be tried again in an hour.` : "";
      text = `Downloading the Gulf of Finland: ${keep.done} of ${keep.total} areas (${vc.mb} MB so far). The chart works meanwhile.${last}${failed}`;
    } else if (keep) {
      text = `The whole Gulf of Finland is on the NAS (${vc.mb} MB). It's refreshed monthly.`;
    }
    $("chart-keep-status").textContent = text;
    // With a key saved the form folds away behind "Change".
    const saved = Boolean(vc?.mml_key);
    $("mml-saved").hidden = !saved;
    $("mml-change").hidden = !saved || mmlEditing;
    $("mml-form").hidden = saved && !mmlEditing;
    $("mml-key").placeholder = saved ? "Paste a new key to replace the saved one" : "Paste your API key";
  }

  let mmlEditing = false;
  $("mml-change").addEventListener("click", () => {
    mmlEditing = true;
    renderChartKeep();
    $("mml-key").focus();
  });

  $("mml-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = $("mml-key").value.trim();
    if (!key) return;
    $("mml-result").textContent = "Checking the key…";
    try {
      const res = await api("api/vchart/key", { method: "POST", body: { key } });
      if (res.ok) {
        $("mml-key").value = "";
        mmlEditing = false;
        $("mml-result").textContent = "Key works. Rocks, wrecks and names are added as each area is downloaded.";
        await loadState();
      } else {
        $("mml-result").textContent = `The Land Survey didn't accept the key: ${res.error}`;
      }
    } catch (err) {
      $("mml-result").textContent = String(err);
    }
  });

  $("chart-keep").addEventListener("change", async (e) => {
    e.target.disabled = true;
    try {
      await api("api/vchart/keep", { method: "POST", body: { enabled: e.target.checked } });
      await loadState();
    } finally {
      e.target.disabled = false;
    }
  });

  $("chart-foreign").addEventListener("change", async (e) => {
    e.target.disabled = true;
    try {
      await api("api/vchart/foreign", { method: "POST", body: { enabled: e.target.checked } });
      await loadState(); // a new chart rev redraws the tiles with or without it
    } finally {
      e.target.disabled = false;
    }
  });

  $("chart-sample").addEventListener("change", (e) => {
    sampleOnPress = e.target.checked;
    store.set("fw-chart-sample", sampleOnPress ? "1" : "0");
  });

  // While the chart downloads, keep its progress current while the menu is open.
  setInterval(() => {
    if (!document.hidden && $("ships-dialog").open && lastState?.vchart?.keep?.running) loadState().catch(() => {});
  }, 5000);

  $("notify-test").addEventListener("click", async () => {
    $("notify-result").textContent = "Sending…";
    try {
      const res = await fetch("api/notify/test", { method: "POST" });
      $("notify-result").textContent = res.ok ? (await res.json()).message : await res.text();
    } catch (err) {
      $("notify-result").textContent = String(err);
    }
  });

  async function loadPassages() {
    const rows = await (await fetch("api/passages?limit=15")).json();
    const list = $("passages-list");
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "muted small";
      li.textContent = "None yet.";
      list.replaceChildren(li);
      return;
    }
    list.replaceChildren(...rows.map((p) => {
      const li = document.createElement("li");
      const grow = document.createElement("div");
      grow.className = "grow";
      const top = document.createElement("span");
      top.textContent = `${p.name}: ${p.route} route, ${DIRECTIONS[p.direction] || p.direction}`;
      const sub = document.createElement("span");
      sub.className = "muted small";
      const when = new Date(p.ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short", hour12: false });
      sub.textContent = p.notified ? when : `${when} · ${p.note || "not sent"}`;
      grow.append(top, sub);
      const wx = FerryWeather.row(p);
      if (wx) grow.append(wx);
      li.append(grow);
      li.classList.add("clickable");
      li.title = "Show this passage on the map";
      li.addEventListener("click", () => {
        $("ships-dialog").close();
        showPassage(p).catch(console.error);
      });
      if (p.notified) {
        const badge = document.createElement("span");
        badge.className = "badge sent";
        badge.textContent = "Alert sent";
        li.append(badge);
      }
      return li;
    }));
  }

  function focusFromNotification() {
    // Opening the panel from an alert: show that ship (once per alert).
    const n = lastState?.last_notification;
    if (!n || now() - n.ts > 15 * 60) return false;
    const key = `${n.mmsi}:${Math.round(n.ts)}`;
    if (store.get("fw-alert-seen") === key || !ships.has(n.mmsi)) return false;
    store.set("fw-alert-seen", key);
    focusShip(n.mmsi);
    return true;
  }

  // --- passage replay -------------------------------------------------------------
  // The track an hour either side of a recorded passage, with a dot every 10 minutes.

  const REPLAY_WINDOW = 3600;
  let replayLayer = null;

  let replayPoints = null, replayColor = null;

  function clearReplay() {
    if (playerOwner === "replay") stopPlayback();
    if (!replayLayer) return;
    replayLayer.remove();
    replayLayer = null;
    $("replay").hidden = true;
    $("strip").hidden = Boolean(focused) || editing;
  }

  async function showPassage(p) {
    if (editing) setEditing(false);
    if (route.on) setRouteMode(false);
    if (focused) unfocus();
    clearReplay();
    const data = await api(`api/track/${p.mmsi}?since=${p.ts - REPLAY_WINDOW}&until=${p.ts + REPLAY_WINDOW}`);
    const color = p.color || ships.get(p.mmsi)?.info.color || "#64748b";
    const pts = data.points.map((q) => [q[0], q[1]]);
    replayLayer = L.layerGroup().addTo(map);
    if (pts.length > 1) {
      L.polyline(pts, { color: "#fff", weight: 8, opacity: 0.8, interactive: false }).addTo(replayLayer);
      L.polyline(pts, { color, weight: 4, opacity: 1, interactive: false }).addTo(replayLayer);
      let next = 0;
      for (const [lat, lon, ts] of data.points) {
        if (ts < next) continue;
        next = ts - (ts % 600) + 600;
        L.circleMarker([lat, lon], { radius: 4, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: 1 })
          .bindTooltip(clock(ts), { direction: "top" })
          .addTo(replayLayer);
      }
      const first = data.points[0], last = data.points[data.points.length - 1];
      L.tooltip({ permanent: true, direction: "left", className: "replay-time" })
        .setLatLng([first[0], first[1]]).setContent(clock(first[2])).addTo(replayLayer);
      L.tooltip({ permanent: true, direction: "right", className: "replay-time" })
        .setLatLng([last[0], last[1]]).setContent(clock(last[2])).addTo(replayLayer);
    }
    if (p.lat != null && p.lon != null) {
      L.circleMarker([p.lat, p.lon], { radius: 8, color, weight: 3, fillColor: "#fff", fillOpacity: 1 })
        .bindTooltip(`Counted here at ${clock(p.ts)}`, { direction: "top" })
        .addTo(replayLayer);
      if (pts.length < 2) pts.push([p.lat, p.lon]);
    }

    $("replay-dot").style.background = color;
    $("replay-title").textContent = `${p.name}: ${p.route} route, ${DIRECTIONS[p.direction] || p.direction}`;
    const when = new Date(p.ts * 1000).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    $("replay-when").textContent = p.sog ? `${when} · ${p.sog.toFixed(0)} kn` : when;
    const keepDays = lastState?.retention_days ?? 60;
    $("replay-note").textContent = data.points.length > 1
      ? "Dots every 10 minutes. Tap one for the time."
      : now() - p.ts > keepDays * 86400
        ? `The track isn't stored any more (positions are kept for ${keepDays} days).`
        : "No track was stored for this passage.";
    replayPoints = data.points;
    replayColor = color;
    $("replay-play").hidden = data.points.length < 2;
    $("replay").hidden = false;
    $("strip").hidden = true;

    const panel = $("replay").getBoundingClientRect();
    if (pts.length > 1) {
      map.fitBounds(L.latLngBounds(pts), { paddingTopLeft: [40, 70], paddingBottomRight: [40, window.innerHeight - panel.top + 20], maxZoom: 13 });
    } else if (pts.length) {
      map.setView(pts[0], 11);
    }
  }

  $("replay-close").addEventListener("click", clearReplay);
  $("replay-play").addEventListener("click", () => {
    if (player && playerOwner === "replay") { player.toggle(); return; }
    if (replayPoints?.length > 1) startPlayback("replay", replayPoints, replayColor, $("replay-player"), coveredBy("replay"));
  });

  // --- stats --------------------------------------------------------------------
  // Counted here rather than on the server so months and clock times follow this
  // device's time zone.

  const MONTHS = Array.from({ length: 12 }, (_, m) => new Date(2000, m, 1).toLocaleString("en", { month: "short" }));
  const MONTHS_LONG = Array.from({ length: 12 }, (_, m) => new Date(2000, m, 1).toLocaleString("en", { month: "long" }));
  let statsData = null;
  let statsYear = new Date().getFullYear();
  let statsView = "month"; // year, month, week, weekday or hour
  let statsPick = null; // the bar tapped in the current view, or null for all
  let statsDay = null; // a day tapped in the month calendar
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

  function isoWeek(d) {
    const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
    return Math.ceil(((t - Date.UTC(t.getUTCFullYear(), 0, 1)) / 86400000 + 1) / 7);
  }

  // Bars for each view: the keys along the axis, a short label, and a long name.
  function statsAxis(view, years) {
    const range = (n) => Array.from({ length: n }, (_, i) => i);
    if (view === "year") return years.map((y) => ({ key: y, label: String(y), name: String(y) }));
    if (view === "week") return range(53).map((w) => ({ key: w + 1, label: (w + 1) % 4 === 1 ? String(w + 1) : "", name: `week ${w + 1}` }));
    if (view === "weekday") return range(7).map((d) => ({ key: d, label: WEEKDAYS[d], name: `${WEEKDAYS_LONG[d]}s` }));
    if (view === "hour") return range(24).map((h) => ({ key: h, label: h % 3 === 0 ? String(h).padStart(2, "0") : "", name: `${hhmm(h * 60)}–${hhmm((h + 1) * 60 % 1440)}` }));
    return range(12).map((m) => ({ key: m, label: MONTHS[m], name: MONTHS_LONG[m] }));
  }

  const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const hhmm = (minutes) => `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;

  function median(values) {
    const v = [...values].sort((a, b) => a - b);
    const mid = v.length >> 1;
    return v.length % 2 ? v[mid] : Math.round((v[mid - 1] + v[mid]) / 2);
  }

  function setTab(name) {
    document.querySelectorAll("[data-tab]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.tab === name));
      b.setAttribute("aria-selected", String(b.dataset.tab === name));
    });
    document.querySelectorAll("[data-pane]").forEach((pane) => { pane.hidden = pane.dataset.pane !== name; });
    store.set("fw-tab", name);
    if (name === "stats") loadStats().catch(console.error);
    if (name === "layers") renderLayers();
    if (name === "settings") renderSettings();
  }
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  // Tap a folded explanation to read it whole (a link in it still opens).
  $("ships-dialog").addEventListener("click", (e) => {
    const note = e.target.closest?.("p.note");
    if (note && !e.target.closest("a")) note.classList.toggle("open");
  });

  async function loadStats() {
    const [data] = await Promise.all([api("api/stats"), loadPassages()]);
    statsData = data;
    statsLoadedAt = Date.now();
    const years = new Set(data.passages.map(([ts]) => new Date(ts * 1000).getFullYear()));
    years.add(new Date().getFullYear());
    const sorted = [...years].sort((a, b) => b - a);
    if (!years.has(statsYear)) { statsYear = sorted[0]; statsPick = null; }
    const select = $("stats-year");
    select.replaceChildren(...sorted.map((y) => new Option(String(y), String(y), false, y === statsYear)));
    select.hidden = sorted.length < 2;
    renderStats();
  }

  $("stats-year").addEventListener("change", (ev) => {
    statsYear = Number(ev.target.value);
    statsPick = null;
    renderStats();
  });

  document.querySelectorAll("[data-stats-view]").forEach((b) => b.addEventListener("click", () => {
    statsView = b.dataset.statsView;
    statsPick = null;
    store.set("fw-stats-view", statsView);
    renderStats();
  }));
  statsView = store.get("fw-stats-view") || "month";

  function renderStats() {
    const all = statsData.passages.map(([ts, mmsi, route, direction, wind_ms, wind_dir, ice]) => {
      const d = new Date(ts * 1000);
      return {
        mmsi, route, direction, ts, wind_ms, wind_dir, ice,
        year: d.getFullYear(), month: d.getMonth(), week: isoWeek(d), weekday: (d.getDay() + 6) % 7,
        hour: d.getHours(), minute: d.getHours() * 60 + d.getMinutes(),
      };
    });
    const years = [...new Set([...all.map((p) => p.year), new Date().getFullYear()])].sort((a, b) => a - b);
    // The Years view covers everything; the others cover the chosen year.
    const rows = statsView === "year" ? all : all.filter((p) => p.year === statsYear);
    const inner = rows.filter((p) => p.route === "inner");
    const axis = statsAxis(statsView, years);
    const today = new Date();

    document.querySelectorAll("[data-stats-view]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.statsView === statsView));
    });
    $("stats-year").hidden = statsView === "year" || years.length < 2;

    if (!statsData.passages.length) {
      $("stats-summary").textContent = "None yet. Passages are counted from when an inner-route line is in place.";
    } else if (statsView === "year") {
      $("stats-summary").textContent = `${inner.length} since ${new Date(all[0].ts * 1000).toLocaleDateString([], { dateStyle: "medium" })}`;
    } else {
      const parts = [`${inner.length} in ${statsYear}`];
      if (statsYear === today.getFullYear()) {
        parts.push(`${inner.filter((p) => p.month === today.getMonth()).length} in ${MONTHS_LONG[today.getMonth()]}`);
        parts.push(`${inner.filter((p) => p.week === isoWeek(today)).length} this week`);
      }
      $("stats-summary").textContent = parts.join(" · ");
    }

    const counts = new Map(axis.map((a) => [a.key, new Map()]));
    for (const p of inner) {
      const bucket = counts.get(p[statsView]);
      if (bucket) bucket.set(p.mmsi, (bucket.get(p.mmsi) || 0) + 1);
    }
    const totals = axis.map((a) => [...counts.get(a.key).values()].reduce((x, y) => x + y, 0));
    renderStatsChart(axis, counts, totals);

    const best = Math.max(0, ...totals);
    const busiest = axis.filter((a, i) => totals[i] === best && best > 0).map((a) => a.name);
    const busyText = busiest.length && busiest.length < axis.length / 2 ? `Busiest: ${busiest.join(", ")} (${best}). ` : "";
    const picked = axis.find((a) => a.key === statsPick);
    const inMonth = statsView === "month" && picked;
    if (!inMonth) statsDay = null;
    renderCalendar(inMonth ? inner.filter((p) => p.month === statsPick) : null);
    let scope = picked ? rows.filter((p) => p[statsView] === statsPick) : rows;
    if (statsDay) {
      scope = scope.filter((p) => new Date(p.ts * 1000).getDate() === statsDay);
      $("stats-scope").textContent = `${statsDay} ${picked.name} ${statsYear}. Tap the day again for the whole month.`;
    } else {
      $("stats-scope").textContent = picked
        ? `${picked.name}${statsView === "year" ? "" : `, ${statsYear}`}. ${inMonth ? "Tap a day to see it on its own, or the month again for all." : "Tap it again for all."}`
        : (inner.length ? `${busyText}Tap a bar to see it on its own.` : "");
    }
    renderStatsShips(scope);
    if (statsDay) {
      const day = new Date(statsYear, statsPick, statsDay);
      const next = new Date(statsYear, statsPick, statsDay + 1);
      FerryWeather.dayList($("stats-day"), day / 1000, next / 1000, {
        api, esc, clock, DIRECTIONS,
        open(p) {
          $("ships-dialog").close();
          showPassage(p).catch(console.error);
        },
      });
    } else {
      FerryWeather.hideDay($("stats-day"));
    }
    const scopeName = statsDay ? `${statsDay} ${picked.name}` : picked ? `${picked.name}${statsView === "year" ? "" : ` ${statsYear}`}`
      : statsView === "year" ? "all years" : String(statsYear);
    FerryWeather.breakdown($("stats-weather"), scope, scopeName);
  }

  // A month as a calendar: each day shows a dot per inner passage in the ship's colour.
  function renderCalendar(inner) {
    const box = $("stats-calendar");
    box.hidden = !inner;
    if (!inner) { box.replaceChildren(); return; }
    const first = new Date(statsYear, statsPick, 1);
    const days = new Date(statsYear, statsPick + 1, 0).getDate();
    const byDay = new Map();
    for (const p of inner) {
      const d = new Date(p.ts * 1000).getDate();
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d).push(p);
    }
    const colors = new Map(statsData.ships.map((s) => [s.mmsi, s.color]));
    const today = new Date();
    const cells = WEEKDAYS.map((w) => {
      const head = document.createElement("span");
      head.className = "cal-head";
      head.textContent = w[0];
      return head;
    });
    for (let i = 0; i < (first.getDay() + 6) % 7; i++) cells.push(document.createElement("span"));
    for (let d = 1; d <= days; d++) {
      const list = byDay.get(d) || [];
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cal-day";
      if (list.length) cell.classList.add("has");
      if (statsDay === d) cell.classList.add("selected");
      if (today.getFullYear() === statsYear && today.getMonth() === statsPick && today.getDate() === d) cell.classList.add("today");
      cell.setAttribute("aria-label", `${d} ${MONTHS_LONG[statsPick]}: ${list.length} inner passages`);
      const num = document.createElement("span");
      num.textContent = String(d);
      const dots = document.createElement("span");
      dots.className = "cal-dots";
      for (const p of list.sort((a, b) => a.ts - b.ts).slice(0, 6)) {
        const dot = document.createElement("i");
        dot.style.background = colors.get(p.mmsi) || "#64748b";
        dot.title = `${hhmm(p.minute)} ${DIRECTIONS[p.direction] || ""}`;
        dots.append(dot);
      }
      cell.append(num, dots);
      cell.addEventListener("click", () => {
        statsDay = statsDay === d ? null : d;
        renderStats();
      });
      cells.push(cell);
    }
    box.replaceChildren(...cells);
  }

  function renderStatsChart(axis, counts, totals) {
    const W = 360, H = 140, TOP = 16, BOTTOM = 20;
    const col = W / axis.length;
    const gap = Math.min(6, col * 0.25);
    const unit = (H - TOP - BOTTOM) / Math.max(1, ...totals);
    const showCounts = axis.length <= 12;
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Inner route passages">`;
    axis.forEach((a, i) => {
      const x = i * col + gap / 2, w = col - gap, cx = x + w / 2;
      const cls = ["bar", statsPick === a.key ? "selected" : "", statsPick !== null && statsPick !== a.key ? "dim" : ""].join(" ");
      let y = H - BOTTOM;
      svg += `<g class="${cls}" data-key="${a.key}"><rect class="hit" x="${i * col}" y="0" width="${col}" height="${H}"><title>${esc(a.name)}: ${totals[i]}</title></rect>`;
      for (const ship of statsData.ships) {
        const n = counts.get(a.key).get(ship.mmsi);
        if (!n) continue;
        y -= n * unit;
        svg += `<rect x="${x}" y="${y}" width="${w}" height="${n * unit}" fill="${esc(ship.color)}"><title>${esc(ship.name)}: ${n}</title></rect>`;
      }
      if (showCounts && totals[i]) svg += `<text class="count" x="${cx}" y="${y - 4}">${totals[i]}</text>`;
      if (a.label) svg += `<text class="label" x="${cx}" y="${H - 6}">${esc(a.label)}</text>`;
      svg += "</g>";
    });
    svg += `<line class="axis" x1="0" x2="${W}" y1="${H - BOTTOM}" y2="${H - BOTTOM}"/></svg>`;
    $("stats-chart").innerHTML = svg;
  }

  $("stats-chart").addEventListener("click", (ev) => {
    const bar = ev.target.closest("[data-key]");
    if (!bar) return;
    const key = Number(bar.dataset.key);
    statsPick = statsPick === key ? null : key;
    statsDay = null;
    renderStats();
  });

  function renderStatsShips(rows) {
    const byShip = new Map();
    for (const p of rows) {
      if (!byShip.has(p.mmsi)) byShip.set(p.mmsi, []);
      byShip.get(p.mmsi).push(p);
    }
    const innerOf = (mmsi) => (byShip.get(mmsi) || []).filter((p) => p.route === "inner");
    const shown = statsData.ships.filter((s) => s.current || byShip.has(s.mmsi))
      .sort((a, b) => innerOf(b.mmsi).length - innerOf(a.mmsi).length || a.name.localeCompare(b.name));
    $("stats-ships").replaceChildren(...shown.map((ship) => {
      const all = byShip.get(ship.mmsi) || [];
      const inner = innerOf(ship.mmsi);
      const east = inner.filter((p) => p.direction === "east");
      const west = inner.filter((p) => p.direction === "west");
      const li = document.createElement("li");
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = ship.color;
      const grow = document.createElement("div");
      grow.className = "grow";
      const line = (text, cls) => {
        const span = document.createElement("span");
        span.className = cls;
        span.textContent = text;
        grow.append(span);
      };
      line(ship.current ? ship.name : `${ship.name} (removed)`, "");
      line(inner.length ? `${east.length} towards Helsinki · ${west.length} towards Stockholm` : "No inner-route passages", "small muted");
      const usual = [];
      if (east.length >= 3) usual.push(`${hhmm(median(east.map((p) => p.minute)))} towards Helsinki`);
      if (west.length >= 3) usual.push(`${hhmm(median(west.map((p) => p.minute)))} towards Stockholm`);
      if (usual.length) line(`Usually ${usual.join(", ")}`, "small muted");
      if (all.length > inner.length) {
        line(`Inner route on ${inner.length} of ${all.length} recorded trips (${Math.round((100 * inner.length) / all.length)}%)`, "small muted");
      }
      const count = document.createElement("span");
      count.className = "stat-count";
      count.textContent = String(inner.length);
      li.append(dot, grow, count);
      return li;
    }));
  }

  // --- fleets -------------------------------------------------------------------

  function renderAircraftSection() {
    const info = lastState?.aircraft || { enabled: false };
    const section = document.createElement("section");
    section.className = "fleet";
    const head = document.createElement("label");
    head.className = "fleet-head";
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = AIRCRAFT_COLORS.FinnHEMS;
    const name = document.createElement("span");
    name.className = "grow strong";
    name.textContent = "Rescue aircraft";
    const count = document.createElement("span");
    count.className = "small muted";
    count.textContent = info.enabled ? `${aircraft.size} in the air` : "";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.className = "switch";
    toggle.checked = info.enabled;
    toggle.addEventListener("change", async () => {
      toggle.disabled = true;
      try {
        await api("api/aircraft", { method: "POST", body: { enabled: toggle.checked } });
        await loadState();
      } finally {
        toggle.disabled = false;
      }
    });
    head.append(dot, name, count, toggle);
    const note = document.createElement("p");
    note.className = info.error ? "small warn-text" : "small muted note";
    note.innerHTML = info.error
      ? `Couldn't reach adsb.fi just now (${esc(info.error)}). It tries again shortly.`
      : "FinnHEMS, Border Guard and police helicopters and Border Guard planes. While this is on, the add-on also records their flights with the map closed (a check every 30 seconds) and keeps a year of them. Data from <a href=\"https://adsb.fi\" target=\"_blank\" rel=\"noopener\">adsb.fi</a>.";
    section.append(head, note);
    if (info.enabled || info.flights_stored) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "ah-open-button";
      const n = info.flights_stored || 0;
      open.innerHTML = `<span class="strong">Aircraft history</span><span class="small muted">${n ? `${n.toLocaleString()} ${n === 1 ? "flight" : "flights"} stored` : "No flights stored yet"}</span>`;
      open.addEventListener("click", () => historyView.open());
      section.append(open);
    }
    return section;
  }

  function renderFleets() {
    const box = $("fleets-list");
    const fleets = lastState?.fleets || [];
    box.replaceChildren(...fleets.map((fleet) => {
      const section = document.createElement("section");
      section.className = "fleet";
      const head = document.createElement("label");
      head.className = "fleet-head";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.className = "switch";
      toggle.checked = fleet.enabled;
      toggle.addEventListener("change", async () => {
        toggle.disabled = true;
        try {
          await api(`api/fleets/${fleet.id}`, { method: "POST", body: { enabled: toggle.checked } });
          await loadState();
          await loadTails();
        } finally {
          toggle.disabled = false;
        }
      });
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = fleet.color;
      const name = document.createElement("span");
      name.className = "grow strong";
      name.textContent = fleet.name;
      const count = document.createElement("span");
      count.className = "small muted";
      head.append(dot, name, count, toggle);
      section.append(head);

      const members = fleet.members.map((mmsi) => ships.get(mmsi)?.info).filter(Boolean);
      const seen = members.filter((info) => info.position && now() - info.position.ts < FLEET_HIDE_SECONDS);
      const moving = seen.filter((info) => !info.in_port && info.position.sog > 2);
      const followed = fleet.id === "followed";
      const n = fleet.members.length;
      count.textContent = n
        ? `${n} ${n === 1 ? "ship" : "ships"}${fleet.enabled ? `, ${moving.length} under way` : ""}`
        : (followed ? "None yet" : "None found yet");
      if (followed && !n) {
        const note = document.createElement("p");
        note.className = "small muted";
        note.textContent = "Search for a ship in the Ships tab and choose Follow to show it here.";
        section.append(note);
      }
      if (!fleet.enabled) return section;
      // Followed ships are listed even when they haven't been heard from.
      if (followed) seen.splice(0, seen.length, ...members);

      seen.sort((a, b) => Number(Boolean(a.fleet)) - Number(Boolean(b.fleet)) || a.name.localeCompare(b.name));
      const list = document.createElement("ul");
      list.className = "ships-list fleet-ships";
      list.replaceChildren(...seen.map((info) => {
        const li = document.createElement("li");
        li.className = "clickable";
        const d = document.createElement("span");
        d.className = "dot";
        d.style.background = info.color;
        const grow = document.createElement("div");
        grow.className = "grow";
        const n = document.createElement("span");
        n.textContent = info.fleet ? info.name : `${info.name} (my ship)`;
        const sub = document.createElement("span");
        sub.className = "small muted";
        sub.textContent = !info.position ? "No position yet" : isStale(info) ? `Last seen ${ago(info.position.ts)}` : motionText(info);
        grow.append(n, sub);
        li.append(d, grow);
        if (followed) {
          const stop = document.createElement("button");
          stop.type = "button";
          stop.textContent = "Stop following";
          stop.addEventListener("click", async (ev) => {
            ev.stopPropagation();
            stop.disabled = true;
            await api(`api/follow/${info.mmsi}`, { method: "DELETE" });
            await loadState();
          });
          li.append(stop);
        }
        li.addEventListener("click", () => {
          $("ships-dialog").close();
          focusShip(info.mmsi);
        });
        return li;
      }));
      section.append(list);
      const quiet = members.length - seen.length + (fleet.members.length - members.length);
      if (quiet > 0) {
        const note = document.createElement("p");
        note.className = "small muted";
        note.textContent = `${quiet} more not seen in Finnish waters this week.`;
        section.append(note);
      }
      return section;
    }));
  }

  // --- "On the map": layer switches ------------------------------------------------

  function switchRow({ title, color, checked, count = "", note, extra, onChange }) {
    const section = document.createElement("section");
    section.className = "fleet";
    const head = document.createElement("label");
    head.className = "fleet-head";
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = color;
    const name = document.createElement("span");
    name.className = "grow strong";
    name.textContent = title;
    const counter = document.createElement("span");
    counter.className = "small muted";
    counter.textContent = count;
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.className = "switch";
    toggle.checked = checked;
    toggle.addEventListener("change", async () => {
      toggle.disabled = true;
      try {
        await onChange(toggle.checked);
      } catch (err) {
        console.error(err);
      } finally {
        toggle.disabled = false;
      }
    });
    head.append(dot, name, counter, toggle);
    section.append(head);
    if (note) section.append(noteText(note));
    if (extra) section.append(extra);
    return section;
  }

  // A switch's explanation: two lines until tapped, so a list of switches stays short. What
  // went wrong with a source is always shown whole.
  function noteText(text) {
    const p = document.createElement("p");
    const problem = /^Couldn't/.test(text);
    p.className = problem ? "small warn-text" : "small muted note";
    p.textContent = text;
    return p;
  }

  // Headings between groups of switches.
  function groupHead(text) {
    const h = document.createElement("h3");
    h.className = "group-head";
    h.textContent = text;
    return h;
  }

  function trafficLegend() {
    const ul = document.createElement("ul");
    ul.className = "legend small";
    for (const [label, color] of AIS_CLASSES) {
      const li = document.createElement("li");
      const i = document.createElement("i");
      i.style.background = color;
      li.append(i, label);
      ul.append(li);
    }
    return ul;
  }

  function renderLayers() {
    const w = lastState?.weather || {};
    const weatherOn = w.enabled !== false;
    const faultCount = faultsData?.faults?.length;
    $("layers-list").replaceChildren(
      groupHead("Weather"),
      switchRow({
        title: "Sea conditions", color: "#0ea5e9", checked: weatherOn,
        note: weatherOn && w.error
          ? `Couldn't reach the Finnish Meteorological Institute just now (${w.error}). It tries again in a few minutes.`
          : "Wind, sea temperature, waves and visibility from FMI stations near Porkkala, read while the map is open. Shown at the top of the map; tap it for details.",
        onChange: async (on) => { await api("api/weather", { method: "POST", body: { enabled: on } }); await loadState(); },
      }),
      switchRow({
        title: "Weather stations", color: "#0284c7", checked: stationsOn,
        count: stationsOn && stationsData?.stations ? `${stationsData.stations.length} stations` : "",
        note: stationsOn && stationsData?.error
          ? `Couldn't reach the Finnish Meteorological Institute just now (${stationsData.error}). It tries again in a few minutes.`
          : "Wind at FMI's coastal and sea stations in m/s, the arrow pointing where it blows. Tap one for gusts, temperatures and the past 24 hours. Read every 10 minutes while the map is open.",
        extra: stationsOn ? windLegend() : null,
        onChange: async (on) => { setStations(on); renderLayers(); },
      }),
      ...overlays.rows(),
      groupHead("On the chart"),
      switchRow({
        title: "Harbours", color: HARBOUR_RED, checked: harboursOn,
        count: harboursOn && harbourCount ? `${harbourCount} in view` : "",
        note: harbourNote || "Marinas and harbours from OpenStreetMap with their services, from zoom 11. Tap one for its card. Downloaded once a month.",
        onChange: async (on) => { setHarbours(on); renderLayers(); },
      }),
      switchRow({
        title: "Faulty lights and buoys", color: "#dc2626", checked: faultsOn,
        count: faultsOn && faultCount != null ? `${faultCount} nearby` : "",
        note: "Unlit, damaged or missing buoys, beacons and lights, from Digitraffic. Shown on the chart view.",
        onChange: async (on) => { faultsOn = on; store.set("fw-faults", on ? "1" : "0"); renderFaults(); renderLayers(); },
      }),
      switchRow({
        title: "Wrecks and coastal heritage", color: "#92400e", checked: heritageOn,
        note: "Wrecks, and bunkers, trenches and gun positions on the shore and islands (from zoom 13), from Museovirasto's registers. Shown on the drawn chart.",
        onChange: async (on) => { heritageOn = on; store.set("fw-heritage", on ? "1" : "0"); applyHeritage(); },
      }),
      groupHead("Traffic"),
      switchRow({
        title: "Other ships", color: "#64748b", checked: trafficOn, count: trafficOn ? trafficCount : "",
        note: trafficNote || "All AIS traffic around the view (zoomed in a little), refreshed every 30 seconds while switched on and the map is open.",
        extra: trafficOn ? trafficLegend() : null,
        onChange: async (on) => { setTraffic(on); renderLayers(); },
      }),
      renderAircraftSection(),
    );
    renderRoutesList();
    renderFleets();
  }

  // --- settings -------------------------------------------------------------------

  function renderSettings() {
    const mem = lastState?.memory;
    const mb = mem?.total ?? lastState?.memory_mb;
    // Where it goes: the parts holding data, then the program itself (Python, libraries).
    const NAMES = { chart: "chart", "depth model": "foreign depths", database: "database", ships: "ships", aircraft: "aircraft" };
    const parts = (mem?.parts || []).filter(([, v]) => v >= 0.1).map(([k, v]) => `${NAMES[k] || k} ${v.toFixed(1)}`);
    if (mem?.program != null) parts.push(`program ${Math.round(mem.program)}`);
    const peak = mem?.peak > mb ? `, at most ${Math.round(mem.peak)} MB since it started` : "";
    setText($("memory"), mb ? `Memory in use: ${Math.round(mb)} MB${peak}.${parts.length ? ` In MB: ${parts.join(", ")}.` : ""}` : "");
    const problems = overlays.problems();
    $("overlay-status").hidden = !problems.length;
    setText($("overlay-status"), problems.length
      ? `Weather layers that couldn't be read just now: ${problems.join("; ")}. They try again in a few minutes.` : "");
    const checks = overlays.checks();
    $("overlay-checks").hidden = !checks.length;
    setText($("overlay-checks"), checks.join(" "));
  }

  // --- wrecks and coastal heritage on the drawn chart ---------------------------------

  let heritageOn = store.get("fw-heritage") !== "0";
  function applyHeritage() {
    try {
      window.FerryChart?.setOptions?.({ heritage: heritageOn });
    } catch (err) {
      console.error(err);
    }
  }
  applyHeritage();

  // --- conditions pill and sheet (FMI observations, the sun, the feed) -----------------

  const WEATHER_STALE = 3 * 3600; // older observations stay off the pill
  const fresh = (v) => v && now() - v.ts < WEATHER_STALE;
  const visibilityText = (km) => (km >= 10 ? `${Math.round(km)} km` : `${km.toFixed(1)} km`);

  function nextSun(sun) {
    if (!sun) return null;
    const t = now();
    if (sun.rise && t < sun.rise) return { kind: "rise", ts: sun.rise };
    if (sun.set && t < sun.set) return { kind: "set", ts: sun.set };
    return sun.next_rise ? { kind: "rise", ts: sun.next_rise } : null;
  }

  function renderConditions() {
    const w = lastState?.weather || {};
    const on = w.enabled !== false;
    const wind = on && fresh(w.wind) ? w.wind : null;
    const pw = $("pill-wind");
    pw.hidden = !wind;
    if (wind) {
      setText(pw.lastElementChild, `${Math.round(wind.ms)} m/s`);
      const arrow = pw.firstElementChild;
      arrow.style.transform = `rotate(${((wind.dir ?? 0) + 180) % 360}deg)`; // points where it blows to
      arrow.style.visibility = wind.dir == null ? "hidden" : "";
      pw.title = `Wind ${compass(wind.dir)} ${wind.ms.toFixed(1)} m/s`.replace("  ", " ");
    }
    const water = on && fresh(w.water) ? w.water : null;
    $("pill-water").hidden = !water;
    if (water) {
      setText($("pill-water"), `Sea ${Math.round(water.c)}°`);
      $("pill-water").title = `Sea temperature ${water.c.toFixed(1)} °C`;
    }
    const sun = nextSun(w.sun);
    const ps = $("pill-sun");
    ps.hidden = !sun;
    if (sun) {
      setText(ps.lastElementChild, `${sun.kind === "rise" ? "↑" : "↓"}${clock(sun.ts)}`);
      ps.classList.toggle("rise", sun.kind === "rise");
      ps.title = `${sun.kind === "rise" ? "Sunrise" : "Sunset"} ${clock(sun.ts)}`;
    }
    $("conditions").setAttribute("aria-label", `Conditions: ${[statusText, pw.hidden ? "" : pw.title,
      $("pill-water").hidden ? "" : $("pill-water").title, ps.hidden ? "" : ps.title].filter(Boolean).join(", ")}`);
    if (!$("conditions-sheet").hidden) renderConditionsSheet();
  }

  function renderConditionsSheet() {
    const w = lastState?.weather || {};
    const rows = [];
    const at = (v) => `${v.where}, ${clock(v.ts)}${now() - v.ts > 86400 ? ` ${new Date(v.ts * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}` : ""}`;
    if (w.enabled !== false) {
      if (w.wind) {
        const gust = w.wind.gust != null ? `, gusts ${Math.round(w.wind.gust)}` : "";
        rows.push(["Wind", `${compass(w.wind.dir)} ${w.wind.ms.toFixed(1)} m/s${gust}`.trim(), at(w.wind)]);
      }
      if (w.air) rows.push(["Air", `${w.air.c.toFixed(1)} °C`, at(w.air)]);
      if (w.visibility) {
        rows.push(["Visibility", `${visibilityText(w.visibility.km)}${w.visibility.weather ? `, ${w.visibility.weather}` : ""}`, at(w.visibility)]);
      }
      if (w.water) rows.push(["Sea temperature", `${w.water.c.toFixed(1)} °C`, at(w.water)]);
      if (w.sea_level) rows.push(["Sea level", `${w.sea_level.cm > 0 ? "+" : ""}${w.sea_level.cm} cm`, `${at(w.sea_level)} · against the long-term mean`]);
      if (w.waves) rows.push(["Waves", `${w.waves.m.toFixed(1)} m${w.waves.dir != null ? ` from ${compass(w.waves.dir)}` : ""}`, at(w.waves)]);
    }
    const sun = w.sun || {};
    if (sun.rise || sun.set) {
      rows.push(["Sun", [sun.rise && `rises ${clock(sun.rise)}`, sun.set && `sets ${clock(sun.set)}`].filter(Boolean).join(" · "),
        sun.dusk ? `Dark from ${clock(sun.dusk)}${sun.next_rise ? ` · next sunrise ${clock(sun.next_rise)}` : ""}` : ""]);
    }
    const ph = lastState?.phone;
    if (ph) {
      rows.push(["Your phone", phoneFix() ? "Shown on the map" : "Not recent enough to show",
        `Located ${ago(ph.ts)}${ph.accuracy ? ` · within ${Math.round(ph.accuracy)} m` : ""}`]);
    }
    const feed = lastState?.feed || {};
    rows.push(["Ship positions", statusText, feed.last_message_ts ? `Last AIS message ${ago(feed.last_message_ts)}` : "Digitraffic"]);
    $("conditions-list").replaceChildren(...rows.flatMap(([label, value, meta]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      if (meta) {
        const m = document.createElement("span");
        m.className = "small muted";
        m.textContent = meta;
        dd.append(m);
      }
      return [dt, dd];
    }));
    let note = "Observations: Finnish Meteorological Institute (CC BY 4.0), read every 10 minutes while the map is open.";
    if (w.enabled === false) note = "Sea conditions are switched off. Switch them on under Menu › On the map.";
    else if (w.error) note = `Couldn't reach FMI just now (${w.error}); showing the last values. ${note}`;
    else if (!w.fetched) note = `Reading the first observations… ${note}`;
    setText($("conditions-note"), note);
  }

  function toggleConditions(open = $("conditions-sheet").hidden) {
    $("conditions-sheet").hidden = !open;
    document.body.classList.toggle("conditions-open", open); // the chips give way on a phone
    $("conditions").setAttribute("aria-expanded", String(open));
    if (open) renderConditionsSheet();
  }
  $("conditions").addEventListener("click", () => toggleConditions());
  $("conditions-close").addEventListener("click", () => toggleConditions(false));

  // --- you are here: the phone's position from Home Assistant ----------------------------

  let phoneLayer = null; // { group, halo, dot }

  function renderPhone() {
    const ph = phoneFix();
    if (!ph) {
      phoneLayer?.group.remove();
      phoneLayer = null;
      return;
    }
    const ll = [ph.lat, ph.lon];
    if (!phoneLayer) {
      const halo = L.circle(ll, { radius: 0, color: "#2563eb", weight: 1, opacity: 0.35, fillOpacity: 0.08, interactive: false });
      const dot = L.circleMarker(ll, { radius: 7, color: "#fff", weight: 2.5, fillColor: "#2563eb", fillOpacity: 1 })
        .bindTooltip("", { direction: "top", offset: [0, -6] });
      phoneLayer = { group: L.layerGroup([halo, dot]).addTo(map), halo, dot };
    }
    phoneLayer.halo.setLatLng(ll).setRadius(Math.min(ph.accuracy || 0, 2000));
    phoneLayer.dot.setLatLng(ll).setTooltipContent(`You · ${ago(ph.ts)}`);
  }

  // --- other ships: all AIS traffic around the view, only while switched on -----------------

  const TRAFFIC_MIN_ZOOM = 8; // the server keeps the circle within 60 km anyway
  const TRAFFIC_POLL = 30000;
  const AIS_CLASSES = [
    ["Passenger", "#2563eb", (t) => t >= 60 && t <= 69],
    ["Cargo", "#16a34a", (t) => t >= 70 && t <= 79],
    ["Tanker", "#dc2626", (t) => t >= 80 && t <= 89],
    ["High speed", "#ca8a04", (t) => t >= 40 && t <= 49],
    ["Tug, pilot, service", "#0891b2", (t) => (t >= 31 && t <= 35) || (t >= 50 && t <= 59)],
    ["Fishing", "#ea580c", (t) => t === 30],
    ["Sailing, pleasure", "#c026d3", (t) => t === 36 || t === 37],
    ["Other or unknown", "#6b7280", () => true],
  ];
  const aisColor = (type) => AIS_CLASSES.find(([, , test]) => test(type ?? -1))[1];
  const AIS_TYPES = { 30: "Fishing vessel", 31: "Towing", 32: "Towing", 33: "Dredger", 34: "Diving support", 35: "Military",
    36: "Sailing boat", 37: "Pleasure craft", 50: "Pilot boat", 51: "Search and rescue", 52: "Tug", 53: "Port tender",
    54: "Anti-pollution", 55: "Law enforcement", 58: "Medical transport" };
  function aisType(type) {
    if (AIS_TYPES[type]) return AIS_TYPES[type];
    if (type >= 40 && type <= 49) return "High-speed craft";
    if (type >= 60 && type <= 69) return "Passenger ship";
    if (type >= 70 && type <= 79) return "Cargo ship";
    if (type >= 80 && type <= 89) return "Tanker";
    return "Ship";
  }

  // A small triangle along the course (a dot when still), drawn on one canvas for all of them.
  const Boat = L.CircleMarker.extend({
    _updatePath() {
      const r = this._renderer;
      if (!r._drawing || this._empty()) return;
      const p = this._point, ctx = r._ctx, size = this._radius;
      ctx.beginPath();
      if (this.options.course == null) {
        ctx.arc(p.x, p.y, size * 0.55, 0, Math.PI * 2);
      } else {
        const a = (this.options.course * Math.PI) / 180, cos = Math.cos(a), sin = Math.sin(a);
        const pt = (dx, dy) => [p.x + dx * cos - dy * sin, p.y + dx * sin + dy * cos];
        ctx.moveTo(...pt(0, -size * 1.15));
        ctx.lineTo(...pt(size * 0.65, size * 0.8));
        ctx.lineTo(...pt(-size * 0.65, size * 0.8));
        ctx.closePath();
      }
      r._fillStroke(ctx, this);
    },
  });

  let trafficOn = store.get("fw-traffic") === "1";
  const traffic = new Map(); // mmsi -> Boat
  let trafficLayer = null, trafficCanvas = null, trafficTimer = null, trafficMoveTimer = null;
  let trafficBusy = false, trafficNote = "", trafficCount = "";

  function clearTraffic() {
    trafficLayer?.clearLayers();
    traffic.clear();
  }

  function setTraffic(on) {
    trafficOn = on;
    store.set("fw-traffic", on ? "1" : "0");
    trafficNote = "";
    trafficCount = "";
    if (on) pollTraffic();
    else { clearTimeout(trafficTimer); clearTraffic(); }
  }

  async function pollTraffic() {
    clearTimeout(trafficTimer);
    if (!trafficOn || document.hidden) return;
    trafficTimer = setTimeout(pollTraffic, TRAFFIC_POLL);
    if (trafficBusy) return;
    if (map.getZoom() < TRAFFIC_MIN_ZOOM) {
      clearTraffic();
      trafficNote = "Zoom in to see other ships.";
      trafficCount = "";
      return;
    }
    const c = map.getCenter();
    const radius = Math.ceil(map.distance(c, map.getBounds().getNorthEast()) / 1000);
    trafficBusy = true;
    try {
      const data = await api(`api/traffic?lat=${c.lat.toFixed(3)}&lon=${c.lng.toFixed(3)}&r=${radius}`);
      if (!trafficOn) return;
      drawTraffic(data.ships || []);
      trafficNote = data.error ? `Couldn't reach Digitraffic just now (${data.error}).` : "";
      trafficCount = `${traffic.size} in view`;
    } catch (err) {
      trafficNote = String(err.message || err);
    } finally {
      trafficBusy = false;
    }
  }

  function drawTraffic(rows) {
    if (!trafficLayer) {
      trafficCanvas = sharedCanvas();
      trafficLayer = L.layerGroup().addTo(map);
    }
    const seen = new Set();
    for (const row of rows) {
      const [mmsi, lat, lon, sog, cog, heading, , type] = row;
      if (ships.has(mmsi)) continue; // already on the map as one of mine or a fleet ship
      seen.add(mmsi);
      const course = sog != null && sog >= 0.5 ? (heading ?? cog) : null;
      const color = aisColor(type);
      let boat = traffic.get(mmsi);
      if (!boat) {
        boat = new Boat([lat, lon], { renderer: trafficCanvas, radius: 7, color: "#fff", weight: 1, opacity: 0.9,
          fillColor: color, fillOpacity: 0.85, course });
        boat.bindPopup(() => trafficPopup(boat), { className: "aid-popup", offset: [0, -4] });
        boat.addTo(trafficLayer);
        traffic.set(mmsi, boat);
      } else {
        const changed = boat.options.course !== course || boat.options.fillColor !== color;
        boat.options.course = course;
        if (changed) boat.setStyle({ fillColor: color });
        const ll = boat.getLatLng();
        if (ll.lat !== lat || ll.lng !== lon) boat.setLatLng([lat, lon]);
        else if (changed) boat.redraw();
      }
      boat.row = row;
    }
    for (const [mmsi, boat] of traffic) {
      if (seen.has(mmsi) || boat.isPopupOpen()) continue;
      boat.remove();
      traffic.delete(mmsi);
    }
  }

  function trafficPopup(boat) {
    const [mmsi, , , sog, cog, heading, name, type, length, dest] = boat.row;
    const el = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = name || `MMSI ${mmsi}`;
    el.append(title);
    const dir = compass(heading ?? cog);
    const lines = [
      [aisType(type), length ? `${length} m` : null].filter(Boolean).join(" · "),
      sog == null ? "" : sog < 0.5 ? "Stopped" : `${sog.toFixed(1)} kn${dir ? ` heading ${dir}` : ""}`,
      dest ? (() => { const t = destinationText(dest); return t[0].toUpperCase() + t.slice(1); })() : "",
    ];
    for (const text of lines.filter(Boolean)) {
      el.append(document.createElement("br"), text);
    }
    const follow = document.createElement("button");
    follow.type = "button";
    follow.className = "link-button popup-action";
    follow.textContent = "Follow";
    follow.title = "Show it on the map with its latest trip";
    follow.addEventListener("click", async () => {
      follow.disabled = true;
      follow.textContent = "Following…";
      try {
        await api("api/follow", { method: "POST", body: { mmsi, name } });
        map.closePopup();
        boat.remove();
        traffic.delete(mmsi);
        await loadState();
        await loadTails();
      } catch (err) {
        follow.textContent = String(err.message || err);
      }
    });
    el.append(document.createElement("br"), follow);
    return el;
  }

  map.on("moveend", () => {
    if (!trafficOn) return;
    clearTimeout(trafficMoveTimer);
    trafficMoveTimer = setTimeout(pollTraffic, 1000);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(trafficTimer);
    else if (trafficOn) pollTraffic();
  });

  // --- faulty lights and buoys (Digitraffic), on the chart view ----------------------------

  const faultIcon = () => L.divIcon({ className: "fault-icon", html: "<span>!</span>", iconSize: [22, 22], iconAnchor: [11, 11] });

  function renderFaults() {
    if (!faultsOn || activeLayer !== layers.chart) {
      faultsLayer?.remove();
      return;
    }
    if (!faultsData || Date.now() - faultsAt > FAULTS_REFRESH) loadFaults();
    if (faultsLayer && !map.hasLayer(faultsLayer)) faultsLayer.addTo(map);
  }

  async function loadFaults() {
    if (faultsLoading) return;
    faultsLoading = true;
    try {
      faultsData = await api("api/faults");
      faultsAt = Date.now();
      drawFaults();
    } catch (err) {
      console.error(err);
      faultsAt = Date.now() - FAULTS_REFRESH + 5 * 60000; // try again in five minutes
    } finally {
      faultsLoading = false;
    }
  }

  function faultPopup(f) {
    const since = f.since ? new Date(f.since * 1000) : null;
    const day = since && since.toLocaleDateString("en-GB", {
      day: "numeric", month: "short", ...(now() - f.since > 300 * 86400 ? { year: "numeric" } : {}),
    });
    const what = [f.name, f.aton_type && f.aton_type.toLowerCase()].filter(Boolean).join(", ");
    return `<strong>${esc(f.type)}${day ? ` since ${esc(day)}` : ""}</strong>`
      + `${what ? `<br>${esc(what)}` : ""}${f.fairway ? `<br><span class="muted">${esc(f.fairway)}</span>` : ""}`;
  }

  function drawFaults() {
    faultsLayer?.remove();
    faultsLayer = L.layerGroup((faultsData?.faults || []).map((f) => L.marker([f.lat, f.lon], {
      icon: faultIcon(), keyboard: false, zIndexOffset: 400, title: `${f.type}: ${f.name || ""}`,
    }).bindPopup(faultPopup(f), { className: "aid-popup", offset: [0, -8] })));
    if (faultsOn && activeLayer === layers.chart) faultsLayer.addTo(map);
  }

  // --- measure and plan a route: nautical miles, true bearings, ETA ------------------------
  // Tap the map to add points, drag one to move it, tap it to remove it. Not a navigation
  // tool: no guidance, just planning and measuring. The route being drawn is kept in this
  // browser between visits; saved routes are kept by the add-on.

  const NavM = window.NavMath;
  const ROUTE_COLOR = "#c026d3";
  const ROUTE_MAX_POINTS = 200;
  const route = { on: false, points: [], undo: [], id: null, name: "", auto: false, status: null };
  let routeSpeed = Number(store.get("fw-route-speed")) || 25;
  let routeDraught = Number(store.get("fw-route-draught")) || 1.5;
  let routeLayer = null; // { group, casing, line, labels, markers }
  const routeMarkers = [];
  let savedRoutes = [];
  const savedLayer = L.layerGroup().addTo(map);
  try {
    const draft = JSON.parse(store.get("fw-route-draft") || "null");
    if (Array.isArray(draft?.points)) Object.assign(route, { points: draft.points, id: draft.id ?? null, name: draft.name || "" });
  } catch { /* no draft */ }

  const round6 = (v) => Math.round(v * 1e6) / 1e6;
  const latLngOf = (ll) => [round6(ll.lat), round6(((ll.lng + 540) % 360) - 180)];

  function setRouteMode(on) {
    if (on === route.on) return;
    route.on = on;
    store.set("fw-route-open", on ? "1" : "0");
    if (on) {
      if (editing) setEditing(false);
      if (focused) unfocus();
      clearReplay();
      map.closePopup();
    } else {
      setAuto(false);
    }
    $("route").hidden = !on;
    $("route-button").setAttribute("aria-pressed", String(on));
    document.body.classList.toggle("route-open", on);
    map.getContainer().classList.toggle("measuring", on);
    $("route-speed").value = String(routeSpeed);
    $("route-draught").value = String(routeDraught);
    $("route-name").value = route.name;
    drawRoute();
    renderSavedRoutes(); // not clickable while measuring, and the one being edited isn't drawn twice
  }

  function routeIcon(i, n) {
    const kind = i === 0 ? " start" : i === n - 1 ? " end" : "";
    return L.divIcon({ className: "rp-icon", html: `<span class="rp${kind}">${i + 1}</span>`, iconSize: [36, 36], iconAnchor: [18, 18] });
  }

  function pushUndo() {
    route.undo.push(JSON.stringify(route.points));
    if (route.undo.length > 60) route.undo.shift();
  }

  function undoRoute() {
    if (!route.undo.length) return;
    route.points = JSON.parse(route.undo.pop());
    route.status = null;
    drawRoute();
  }

  function addRoutePoint(ll) {
    if (route.points.length >= ROUTE_MAX_POINTS) return;
    pushUndo();
    route.points.push(latLngOf(ll));
    if (route.auto && route.points.length === 2) planAuto();
    else drawRoute();
  }

  function makeRouteMarker() {
    const m = L.marker([0, 0], { draggable: true, keyboard: false, zIndexOffset: 2000, autoPan: true });
    m.on("dragstart", () => { pushUndo(); m.dragged = true; });
    m.on("drag", () => {
      const i = routeMarkers.indexOf(m);
      if (i >= 0) route.points[i] = latLngOf(m.getLatLng());
      scheduleRouteDraw();
    });
    // Leaflet may still send a click when a drag ends; that must not delete the point.
    m.on("dragend", () => { setTimeout(() => { m.dragged = false; }, 60); drawRoute(); });
    m.on("click", () => {
      if (m.dragged) return;
      const i = routeMarkers.indexOf(m);
      if (i < 0) return;
      pushUndo();
      route.points.splice(i, 1);
      drawRoute();
    });
    return m;
  }

  let routeFrame = 0;
  function scheduleRouteDraw() {
    if (!routeFrame) routeFrame = requestAnimationFrame(() => { routeFrame = 0; drawRoute(); });
  }

  function drawRoute() {
    if (!route.on) {
      routeLayer?.group.remove();
      return;
    }
    if (!routeLayer) {
      const group = L.layerGroup();
      routeLayer = {
        group,
        casing: L.polyline([], { color: "#fff", weight: 7, opacity: 0.85, interactive: false }).addTo(group),
        line: L.polyline([], { color: ROUTE_COLOR, weight: 3.5, opacity: 1, interactive: false }).addTo(group),
        labels: L.layerGroup().addTo(group),
        markers: L.layerGroup().addTo(group),
      };
    }
    if (!map.hasLayer(routeLayer.group)) routeLayer.group.addTo(map);
    const pts = route.points;
    routeLayer.casing.setLatLngs(pts);
    routeLayer.line.setLatLngs(pts);
    while (routeMarkers.length > pts.length) routeMarkers.pop().remove();
    pts.forEach((p, i) => {
      let m = routeMarkers[i];
      if (!m) {
        m = makeRouteMarker();
        routeMarkers.push(m);
        m.setLatLng(p).addTo(routeLayer.markers);
      }
      const ll = m.getLatLng();
      if (ll.lat !== p[0] || ll.lng !== p[1]) m.setLatLng(p);
      const key = `${i}/${pts.length}`;
      if (m.iconKey !== key) { m.iconKey = key; m.setIcon(routeIcon(i, pts.length)); }
    });
    drawLegLabels();
    renderRoutePanel();
    store.set("fw-route-draft", JSON.stringify({ points: pts, id: route.id, name: route.name }));
  }

  // "2.2 NM · 214°" on each leg long enough on screen to hold it.
  function drawLegLabels() {
    if (!routeLayer) return;
    routeLayer.labels.clearLayers();
    const pts = route.points;
    for (let i = 1; i < pts.length; i++) {
      const a = map.latLngToContainerPoint(pts[i - 1]), b = map.latLngToContainerPoint(pts[i]);
      if (a.distanceTo(b) < 120) continue;
      const leg = NavM.legs([pts[i - 1], pts[i]])[0];
      L.marker(map.containerPointToLatLng(a.add(b).divideBy(2)), {
        interactive: false, keyboard: false,
        icon: L.divIcon({ className: "leg-label", html: `<span>${NavM.nm(leg.nm)} · ${NavM.degrees(leg.bearing)}</span>`, iconSize: null }),
      }).addTo(routeLayer.labels);
    }
  }
  map.on("zoomend", () => { if (route.on) drawLegLabels(); });

  function routeSummary(points, speed) {
    const total = NavM.totalNm(points);
    const eta = NavM.etaSeconds(total, speed);
    return { total, eta, text: `${NavM.nm(total)} · ${points.length - 1} ${points.length === 2 ? "leg" : "legs"}${eta != null ? ` · ${NavM.duration(eta)} at ${speed} kn` : ""}` };
  }

  function renderRoutePanel() {
    const pts = route.points;
    const legs = NavM.legs(pts);
    const { total, eta } = routeSummary(pts, routeSpeed);
    setText($("route-total"), pts.length > 1 ? NavM.nm(total) : pts.length ? "1 point" : "Route");
    setText($("route-eta"), pts.length > 1 && eta != null ? `${NavM.duration(eta)} · ETA ${clock(now() + eta)}` : "");
    let hint = "";
    if (route.status) hint = route.status.text;
    else if (route.auto) hint = pts.length < 2 ? "Tap the start, then the end." : "From the first point to the last, along the marked fairways. Planning only, not for navigation.";
    else if (pts.length < 2) hint = "Tap the map to add points. Drag a point to move it, tap it to remove it.";
    setText($("route-hint"), hint);
    $("route-hint").hidden = !hint;
    $("route-hint").classList.toggle("error", Boolean(route.status?.error));

    const items = [];
    pts.forEach((p, i) => {
      if (i > 0) {
        const leg = legs[i - 1];
        const li = document.createElement("li");
        li.className = "rp-leg";
        const t = NavM.etaSeconds(leg.nm, routeSpeed);
        li.innerHTML = `<span class="leg-n">${i}–${i + 1}</span><strong>${NavM.nm(leg.nm)}</strong><span>${NavM.degrees(leg.bearing)}</span>${t != null ? `<span class="muted">${NavM.duration(t)}</span>` : ""}`;
        items.push(li);
      }
      const li = document.createElement("li");
      li.className = "rp-row";
      const n = document.createElement("span");
      n.className = `rp${i === 0 ? " start" : i === pts.length - 1 && i > 0 ? " end" : ""}`;
      n.textContent = String(i + 1);
      const where = document.createElement("span");
      where.className = "grow";
      where.textContent = NavM.position(p[0], p[1]);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "icon-button plain";
      del.setAttribute("aria-label", `Remove point ${i + 1}`);
      del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg>';
      del.addEventListener("click", () => { pushUndo(); route.points.splice(i, 1); drawRoute(); });
      where.addEventListener("click", () => map.panTo(p));
      li.append(n, where, del);
      items.push(li);
    });
    $("route-points").replaceChildren(...items);
    $("route-points").hidden = !items.length;
    $("route-undo").disabled = !route.undo.length;
    $("route-clear").disabled = !pts.length && !route.id;
    $("route-save").disabled = pts.length < 2;
    $("route-save").textContent = route.id ? "Update" : "Save";
    $("route-plan").disabled = pts.length < 2;
    $("route-plan").textContent = pts.length < 2 ? "Tap start and end" : "Plan along fairways";
  }

  function setAuto(on) {
    route.auto = on;
    route.status = null;
    $("route-auto").hidden = !on;
    $("route-auto-button").setAttribute("aria-expanded", String(on));
    $("route-auto-button").classList.toggle("active", on);
    if (route.on) renderRoutePanel();
  }

  // The route from the first point to the last along the marked fairways, worked out by
  // the add-on from the drawn chart's fairway lines. The points can be edited afterwards.
  async function planAuto() {
    const pts = route.points;
    if (pts.length < 2) return;
    const a = pts[0], b = pts[pts.length - 1];
    route.status = { text: "Planning along the marked fairways…" };
    drawRoute();
    try {
      const res = await api(`api/autoroute?from=${a[0]},${a[1]}&to=${b[0]},${b[1]}&draught=${routeDraught}`);
      if (res.error) {
        route.status = { text: res.error, error: true };
      } else {
        pushUndo(); // undo brings back the start and end
        route.points = res.points;
        setAuto(false);
        const notes = [`Along the marked fairways for ${routeDraught} m draught. Check it on the chart: planning only, not for navigation.`,
          ...(res.warnings || []), res.downloading ? "Some chart areas are still downloading; plan again in a minute for a better route." : ""];
        route.status = { text: notes.filter(Boolean).join(" ") };
        map.fitBounds(L.latLngBounds(res.points), { ...routePadding(), maxZoom: 13 });
      }
    } catch (err) {
      route.status = { text: String(err.message || err), error: true };
    }
    drawRoute();
  }

  function routePadding() {
    const panel = $("route");
    if (window.innerWidth <= 560) return { paddingTopLeft: [30, 110], paddingBottomRight: [30, window.innerHeight - panel.getBoundingClientRect().top + 30] };
    return { paddingTopLeft: [panel.offsetWidth + 40, 80], paddingBottomRight: [40, 40] };
  }

  async function saveRoute() {
    const body = { name: $("route-name").value.trim(), points: route.points, speed: routeSpeed };
    if (route.id) body.id = route.id;
    $("route-save").disabled = true;
    try {
      let saved;
      try {
        saved = await api("api/routes", { method: "POST", body });
      } catch (err) {
        if (!body.id) throw err;
        delete body.id; // deleted elsewhere meanwhile: save it as a new one
        saved = await api("api/routes", { method: "POST", body });
      }
      route.id = saved.id;
      route.name = saved.name;
      $("route-name").value = saved.name;
      route.status = { text: `Saved as "${saved.name}". It's listed under Menu › On the map.` };
      await loadRoutes();
    } catch (err) {
      route.status = { text: String(err.message || err), error: true };
    }
    drawRoute();
  }

  function clearRoute() {
    if (route.points.length) pushUndo();
    route.points = [];
    route.id = null;
    route.name = "";
    route.status = null;
    $("route-name").value = "";
    drawRoute();
    renderSavedRoutes();
  }

  function editRoute(r) {
    route.points = r.points.map((p) => [...p]);
    route.id = r.id;
    route.name = r.name;
    route.undo = [];
    route.status = null;
    routeSpeed = r.speed || routeSpeed;
    if (route.on) {
      $("route-name").value = r.name;
      $("route-speed").value = String(routeSpeed);
      drawRoute();
      renderSavedRoutes();
    } else {
      setRouteMode(true);
    }
    map.fitBounds(L.latLngBounds(r.points), { ...routePadding(), maxZoom: 13 });
  }

  $("route-button").addEventListener("click", () => setRouteMode(!route.on));
  $("route-close").addEventListener("click", () => setRouteMode(false));
  $("route-undo").addEventListener("click", undoRoute);
  $("route-clear").addEventListener("click", clearRoute);
  $("route-save").addEventListener("click", saveRoute);
  $("route-name").addEventListener("keydown", (e) => { if (e.key === "Enter" && route.points.length > 1) saveRoute(); });
  $("route-name").addEventListener("input", (e) => { route.name = e.target.value; });
  $("route-auto-button").addEventListener("click", () => {
    if (route.auto) { setAuto(false); return; }
    setAuto(true);
  });
  $("route-plan").addEventListener("click", planAuto);
  $("route-speed").addEventListener("change", (e) => {
    const v = Number(String(e.target.value).replace(",", "."));
    if (v >= 0.5 && v <= 60) { routeSpeed = Math.round(v * 10) / 10; store.set("fw-route-speed", String(routeSpeed)); }
    e.target.value = String(routeSpeed);
    renderRoutePanel();
  });
  $("route-draught").addEventListener("change", (e) => {
    const v = Number(String(e.target.value).replace(",", "."));
    if (v >= 0 && v <= 15) { routeDraught = Math.round(v * 10) / 10; store.set("fw-route-draught", String(routeDraught)); }
    e.target.value = String(routeDraught);
  });
  map.on("click", (e) => { if (route.on) addRoutePoint(e.latlng); });

  // Saved routes: a dashed line each, with the name at the start; tap one for its numbers.
  async function loadRoutes() {
    savedRoutes = await api("api/routes");
    renderSavedRoutes();
  }

  function renderSavedRoutes() {
    savedLayer.clearLayers();
    for (const r of savedRoutes) {
      if (!r.shown || (route.on && route.id === r.id)) continue;
      const line = L.polyline(r.points, { color: ROUTE_COLOR, weight: 3, opacity: 0.8, dashArray: "8 6", interactive: !route.on });
      if (!route.on) {
        line.on("click", (e) => {
          L.DomEvent.stop(e);
          const mark = markNear(e.latlng); // routes start and end at harbours: those win
          if (mark) mark.fire("click");
          else L.popup({ className: "aid-popup", offset: [0, -2] }).setLatLng(e.latlng).setContent(savedRoutePopup(r)).openOn(map);
        });
      }
      line.addTo(savedLayer);
      // The name halfway along, where it doesn't hide the harbour the route starts from;
      // left out while measuring, to keep the route being drawn clear.
      if (!route.on) {
        L.tooltip({ permanent: true, direction: "top", offset: [0, -4], className: "route-name-label" })
          .setLatLng(halfway(r.points)).setContent(esc(r.name)).addTo(savedLayer);
      }
      for (const p of r.points) {
        L.circleMarker(p, { radius: 3, color: "#fff", weight: 1.5, fillColor: ROUTE_COLOR, fillOpacity: 1, interactive: false }).addTo(savedLayer);
      }
    }
    if (!$("ships-dialog").querySelector('[data-pane="layers"]').hidden) renderRoutesList();
  }

  // A harbour or weather station mark under a tap, if any.
  function markNear(latlng) {
    const pt = map.latLngToContainerPoint(latlng);
    const near = (m) => map.hasLayer(m) && map.latLngToContainerPoint(m.getLatLng()).distanceTo(pt) <= m.options.radius + 6;
    return [...harbourMarks.values()].find(near) || [...stationMarks.values()].find(near) || null;
  }

  function halfway(points) {
    const legs = NavM.legs(points);
    let left = NavM.totalNm(points) / 2;
    for (let i = 0; i < legs.length; i++) {
      if (left <= legs[i].nm && legs[i].nm > 0) {
        const t = left / legs[i].nm, a = points[i], b = points[i + 1];
        return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      }
      left -= legs[i].nm;
    }
    return points[0];
  }

  function savedRoutePopup(r) {
    const el = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = r.name;
    const sub = document.createElement("div");
    sub.className = "muted";
    sub.textContent = routeSummary(r.points, r.speed).text;
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "link-button popup-action";
    edit.textContent = "Edit route";
    edit.addEventListener("click", () => { map.closePopup(); editRoute(r); });
    el.append(title, sub, edit);
    return el;
  }

  function renderRoutesList() {
    const box = $("routes-list");
    if (!savedRoutes.length) {
      const p = document.createElement("p");
      p.className = "small muted";
      p.textContent = "Measure and plan with the ruler button at the top of the map, then Save. Saved routes are listed here.";
      box.replaceChildren(p);
      return;
    }
    const list = document.createElement("ul");
    list.className = "ships-list routes";
    list.replaceChildren(...savedRoutes.map((r) => {
      const li = document.createElement("li");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.className = "switch";
      toggle.checked = r.shown;
      toggle.setAttribute("aria-label", `Show ${r.name} on the map`);
      toggle.addEventListener("change", async () => {
        toggle.disabled = true;
        try {
          await api("api/routes", { method: "POST", body: { id: r.id, shown: toggle.checked } });
          await loadRoutes();
        } catch (err) { console.error(err); } finally { toggle.disabled = false; }
      });
      const grow = document.createElement("div");
      grow.className = "grow clickable";
      const name = document.createElement("span");
      name.textContent = r.name;
      const sub = document.createElement("span");
      sub.className = "small muted";
      sub.textContent = routeSummary(r.points, r.speed).text;
      grow.append(name, sub);
      grow.addEventListener("click", async () => {
        $("ships-dialog").close();
        if (!r.shown) {
          await api("api/routes", { method: "POST", body: { id: r.id, shown: true } });
          await loadRoutes();
        }
        map.fitBounds(L.latLngBounds(r.points), { padding: [40, 80], maxZoom: 13 });
      });
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "Edit";
      edit.addEventListener("click", () => { $("ships-dialog").close(); editRoute(r); });
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete "${r.name}"?`)) return;
        await api(`api/routes/${r.id}`, { method: "DELETE" });
        if (route.id === r.id) route.id = null;
        await loadRoutes();
      });
      li.append(toggle, grow, edit, del);
      return li;
    }));
    box.replaceChildren(list);
  }

  // --- weather stations (FMI), live on every map --------------------------------------------
  // Wind at each coastal and sea station: a disc coloured by strength with the speed in
  // m/s and a pointer showing where the wind blows. Asked for every 5 minutes while the map
  // is open (the add-on reads FMI at most every 10).

  let stationsOn = store.get("fw-stations") !== "0";
  let stationsData = null, stationsTimer = null, stationsLayer = null, stationsCanvas = null;
  const stationMarks = new Map(); // fmisid -> StationMark
  const stationHistory = new Map(); // fmisid -> { at, points }
  const STATIONS_POLL = 5 * 60000;
  const STATIONS_MIN_ZOOM = 6;
  const FMI_CREDIT = 'Weather stations © <a href="https://en.ilmatieteenlaitos.fi/open-data" target="_blank" rel="noopener">FMI</a> (CC BY 4.0)';
  // The wind colours (wind.js), the same as the forecast arrows': under 4 m/s light,
  // orange from 11 (the small craft warning), red from 14 (gale).
  const windColor = FerryWind.color;
  const windLegend = FerryWind.legend;

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
    else ctx.rect(x, y, w, h); // older WebViews
  }

  const StationMark = L.CircleMarker.extend({
    _updatePath() {
      const r = this._renderer;
      if (!r._drawing || this._empty()) return;
      const s = this.options.station, p = this._point, ctx = r._ctx;
      ctx.save();
      ctx.translate(p.x, p.y);
      if (s.ws != null) {
        const color = windColor(s.ws), R = 11;
        if (s.wd != null) { // a pointer on the downwind side
          const a = (((s.wd + 180) % 360) * Math.PI) / 180;
          ctx.save();
          ctx.rotate(a);
          ctx.beginPath();
          ctx.moveTo(0, -R - 8);
          ctx.lineTo(6.5, -R + 3);
          ctx.lineTo(-6.5, -R + 3);
          ctx.closePath();
          ctx.fillStyle = color;
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 2;
          ctx.lineJoin = "round";
          ctx.stroke();
          ctx.fill();
          ctx.restore();
        }
        ctx.beginPath();
        ctx.arc(0, 0, R, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#fff";
        ctx.stroke();
        ctx.fillStyle = "#fff";
        ctx.font = "700 11.5px system-ui, -apple-system, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(Math.round(s.ws)), 0, 0.5);
      } else { // a buoy or mareograph: waves or sea temperature in a small tag
        const text = s.wave != null ? `${s.wave.toFixed(1)} m` : `${Math.round(s.sea)}°`;
        ctx.font = "700 11px system-ui, -apple-system, sans-serif";
        const w = ctx.measureText(text).width + 10;
        roundRect(ctx, -w / 2, -9, w, 18, 9);
        ctx.fillStyle = "#0f766e";
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#fff";
        ctx.stroke();
        ctx.fillStyle = "#fff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(text, 0, 0.5);
      }
      ctx.restore();
    },
  });

  function setStations(on) {
    stationsOn = on;
    store.set("fw-stations", on ? "1" : "0");
    if (on) pollStations();
    else {
      clearTimeout(stationsTimer);
      stationsLayer?.remove();
      map.attributionControl.removeAttribution(FMI_CREDIT);
    }
  }

  async function pollStations() {
    clearTimeout(stationsTimer);
    if (!stationsOn || document.hidden) return;
    stationsTimer = setTimeout(pollStations, STATIONS_POLL);
    try {
      const data = await api("api/stations");
      if (!stationsOn) return;
      stationsData = data;
      drawStations();
    } catch (err) {
      console.error(err);
    }
  }

  function drawStations() {
    if (!stationsLayer) {
      stationsCanvas = sharedCanvas();
      stationsLayer = L.layerGroup();
    }
    const seen = new Set();
    for (const s of stationsData?.stations || []) {
      seen.add(s.id);
      let mark = stationMarks.get(s.id);
      if (!mark) {
        mark = new StationMark([s.lat, s.lon], { renderer: stationsCanvas, radius: 13, stroke: false, fill: false,
          bubblingMouseEvents: false, station: s });
        mark.on("click", () => (route.on ? addRoutePoint(mark.getLatLng()) : openStation(mark)));
        stationMarks.set(s.id, mark);
      } else {
        mark.options.station = s;
        if (mark.isPopupOpen()) fillStationPopup(mark);
      }
    }
    for (const [id, mark] of stationMarks) {
      if (seen.has(id)) continue;
      mark.remove();
      stationMarks.delete(id);
    }
    if (!map.hasLayer(stationsLayer)) {
      stationsLayer.addTo(map);
      map.attributionControl.addAttribution(FMI_CREDIT);
    }
    declutterStations();
  }

  // Zoomed out, stations closer than a marker's width give way to their neighbours.
  function declutterStations() {
    if (!stationsLayer || !map.hasLayer(stationsLayer)) return;
    const zoom = map.getZoom();
    const kept = [];
    const marks = [...stationMarks.values()].sort((a, b) => (b.options.station.ws ?? -1) - (a.options.station.ws ?? -1));
    for (const mark of marks) {
      const pt = map.project(mark.getLatLng(), zoom);
      const show = zoom >= STATIONS_MIN_ZOOM && kept.every((q) => q.distanceTo(pt) >= 30);
      if (show) kept.push(pt);
      if (show && !stationsLayer.hasLayer(mark)) mark.addTo(stationsLayer);
      else if (!show && stationsLayer.hasLayer(mark) && !mark.isPopupOpen()) mark.remove();
    }
  }
  map.on("zoomend", declutterStations);

  function openStation(mark) {
    if (!mark.getPopup()) {
      const el = document.createElement("div");
      el.className = "stn";
      el.innerHTML = `<div class="stn-head"><strong class="stn-name"></strong><span class="stn-age small muted"></span></div>
        <div class="stn-wind"><svg class="stn-arrow" viewBox="-8 -8 16 16" aria-hidden="true"><path d="M0 -6 L4 5 L0 2.5 L-4 5 Z"/></svg>
          <span class="stn-speed"></span><span class="stn-gust"></span></div>
        <div class="stn-facts small"></div><div class="stn-spark"></div>
        <div class="stn-foot small muted">FMI open data (CC BY 4.0)</div>`;
      mark.bindPopup(el, { className: "fw-popup", minWidth: 250, maxWidth: 270, offset: [0, -10], autoPanPaddingTopLeft: [12, 110] });
    }
    fillStationPopup(mark);
    mark.openPopup();
    loadStationHistory(mark);
  }

  function fillStationPopup(mark) {
    const s = mark.options.station, el = mark.getPopup()?.getContent();
    if (!el) return;
    el.querySelector(".stn-name").textContent = s.name;
    el.querySelector(".stn-age").textContent = `Updated ${ago(s.ts)}`;
    const wind = el.querySelector(".stn-wind");
    wind.hidden = s.ws == null;
    if (s.ws != null) {
      const arrow = el.querySelector(".stn-arrow");
      arrow.style.fill = windColor(s.ws);
      arrow.style.transform = `rotate(${((s.wd ?? 0) + 180) % 360}deg)`;
      arrow.style.visibility = s.wd == null ? "hidden" : "";
      el.querySelector(".stn-speed").innerHTML = `<strong>${s.ws.toFixed(1)}</strong> m/s`;
      el.querySelector(".stn-gust").textContent = [s.wg != null ? `gusts ${s.wg.toFixed(1)}` : "",
        s.wd != null ? `from ${compass(s.wd)} ${NavM.degrees(s.wd)}` : ""].filter(Boolean).join(" · ");
    }
    const facts = [];
    if (s.t != null) facts.push(`Air ${s.t.toFixed(1)} °C`);
    if (s.sea != null) facts.push(`Sea ${s.sea.toFixed(1)} °C`);
    if (s.wave != null) facts.push(`Waves ${s.wave.toFixed(1)} m`);
    el.querySelector(".stn-facts").textContent = facts.join(" · ")
      + (s.sea_from && s.sea_from !== s.name ? ` (${s.sea_from})` : "");
  }

  async function loadStationHistory(mark) {
    const s = mark.options.station, el = mark.getPopup().getContent();
    const box = el.querySelector(".stn-spark");
    if (s.ws == null) { box.replaceChildren(); return; }
    let hist = stationHistory.get(s.id);
    if (!hist || Date.now() - hist.at > 10 * 60000) {
      if (!hist) box.innerHTML = '<span class="small muted">Loading the past 24 hours…</span>';
      try {
        hist = { at: Date.now(), points: (await api(`api/stations/${s.id}/history`)).points };
        stationHistory.set(s.id, hist);
        if (stationHistory.size > 20) stationHistory.delete(stationHistory.keys().next().value);
      } catch (err) {
        box.innerHTML = '<span class="small muted">The past 24 hours are not available just now.</span>';
        return;
      }
    }
    box.innerHTML = sparkline(hist.points);
    mark.getPopup().update();
  }

  // Wind (line) and gusts (shaded) over the past 24 hours.
  function sparkline(points) {
    const pts = points.filter((p) => p[1] != null || p[2] != null);
    if (pts.length < 2) return '<span class="small muted">No wind history for this station.</span>';
    const W = 250, H = 64, T = 6, B = 14;
    const t1 = pts[pts.length - 1][0], t0 = t1 - 24 * 3600;
    const top = Math.max(5, Math.ceil(Math.max(...pts.map((p) => Math.max(p[1] ?? 0, p[2] ?? 0))) / 5) * 5);
    const x = (t) => Math.max(0, ((t - t0) / (t1 - t0)) * W);
    const y = (v) => T + (1 - v / top) * (H - T - B);
    const path = (i) => pts.filter((p) => p[i] != null).map((p, k) => `${k ? "L" : "M"}${x(p[0]).toFixed(1)} ${y(p[i]).toFixed(1)}`).join("");
    const gusts = pts.filter((p) => p[2] != null);
    const area = gusts.length > 1 ? `${path(2)}L${x(gusts[gusts.length - 1][0]).toFixed(1)} ${y(0)}L${x(gusts[0][0]).toFixed(1)} ${y(0)}Z` : "";
    const last = pts[pts.length - 1];
    const peak = Math.max(...gusts.map((p) => p[2]), 0);
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Wind and gusts over the past 24 hours">
      ${[top / 2, top].map((v) => `<line class="grid" x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="tick" x="2" y="${y(v) + 9}">${v}</text>`).join("")}
      ${area ? `<path class="gust" d="${area}"/>` : ""}
      <path class="wind" d="${path(1)}"/>
      ${last[1] != null ? `<circle class="now" cx="${x(last[0])}" cy="${y(last[1])}" r="2.5"/>` : ""}
      <line class="axis" x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>
      <text class="tick" x="0" y="${H - 2}">24 h ago</text><text class="tick end" x="${W}" y="${H - 2}">now</text>
    </svg><div class="stn-legend small muted"><span><i class="wind"></i>wind</span><span><i class="gust"></i>gusts${peak ? `, top ${peak.toFixed(0)} m/s` : ""}</span></div>`;
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(stationsTimer);
    else if (stationsOn) pollStations();
  });

  // --- harbours (OpenStreetMap), from zoom 11 ------------------------------------------------
  // On the chart: the chart's red sailboat; on the map and satellite views: a blue tag.

  const HARBOUR_RED = "#d0021b";
  const HARBOUR_MIN_ZOOM = 11;
  const OSM_CREDIT = 'Harbours © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a> (ODbL)';
  const harbourMarks = new Map(); // osm id -> HarbourMark
  let harbourCanvas = null, harbourBox = null, harbourTimer = null, harbourMoveTimer = null, harbourBusy = false;
  let harbourCount = 0, harbourNote = "";
  const SERVICE_INFO = {
    guest: ["Guest berths", '<path d="M12 3v14M8 7h8M5 13a7 7 0 0 0 14 0M5 13H3M19 13h2"/><circle cx="12" cy="4" r="1.5"/>'],
    fuel: ["Fuel", '<path d="M5 20V5a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v15M3.5 20h11M5 10h8M13 9l3 2.5V17a1.5 1.5 0 0 0 3 0V8.5L16 6"/>'],
    toilets: ["Toilets", '<circle cx="7.5" cy="5" r="1.6"/><circle cx="16.5" cy="5" r="1.6"/><path d="M7.5 8v12M5 9h5l-.5 6M14.5 9h4l2 7h-8zM15.5 16v4M17.5 16v4M12 3v18"/>'],
    shower: ["Shower", '<path d="M4 20V8a4 4 0 0 1 4-4h2a4 4 0 0 1 4 4M10 8h8M11 12v1M14 12v1M17 12v1M12.5 15.5v1M15.5 15.5v1M14 19v1"/>'],
    sauna: ["Sauna", '<path d="M8 3c-1.5 1.8 1.5 3 0 5M12 3c-1.5 1.8 1.5 3 0 5M16 3c-1.5 1.8 1.5 3 0 5M3 12h18M5 12v8M19 12v8M5 16h14"/>'],
    electricity: ["Electricity", '<path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z"/>'],
    water: ["Water", '<path d="M12 3c3 4 6 7.2 6 11a6 6 0 0 1-12 0c0-3.8 3-7 6-11z"/>'],
    waste: ["Waste", '<path d="M4 7h16M9.5 7V4h5v3M6.5 7l1 13h9l1-13M10 11v5M14 11v5"/>'],
    slipway: ["Slipway", '<path d="M2 19h20M3 19l15-9M15 5l3 5-3 2M6 21h0"/>'],
    restaurant: ["Restaurant", '<path d="M7 3v7M5 3v5a2 2 0 0 0 4 0V3M7 10v11M17 21V3c-2.2 1-3.5 4-3.5 8h3.5"/>'],
  };

  // A sailboat on the chart, as on Finnish paper charts; a blue tag elsewhere.
  function boatPath(ctx, s) {
    ctx.beginPath();
    ctx.moveTo(-0.5 * s, -7 * s); ctx.lineTo(-0.5 * s, 2.5 * s); ctx.lineTo(-6 * s, 2.5 * s); ctx.closePath(); // mainsail
    ctx.moveTo(0.8 * s, -5 * s); ctx.lineTo(0.8 * s, 2.5 * s); ctx.lineTo(5 * s, 2.5 * s); ctx.closePath(); // jib
    ctx.moveTo(-7 * s, 4 * s); ctx.lineTo(7 * s, 4 * s); ctx.lineTo(5 * s, 7 * s); ctx.lineTo(-5 * s, 7 * s); ctx.closePath(); // hull
  }

  const HarbourMark = L.CircleMarker.extend({
    _updatePath() {
      const r = this._renderer;
      if (!r._drawing || this._empty()) return;
      const p = this._point, ctx = r._ctx, o = this.options;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.lineJoin = "round";
      if (o.chart) {
        boatPath(ctx, 1.15);
        ctx.strokeStyle = "rgba(255,255,255,0.95)";
        ctx.lineWidth = 3.5;
        ctx.stroke();
        ctx.fillStyle = HARBOUR_RED;
        ctx.fill();
      } else {
        roundRect(ctx, -10.5, -10.5, 21, 21, 6);
        ctx.fillStyle = "#1d4ed8";
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#fff";
        ctx.stroke();
        ctx.translate(0, -1);
        boatPath(ctx, 0.95);
        ctx.fillStyle = "#fff";
        ctx.fill();
        ctx.translate(0, 1);
      }
      if (o.label) { // names when zoomed in
        ctx.font = "600 12px system-ui, -apple-system, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.lineWidth = 3;
        ctx.strokeStyle = o.dark ? "rgba(0,0,0,0.75)" : "rgba(255,255,255,0.9)";
        ctx.fillStyle = o.dark ? "#fff" : (o.chart ? HARBOUR_RED : "#1e3a8a");
        ctx.strokeText(o.label, 14, 0);
        ctx.fillText(o.label, 14, 0);
      }
      ctx.restore();
    },
  });

  function harbourStyle() {
    const zoom = map.getZoom();
    return { chart: activeLayer === layers.chart, dark: activeLayer === layers.satellite, labels: zoom >= 13 };
  }

  function restyleHarbours() {
    if (!harbourLayer) return;
    const st = harbourStyle();
    for (const mark of harbourMarks.values()) {
      mark.options.chart = st.chart;
      mark.options.dark = st.dark;
      mark.options.label = st.labels ? mark.row[3] : "";
    }
    if (harbourCanvas?._map) harbourCanvas._redraw();
  }

  function setHarbours(on) {
    harboursOn = on;
    store.set("fw-harbours", on ? "1" : "0");
    harbourBox = null;
    loadHarbours();
  }

  async function loadHarbours() {
    clearTimeout(harbourTimer);
    const visible = harboursOn && map.getZoom() >= HARBOUR_MIN_ZOOM;
    if (!visible) {
      if (harbourLayer && map.hasLayer(harbourLayer)) {
        harbourLayer.remove();
        map.attributionControl.removeAttribution(OSM_CREDIT);
      }
      harbourCount = 0;
      return;
    }
    const view = map.getBounds();
    if (harbourBox?.contains(view) || harbourBusy || document.hidden) {
      showHarbourLayer();
      return;
    }
    let box = view.pad(0.5);
    if ((box.getNorth() - box.getSouth()) * (box.getEast() - box.getWest()) > 3.5) box = view;
    harbourBusy = true;
    try {
      const f = (v) => v.toFixed(4);
      const data = await api(`api/harbours?bbox=${f(box.getWest())},${f(box.getSouth())},${f(box.getEast())},${f(box.getNorth())}`);
      harbourNote = data.pending ? "Downloading the harbours from OpenStreetMap…"
        : data.error && !data.harbours.length ? `Couldn't reach OpenStreetMap just now (${data.error}).` : "";
      if (data.pending) harbourTimer = setTimeout(loadHarbours, 5000);
      else harbourBox = box;
      if (harboursOn) drawHarbours(data.harbours);
    } catch (err) {
      console.error(err);
    } finally {
      harbourBusy = false;
    }
  }

  function showHarbourLayer() {
    if (harbourLayer && !map.hasLayer(harbourLayer)) {
      harbourLayer.addTo(map);
      map.attributionControl.addAttribution(OSM_CREDIT);
    }
  }

  function drawHarbours(rows) {
    if (!harbourLayer) {
      harbourCanvas = sharedCanvas();
      harbourLayer = L.layerGroup();
    }
    const st = harbourStyle();
    const seen = new Set();
    for (const row of rows) {
      seen.add(row[0]);
      let mark = harbourMarks.get(row[0]);
      if (!mark) {
        mark = new HarbourMark([row[1], row[2]], { renderer: harbourCanvas, radius: 11, stroke: false, fill: false,
          bubblingMouseEvents: false });
        mark.on("click", () => (route.on ? addRoutePoint(mark.getLatLng()) : openHarbour(mark)));
        mark.addTo(harbourLayer);
        harbourMarks.set(row[0], mark);
      }
      mark.row = row;
      Object.assign(mark.options, { chart: st.chart, dark: st.dark, label: st.labels ? row[3] : "" });
    }
    // Keep at most a few hundred: forget those far out of view.
    if (harbourMarks.size > 800) {
      for (const [id, mark] of harbourMarks) {
        if (seen.has(id) || mark.isPopupOpen()) continue;
        mark.remove();
        harbourMarks.delete(id);
      }
    }
    harbourCount = [...harbourMarks.values()].filter((m) => map.getBounds().contains(m.getLatLng())).length;
    showHarbourLayer();
    if (harbourCanvas._map) harbourCanvas._redraw();
  }

  function openHarbour(mark) {
    const [, , , name, kind, services, depth, draught, website, phone] = mark.row;
    const el = document.createElement("div");
    el.className = "hb";
    const title = document.createElement("strong");
    title.className = "hb-name";
    title.textContent = name || kind;
    const sub = document.createElement("div");
    sub.className = "small muted";
    sub.textContent = [name ? kind : "", depth != null ? `depth ${depth} m` : "", draught != null ? `max draught ${draught} m` : ""]
      .filter(Boolean).join(" · ");
    el.append(title, sub);
    if (services.length) {
      const chips = document.createElement("ul");
      chips.className = "hb-chips";
      for (const key of services) {
        const [label, icon] = SERVICE_INFO[key] || [key, ""];
        const li = document.createElement("li");
        li.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${icon}</svg>`;
        li.append(label);
        chips.append(li);
      }
      el.append(chips);
    } else {
      const none = document.createElement("div");
      none.className = "small muted hb-none";
      none.textContent = "No services marked in OpenStreetMap.";
      el.append(none);
    }
    const links = [];
    if (website && /^https?:\/\//.test(website)) {
      const a = document.createElement("a");
      a.href = website;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "");
      links.push(a);
    }
    if (phone) {
      const a = document.createElement("a");
      a.href = `tel:${phone.replace(/[^+\d]/g, "")}`;
      a.textContent = phone;
      links.push(a);
    }
    if (links.length) {
      const row = document.createElement("div");
      row.className = "hb-links small";
      links.forEach((a, i) => row.append(...(i ? [" · ", a] : [a])));
      el.append(row);
    }
    const foot = document.createElement("div");
    foot.className = "small muted hb-foot";
    foot.innerHTML = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a> (ODbL)';
    el.append(foot);
    L.popup({ className: "fw-popup", minWidth: 230, maxWidth: 280, offset: [0, -8], autoPanPaddingTopLeft: [12, 110] })
      .setLatLng(mark.getLatLng()).setContent(el).openOn(map);
  }

  map.on("moveend", () => {
    clearTimeout(harbourMoveTimer);
    harbourMoveTimer = setTimeout(loadHarbours, 300);
  });
  map.on("zoomend", restyleHarbours);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadHarbours(); });

  // --- keyboard and small things ------------------------------------------------------

  let popupOpen = false;
  map.on("popupopen", () => { popupOpen = true; });
  map.on("popupclose", () => { popupOpen = false; });

  // Escape backs out of one thing at a time: a route field being typed in, drawing, a popup,
  // the aircraft history, the sheet, a playback, the route tool, the ship card, a replay.
  document.addEventListener("keydown", (e) => {
    if ($("ships-dialog").open) return;
    const typing = e.target instanceof HTMLInputElement;
    if (route.on && !typing && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      undoRoute();
      return;
    }
    if (e.key !== "Escape") return;
    if (typing && e.target.closest("#route")) e.target.blur();
    else if (drawing) cancelDrawing();
    else if (popupOpen) map.closePopup();
    else if (historyView.isOpen()) historyView.escape();
    else if (!$("conditions-sheet").hidden) toggleConditions(false);
    else if (player) stopPlayback();
    else if (route.on) setRouteMode(false);
    else if (focused) unfocus();
    else if (replayLayer) clearReplay();
    else if (editing) setEditing(false);
  });

  // On a phone the attribution is one line; tap it to read it all.
  const attribution = map.attributionControl.getContainer();
  attribution.title = "Map data credits";
  attribution.addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    document.body.classList.toggle("attribution-open", attribution.classList.toggle("expanded"));
  });

  // --- start --------------------------------------------------------------------

  function shipFromUrl() {
    const params = new URLSearchParams(location.search);
    const hash = new URLSearchParams(location.hash.slice(1));
    const path = location.pathname.match(/\/ship\/(\d+)\/?$/);
    const value = hash.get("ship") || params.get("ship") || (path && path[1]);
    return value ? Number(value) : null;
  }

  (async () => {
    try {
      await loadState();
      await loadTails();
    } catch (err) {
      console.error(err);
    }
    const target = shipFromUrl();
    if (target && ships.has(target)) focusShip(target);
    else if (!focusFromNotification()) fitAllShips();
    connectSocket();
    if (trafficOn) pollTraffic();
    if (stationsOn) pollStations();
    loadHarbours();
    loadRoutes().catch(console.error);
    if (route.points.length && store.get("fw-route-open") === "1") setRouteMode(true);
  })();
})();
