/* Rescue aircraft history: a selected aircraft's whole flight coloured by altitude, and the
   "Aircraft history" view, where every stored flight of a period glows on a dimmed map,
   as lines or as heat, with a time-lapse player. The flights come from the add-on as
   simplified lines; a tapped flight is fetched in full. */
window.FerryHistory = (() => {
  "use strict";

  // --- altitude colours ------------------------------------------------------------

  const ALT_STOPS = [[0, "#f59e0b"], [150, "#84cc16"], [300, "#10b981"], [600, "#06b6d4"],
    [1200, "#3b82f6"], [2400, "#8b5cf6"], [4000, "#d946ef"]];
  const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const ALT_RGB = ALT_STOPS.map(([m, c]) => [m, rgb(c)]);

  function altColor(m) {
    const v = Math.max(0, Math.min(4000, m ?? 0));
    const i = ALT_RGB.findIndex(([k]) => k >= v);
    if (i <= 0) return `rgb(${ALT_RGB[0][1]})`;
    const [k0, c0] = ALT_RGB[i - 1], [k1, c1] = ALT_RGB[i];
    const t = (v - k0) / (k1 - k0);
    return `rgb(${c0.map((c, j) => Math.round(c + (c1[j] - c) * t))})`;
  }

  // The altitude scale isn't linear (most rescue flying is low), so the legend spaces the
  // stops evenly and labels a few of them.
  function altLegend() {
    const stops = ALT_STOPS.map(([, c], i) => `${c} ${((i / (ALT_STOPS.length - 1)) * 100).toFixed(0)}%`).join(", ");
    return `<div class="alt-legend"><i style="background: linear-gradient(90deg, ${stops})"></i>`
      + "<span><span>0</span><span>300</span><span>1200</span><span>4000 m</span></span></div>";
  }

  /* A flight as a thick line coloured by altitude, with a dark edge and a white halo so it
     reads on any map. track: [[lat, lon, ts, alt_m]]. */
  function flightTrack(map, track, { weight = 5, pane } = {}) {
    const group = L.layerGroup().addTo(map);
    const line = { lineCap: "round", lineJoin: "round", interactive: false, ...(pane ? { pane } : {}) };
    const edge = L.polyline([], { ...line, color: "#0b1220", weight: weight + 6, opacity: 0.45 }).addTo(group);
    const halo = L.polyline([], { ...line, color: "#ffffff", weight: weight + 3.5, opacity: 0.95 }).addTo(group);
    const runs = L.layerGroup().addTo(group);
    let pts = [];

    function draw() {
      const lls = pts.map((p) => [p[0], p[1]]);
      edge.setLatLngs(lls);
      halo.setLatLngs(lls);
      runs.clearLayers();
      // Altitude rounded to 50 m: one line per run of points with the same colour.
      let run = [], color = null;
      pts.forEach((p, i) => {
        const c = altColor(Math.round((p[3] ?? 0) / 50) * 50);
        if (c !== color && run.length) {
          run.push(lls[i]);
          runs.addLayer(L.polyline(run, { ...line, color, weight }));
          run = [];
        }
        color = c;
        run.push(lls[i]);
      });
      if (run.length > 1) runs.addLayer(L.polyline(run, { ...line, color, weight }));
    }

    pts = track.slice();
    draw();
    return {
      layer: group,
      add(p) {
        const last = pts[pts.length - 1];
        if (last && last[0] === p[0] && last[1] === p[1]) return;
        if (last && p[2] != null && last[2] != null && p[2] < last[2]) return;
        pts.push(p);
        draw();
      },
      bounds: () => L.latLngBounds(pts.map((p) => [p[0], p[1]])),
      remove: () => group.remove(),
    };
  }

  // --- the add-on's line format: (lat, lon, time) as deltas, 1e-4° and 1 s -------------
  // Plain arithmetic rather than bit operators: the first time value is ~3.6e9 after the
  // zigzag, past what JavaScript's 32-bit bit operators hold.

  function decode(text) {
    const values = [];
    let result = 0, mult = 1;
    for (let i = 0; i < text.length; i++) {
      const b = text.charCodeAt(i) - 63;
      result += (b & 0x1f) * mult;
      mult *= 32;
      if (b < 0x20) {
        values.push(result % 2 ? -Math.floor(result / 2) - 1 : result / 2);
        result = 0;
        mult = 1;
      }
    }
    const out = [];
    let lat = 0, lon = 0, ts = 0;
    for (let i = 0; i + 2 < values.length; i += 3) {
      lat += values[i]; lon += values[i + 1]; ts += values[i + 2];
      out.push([lat / 1e4, lon / 1e4, ts]);
    }
    return out;
  }

  // --- the history view --------------------------------------------------------------

  const PALETTE = ["#ff5c7a", "#38bdf8", "#a3e635", "#fbbf24", "#c084fc", "#2dd4bf",
    "#fb923c", "#f472b6", "#818cf8", "#facc15", "#4ade80", "#f87171"];
  const PERIOD_NAMES = { "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days", year: "Last year" };
  const PLAY_SECONDS = 24; // a whole period plays in about this long at the first speed
  const SPEEDS = [1, 2, 4, 8];
  const PAD = 0.15; // canvas margin around the view, so a short pan shows lines straight away

  const ICON = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path class="fill" d="M8 5.5v13l10.5-6.5z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path class="fill" d="M7 5.5h3.5v13H7zM13.5 5.5H17v13h-3.5z"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    heli: '<svg viewBox="-13 -13 26 26" aria-hidden="true"><ellipse cx="0" cy="1" rx="4.5" ry="6"/><path d="M0 6 L0 12 M-3 12 L3 12 M-11 -9 L11 9 M11 -9 L-11 9"/></svg>',
    plane: '<svg viewBox="-13 -13 26 26" aria-hidden="true"><path d="M0 -12 L2 -4 L11 1 L11 3 L2 1 L1.5 8 L5 11 L5 12.5 L0 11 L-5 12.5 L-5 11 L-1.5 8 L-2 1 L-11 3 L-11 1 L-2 -4 Z"/></svg>',
  };

  function spanText(seconds) {
    if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
    const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  const hours = (seconds) => (seconds >= 36000 ? `${Math.round(seconds / 3600)} h` : `${(seconds / 3600).toFixed(1).replace(/\.0$/, "")} h`);
  function rateText(perSecond) {
    if (perSecond < 5400) return `${Math.round(perSecond / 60)} min/s`;
    if (perSecond < 2 * 86400) return `${Math.round(perSecond / 3600)} h/s`;
    return `${Math.round(perSecond / 86400)} days/s`;
  }
  function stamp(ts, withYear) {
    const d = new Date(ts * 1000);
    const day = d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}) });
    return `${day} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`;
  }

  // Web Mercator pixels at zoom z, without Leaflet's per-point objects.
  function project(lat, lon, scale) {
    const s = Math.sin((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180);
    return [((lon + 180) / 360) * scale, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * scale];
  }

  /* ctx: { map, api, esc, knownPhoto(hex), enabled(), onOpen(), onClose() } */
  function view(ctx) {
    const { map, esc } = ctx;
    let el = null, canvas = null, g = null, trails = null, heatBuf = null;
    let open = false, period = "24h", mode = "lines", data = null, flights = [], aircraft = [];
    let solo = null, pick = null, pickTrack = null, loadId = 0;
    let t = 0, tFrom = 0, tTo = 0, playing = false, speedIdx = 0, frame = 0, lastFrame = 0;
    let doneIdx = 0, byEnd = [], view = null; // view: canvas placement for the current map position
    const dpr = Math.min(2, window.devicePixelRatio || 1);

    // --- DOM ---
    function build() {
      el = document.createElement("section");
      el.className = "ah panel";
      el.hidden = true;
      el.setAttribute("aria-label", "Aircraft history");
      el.innerHTML = `
        <div class="ah-head">
          <div class="ah-title"><strong>Aircraft history</strong><span class="ah-sub"></span></div>
          <div class="segmented ah-mode" role="group" aria-label="Show as">
            <button type="button" data-mode="lines">Lines</button><button type="button" data-mode="heat">Heat</button>
          </div>
          <button type="button" class="icon-button plain ah-close" aria-label="Close">${ICON.close}</button>
        </div>
        <div class="ah-chips" role="group" aria-label="Period">
          ${Object.keys(PERIOD_NAMES).map((p) => `<button type="button" data-period="${p}">${p === "year" ? "Year" : p.replace(/(\d+)/, "$1 ")}</button>`).join("")}
        </div>
        <div class="ah-player">
          <button type="button" class="player-btn ah-play" aria-label="Play the time-lapse">${ICON.play}</button>
          <div class="ah-time">
            <div class="ah-clock"><strong></strong><span></span></div>
            <div class="ah-track"><svg class="ah-spark" preserveAspectRatio="none" aria-hidden="true"></svg>
              <input type="range" min="0" max="1000" step="1" value="1000" aria-label="Time in the period"></div>
          </div>
          <button type="button" class="player-speed ah-speed" title="Time-lapse speed"></button>
        </div>
        <div class="ah-pick" hidden></div>
        <ul class="ah-legend" aria-label="Aircraft"></ul>
        <p class="ah-note small muted" hidden></p>`;
      document.body.append(el);
      el.querySelector(".ah-close").addEventListener("click", close);
      el.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
        mode = b.dataset.mode;
        if (mode === "heat") stop(true);
        render();
        controls();
      }));
      el.querySelectorAll("[data-period]").forEach((b) => b.addEventListener("click", () => {
        if (b.dataset.period !== period) load(b.dataset.period, true);
      }));
      el.querySelector(".ah-play").addEventListener("click", () => (playing ? stop(false) : play()));
      el.querySelector(".ah-speed").addEventListener("click", () => {
        speedIdx = (speedIdx + 1) % SPEEDS.length;
        controls();
      });
      const range = el.querySelector(".ah-track input");
      range.addEventListener("input", () => {
        if (mode === "heat") mode = "lines";
        t = tFrom + (Number(range.value) / 1000) * (tTo - tFrom);
        if (Number(range.value) >= 1000) t = tTo;
        rewind();
        render();
        controls();
      });

      const pane = map.getPane("airHistory") || map.createPane("airHistory");
      pane.style.zIndex = 450;
      pane.style.pointerEvents = "none";
      canvas = document.createElement("canvas");
      canvas.className = "ah-canvas";
      pane.append(canvas);
      g = canvas.getContext("2d");
      trails = document.createElement("canvas");
    }

    // --- loading ---
    async function load(p, fit) {
      period = p;
      const id = ++loadId;
      stop(true);
      clearPick();
      el.classList.add("loading");
      controls();
      let got;
      try {
        got = await ctx.api(`api/aircraft/overview?period=${p}`);
      } catch (err) {
        console.error(err);
        got = null;
      }
      if (id !== loadId || !open) return;
      el.classList.remove("loading");
      data = got;
      prepare();
      t = tTo;
      solo = solo && aircraft.some((a) => a.icao === solo) ? solo : null;
      legend();
      controls();
      if (fit) fitAll();
      reset();
    }

    function prepare() {
      const info = data?.aircraft || {};
      aircraft = Object.entries(info).map(([icao, a]) => ({ icao, ...a }))
        .sort((a, b) => b.flights - a.flights || b.seconds - a.seconds);
      aircraft.forEach((a, i) => { a.color = PALETTE[i % PALETTE.length]; });
      const colorOf = new Map(aircraft.map((a) => [a.icao, a.color]));
      flights = (data?.flights || []).map(([id, icao, start, end, maxAlt, km, line]) => ({
        id, icao, start, end, maxAlt, km, pts: decode(line), color: colorOf.get(icao) || PALETTE[0], z: null, px: null,
      })).filter((f) => f.pts.length > 1).sort((a, b) => a.start - b.start);
      byEnd = flights.slice().sort((a, b) => a.end - b.end);
      tTo = data?.until ?? Date.now() / 1000;
      tFrom = Math.max(data?.since ?? tTo - 86400, flights.length ? flights[0].start - 600 : tTo - 86400);
      spark();
    }

    function fitAll() {
      if (!flights.length) return;
      let s = 90, w = 180, n = -90, e = -180;
      for (const f of flights) for (const [la, lo] of f.pts) {
        if (la < s) s = la; if (la > n) n = la; if (lo < w) w = lo; if (lo > e) e = lo;
      }
      const phone = window.innerWidth <= 560;
      map.fitBounds([[s, w], [n, e]], {
        paddingTopLeft: [24, 110], maxZoom: 12,
        paddingBottomRight: phone ? [24, window.innerHeight - el.getBoundingClientRect().top + 16] : [el.offsetWidth + 40, 24],
      });
    }

    // --- canvas placement ---
    function reset() {
      if (!open) return;
      const size = map.getSize();
      const pad = [Math.round(size.x * PAD), Math.round(size.y * PAD)];
      const topLeft = map.containerPointToLayerPoint([-pad[0], -pad[1]]).round();
      L.DomUtil.setPosition(canvas, topLeft);
      const w = size.x + 2 * pad[0], h = size.y + 2 * pad[1];
      for (const c of [canvas, trails]) {
        c.width = Math.round(w * dpr);
        c.height = Math.round(h * dpr);
      }
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      const z = map.getZoom();
      const origin = map.getPixelOrigin().add(topLeft);
      view = { w, h, z, scale: map.options.crs.scale(z), ox: origin.x, oy: origin.y };
      canvas.style.opacity = "1";
      rewind();
      render();
    }

    // Each flight's points in pixels at this zoom, worked out once per zoom.
    function pixels(f) {
      if (f.z !== view.z) {
        f.z = view.z;
        f.px = new Float64Array(f.pts.length * 2);
        f.pts.forEach(([la, lo], i) => {
          const [x, y] = project(la, lo, view.scale);
          f.px[i * 2] = x;
          f.px[i * 2 + 1] = y;
        });
      }
      return f.px;
    }

    function path(c, f, upTo) {
      const px = pixels(f), ox = view.ox, oy = view.oy;
      c.beginPath();
      c.moveTo(px[0] - ox, px[1] - oy);
      let head = null;
      for (let i = 1; i < f.pts.length; i++) {
        if (upTo != null && f.pts[i][2] > upTo) {
          const a = f.pts[i - 1], k = (upTo - a[2]) / Math.max(1, f.pts[i][2] - a[2]);
          const x = px[i * 2 - 2] + (px[i * 2] - px[i * 2 - 2]) * k - ox;
          const y = px[i * 2 - 1] + (px[i * 2 + 1] - px[i * 2 - 1]) * k - oy;
          c.lineTo(x, y);
          head = [x, y];
          break;
        }
        c.lineTo(px[i * 2] - ox, px[i * 2 + 1] - oy);
      }
      return head;
    }

    const visible = (f) => !solo || f.icao === solo;

    // Glowing lines: three strokes added on top of each other, wide and faint to thin and
    // bright. Busy periods get fainter lines so the common routes glow rather than burn.
    function glow(c, f, strength, upTo) {
      const k = (visible(f) ? 1 : 0.12) * strength;
      c.strokeStyle = f.color;
      const head = path(c, f, upTo);
      c.globalAlpha = 0.07 * k; c.lineWidth = 9; c.stroke();
      c.globalAlpha = 0.2 * k; c.lineWidth = 4; c.stroke();
      c.globalAlpha = Math.min(1, 0.95 * k + 0.05); c.lineWidth = 1.4; c.stroke();
      return head;
    }

    function strength() {
      const n = solo ? flights.filter(visible).length : flights.length;
      return Math.max(0.35, Math.min(1, Math.sqrt(40 / Math.max(1, n))));
    }

    function scrim(c) {
      c.globalCompositeOperation = "source-over";
      c.globalAlpha = 1;
      c.fillStyle = "rgba(5, 10, 24, 0.6)";
      c.fillRect(0, 0, view.w, view.h);
    }

    // --- drawing ---
    function render() {
      if (!open || !view) return;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, view.w, view.h);
      scrim(g);
      g.lineCap = "round";
      g.lineJoin = "round";
      if (t >= tTo && !playing) {
        if (mode === "heat") heat();
        else {
          g.globalCompositeOperation = "lighter";
          const s = strength();
          for (const f of flights) if (f !== pick) glow(g, f, s);
        }
      } else {
        lapse();
      }
      if (pick) {
        g.globalCompositeOperation = "source-over";
        g.globalAlpha = 1;
        g.strokeStyle = "#fff";
        g.lineWidth = 3;
        path(g, pick);
        g.stroke();
      }
      g.globalCompositeOperation = "source-over";
      g.globalAlpha = 1;
    }

    // Heat: every line added up faintly on a small canvas, blurred, then coloured from deep
    // purple through red and orange to white by how many flights crossed each spot, scaled
    // to the busiest spot in view.
    const HEAT = (() => {
      const stops = [[0, [60, 10, 110, 0]], [50, [110, 20, 150, 120]], [110, [215, 40, 90, 200]],
        [170, [250, 120, 30, 235]], [225, [255, 214, 80, 250]], [255, [255, 255, 230, 255]]];
      const lut = new Uint8ClampedArray(256 * 4);
      for (let v = 0; v < 256; v++) {
        const i = stops.findIndex(([k]) => k >= v);
        const [k0, c0] = stops[Math.max(0, i - 1)], [k1, c1] = stops[i];
        const f = k1 > k0 ? (v - k0) / (k1 - k0) : 0;
        for (let j = 0; j < 4; j++) lut[v * 4 + j] = c0[j] + (c1[j] - c0[j]) * f;
      }
      return lut;
    })();

    function heat() {
      const s = 0.5; // half resolution: heat is soft anyway
      const w = Math.ceil(view.w * s), h = Math.ceil(view.h * s);
      heatBuf = heatBuf || document.createElement("canvas");
      heatBuf.width = w;
      heatBuf.height = h;
      const c = heatBuf.getContext("2d", { willReadFrequently: true });
      c.setTransform(s, 0, 0, s, 0, 0);
      c.filter = "blur(2.5px)";
      c.globalCompositeOperation = "lighter";
      c.strokeStyle = "#fff";
      c.lineCap = "round";
      c.lineJoin = "round";
      c.lineWidth = 6;
      const shown = flights.filter(visible);
      c.globalAlpha = shown.length > 40 ? 0.025 : 0.08; // about 5 or 20 steps of 255 a flight
      for (const f of shown) { path(c, f); c.stroke(); }
      const img = c.getImageData(0, 0, w, h), d = img.data;
      // The count is in the alpha channel: white strokes read back as white wherever there's
      // any, since the canvas stores colours premultiplied.
      let max = 1;
      for (let i = 3; i < d.length; i += 4) if (d[i] > max) max = d[i];
      // A square root, so places crossed a few times still show next to the busiest ones.
      const scale = new Uint8Array(256);
      for (let v = 1; v < 256; v++) scale[v] = Math.min(255, Math.round(255 * Math.sqrt(v / max)));
      for (let i = 0; i < d.length; i += 4) {
        const v = scale[d[i + 3]] * 4;
        d[i] = HEAT[v]; d[i + 1] = HEAT[v + 1]; d[i + 2] = HEAT[v + 2]; d[i + 3] = HEAT[v + 3];
      }
      c.putImageData(img, 0, 0);
      g.globalCompositeOperation = "source-over";
      g.imageSmoothingEnabled = true;
      g.drawImage(heatBuf, 0, 0, w, h, 0, 0, view.w, view.h);
    }

    // Time-lapse: flights that have landed by time t are drawn once onto a trails canvas;
    // each frame copies it and adds the flights still in the air, with a bright head.
    function rewind() {
      doneIdx = 0;
      const c = trails.getContext("2d");
      c.setTransform(1, 0, 0, 1, 0, 0);
      c.clearRect(0, 0, trails.width, trails.height);
    }

    function lapse() {
      const c = trails.getContext("2d");
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.globalCompositeOperation = "lighter";
      c.lineCap = "round";
      c.lineJoin = "round";
      const s = strength() * 0.75;
      while (doneIdx < byEnd.length && byEnd[doneIdx].end <= t) glow(c, byEnd[doneIdx++], s);
      g.globalCompositeOperation = "lighter";
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.drawImage(trails, 0, 0);
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      const heads = [];
      for (const f of flights) {
        if (f.start > t) break;
        if (f.end <= t) continue;
        const head = glow(g, f, 1, t);
        if (head && visible(f)) heads.push([head, f.color]);
      }
      g.globalCompositeOperation = "lighter";
      for (const [[x, y], color] of heads) {
        const r = 14;
        const grad = g.createRadialGradient(x, y, 0, x, y, r);
        grad.addColorStop(0, "rgba(255,255,255,0.95)");
        grad.addColorStop(0.25, color);
        grad.addColorStop(1, "rgba(0,0,0,0)");
        g.globalAlpha = 1;
        g.fillStyle = grad;
        g.fillRect(x - r, y - r, r * 2, r * 2);
      }
    }

    // --- time-lapse player ---
    function play() {
      if (!flights.length) return;
      if (mode === "heat") mode = "lines";
      if (t >= tTo) { t = tFrom; rewind(); }
      playing = true;
      lastFrame = performance.now();
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(tick);
      controls();
    }

    function stop(toEnd) {
      playing = false;
      cancelAnimationFrame(frame);
      if (toEnd) t = tTo;
      if (el) { render(); controls(); }
    }

    function tick(now) {
      const dt = Math.min(0.1, (now - lastFrame) / 1000);
      lastFrame = now;
      t = Math.min(tTo, t + dt * rate());
      if (t >= tTo) { stop(true); return; }
      render();
      controls();
      frame = requestAnimationFrame(tick);
    }

    const rate = () => ((tTo - tFrom) / PLAY_SECONDS) * SPEEDS[speedIdx];

    function controls() {
      if (!el) return;
      el.querySelectorAll("[data-period]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.period === period)));
      el.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
      const btn = el.querySelector(".ah-play");
      btn.innerHTML = playing ? ICON.pause : ICON.play;
      btn.setAttribute("aria-label", playing ? "Pause" : "Play the time-lapse");
      btn.disabled = !flights.length;
      el.querySelector(".ah-speed").textContent = rateText(rate());
      const range = el.querySelector(".ah-track input");
      const p = tTo > tFrom ? Math.round(((t - tFrom) / (tTo - tFrom)) * 1000) : 1000;
      if (Number(range.value) !== p) range.value = String(p);
      range.style.setProperty("--p", `${p / 10}%`);
      const [strong, span] = el.querySelectorAll(".ah-clock > *");
      const lapsing = playing || t < tTo;
      strong.textContent = lapsing ? stamp(t, period === "year") : PERIOD_NAMES[period];
      const up = lapsing ? flights.filter((f) => f.start <= t && f.end > t).length : 0;
      span.textContent = lapsing ? (up ? ` · ${up} in the air` : "") : "";
      const total = flights.reduce((s, f) => s + (f.end - f.start), 0);
      el.querySelector(".ah-sub").textContent = data
        ? `${flights.length} ${flights.length === 1 ? "flight" : "flights"} · ${aircraft.length} aircraft · ${hours(total)}`
        : el.classList.contains("loading") ? "Loading…" : "";
    }

    // Flights in the air over the period, behind the time slider.
    function spark() {
      const svg = el.querySelector(".ah-spark");
      const bins = new Array(60).fill(0);
      const span = Math.max(1, tTo - tFrom);
      for (const f of flights) {
        const a = Math.max(0, Math.floor(((f.start - tFrom) / span) * 60));
        const b = Math.min(59, Math.floor(((f.end - tFrom) / span) * 60));
        for (let i = a; i <= b; i++) bins[i] += 1;
      }
      const top = Math.max(1, ...bins);
      svg.setAttribute("viewBox", "0 0 60 10");
      svg.innerHTML = bins.map((n, i) => (n ? `<rect x="${i + 0.1}" y="${10 - (n / top) * 10}" width="0.8" height="${(n / top) * 10}"/>` : "")).join("");
    }

    // --- legend ---
    function legend() {
      const list = el.querySelector(".ah-legend");
      const note = el.querySelector(".ah-note");
      note.hidden = flights.length > 0 && ctx.enabled();
      note.textContent = !data
        ? "Couldn't load the flights. Try again in a moment."
        : !ctx.enabled()
          ? "Rescue aircraft is switched off, so no new flights are recorded. Switch it on under Menu › On the map."
          : `No rescue flights recorded in the ${PERIOD_NAMES[period].toLowerCase()}.`;
      list.replaceChildren(...aircraft.map((a) => {
        const li = document.createElement("li");
        li.className = "ah-ac";
        li.classList.toggle("solo", solo === a.icao);
        li.classList.toggle("dim", Boolean(solo) && solo !== a.icao);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.setAttribute("aria-pressed", String(solo === a.icao));
        const photo = ctx.knownPhoto(a.icao);
        const thumb = photo
          ? `<img src="${esc(photo.src)}" alt="" loading="lazy">`
          : `<span class="ah-icon">${a.helicopter ? ICON.heli : ICON.plane}</span>`;
        const name = a.reg || a.callsign || a.icao.toUpperCase();
        const kind = [a.type_name || a.type, a.operator].filter(Boolean).join(" · ");
        btn.innerHTML = `<span class="ah-thumb" style="--c: ${a.color}">${thumb}</span>
          <span class="ah-name"><strong>${esc(name)}</strong><span>${esc(kind)}</span></span>
          <span class="ah-count"><strong>${a.flights}</strong><span>${hours(a.seconds)}</span></span>`;
        btn.addEventListener("click", () => {
          solo = solo === a.icao ? null : a.icao;
          if (pick && solo && pick.icao !== solo) clearPick();
          legend();
          rewind();
          render();
        });
        li.append(btn);
        return li;
      }));
    }

    // --- a tapped flight ---
    function clearPick() {
      pick = null;
      pickTrack?.remove();
      pickTrack = null;
      if (el) el.querySelector(".ah-pick").hidden = true;
    }

    async function choose(f) {
      clearPick();
      pick = f;
      render();
      const box = el.querySelector(".ah-pick");
      const a = aircraft.find((x) => x.icao === f.icao) || {};
      const d0 = new Date(f.start * 1000);
      const hm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
      box.innerHTML = `<span class="ah-swatch" style="--c: ${f.color}"></span>
        <div class="grow"><strong>${esc(a.reg || f.icao.toUpperCase())}${a.type_name || a.type ? ` · ${esc(a.type_name || a.type)}` : ""}</strong>
        <span>${esc(d0.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" }))}, ${hm(f.start)}–${hm(f.end)} · ${spanText(f.end - f.start)}</span>
        <span class="muted">Up to ${Math.round(f.maxAlt ?? 0).toLocaleString()} m · ${f.km} km${a.operator ? ` · ${esc(a.operator)}` : ""}</span>
        ${altLegend()}</div>
        <button type="button" class="icon-button plain" aria-label="Close the flight">${ICON.close}</button>`;
      box.querySelector("button").addEventListener("click", () => { clearPick(); render(); });
      box.hidden = false;
      try {
        const full = await ctx.api(`api/aircraft/flights/${f.id}`);
        if (pick !== f || !open) return;
        if (!map.getPane("airHistoryPick")) map.createPane("airHistoryPick").style.zIndex = 460;
        pickTrack = flightTrack(map, full.track, { weight: 4, pane: "airHistoryPick" });
      } catch (err) {
        console.error(err);
      }
    }

    // Taps on the map: the nearest flight line within reach of a finger.
    function onClick(e) {
      if (!open || !view) return;
      const p = map.latLngToContainerPoint(e.latlng);
      const size = map.getSize();
      const cx = p.x + Math.round(size.x * PAD), cy = p.y + Math.round(size.y * PAD);
      let best = null, bestD = 14 * 14;
      for (const f of flights) {
        if (!visible(f) || (t < tTo && f.start > t)) continue;
        const px = pixels(f);
        for (let i = 2; i < px.length; i += 2) {
          const ax = px[i - 2] - view.ox, ay = px[i - 1] - view.oy, bx = px[i] - view.ox, by = px[i + 1] - view.oy;
          const dx = bx - ax, dy = by - ay, len = dx * dx + dy * dy;
          const k = len ? Math.max(0, Math.min(1, ((cx - ax) * dx + (cy - ay) * dy) / len)) : 0;
          const ex = ax + dx * k - cx, ey = ay + dy * k - cy, d = ex * ex + ey * ey;
          if (d < bestD) { bestD = d; best = f; }
        }
      }
      if (best) choose(best);
      else if (pick) { clearPick(); render(); }
    }

    function onZoomStart() { if (canvas) canvas.style.opacity = "0"; }

    // --- open and close ---
    function show({ icao } = {}) {
      if (!el) build();
      ctx.onOpen();
      open = true;
      el.hidden = false;
      document.body.classList.add("ah-open");
      solo = icao || null;
      map.on("moveend resize", reset);
      map.on("zoomstart", onZoomStart);
      map.on("click", onClick);
      reset();
      load(period, true);
    }

    function close() {
      if (!open) return;
      open = false;
      stop(true);
      clearPick();
      el.hidden = true;
      document.body.classList.remove("ah-open");
      map.off("moveend resize", reset);
      map.off("zoomstart", onZoomStart);
      map.off("click", onClick);
      canvas.width = canvas.height = trails.width = trails.height = 0; // let the memory go
      heatBuf = null;
      flights = [];
      byEnd = [];
      data = null;
      ctx.onClose();
    }

    // Escape: a chosen flight first, then the time-lapse, then the view.
    function escape() {
      if (pick) { clearPick(); render(); } else if (playing || t < tTo) stop(true); else close();
    }

    return { open: show, close, escape, isOpen: () => open, height: () => (el && !el.hidden ? el.offsetHeight : 0) };
  }

  return { altColor, altLegend, flightTrack, decode, view };
})();
