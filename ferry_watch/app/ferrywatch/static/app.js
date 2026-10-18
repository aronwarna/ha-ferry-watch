/* Ferry Watch panel. All URLs are relative because HA ingress serves this under a path prefix. */
(() => {
  "use strict";

  const TAIL_MINUTES = 60;
  const STALE_SECONDS = 30 * 60;
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

  // --- map and layers -------------------------------------------------------

  const map = L.map("map", { zoomControl: false, attributionControl: true, worldCopyJump: true })
    .setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  map.attributionControl.setPrefix(false);

  const osm = () => L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "© OpenStreetMap contributors",
  });
  const seamarks = () => L.tileLayer("https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "Sea marks © OpenSeaMap",
  });

  // Traficom's WMTS uses its own matrix ids per zoom level.
  const MatrixTileLayer = L.TileLayer.extend({
    getTileUrl(coords) {
      const id = this.options.matrixIds[coords.z];
      return this._url.replace("{TileMatrix}", encodeURIComponent(id))
        .replace("{x}", coords.x).replace("{y}", coords.y);
    },
  });

  const layers = {
    map: osm(),
    satellite: L.layerGroup([
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
      }),
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 19,
      }),
    ]),
    chart: L.layerGroup([osm(), seamarks()]),
  };
  let activeLayer = null;

  function buildChartLayer(info) {
    if (!info) return L.layerGroup([osm(), seamarks()]);
    return L.layerGroup([
      osm(),
      new MatrixTileLayer(info.url, {
        matrixIds: info.matrix_ids,
        minZoom: 0,
        minNativeZoom: info.min_zoom,
        maxNativeZoom: info.max_zoom,
        maxZoom: 19,
        attribution: info.attribution,
      }),
      seamarks(),
    ]);
  }

  function setLayer(name) {
    if (!layers[name]) name = "map";
    if (activeLayer) map.removeLayer(activeLayer);
    activeLayer = layers[name];
    activeLayer.addTo(map);
    activeLayer.eachLayer?.((l) => l.bringToBack?.());
    document.querySelectorAll("[data-layer]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.layer === name));
    });
    store.set("fw-layer", name);
  }

  document.querySelectorAll("[data-layer]").forEach((b) => {
    b.addEventListener("click", () => setLayer(b.dataset.layer));
  });
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
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
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
    if (info.in_port) return `In port${info.port ? ` in ${info.port}` : ""}`;
    const parts = [];
    if (p.sog != null) parts.push(`${p.sog.toFixed(1)} kn`);
    const dir = compass(p.heading ?? p.cog);
    if (dir && p.sog > 0.5) parts.push(`heading ${dir}`);
    if (info.destination) parts.push(`to ${titleCase(info.destination)}`);
    return parts.join(" · ") || "Stopped";
  }

  function titleCase(text) {
    return text === text.toUpperCase()
      ? text.toLowerCase().replace(/(^|[\s-])\p{L}/gu, (c) => c.toUpperCase())
      : text;
  }

  const isStale = (info) => !info.position || now() - info.position.ts > STALE_SECONDS;

  // --- markers ----------------------------------------------------------------

  function shipSvg(color, rotation) {
    return `<svg width="28" height="28" viewBox="-14 -14 28 28" style="transform: rotate(${rotation}deg)">
      <path d="M0 -12 L6 -2 L6 10 Q0 13 -6 10 L-6 -2 Z" fill="${color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>
    </svg>`;
  }

  function markerIcon(info, dimmed) {
    const p = info.position;
    const rotation = p ? (p.heading ?? p.cog ?? 0) : 0;
    const classes = ["ship-icon"];
    if (dimmed) classes.push("dimmed");
    if (isStale(info)) classes.push("stale");
    return L.divIcon({
      className: classes.join(" "),
      html: shipSvg(info.color, rotation),
      iconSize: [28, 28],
      iconAnchor: [14, 14],
    });
  }

  function upsertShip(info) {
    let entry = ships.get(info.mmsi);
    if (!entry) {
      entry = { info, marker: null, tail: null, tailPoints: [] };
      ships.set(info.mmsi, entry);
    }
    entry.info = info;
    const p = info.position;
    if (!p) return entry;
    const dimmed = focused && focused.mmsi !== info.mmsi;
    if (!entry.marker) {
      entry.marker = L.marker([p.lat, p.lon], { icon: markerIcon(info, dimmed), keyboard: true, title: info.name })
        .on("click", () => focusShip(info.mmsi))
        .bindTooltip(shortName(info.name), {
          permanent: true, direction: "right", offset: [12, 0], className: "ship-label",
        })
        .addTo(map);
      entry.tail = L.polyline([], { color: info.color, weight: 2.5, opacity: 0.55, interactive: false }).addTo(map);
    } else {
      entry.marker.setLatLng([p.lat, p.lon]);
      entry.marker.setIcon(markerIcon(info, dimmed));
      entry.marker.getTooltip()?.setContent(shortName(info.name));
    }
    entry.tail.setStyle({ color: info.color });
    entry.marker.getTooltip()?.getElement()?.classList.toggle("dimmed", Boolean(dimmed));
    return entry;
  }

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
    const entry = upsertShip({ ...ships.get(info.mmsi)?.info, ...info });
    const p = info.position;
    if (!p || !entry.tail) return;
    appendPoint(entry.tailPoints, [p.lat, p.lon, p.ts]);
    const cutoff = now() - TAIL_MINUTES * 60;
    while (entry.tailPoints.length && entry.tailPoints[0][2] < cutoff) entry.tailPoints.shift();
    entry.tail.setLatLngs(entry.tailPoints.map((q) => [q[0], q[1]]));
    if (focused?.mmsi === info.mmsi) {
      appendPoint(focused.points, [p.lat, p.lon, p.ts]);
      focused.line.setLatLngs(focused.points.map((q) => [q[0], q[1]]));
      renderCard();
    }
    renderStrip();
  }

  // --- focus a ship -------------------------------------------------------------

  async function focusShip(mmsi, { fit = true } = {}) {
    const entry = ships.get(mmsi);
    if (!entry) return;
    if (focused) focused.line.remove();
    focused = { mmsi, line: null, points: [] };
    applyDimming();
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
    if (focused?.mmsi !== mmsi) return;
    focused.points = data.points;
    focused.line = L.polyline(data.points.map((p) => [p[0], p[1]]), {
      color: entry.info.color, weight: 4, opacity: 0.9, interactive: false,
    }).addTo(map);
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
      return { paddingTopLeft: [30, 70], paddingBottomRight: [30, card.offsetHeight + 40] };
    }
    return { paddingTopLeft: [card.offsetWidth + 40, 70], paddingBottomRight: [40, 40] };
  }

  function fitAllShips() {
    const pts = [...ships.values()].filter((e) => e.info.position && !isStale(e.info))
      .map((e) => [e.info.position.lat, e.info.position.lon]);
    if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), { ...cardPadding(), maxZoom: 11 });
  }

  function unfocus() {
    if (focused?.line) focused.line.remove();
    const entry = focused && ships.get(focused.mmsi);
    entry?.marker?.setZIndexOffset(0);
    focused = null;
    applyDimming();
    $("card").hidden = true;
    $("strip").hidden = false;
    history.replaceState(null, "", location.pathname + location.search);
    renderStrip();
  }

  function applyDimming() {
    for (const entry of ships.values()) {
      const dimmed = Boolean(focused && focused.mmsi !== entry.info.mmsi);
      if (entry.marker) {
        entry.marker.setIcon(markerIcon(entry.info, dimmed));
        entry.marker.getTooltip()?.getElement()?.classList.toggle("dimmed", dimmed);
      }
      entry.tail?.setStyle({ opacity: dimmed ? 0.15 : 0.55 });
      if (focused && focused.mmsi === entry.info.mmsi) entry.tail?.setStyle({ opacity: 0 });
    }
  }

  function renderCard() {
    if (!focused) return;
    const entry = ships.get(focused.mmsi);
    if (!entry) return;
    const info = entry.info;
    $("card").hidden = false;
    $("card-name").textContent = info.name;
    $("card-dot").style.background = info.color;
    $("card-motion").textContent = motionText(info);

    let voyage = "";
    if (info.voyage_start) {
      const since = info.voyage_from ? `since ${info.voyage_from} ${clock(info.voyage_start)}` : `since ${clock(info.voyage_start)}`;
      voyage = `Track ${since} (${duration(now() - info.voyage_start)})`;
      if (now() - info.voyage_start > 86400) {
        voyage = `Track since ${new Date(info.voyage_start * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short", hour12: false })}`;
      }
    }
    $("card-voyage").textContent = voyage;
    $("card-seen").textContent = info.position ? `Last report ${ago(info.position.ts)}` : "Waiting for the first report";

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

  $("card-close").addEventListener("click", unfocus);
  $("card-zoom").addEventListener("click", zoomToTrack);
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

  function renderStrip() {
    const strip = $("strip");
    const items = [...ships.values()].map((e) => e.info);
    strip.replaceChildren(...items.map((info) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip panel" + (isStale(info) ? " stale" : "");
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = info.color;
      const text = document.createElement("span");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = info.name;
      const sub = document.createElement("span");
      sub.className = "sub";
      sub.textContent = info.position ? (isStale(info) ? `Last seen ${ago(info.position.ts)}` : motionText(info)) : "No position yet";
      text.append(name, document.createElement("br"), sub);
      b.append(dot, text);
      b.addEventListener("click", () => focusShip(info.mmsi));
      return b;
    }));
  }

  // --- status -------------------------------------------------------------------

  let lastState = null;
  let socketOpen = false;

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
    el.lastElementChild.textContent = text;
  }

  // --- data loading ---------------------------------------------------------------

  async function loadState() {
    const res = await fetch("api/state");
    const state = await res.json();
    lastState = state;
    clockSkew = state.now - Date.now() / 1000;
    const seen = new Set();
    for (const info of state.ships) {
      seen.add(info.mmsi);
      upsertShip(info);
    }
    for (const mmsi of [...ships.keys()]) if (!seen.has(mmsi)) removeShip(mmsi);
    updateChart(state.chart);
    renderStrip();
    renderCard();
    renderStatus();
    renderShipsList();
    $("version").textContent = `Ferry Watch ${state.version} · ${state.positions_stored.toLocaleString()} positions stored`;
  }

  async function loadTails() {
    const tails = await (await fetch(`api/tails?minutes=${TAIL_MINUTES}`)).json();
    for (const [mmsi, points] of Object.entries(tails)) {
      const entry = ships.get(Number(mmsi));
      if (entry) setTail(entry, points);
    }
  }

  function connectSocket() {
    const url = new URL("api/ws", location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.hash = "";
    const ws = new WebSocket(url);
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
    };
    ws.onclose = () => {
      socketOpen = false;
      renderStatus();
      setTimeout(connectSocket, 3000);
    };
  }

  // Refresh "x min ago" texts and feed status.
  setInterval(() => {
    renderStrip();
    renderCard();
    fetch("api/state").then((r) => r.json()).then((s) => { lastState = s; renderStatus(); }).catch(() => {});
  }, 30000);

  // --- ships dialog -------------------------------------------------------------

  function renderShipsList() {
    const list = $("ships-list");
    list.replaceChildren(...[...ships.values()].map(({ info }) => {
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
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        if (!confirm(`Stop tracking ${info.name}?`)) return;
        await fetch(`api/ships/${info.mmsi}`, { method: "DELETE" });
        await loadState();
      });
      li.append(dot, grow, remove);
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
      sub.textContent = `MMSI ${r.mmsi}${r.destination ? ` · to ${titleCase(r.destination)}` : ""}`;
      grow.append(name, sub);
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = "Add";
      add.addEventListener("click", async () => {
        add.disabled = true;
        await fetch("api/ships", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mmsi: r.mmsi, name: r.name }),
        });
        $("ship-search").value = "";
        list.replaceChildren();
        await loadState();
        await loadTails();
      });
      li.append(grow, add);
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
    $("ships-dialog").showModal();
  });

  // --- start --------------------------------------------------------------------

  function shipFromUrl() {
    const params = new URLSearchParams(location.search);
    const hash = new URLSearchParams(location.hash.slice(1));
    const value = hash.get("ship") || params.get("ship");
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
    else fitAllShips();
    connectSocket();
  })();
})();
