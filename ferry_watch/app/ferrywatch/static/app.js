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
  // Nature reserves (static/reserves.js): made once the map is up, see below.
  let reserves = null;
  let notices = null;
  let logbook = null;
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
  // Open where the map was last left if that was in the last 6 hours (else on the ships).
  let viewRestored = false;
  try {
    const v = (store.get("fw-view") || "").split(",").map(Number);
    if (v.length >= 3 && v.slice(0, 3).every(Number.isFinite) && !(v[3] && Date.now() - v[3] > 6 * 3600e3)) {
      map.setView([v[0], v[1]], v[2], { animate: false });
      viewRestored = true;
    }
  } catch (err) { /* the default view */ }
  map.on("moveend", () => {
    const c = map.getCenter();
    store.set("fw-view", `${c.lat.toFixed(5)},${c.lng.toFixed(5)},${map.getZoom()},${Date.now()}`);
  });
  // A scale bar in nautical miles and metres.
  const ScaleBar = L.Control.extend({
    options: { position: "bottomright" },
    onAdd() {
      this.el = L.DomUtil.create("div", "nm-scale");
      this.el.innerHTML = '<div class="nm-row"><i></i><span></span></div><div class="nm-row km"><i></i><span></span></div>';
      map.on("zoomend moveend", this.update, this);
      setTimeout(() => this.update(), 0);
      return this.el;
    },
    update() {
      const y = map.getSize().y / 2;
      const m = map.distance(map.containerPointToLatLng([0, y]), map.containerPointToLatLng([100, y])); // per 100 px
      const nice = (v) => { const e = 10 ** Math.floor(Math.log10(v)); const f = v / e; return (f >= 5 ? 5 : f >= 2 ? 2 : 1) * e; };
      const nm = nice(m / 1852);
      const metres = nice(m);
      const [rowNm, rowKm] = this.el.querySelectorAll(".nm-row");
      rowNm.querySelector("i").style.width = `${Math.round((nm * 1852 / m) * 100)}px`;
      rowNm.querySelector("span").textContent = `${nm < 1 ? nm.toFixed(nm < 0.1 ? 2 : 1) : nm} NM`;
      rowKm.querySelector("i").style.width = `${Math.round((metres / m) * 100)}px`;
      rowKm.querySelector("span").textContent = metres >= 1000 ? `${metres / 1000} km` : `${metres} m`;
    },
  });
  new ScaleBar().addTo(map);
  map.attributionControl.setPrefix(false);

  // Weather layers (overlays.js): ice, radar and wind over the map, with a shared time bar.
  // Made here, before anything can ask for the menu's rows or Settings.
  const overlays = FerryOverlays.create({
    map, store, switchRow, canvas: sharedCanvas, strip: $("strip"),
    // A tap shows the weather there, unless it was for the route tool or a chart mark.
    canTap: (latlng) => !route.on && !(activeLayer === layers.chart && chartStyle === "drawn" && map.getZoom() >= 11
      && FerryChart.aidAt(map, latlng)),
    onChange() {
      renderWeather();
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

  // Aerial photos where a country gives them away (Finland 0.5 m, Estonia), fetched and
  // kept by the add-on, over Esri's imagery. Off unless switched on in Layers: ben
  // prefers Esri's look. Elsewhere, and where they have no picture, Esri shows through.
  let aerialOn = store.get("fw-aerial") === "1";
  const aerialPhotos = L.tileLayer("api/tiles/photo/{z}/{x}/{y}.jpg", {
    minZoom: 11, maxZoom: 19, maxNativeZoom: 18, zIndex: 2,
    attribution: "Aerial photos © Maanmittauslaitos (via Kapsi), Maa-amet",
  });

  // Esri's imagery stops at different zooms in different places (often 17 or 18 here);
  // past that it sends grey "Map data not yet available" tiles. So the deepest zoom it
  // really has around the view is asked from its tile map, and closer in the last real
  // picture is enlarged instead.
  const ESRI_SAFE_Z = 17;
  const esriImagery = L.tileLayer(`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, {
    maxZoom: 19, maxNativeZoom: ESRI_SAFE_Z, referrerPolicy: REFERRER, zIndex: 1,
    attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
  });
  const esriHas = new Map(); // "z/x/y" -> Promise<boolean>
  function esriTileExists(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (!esriHas.has(key)) {
      esriHas.set(key, fetch(`${ESRI}/World_Imagery/MapServer/tilemap/${z}/${y}/${x}/1/1?f=json`)
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => Array.isArray(j?.data) && j.data[0] === 1)
        .catch(() => false));
      if (esriHas.size > 500) esriHas.delete(esriHas.keys().next().value);
    }
    return esriHas.get(key);
  }
  async function fitEsriZoom() {
    if (activeLayer !== layers.satellite || map.getZoom() < 15) return;
    const c = map.getCenter();
    let best = ESRI_SAFE_Z;
    for (const z of [19, 18]) {
      const p = map.project(c, z).divideBy(256).floor();
      if (await esriTileExists(z, p.x, p.y)) { best = z; break; }
    }
    if (best === esriImagery.options.maxNativeZoom) return;
    esriImagery.options.maxNativeZoom = best;
    if (layers.satellite.hasLayer(esriImagery)) {  // re-add so Leaflet picks the new zoom
      layers.satellite.removeLayer(esriImagery);
      layers.satellite.addLayer(esriImagery);
    }
  }

  const layers = {
    map: streets(),
    satellite: L.layerGroup([
      esriImagery,
      L.tileLayer(`${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`, {
        maxZoom: 19, referrerPolicy: REFERRER, zIndex: 3,
      }),
    ]),
    chart: null, // built by buildChartLayer below
    // The Land Survey's terrain map (maastokartta: contours, paths, cliffs, marshes, every
    // building), fetched and kept by the add-on, over the plain map, which shows outside
    // Finland. It stops at zoom 16; closer in the last picture is enlarged.
    terrain: L.layerGroup([
      streets(),
      L.tileLayer("api/tiles/topo/{z}/{x}/{y}.png", {
        minZoom: 5, maxZoom: 19, maxNativeZoom: 16, zIndex: 2,
        attribution: "Terrain map © Maanmittauslaitos (CC BY 4.0)",
      }),
    ]),
  };
  if (aerialOn) layers.satellite.addLayer(aerialPhotos);
  // Then & now: old aerial photos and old maps over today's picture, plus the catalogue's
  // overlays and building ages (histmaps.js, mapsources.py).
  const hist = FerryHist.create({
    map, api, store,
    today: {
      photo: () => L.layerGroup([
        L.tileLayer(`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, {
          maxZoom: 19, maxNativeZoom: ESRI_SAFE_Z, referrerPolicy: REFERRER, zIndex: 1,
          attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
        }),
        L.tileLayer("api/tiles/photo/{z}/{x}/{y}.jpg", {
          minZoom: 11, maxZoom: 19, maxNativeZoom: 18, zIndex: 2,
          attribution: "Aerial photos © Maanmittauslaitos (via Kapsi)",
        }),
      ]),
      map: streets,
    },
    onChange: () => {
      if (!$("ships-dialog").querySelector('[data-pane="layers"]').hidden) renderLayers();
    },
  });
  layers.historic = hist.layer;
  // Where am I, What's here (long press), Find a place, My pins (whereami.js, here.py, pins.py).
  const here = FerryHere.create({
    map, api, store,
    phoneFix: () => phoneFix(),
    // A long press on the drawn chart saves a chart sample instead, when that's switched on.
    busy: () => sampleOnPress && activeLayer === layers.chart && chartStyle === "drawn",
    onChange: () => {
      if (!$("ships-dialog").querySelector('[data-pane="layers"]').hidden) renderLayers();
    },
  });
  FerryHist.compactPicker(document.querySelector('.segmented[aria-label="Map type"]'));
  function setAerial(on) {
    aerialOn = on;
    store.set("fw-aerial", on ? "1" : "0");
    if (on) layers.satellite.addLayer(aerialPhotos);
    else layers.satellite.removeLayer(aerialPhotos);
  }
  let activeLayer = null;

  // "drawn": the add-on's own seamless chart (vchart.js), at every zoom. "official": Traficom's electronic chart (S-57 view service), drawn
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
      // The drawn chart first, so its credit leads the line at the bottom. Drawn at every
      // zoom (zoomed far out from Natural Earth's land and lakes where the chart has none
      // yet), with no scanned chart under it.
      return L.layerGroup([
        FerryChart.layer({
          attribution: "Chart © Traficom, Väylävirasto, Maanmittauslaitos · base map: Natural Earth · abroad: depths © "
            + "EMODnet Bathymetry, coast and sea marks © OpenStreetMap contributors · not for navigation",
          zIndex: 3,
        }),
        oceanBase(),
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
    fitEsriZoom();
    renderChartStyle();
    restyleHarbours();
  }

  document.querySelectorAll("[data-layer]").forEach((b) => {
    b.addEventListener("click", () => setLayer(b.dataset.layer));
  });
  map.on("moveend", fitEsriZoom);
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
  // One date style everywhere: "Thu 30 Oct", "Thu 30 Oct, 07:34" (24-hour).
  const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDay = (ts) => {
    const d = new Date(ts * 1000);
    return `${DAY_NAMES[(d.getDay() + 6) % 7]} ${d.getDate()} ${MONTH_NAMES[d.getMonth()]}`;
  };
  const fmtWhen = (ts) => `${fmtDay(ts)}, ${clock(ts)}`;

  function ago(ts) {
    const s = Math.max(0, Math.round(now() - ts));
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min ago`;
    return fmtWhen(ts);
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
      const day = sameDay ? "" : `${DAY_NAMES[(d.getDay() + 6) % 7]} `;
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
        voyage = `Track since ${fmtWhen(info.voyage_start)}`;
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
    if ($("card-links").dataset.mmsi !== String(info.mmsi)) {
      $("card-links").dataset.mmsi = info.mmsi;
      $("card-links").replaceChildren(...shipLinks(info.mmsi));
    }
    $("card-follow").hidden = !info.fleet;
    $("card-unfollow").hidden = info.fleet !== "followed";
    // My ships: their alerts bell by the name, and a way to their stats.
    $("card-stats-link").hidden = Boolean(info.fleet);
    const bell = $("card-bell").firstChild;
    if (info.fleet) $("card-bell").replaceChildren();
    else if (!bell?.disabled) $("card-bell").replaceChildren(bellButton(info, () => { renderShipsList(); renderAlertBox(); }));
    $("card-play").hidden = (focused.points || []).length < 2;
    $("peek-play").hidden = (focused.points || []).length < 2;
    renderCardStats(info);
    renderPortTimes(info);
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
      credit.textContent = `Photo: ${photo.source || "Wikipedia"}`;
    } else {
      $("card-img").removeAttribute("src");
      box.classList.add("empty");
    }
  }

  // --- port times: when the ship left, is due and leaves (Portnet, AIS, its own history) ---
  const portTimes = new Map(); // mmsi -> {data, at, key}
  const PORT_SRC = { portnet: "port", usual: "usual", ais_eta: "AIS", ais: "" };
  const PORT_SRC_TITLE = {
    portnet: "Announced by the ship's agent to the Finnish port (Portnet, via Digitraffic)",
    usual: "From this ship's usual crossing time and stay, out of its earlier trips",
    ais_eta: "The crew's own ETA, sent over AIS",
  };

  function loadPortTimes(mmsi, key) {
    const have = portTimes.get(mmsi);
    if (have?.loading || (have && have.key === key && Date.now() - have.at < 60000)) return have?.data;
    portTimes.set(mmsi, { ...have, loading: true });
    fetch(`api/ports/${mmsi}`).then((r) => (r.ok ? r.json() : null))
      .then((data) => { portTimes.set(mmsi, { data, at: Date.now(), key }); if (focused?.mmsi === mmsi) renderCard(); })
      .catch(() => portTimes.set(mmsi, { ...have, loading: false, at: Date.now(), key }));
    return have?.data;
  }

  function portClock(t, verb) {
    const el = document.createElement("span");
    el.className = "port-time";
    const d = new Date(t.ts * 1000);
    const sameDay = d.toDateString() === new Date(now() * 1000).toDateString();
    const day = sameDay ? "" : `${DAY_NAMES[(d.getDay() + 6) % 7]} `;
    el.textContent = `${verb} ${t.actual ? "" : "≈"}${day}${clock(t.ts)}`;
    if (!t.actual) el.classList.add("expected");
    el.title = t.actual ? (t.src === "portnet" ? "Reported to the port" : "Seen from its AIS positions") : PORT_SRC_TITLE[t.src] || "";
    return el;
  }

  // One port: "Mariehamn | due ≈23:53 · leaves ≈00:08 port" (the tag says where expected times come from).
  function portRowCells(r, t = now()) {
    const name = document.createElement("span");
    name.className = "port-name";
    name.textContent = r.port;
    const times = document.createElement("span");
    times.className = "port-times";
    const parts = [];
    // A port it has already left: only when it left (the arrival is old news).
    if (r.arrive && !(r.arrive.actual && r.leave?.actual)) parts.push(portClock(r.arrive, r.arrive.actual ? "arrived" : "due"));
    if (r.leave) parts.push(portClock(r.leave, r.leave.actual ? "left" : "leaves"));
    parts.forEach((el, i) => { if (i) times.append(" · "); times.append(el); });
    if (!parts.length) times.append("next port, no time yet");
    const src = [r.arrive, r.leave].find((x) => x && !x.actual && PORT_SRC[x.src])?.src;
    const extra = [src && PORT_SRC[src], r.berth && r.berth.toLowerCase() !== r.port.toLowerCase() ? titleCase(r.berth) : ""].filter(Boolean);
    if (extra.length) {
      const tag = document.createElement("small");
      tag.textContent = ` ${extra.join(" · ")}`;
      if (src) tag.title = PORT_SRC_TITLE[src];
      times.append(tag);
    }
    if (r.usual_trip && !r.arrive?.actual) times.title = `Usual crossing ${duration(r.usual_trip)}`;
    return [name, times];
  }

  function renderPortTimes(info) {
    const box = $("card-ports");
    const data = loadPortTimes(info.mmsi, `${info.in_port}|${info.voyage_start}`);
    const rows = data?.rows || [];
    box.hidden = !rows.length;
    if (!rows.length) { box.replaceChildren(); return; }
    const head = document.createElement("div");
    head.className = "port-head small muted";
    head.textContent = "Port times";
    box.replaceChildren(head, ...rows.flatMap((r) => portRowCells(r)));
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
    const mine = statsData.passages.filter(([, mmsi]) => mmsi === info.mmsi).map(([ts, , route, direction, , , , , , porkkala]) => {
      const d = new Date(ts * 1000), at = new Date((porkkala || ts) * 1000);
      return { ts, route, direction, year: d.getFullYear(), month: d.getMonth(), minute: at.getHours() * 60 + at.getMinutes() };
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
  // Stats counting only this ship.
  $("card-stats-link").addEventListener("click", () => {
    const info = focused && ships.get(focused.mmsi)?.info;
    if (!info) return;
    const pool = statsData ? statsData.ships.map((s) => s.mmsi) : [...ships.values()].filter(({ info: i }) => !i.fleet).map(({ info: i }) => i.mmsi);
    const others = pool.filter((m) => m !== info.mmsi);
    statsHidden.clear();
    for (const m of others) statsHidden.add(m);
    store.set("fw-stats-hidden", JSON.stringify(others));
    statsPick = null;
    statsDay = null;
    if (!menu.open) openMenu("stats");
    else setTab("stats");
  });
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
    renderWeather();
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

  // My ships: tap one to show it; the bell turns its alerts on and off; ⋯ holds Remove.
  const BELL = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/></svg>';
  const BELL_OFF = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/><path d="M4 4l16 16"/></svg>';
  let shipMenuOpen = null; // the ship whose ⋯ is open

  function bellButton(info, onDone) {
    const bell = document.createElement("button");
    bell.type = "button";
    bell.className = "icon-button plain bell-button";
    const paint = () => {
      const on = info.alerts !== false;
      bell.innerHTML = on ? BELL : BELL_OFF;
      bell.setAttribute("aria-pressed", String(on));
      bell.setAttribute("aria-label", on ? `Alerts on for ${info.name}: tap to turn off` : `Alerts off for ${info.name}: tap to turn on`);
      bell.title = on ? "Alerts on: a phone alert when it takes the inner route" : "Alerts off";
    };
    paint();
    bell.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      bell.disabled = true;
      try {
        const on = info.alerts === false;
        await api(`api/ships/${info.mmsi}/alerts`, { method: "POST", body: { enabled: on } });
        info.alerts = on;
        paint();
        onDone?.();
      } finally {
        bell.disabled = false;
      }
    });
    return bell;
  }

  function renderShipsList() {
    const list = $("ships-list");
    list.replaceChildren(...[...ships.values()].filter(({ info }) => !info.fleet).map(({ info }) => {
      const li = document.createElement("li");
      li.className = "clickable";
      li.title = `Show ${info.name} on the map`;
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = info.color;
      const grow = document.createElement("div");
      grow.className = "grow";
      const name = document.createElement("span");
      name.textContent = info.name;
      const sub = document.createElement("span");
      sub.className = "muted small";
      sub.textContent = chipText(info);
      grow.append(name, sub);
      const more = document.createElement("button");
      more.type = "button";
      more.className = "icon-button plain";
      more.setAttribute("aria-label", `More for ${info.name}`);
      more.setAttribute("aria-expanded", String(shipMenuOpen === info.mmsi));
      more.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle class="fill" cx="5.5" cy="12" r="1.6"/><circle class="fill" cx="12" cy="12" r="1.6"/><circle class="fill" cx="18.5" cy="12" r="1.6"/></svg>';
      more.addEventListener("click", (ev) => {
        ev.stopPropagation();
        shipMenuOpen = shipMenuOpen === info.mmsi ? null : info.mmsi;
        renderShipsList();
      });
      li.append(dot, grow, bellButton(info, renderAlertBox), more);
      if (shipMenuOpen === info.mmsi) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "danger";
        remove.textContent = "Remove";
        remove.title = `Stop tracking ${info.name} (its passages stay in Stats)`;
        remove.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          if (!confirm(`Stop tracking ${info.name}?`)) return;
          shipMenuOpen = null;
          await fetch(`api/ships/${info.mmsi}`, { method: "DELETE" });
          await loadState();
        });
        li.append(remove);
      }
      li.addEventListener("click", () => {
        if (menuModal()) menu.close();
        focusShip(info.mmsi);
      });
      return li;
    }));
    renderFleets();
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
      follow.className = "two-line";
      follow.innerHTML = "Follow<small>map only</small>";
      follow.title = "Show on the map with its latest trip";
      follow.addEventListener("click", () => choose("api/follow", follow));
      const add = document.createElement("button");
      add.type = "button";
      add.className = "primary two-line";
      add.innerHTML = "Add<small>alerts and stats</small>";
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

  // --- the menu -------------------------------------------------------------------
  // Wide screens: a panel down the right that leaves the map usable (Stats widens it for its
  // two columns). Narrower ones: a sheet of fixed height from the bottom, dragged down or
  // tapped beside to close. Each tab keeps its own scroll.

  const menu = $("ships-dialog");
  const SIDE = matchMedia("(min-width: 900px)");
  const menuModal = () => menu.open && menu.matches(":modal");
  const tabScroll = new Map();

  // Stats while its numbers are on the way (the first time), and when they don't come.
  function openStats(pane) {
    let status = pane.querySelector(".pane-status");
    if (!status) {
      status = document.createElement("div");
      status.className = "pane-status";
      pane.prepend(status);
    }
    const first = !statsData;
    pane.classList.toggle("pane-loading", first);
    status.hidden = !first;
    status.textContent = "Loading the passages…";
    loadStats().then(() => {
      status.hidden = true;
      pane.classList.remove("pane-loading");
      pane.scrollTop = tabScroll.get("stats") || 0;
    }).catch((err) => {
      console.error(err);
      if (statsData) return; // the earlier numbers stay up
      status.hidden = false;
      status.textContent = "Couldn't load the stats. ";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "small";
      retry.textContent = "Try again";
      retry.addEventListener("click", () => openStats(pane));
      status.append(retry);
    });
  }

  function setTab(name) {
    const current = menu.querySelector("[data-pane]:not([hidden])");
    if (current) tabScroll.set(current.dataset.pane, current.scrollTop);
    document.querySelectorAll("[data-tab]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.tab === name));
      b.setAttribute("aria-selected", String(b.dataset.tab === name));
    });
    document.querySelectorAll("[data-pane]").forEach((pane) => { pane.hidden = pane.dataset.pane !== name; });
    document.body.classList.toggle("menu-wide", name === "stats");
    const pane = menu.querySelector(`[data-pane="${name}"]`);
    pane.scrollTop = tabScroll.get(name) || 0;
    store.set("fw-tab", name);
    if (name === "stats") openStats(pane);
    if (name === "layers") renderLayers();
    if (name === "settings") renderSettings();
    if (name === "logbook") logbook?.load();
  }
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  // Tap a folded explanation to read it whole (a link in it still opens).
  menu.addEventListener("click", (e) => {
    const note = e.target.closest?.("p.note");
    if (note && !e.target.closest("a")) note.classList.toggle("open");
    if (e.target === menu && menuModal()) menu.close(); // a tap beside the sheet
  });

  function openMenu(tab) {
    renderShipsList();
    $("notify-result").textContent = "";
    const saved = store.get("fw-tab") === "fleets" ? "layers" : store.get("fw-tab");
    if (SIDE.matches) {
      document.body.classList.add("menu-open");
      menu.show();
    } else {
      document.body.classList.add("menu-sheet");
      menu.showModal();
    }
    setTab(tab || (["stats", "logbook", "layers", "settings"].includes(saved) ? saved : "ships"));
    menu.focus(); // not the first tab, which would show a focus ring
  }
  menu.addEventListener("close", () => {
    document.body.classList.remove("menu-open", "menu-wide", "menu-sheet");
    menu.style.transform = "";
  });
  $("ships-button").addEventListener("click", () => {
    if (menu.open) menu.close();
    else openMenu();
  });
  // Crossing the width where the menu changes shape: open it again in the new shape.
  SIDE.addEventListener("change", () => {
    if (!menu.open) return;
    const tab = store.get("fw-tab");
    menu.close();
    openMenu(tab);
  });

  // Drag the sheet down by its handle or tabs to close it.
  {
    let start = null;
    const grab = (e) => {
      if (SIDE.matches || (e.target.closest("button") && !e.target.closest("#menu-grab"))) return;
      start = { y: e.clientY, id: e.pointerId, dy: 0 };
      menu.classList.add("dragging");
    };
    $("menu-grab").addEventListener("pointerdown", grab);
    menu.querySelector(".dialog-head").addEventListener("pointerdown", grab);
    window.addEventListener("pointermove", (e) => {
      if (!start || e.pointerId !== start.id) return;
      start.dy = Math.max(0, e.clientY - start.y);
      menu.style.transform = `translateY(${start.dy}px)`;
    });
    const drop = (e) => {
      if (!start || e.pointerId !== start.id) return;
      const far = start.dy > Math.min(140, menu.offsetHeight * 0.25);
      start = null;
      menu.classList.remove("dragging");
      menu.style.transform = "";
      if (far) menu.close();
    };
    window.addEventListener("pointerup", drop);
    window.addEventListener("pointercancel", drop);
  }


  // --- areas (zones) ------------------------------------------------------------

  const ZONE_STYLE = {
    inner: { color: "#f97316", label: "Inner route" },
    outer: { color: "#8b5cf6", label: "Outer route" },
    phone: { color: "#16a34a", label: "My area" },
  };
  const zoneLayers = new Map(); // id -> { zone, layer }
  let editing = false;
  // The areas are only on the map while they're edited or asked for (Settings › Alerts):
  // once set up they'd just be lines across the chart.
  let zonesShown = false;
  const zonesOnMap = () => editing || zonesShown;
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
      const layer = make(zone.points, { ...zoneStyle(zone), interactive: editing, pmIgnore: false });
      layer.bindTooltip(zone.name, { sticky: true });
      layer.on("pm:edit", () => saveZoneShape(zone.id));
      zoneLayers.set(zone.id, { zone, layer });
      if (!zonesOnMap()) continue;
      layer.addTo(map);
      if (editing) layer.pm.enable({ allowSelfIntersection: false, snappable: false });
      layer.bringToBack();
    }
    renderZonesList();
  }

  // Show the areas on the map to check them (or hide them again); with a zone, go to it.
  // With porkkala, the places passages are timed at too (Stats › Times past Porkkala): inner
  // ones between Utterböte and Östra Styrskär, outer ones on the meridian (passages.py
  // INNER_TIMING_GATE, PORKKALA_MERIDIAN and MERIDIAN_LATS).
  const PORKKALA_LINE = [[59.55, 24.40], [60.15, 24.40]];
  const INNER_GATE = [[59.96566, 24.45308], [59.95250, 24.45639]];
  let porkkalaLine = null;
  function showZones(on, zone = null, { porkkala = false } = {}) {
    zonesShown = on;
    zonesKey = "";
    renderZones(lastState?.zones || []);
    if (on && porkkala) {
      const label = (at, html, direction, className) => L.marker(at, {
        icon: L.divIcon({ className: "", iconSize: [0, 0] }), interactive: false, keyboard: false,
      }).bindTooltip(html, { permanent: true, direction, offset: direction === "left" ? [-8, 0] : [0, 10], className });
      porkkalaLine ||= L.featureGroup([
        L.polyline(PORKKALA_LINE, { color: "#8b5cf6", weight: 3, opacity: 0.9, dashArray: "8 6", interactive: false }),
        label([59.875, 24.40], "Outer route timed here<br>24.40° E", "left", "porkkala-tip"),
        L.polyline(INNER_GATE, { color: "#ea580c", weight: 7, opacity: 0.95, interactive: false }),
        label(INNER_GATE[1], "Inner route timed here<br>Utterböte – Östra Styrskär", "bottom", "porkkala-tip inner"),
      ]);
      porkkalaLine.addTo(map);
    } else if (porkkalaLine && !on) {
      porkkalaLine.remove();
    }
    renderZonesShown();
    const layer = zone && zoneLayers.get(zone.id)?.layer;
    if (on && (layer || zoneLayers.size || porkkala)) {
      if (menuModal()) $("ships-dialog").close();
      const all = [...zoneLayers.values()].map((e) => e.layer);
      // The timing places close up: the inner gate, and the meridian down to the outer route.
      const bounds = layer ? layer.getBounds()
        : porkkala ? L.latLngBounds([[59.84, 24.33], [60.02, 24.52]]) : L.featureGroup(all).getBounds();
      const pad = cardPadding();
      // Clear of the menu too, where it stays open beside the map.
      if (menu.open) pad.paddingBottomRight = [pad.paddingBottomRight[0] + menu.offsetWidth, pad.paddingBottomRight[1]];
      map.fitBounds(bounds, { ...pad, maxZoom: 13 });
    }
  }

  const timingLinesShown = () => Boolean(porkkalaLine?._map);
  // The Stats link that shows the timing lines hides them again too.
  function labelTimingLines(button) {
    button.textContent = timingLinesShown() ? "Hide the lines from the map" : "Show the lines on the map";
    button.setAttribute("aria-pressed", String(timingLinesShown()));
  }
  function renderZonesShown() {
    $("areas-shown").hidden = !zonesShown || editing;
    $("areas-shown").firstElementChild.textContent = timingLinesShown() ? "Passage lines shown" : "Alert areas shown";
    $("areas-shown").title = timingLinesShown() ? "Hide the passage lines from the map" : "Hide the alert areas from the map";
    document.querySelectorAll("#stats-more .sm-lines").forEach(labelTimingLines);
    $("areas-show").textContent = zonesShown ? "Hide from map" : "Show on map";
    $("areas-show").setAttribute("aria-pressed", String(zonesShown));
  }
  $("areas-shown").addEventListener("click", () => showZones(false));
  $("areas-show").addEventListener("click", () => showZones(!zonesShown));

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
    renderZonesShown();
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

  function renderNotifyStatus() { renderAlertBox(); }

  // Whether a point is inside a polygon given as [[lat, lon], ...].
  function inPolygon(lat, lon, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [ay, ax] = pts[i], [by, bx] = pts[j];
      if ((ay > lat) !== (by > lat) && lon < ((bx - ax) * (lat - ay)) / (by - ay) + ax) inside = !inside;
    }
    return inside;
  }

  // Alerts in one box: where they go, when they're held back, the areas, which ships alert,
  // the last one sent, and the add-on options (changed in Home Assistant).
  // "notify.mobile_app_samsung_galaxy_s22" reads "Samsung Galaxy S22 (companion app)".
  function serviceName(service) {
    if (!service) return "–";
    const m = /^notify\.mobile_app_(.+)$/.exec(service);
    if (!m) return service;
    const words = m[1].split("_").map((w) => (/\d/.test(w) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)));
    return `${words.join(" ")} (companion app)`;
  }

  function renderAlertBox() {
    const st = lastState;
    if (!st) return;
    const a = st.alerts || {};
    const canNotify = st.can_notify ?? st.ha_connected;
    $("notify-status").textContent = st.ha_connected
      ? "A phone alert when one of your ships takes the inner route."
      : canNotify ? "A phone alert through ntfy when one of your ships takes the inner route. Home Assistant isn't set up, so where your phone is can't be checked."
      : "Not connected to Home Assistant, so no alerts can be sent.";
    $("notify-test").disabled = !canNotify;
    const zones = st.zones || [];
    const areas = zones.filter((z) => z.role === "phone" && z.shape !== "line");
    const ph = phoneFix();
    const inArea = ph ? areas.find((z) => inPolygon(ph.lat, ph.lon, z.points)) : null;
    let where;
    if (!areas.length) where = "you have no area drawn, so alerts go out wherever you are";
    else if (!ph) where = "your phone's position isn't known just now";
    else where = inArea ? `your phone is in ${inArea.name} now` : "your phone is outside your areas now";
    const last = st.last_notification;
    const lastShip = last && ships.get(last.mmsi)?.info.name;
    const facts = [
      ["Sent to", (st.ha_connected ? serviceName(a.notify_service) : a.ntfy ? "ntfy" : "–")
        + (st.ha_connected && a.ntfy ? ", or ntfy when Home Assistant doesn't answer" : "")],
      ["Only in my area", a.require_phone_area === false ? "No: alerts go out wherever you are" : `Yes: ${where}`],
      ["Phone position", a.phone_name ? `${a.phone_name} (${a.phone_entity})` : a.phone_entity || "–"],
      ["Feed problems", a.notify_feed_problems === false ? "Not alerted" : "Alerted when ship data stops coming"],
      ["Last alert", last ? `${lastShip ? `${shortName(lastShip)}, ` : ""}${fmtWhen(last.ts)}` : "None yet"],
    ];
    const dl = $("alert-facts");
    dl.replaceChildren(...facts.flatMap(([k, v]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      return [dt, dd];
    }));
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    if (a.standalone) dd.innerHTML = `<span class="muted">Change these in the container's settings (its FW_… environment values), then restart it.</span>`;
    else dd.innerHTML = `<a href="${esc(a.config_url || "/hassio/dashboard")}" target="_top">Change these in Home Assistant</a> <span class="muted">(the app's Configuration tab)</span>`;
    dl.append(dt, dd);

    const list = $("alert-areas");
    list.replaceChildren(...(zones.length ? zones.map((zone) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="swatch ${zone.shape}" style="background:${esc(ZONE_STYLE[zone.role]?.color || "#64748b")}"></span>`
        + `<div class="grow clickable" title="Show it on the map"><span>${esc(zone.name)}</span><span class="small muted">${esc(ZONE_STYLE[zone.role]?.label || zone.role)}`
        + `${zone === inArea ? " · your phone is here" : ""}</span></div>`;
      li.querySelector(".grow").addEventListener("click", () => showZones(true, zone));
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "Edit";
      edit.addEventListener("click", () => {
        $("areas-open").click();
        const layer = zoneLayers.get(zone.id)?.layer;
        if (layer) map.fitBounds(layer.getBounds(), { ...cardPadding(), maxZoom: 13 });
      });
      li.append(edit);
      return li;
    }) : [Object.assign(document.createElement("li"), { className: "small muted", textContent: "No areas yet." })]));

    const mine = [...ships.values()].filter(({ info }) => !info.fleet);
    $("alert-ships").replaceChildren(...mine.map(({ info }) => {
      const chip = document.createElement("span");
      chip.className = "alert-chip";
      chip.innerHTML = `<i class="dot" style="background:${esc(info.color)}"></i><span>${esc(shortName(info.name))}</span>`;
      chip.classList.toggle("off", info.alerts === false);
      chip.append(bellButton(info, () => { renderAlertBox(); renderShipsList(); }));
      return chip;
    }));
  }

  function renderChartKeep() {
    const vc = lastState?.vchart;
    const keep = vc?.keep;
    $("chart-keep").checked = Boolean(keep);
    $("chart-foreign").checked = vc?.foreign !== false;
    $("chart-sample").checked = sampleOnPress;
    let text = "Downloads the drawn chart of all Finnish waters once (the Gulf of Finland first, then the Archipelago Sea and Åland, the Gulf of Bothnia and the charted lakes), so it opens quickly everywhere. Off: only the parts you look at are downloaded.";
    if (keep && keep.running) {
      const last = keep.last_ok ? ` Last area finished at ${clock(keep.last_ok)}.` : "";
      const failed = keep.failed ? ` ${keep.failed} areas failed and will be tried again in an hour.` : "";
      const what = keep.stage === "overview"
        ? "Downloading Finnish waters for the zoomed-out chart"
        : "Downloading the detailed chart of Finnish waters";
      text = `${what}: ${keep.done} of ${keep.total} areas (${vc.mb} MB so far). The chart works meanwhile.${last}${failed}`;
    } else if (keep) {
      text = `The chart of all Finnish waters is on the NAS (${vc.mb} MB). It's refreshed monthly.`;
    }
    $("chart-keep-status").textContent = text;
    const bar = $("chart-keep-progress");
    bar.hidden = !(keep && keep.running && keep.total);
    if (!bar.hidden) bar.firstElementChild.style.width = `${Math.min(100, (100 * keep.done) / keep.total).toFixed(1)}%`;
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
  // The whole trip a recorded passage was part of (port to port, the server caps it),
  // with a dot every 10 minutes. The map opens on the hour either side of the passage.

  const REPLAY_FOCUS = 3600;
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
    const data = await api(`api/track/${p.mmsi}?around=${p.ts}`);
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
    const when = fmtWhen(p.ts);
    $("replay-when").textContent = p.sog ? `${when} · ${p.sog.toFixed(0)} kn` : when;
    const keepDays = lastState?.retention_days ?? 60;
    $("replay-note").textContent = data.points.length > 1
      ? "The whole trip, port to port. Dots every 10 minutes; tap one for the time."
      : now() - p.ts > keepDays * 86400
        ? `The track isn't stored any more (positions are kept for ${keepDays} days).`
        : "No track was stored for this passage.";
    replayPoints = data.points;
    replayColor = color;
    $("replay-play").hidden = data.points.length < 2;
    $("replay").hidden = false;
    $("strip").hidden = true;

    const panel = $("replay").getBoundingClientRect();
    // The whole trip is drawn, but the map fits the hour either side of the passage.
    const near = data.points.filter((q) => Math.abs(q[2] - p.ts) <= REPLAY_FOCUS).map((q) => [q[0], q[1]]);
    if (near.length > 1) {
      map.fitBounds(L.latLngBounds(near), { paddingTopLeft: [40, 70], paddingBottomRight: [40, window.innerHeight - panel.top + 20], maxZoom: 13 });
    } else if (pts.length > 1) {
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
  // device's time zone. The ship and direction filters apply to everything in the tab;
  // tapping a bar (or a day in a month) narrows the passages, ships and weather to it.

  const MONTHS = Array.from({ length: 12 }, (_, m) => new Date(2000, m, 1).toLocaleString("en", { month: "short" }));
  const MONTHS_LONG = Array.from({ length: 12 }, (_, m) => new Date(2000, m, 1).toLocaleString("en", { month: "long" }));
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const INNER_COLOR = "#f97316";
  const SHORT_DIR = { east: "to Helsinki", west: "to Stockholm" };
  const PAGE = 20; // passages shown, and added by "Show more"
  let statsData = null;
  let statsRowsOf = null, statsRowsAll = []; // statsData's passages as objects, made once
  let statsYear = new Date().getFullYear();
  let statsView = "month"; // year, month, week, weekday or time
  let statsPick = null; // the bar tapped in the current view (in the time view "east:450"), or null
  let statsDay = null; // a day tapped in the month calendar
  let statsMode = store.get("fw-stats-mode") === "share" ? "share" : "count";
  let statsDir = ["east", "west"].includes(store.get("fw-stats-dir")) ? store.get("fw-stats-dir") : "both";
  const statsHidden = new Set((() => { try { return JSON.parse(store.get("fw-stats-hidden") || "[]"); } catch { return []; } })());
  let passagesShown = PAGE;
  let statsBars = []; // the bars drawn, for the hover box and arrow keys

  function isoWeek(d) {
    const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
    return Math.ceil(((t - Date.UTC(t.getUTCFullYear(), 0, 1)) / 86400000 + 1) / 7);
  }

  // Bars for each view (not the time view): the keys along the axis, a short label, a long name.
  function statsAxis(view, years) {
    const range = (n) => Array.from({ length: n }, (_, i) => i);
    if (view === "year") return years.map((y) => ({ key: y, label: String(y), name: String(y) }));
    if (view === "week") return range(53).map((w) => ({ key: w + 1, label: (w + 1) % 4 === 1 ? String(w + 1) : "", name: `Week ${w + 1}` }));
    if (view === "weekday") return range(7).map((d) => ({ key: d, label: WEEKDAYS[d], name: `${WEEKDAYS_LONG[d]}s` }));
    return range(12).map((m) => ({ key: m, label: MONTHS[m], name: MONTHS_LONG[m] }));
  }

  const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const hhmm = (minutes) => `${String(Math.floor(minutes / 60) % 24).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;

  function median(values) {
    const v = [...values].sort((a, b) => a - b);
    const mid = v.length >> 1;
    return v.length % 2 ? v[mid] : Math.round((v[mid - 1] + v[mid]) / 2);
  }

  async function loadStats() {
    statsData = await api("api/stats");
    statsLoadedAt = Date.now();
    const years = new Set(statsData.passages.map(([ts]) => new Date(ts * 1000).getFullYear()));
    years.add(new Date().getFullYear());
    const sorted = [...years].sort((a, b) => b - a);
    if (!years.has(statsYear)) { statsYear = sorted[0]; statsPick = null; }
    const select = $("stats-year");
    select.replaceChildren(...sorted.map((y) => new Option(String(y), String(y), false, y === statsYear)));
    renderStats();
  }

  $("stats-year").addEventListener("change", (ev) => {
    statsYear = Number(ev.target.value);
    statsPick = null;
    statsDay = null;
    renderStats();
  });
  document.querySelectorAll("[data-stats-view]").forEach((b) => b.addEventListener("click", () => {
    statsView = b.dataset.statsView;
    statsPick = null;
    statsDay = null;
    store.set("fw-stats-view", statsView);
    renderStats();
  }));
  statsView = store.get("fw-stats-view") || "month";
  if (statsView === "hour") statsView = "time";
  document.querySelectorAll("#stats-mode [data-mode]").forEach((b) => b.addEventListener("click", () => {
    statsMode = b.dataset.mode;
    store.set("fw-stats-mode", statsMode);
    renderStats();
  }));
  document.querySelectorAll("#stats-dir [data-dir]").forEach((b) => b.addEventListener("click", () => {
    statsDir = b.dataset.dir;
    store.set("fw-stats-dir", statsDir);
    if (statsView === "time") statsPick = null;
    renderStats();
  }));

  function setHidden(mmsis) {
    statsHidden.clear();
    for (const m of mmsis) statsHidden.add(m);
    store.set("fw-stats-hidden", JSON.stringify([...statsHidden]));
    passagesShown = PAGE;
    renderStats();
  }

  // The ships Stats has anything for: today's, and removed ones with passages.
  const statsShips = () => statsData.ships;

  function statsRows() {
    if (statsRowsOf !== statsData) {
      statsRowsOf = statsData;
      statsRowsAll = statsData.passages.map(([ts, mmsi, route, direction, wind_ms, wind_dir, ice, notified, sog, porkkala]) => {
        const d = new Date(ts * 1000);
        const at = new Date((porkkala || ts) * 1000); // timed at its route's place off Porkkala, where known
        return {
          ts, mmsi, route, direction, wind_ms, wind_dir, ice, notified, sog,
          year: d.getFullYear(), month: d.getMonth(), date: d.getDate(), week: isoWeek(d), weekday: (d.getDay() + 6) % 7,
          minute: at.getHours() * 60 + at.getMinutes(),
        };
      });
    }
    return statsRowsAll;
  }

  // Time view bins: 10 minutes; a pick is "east:450" (the bin starting 07:30 towards Helsinki).
  const BIN = 10;
  const timePick = () => (statsView === "time" && statsPick ? statsPick.split(":") : null);
  function inPick(p) {
    if (statsPick == null) return true;
    if (statsView === "time") {
      const [dir, bin] = timePick();
      return p.direction === dir && p.minute >= Number(bin) && p.minute < Number(bin) + BIN;
    }
    return p[statsView] === statsPick;
  }

  function renderStats() {
    if (!statsData) return;
    const all = statsRows();
    const shipOk = (p) => !statsHidden.has(p.mmsi);
    const dirOk = (p) => statsDir === "both" || p.direction === statsDir;
    const filtered = all.filter((p) => shipOk(p) && dirOk(p));
    const yearsWithData = [...new Set(all.map((p) => p.year))];
    const years = [...new Set([...yearsWithData, new Date().getFullYear()])].sort((a, b) => a - b);
    if (statsView === "year" && yearsWithData.length < 2) statsView = "month";

    document.querySelectorAll("[data-stats-view]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.statsView === statsView));
      if (b.dataset.statsView === "year") b.hidden = yearsWithData.length < 2;
    });
    document.querySelectorAll("#stats-mode [data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === statsMode)));
    document.querySelectorAll("#stats-dir [data-dir]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.dir === statsDir)));
    $("stats-year").hidden = statsView === "year" || years.length < 2;

    renderStatsTiles(filtered, all);
    renderStatsChips(all);

    // The year view covers everything; the others the chosen year.
    const rows = statsView === "year" ? filtered : filtered.filter((p) => p.year === statsYear);
    let picked = null;
    if (statsView === "time") {
      picked = renderTimeChart(rows);
    } else {
      const axis = statsAxis(statsView, years);
      picked = axis.find((a) => a.key === statsPick) || null;
      if (!picked) statsPick = null;
      renderBarChart(axis, rows);
    }
    const inMonth = statsView === "month" && picked;
    if (!inMonth) statsDay = null;
    renderCalendar(inMonth ? rows.filter((p) => p.month === statsPick && p.route === "inner") : null);

    let scope = rows.filter(inPick);
    if (statsDay) scope = scope.filter((p) => p.date === statsDay);
    const scopeName = statsDay ? `${statsDay} ${MONTHS_LONG[statsPick]} ${statsYear}`
      : picked ? `${picked.name}${statsView === "year" ? "" : ` ${statsYear}`}`
      : statsView === "year" ? "all years" : String(statsYear);
    const innerN = scope.filter((p) => p.route === "inner").length;
    if (!statsData.passages.length) {
      $("stats-scope").textContent = "None yet. Passages are counted from when an inner-route line is in place.";
    } else {
      const hint = statsDay ? "Tap the day again for the whole month."
        : inMonth ? "Tap a day, or the month again for all of it."
        : picked ? "Tap it again for all." : "Tap a bar to look at it on its own.";
      $("stats-scope").textContent = `${scopeName}: ${innerN} inner of ${scope.length} passages. ${hint}`;
    }

    // Ship rows count every ship (the hidden ones dimmed), so tapping one can bring it back.
    const shipScope = (statsView === "year" ? all : all.filter((p) => p.year === statsYear))
      .filter((p) => dirOk(p) && inPick(p) && (!statsDay || p.date === statsDay));
    renderStatsShips(shipScope);
    renderStatsMore(filtered, all);
    renderPassageList(scope, scopeName);
    renderStatsWeather(scope, scopeName);
    renderStatsCsv(picked, scopeName);
    renderStatsAircraft();
  }

  // Four tiles: this week, this month, this year, and the inner share of the last 30 days,
  // each against the same stretch of the period before (only once there's data for it).
  function renderStatsTiles(rows, all) {
    const box = $("stats-tiles");
    if (!all.length) { box.replaceChildren(); return; }
    const t = Date.now() / 1000;
    const d = new Date();
    const first = all[0].ts;
    const between = (a, b) => rows.filter((p) => p.route === "inner" && p.ts >= a && p.ts < b).length;
    const sec = (date) => date.getTime() / 1000;
    const weekStart = sec(new Date(d.getFullYear(), d.getMonth(), d.getDate() - (d.getDay() + 6) % 7));
    const monthStart = sec(new Date(d.getFullYear(), d.getMonth(), 1));
    const prevMonth = sec(new Date(d.getFullYear(), d.getMonth() - 1, 1));
    const yearStart = sec(new Date(d.getFullYear(), 0, 1));
    const prevYear = sec(new Date(d.getFullYear() - 1, 0, 1));
    const tile = (value, label, now, before, prevStart, unit, versus) => {
      const el = document.createElement("div");
      el.className = "tile";
      let diff = "";
      if (before != null && prevStart >= first) {
        const delta = now - before;
        diff = delta === 0 ? `<em>same as ${versus}</em>`
          : `<em class="${delta > 0 ? "up" : "down"}">${delta > 0 ? "▲" : "▼"} ${Math.abs(delta)}${unit} vs ${versus}</em>`;
      }
      el.innerHTML = `<strong>${value}</strong><span>${label}</span>${diff}`;
      return el;
    };
    const week = between(weekStart, t), weekBefore = between(weekStart - 7 * 86400, t - 7 * 86400);
    const month = between(monthStart, t), monthBefore = between(prevMonth, Math.min(monthStart, prevMonth + (t - monthStart)));
    const year = between(yearStart, t), yearBefore = between(prevYear, Math.min(yearStart, prevYear + (t - yearStart)));
    const share = (a, b) => {
      const these = rows.filter((p) => p.ts >= a && p.ts < b);
      return these.length ? Math.round((100 * these.filter((p) => p.route === "inner").length) / these.length) : null;
    };
    const s30 = share(t - 30 * 86400, t), s60 = share(t - 60 * 86400, t - 30 * 86400);
    box.replaceChildren(
      tile(week, "this week", week, weekBefore, weekStart - 7 * 86400, "", "last week"),
      tile(month, MONTHS_LONG[d.getMonth()], month, monthBefore, prevMonth, "", MONTHS[(d.getMonth() + 11) % 12]),
      tile(year, String(d.getFullYear()), year, yearBefore, prevYear, "", d.getFullYear() - 1),
      tile(s30 == null ? "–" : `${s30}%`, "inner, 30 days", s30, s30 == null ? null : s60, t - 60 * 86400, " pts", "before"),
    );
  }

  // Ship chips: the colour key, and which ships are counted (tap to leave one out).
  function renderStatsChips(all) {
    const withPassages = new Set(all.map((p) => p.mmsi));
    const ships = statsShips().filter((s) => s.current || withPassages.has(s.mmsi));
    $("stats-ship-chips").replaceChildren(...ships.map((ship) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "stat-chip";
      b.setAttribute("aria-pressed", String(!statsHidden.has(ship.mmsi)));
      b.title = statsHidden.has(ship.mmsi) ? `Count ${ship.name}` : `Leave ${ship.name} out`;
      b.innerHTML = `<i class="dot" style="background:${esc(ship.color)}"></i>${esc(shortName(ship.name))}`;
      b.addEventListener("click", () => {
        const next = new Set(statsHidden);
        if (next.has(ship.mmsi)) next.delete(ship.mmsi); else next.add(ship.mmsi);
        setHidden(ships.every((s) => next.has(s.mmsi)) ? [] : next);
      });
      return b;
    }));
  }

  // A tidy step for grid lines: 1, 2 or 5 times a power of ten.
  function niceStep(max, lines = 3) {
    const raw = Math.max(1, max) / lines;
    const pow = 10 ** Math.floor(Math.log10(raw));
    return [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw);
  }

  // Bars: each { key, label, name, total, stacks: [{ color, n, title }], sub }, drawn at the
  // chart's real width, with grid lines and their numbers on the left.
  function barsSvg(bars, { W, H, max, share, label, mark }) {
    const LEFT = 26, TOP = 14, BOTTOM = 20;
    const col = (W - LEFT) / bars.length;
    const w = Math.min(44, Math.max(1, col - Math.min(6, col * 0.25)));
    const unit = (H - TOP - BOTTOM) / Math.max(1, max);
    const step = share ? 25 : niceStep(max);
    let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(label)}">`;
    for (let v = step; v <= (share ? 100 : max); v += step) {
      const y = H - BOTTOM - v * unit;
      svg += `<line class="grid" x1="${LEFT}" x2="${W}" y1="${y}" y2="${y}"/><text class="tick" x="${LEFT - 4}" y="${y + 4}">${v}${share ? "%" : ""}</text>`;
    }
    const showCounts = bars.length <= 12;
    bars.forEach((b, i) => {
      const x = LEFT + i * col + (col - w) / 2, cx = x + w / 2;
      const cls = ["bar", statsPick === b.key ? "selected" : "", statsPick != null && statsPick !== b.key ? "dim" : ""].join(" ");
      let y = H - BOTTOM;
      svg += `<g class="${cls}" data-i="${i}"><rect class="hit" x="${LEFT + i * col}" y="0" width="${col}" height="${H}"/>`;
      for (const s of b.stacks) {
        if (!s.n) continue;
        y -= s.n * unit;
        svg += `<rect x="${x}" y="${y}" width="${w}" height="${s.n * unit}" fill="${esc(s.color)}"/>`;
      }
      if (showCounts && b.text) svg += `<text class="count" x="${cx}" y="${y - 4}">${esc(b.text)}</text>`;
      if (b.label) svg += `<text class="label" x="${cx}" y="${H - 5}">${esc(b.label)}</text>`;
      svg += "</g>";
    });
    if (mark != null) {
      const mx = LEFT + mark * col;
      svg += `<line class="usual" x1="${mx}" x2="${mx}" y1="${TOP - 6}" y2="${H - BOTTOM}"/>`;
    }
    svg += `<line class="axis" x1="${LEFT}" x2="${W}" y1="${H - BOTTOM}" y2="${H - BOTTOM}"/></svg>`;
    return svg;
  }

  // A bucket's bar: inner passages by ship, or the share of all its passages that were inner.
  function makeBar(key, label, name, list) {
    const inner = list.filter((p) => p.route === "inner");
    if (statsMode === "share") {
      const pct = list.length ? Math.round((100 * inner.length) / list.length) : 0;
      return {
        key, label, name, value: pct, total: list.length, inner: inner.length,
        text: list.length ? `${pct}%` : "", stacks: [{ color: INNER_COLOR, n: pct }],
        lines: list.length ? [`${pct}% inner (${inner.length} of ${list.length})`] : ["No passages"],
      };
    }
    const stacks = statsShips().map((s) => {
      const n = inner.filter((p) => p.mmsi === s.mmsi).length;
      return { color: s.color, n, title: `${s.name}: ${n}` };
    });
    return {
      key, label, name, value: inner.length, total: list.length, inner: inner.length, text: inner.length ? String(inner.length) : "",
      stacks, lines: inner.length ? stacks.filter((s) => s.n).map((s) => s.title) : ["No inner passages"],
    };
  }

  const chartWidth = () => Math.max(260, Math.round($("stats-chart").clientWidth || 340));

  function renderBarChart(axis, rows) {
    const byKey = new Map(axis.map((a) => [a.key, []]));
    for (const p of rows) byKey.get(p[statsView])?.push(p);
    const bars = axis.map((a) => makeBar(a.key, a.label, a.name, byKey.get(a.key)));
    const max = statsMode === "share" ? 100 : Math.max(1, ...bars.map((b) => b.value));
    statsBars = [bars];
    $("stats-chart").innerHTML = barsSvg(bars, {
      W: chartWidth(), H: 180, max, share: statsMode === "share",
      label: statsMode === "share" ? "Share of passages on the inner route" : "Inner route passages",
    });
  }

  // The time view: a strip per direction in 10-minute bins, each zoomed to the hours its
  // passages fall in (the odd late one aside), the usual time marked.
  function renderTimeChart(rows) {
    const dirs = statsDir === "both" ? ["east", "west"] : [statsDir];
    const box = $("stats-chart");
    const parts = [];
    statsBars = [];
    let picked = null;
    for (const dir of dirs) {
      const these = rows.filter((p) => p.direction === dir);
      const inner = these.filter((p) => p.route === "inner");
      const head = document.createElement("div");
      head.className = "time-head small";
      if (!these.length) {
        head.innerHTML = `<span class="strong">${SHORT_DIR[dir][0].toUpperCase()}${SHORT_DIR[dir].slice(1)}</span> <span class="muted">· no passages</span>`;
        parts.push(head);
        continue;
      }
      const mins = these.map((p) => p.minute).sort((a, b) => a - b);
      const lo = mins[Math.floor(mins.length * 0.02)], hi = mins[Math.min(mins.length - 1, Math.floor(mins.length * 0.98))];
      let start = Math.floor(lo / 60) * 60, end = Math.ceil((hi + 1) / 60) * 60;
      while (end - start < 120) { start = Math.max(0, start - 30); end = Math.min(1440, end + 30); }
      const outside = these.filter((p) => p.minute < start || p.minute >= end).length;
      const bars = [];
      for (let m = start; m < end; m += BIN) {
        const key = `${dir}:${m}`;
        const bar = makeBar(key, m % (end - start <= 240 ? 30 : 60) === 0 ? hhmm(m) : "", `${hhmm(m)}–${hhmm(m + BIN)} ${SHORT_DIR[dir]}`,
          these.filter((p) => p.minute >= m && p.minute < m + BIN));
        bar.text = "";
        bars.push(bar);
        if (statsPick === key) picked = { name: bar.name };
      }
      const usual = inner.length >= 3 ? median(inner.map((p) => p.minute)) : null;
      head.innerHTML = `<span class="strong">${SHORT_DIR[dir][0].toUpperCase()}${SHORT_DIR[dir].slice(1)}</span>`
        + (usual != null ? ` <span class="muted">· usually ${hhmm(usual)} on the inner route</span>` : "")
        + (outside ? ` <span class="muted">· ${outside} outside these hours</span>` : "");
      const max = statsMode === "share" ? 100 : Math.max(1, ...bars.map((b) => b.value));
      const strip = document.createElement("div");
      strip.className = "time-strip";
      strip.dataset.strip = String(statsBars.length);
      strip.innerHTML = barsSvg(bars, {
        W: chartWidth(), H: 120, max, share: statsMode === "share", label: `Passages ${SHORT_DIR[dir]} by time of day`,
        mark: usual != null && usual >= start && usual < end ? (usual - start) / BIN : null,
      });
      statsBars.push(bars);
      parts.push(head, strip);
    }
    if (statsPick && !picked) statsPick = null;
    box.replaceChildren(...parts);
    return picked;
  }

  // Arrow keys step through the bars, or through the days once a day is picked.
  function stepStats(delta) {
    if (!statsData) return false;
    if (statsDay) {
      const days = new Date(statsYear, statsPick + 1, 0).getDate();
      statsDay = Math.min(days, Math.max(1, statsDay + delta));
    } else {
      const strip = statsBars.find((bars) => bars.some((b) => b.key === statsPick)) || statsBars[0];
      if (!strip?.length) return false;
      const i = strip.findIndex((b) => b.key === statsPick);
      const next = i < 0 ? (delta > 0 ? 0 : strip.length - 1) : Math.min(strip.length - 1, Math.max(0, i + delta));
      statsPick = strip[next].key;
    }
    passagesShown = PAGE;
    renderStats();
    return true;
  }

  // Tap a bar to look at it on its own; hover one (with a mouse) for its numbers by ship.
  function barAt(target) {
    const g = target.closest?.("[data-i]");
    if (!g) return null;
    const strip = g.closest("[data-strip]");
    return statsBars[strip ? Number(strip.dataset.strip) : 0]?.[Number(g.dataset.i)] || null;
  }
  $("stats-chart").addEventListener("click", (ev) => {
    const bar = barAt(ev.target);
    if (!bar) return;
    statsPick = statsPick === bar.key ? null : bar.key;
    statsDay = null;
    passagesShown = PAGE;
    renderStats();
  });
  $("stats-chart").addEventListener("mousemove", (ev) => {
    const tip = $("stats-tip");
    const bar = barAt(ev.target);
    if (!bar || !matchMedia("(pointer: fine)").matches) { tip.hidden = true; return; }
    tip.innerHTML = `<strong>${esc(bar.name)}</strong>${bar.lines.map((l) => `<span>${esc(l)}</span>`).join("")}`;
    tip.hidden = false;
    const wrap = $("stats-chart").parentElement.getBoundingClientRect();
    const x = ev.clientX - wrap.left, y = ev.clientY - wrap.top;
    tip.style.left = `${Math.min(wrap.width - tip.offsetWidth - 4, Math.max(4, x + 12))}px`;
    tip.style.top = `${Math.max(4, y - tip.offsetHeight - 10)}px`;
  });
  $("stats-chart").addEventListener("mouseleave", () => { $("stats-tip").hidden = true; });
  let chartW = 0;
  new ResizeObserver(() => {
    const w = $("stats-chart").clientWidth;
    if (statsData && w && Math.abs(w - chartW) > 8) { chartW = w; renderStats(); }
  }).observe($("stats-chart"));

  // A month as a calendar, with arrows to the months either side: each day shows a dot per
  // inner passage in the ship's colour.
  function renderCalendar(inner) {
    const box = $("stats-calendar");
    box.hidden = !inner;
    if (!inner) { box.replaceChildren(); return; }
    const first = new Date(statsYear, statsPick, 1);
    const days = new Date(statsYear, statsPick + 1, 0).getDate();
    const byDay = new Map();
    for (const p of inner) {
      if (!byDay.has(p.date)) byDay.set(p.date, []);
      byDay.get(p.date).push(p);
    }
    const colors = new Map(statsShips().map((s) => [s.mmsi, s.color]));
    const today = new Date();
    const years = [...$("stats-year").options].map((o) => Number(o.value));
    const head = document.createElement("div");
    head.className = "cal-nav";
    const step = (delta) => {
      let m = statsPick + delta, y = statsYear;
      if (m < 0) { m = 11; y -= 1; }
      if (m > 11) { m = 0; y += 1; }
      return years.includes(y) ? [y, m] : null;
    };
    const arrow = (delta, label, path) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "icon-button plain";
      b.setAttribute("aria-label", label);
      b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`;
      const to = step(delta);
      b.disabled = !to;
      b.addEventListener("click", () => {
        [statsYear, statsPick] = to;
        $("stats-year").value = String(statsYear);
        statsDay = null;
        passagesShown = PAGE;
        renderStats();
      });
      return b;
    };
    const title = document.createElement("span");
    title.className = "strong";
    title.textContent = `${MONTHS_LONG[statsPick]} ${statsYear}`;
    head.append(arrow(-1, "Month before", "M15 6l-6 6 6 6"), title, arrow(1, "Month after", "M9 6l6 6-6 6"));
    const cells = WEEKDAYS.map((w) => {
      const h = document.createElement("span");
      h.className = "cal-head";
      h.textContent = w[0];
      return h;
    });
    for (let i = 0; i < (first.getDay() + 6) % 7; i++) cells.push(document.createElement("span"));
    for (let d = 1; d <= days; d++) {
      const list = byDay.get(d) || [];
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cal-day";
      cell.dataset.day = String(d);
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
        dot.title = `${hhmm(p.minute)} ${SHORT_DIR[p.direction] || ""}`;
        dots.append(dot);
      }
      cell.append(num, dots);
      cell.addEventListener("click", () => {
        statsDay = statsDay === d ? null : d;
        passagesShown = PAGE;
        renderStats();
      });
      cells.push(cell);
    }
    const grid = document.createElement("div");
    grid.className = "stats-calendar";
    grid.replaceChildren(...cells);
    box.replaceChildren(head, grid);
  }

  // One row per ship: its inner passages and share; then its usual times. Tap a row to
  // count only that ship (again for all).
  function renderStatsShips(rows) {
    const byShip = new Map();
    for (const p of rows) {
      if (!byShip.has(p.mmsi)) byShip.set(p.mmsi, []);
      byShip.get(p.mmsi).push(p);
    }
    const innerOf = (mmsi) => (byShip.get(mmsi) || []).filter((p) => p.route === "inner");
    const ships = statsShips();
    const shown = ships.filter((s) => s.current || byShip.has(s.mmsi))
      .sort((a, b) => innerOf(b.mmsi).length - innerOf(a.mmsi).length || a.name.localeCompare(b.name));
    $("stats-ships").replaceChildren(...shown.map((ship) => {
      const all = byShip.get(ship.mmsi) || [];
      const inner = innerOf(ship.mmsi);
      const li = document.createElement("li");
      li.className = "clickable stat-ship";
      if (statsHidden.has(ship.mmsi)) li.classList.add("left-out");
      const only = shown.every((s) => s.mmsi === ship.mmsi || statsHidden.has(s.mmsi)) && !statsHidden.has(ship.mmsi);
      li.title = only ? "Count every ship again" : `Count only ${ship.name}`;
      const usual = ["east", "west"].map((dir) => {
        const these = inner.filter((p) => p.direction === dir);
        return these.length >= 3 ? `${hhmm(median(these.map((p) => p.minute)))} ${SHORT_DIR[dir]}` : null;
      }).filter(Boolean);
      const share = all.length ? ` · ${Math.round((100 * inner.length) / all.length)}% of ${all.length}` : "";
      li.innerHTML = `<span class="dot" style="background:${esc(ship.color)}"></span>`
        + `<div class="grow"><span>${esc(ship.current ? ship.name : `${ship.name} (removed)`)}</span>`
        + `<span class="small muted">${inner.length ? `${inner.length} inner${share}` : "No inner passages"}${usual.length ? ` · usually ${usual.join(", ")}` : ""}</span></div>`
        + `<span class="stat-count">${inner.length}</span>`;
      li.addEventListener("click", () => setHidden(only ? [] : ships.filter((s) => s.mmsi !== ship.mmsi).map((s) => s.mmsi)));
      return li;
    }));
  }

  // Highlights, the inner route in wind like now, times past Porkkala and the year at a
  // glance (stats-more.js), for the ships and direction chosen.
  function renderStatsMore(filtered, all) {
    const w = lastState?.weather || {};
    FerryStatsMore.render($("stats-more"), filtered, {
      ships: statsShips(),
      year: statsYear,
      wind: w.enabled !== false && fresh(w.wind) ? w.wind : null,
      firstTs: all.length ? all[0].ts : null,
      lines: { label: labelTimingLines, toggle: () => showZones(!timingLinesShown(), null, { porkkala: true }) },
      onDay: (ts) => {
        const d = new Date(ts * 1000);
        statsView = "month";
        store.set("fw-stats-view", statsView);
        statsYear = d.getFullYear();
        $("stats-year").value = String(statsYear);
        statsPick = d.getMonth();
        statsDay = d.getDate();
        passagesShown = PAGE;
        renderStats();
        $("passages-title").scrollIntoView({ behavior: "smooth", block: "start" });
      },
    });
  }

  // The passages in the selection, newest first, a day heading over each day's.
  function renderPassageList(scope, scopeName) {
    const list = $("passages-list");
    const sorted = [...scope].sort((a, b) => b.ts - a.ts);
    $("passages-title").textContent = statsPick != null || statsDay ? `Passages, ${scopeName}` : "Passages";
    $("passages-count").textContent = sorted.length ? `${sorted.length} · newest first` : "";
    if (!sorted.length) {
      const p = document.createElement("p");
      p.className = "small muted";
      p.textContent = statsData.passages.length ? "No passages in this selection." : "None yet.";
      list.replaceChildren(p);
      $("passages-more").hidden = true;
      return;
    }
    const colors = new Map(statsShips().map((s) => [s.mmsi, s]));
    const out = [];
    let day = null;
    for (const p of sorted.slice(0, passagesShown)) {
      const d = fmtDay(p.ts);
      if (d !== day) {
        day = d;
        const h = document.createElement("h4");
        h.className = "day-head";
        h.textContent = d;
        out.push(h);
      }
      const ship = colors.get(p.mmsi);
      const row = document.createElement("button");
      row.type = "button";
      row.className = "passage-row";
      row.title = `${ship?.name || `MMSI ${p.mmsi}`}: show this passage on the map`;
      row.innerHTML = `<span class="pr-time">${clock(p.ts)}</span>`
        + `<i class="dot" style="background:${esc(ship?.color || "#64748b")}"></i>`
        + `<span class="pr-ship">${esc(shortName(ship?.name || String(p.mmsi)))}</span>`
        + `<span class="route-tag ${p.route}">${p.route === "inner" ? "Inner" : "Outer"}</span>`
        + `<span class="pr-dir" title="${SHORT_DIR[p.direction] || ""}">${p.direction === "east" ? "→ Hel" : p.direction === "west" ? "← Sto" : ""}</span>`;
      const wx = FerryWeather.row(p);
      if (wx) { wx.classList.add("pr-wx"); row.append(wx); }
      if (p.notified) row.insertAdjacentHTML("beforeend", '<svg class="pr-bell" viewBox="0 0 24 24" aria-label="Alert sent"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/></svg>');
      row.addEventListener("click", () => openPassage(p).catch(console.error));
      out.push(row);
    }
    list.replaceChildren(...out);
    const more = $("passages-more");
    more.hidden = sorted.length <= passagesShown;
    more.textContent = `Show ${Math.min(PAGE, sorted.length - passagesShown)} more`;
  }
  $("passages-more").addEventListener("click", () => { passagesShown += PAGE; renderStats(); });

  async function openPassage(p) {
    const [row] = await api(`api/passages?since=${p.ts}&until=${p.ts + 1}&mmsi=${p.mmsi}&limit=1`);
    if (menuModal()) $("ships-dialog").close();
    await showPassage(row || p);
  }

  // "Does the weather matter?": only with enough passages to say anything.
  const WEATHER_MIN = 20;
  function renderStatsWeather(scope, scopeName) {
    const box = $("stats-weather-box"), note = $("stats-weather-note");
    const enough = scope.length >= WEATHER_MIN;
    box.hidden = !enough;
    note.hidden = enough || !scope.length;
    note.textContent = `The weather comparison needs at least ${WEATHER_MIN} passages in the selection (this has ${scope.length}).`;
    if (enough) FerryWeather.breakdown($("stats-weather"), scope, scopeName);
  }

  // CSV of the selection: its time span (the year, month, week or day picked), ships and
  // direction. A weekday or time of day can't be cut out on the server: that gives the year.
  function renderStatsCsv(picked, scopeName) {
    const q = new URLSearchParams();
    const sec = (date) => String(Math.round(date.getTime() / 1000));
    const span = (a, b) => { q.set("since", sec(a)); q.set("until", sec(b)); };
    let what = scopeName;
    if (statsDay) span(new Date(statsYear, statsPick, statsDay), new Date(statsYear, statsPick, statsDay + 1));
    else if (statsView === "month" && picked) span(new Date(statsYear, statsPick, 1), new Date(statsYear, statsPick + 1, 1));
    else if (statsView === "year" && picked) span(new Date(statsPick, 0, 1), new Date(statsPick + 1, 0, 1));
    else if (statsView === "week" && picked) {
      const jan4 = new Date(statsYear, 0, 4);
      const monday = new Date(statsYear, 0, 4 - ((jan4.getDay() + 6) % 7) + (statsPick - 1) * 7);
      span(monday, new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 7));
    } else if (statsView !== "year") { span(new Date(statsYear, 0, 1), new Date(statsYear + 1, 0, 1)); what = String(statsYear); }
    else what = "all years";
    const shown = statsShips().filter((s) => !statsHidden.has(s.mmsi));
    if (statsHidden.size) q.set("mmsi", shown.map((s) => s.mmsi).join(","));
    if (statsDir !== "both") q.set("direction", statsDir);
    const link = $("stats-csv-sel");
    link.href = `api/passages.csv?${q}`;
    link.title = [what, statsHidden.size ? shown.map((s) => shortName(s.name)).join(", ") : "", statsDir !== "both" ? SHORT_DIR[statsDir] : ""]
      .filter(Boolean).join(" · ");
  }

  // The aircraft history lives at the bottom of Stats (switched on in Layers).
  function renderStatsAircraft() {
    const box = $("stats-aircraft");
    const info = lastState?.aircraft || {};
    if (!info.enabled && !info.flights_stored) { box.replaceChildren(); return; }
    const open = document.createElement("button");
    open.type = "button";
    open.className = "ah-open-button";
    const n = info.flights_stored || 0;
    open.innerHTML = `<span class="strong">Aircraft history</span><span class="small muted">${n ? `${n.toLocaleString()} ${n === 1 ? "flight" : "flights"} of rescue aircraft stored` : "No flights stored yet"}</span>`;
    open.addEventListener("click", () => historyView.open());
    box.replaceChildren(open);
  }

  // --- fleets -------------------------------------------------------------------

  function renderAircraftSection() {
    const info = lastState?.aircraft || { enabled: false };
    return switchRow({
      compact: true,
      title: "Rescue aircraft", color: AIRCRAFT_COLORS.FinnHEMS, checked: info.enabled,
      count: info.enabled ? `${aircraft.size} in the air` : "",
      note: info.error
        ? `Couldn't reach adsb.fi just now (${info.error}). It tries again shortly.`
        : "FinnHEMS, Border Guard and police helicopters and Border Guard planes, from adsb.fi. While this is on, the add-on also records their flights with the map closed (a check every 30 seconds) and keeps a year of them, shown in Stats › Aircraft history.",
      onChange: async (on) => { await api("api/aircraft", { method: "POST", body: { enabled: on } }); await loadState(); },
    });
  }

  // Fleets fold: a row with its switch; open it to see its ships.
  const fleetsOpen = new Set();
  function renderFleets() {
    const box = $("fleets-list");
    const fleets = lastState?.fleets || [];
    renderTrafficRow();
    box.replaceChildren(...fleets.map((fleet) => {
      const section = document.createElement("details");
      section.className = "fleet fleet-fold";
      section.open = fleetsOpen.has(fleet.id);
      section.addEventListener("toggle", () => {
        if (section.open) fleetsOpen.add(fleet.id); else fleetsOpen.delete(fleet.id);
      });
      const head = document.createElement("summary");
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
      const sw = document.createElement("label");
      sw.className = "fold-switch";
      sw.setAttribute("aria-label", `Show ${fleet.name}`);
      sw.append(toggle);
      head.append(dot, name, count, sw);
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
          if (menuModal()) menu.close();
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

  const infoOpen = new Set(); // compact rows whose explanation is unfolded
  function switchRow({ title, color, checked, count = "", note, extra, onChange, compact = false }) {
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
    head.append(dot, name, counter);
    const problem = note && /^Couldn't/.test(note);
    if (compact && note && !problem) {
      // One line; the explanation behind ⓘ.
      const info = document.createElement("button");
      info.type = "button";
      info.className = "icon-button plain info-button";
      info.setAttribute("aria-label", `About ${title}`);
      info.setAttribute("aria-expanded", String(infoOpen.has(title)));
      info.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle class="fill" cx="12" cy="7.6" r="1.2"/></svg>';
      const p = document.createElement("p");
      p.className = "small muted";
      p.textContent = note;
      p.hidden = !infoOpen.has(title);
      info.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (infoOpen.has(title)) infoOpen.delete(title); else infoOpen.add(title);
        p.hidden = !infoOpen.has(title);
        info.setAttribute("aria-expanded", String(!p.hidden));
      });
      head.append(info, toggle);
      section.append(head, p);
    } else {
      head.append(toggle);
      section.append(head);
      if (note) section.append(noteText(note));
    }
    section.classList.toggle("compact", compact);
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

  // "Other ships": the same switch in Layers and at the end of the Ships tab.
  function trafficRow() {
    return switchRow({
      compact: true,
      title: "Other ships", color: "#64748b", checked: trafficOn, count: trafficOn ? trafficCount : "",
      note: trafficNote || "Every ship Digitraffic hears (AIS), coloured by type, in the part of the map you're looking at, down to all of Finland. Refreshed every 30 seconds while switched on and the map is open. Tap one for its photo and links.",
      extra: trafficOn ? trafficLegend() : null,
      onChange: async (on) => { setTraffic(on); renderLayers(); renderTrafficRow(); },
    });
  }
  function renderTrafficRow() {
    $("traffic-row")?.replaceChildren(trafficRow());
  }

  function renderLayers() {
    const faultCount = faultsData?.faults?.length;
    $("layers-list").replaceChildren(
      groupHead("On the chart"),
      switchRow({
        compact: true,
        title: "Harbours", color: HARBOUR_RED, checked: harboursOn,
        count: harboursOn && harbourCount ? `${harbourCount} in view` : "",
        note: harbourNote || "Marinas and harbours from OpenStreetMap with their services, from zoom 11. Tap one for its card. Downloaded once a month.",
        onChange: async (on) => { setHarbours(on); renderLayers(); },
      }),
      switchRow({
        compact: true,
        title: "Faulty lights and buoys", color: "#dc2626", checked: faultsOn,
        count: faultsOn && faultCount != null ? `${faultCount} nearby` : "",
        note: "Unlit, damaged or missing buoys, beacons and lights, from Digitraffic. Shown on the chart view.",
        onChange: async (on) => { faultsOn = on; store.set("fw-faults", on ? "1" : "0"); renderFaults(); renderLayers(); },
      }),
      switchRow({
        compact: true,
        title: "Wrecks and coastal heritage", color: "#92400e", checked: heritageOn,
        note: "Wrecks, and bunkers, trenches and gun positions on the shore and islands (from zoom 13), from Museovirasto's registers. Shown on the drawn chart.",
        onChange: async (on) => { heritageOn = on; store.set("fw-heritage", on ? "1" : "0"); applyHeritage(); },
      }),
      switchRow({
        compact: true,
        title: "More depth contours", color: "#a3bccd", checked: contoursOn,
        note: "Up close (zoom 14 and in), the depth contours the paper chart leaves out, as faint lines like the official chart's. Shown on the drawn chart.",
        onChange: async (on) => { contoursOn = on; store.set("fw-contours", on ? "1" : "0"); applyHeritage(); },
      }),
      switchRow({
        compact: true,
        title: "Highlight nature reserves", color: "#3f9a4f", checked: Boolean(reserves?.all),
        count: [reserves?.all && reserves.count ? `${reserves.count} in view` : "", reserves?.closed ? `${reserves.closed} closed now` : ""].filter(Boolean).join(" · "),
        note: reserves?.note || "Tints and names every nature reserve and national park in view, from zoom 10, on any map. On the drawn chart you can also tap inside one (zoom 11 and in) for its card and links. Bird and seal sanctuaries closed today (no landing or no access) are shown in red dashes with the date they open (on the drawn chart from zoom 11 even with this off). From OpenStreetMap and SYKE's register of movement restriction areas.",
        onChange: async (on) => { reserves?.setAll(on); renderLayers(); },
      }),
      groupHead("Warnings and waterways"),
      ...(notices?.rows() || []).map((r) => switchRow({ compact: true, ...r, onChange: async (on) => { await r.onChange(on); renderLayers(); } })),
      groupHead("On the satellite view"),
      switchRow({
        compact: true,
        title: "Aerial photos (Finland, Estonia)", color: "#4d7c0f", checked: aerialOn,
        note: "From zoom 11 in, the Land Survey's and Maa-amet's own aerial photos (0.5 m, summer) over Esri's satellite images. Sharper close up, but a different look; off shows Esri's as before.",
        onChange: async (on) => { setAerial(on); },
      }),
      groupHead("On every map"),
      ...here.rows().map((r) => switchRow({ compact: true, ...r })),
      ...hist.rows().map((r) => switchRow({ compact: true, ...r, onChange: async (on) => { await r.onChange(on); renderLayers(); } })),
      groupHead("Traffic"),
      trafficRow(),
      renderAircraftSection(),
    );
    renderRoutesList();
    renderMapKey();
  }

  // The map key at the bottom of Layers: what the lines, circles and tags on the map mean.
  function renderMapKey() {
    const icon = (svg) => `<svg class="key-icon" viewBox="0 0 28 18" aria-hidden="true">${svg}</svg>`;
    const line = (color) => icon(`<path d="M2 9h24" stroke="${color}" stroke-width="3" stroke-dasharray="5 4" fill="none"/>`);
    const boat = '<path d="M14 3v9M14 3l5 8h-5M9 12h11l-2 3h-7z" fill="currentColor" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round"/>';
    const rows = [
      [line(ZONE_STYLE.inner.color), "Inner route line: a ship crossing it counts as an inner passage"],
      [line(ZONE_STYLE.outer.color), "Outer route line"],
      [icon(`<path d="M5 4h18v10H5z" stroke="${ZONE_STYLE.phone.color}" stroke-width="2.5" stroke-dasharray="5 4" fill="${ZONE_STYLE.phone.color}" fill-opacity="0.12"/>`), "My area: alerts only while your phone is in it"],
      [icon('<circle cx="14" cy="9" r="6" fill="#2563eb" stroke="#fff" stroke-width="2.5"/>'), "Your phone, from Home Assistant"],
      [icon('<circle cx="14" cy="9" r="6" fill="#0ea5e9" stroke="#fff" stroke-width="2.5"/>'), "You, from this device's GPS (locate button)"],
      [icon('<path d="M14 17 C14 17 8 11 8 7.5 A6 6 0 0 1 20 7.5 C20 11 14 17 14 17Z" fill="#2563eb" stroke="#fff" stroke-width="1.5"/>'), "Your pin (long-press the map to add one)"],
      [icon('<circle cx="14" cy="9" r="8" fill="#16a34a"/><text x="14" y="12.5" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">6</text>'), "Weather station: wind in m/s, coloured by strength"],
      [icon('<rect x="2" y="3" width="24" height="12" rx="4" fill="#0f766e"/><text x="14" y="12.5" text-anchor="middle" font-size="9" font-weight="700" fill="#fff">1.4 m</text>'), "Wave buoy (wave height) or sea temperature"],
      [icon('<rect x="5" y="3" width="18" height="12" rx="4" fill="#64748b"/><text x="14" y="12.5" text-anchor="middle" font-size="9" font-weight="700" fill="#fff">4°</text>'), "Station without wind: air temperature"],
      [icon(`<g style="color:${HARBOUR_RED}">${boat}</g>`), "Harbour on the chart"],
      [icon(`<rect x="7" y="0.5" width="14" height="17" rx="4" fill="#1d4ed8"/><g style="color:#fff" transform="translate(0.3 0.2) scale(1)">${boat}</g>`), "Harbour on the map and satellite views"],
      [icon('<path d="M3 4h22v10H3z" stroke="#2f7d3d" stroke-width="2" fill="#3f9a4f" fill-opacity="0.25"/>'), "Nature reserve, highlighted (tap one on the drawn chart for its card)"],
      [icon('<path d="M3 4h22v10H3z" stroke="#c62828" stroke-width="2" stroke-dasharray="5 3" fill="#e53935" fill-opacity="0.15"/>'), "Closed now: a bird or seal sanctuary's no-landing or no-access season"],
      [icon('<circle cx="14" cy="9" r="7" fill="#fff" stroke="#dc2626" stroke-width="2.5"/><text x="14" y="12.5" text-anchor="middle" font-size="10" font-weight="800" fill="#dc2626">!</text>'), "Faulty light or buoy"],
    ];
    const list = document.createElement("ul");
    list.className = "key-list small";
    for (const [svg, text] of rows) {
      const li = document.createElement("li");
      li.innerHTML = `${svg}<span>${esc(text)}</span>`;
      list.append(li);
    }
    const speed = document.createElement("div");
    speed.className = "key-block small";
    speed.innerHTML = `<span class="muted">A ship's track, by speed</span><div class="speed-scale"><i style="background:${SPEED_GRADIENT}"></i><span class="small muted"><span>0</span><span>${SPEED_MAX} kn</span></span></div>`;
    const wind = document.createElement("div");
    wind.className = "key-block small";
    wind.innerHTML = '<span class="muted">Wind colours</span>';
    wind.append(windLegend());
    const air = document.createElement("div");
    air.className = "key-block small";
    air.innerHTML = `<span class="muted">Rescue aircraft</span><ul class="legend">${Object.entries(AIRCRAFT_COLORS)
      .map(([k, c]) => `<li><i style="background:${c}; clip-path:none; border-radius:50%"></i>${esc(k)}</li>`).join("")}</ul>`;
    const traffic = document.createElement("div");
    traffic.className = "key-block small";
    traffic.innerHTML = '<span class="muted">Other ships</span>';
    traffic.append(trafficLegend());
    $("map-key").replaceChildren(list, speed, wind, air, traffic);
  }

  // --- weather on the map: its own button and panel ---------------------------------------
  // Sea conditions (the pill), the stations and the overlays, each a big switch with its
  // opacity; the button lights up while any weather is drawn on the map.

  function renderWeather() {
    const w = lastState?.weather || {};
    const weatherOn = w.enabled !== false;
    const lit = stationsOn || [...overlays.entries.values()].some((e) => e.on);
    $("weather-button").classList.toggle("lit", lit);
    if ($("weather-sheet").hidden) return;
    const onTop = document.createElement("h3");
    onTop.className = "wx-heading";
    onTop.textContent = "On top";
    $("weather-list").replaceChildren(
      overlays.fieldChips(),
      skyButton(),
      onTop,
      ...overlays.rows(),
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
        onChange: async (on) => { setStations(on); renderWeather(); },
      }),
    );
  }

  // The night sky (sky.js): a star map for where the phone was last seen (or the map's
  // centre), and a note on the map once a night when something up there is worth seeing.
  const skyPlace = (c, label) => ({ lat: c.lat, lon: c.lng ?? c.lon, label });
  const sky = FerrySky.create({
    esc,
    where() {
      const ph = lastState?.phone;
      if (ph && now() - ph.ts < 3 * 86400) return skyPlace(ph, "Where your phone was last seen");
      return skyPlace(map.getCenter(), "At the map's centre (no phone position)");
    },
    mapCentre: () => skyPlace(map.getCenter(), "At the map's centre"),
  });
  window.ferrySky = sky;

  function skyButton() {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sky-open-button";
    b.innerHTML = `<span aria-hidden="true">✦</span><span><strong>Night sky</strong><small>Stars, planets and what's up tonight</small></span><span aria-hidden="true">›</span>`;
    b.addEventListener("click", () => { toggleWeather(false); sky.open(); });
    return b;
  }

  function toggleWeather(open = $("weather-sheet").hidden) {
    $("weather-sheet").hidden = !open;
    document.body.classList.toggle("weather-open", open);
    $("weather-button").setAttribute("aria-expanded", String(open));
    if (open && !$("conditions-sheet").hidden) toggleConditions(false);
    if (open && SIDE.matches === false && menu.open) menu.close();
    renderWeather();
  }
  $("weather-button").addEventListener("click", () => toggleWeather());
  $("weather-close").addEventListener("click", () => toggleWeather(false));

  // --- settings -------------------------------------------------------------------

  function renderSettings() {
    renderMemory();
    renderSources();
    const problems = overlays.problems();
    $("overlay-status").hidden = !problems.length;
    setText($("overlay-status"), problems.length
      ? `Weather layers that couldn't be read just now: ${problems.join("; ")}. They try again in a few minutes.` : "");
    const checks = overlays.checks();
    $("overlay-checks").hidden = !checks.length;
    setText($("overlay-checks"), checks.join(" "));
  }

  // Memory as a bar: the parts holding data and the program, the peak marked; the split by
  // part behind a tap.
  const MEM_NAMES = { chart: "Chart", "depth model": "Foreign depths", database: "Database", ships: "Ships", aircraft: "Aircraft" };
  const MEM_COLORS = ["#2563eb", "#0ea5e9", "#14b8a6", "#f59e0b", "#ec4899", "#8b5cf6"];
  function renderMemory() {
    const mem = lastState?.memory;
    const mb = mem?.total ?? lastState?.memory_mb;
    $("memory-box").hidden = !mb;
    if (!mb) return;
    const parts = (mem?.parts || []).filter(([, v]) => v >= 0.1).map(([k, v]) => [MEM_NAMES[k] || k, v]);
    if (mem?.program != null) parts.push(["Program (Python and libraries)", mem.program]);
    const peak = Math.max(mb, mem?.peak || 0);
    const scale = peak * 1.08;
    setText($("memory-text"), `${Math.round(mb)} MB now${mem?.peak > mb ? ` · ${Math.round(mem.peak)} MB at most` : ""}`);
    const bar = $("memory-bar");
    bar.innerHTML = parts.map(([, v], i) => `<i style="width:${((100 * v) / scale).toFixed(1)}%; background:${MEM_COLORS[i % MEM_COLORS.length]}"></i>`).join("")
      + `<b style="left:${((100 * peak) / scale).toFixed(1)}%" title="Peak"></b>`;
    $("memory-parts").innerHTML = parts.map(([k, v], i) => `<li><i style="background:${MEM_COLORS[i % MEM_COLORS.length]}"></i>${esc(k)}<span>${v.toFixed(v < 10 ? 1 : 0)} MB</span></li>`).join("")
      + '<li class="muted">Measured on the NAS. The peak is the most since the app started.</li>';
  }

  // Every source the app reads, with a dot: green answering, red failing, grey off.
  function renderSources() {
    const st = lastState;
    if (!st) return;
    const rows = [];
    const ok = (ts) => (ts ? `OK, ${ago(ts)}` : "OK");
    const feed = st.feed || {};
    if (st.simulated) rows.push(["Ship positions (Digitraffic)", "ok", "Simulated data"]);
    else {
      const quiet = feed.last_message_ts && now() - feed.last_message_ts > 300;
      rows.push(["Ship positions (Digitraffic)", feed.connected && !quiet ? "ok" : "bad",
        feed.connected ? (quiet ? `Connected, but nothing since ${fmtWhen(feed.last_message_ts)}` : ok(feed.last_message_ts)) : "Not connected; it keeps trying"]);
    }
    const pc = st.port_calls || {};
    rows.push(["Port calls (Digitraffic)", pc.error ? "bad" : pc.fetched ? "ok" : "off",
      pc.error ? `Failing: ${pc.error}` : pc.fetched ? `${ok(pc.fetched)} · ${pc.calls} calls` : "Waiting for the first fetch"]);
    rows.push(["Home Assistant", st.ha_connected ? "ok" : "bad", st.ha_connected ? "Connected" : "Not connected: no alerts or phone position"]);
    const w = st.weather || {};
    rows.push(["Sea conditions (FMI)", w.enabled === false ? "off" : w.error ? "bad" : "ok",
      w.enabled === false ? "Off" : w.error ? `Failing: ${w.error}` : ok(w.fetched)]);
    rows.push(["Weather stations (FMI)", !stationsOn ? "off" : stationsData?.error ? "bad" : "ok",
      !stationsOn ? "Off" : stationsData?.error ? `Failing: ${stationsData.error}` : stationsData ? "OK" : "Waiting for the first reading"]);
    for (const e of overlays.entries.values()) {
      rows.push([e.ov.title, !e.on ? "off" : e.ov.error ? "bad" : "ok", !e.on ? "Off" : e.ov.error ? `Failing: ${e.ov.error}` : "OK"]);
    }
    for (const sv of st.vchart?.services || []) {
      rows.push([`Chart: ${sv.name}`, sv.failing ? "bad" : "ok",
        sv.failing ? `Failing since ${fmtWhen(sv.failing)}${sv.ok ? ` (last OK ${fmtWhen(sv.ok)})` : ""}` : ok(sv.ok)]);
    }
    for (const sv of st.satellite || []) {
      if (sv.failing) rows.push([sv.name, "bad", `Failing: ${sv.failing}`]);
    }
    if (st.vchart && !st.vchart.mml_key) rows.push(["Chart: Land Survey (rocks, names, land)", "off", "No API key (see Drawn chart above)"]);
    const air = st.aircraft || {};
    rows.push(["Rescue aircraft (adsb.fi)", !air.enabled ? "off" : air.error ? "bad" : "ok", !air.enabled ? "Off" : air.error ? `Failing: ${air.error}` : "OK"]);
    $("sources-status").replaceChildren(...rows.map(([name, state, text]) => {
      const li = document.createElement("li");
      li.className = state;
      li.innerHTML = `<i></i><span class="grow"><span class="small strong">${esc(name)}</span><span class="small muted">${esc(text)}</span></span>`;
      return li;
    }));
  }

  // --- wrecks and coastal heritage on the drawn chart ---------------------------------

  let heritageOn = store.get("fw-heritage") !== "0";
  let contoursOn = store.get("fw-contours") !== "0";
  function applyHeritage() {
    try {
      window.FerryChart?.setOptions?.({ heritage: heritageOn, contours: contoursOn });
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
    const at = (v) => `${v.where}, ${now() - v.ts > 86400 ? fmtWhen(v.ts) : clock(v.ts)}`;
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

  // Down to zoom 5 (all of Finland): views wider than 60 km get every ship in the box from
  // one shared snapshot on the server, smaller marks the further out.
  const TRAFFIC_MIN_ZOOM = 5;
  const TRAFFIC_CIRCLE_KM = 60;
  const trafficSize = (z) => (z >= 9 ? 7 : z >= 7 ? 5.5 : 4.5);
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

  // Free ship sites, by MMSI. VesselFinder first: photo, particulars and the last port calls
  // without signing in; MarineTraffic hides much of it behind a login now.
  const SHIP_SITES = [
    ["VesselFinder", (m) => `https://www.vesselfinder.com/vessels/details/${m}`],
    ["MarineTraffic", (m) => `https://www.marinetraffic.com/en/ais/details/ships/mmsi:${m}`],
  ];
  function shipLinks(mmsi) {
    const out = [document.createTextNode("Open in ")];
    SHIP_SITES.forEach(([label, url], i) => {
      if (i) out.push(document.createTextNode(" · "));
      const a = document.createElement("a");
      a.href = url(mmsi);
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = label;
      out.push(a);
    });
    return out;
  }

  // Photos of other ships, looked up by the server (Wikimedia, by IMO number) when a popup opens.
  const vesselPhotos = new Map(); // mmsi -> photo | null | Promise
  function vesselPhoto(mmsi) {
    if (vesselPhotos.has(mmsi)) return vesselPhotos.get(mmsi);
    const lookup = api(`api/vessels/${mmsi}/photo`)
      .then((d) => {
        const p = d.photo || { missing: true, why: d.miss?.why, error: d.miss?.error };
        vesselPhotos.set(mmsi, p);
        return p;
      })
      .catch((err) => { vesselPhotos.delete(mmsi); return { missing: true, why: "error", error: String(err.message || err) }; });
    vesselPhotos.set(mmsi, lookup);
    return lookup;
  }
  const PHOTO_MISS = {
    none: "No free photo on Wikimedia. See its photo on VesselFinder ›",
    no_imo: "No IMO number in its AIS data. Look on VesselFinder ›",
  };
  function showVesselPhoto(a, mmsi, photo) {
    const note = a.nextElementSibling?.classList.contains("photo-miss") ? a.nextElementSibling : null;
    if (!photo || photo.missing) {
      a.hidden = true;
      if (note && photo) {
        note.textContent = PHOTO_MISS[photo.why] || `Couldn't look up a photo just now (${photo.error || "network"}).`;
        note.hidden = false;
      }
      return;
    }
    if (note) note.hidden = true;
    a.querySelector("img").src = `api/photo/${mmsi}?v=${photo.version}`;
    a.querySelector(".credit").textContent = `Photo: ${photo.source || "Wikipedia"}`;
    if (photo.credit_url) a.href = photo.credit_url;
    else a.removeAttribute("href");
    a.hidden = false;
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
    const b = map.getBounds().pad(0.1);
    const url = radius > TRAFFIC_CIRCLE_KM
      ? `api/traffic?s=${b.getSouth().toFixed(2)}&w=${b.getWest().toFixed(2)}&n=${b.getNorth().toFixed(2)}&e=${b.getEast().toFixed(2)}`
      : `api/traffic?lat=${c.lat.toFixed(3)}&lon=${c.lng.toFixed(3)}&r=${radius}`;
    trafficBusy = true;
    try {
      const data = await api(url);
      if (!trafficOn) return;
      drawTraffic(data.ships || []);
      trafficNote = data.error ? `Couldn't reach Digitraffic just now (${data.error}).` : "";
      trafficCount = `${traffic.size} in view`;
    } catch (err) {
      trafficNote = String(err.message || err);
    } finally {
      trafficBusy = false;
      renderTrafficRow();
    }
  }

  function drawTraffic(rows) {
    if (!trafficLayer) {
      trafficCanvas = sharedCanvas();
      trafficLayer = L.layerGroup().addTo(map);
    }
    const seen = new Set();
    const size = trafficSize(map.getZoom());
    for (const row of rows) {
      const [mmsi, lat, lon, sog, cog, heading, , type] = row;
      if (ships.has(mmsi)) continue; // already on the map as one of mine or a fleet ship
      seen.add(mmsi);
      const course = sog != null && sog >= 0.5 ? (heading ?? cog) : null;
      const color = aisColor(type);
      let boat = traffic.get(mmsi);
      if (!boat) {
        boat = new Boat([lat, lon], { renderer: trafficCanvas, radius: size, color: "#fff", weight: 1, opacity: 0.9,
          fillColor: color, fillOpacity: 0.85, course });
        boat.bindPopup(() => trafficPopup(boat), { className: "aid-popup", offset: [0, -4] });
        boat.addTo(trafficLayer);
        traffic.set(mmsi, boat);
      } else {
        const changed = boat.options.course !== course || boat.options.fillColor !== color || boat.options.radius !== size;
        boat.options.course = course;
        boat.options.radius = size;
        boat._radius = size;
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
    el.className = "traffic-popup";
    const pic = document.createElement("a");
    pic.className = "ac-photo";
    pic.target = "_blank";
    pic.rel = "noopener";
    pic.hidden = true;
    pic.innerHTML = '<img alt=""><span class="credit"></span>';
    pic.querySelector("img").addEventListener("load", () => boat.getPopup()?.update());
    const photo = vesselPhoto(mmsi);
    if (photo instanceof Promise) photo.then((p) => { showVesselPhoto(pic, mmsi, p); boat.getPopup()?.update(); });
    // No free photo: a link to VesselFinder's page, which shows theirs (their photos may
    // not be copied into the app).
    const miss = document.createElement("a");
    miss.className = "photo-miss small";
    miss.href = SHIP_SITES[0][1](mmsi);
    miss.target = "_blank";
    miss.rel = "noopener";
    miss.hidden = true;
    el.append(pic, miss);
    if (!(photo instanceof Promise)) showVesselPhoto(pic, mmsi, photo);
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
    // Its announced calls at Finnish ports (Portnet), when it has any.
    const ports = document.createElement("div");
    ports.className = "card-ports popup-ports";
    ports.hidden = true;
    el.append(ports);
    fetch(`api/ports/${mmsi}`).then((r) => (r.ok ? r.json() : null)).then((data) => {
      const rows = data?.rows || [];
      if (!rows.length) return;
      ports.replaceChildren(...rows.flatMap((r) => portRowCells(r)));
      ports.hidden = false;
      boat.getPopup()?.update();
    }).catch(() => {});
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
    const links = document.createElement("div");
    links.className = "ext-links small";
    links.append(...shipLinks(mmsi));
    el.append(document.createElement("br"), follow, links);
    return el;
  }

  map.on("zoomend", () => {
    const size = trafficSize(map.getZoom());
    for (const boat of traffic.values()) if (boat.options.radius !== size) boat.setRadius(size);
  });
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
    const day = since && `${fmtDay(f.since)}${now() - f.since > 300 * 86400 ? ` ${since.getFullYear()}` : ""}`;
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
  // Tap the map to add stops (or find a harbour by name). Each new leg goes straight or
  // along the marked fairways, as the switch at the top of the panel says; the add-on plans
  // fairway legs from the drawn chart's fairway lines (see routeplan.js).
  // Changing the way the route goes:
  // - drag any dot on it: a filled one (a turn or a bend) moves, a hollow one (halfway along
  //   a piece) pulls the line out. Let go on another fairway (the fairways in reach show
  //   while dragging, and the dot snaps to them) and the route is planned again through
  //   that point, along that fairway: a via point. Let go anywhere else and it's a bend off
  //   the fairways, with straight lines to the turns either side; nothing else moves.
  // - tap a point or the line, "Straight from here to…", then tap where to rejoin the route:
  //   the stretch between becomes one straight leg (a loop round an island is cut out).
  // - tap a leg for "Along the fairways" or "Straight line" between your own stops.
  // Moving one of your own stops plans its automatic fairway legs again. Not a navigation
  // tool. The route being drawn is kept in this browser between visits; saved routes are
  // kept by the add-on.

  const NavM = window.NavMath;
  const Plan = window.RoutePlan;
  const ROUTE_COLOR = "#c026d3";
  const ROUTE_ERROR = "#dc2626";
  const ROUTE_MAX_POINTS = 500;
  const HANDLE_MIN_PX = 84; // a piece this long on screen gets a hollow dot to pull it by
  const TURN_GAP_PX = 26; // fairway turns closer than this on screen to the last dot shown aren't given one
  const SNAP_PX = 24; // a dot dragged this near a fairway snaps onto it
  const FAIRWAY_MIN_Z = 11; // the fairways to snap to are fetched from this zoom
  // drag: the dot being dragged { base: the plan before, op, stop: its index now, snap }
  const route = { on: false, stops: [], legs: [], points: [], undo: [], id: null, name: "", status: null, dragging: false, pick: null, drag: null };
  let routeMode = ["fw", "auto"].includes(store.get("fw-route-mode")) ? store.get("fw-route-mode") : "line"; // for new legs
  const newLegMode = () => (routeMode === "auto" ? "auto" : routeMode === "fw");
  let sailStyle = store.get("fw-sail-style") === "comfort" ? "comfort" : "fast";
  let powerStyle = store.get("fw-power-style") === "direct" ? "direct" : "shelter";
  let routeRoom = ["close", "normal", "wide"].includes(store.get("fw-route-room")) ? store.get("fw-route-room") : "normal";
  let routeSpeed = Number(store.get("fw-route-speed")) || 25;
  let routeDraught = Number(store.get("fw-route-draught")) || 1.5;
  let routeLayer = null; // { group, casing, legs, labels, handles, turns, markers, pick }
  let routePopup = null, routePopupWasOpen = false;
  const routeMarkers = [];
  let savedRoutes = [];
  const savedLayer = L.layerGroup().addTo(map);

  const legState = (l) => ({ fw: Boolean(l.fw), ...(l.auto ? { auto: true } : {}), path: Array.isArray(l.path) ? l.path : [],
    planned: Boolean(l.planned), error: l.error || null, note: l.note || "", info: l.info || null });
  try {
    const draft = JSON.parse(store.get("fw-route-draft") || "null");
    if (Array.isArray(draft?.stops) && Array.isArray(draft.legs) && draft.legs.length === Math.max(0, draft.stops.length - 1)) {
      route.stops = draft.stops;
      route.legs = draft.legs.map(legState);
    } else if (Array.isArray(draft?.points)) { // saved by an older version: straight legs
      Object.assign(route, Plan.fromSaved(draft.points, null));
    }
    if (draft) Object.assign(route, { id: draft.id ?? null, name: draft.name || "" });
  } catch { /* no draft */ }
  route.points = Plan.points(route.stops, route.legs);

  const round6 = (v) => Math.round(v * 1e6) / 1e6;
  const latLngOf = (ll) => [round6(ll.lat), round6(((ll.lng + 540) % 360) - 180)];
  const legPending = (l) => l.fw && (l.busy || !l.planned);
  const unplannedLegs = () => route.legs.map((l, i) => (l.fw && !l.planned ? i : -1)).filter((i) => i >= 0);

  // The number shown on each of the user's own stops (bends and via points have none).
  function stopNumbers() {
    let n = 0;
    return route.stops.map((p) => (Plan.isOwn(p) ? ++n : null));
  }

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
      showFind(false);
      routePopup?.remove();
      if (route.pick) cancelPick();
      rw.layer.remove();
    }
    $("route").hidden = !on;
    $("route-button").setAttribute("aria-pressed", String(on));
    document.body.classList.toggle("route-open", on);
    map.getContainer().classList.toggle("measuring", on);
    $("route-speed").value = String(routeSpeed);
    $("route-draught").value = String(routeDraught);
    $("route-name").value = route.name;
    renderRouteModes();
    if (on) {
      planLegs(unplannedLegs());
      loadFairways();
    } else drawRoute();
    renderSavedRoutes(); // not clickable while measuring, and the one being edited isn't drawn twice
  }

  function stopIcon(i, numbers) {
    if (Plan.isBend(route.stops[i])) return { key: "bend", icon: bendIcon };
    if (Plan.isVia(route.stops[i])) return { key: "via", icon: viaIcon };
    const kind = i === 0 ? " start" : i === route.stops.length - 1 ? " end" : "";
    return {
      key: `${kind}/${numbers[i]}`,
      icon: L.divIcon({ className: "rp-icon", html: `<span class="rp${kind}">${numbers[i]}</span>`, iconSize: [36, 36], iconAnchor: [18, 18] }),
    };
  }
  // Filled: a point to drag (a fairway turn or a bend). Hollow: pull the line out from here.
  const bendIcon = L.divIcon({ className: "rp-bend-icon", html: '<span class="rp-bend"></span>', iconSize: [28, 28], iconAnchor: [14, 14] });
  const midIcon = L.divIcon({ className: "rp-mid-icon", html: '<span class="rp-mid"></span>', iconSize: [30, 30], iconAnchor: [15, 15] });
  // A via point: the route is planned through it, along the fairway it's on.
  const viaIcon = L.divIcon({ className: "rp-via-icon", html: '<span class="rp-via"></span>', iconSize: [30, 30], iconAnchor: [15, 15] });

  function snapshot() {
    return JSON.stringify({ stops: route.stops, legs: route.legs.map((l) => legState({ ...l, planned: l.planned && !l.busy })) });
  }

  function pushUndo() {
    route.undo.push(snapshot());
    if (route.undo.length > 60) route.undo.shift();
  }

  function restore(snap) {
    const back = JSON.parse(snap);
    route.stops = back.stops;
    route.legs = back.legs.map(legState);
  }

  function undoRoute() {
    if (!route.undo.length) return;
    restore(route.undo.pop());
    route.status = null;
    route.pick = null;
    routePopup?.remove();
    planLegs(unplannedLegs());
  }

  function addRoutePoint(ll) {
    if (route.pick) { cancelPick(); return; }
    if (route.points.length >= ROUTE_MAX_POINTS) return;
    pushUndo();
    route.status = null;
    const todo = Plan.addStop(route.stops, route.legs, latLngOf(ll), newLegMode());
    planLegs(todo, { fit: route.stops.length === 2 });
  }

  // Fairway legs are planned one at a time by the add-on. A leg changed meanwhile is a new
  // leg object, so an answer for the old one is dropped. While chart areas along the way are
  // still downloading, the leg is asked for again a little later.
  let planChain = Promise.resolve();
  let planSeq = 0;
  function planLegs(list, { fit = false, tries = 0 } = {}) {
    for (const i of list) {
      const leg = route.legs[i];
      if (!leg?.fw) continue;
      Object.assign(leg, { busy: true, planned: false, path: [], error: null, note: "", info: null, seq: ++planSeq });
      const seq = leg.seq;
      planChain = planChain.then(() => planLeg(leg, seq, fit, tries)).catch(console.error);
    }
    drawRoute();
  }

  async function planLeg(leg, seq, fit, tries) {
    const i = route.legs.indexOf(leg);
    if (i < 0 || leg.seq !== seq) return;
    const a = route.stops[i], b = route.stops[i + 1];
    // Bends and via points are on (or by) a fairway: the leg keeps to that one there.
    const on = [Plan.isOwn(a) ? "" : "from", Plan.isOwn(b) ? "" : "to"].filter(Boolean).join(",");
    let res;
    try {
      res = leg.auto
        ? await api("api/smartroute", { method: "POST", body: {
          from: [a[0], a[1]], to: [b[0], b[1]], boat: routeBoat, draught: routeDraught, speed: routeSpeed,
          depart: departFor(i), style: isSail() ? sailStyle : powerStyle, room: routeRoom } })
        : await api(`api/autoroute?from=${a[0]},${a[1]}&to=${b[0]},${b[1]}&draught=${routeDraught}${on ? `&on=${on}` : ""}`);
    } catch (err) {
      res = { error: String(err.message || err) };
    }
    if (!route.legs.includes(leg) || leg.seq !== seq) return;
    leg.busy = false;
    leg.planned = true;
    if (res.error) {
      leg.error = res.error;
      if (res.downloading && tries < 4) {
        setTimeout(() => {
          const k = route.legs.indexOf(leg);
          if (k >= 0 && leg.seq === seq && route.on) planLegs([k], { fit, tries: tries + 1 });
        }, 20000);
      }
    } else {
      leg.path = (res.points || []).slice(1, -1).map((p) => [p[0], p[1]]);
      if (leg.auto) {
        leg.info = { hows: res.hows || "", nm: res.nm || {}, seconds: res.seconds, shallow: res.shallow_nm || 0, style: res.style || null,
          alt: res.alt || null, depart: res.depart || null, sail: isSail() && res.style !== "motor", cell: res.cell_m,
          shelter: res.shelter || null, exposed: res.exposed_nm, trunk: res.trunk_nm || 0, fairway: res.fairway_nm || 0, checks: res.checks || [], near: res.near_m || 100, metres: res.metres };
      }
      leg.note = [...(res.warnings || []), res.downloading && !leg.auto ? "Some chart areas are still downloading: tap the leg and plan it again in a minute for a better way." : ""]
        .filter(Boolean).join(" ");
    }
    drawRoute();
    if (fit && !res.error && route.legs.length === 1) map.fitBounds(L.latLngBounds(route.points), { ...routePadding(), maxZoom: 13 });
  }

  // When the boat is at the start of leg i: the hour picked in Boating weather (or now), and
  // the legs before it at their planned times.
  function departFor(i) {
    const h = rw.data?.hours?.[rw.sel];
    let t = h && h.t > now() ? h.t : now();
    for (let k = 0; k < i; k++) t += legSeconds(k) || 0;
    return Math.round(t);
  }

  // A leg's time: a sailing leg's as planned from the forecast, the rest at the speed set.
  function legSeconds(k) {
    const leg = route.legs[k];
    if (leg?.info?.sail && leg.info.seconds) return leg.info.seconds;
    return NavM.etaSeconds(NavM.totalNm(Plan.legPoints(route.stops, route.legs, k)), routeSpeed);
  }
  const routeSeconds = (a = 0, b = route.legs.length) => {
    let t = 0;
    for (let k = a; k < b; k++) {
      const v = legSeconds(k);
      if (v == null) return null;
      t += v;
    }
    return t;
  };

  // --- dragging: stops, fairway turns, bends and via points move; hollow dots pull the line out ---
  // Each step of a drag starts again from the plan as it was (route.drag.base) and does what
  // letting go there would do, so what's drawn is what you get. Off the fairways that's a
  // bend; snapped onto one, the line is drawn to the bend dashed, and on letting go the route
  // is planned again through a via point there. The user's numbered stops never snap.
  function routeDragStart() {
    route.dragging = true;
    route.pick = null;
    routePopup?.remove();
    map.getContainer().classList.add("route-dragging");
  }
  function routeDragEnd() {
    route.dragging = false;
    map.getContainer().classList.remove("route-dragging");
  }

  // op: { kind: "stop", i } | { kind: "turn", leg, k } | { kind: "mid", leg, k }
  function beginDrag(op) {
    pushUndo();
    const i = op.kind === "stop" ? op.i : -1;
    route.drag = { base: route.undo[route.undo.length - 1], op, stop: i, snap: null,
      snaps: op.kind !== "stop" || !Plan.isOwn(route.stops[i]) };
    routeDragStart();
    showFairways(route.drag.snaps);
  }

  // The plan with the dot let go at p, off the fairways: the dot's stop index.
  function dragOffFairway(op, p) {
    const { stops, legs } = route;
    if (op.kind === "mid") return Plan.insertBend(stops, legs, op.leg, op.k, p);
    if (op.kind === "turn") {
      const i = Plan.turnStop(stops, legs, op.leg, op.k);
      Plan.moveStop(stops, legs, i, p);
      return i;
    }
    if (Plan.isVia(stops[op.i])) return Plan.viaToBend(stops, legs, op.i, p);
    Plan.moveStop(stops, legs, op.i, p);
    return op.i;
  }

  // The plan with the dot let go on a fairway at p: a via point there.
  function dragOntoFairway(op, p) {
    const { stops, legs } = route;
    if ((op.kind === "mid" || op.kind === "turn") && legs[op.leg].fw) Plan.insertVia(stops, legs, op.leg, p);
    else if (op.kind === "mid") Plan.toVia(stops, legs, Plan.insertBend(stops, legs, op.leg, op.k, p), p);
    else if (Plan.isVia(stops[op.i])) Plan.moveStop(stops, legs, op.i, p);
    else Plan.toVia(stops, legs, op.i, p);
  }

  function dragTo(ll, ev) {
    const d = route.drag;
    if (!d) return;
    restore(d.base);
    // Shift or Alt held (on a computer): no snapping, for a bend right by a fairway.
    const free = ev && (ev.shiftKey || ev.altKey);
    d.snap = d.snaps && !free ? snapToFairway(ll) : null;
    d.stop = dragOffFairway(d.op, d.snap ? d.snap.at : latLngOf(ll));
    drawSnap();
    scheduleRouteDraw();
  }

  function finishDrag() {
    const d = route.drag;
    if (!d) return;
    route.drag = null;
    if (d.snap) {
      restore(d.base);
      dragOntoFairway(d.op, d.snap.at);
    }
    showFairways(false);
    drawSnap();
    routeDragEnd();
    planLegs(unplannedLegs()); // and the legs that were being planned when the drag began
  }

  function makeRouteMarker() {
    const m = L.marker([0, 0], { draggable: true, keyboard: false, zIndexOffset: 2000, autoPan: true });
    m.on("dragstart", () => {
      m.dragged = true;
      beginDrag({ kind: "stop", i: routeMarkers.indexOf(m) });
    });
    m.on("drag", (e) => dragTo(m.getLatLng(), e.originalEvent));
    // Leaflet may still send a click when a drag ends; that must not open the menu.
    m.on("dragend", () => {
      setTimeout(() => { m.dragged = false; }, 60);
      finishDrag();
    });
    m.on("click", () => {
      if (m.dragged) return;
      const i = routeMarkers.indexOf(m);
      if (i >= 0) pointTapped({ stop: i }, route.stops[i]);
    });
    return m;
  }

  // A dot on the line: a fairway turn, or halfway along a piece.
  function makeDragDot(at, icon, title, op, onTap) {
    const d = L.marker(at, { icon, draggable: true, keyboard: false, zIndexOffset: 1800, autoPan: true, title });
    d.on("dragstart", () => {
      d.dragged = true;
      beginDrag(op);
    });
    d.on("drag", (e) => dragTo(d.getLatLng(), e.originalEvent));
    d.on("dragend", () => {
      setTimeout(() => { d.dragged = false; }, 60);
      finishDrag();
    });
    d.on("click", () => { if (!d.dragged) onTap(d.getLatLng()); });
    return d;
  }

  // Turn k of fairway leg i.
  const makeTurn = (i, k, at) => makeDragDot(at, bendIcon, "Drag to move this turn, or onto another fairway",
    { kind: "turn", leg: i, k }, (ll) => pointTapped({ leg: i, turn: k }, ll));
  // Halfway along piece k of leg i.
  const makeHandle = (i, k, at) => makeDragDot(at, midIcon, "Drag to bend the route here, or onto another fairway",
    { kind: "mid", leg: i, k }, (ll) => lineTapped(i, ll));

  // --- the fairways to snap a dragged dot onto: those deep enough in and around the view ---
  const fairways = { lines: [], bounds: null, draught: null, zoom: null, px: null, seq: 0 };
  let fairwayTimer = 0;
  function loadFairways() {
    clearTimeout(fairwayTimer);
    if (!route.on || map.getZoom() < FAIRWAY_MIN_Z) return;
    const view = map.getBounds();
    if (fairways.bounds && fairways.draught === routeDraught && fairways.bounds.contains(view)) return;
    fairwayTimer = setTimeout(async () => {
      const b = map.getBounds().pad(0.4), draught = routeDraught, seq = ++fairways.seq;
      let res;
      try {
        res = await api(`api/fairways?bbox=${[b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((v) => v.toFixed(4)).join(",")}&draught=${draught}`);
      } catch {
        return;
      }
      if (seq !== fairways.seq) return;
      // Kept for this area unless some of it was still downloading (asked again on the next move).
      Object.assign(fairways, { lines: res.lines || [], bounds: res.missing || res.too_big ? null : b, draught, zoom: null, px: null });
    }, 400);
  }
  map.on("moveend", loadFairways);

  // The nearest point on a fairway within SNAP_PX of a place: { at: [lat, lon] } or null.
  function snapToFairway(ll) {
    if (!fairways.lines.length || fairways.draught !== routeDraught || map.getZoom() < FAIRWAY_MIN_Z) return null;
    const z = map.getZoom();
    if (fairways.zoom !== z) { // the lines in pixels at this zoom, with each line's box
      fairways.px = fairways.lines.map((line) => {
        const pts = line.map((p) => map.project(p, z));
        const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
        return { pts, x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
      });
      fairways.zoom = z;
    }
    const p = map.project(ll, z);
    let best = null, bestD = SNAP_PX;
    for (const line of fairways.px) {
      if (p.x < line.x0 - bestD || p.x > line.x1 + bestD || p.y < line.y0 - bestD || p.y > line.y1 + bestD) continue;
      for (let k = 0; k + 1 < line.pts.length; k++) {
        const foot = L.LineUtil.closestPointOnSegment(p, line.pts[k], line.pts[k + 1]);
        const d = foot.distanceTo(p);
        if (d < bestD) { bestD = d; best = foot; }
      }
    }
    return best ? { at: latLngOf(map.unproject(best, z)) } : null;
  }

  // While a dot that can snap is dragged: the fairways it can go onto, faintly, and a ring
  // where it would snap.
  let fairwayGlow = null, snapRing = null;
  function showFairways(on) {
    fairwayGlow?.remove();
    fairwayGlow = null;
    if (on && fairways.lines.length && fairways.draught === routeDraught && map.getZoom() >= FAIRWAY_MIN_Z) {
      fairwayGlow = L.polyline(fairways.lines, { color: ROUTE_COLOR, weight: 7, opacity: 0.24, interactive: false, lineCap: "round" }).addTo(map);
    }
  }
  function drawSnap() {
    const at = route.drag?.snap?.at;
    if (!at) { snapRing?.remove(); snapRing = null; return; }
    if (!snapRing) snapRing = L.circleMarker(at, { radius: 14, color: ROUTE_COLOR, weight: 3, fill: true, fillColor: "#fff", fillOpacity: 0.35, interactive: false }).addTo(map);
    else snapRing.setLatLng(at);
  }

  let routeFrame = 0;
  function scheduleRouteDraw() {
    if (!routeFrame) routeFrame = requestAnimationFrame(() => { routeFrame = 0; drawRoute(); });
  }

  function saveDraft() {
    store.set("fw-route-draft", JSON.stringify({ stops: route.stops, legs: route.legs.map(legState), id: route.id, name: route.name }));
  }

  function drawRoute() {
    route.points = Plan.points(route.stops, route.legs);
    if (!route.on) {
      routeLayer?.group.remove();
      saveDraft();
      return;
    }
    if (!routeLayer) {
      const group = L.layerGroup();
      routeLayer = {
        group,
        casing: L.polyline([], { color: "#fff", weight: 8, opacity: 0.95, lineJoin: "round", lineCap: "round", interactive: false }).addTo(group),
        legs: L.layerGroup().addTo(group),
        labels: L.layerGroup().addTo(group),
        handles: L.layerGroup().addTo(group),
        turns: L.layerGroup().addTo(group),
        markers: L.layerGroup().addTo(group),
        checks: L.layerGroup().addTo(group),
        pick: L.layerGroup().addTo(group),
      };
    }
    if (!map.hasLayer(routeLayer.group)) routeLayer.group.addTo(map);
    routeLayer.casing.setLatLngs(route.points);
    drawLegLines();
    if (route.dragging) { // the dot being dragged must stay; the rest catches up on dragend
      renderRoutePanel();
      return;
    }
    const stops = route.stops, numbers = stopNumbers();
    while (routeMarkers.length > stops.length) routeMarkers.pop().remove();
    stops.forEach((p, i) => {
      let m = routeMarkers[i];
      if (!m) {
        m = makeRouteMarker();
        routeMarkers.push(m);
        m.setLatLng(p).addTo(routeLayer.markers);
      }
      const ll = m.getLatLng();
      if (ll.lat !== p[0] || ll.lng !== p[1]) m.setLatLng([p[0], p[1]]);
      const { key, icon } = stopIcon(i, numbers);
      if (m.iconKey !== key) { m.iconKey = key; m.setIcon(icon); m.setZIndexOffset(key === "bend" ? 1900 : 2000); }
    });
    drawDots();
    drawChecks();
    drawPick();
    renderRoutePanel();
    saveDraft();
  }

  // The places an automatic route says to check yourself (close to the shore or a shoal,
  // or over water of uncertain depth): a small sign on the map and a row in the panel.
  function routeChecks() {
    const out = [];
    route.legs.forEach((leg) => {
      if (!leg.auto || legPending(leg) || leg.error) return;
      for (const c of leg.info?.checks || []) {
        out.push({ ...c, text: c.kind === "depth"
          ? `Depth uncertain for ${`${c.m} m`}: the chart only gives a range that may be too shallow`
          : `Within ${leg.info.near} m of the shore or a shoal for ${c.m} m` });
      }
    });
    return out;
  }
  function showCheck(c) {
    map.setView([c.lat, c.lon], Math.max(map.getZoom(), 14));
    L.popup({ className: "check-popup", offset: [0, -6] }).setLatLng([c.lat, c.lon])
      .setContent(`<b>Check this place</b><br>${esc(c.text)}.`).openOn(map);
  }
  function drawChecks() {
    routeLayer.checks.clearLayers();
    for (const c of routeChecks()) {
      L.marker([c.lat, c.lon], { keyboard: false, zIndexOffset: 1200, bubblingMouseEvents: false, title: c.text,
        icon: L.divIcon({ className: `route-check ${c.kind}`, html: "<span>!</span>", iconSize: [18, 18], iconAnchor: [9, 22] }) })
        .addTo(routeLayer.checks).on("click", (e) => { L.DomEvent.stop(e); showCheck(c); });
    }
  }

  // Each leg: straight, along the fairways (a white line down its middle), still being
  // planned (dashed) or not plannable (dashed red). A wide see-through line over each takes
  // the taps.
  function drawLegLines() {
    const group = routeLayer.legs;
    group.clearLayers();
    const snapped = route.drag?.snap ? route.drag.stop : -1; // its legs are planned again on letting go
    route.legs.forEach((leg, i) => {
      const pts = Plan.legPoints(route.stops, route.legs, i);
      if (snapped >= 0 && (i === snapped - 1 || i === snapped)) {
        L.polyline(pts, { color: ROUTE_COLOR, weight: 3.5, opacity: 0.9, dashArray: "2 7", lineCap: "round", interactive: false }).addTo(group);
      } else if (leg.fw && leg.error) {
        L.polyline(pts, { color: ROUTE_ERROR, weight: 3, dashArray: "5 7", interactive: false }).addTo(group);
      } else if (legPending(leg)) {
        L.polyline(pts, { color: ROUTE_COLOR, weight: 3, opacity: 0.75, dashArray: "5 7", interactive: false }).addTo(group);
      } else if (leg.auto && leg.info?.hows?.length === pts.length - 1) {
        drawAutoLeg(group, pts, leg.info.hows);
        if (leg.info.alt && !route.dragging) drawAltLeg(group, leg, i);
      } else {
        L.polyline(pts, { color: ROUTE_COLOR, weight: leg.fw ? 4 : 3.5, opacity: 1, lineJoin: "round", lineCap: "round", interactive: false }).addTo(group);
        if (leg.fw) L.polyline(pts, { color: "#fff", weight: 1.3, opacity: 0.95, interactive: false }).addTo(group);
      }
      if (!route.dragging) {
        const hit = L.polyline(pts, { color: "#000", opacity: 0, weight: 24, interactive: true, bubblingMouseEvents: false }).addTo(group);
        hit.on("click", (e) => { L.DomEvent.stop(e); lineTapped(i, e.latlng); });
      }
    });
  }

  // An automatic leg, stretch by stretch as the add-on says it is travelled (hows, a letter
  // per piece): o open water (white dots down the middle), f along a fairway (a white line),
  // s sailing (dots), b beating (white dashes), g gybing downwind (short dashes), m under
  // engine (plain). A capital letter is water of uncertain depth: amber and dashed.
  const AUTO_MID = { b: "8 6", g: "3 6", f: null };
  function drawAutoLeg(group, pts, hows) {
    let a = 0;
    for (let k = 1; k <= hows.length; k++) {
      if (k < hows.length && hows[k] === hows[a]) continue;
      const run = pts.slice(a, k + 1), c = hows[a], low = c.toLowerCase();
      if (c !== low) {
        L.polyline(run, { color: "#b45309", weight: 4, dashArray: "7 6", lineCap: "butt", interactive: false }).addTo(group);
      } else {
        L.polyline(run, { color: ROUTE_COLOR, weight: 4, lineJoin: "round", lineCap: "round", interactive: false }).addTo(group);
        // Plain over open water and under sail or engine; a fairway has its white middle line.
        if (low === "f" || low === "b" || low === "g") L.polyline(run, { color: "#fff", weight: low === "f" ? 1.3 : 1.7, opacity: 0.95, dashArray: AUTO_MID[low] ?? null, lineCap: "round", interactive: false }).addTo(group);
      }
      a = k;
    }
  }

  // The sailing leg's other route (the comfortable one beside the fastest, or the other way
  // round): thin and dashed, with what it costs; tap it to take it.
  const ALT_COLOR = "#7c3aed";
  function drawAltLeg(group, leg, i) {
    const alt = leg.info.alt, pts = alt.points;
    L.polyline(pts, { color: "#fff", weight: 5, opacity: 0.7, interactive: false }).addTo(group);
    L.polyline(pts, { color: ALT_COLOR, weight: 2.5, dashArray: "5 6", interactive: false }).addTo(group);
    const pick = (e) => { L.DomEvent.stop(e); setSailStyle(alt.style); };
    L.polyline(pts, { color: "#000", opacity: 0, weight: 20, interactive: true, bubblingMouseEvents: false }).addTo(group).on("click", pick);
    const diff = Math.round((alt.seconds - leg.info.seconds) / 60);
    const text = `${{ comfort: "Comfortable", fast: "Fastest", shelter: "Sheltered", direct: "Direct" }[alt.style] || "Other"} ${diff >= 0 ? "+" : "−"}${NavM.duration(Math.abs(diff) * 60)}`;
    if (screenLength(pts) > 140) {
      L.marker(halfwayAt(pts, 0.5), { keyboard: false, bubblingMouseEvents: false,
        icon: L.divIcon({ className: "leg-label alt", html: `<span>${esc(text)}</span>`, iconSize: null }) }).addTo(group).on("click", pick);
    }
  }

  function setSailStyle(style) {
    if (style === "shelter" || style === "direct") {
      powerStyle = style;
      store.set("fw-power-style", style);
    } else if (style === "fast" || style === "comfort") {
      sailStyle = style;
      store.set("fw-sail-style", style);
    } else return;
    planLegs(route.legs.map((l, i) => (l.auto ? i : -1)).filter((i) => i >= 0));
  }

  const px = (p) => map.latLngToContainerPoint(p);
  const screenLength = (pts) => {
    let n = 0;
    for (let k = 1; k < pts.length; k++) n += px(pts[k - 1]).distanceTo(px(pts[k]));
    return n;
  };

  // The dots to drag: each fairway turn (where there's room), a hollow one halfway along
  // every piece long enough on screen, and a label on each section.
  function drawDots() {
    routeLayer.labels.clearLayers();
    routeLayer.handles.clearLayers();
    routeLayer.turns.clearLayers();
    const size = map.getSize();
    const onScreen = (p) => p.x > -40 && p.y > -40 && p.x < size.x + 40 && p.y < size.y + 40;
    route.legs.forEach((leg, i) => {
      const pts = Plan.legPoints(route.stops, route.legs, i);
      const scr = pts.map(px);
      if (leg.fw && !legPending(leg) && !leg.error) {
        let last = scr[0];
        for (let k = 0; k < leg.path.length; k++) {
          const p = scr[k + 1];
          if (p.distanceTo(last) < TURN_GAP_PX || p.distanceTo(scr[scr.length - 1]) < TURN_GAP_PX || !onScreen(p)) continue;
          makeTurn(i, k, pts[k + 1]).addTo(routeLayer.turns);
          last = p;
        }
      }
      if (legPending(leg)) return;
      for (let k = 0; k + 1 < pts.length; k++) {
        if (scr[k].distanceTo(scr[k + 1]) < HANDLE_MIN_PX) continue;
        const mid = scr[k].add(scr[k + 1]).divideBy(2);
        if (onScreen(mid)) makeHandle(i, k, map.containerPointToLatLng(mid)).addTo(routeLayer.handles);
      }
    });
    for (const [a, b] of Plan.sections(route.stops)) {
      const pts = Plan.sectionPoints(route.stops, route.legs, a, b);
      const len = screenLength(pts);
      if (len < 130) continue;
      // Beside the middle, along the line, clear of the hollow dot there.
      const along = halfwayAt(pts, 0.5 + Math.min(0.25, 60 / len));
      const bad = route.legs.slice(a, b).some((l) => l.fw && l.error);
      L.marker(along, {
        interactive: false, keyboard: false, zIndexOffset: 1500, // over the dots, which it doesn't take taps from
        icon: L.divIcon({ className: "leg-label", html: `<span${bad ? ' class="error"' : ""}>${esc(sectionLabel(a, b, pts))}</span>`, iconSize: null }),
      }).addTo(routeLayer.labels);
    }
  }
  map.on("moveend", () => { if (route.on && !route.dragging) drawDots(); });

  function sectionLabel(a, b, pts) {
    const nm = NavM.nm(NavM.totalNm(pts));
    const kind = Plan.sectionKind(route.legs, a, b);
    if (kind === "line") return `${nm} · ${NavM.degrees(NavM.legs(pts)[0].bearing)}`;
    if (kind === "own") return nm;
    const legs = route.legs.slice(a, b);
    if (legs.some((l) => l.error)) return kind === "auto" ? "No way found" : "No fairway way";
    if (legs.some(legPending)) return "Planning…";
    if (kind === "auto") {
      const t = routeSeconds(a, b);
      return legs.some((l) => l.info?.sail) && t != null ? `${nm} · ${NavM.duration(t)}` : `${nm} · auto`;
    }
    return `${nm} · fairway`;
  }

  // The point a share of the way along a line, by distance.
  function halfwayAt(points, share) {
    const legs = NavM.legs(points);
    let left = NavM.totalNm(points) * share;
    for (let i = 0; i < legs.length; i++) {
      if (left <= legs[i].nm && legs[i].nm > 0) {
        const t = left / legs[i].nm, a = points[i], b = points[i + 1];
        return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      }
      left -= legs[i].nm;
    }
    return points[points.length - 1];
  }

  // The piece of leg i nearest a place, and the nearest point on it.
  function nearestPiece(i, ll) {
    const pts = Plan.legPoints(route.stops, route.legs, i);
    const p = px(ll);
    let best = { k: 0, d: Infinity, at: pts[0] };
    for (let k = 0; k + 1 < pts.length; k++) {
      const foot = L.LineUtil.closestPointOnSegment(p, px(pts[k]), px(pts[k + 1]));
      const d = foot.distanceTo(p);
      if (d < best.d) best = { k, d, at: latLngOf(map.containerPointToLatLng(foot)) };
    }
    return best;
  }

  // --- "Straight from here to…": cut a stretch out of the route ---
  // A place on the route: { stop }, a fairway turn { leg, turn }, or a place on a leg's line
  // { leg, at }. Made a stop of its own when needed; returns its index.
  function stopAt(ref) {
    if (ref.stop != null) return ref.stop;
    if (ref.turn != null) return Plan.turnStop(route.stops, route.legs, ref.leg, ref.turn);
    const { k, at } = nearestPiece(ref.leg, ref.at);
    return Plan.insertBend(route.stops, route.legs, ref.leg, k, at);
  }
  const refPosition = (ref) => (ref.stop != null ? ref.stop : ref.leg + 0.5);

  function startPick(ref) {
    pushUndo();
    route.pick = { stop: stopAt(ref), undo: route.undo.length };
    route.status = null;
    drawRoute();
  }

  function finishPick(ref) {
    const before = route.stops.length;
    let from = route.pick.stop;
    const to = stopAt(ref);
    if (refPosition(ref) < from) from += route.stops.length - before;
    route.pick = null;
    if (to === from) {
      route.status = { text: "That's where the cut starts: tap another point on the route." };
      route.pick = { stop: from, undo: route.undo.length };
      drawRoute();
      return;
    }
    Plan.setSection(route.stops, route.legs, Math.min(from, to), Math.max(from, to), false);
    route.status = { text: "Cut straight across. Drag its hollow dot to bend it round anything in the way." };
    drawRoute();
  }

  function cancelPick() {
    const pick = route.pick;
    route.pick = null;
    // Nothing else changed since: take back the stop the cut would have started from.
    if (pick && route.undo.length === pick.undo) undoRoute();
    else drawRoute();
  }

  function drawPick() {
    routeLayer.pick.clearLayers();
    map.getContainer().classList.toggle("route-picking", Boolean(route.pick));
    const p = route.pick && route.stops[route.pick.stop];
    if (p) L.circleMarker([p[0], p[1]], { radius: 17, color: ROUTE_COLOR, weight: 2.5, dashArray: "4 4", fill: false, interactive: false }).addTo(routeLayer.pick);
  }

  function pointTapped(ref, at) {
    if (route.pick) finishPick(ref);
    else pointMenu(ref, at);
  }

  function lineTapped(i, ll) {
    if (route.pick) finishPick({ leg: i, at: ll });
    else sectionMenu(i, ll);
  }

  // --- the little menus for a point and a leg ---
  function openRoutePopup(at, el, offset = [0, -6]) {
    routePopup?.remove();
    routePopup = L.popup({ className: "fw-popup aid-popup route-popup", offset, autoPanPaddingTopLeft: [20, 90] })
      .setLatLng(at).setContent(el).openOn(map);
  }

  function menuButton(text, onClick, primary = false) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = primary ? "primary" : "";
    b.textContent = text;
    b.addEventListener("click", (e) => { e.stopPropagation(); routePopup?.remove(); onClick(); });
    return b;
  }

  function routeMenu(titleText, subText, subError = false) {
    const el = document.createElement("div");
    el.className = "rp-menu";
    const title = document.createElement("strong");
    title.textContent = titleText;
    const sub = document.createElement("div");
    sub.className = `small muted${subError ? " error" : ""}`;
    sub.textContent = subText;
    el.append(title, sub);
    return el;
  }

  function pointMenu(ref, at) {
    const n = route.stops.length;
    const numbers = stopNumbers();
    const p = ref.stop != null ? route.stops[ref.stop] : null;
    let title, remove = "Remove this bend";
    if (ref.turn != null) title = "Fairway turn";
    else if (Plan.isBend(p)) title = "Bend";
    else if (Plan.isVia(p)) [title, remove] = ["Via point", "Remove this via point"];
    else [title, remove] = [ref.stop === 0 ? "Start" : ref.stop === n - 1 ? "End" : `Stop ${numbers[ref.stop]}`, "Remove this stop"];
    const where = L.latLng(at);
    const el = routeMenu(title, Plan.isVia(p) ? `The route goes along the fairway here. ${NavM.position(where.lat, where.lng)}`
      : NavM.position(where.lat, where.lng));
    if (n > 1) el.append(menuButton("Straight from here to…", () => startPick(ref), true));
    el.append(menuButton(remove, () => {
      pushUndo();
      planLegs(Plan.removeStop(route.stops, route.legs, stopAt(ref)));
    }));
    openRoutePopup(at, el, [0, p && Plan.isOwn(p) ? -14 : -8]);
  }

  function sectionMenu(i, at) {
    const [a, b] = Plan.sectionOf(route.stops, i);
    const kind = Plan.sectionKind(route.legs, a, b);
    const pts = Plan.sectionPoints(route.stops, route.legs, a, b);
    const numbers = stopNumbers();
    const legs = route.legs.slice(a, b);
    const error = legs.find((l) => l.fw && l.error)?.error;
    const vias = route.stops.slice(a + 1, b).filter(Plan.isVia).length;
    const planned = kind === "fw" || kind === "auto";
    const sub = kind === "line" ? `Straight, ${NavM.degrees(NavM.legs(pts)[0].bearing)}`
      : kind === "own" ? `Your own line, ${b - a - 1} ${b - a === 2 ? "point" : "points"}`
        : error || (legs.some(legPending) ? (kind === "auto" ? "Planning a way over open water…" : "Planning along the marked fairways…")
          : kind === "auto" ? autoLegText(legs)
            : `Along the marked fairways for ${routeDraught} m draught${vias ? `, through ${vias === 1 ? "your via point" : `your ${vias} via points`}` : ""}`);
    const el = routeMenu(`Leg ${numbers[a]}–${numbers[b]} · ${NavM.nm(NavM.totalNm(pts))}`, sub, planned && Boolean(error));
    const where = L.latLng(at);
    el.append(menuButton("Straight from here to…", () => startPick({ leg: i, at: where }), true));
    if (kind !== "fw" || vias) {
      el.append(menuButton(vias ? "Fairways, the shortest way" : "Along the fairways", () => {
        pushUndo();
        planLegs(Plan.setSection(route.stops, route.legs, a, b, true));
      }));
    }
    if (kind !== "auto") {
      el.append(menuButton("Automatic, over open water", () => {
        pushUndo();
        planLegs(Plan.setSection(route.stops, route.legs, a, b, "auto"));
      }));
    }
    if (planned && (kind === "auto" || error || legs.some((l) => l.note))) {
      el.append(menuButton("Plan it again", () => planLegs(route.legs.map((_, k) => k).slice(a, b))));
    }
    if (kind !== "line") {
      el.append(menuButton("Straight line", () => {
        pushUndo();
        Plan.setSection(route.stops, route.legs, a, b, false);
        drawRoute();
      }));
    }
    el.append(menuButton("Add a point here", () => {
      pushUndo();
      const { k, at: foot } = nearestPiece(i, where);
      Plan.insertBend(route.stops, route.legs, i, k, foot);
      drawRoute();
    }));
    openRoutePopup(at, el);
  }

  // An automatic leg in words: for whom, and how it is travelled.
  // What an automatic route is made of, as short facts for the panel: [text, tone, title].
  function autoFacts(legs) {
    const sum = (f) => legs.reduce((n, l) => n + (f(l.info || {}) || 0), 0);
    const out = [];
    const b = boatInfo();
    if (b) out.push([`${b.title} · needs ${(routeDraught + (b.keel_margin || 0)).toFixed(1)} m`, "", "The water depth the route is planned for: the draught and the room under the keel"]);
    const fw = sum((x) => x.fairway), trunk = sum((x) => x.trunk), shallow = sum((x) => x.shallow);
    if (fw >= 0.1) out.push([`Fairways ${fw.toFixed(1)} NM`, "", "Along marked fairways"]);
    if (trunk >= 0.1) out.push([`Green routes ${trunk.toFixed(1)} NM`, "good", "Of that, on the chart's green small-craft routes"]);
    const sh = legs.map((l) => l.info?.shelter).find((x) => x && x.weight > 0);
    if (sh) {
      const exposed = sum((x) => x.exposed);
      out.push([exposed >= 0.1 ? `Open water ${exposed.toFixed(1)} NM` : "All in shelter", exposed >= 0.1 ? "" : "good",
        "How much of the route lies open to the wind. The route keeps to the shelter of islands where that isn't much further."]);
      out.push([sh.wind_ms != null ? `Wind ${Math.round(sh.wind_ms)} m/s${sh.wind_from != null ? ` ${compass(sh.wind_from)}` : ""} at ${clock(sh.at)}` : "No wind forecast",
        "", sh.wind_ms != null ? "The forecast the shelter was planned for. Pick another hour under Boating weather to plan for that hour's wind."
          : "Planned for shelter from all sides"]);
    }
    const total = sum((x) => x.metres) / 1852;
    if (b?.fuel_lnm && !isSail() && total >= 0.5) {
      out.push([`Fuel about ${Math.max(1, Math.round(total * b.fuel_lnm))} litres`, "", `A rough guess at cruising speed: ${b.fuel_lnm} litres a nautical mile for ${b.title}. Your engine and load decide.`]);
    }
    if (shallow >= 0.1) out.push([`Uncertain depth ${shallow.toFixed(1)} NM`, "warn", "Water the chart only gives as a depth range that may be too shallow: the amber dashed stretches"]);
    return out;
  }

  function autoLegText(legs) {
    const b = boatInfo();
    const sum = {};
    let shallow = 0;
    for (const l of legs) {
      for (const [k, v] of Object.entries(l.info?.nm || {})) sum[k] = (sum[k] || 0) + v;
      shallow += l.info?.shallow || 0;
    }
    const words = { open: "open water", fairway: "along fairways", sail: "sailing", beat: "beating", gybe: "gybing downwind", motor: "under engine" };
    const parts = Object.entries(sum).filter(([, v]) => v >= 0.05).sort((x, y) => y[1] - x[1]).map(([k, v]) => `${words[k] || k} ${v.toFixed(1)} NM`);
    return `Automatic for ${b?.title || "the boat"} (${routeDraught} m draught): ${parts.join(", ") || "planned"}${shallow >= 0.1 ? `; ${shallow.toFixed(1)} NM of uncertain depth` : ""}`;
  }

  function routeSummary(points, speed) {
    const total = NavM.totalNm(points);
    const eta = NavM.etaSeconds(total, speed);
    return { total, eta, text: `${NavM.nm(total)} · ${points.length - 1} ${points.length === 2 ? "leg" : "legs"}${eta != null ? ` · ${NavM.duration(eta)} at ${speed} kn` : ""}` };
  }

  function routeHint() {
    if (route.pick) return { text: "Now tap where to rejoin the route: a stop, a dot or the line. Tap the map to cancel." };
    if (route.status) return route.status;
    const legs = route.legs;
    if (legs.some((l) => l.auto && l.busy)) return { text: isSail() ? "Planning the sailing routes from the wind forecast…" : "Planning a way over open water…" };
    if (legs.some((l) => l.fw && l.busy)) return { text: "Planning along the marked fairways…" };
    const bad = legs.findIndex((l) => l.fw && l.error);
    if (bad >= 0) return { text: `${legs[bad].error} Tap the dashed leg to make it straight.`, error: true };
    const notes = [...new Set(legs.filter((l) => l.fw && l.note).map((l) => l.note))];
    if (route.stops.length < 2) {
      return { text: routeMode === "fw" ? "Tap the start, then the end, or find a harbour by name. The route follows the marked fairways."
        : routeMode === "auto" ? `Tap the start, then the end, or find a harbour by name. The route is planned over water deep enough for ${boatInfo()?.title || "the boat"}, on or off the fairways.`
          : "Tap the map to add points. Drag a point to move it, tap it to remove it." };
    }
    if (legs.some((l) => l.auto)) {
      return { text: "Planning only: check the route on the chart. Drag a dot to change the way, or tap the line for more.",
        facts: autoFacts(legs.filter((l) => l.auto)), notes };
    }
    if (legs.some((l) => l.fw)) {
      return { text: `Fairway legs for ${routeDraught} m draught, planning only: check them on the chart. Drag a dot onto another fairway to go that way, or off the fairways to go straight there.`, notes };
    }
    return null;
  }

  function renderRoutePanel() {
    const pts = route.points, stops = route.stops;
    const { total } = routeSummary(pts, routeSpeed);
    const eta = pts.length > 1 ? routeSeconds() : null;
    setText($("route-total"), pts.length > 1 ? NavM.nm(total) : pts.length ? "1 point" : "Route");
    setText($("route-eta"), pts.length > 1 && eta != null ? `${NavM.duration(eta)} · ETA ${clock(now() + eta)}` : "");
    const hint = routeHint();
    setText($("route-hint"), hint?.text || "");
    $("route-hint").hidden = !hint;
    $("route-hint").classList.toggle("error", Boolean(hint?.error));
    const facts = hint?.facts || [];
    $("route-facts").replaceChildren(...facts.map(([text, tone, title]) => {
      const chip = document.createElement("span");
      chip.className = `route-fact${tone ? ` ${tone}` : ""}`;
      chip.textContent = text;
      if (title) chip.title = title;
      return chip;
    }));
    $("route-facts").hidden = !facts.length;
    const checks = route.pick ? [] : routeChecks();
    $("route-checks").hidden = !checks.length;
    setText($("route-checks-sum"), `${checks.length} ${checks.length === 1 ? "place" : "places"} to check yourself`);
    $("route-checks-list").replaceChildren(...checks.map((c) => {
      const li = document.createElement("li");
      li.className = c.kind;
      li.tabIndex = 0;
      li.textContent = c.text;
      li.addEventListener("click", () => showCheck(c));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") showCheck(c); });
      return li;
    }));
    $("route-room-box").hidden = !(routeMode === "auto" || route.legs.some((l) => l.auto));
    $("route-room").value = routeRoom;
    $("route-reverse").disabled = stops.length < 2 || route.legs.some(legPending);
    const notes = hint?.notes || [];
    $("route-notes").replaceChildren(...notes.map((text) => { const li = document.createElement("li"); li.textContent = text; return li; }));
    $("route-notes").hidden = !notes.length;
    $("route-pick-cancel").hidden = !route.pick;

    const numbers = stopNumbers();
    const items = [];
    const stopRow = (i) => {
      const p = stops[i];
      const li = document.createElement("li");
      li.className = "rp-row";
      const n = document.createElement("span");
      n.className = `rp${i === 0 ? " start" : i === stops.length - 1 && i > 0 ? " end" : ""}`;
      n.textContent = String(numbers[i]);
      const where = document.createElement("span");
      where.className = "grow";
      where.textContent = NavM.position(p[0], p[1]);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "icon-button plain";
      del.setAttribute("aria-label", `Remove stop ${numbers[i]}`);
      del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg>';
      del.addEventListener("click", () => { pushUndo(); planLegs(Plan.removeStop(route.stops, route.legs, i)); });
      where.addEventListener("click", () => map.panTo([p[0], p[1]]));
      li.append(n, where, del);
      return li;
    };
    if (stops.length) items.push(stopRow(0));
    for (const [a, b] of Plan.sections(stops)) {
      const kind = Plan.sectionKind(route.legs, a, b);
      const legs = route.legs.slice(a, b);
      const secPts = Plan.sectionPoints(stops, route.legs, a, b);
      const nm = NavM.totalNm(secPts);
      const busy = legs.some(legPending), bad = legs.some((l) => l.fw && l.error);
      const li = document.createElement("li");
      li.className = `rp-leg ${kind}${bad ? " error" : ""}${busy ? " busy" : ""}`;
      li.tabIndex = 0;
      li.title = "Change this leg";
      const t = routeSeconds(a, b);
      const how = kind === "line" ? NavM.degrees(NavM.legs(secPts)[0].bearing) : kind === "own" ? "own line"
        : bad ? (kind === "auto" ? "no way" : "no fairway") : busy ? "planning…" : kind === "auto" ? "automatic" : "fairway";
      li.innerHTML = `<span class="leg-n">${numbers[a]}–${numbers[b]}</span><strong>${NavM.nm(nm)}</strong><span class="leg-how">${how}</span>${t != null ? `<span class="muted">${NavM.duration(t)}</span>` : ""}`;
      const open = () => {
        const mid = halfwayAt(secPts, 0.5);
        map.panInside(mid, routePadding());
        let leg = a, d = Infinity; // the leg the middle is on
        for (let k = a; k < b; k++) {
          const near = nearestPiece(k, L.latLng(mid)).d;
          if (near < d) { d = near; leg = k; }
        }
        sectionMenu(leg, mid);
      };
      li.addEventListener("click", open);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
      items.push(li, stopRow(b));
    }
    $("route-points").replaceChildren(...items);
    $("route-points").hidden = !items.length;
    $("route-undo").disabled = !route.undo.length;
    $("route-clear").disabled = !stops.length && !route.id;
    $("route-save").disabled = pts.length < 2 || route.legs.some((l) => l.fw && l.busy);
    $("route-save").textContent = route.id ? "Update" : "Save";
    $("route-draught-box").hidden = routeMode === "line" && !route.legs.some((l) => l.fw);
    renderSail();
    scheduleDayScore();
    notices?.route(route.on && pts.length > 1 ? pts : null, routeSpeed,
      !route.legs.some(legPending) && !route.dragging);
    notices?.routeOpen(route.on);
  }

  // --- boating day score: good, okay or bad to set off on the route, hour by hour ---------
  // The add-on follows the route at the speed above and reads the forecast where the boat
  // would be when it's there (wind, gusts, waves, rain, visibility, daylight), against the
  // limits for the boat size picked here (see dayscore.py). The strip is each whole hour of
  // the next two days to leave at; tap one for why, and the stretches that make it okay or
  // bad show on the route.
  const RW_COLORS = ["#16a34a", "#f59e0b", "#dc2626"];
  const RW_WORDS = ["Good", "Okay", "Bad"];
  const RW_NAMES = { wind: "wind", gust: "gusts", wave: "waves", rain: "rain", vis: "visibility", dark: "dark" };
  const rw = { key: "", data: null, error: null, busy: false, sel: null, timer: 0, seq: 0, layer: L.layerGroup() };
  // The boats (boats.py): what each needs and takes. The add-on's list replaces this one.
  let boatList = { boats: {
    buster_m: { title: "Buster M", kind: "power", draught: 0.5, cruise_kn: 20 },
    buster_xl: { title: "Buster XL", kind: "power", draught: 0.6, cruise_kn: 26 },
    cabin8: { title: "Cabin boat 8 m", kind: "power", draught: 0.8, cruise_kn: 25 },
    sail35: { title: "Sailboat 35 ft", kind: "sail", draught: 1.9, cruise_kn: 6 },
  } };
  const OLD_BOATS = { small: "buster_m", medium: "buster_xl", large: "cabin8" };
  let routeBoat = OLD_BOATS[store.get("fw-route-boat")] || store.get("fw-route-boat");
  if (!boatList.boats[routeBoat]) routeBoat = "buster_xl";
  const boatInfo = () => boatList.boats[routeBoat];
  const isSail = () => boatInfo()?.kind === "sail";

  function renderBoats() {
    const sel = $("route-boat");
    sel.replaceChildren(...Object.entries(boatList.boats).map(([id, b]) => {
      const o = document.createElement("option");
      o.value = id;
      o.textContent = b.title;
      return o;
    }));
    sel.value = routeBoat;
    renderNumbers();
  }

  // Every number the planning uses, for looking through: the boats, the sailing boat's
  // speeds, and the planner's own rules.
  function renderNumbers() {
    const box = $("route-numbers-body");
    const P = boatList.planner, S = boatList.sail;
    if (!P || !S) { box.textContent = "The list comes with the add-on's answer."; return; }
    const pair = (v, unit) => `${v[0]}–${v[1]} ${unit}`;
    const rows = Object.values(boatList.boats).map((b) => `<tr><th>${esc(b.title)}</th><td>${b.length} × ${b.beam} m</td><td>${b.draught} m</td><td>${b.depth} m</td><td>${b.cruise_kn} kn</td>`
      + `<td>${pair(b.wind, "m/s")}</td><td>${pair(b.gust, "m/s")}</td><td>${pair(b.wave, "m")}</td></tr>`).join("");
    const polar = Object.entries(S.polar).map(([tws, row]) => `<tr><th>${tws} kn</th>${row.map((v) => `<td>${v}</td>`).join("")}</tr>`).join("");
    const pct = (v) => `${Math.round((v - 1) * 100)} %`;
    box.innerHTML = `<div class="numbers-scroll"><table class="numbers"><thead><tr><th>Boat</th><th>Size</th><th>Draught</th><th>Depth needed</th><th>Cruise</th><th>Wind okay–bad</th><th>Gusts</th><th>Waves</th></tr></thead><tbody>${rows}</tbody></table></div>`
      + `<p>Depth needed is the draught plus room under the keel. Wind, gusts and waves: below the first number the hour is good, from the second it is bad. Picking a boat sets the draught and speed above; change them as you like.</p>`
      + `<p><b>Automatic route.</b> The chart's depth areas become a grid of ${P.cell_steps_m[0]} m cells (wider on long trips, up to ${P.cell_steps_m[P.cell_steps_m.length - 1]} m). Water whose depth range starts at the depth needed or deeper is open. A range that only includes it (0–3 m for a small boat) is used when the boat needs no more than ${Math.round(P.marginal_share * 100)} % of the way into it, counted ${P.marginal_cost}× longer and drawn amber and dashed. Rocks, wrecks and shallow soundings close ${P.rock_clear_m} m round them; the shore is kept ${P.shore_clear_m} m off and the first ${P.near_shore_m} m counts ${pct(P.near_shore_cost)} longer. Fairways with a design draught of the boat's draught plus ${P.fairway_margin_m} m are always open and count shorter than open water: ${Object.values(boatList.boats || {}).map((x) => `${Math.round((1 - x.fairway[0]) * 100)} % for ${x.title} (${Math.round((1 - x.fairway[1]) * 100)} % on the green small-craft routes)`).join(", ")}. Speed limits slow the boat inside their areas. The start and end join open water up to ${P.snap_max_m / 1000} km away.</p>`
      + `<p><b>Shelter.</b> For the open boats and the cabin boat, open water counts longer than water among islands, so the route goes inside the archipelago where that is not much further. With the wind at the boat's “okay” limit it counts ${Object.values(boatList.boats || {}).filter((x) => x.shelter > 0).map((x) => `${pct(1 + x.shelter)} longer for ${x.title}`).join(", ")}; less in light wind, up to twice that in strong wind (the wind forecast for the hour picked under Boating weather). Sheltered means land or shoals within about ${(P.shelter_radius_m / 1000).toFixed(1)} km, and most of all ${(P.shelter_upwind_m / 1000).toFixed(1)} km upwind.</p>`
      + `<p><b>Sailing.</b> Speeds below are the boat's at each angle to the true wind, times ${S.trim} for cruising trim. Closer to the wind than about 40° it tacks, downwind it may gybe: the speed made good, times ${S.tack_loss} for the turns. Engine (${S.motor_kn} kn) under ${S.motor_wind_ms} m/s of wind, when sailing would be slower than ${S.motor_below_kn} kn, and when beating with less than ${S.narrow_m} m to the nearest shoal. The wind comes from the forecast at ${P.wind_grid[0]} × ${P.wind_grid[1]} points over the area, hour by hour; the boat steers in ${P.headings} directions.</p>`
      + `<p>The comfortable route counts time as longer: beating ${pct(S.comfort_beat)}, dead downwind ${pct(S.comfort_run)}, engine ${pct(S.comfort_motor)}, each m/s of wind over ${S.comfort_wind_ms} m/s ${Math.round(S.comfort_wind * 100)} %, each metre of waves over ${S.comfort_wave_m} m ${Math.round(S.comfort_wave * 100)} %.</p>`
      + `<div class="numbers-scroll"><table class="numbers polar"><thead><tr><th>Wind</th>${S.angles.map((a) => `<th>${a}°</th>`).join("")}</tr></thead><tbody>${polar}</tbody></table></div>`
      + `<p class="muted">Boat speed in knots by true wind speed and angle. Planning only, not for navigation.</p>`;
  }

  // The sailing choice: fastest or comfortable, and what the planned legs are made of.
  // A motor boat's two routes: the sheltered one along fairways, and the direct one.
  function renderPower() {
    const legs = route.legs.filter((l) => l.auto && l.info && !l.info.sail);
    const show = route.on && !isSail() && boatInfo()?.shelter > 0 && route.legs.some((l) => l.auto);
    $("route-power").hidden = !show;
    if (!show) return;
    for (const b of $("power-styles").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.style === powerStyle));
    const alts = legs.filter((l) => l.info.alt);
    if (!legs.length) { setText($("power-sum"), ""); return; }
    if (!alts.length) { setText($("power-sum"), "Sheltered and direct are the same route here."); return; }
    const nm = (m) => m / 1852;
    const dNm = alts.reduce((n, l) => n + nm(l.info.alt.metres - l.info.metres), 0);
    const open = alts.reduce((n, l) => n + (l.info.alt.exposed_nm || 0), 0);
    const other = powerStyle === "shelter" ? "direct" : "sheltered";
    setText($("power-sum"), `The ${other} route (dashed purple) is ${Math.abs(dNm).toFixed(1)} NM ${dNm < 0 ? "shorter" : "longer"}, with ${open.toFixed(1)} NM of open water. Tap it to take it.`);
  }

  function renderSail() {
    renderPower();
    const legs = route.legs.filter((l) => l.auto && l.info?.sail);
    const show = route.on && isSail() && route.legs.some((l) => l.auto);
    $("route-sail").hidden = !show;
    if (!show) return;
    for (const b of $("sail-styles").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.style === sailStyle));
    if (!legs.length) { setText($("sail-sum"), route.legs.some(legPending) ? "Reading the wind forecast…" : ""); return; }
    const sum = {};
    let other = 0, mine = 0, alts = 0;
    for (const l of legs) {
      for (const [k, v] of Object.entries(l.info.nm || {})) sum[k] = (sum[k] || 0) + v;
      mine += l.info.seconds;
      other += l.info.alt ? l.info.alt.seconds : l.info.seconds;
      alts += l.info.alt ? 1 : 0;
    }
    const words = { sail: "sailing", beat: "beating", gybe: "gybing", motor: "engine", open: "engine", fairway: "engine" };
    const parts = Object.entries(sum).filter(([, v]) => v >= 0.05).sort((x, y) => y[1] - x[1]).map(([k, v]) => `${words[k] || k} ${v.toFixed(1)} NM`);
    const otherName = sailStyle === "fast" ? "comfortable" : "fastest";
    const diff = Math.round(Math.abs(other - mine) / 60);
    const cmp = alts ? ` The ${otherName} route (dashed purple) takes ${NavM.duration(other)}${diff ? `, ${NavM.duration(diff * 60)} ${other > mine ? "more" : "less"}` : ""}.`
      : " Fastest and comfortable are the same route in this wind.";
    const dep = legs[0].info.depart;
    setText($("sail-sum"), `${NavM.duration(mine)}${dep ? ` leaving ${clock(dep)}` : ""}: ${parts.join(", ")}.${cmp}`);
  }
  let rwOpen = store.get("fw-route-wx") !== "0";

  function scheduleDayScore() {
    const pts = route.points;
    const ready = route.on && pts.length > 1 && !route.legs.some(legPending) && !route.dragging;
    $("route-wx").hidden = !(route.on && pts.length > 1);
    if (!ready) { if (!route.on || pts.length < 2) { rw.key = ""; rw.data = null; drawDayScoreMarks(); } return; }
    const speed = dayScoreSpeed();
    const key = JSON.stringify([pts.map((p) => [Math.round(p[0] * 1e4), Math.round(p[1] * 1e4)]), speed, routeBoat]);
    if (key === rw.key) return;
    clearTimeout(rw.timer);
    rw.timer = setTimeout(() => loadDayScore(key, pts), rw.data ? 900 : 300);
  }

  // The speed the weather is read at along the route: the whole trip's own average, so a
  // sailing route's planned time counts.
  function dayScoreSpeed() {
    const t = routeSeconds(), nm = NavM.totalNm(route.points);
    const v = t > 0 && nm > 0 ? nm / (t / 3600) : routeSpeed;
    return Math.min(60, Math.max(0.5, Math.round(v * 10) / 10));
  }

  async function loadDayScore(key, pts) {
    const seq = ++rw.seq;
    rw.key = key;
    rw.busy = true;
    renderDayScore();
    let data = null, error = null;
    try {
      data = await api("api/day-score", { method: "POST", body: { points: pts, speed: dayScoreSpeed(), boat: routeBoat } });
    } catch (err) {
      error = String(err.message || err).slice(0, 160) || "no answer";
    }
    if (seq !== rw.seq) return;
    rw.busy = false;
    rw.error = error;
    if (data) {
      const keep = rw.data && rw.sel != null ? rw.data.hours[rw.sel]?.t : null;
      rw.data = data;
      const i = keep != null ? data.hours.findIndex((h) => h.t === keep) : -1;
      rw.sel = i >= 0 ? i : data.hours.length ? 0 : null;
    } else if (error) {
      rw.data = null;
      rw.key = ""; // ask again on the next change
    }
    renderDayScore();
  }

  // "today 09–14" for runs of hours of one score, by local day.
  function scoreWindows(hours, score, limit = 2) {
    const out = [];
    let run = null;
    for (const h of hours) {
      const day = fmtDay(h.t);
      if (h.score === score && run && run.day === day && h.t === run.end + 3600) run.end = h.t;
      else if (h.score === score) { run = { day, start: h.t, end: h.t }; out.push(run); }
      else run = null;
    }
    const today = fmtDay(now());
    return out.slice(0, limit).map((r) => {
      const d = r.day === today ? "today" : r.day === fmtDay(now() + 86400) ? "tomorrow" : r.day.split(" ")[0];
      const hh = (t) => String(new Date(t * 1000).getHours()).padStart(2, "0");
      return `${d} ${hh(r.start)}–${hh(r.end + 3600)}`;
    });
  }

  function dayScoreSummary(d) {
    if (!d.hours.length) return "too long a trip for the forecast";
    if (d.hours[0].score === 0 && d.hours[0].t <= now()) return "good to go now";
    const good = scoreWindows(d.hours, 0);
    if (good.length) return `good ${good.join(", ")}`;
    const okay = scoreWindows(d.hours, 1);
    return okay.length ? `no good hours; okay ${okay.join(", ")}` : "bad for the next two days";
  }

  function renderDayScore() {
    const d = rw.data;
    $("rw-toggle").setAttribute("aria-expanded", String(rwOpen));
    $("rw-body").hidden = !rwOpen;
    $("route-boat").value = routeBoat;
    setText($("rw-sum"), rw.busy && !d ? "reading the forecast…" : rw.error ? "no forecast" : d ? dayScoreSummary(d) : "");
    const strip = $("rw-strip");
    if (!d) {
      strip.replaceChildren();
      setText($("rw-detail"), rw.error ? `No forecast for the route: ${rw.error}` : rw.busy ? "Reading the forecast along the route…" : "");
      drawDayScoreMarks();
      return;
    }
    strip.classList.toggle("busy", rw.busy);
    let lastDay = null;
    strip.replaceChildren(...d.hours.map((h, i) => {
      const col = document.createElement("button");
      col.type = "button";
      const hr = new Date(h.t * 1000).getHours();
      const day = fmtDay(h.t);
      col.className = `rw-h s${h.score}${h.dark ? " dark" : ""}${i === rw.sel ? " sel" : ""}${day !== lastDay && i ? " newday" : ""}`;
      col.dataset.day = day !== lastDay ? (day === fmtDay(now()) ? "Today" : day.split(" ")[0]) : "";
      lastDay = day;
      if (hr % 3 === 0) col.innerHTML = `<span class="rw-hr">${String(hr).padStart(2, "0")}</span>`;
      col.title = `${clock(h.t)}: ${RW_WORDS[h.score]}`;
      col.setAttribute("aria-label", `Leave ${fmtWhen(h.t)}: ${RW_WORDS[h.score]}`);
      col.setAttribute("aria-pressed", String(i === rw.sel));
      col.addEventListener("click", () => {
        rw.sel = i;
        renderDayScore();
        // A sailing route depends on when it leaves, and a small boat's on that hour's wind
        // (it keeps to the lee of the islands): planned again for this hour.
        if ((isSail() || boatInfo()?.shelter > 0) && route.legs.some((l) => l.auto)) {
          clearTimeout(rw.replan);
          rw.replan = setTimeout(() => planLegs(route.legs.map((l, k) => (l.auto ? k : -1)).filter((k) => k >= 0)), 500);
        }
      });
      return col;
    }));
    renderDayScoreDetail();
    drawDayScoreMarks();
  }

  function renderDayScoreDetail() {
    const d = rw.data, h = d?.hours[rw.sel];
    const box = $("rw-detail");
    if (!h) { setText(box, d && !d.hours.length ? "The trip is longer than the two days of forecast." : ""); return; }
    const lim = (k) => h.why.find((w) => w.k === k);
    const val = (k, text) => {
      const w = lim(k);
      return w ? `<b class="s${w.lvl}">${text}</b>` : text;
    };
    const parts = [];
    if (h.wind != null) parts.push(`${val("wind", `wind ${Math.round(h.wind)}`)}${h.gust != null ? ` (${val("gust", `gusts ${Math.round(h.gust)}`)})` : ""} m/s${h.dir != null ? ` ${compassName(h.dir)}` : ""}`);
    if (h.wave != null) parts.push(val("wave", `waves ${h.wave.toFixed(1)} m`));
    if (h.rain != null) parts.push(h.rain < 0.1 ? "dry" : val("rain", `rain ${h.rain.toFixed(1)} mm/h`));
    if (h.vis != null && h.vis < 10) parts.push(val("vis", `sight ${h.vis < 1 ? h.vis.toFixed(1) : Math.round(h.vis)} km`));
    if (h.dark) parts.push(val("dark", "dark"));
    if (h.temp != null) parts.push(`${Math.round(h.temp)}°`);
    // Where it's at its worst: "waves worst 11.4 nm out", things worst at one place together.
    const at = new Map();
    for (const w of h.why) {
      if (w.lvl !== h.score || w.nm == null || d.nm <= 1) continue;
      const k = w.nm.toFixed(1);
      at.set(k, [...(at.get(k) || []), RW_NAMES[w.k]]);
    }
    const where = h.score && at.size ? ` Worst ${[...at].map(([nm, ks]) => `${ks.join(" and ").replace(/ and (?=.* and )/g, ", ")} ${nm} nm out`).join("; ")}.` : "";
    const src = { fmi: "FMI HARMONIE", "open-meteo": "Open-Meteo", layers: "weather layers" };
    const notes = [];
    if (h.sheltered) notes.push("Waves counted lower among the islands, where the wave model's sea is a few km off.");
    notes.push(`Forecast: ${src[d.sources.air] || "?"}${d.sources.sea ? `, waves ${src[d.sources.sea]}` : ", no waves"}. Planning only.`);
    box.innerHTML = `<span class="rw-when">Leave ${clock(h.t)}${fmtDay(h.t) !== fmtDay(now()) ? ` ${fmtDay(h.t).split(" ")[0]}` : ""}, arrive ${clock(h.t + d.trip_s)}</span> `
      + `<b class="rw-word s${h.score}">${RW_WORDS[h.score]}</b>: ${parts.join(" · ")}.${where}`
      + `<span class="rw-note muted">${notes.join(" ")}</span>`;
  }

  // The route coloured by what the forecast says for each stretch when the boat is there,
  // leaving at the hour picked: a green, amber or red band under the line, and a dot where
  // it's at its worst.
  map.createPane("routeWx").style.zIndex = 399;
  map.getPane("routeWx").style.pointerEvents = "none";
  function drawDayScoreMarks() {
    rw.layer.clearLayers();
    const d = rw.data, h = d?.hours[rw.sel];
    if (!route.on || !rwOpen || !h || d.samples.length < 2) { rw.layer.remove(); return; }
    if (!map.hasLayer(rw.layer)) rw.layer.addTo(map);
    const pts = route.points, legs = NavM.legs(pts);
    const at = (nm) => { // the point this far along the route, and the leg it's on
      let left = nm;
      for (let i = 0; i < legs.length; i++) {
        if (left <= legs[i].nm || i === legs.length - 1) {
          const t = legs[i].nm > 0 ? Math.min(1, Math.max(0, left / legs[i].nm)) : 0, a = pts[i], b = pts[i + 1];
          return { i, p: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t] };
        }
        left -= legs[i].nm;
      }
      return { i: 0, p: pts[0] };
    };
    const levels = [...h.levels].map(Number);
    let start = 0;
    for (let k = 1; k <= levels.length; k++) {
      if (k < levels.length && levels[k] === levels[start]) continue;
      // Each sample stands for the route halfway to its neighbours.
      const from = start ? (d.samples[start - 1][2] + d.samples[start][2]) / 2 : 0;
      const to = k < levels.length ? (d.samples[k - 1][2] + d.samples[k][2]) / 2 : d.samples[levels.length - 1][2];
      const a = at(from), b = at(to);
      const line = [a.p, ...pts.slice(a.i + 1, b.i + 1), b.p];
      L.polyline(line, { pane: "routeWx", color: RW_COLORS[levels[start]], weight: 14, opacity: levels[start] ? 0.38 : 0.22, lineCap: "butt", lineJoin: "round", interactive: false }).addTo(rw.layer);
      start = k;
    }
    if (h.score) {
      const [lat, lon] = d.samples[h.at];
      L.circleMarker([lat, lon], { radius: 7, color: "#fff", weight: 1.5, fillColor: RW_COLORS[h.score], fillOpacity: 0.95, interactive: false }).addTo(rw.layer);
    }
  }

  function compassName(deg) {
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
  }

  $("rw-toggle").addEventListener("click", () => {
    rwOpen = !rwOpen;
    store.set("fw-route-wx", rwOpen ? "1" : "0");
    renderDayScore();
  });
  // Picking a boat sets its draught and cruising speed, and plans the route again for it.
  $("route-boat").addEventListener("change", (e) => {
    routeBoat = boatList.boats[e.target.value] ? e.target.value : routeBoat;
    store.set("fw-route-boat", routeBoat);
    const b = boatInfo();
    routeDraught = b.draught;
    routeSpeed = b.cruise_kn;
    store.set("fw-route-draught", String(routeDraught));
    store.set("fw-route-speed", String(routeSpeed));
    $("route-draught").value = String(routeDraught);
    $("route-speed").value = String(routeSpeed);
    route.status = null;
    planLegs(route.legs.map((l, i) => (l.fw ? i : -1)).filter((i) => i >= 0));
    loadFairways();
    renderRoutePanel();
  });
  for (const b of $("sail-styles").querySelectorAll("button")) b.addEventListener("click", () => setSailStyle(b.dataset.style));
  for (const b of $("power-styles").querySelectorAll("button")) b.addEventListener("click", () => setSailStyle(b.dataset.style));
  renderBoats();
  api("api/boats").then((got) => {
    if (!got?.boats) return;
    boatList = got;
    if (!boatList.boats[routeBoat]) routeBoat = got.default;
    renderBoats();
    renderRoutePanel();
  }).catch(() => {});

  function renderRouteModes() {
    for (const b of $("route-modes").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.mode === routeMode));
    $("route-modes").title = routeMode === "fw" ? "New legs follow the marked fairways"
      : routeMode === "auto" ? "New legs are planned over water deep enough for the boat, on or off the fairways" : "New legs go straight";
  }

  function routePadding() {
    const panel = $("route");
    if (window.innerWidth <= 560) return { paddingTopLeft: [30, 110], paddingBottomRight: [30, window.innerHeight - panel.getBoundingClientRect().top + 30] };
    return { paddingTopLeft: [panel.offsetWidth + 40, 80], paddingBottomRight: [40, 40] };
  }

  // --- find a harbour by name, to start or end the route there ---
  let findTimer = 0, findSeq = 0;
  function showFind(on) {
    $("route-find").hidden = !on;
    $("route-find-button").setAttribute("aria-expanded", String(on));
    $("route-find-button").classList.toggle("active", on);
    if (on) {
      $("route-find-input").placeholder = route.stops.length ? "Harbour for the next point" : "Harbour to start from";
      $("route-find-input").focus();
    } else {
      $("route-find-input").value = "";
      $("route-find-results").replaceChildren();
    }
  }

  async function findHarbours() {
    const q = $("route-find-input").value.trim();
    const seq = ++findSeq;
    if (q.length < 2) { $("route-find-results").replaceChildren(); return; }
    const c = map.getCenter();
    let rows = [], pending = false;
    try {
      const res = await api(`api/harbours?q=${encodeURIComponent(q)}&near=${c.lat.toFixed(4)},${c.lng.toFixed(4)}`);
      rows = res.harbours || [];
      pending = Boolean(res.pending);
    } catch (err) {
      console.error(err);
    }
    if (seq !== findSeq) return;
    const items = rows.map((row) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      const name = document.createElement("span");
      name.textContent = row[3] || "Harbour";
      const sub = document.createElement("span");
      sub.className = "small muted";
      sub.textContent = `${NavM.nm(NavM.distanceNm([c.lat, c.lng], [row[1], row[2]]))} away`;
      b.append(name, sub);
      b.addEventListener("click", () => {
        addRoutePoint(L.latLng(row[1], row[2]));
        if (route.stops.length === 1) map.setView([row[1], row[2]], Math.max(map.getZoom(), 11));
        showFind(false);
        if (route.stops.length < 2) showFind(true); // and now the end
      });
      li.append(b);
      return li;
    });
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "small muted";
      li.textContent = pending ? "The harbour list is still downloading: try again in a minute." : "No harbour by that name.";
      items.push(li);
    }
    $("route-find-results").replaceChildren(...items);
  }

  async function saveRoute() {
    const saved = Plan.toSaved(route.stops, route.legs);
    const body = { name: $("route-name").value.trim(), points: saved.points, plan: saved.plan, speed: routeSpeed };
    if (route.id) body.id = route.id;
    $("route-save").disabled = true;
    try {
      let reply;
      try {
        reply = await api("api/routes", { method: "POST", body });
      } catch (err) {
        if (!body.id) throw err;
        delete body.id; // deleted elsewhere meanwhile: save it as a new one
        reply = await api("api/routes", { method: "POST", body });
      }
      route.id = reply.id;
      route.name = reply.name;
      $("route-name").value = reply.name;
      route.status = { text: `Saved as "${reply.name}". It's listed under Menu › On the map.` };
      await loadRoutes();
    } catch (err) {
      route.status = { text: String(err.message || err), error: true };
    }
    drawRoute();
  }

  function clearRoute() {
    if (route.stops.length) pushUndo();
    route.pick = null;
    route.stops = [];
    route.legs = [];
    route.id = null;
    route.name = "";
    route.status = null;
    routePopup?.remove();
    $("route-name").value = "";
    drawRoute();
    renderSavedRoutes();
  }

  function editRoute(r) {
    const plan = Plan.fromSaved(r.points, r.plan);
    route.stops = plan.stops;
    route.legs = plan.legs.map((l) => legState({ ...l, planned: true }));
    route.id = r.id;
    route.name = r.name;
    route.undo = [];
    route.pick = null;
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
  $("route-undo").addEventListener("click", () => { route.pick = null; undoRoute(); });
  // The other way: the stops and legs turned round; automatic legs are planned again (the
  // wind and the shelter are not the same on the way back).
  $("route-reverse").addEventListener("click", () => {
    if (route.stops.length < 2) return;
    pushUndo();
    route.pick = null;
    route.stops.reverse();
    route.legs.reverse();
    for (const leg of route.legs) leg.path = (leg.path || []).slice().reverse();
    Plan.fixEnds?.(route.stops);
    const again = route.legs.map((l, i) => (l.auto ? i : -1)).filter((i) => i >= 0);
    if (again.length) planLegs(again); else drawRoute();
  });
  $("route-room").addEventListener("change", (e) => {
    routeRoom = e.target.value;
    store.set("fw-route-room", routeRoom);
    const again = route.legs.map((l, i) => (l.auto ? i : -1)).filter((i) => i >= 0);
    if (again.length) planLegs(again); else renderRoutePanel();
  });
  $("route-pick-cancel").addEventListener("click", cancelPick);
  $("route-clear").addEventListener("click", clearRoute);
  $("route-save").addEventListener("click", saveRoute);
  $("route-name").addEventListener("keydown", (e) => { if (e.key === "Enter" && route.points.length > 1) saveRoute(); });
  $("route-name").addEventListener("input", (e) => { route.name = e.target.value; });
  for (const b of $("route-modes").querySelectorAll("button")) {
    b.addEventListener("click", () => {
      routeMode = b.dataset.mode;
      store.set("fw-route-mode", routeMode);
      route.status = null;
      renderRouteModes();
      renderRoutePanel();
    });
  }
  $("route-find-button").addEventListener("click", () => showFind($("route-find").hidden));
  $("route-find-input").addEventListener("input", () => { clearTimeout(findTimer); findTimer = setTimeout(findHarbours, 250); });
  $("route-find-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("route-find-results").querySelector("button")?.click(); }
  });
  $("route-speed").addEventListener("change", (e) => {
    const v = Number(String(e.target.value).replace(",", "."));
    if (v >= 0.5 && v <= 60) { routeSpeed = Math.round(v * 10) / 10; store.set("fw-route-speed", String(routeSpeed)); }
    e.target.value = String(routeSpeed);
    renderRoutePanel();
  });
  $("route-draught").addEventListener("change", (e) => {
    const v = Number(String(e.target.value).replace(",", "."));
    const before = routeDraught;
    if (v >= 0 && v <= 15) { routeDraught = Math.round(v * 10) / 10; store.set("fw-route-draught", String(routeDraught)); }
    e.target.value = String(routeDraught);
    if (routeDraught !== before) {
      planLegs(route.legs.map((l, i) => (l.fw ? i : -1)).filter((i) => i >= 0));
      loadFairways();
    }
  });
  // A tap on the map that only closes the route's little menu doesn't also add a point.
  map.on("preclick", () => { routePopupWasOpen = Boolean(routePopup && map.hasLayer(routePopup)); });
  map.on("click", (e) => {
    if (!route.on) return;
    if (routePopupWasOpen) { routePopupWasOpen = false; return; }
    addRoutePoint(e.latlng); // or, while picking where a cut rejoins, cancels that
  });

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
      // Fairway turns, bends and via points get no dot.
      const unnumbered = new Set([...(r.plan?.bend || []), ...(r.plan?.via || [])]);
      const stops = r.plan?.wp ? r.plan.wp.filter((_, i) => !unnumbered.has(i)).map((k) => r.points[k]).filter(Boolean) : r.points;
      for (const p of stops) {
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

  // "Import GPX": routes (or tracks) from a file become saved routes.
  function routeImportRow() {
    const row = document.createElement("p");
    row.className = "small route-import";
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".gpx,application/gpx+xml,text/xml,application/xml";
    input.hidden = true;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Import GPX";
    const note = document.createElement("span");
    note.className = "muted";
    note.setAttribute("role", "status");
    button.addEventListener("click", () => input.click());
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      note.textContent = " Reading…";
      try {
        const res = await fetch("api/routes/import", { method: "POST", body: file });
        if (!res.ok) throw new Error(await res.text());
        const n = (await res.json()).routes.length;
        await loadRoutes();
        const row2 = $("routes-list").querySelector(".route-import [role=status]");
        if (row2) row2.textContent = ` Added ${n} route${n === 1 ? "" : "s"}.`;
      } catch (err) {
        note.textContent = ` ${err.message || err}`;
      }
    });
    row.append(button, input, note);
    return row;
  }

  function renderRoutesList() {
    const box = $("routes-list");
    if (!savedRoutes.length) {
      const p = document.createElement("p");
      p.className = "small muted";
      p.textContent = "Measure and plan with the ruler button at the top of the map, then Save. Saved routes are listed here.";
      box.replaceChildren(p, routeImportRow());
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
      // For the chart plotter: the route as a GPX file.
      const gpx = document.createElement("a");
      gpx.className = "button-link";
      gpx.href = `api/routes/${r.id}.gpx`;
      gpx.download = "";
      gpx.textContent = "GPX";
      gpx.title = "Download as a GPX route for a chart plotter";
      li.append(toggle, grow, gpx, edit, del);
      return li;
    }));
    box.replaceChildren(list, routeImportRow());
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
        // (a weather station without wind: its air temperature, in grey)
        const air = s.wave == null && s.sea == null;
        const text = s.wave != null ? `${s.wave.toFixed(1)} m` : `${Math.round(air ? s.t : s.sea)}°`;
        ctx.font = "700 11px system-ui, -apple-system, sans-serif";
        const w = ctx.measureText(text).width + 10;
        roundRect(ctx, -w / 2, -9, w, 18, 9);
        ctx.fillStyle = air ? "#64748b" : "#0f766e";
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
      if (s.ws == null && s.wave == null && s.sea == null && s.t == null) continue; // nothing to show
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

  // Nature reserves: tap inside one on the drawn chart (where their borders show) or with
  // the highlight switch on, for its card. Buoys, lights and wrecks win the tap.
  reserves = FerryReserves.create({
    map, api, esc, store,
    canTap: () => !route.on && activeLayer === layers.chart && chartStyle === "drawn" && map.getZoom() >= 11,
    onChange: () => {
      if (!$("ships-dialog").querySelector('[data-pane="layers"]').hidden) renderLayers();
    },
  });
  // Nautical and sea weather warnings, bridges and locks, speed limits on a route (notices.js).
  notices = FerryNotices.create({
    map, api, esc, store, fmtWhen,
    onChange: () => {
      if (!$("ships-dialog").querySelector('[data-pane="layers"]').hidden) renderLayers();
    },
  });
  // Trips from ben's own Garmin plotter (Logbook tab). On a phone the sheet covers the
  // map, so it closes when a trip is shown.
  logbook = FerryLogbook.create({
    map, api, esc, fmtDay, clock,
    pane: $("logbook"), pill: $("trip-shown"),
    onShow: () => { if (menuModal()) menu.close(); },
    sideWidth: () => (document.body.classList.contains("menu-open") ? menu.offsetWidth : 0),
  });
  map.on("click", (e) => {
    if (route.on) return;
    if (activeLayer === layers.chart && chartStyle === "drawn" && map.getZoom() >= 11 && FerryChart.aidAt(map, e.latlng)) return;
    if (hist.tap(e.latlng)) return;
    reserves.tap(e.latlng);
  });
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
        : data.error && !data.harbours.length ? `Couldn't get them from OpenStreetMap just now: ${data.error}. Trying again by itself.` : "";
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
    if (menu.open) {
      // The side panel isn't modal, so Escape is ours to handle (a modal sheet closes itself).
      if (e.key === "Escape" && !menuModal() && !(e.target instanceof HTMLInputElement && e.target.value)) menu.close();
      if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !e.target.closest?.("input, select, textarea")
          && !menu.querySelector('[data-pane="stats"]').hidden && stepStats(e.key === "ArrowRight" ? 1 : -1)) e.preventDefault();
      return;
    }
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
    else if (route.pick) cancelPick();
    else if (historyView.isOpen()) historyView.escape();
    else if (!$("weather-sheet").hidden) toggleWeather(false);
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
    else if (here.fromHash()) { /* a place link */ }
    else if (!focusFromNotification() && !viewRestored) fitAllShips();
    connectSocket();
    if (trafficOn) pollTraffic();
    if (stationsOn) pollStations();
    loadHarbours();
    loadRoutes().catch(console.error);
    if (route.points.length && store.get("fw-route-open") === "1") setRouteMode(true);
    // Tonight's sky: looked at now and every half hour while the map is open.
    setTimeout(() => sky.note().catch(console.error), 4000);
    setInterval(() => sky.note().catch(console.error), 30 * 60 * 1000);
  })();
})();
