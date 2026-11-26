/* Windy-style colour layers (temperature, gusts, clouds, rain, fog, waves, sea temperature,
   sea level, northern lights) and the pressure isobars and lightning over them, drawn for
   overlays.js from the points the add-on sends for a forecast hour.

   A colour layer is one canvas over the view: the value is looked up between the four grid
   points around each few screen pixels and coloured from the layer's scale, then the small
   picture is stretched smoothly over the map, so the colours blend like Windy's. It is
   drawn again after each pan or zoom (moving with the map in between) and when the hour
   changes; a tap on the map reads the same grid. The grid maths, colour scales and number
   formats are kept apart from the drawing so the tests can run them under Node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.FerryFields = api;
    if (root.FerryOverlays) Object.assign(root.FerryOverlays.renderers, api.renderers);
  }
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  // --- colour scales ------------------------------------------------------------------
  // [value, r, g, b, alpha 0..255]; between stops the colour blends.

  const SCALES = {
    temp: { unit: "°C", stops: [[-30, 125, 70, 160, 210], [-20, 80, 80, 190, 210], [-10, 50, 130, 210, 205],
      [-5, 80, 180, 225, 200], [0, 160, 225, 230, 195], [3, 130, 210, 170, 195], [7, 120, 200, 110, 200],
      [11, 190, 210, 90, 200], [15, 240, 205, 70, 205], [20, 245, 150, 50, 210], [25, 230, 90, 50, 215],
      [30, 190, 40, 60, 220], [35, 140, 20, 70, 225]], ticks: [-20, -10, 0, 10, 20, 30] },
    wind: { unit: "m/s", stops: [[0, 98, 113, 183, 190], [2, 57, 136, 189, 195], [4, 70, 170, 170, 195],
      [6, 77, 185, 120, 200], [8, 110, 195, 70, 200], [10, 200, 205, 60, 205], [12, 240, 180, 50, 210],
      [14, 240, 130, 50, 215], [17, 220, 70, 60, 220], [21, 180, 40, 110, 225], [26, 125, 40, 150, 225],
      [33, 80, 30, 110, 230]], ticks: [0, 4, 8, 11, 14, 21, 30] },
    clouds: { unit: "%", stops: [[0, 40, 60, 90, 25], [20, 90, 105, 130, 70], [50, 160, 168, 180, 130],
      [80, 215, 218, 222, 180], [100, 245, 245, 247, 215]], ticks: [0, 25, 50, 75, 100] },
    rain: { unit: "mm/h", stops: [[0, 120, 170, 240, 0], [0.09, 120, 170, 240, 0], [0.1, 140, 195, 255, 130],
      [0.5, 70, 145, 240, 185], [1, 35, 95, 225, 205], [2, 40, 175, 80, 210], [4, 235, 210, 50, 215],
      [8, 240, 120, 40, 220], [15, 215, 40, 45, 225], [30, 160, 40, 160, 230]], ticks: [0.1, 0.5, 1, 2, 4, 8, 15], log: true },
    snow: { unit: "mm/h", stops: [[0, 230, 230, 255, 0], [0.09, 230, 230, 255, 0], [0.1, 235, 235, 255, 140],
      [0.5, 205, 195, 255, 185], [1, 170, 150, 245, 205], [3, 130, 95, 225, 215], [8, 95, 50, 190, 225]], ticks: [0.1, 0.5, 1, 3, 8], log: true },
    vis: { unit: "km", stops: [[0, 235, 235, 240, 225], [0.3, 220, 220, 228, 215], [1, 190, 192, 205, 185],
      [3, 150, 158, 178, 130], [6, 110, 120, 145, 70], [10, 80, 90, 115, 25], [20, 70, 80, 100, 0]], ticks: [0.3, 1, 3, 6, 10, 20], log: true },
    waves: { unit: "m", stops: [[0, 60, 85, 155, 175], [0.25, 50, 125, 195, 190], [0.5, 55, 175, 200, 200],
      [1, 85, 200, 120, 205], [1.5, 215, 210, 70, 210], [2, 240, 155, 50, 215], [3, 220, 75, 60, 220],
      [4, 170, 40, 120, 225], [6, 110, 30, 130, 230]], ticks: [0, 1, 2, 3, 4, 6] },
    sst: { unit: "°C", stops: [[-1, 70, 50, 150, 205], [2, 60, 95, 200, 205], [5, 50, 150, 210, 205],
      [8, 60, 190, 180, 205], [11, 110, 200, 110, 205], [14, 220, 210, 70, 210], [17, 245, 150, 50, 215],
      [20, 230, 80, 50, 220], [24, 180, 30, 60, 225]], ticks: [0, 4, 8, 12, 16, 20] },
    level: { unit: "cm", stops: [[-80, 70, 45, 150, 215], [-40, 60, 110, 210, 205], [-15, 120, 190, 230, 195],
      [0, 235, 235, 235, 185], [15, 245, 200, 120, 195], [40, 235, 120, 60, 210], [80, 190, 40, 60, 220],
      [120, 120, 20, 80, 230]], ticks: [-60, -30, 0, 30, 60, 100] },
    aurora: { unit: "%", stops: [[0, 30, 90, 50, 0], [3, 30, 110, 60, 0], [5, 40, 140, 70, 90], [15, 40, 190, 80, 150],
      [30, 120, 230, 80, 190], [50, 230, 230, 60, 210], [70, 240, 120, 50, 220], [90, 220, 40, 60, 230]], ticks: [5, 15, 30, 50, 70, 90] },
    sunset: { unit: "of 100", stops: [[0, 45, 45, 75, 140], [20, 80, 70, 120, 160], [40, 170, 80, 140, 185],
      [60, 240, 110, 90, 205], [80, 255, 170, 60, 220], [100, 255, 225, 120, 235]], ticks: [10, 30, 50, 70, 90],
      names: { 10: "dull", 30: "fair", 50: "good", 70: "great", 90: "fiery" } },
  };

  const LUT_SIZE = 512;
  function lut(scale) {
    if (scale._lut) return scale._lut;
    const s = scale.stops, lo = s[0][0], hi = s[s.length - 1][0];
    const f = scale.log ? (v) => Math.log(Math.max(v, 0.01)) : (v) => v;
    const a = f(Math.max(lo, 0.01)), b = f(hi);
    const out = new Uint8ClampedArray(LUT_SIZE * 4);
    for (let i = 0; i < LUT_SIZE; i++) {
      const fv = a + ((b - a) * i) / (LUT_SIZE - 1);
      const v = scale.log ? Math.exp(fv) : fv;
      out.set(colourAt(scale, v), i * 4);
    }
    scale._lut = { out, a, b, f };
    return scale._lut;
  }

  // The colour of a value: [r, g, b, a].
  function colourAt(scale, v) {
    const s = scale.stops;
    if (v <= s[0][0]) return s[0].slice(1);
    for (let i = 1; i < s.length; i++) {
      if (v <= s[i][0]) {
        const t = (v - s[i - 1][0]) / (s[i][0] - s[i - 1][0]);
        return [1, 2, 3, 4].map((k) => Math.round(s[i - 1][k] + (s[i][k] - s[i - 1][k]) * t));
      }
    }
    return s[s.length - 1].slice(1);
  }

  // --- the grid -----------------------------------------------------------------------

  // A forecast hour ({ fields, points: [[lat, lon, ...values]] }) as a regular grid: rising
  // latitudes and longitudes, and per field a Float32Array, row by row, NaN where missing.
  function grid(data) {
    const lats = [...new Set(data.points.map((p) => p[0]))].sort((a, b) => a - b);
    const lons = [...new Set(data.points.map((p) => p[1]))].sort((a, b) => a - b);
    const n = lats.length * lons.length;
    const row = new Map(lats.map((v, i) => [v, i])), col = new Map(lons.map((v, i) => [v, i]));
    const f = {};
    data.fields.forEach((name, j) => {
      const a = new Float32Array(n).fill(NaN);
      for (const p of data.points) {
        const v = p[2 + j];
        if (v != null) a[row.get(p[0]) * lons.length + col.get(p[1])] = v;
      }
      f[name] = a;
    });
    return { lats, lons, f, info: data.info || null, time: data.time };
  }

  // Where v falls on a rising axis: [step, 0..1 along it], or null outside (with half a
  // step's margin, so the colours reach the edge of the grid).
  function cell(axis, v) {
    const last = axis.length - 1;
    if (last < 1) return null;
    const half0 = (axis[1] - axis[0]) / 2, half1 = (axis[last] - axis[last - 1]) / 2;
    if (!(v >= axis[0] - half0 && v <= axis[last] + half1)) return null;
    if (v <= axis[0]) return [0, 0];
    if (v >= axis[last]) return [last - 1, 1];
    let lo = 0, hi = last;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (axis[mid] <= v) lo = mid; else hi = mid;
    }
    return [lo, (v - axis[lo]) / (axis[lo + 1] - axis[lo])];
  }

  // A field's value at a place, between the grid points around it that have one (at least
  // `need` of the weight, so a coast doesn't fade into nothing too soon); NaN outside.
  function sample(g, name, lat, lon, need = 0.3) {
    const a = cell(g.lats, lat), b = cell(g.lons, lon);
    const arr = g.f[name];
    if (!a || !b || !arr) return NaN;
    return at(arr, g.lons.length, a, b, need);
  }

  // How much of a sea layer's value must come from the sea's grid points (see sample): half
  // when drawn as it is, so it doesn't spill far over the land; next to nothing when cut to
  // the water (g.seaNeed, see fieldRenderer), so it reaches every bay and harbour.
  const SEA_NEED = 0.02;
  const seaNeed = (g) => g.seaNeed ?? 0.5;

  // The value of the grid point nearest a place (for what mustn't be blended: times, directions).
  function nearest(g, name, lat, lon) {
    const a = cell(g.lats, lat), b = cell(g.lons, lon), arr = g.f[name];
    if (!a || !b || !arr) return NaN;
    return arr[(a[0] + (a[1] > 0.5 ? 1 : 0)) * g.lons.length + b[0] + (b[1] > 0.5 ? 1 : 0)];
  }

  function at(arr, w, a, b, need = 0.3) {
    let s = 0, total = 0;
    const k0 = a[0] * w + b[0];
    const wy = a[1], wx = b[1];
    const pairs = [[k0, (1 - wy) * (1 - wx)], [k0 + 1, (1 - wy) * wx], [k0 + w, wy * (1 - wx)], [k0 + w + 1, wy * wx]];
    for (const [k, weight] of pairs) {
      if (weight <= 0) continue;
      const v = arr[k];
      if (Number.isNaN(v) || v === undefined) continue;
      s += v * weight; total += weight;
    }
    return total >= need ? s / total : NaN;
  }

  // --- what each layer shows ----------------------------------------------------------

  // Fog: the model's visibility, or where it has none, an estimate from how close the air
  // is to its dew point, the humidity, the wind and low cloud.
  function fogVis(g, lat, lon) {
    const vis = sample(g, "vis", lat, lon);
    if (!Number.isNaN(vis)) return vis;
    const t = sample(g, "t", lat, lon), td = sample(g, "td", lat, lon), rh = sample(g, "rh", lat, lon);
    if (Number.isNaN(t) || Number.isNaN(rh)) return NaN;
    const spread = Number.isNaN(td) ? Math.max(0, (100 - rh) / 5) : t - td;
    const ws = sample(g, "ms", lat, lon), low = sample(g, "low", lat, lon);
    let risk = Math.max(0, Math.min(1, (2.5 - spread) / 2.5)) * Math.max(0, Math.min(1, (rh - 88) / 10));
    if (!Number.isNaN(ws) && ws > 6) risk *= 0.5;
    if (!Number.isNaN(low) && low > 80) risk = Math.max(risk, 0.4);
    return 10 ** (1.3 - 2 * risk); // 20 km clear, 0.2 km in thick fog
  }

  const r1 = (v) => (Math.abs(v) < 10 ? v.toFixed(1) : String(Math.round(v)));
  const signed = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(Math.round(v))}`;
  const compass = (deg) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(((deg % 360) + 360) % 360 / 45) % 8];

  // Per overlay id: the scale, the value to colour by at a place, a label for the map (or
  // none), and the lines for a tap.
  const LAYERS = {
    temperature: {
      scale: "temp", value: (g, la, lo) => sample(g, "t", la, lo), label: (v) => `${Math.round(v)}°`,
      tap: (g, la, lo) => { const v = sample(g, "t", la, lo); return Number.isNaN(v) ? null : [`${r1(v)} °C`, "Air temperature"]; },
    },
    gusts: {
      scale: "wind", value: (g, la, lo) => sample(g, "gust", la, lo), label: (v) => String(Math.round(v)),
      tap: (g, la, lo) => {
        const v = sample(g, "gust", la, lo), ws = sample(g, "ms", la, lo);
        return Number.isNaN(v) ? null : [`${Math.round(v)} m/s gusts`, Number.isNaN(ws) ? "" : `Mean wind ${Math.round(ws)} m/s`];
      },
    },
    clouds: {
      scale: "clouds", value: (g, la, lo) => sample(g, "cloud", la, lo),
      tap: (g, la, lo) => {
        const v = sample(g, "cloud", la, lo);
        if (Number.isNaN(v)) return null;
        const p = (n) => { const x = sample(g, n, la, lo); return Number.isNaN(x) ? "–" : `${Math.round(x)}%`; };
        return [`${Math.round(v)}% cloud`, `Low ${p("low")} · middle ${p("mid")} · high ${p("high")}`];
      },
    },
    rain: {
      scale: "rain",
      value: (g, la, lo) => sample(g, "rain", la, lo),
      scaleAt: (g, la, lo) => (sample(g, "t", la, lo) <= 0.5 ? "snow" : "rain"),
      label: (v) => (v >= 0.2 ? r1(v) : ""),
      tap: (g, la, lo) => {
        const v = sample(g, "rain", la, lo), t = sample(g, "t", la, lo);
        if (Number.isNaN(v)) return null;
        const kind = t <= 0.5 ? "snow" : t <= 2 ? "sleet or rain" : "rain";
        return v < 0.1 ? ["Dry", "Under 0.1 mm in this hour"] : [`${r1(v)} mm/h`, `As ${kind}`];
      },
    },
    fog: {
      scale: "vis", value: fogVis,
      tap: (g, la, lo) => {
        const v = fogVis(g, la, lo);
        if (Number.isNaN(v)) return null;
        const est = Number.isNaN(sample(g, "vis", la, lo)) ? "about " : "";
        const word = v < 1 ? "fog" : v < 4 ? "mist or haze" : v < 10 ? "moderate" : "good";
        const rh = sample(g, "rh", la, lo);
        return [`${est}${v < 10 ? r1(v) : Math.round(v)} km visibility`, `${word[0].toUpperCase()}${word.slice(1)}${Number.isNaN(rh) ? "" : ` · humidity ${Math.round(rh)}%`}`];
      },
    },
    // The sea layers (sea: true) are cut to the water (see waterMask); cut, they reach a
    // whole grid step past the sea's grid points (seaNeed), so the colours come up to the coast.
    waves: {
      sea: true, scale: "waves", value: (g, la, lo) => sample(g, "wave", la, lo, seaNeed(g)), label: (v) => v.toFixed(1),
      tap: (g, la, lo) => {
        const n = seaNeed(g), v = sample(g, "wave", la, lo, n);
        if (Number.isNaN(v)) return null;
        const per = sample(g, "period", la, lo, n), sw = sample(g, "swell", la, lo, n), d = sample(g, "wdir", la, lo, n);
        return [`${v.toFixed(1)} m waves`, [Number.isNaN(d) ? "" : `from ${compass(d)}`, Number.isNaN(per) ? "" : `${per.toFixed(1)} s`,
          Number.isNaN(sw) || sw < 0.05 ? "" : `swell ${sw.toFixed(1)} m`].filter(Boolean).join(" · ")];
      },
    },
    "sea-temp": {
      sea: true, scale: "sst", value: (g, la, lo) => sample(g, "sst", la, lo, seaNeed(g)), label: (v) => `${Math.round(v)}°`,
      tap: (g, la, lo) => { const v = sample(g, "sst", la, lo, seaNeed(g)); return Number.isNaN(v) ? null : [`${r1(v)} °C`, "Sea surface"]; },
    },
    "sea-level": {
      sea: true, scale: "level", value: (g, la, lo) => sample(g, "level", la, lo, seaNeed(g)), label: signed,
      tap: (g, la, lo) => {
        const v = sample(g, "level", la, lo, seaNeed(g));
        return Number.isNaN(v) ? null : [`${signed(v)} cm`, v > 0 ? "Above mean sea level" : "Below mean sea level"];
      },
    },
    aurora: {
      scale: "aurora", value: (g, la, lo) => sample(g, "prob", la, lo, 0.5),
      tap: (g, la, lo) => {
        const p = sample(g, "prob", la, lo, 0.5), seen = sample(g, "seen", la, lo, 0.5);
        if (Number.isNaN(p)) return null;
        const c = sample(g, "cloud", la, lo, 0.5), dark = sample(g, "dark", la, lo, 0.5);
        const why = dark < 0.2 ? "too light now" : !Number.isNaN(c) && c > 70 ? `${Math.round(c)}% cloud` : "";
        return [`${Math.round(p)}% aurora overhead`, `Chance to see it here ${Math.round(seen)}%${why ? ` (${why})` : ""}`];
      },
    },
    sunset: {
      scale: "sunset", value: (g, la, lo) => sample(g, "score", la, lo),
      fade: (z) => (z >= 11 ? 0.35 : z >= 9 ? 0.7 : 1), // the spots matter more close in
      label: (v) => `${Math.round(v)}`,
      tap: (g, la, lo) => sunsetWhy(g, la, lo),
    },
    wind: {
      scale: "wind", value: (g, la, lo) => sample(g, "ms", la, lo),
      tap: (g, la, lo) => {
        const v = sample(g, "ms", la, lo), gu = sample(g, "gust", la, lo), d = sample(g, "dir", la, lo);
        return Number.isNaN(v) ? null : [`${Math.round(v)} m/s${Number.isNaN(d) ? "" : ` from ${compass(d)}`}`, Number.isNaN(gu) ? "Wind" : `Gusts ${Math.round(gu)} m/s`];
      },
    },
  };

  // --- drawing ------------------------------------------------------------------------

  const STEP = 3; // CSS px between looked-up values; the picture is stretched smoothly between
  const LABEL_GAP = 70; // px between numbers on the map
  const LABEL_ZOOM = 6;

  // A canvas over the view in the weather pane that follows the map: draw(ctx, w, h) after
  // each pan and zoom, scaled with the map while it zooms. With prepare(map, w, h), a
  // promise of what draw needs (its 5th argument): the picture stays where it is, moving with
  // the map, until that's there.
  function canvasLayer(draw, prepare) {
    const Layer = L.Layer.extend({
      onAdd(map) {
        this._canvas = L.DomUtil.create("canvas", "wx-canvas leaflet-zoom-animated");
        Object.assign(this._canvas.style, { position: "absolute", pointerEvents: "none" });
        map.getPane("weather").append(this._canvas);
        this._events = { moveend: this.redraw, resize: this.redraw, zoomanim: this._anim };
        map.on(this._events, this);
        this.redraw();
      },
      onRemove(map) {
        map.off(this._events, this);
        this._canvas.remove();
        this._canvas = null;
      },
      setOpacity(a) { this._alpha = a; if (this._canvas) this._canvas.style.opacity = String(a); return this; },
      _anim(e) {
        if (!this._topLeft) return;
        const map = this._map, scale = map.getZoomScale(e.zoom, map.getZoom());
        const offset = map._latLngToNewLayerPoint(this._topLeft, e.zoom, e.center);
        L.DomUtil.setTransform(this._canvas, offset, scale);
      },
      redraw() {
        const map = this._map;
        if (!map || !this._canvas) return;
        if (!prepare) { this._draw(map, null); return; }
        const token = (this._token || 0) + 1, size = map.getSize();
        this._token = token;
        const go = (ready) => { if (token === this._token && this._map === map && this._canvas) this._draw(map, ready); };
        Promise.resolve(prepare.call(this, map, size.x, size.y)).then(go, () => go(null));
      },
      _draw(map, ready) {
        const size = map.getSize(), dpr = Math.min(window.devicePixelRatio || 1, 2);
        const c = this._canvas;
        const pos = map.containerPointToLayerPoint([0, 0]);
        this._topLeft = map.containerPointToLatLng([0, 0]);
        L.DomUtil.setTransform(c, pos, 1);
        c.width = Math.round(size.x * dpr);
        c.height = Math.round(size.y * dpr);
        c.style.width = `${size.x}px`;
        c.style.height = `${size.y}px`;
        if (this._alpha != null) c.style.opacity = String(this._alpha);
        const ctx = c.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        draw.call(this, ctx, size.x, size.y, map, ready);
      },
    });
    return new Layer();
  }

  // Latitude of each screen row and longitude of each column (web mercator keeps them apart).
  function axes(map, w, h, step) {
    const rows = Math.ceil(h / step) + 1, cols = Math.ceil(w / step) + 1;
    const lat = new Float64Array(rows), lon = new Float64Array(cols);
    for (let r = 0; r < rows; r++) lat[r] = map.containerPointToLatLng([0, r * step]).lat;
    for (let c = 0; c < cols; c++) lon[c] = map.containerPointToLatLng([c * step, 0]).lng;
    return { rows, cols, lat, lon };
  }

  // The colour picture of one layer for the view, at one value per STEP px.
  function paint(ctx, w, h, map, g, def) {
    const { rows, cols, lat, lon } = axes(map, w, h, STEP);
    const small = new ImageData(cols, rows);
    const px = small.data;
    const tables = {};
    const tableOf = (name) => tables[name] || (tables[name] = lut(SCALES[name]));
    const fixed = tableOf(def.scale);
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const v = def.value(g, lat[r], lon[c]);
        if (Number.isNaN(v)) continue;
        const t = def.scaleAt ? tableOf(def.scaleAt(g, lat[r], lon[c])) : fixed;
        const fv = t.f(v);
        const i = Math.max(0, Math.min(LUT_SIZE - 1, Math.round(((fv - t.a) / (t.b - t.a)) * (LUT_SIZE - 1))));
        const o = (r * cols + c) * 4;
        px[o] = t.out[i * 4]; px[o + 1] = t.out[i * 4 + 1]; px[o + 2] = t.out[i * 4 + 2]; px[o + 3] = t.out[i * 4 + 3];
      }
    }
    const tmp = document.createElement("canvas");
    tmp.width = cols;
    tmp.height = rows;
    tmp.getContext("2d").putImageData(small, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(tmp, 0, 0, cols, rows, 0, 0, cols * STEP, rows * STEP);
  }

  // --- the sea layers only over the water ------------------------------------------------
  // Waves, sea temperature and sea level are cut to the Drawn chart's water (Traficom's depth
  // areas, the Swedish and Estonian side's, Natural Earth's open sea; less the lakes), on
  // whichever map, so they don't spill over the land: the add-on sends each chart tile's
  // water alone (api/vchart-water). Tiles a zoom coarser than the map's, at most zoom 12's
  // (tens of metres): a quarter as many to fetch, and the weather is far coarser anyway.
  const WATER_MIN_Z = 5, WATER_MAX_Z = 12;
  const WATER_KEEP = 96; // tiles' water kept, as paths
  const WATER_WAIT_MS = 2500; // a redraw waits this long for missing water, then draws what it has
  const WATER_RETRY_MS = 1500, WATER_TRIES = 6; // tiles whose chart data is still downloading
  const WATER_STALE_MS = { partial: 30000, pending: 10000, error: 60000 };
  const water = new Map(); // "z/x/y" -> { at, state: "ok" | "partial" | "pending" | "error", fills, lakes } | Promise
  const waterLate = new Set(); // what to do when water waited for comes in: redraw the layers
  let waterGen = 0; // goes up when water comes in, so masks made before are made again
  let waterAgain = 0, waterAgainMs = WATER_STALE_MS.pending; // the next look for water still downloading
  let lastMask = null;

  const chartRev = () => { try { return (typeof FerryChart !== "undefined" && FerryChart.rev?.()) || ""; } catch { return ""; } };
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function paths(rows) {
    return (rows || []).map((rings) => {
      const p = new Path2D();
      for (const r of rings) {
        if (r.length < 6) continue;
        p.moveTo(r[0], r[1]);
        for (let i = 2; i < r.length; i += 2) p.lineTo(r[i], r[i + 1]);
        p.closePath();
      }
      return p;
    });
  }

  async function fetchWater(key) {
    for (let tries = 0; ; tries++) {
      let r;
      try {
        r = await fetch(`api/vchart-water/${key}?r=${encodeURIComponent(chartRev())}`);
      } catch {
        return { state: "error" };
      }
      if (r.status === 202) {
        if (tries + 1 >= WATER_TRIES) return { state: "pending" };
        await pause(WATER_RETRY_MS);
        continue;
      }
      if (!r.ok) return { state: "error" };
      try {
        const data = await r.json();
        const k = 1 / (data.e || 512); // paths in tile units; drawn scaled to a unit square
        return { state: r.headers.get("X-Chart-Complete") === "0" ? "partial" : "ok", k,
          fills: paths(data.water), lakes: paths(data.lake) };
      } catch {
        return { state: "error" };
      }
    }
  }

  // A tile's water: kept, or fetched (a promise); stale ones (still downloading, or failed)
  // are asked for again.
  function waterTile(key) {
    const hit = water.get(key);
    if (hit instanceof Promise) return hit;
    if (hit && (hit.state === "ok" || Date.now() - hit.at < WATER_STALE_MS[hit.state])) {
      water.delete(key);
      water.set(key, hit);
      return hit;
    }
    const p = fetchWater(key).then((t) => {
      t.at = Date.now();
      water.set(key, t);
      for (const k of water.keys()) {
        if (water.size <= WATER_KEEP) break;
        if (!(water.get(k) instanceof Promise)) water.delete(k);
      }
      waterGen++;
      return t;
    });
    water.set(key, p);
    return p;
  }

  const viewKey = (map) => {
    const size = map.getSize(), o = map.project(map.containerPointToLatLng([0, 0]), map.getZoom());
    return `${map.getZoom()}|${Math.round(o.x)}|${Math.round(o.y)}|${size.x}|${size.y}`;
  };

  // The water over the view (where `box` [south, west, north, east], the layer's grid, is):
  // a canvas the size of the map, opaque over the water, and which CSS pixels are water.
  // null where it can't be had (zoomed far out, or an add-on without it): drawn as before.
  async function waterMask(map, w, h, box) {
    if (typeof Path2D === "undefined" || typeof fetch === "undefined" || typeof document === "undefined") return null;
    const zoom = map.getZoom();
    if (zoom < WATER_MIN_Z - 0.5) return null;
    const z = Math.max(WATER_MIN_Z, Math.min(WATER_MAX_Z, Math.round(zoom) - 1));
    const n = 2 ** z, size = 256 * 2 ** (zoom - z); // a tile's CSS px at the map's zoom
    const o = map.project(map.containerPointToLatLng([0, 0]), zoom);
    const tileOf = (lat, lon) => map.project([lat, lon], z).divideBy(256);
    const sw = tileOf(box[0], box[1]), ne = tileOf(box[2], box[3]);
    const x0 = Math.max(0, Math.floor(o.x / size), Math.floor(sw.x)), x1 = Math.min(n - 1, Math.floor((o.x + w) / size), Math.floor(ne.x));
    const y0 = Math.max(0, Math.floor(o.y / size), Math.floor(ne.y)), y1 = Math.min(n - 1, Math.floor((o.y + h) / size), Math.floor(sw.y));
    const keys = [];
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) keys.push([x, y, `${z}/${x}/${y}`]);
    const view = viewKey(map), boxKey = box.join(",");
    if (lastMask && lastMask.view === view && lastMask.box === boxKey && lastMask.gen === waterGen) return lastMask;
    const tiles = keys.map(([, , key]) => waterTile(key));
    const waiting = tiles.filter((t) => t instanceof Promise);
    if (waiting.length) {
      const all = Promise.all(waiting);
      const late = await Promise.race([all.then(() => false), pause(WATER_WAIT_MS).then(() => true)]);
      if (late) all.then(() => { for (const fn of waterLate) fn(); });
    }
    const got = keys.map(([, , key]) => water.get(key));
    const states = got.map((t) => (!t || t instanceof Promise ? "waiting" : t.state));
    // None to be had (not even "still downloading"): drawn uncut. Some still on their way:
    // the sea there stays clear until it comes, asked for again a little later each time.
    if (keys.length && states.every((st) => st === "error")) return null;
    if (states.some((st) => st === "pending" || st === "partial")) {
      if (!waterAgain) {
        waterAgain = setTimeout(() => { waterAgain = 0; for (const fn of waterLate) fn(); }, waterAgainMs);
        waterAgainMs = Math.min(300000, waterAgainMs * 2);
      }
    } else waterAgainMs = WATER_STALE_MS.pending;

    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const c = canvas.getContext("2d", { willReadFrequently: true });
    const each = (fn) => keys.forEach(([x, y], i) => {
      const t = got[i];
      if (!t || t instanceof Promise || !t.fills) return;
      const k = size * t.k;
      c.setTransform(k, 0, 0, k, x * size - o.x, y * size - o.y);
      fn(t);
    });
    c.fillStyle = "#000";
    each((t) => { for (const p of t.fills) c.fill(p, "evenodd"); });
    c.globalCompositeOperation = "destination-out";
    each((t) => { for (const p of t.lakes) c.fill(p, "evenodd"); });
    c.globalCompositeOperation = "source-over";
    c.setTransform(1, 0, 0, 1, 0, 0);
    // Water or not, without the faint seams where two areas meet (their edges' soft pixels
    // together still a little see-through), which would show as lines through the colours.
    const img = c.getImageData(0, 0, w, h), px = img.data, wet = new Uint8Array(w * h);
    for (let i = 0, j = 3; i < wet.length; i++, j += 4) {
      const on = px[j] >= 100;
      wet[i] = on ? 1 : 0;
      px[j] = on ? 255 : 0;
    }
    c.putImageData(img, 0, 0);
    lastMask = {
      canvas, view, box: boxKey, gen: waterGen, w, h,
      matches: (m) => viewKey(m) === view,
      isWater: (x, y) => {
        const i = Math.floor(y) * w + Math.floor(x);
        return x >= 0 && y >= 0 && x < w && y < h && wet[i] === 1;
      },
    };
    return lastMask;
  }

  // One map tile's water, for tile overlays cut to the sea (the algae map): `ctx` holds the
  // tile's picture, `size` px a side, at tile z/x/y; what isn't water is cleared. The water
  // comes from the chart tile at the same zoom (zoom 12's past it, zoom 5's before it is
  // too coarse to be worth it: left uncut). Says "ok", "pending" (chart still downloading:
  // the tile is cleared, ask again later), or "none" (no water to be had: left as it is).
  async function cutTileToWater(ctx, size, z, x, y) {
    if (typeof Path2D === "undefined" || typeof fetch === "undefined") return "none";
    if (z < WATER_MIN_Z) return "none";
    const zw = Math.min(WATER_MAX_Z, z), d = 2 ** (z - zw);
    const wx = Math.floor(x / d), wy = Math.floor(y / d);
    let t = waterTile(`${zw}/${wx}/${wy}`);
    if (t instanceof Promise) t = await Promise.race([t, pause(WATER_WAIT_MS).then(() => null)]);
    if (!t) { ctx.clearRect(0, 0, size, size); return "pending"; } // still coming: clear for now
    if (!t || t.state === "error" || !t.fills) {
      if (t && t.state === "pending") { ctx.clearRect(0, 0, size, size); return "pending"; }
      return "none";
    }
    const mask = document.createElement("canvas");
    mask.width = mask.height = size;
    const m = mask.getContext("2d");
    const k = size * d * t.k;
    m.setTransform(k, 0, 0, k, (wx * d - x) * size, (wy * d - y) * size);
    m.fillStyle = "#000";
    for (const p of t.fills) m.fill(p, "evenodd");
    m.globalCompositeOperation = "destination-out";
    for (const p of t.lakes) m.fill(p, "evenodd");
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "destination-in";
    ctx.drawImage(mask, 0, 0);
    ctx.restore();
    return t.state === "ok" ? "ok" : "pending";
  }

  // The layer's grid's reach (half a step past its outer points), for which tiles to fetch.
  function gridBox(g) {
    const la = g.lats, lo = g.lons, hy = la.length > 1 ? (la[1] - la[0]) / 2 : 0.5, hx = lo.length > 1 ? (lo[1] - lo[0]) / 2 : 0.5;
    return [la[0] - hy, lo[0] - hx, la[la.length - 1] + hy, lo[lo.length - 1] + hx];
  }

  // Numbers at grid points, far enough apart on screen (and not on the land, for the sea's).
  function labels(ctx, w, h, map, g, def, mask) {
    if (!def.label || map.getZoom() < LABEL_ZOOM) return;
    const p0 = map.latLngToContainerPoint([g.lats[0], g.lons[0]]);
    const p1 = map.latLngToContainerPoint([g.lats[Math.min(1, g.lats.length - 1)], g.lons[Math.min(1, g.lons.length - 1)]]);
    const spacing = Math.max(1, Math.min(Math.abs(p1.x - p0.x) || 99, Math.abs(p1.y - p0.y) || 99));
    const k = Math.max(1, Math.ceil(LABEL_GAP / spacing));
    ctx.font = "700 12px system-ui, -apple-system, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,0.55)";
    ctx.fillStyle = "#fff";
    for (let i = 0; i < g.lats.length; i += k) {
      for (let j = 0; j < g.lons.length; j += k) {
        const p = map.latLngToContainerPoint([g.lats[i], g.lons[j]]);
        if (p.x < -20 || p.y < -20 || p.x > w + 20 || p.y > h + 20) continue;
        if (mask && !mask.isWater(p.x, p.y)) continue;
        const v = def.value(g, g.lats[i], g.lons[j]);
        if (Number.isNaN(v)) continue;
        const text = def.label(v);
        if (!text) continue;
        ctx.strokeText(text, p.x, p.y);
        ctx.fillText(text, p.x, p.y);
      }
    }
  }

  // One colour layer per overlay id: draw() gets each hour's data. onMask(mask): told the
  // water each picture of a sea layer was cut to (null: not cut).
  function fieldRenderer(id, { onMask } = {}) {
    let layer = null, g = null, map = null, mask = null;
    const def = LAYERS[id];
    const late = () => layer?.redraw(); // water that was still on its way came in
    // The sea's layers wait for the water over the view (see waterMask) before drawing.
    const prepare = def.sea ? (mp, w, h) => (g ? waterMask(mp, w, h, gridBox(g)) : null) : undefined;
    return {
      draw(group, data, { opacity, map: m }) {
        map = m;
        g = grid(data);
        if (!layer) {
          layer = canvasLayer(function (ctx, w, h, mp, ready) {
            if (!g) return;
            if (def.fade) this._canvas.style.opacity = String((this._alpha ?? 1) * def.fade(mp.getZoom()));
            mask = ready || null;
            if (def.sea) g.seaNeed = mask ? SEA_NEED : undefined;
            paint(ctx, w, h, mp, g, def);
            if (mask) {
              ctx.globalCompositeOperation = "destination-in";
              ctx.drawImage(mask.canvas, 0, 0, w, h);
              ctx.globalCompositeOperation = "source-over";
            }
            labels(ctx, w, h, mp, g, def, mask);
            if (def.sea) onMask?.(mask);
          }, prepare);
          if (def.sea) waterLate.add(late);
          layer.setOpacity(opacity);
          layer.addTo(map);
        } else {
          layer.setOpacity(opacity);
          layer.redraw();
        }
      },
      hide() {
        waterLate.delete(late);
        layer?.remove(); layer = null; g = null; mask = null;
      },
      valueAt(latlng) {
        if (!g) return null;
        // Not on the land, where the sea's colours were cut off.
        if (mask && map && mask.matches(map)) {
          const p = map.latLngToContainerPoint(latlng);
          if (!mask.isWater(p.x, p.y)) return null;
        }
        return def.tap(g, latlng.lat, latlng.lng);
      },
      legend() { return legendFor(id, g); },
      summary() { return id === "aurora" && g?.info?.kp ? kpSummary(g.info.kp, Date.now() / 1000) : null; },
      get grid() { return g; },
    };
  }

  // What the Kp forecast means for the coming night, for southern Finland (about 60 N).
  function kpSummary(kp, now) {
    // The night ahead: 18:00 to 06:00 local time from today (or now, if it's night already).
    const d = new Date(now * 1000);
    const start = new Date(d);
    start.setHours(d.getHours() < 6 ? -6 : 18, 0, 0, 0);
    const from = start.getTime() / 1000, to = from + 12 * 3600;
    const night = kp.filter(([ts]) => ts + 3 * 3600 > from && ts < to).map(([, k]) => k);
    if (!night.length) return null;
    const max = Math.max(...night);
    const what = max >= 6 ? "a strong storm: northern lights possible overhead even in the south"
      : max >= 5 ? "a storm: good chances in southern Finland, high in the northern sky"
        : max >= 4 ? "active: often seen low in the north from the southern coast on a dark, clear night"
          : max >= 3 ? "unsettled: likely in Lapland and the north, rarely in the south"
            : "quiet: mostly only in Lapland";
    return `Tonight Kp up to ${max.toFixed(1)}: ${what}.`;
  }

  // --- the legend -----------------------------------------------------------------------

  function legendFor(id, g) {
    const def = LAYERS[id];
    if (!def) return null;
    let scaleName = def.scale;
    const scale = SCALES[scaleName];
    const s = scale.stops, lo = s[0][0], hi = s[s.length - 1][0];
    const f = scale.log ? (v) => Math.log(Math.max(v, 0.01)) : (v) => v;
    const pos = (v) => ((f(v) - f(Math.max(lo, 0.01))) / (f(hi) - f(Math.max(lo, 0.01)))) * 100;
    const grad = s.map((st) => `rgba(${st[1]},${st[2]},${st[3]},${Math.max(0.35, st[4] / 255).toFixed(2)}) ${pos(st[0]).toFixed(1)}%`).join(",");
    const note = id === "fog" && g && !g.f.vis ? "estimated" : g?.info?.source === "open-meteo" ? "Open-Meteo" : "";
    return { unit: scale.unit, gradient: `linear-gradient(90deg, ${grad})`,
      ticks: scale.ticks.map((v) => [Math.max(0, Math.min(100, pos(v))), scale.names?.[v] ?? v]), note };
  }

  // --- pressure isobars -----------------------------------------------------------------

  // Contour lines of a regular grid by marching squares: [[x, y] in grid units], one list of
  // segments per level.
  function contours(values, nx, ny, level) {
    const segs = [];
    const v = (i, j) => values[j * nx + i];
    const lerp = (a, b) => (level - a) / (b - a);
    for (let j = 0; j < ny - 1; j++) {
      for (let i = 0; i < nx - 1; i++) {
        const a = v(i, j), b = v(i + 1, j), c = v(i + 1, j + 1), d = v(i, j + 1);
        if ([a, b, c, d].some(Number.isNaN)) continue;
        const idx = (a > level ? 1 : 0) | (b > level ? 2 : 0) | (c > level ? 4 : 0) | (d > level ? 8 : 0);
        if (idx === 0 || idx === 15) continue;
        const e = {
          t: [i + lerp(a, b), j], r: [i + 1, j + lerp(b, c)], b: [i + lerp(d, c), j + 1], l: [i, j + lerp(a, d)],
        };
        const table = { 1: ["l", "t"], 2: ["t", "r"], 3: ["l", "r"], 4: ["r", "b"], 5: ["l", "t", "r", "b"], 6: ["t", "b"],
          7: ["l", "b"], 8: ["b", "l"], 9: ["b", "t"], 10: ["t", "r", "b", "l"], 11: ["b", "r"], 12: ["r", "l"], 13: ["r", "t"], 14: ["t", "l"] }[idx];
        for (let k = 0; k < table.length; k += 2) segs.push([e[table[k]], e[table[k + 1]]]);
      }
    }
    return segs;
  }

  // Highs and lows: grid points that are the highest (lowest) within `r` points around.
  function extremes(values, nx, ny, r = 3) {
    const out = [];
    for (let j = r; j < ny - r; j++) {
      for (let i = r; i < nx - r; i++) {
        const v = values[j * nx + i];
        if (Number.isNaN(v)) continue;
        let hi = true, lo = true;
        for (let dj = -r; dj <= r && (hi || lo); dj++) {
          for (let di = -r; di <= r; di++) {
            if (!di && !dj) continue;
            const o = values[(j + dj) * nx + i + di];
            if (Number.isNaN(o)) { hi = lo = false; break; }
            if (o >= v) hi = false;
            if (o <= v) lo = false;
          }
        }
        if (hi || lo) out.push({ i, j, v, kind: hi ? "H" : "L" });
      }
    }
    return out;
  }

  const ISOBAR_STEP = 2; // hPa
  const LABEL_ROOM = 70; // px between isobar labels

  function isobarRenderer() {
    let layer = null, g = null;
    return {
      draw(group, data, { opacity, map }) {
        g = grid(data);
        if (!layer) {
          layer = canvasLayer(function (ctx, w, h, mp) {
            if (!g) return;
            const p = g.f.p, nx = g.lons.length, ny = g.lats.length;
            const toScreen = ([x, y]) => {
              const i = Math.min(nx - 1.001, Math.max(0, x)), j = Math.min(ny - 1.001, Math.max(0, y));
              const i0 = Math.floor(i), j0 = Math.floor(j);
              const lon = g.lons[i0] + (g.lons[i0 + 1] - g.lons[i0]) * (i - i0);
              const lat = g.lats[j0] + (g.lats[j0 + 1] - g.lats[j0]) * (j - j0);
              return mp.latLngToContainerPoint([lat, lon]);
            };
            let lo = Infinity, hi = -Infinity;
            for (const v of p) if (!Number.isNaN(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
            if (!Number.isFinite(lo)) return;
            ctx.lineCap = "round";
            ctx.lineJoin = "round";
            ctx.font = "600 11px system-ui, -apple-system, sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            const highsLows = extremes(p, nx, ny);
            const placed = highsLows.map((ex) => mp.latLngToContainerPoint([g.lats[ex.j], g.lons[ex.i]]));
            for (let level = Math.ceil(lo / ISOBAR_STEP) * ISOBAR_STEP; level <= hi; level += ISOBAR_STEP) {
              const segs = contours(p, nx, ny, level);
              if (!segs.length) continue;
              const major = level % 10 === 0 || level % 4 === 0;
              ctx.beginPath();
              for (const [a, b] of segs) {
                const pa = toScreen(a), pb = toScreen(b);
                ctx.moveTo(pa.x, pa.y);
                ctx.lineTo(pb.x, pb.y);
              }
              ctx.strokeStyle = "rgba(255,255,255,0.85)";
              ctx.lineWidth = major ? 1.6 : 1;
              ctx.stroke();
              // A label where the line is in view and not crowded by another label (near a
              // deep low the lines bunch up): the middle segment first, then others along it.
              if (!major) continue;
              for (const f of [0.5, 0.25, 0.75, 0.125, 0.375, 0.625, 0.875]) {
                const mid = toScreen(segs[Math.floor(segs.length * f)][0]);
                if (!(mid.x > 20 && mid.y > 20 && mid.x < w - 20 && mid.y < h - 20)) continue;
                if (placed.some((q) => Math.hypot(q.x - mid.x, q.y - mid.y) < LABEL_ROOM)) continue;
                placed.push(mid);
                ctx.lineWidth = 3;
                ctx.strokeStyle = "rgba(30,41,59,0.75)";
                ctx.strokeText(String(level), mid.x, mid.y);
                ctx.fillStyle = "#fff";
                ctx.fillText(String(level), mid.x, mid.y);
                break;
              }
            }
            ctx.font = "800 18px system-ui, -apple-system, sans-serif";
            for (const ex of highsLows) {
              const s = mp.latLngToContainerPoint([g.lats[ex.j], g.lons[ex.i]]);
              if (s.x < 0 || s.y < 0 || s.x > w || s.y > h) continue;
              ctx.lineWidth = 4;
              ctx.strokeStyle = "rgba(15,23,42,0.6)";
              ctx.fillStyle = ex.kind === "H" ? "#93c5fd" : "#fca5a5";
              ctx.strokeText(ex.kind, s.x, s.y - 8);
              ctx.fillText(ex.kind, s.x, s.y - 8);
              ctx.font = "600 11px system-ui, -apple-system, sans-serif";
              ctx.lineWidth = 3;
              ctx.strokeText(String(Math.round(ex.v)), s.x, s.y + 8);
              ctx.fillStyle = "#fff";
              ctx.fillText(String(Math.round(ex.v)), s.x, s.y + 8);
              ctx.font = "800 18px system-ui, -apple-system, sans-serif";
            }
          });
          layer.setOpacity(opacity);
          layer.addTo(map);
        } else {
          layer.setOpacity(opacity);
          layer.redraw();
        }
      },
      hide() { layer?.remove(); layer = null; g = null; },
      valueAt(latlng) {
        if (!g) return null;
        const v = sample(g, "p", latlng.lat, latlng.lng);
        return Number.isNaN(v) ? null : [`${Math.round(v)} hPa`, "Air pressure at sea level"];
      },
    };
  }

  // --- lightning ------------------------------------------------------------------------

  const STRIKE_AGES = [[5, "#ffffff"], [15, "#fde047"], [30, "#fb923c"], [60, "#ef4444"]];
  const strikeColour = (ago) => (STRIKE_AGES.find(([m]) => ago < m) || STRIKE_AGES[STRIKE_AGES.length - 1])[1];

  function lightningRenderer() {
    let layer = null, data = null, fetched = 0;
    return {
      draw(group, d, { opacity, map }) {
        data = d;
        fetched = Date.now() / 1000;
        if (!layer) {
          layer = canvasLayer(function (ctx, w, h, mp) {
            if (!data) return;
            const ia = 2 + data.fields.indexOf("ago"), ic = 2 + data.fields.indexOf("cloud");
            const since = (Date.now() / 1000 - fetched) / 60;
            const pts = data.points.slice().sort((a, b) => b[ia] - a[ia]); // newest on top
            for (const p of pts) {
              const ago = p[ia] + since;
              if (ago > 60) continue;
              const s = mp.latLngToContainerPoint([p[0], p[1]]);
              if (s.x < -10 || s.y < -10 || s.x > w + 10 || s.y > h + 10) continue;
              const r = ago < 5 ? 6 : 4.5;
              ctx.globalAlpha = p[ic] === 1 ? 0.55 : 1;
              ctx.lineCap = "round";
              ctx.strokeStyle = "rgba(0,0,0,0.6)";
              ctx.lineWidth = 4;
              ctx.beginPath();
              ctx.moveTo(s.x - r, s.y); ctx.lineTo(s.x + r, s.y); ctx.moveTo(s.x, s.y - r); ctx.lineTo(s.x, s.y + r);
              ctx.stroke();
              ctx.strokeStyle = strikeColour(ago);
              ctx.lineWidth = 2;
              ctx.stroke();
            }
            ctx.globalAlpha = 1;
          });
          layer.setOpacity(opacity);
          layer.addTo(map);
        } else {
          layer.setOpacity(opacity);
          layer.redraw();
        }
      },
      hide() { layer?.remove(); layer = null; data = null; },
      valueAt(latlng, map) {
        if (!data || !map) return null;
        const ia = 2 + data.fields.indexOf("ago"), ik = 2 + data.fields.indexOf("ka");
        const s = map.latLngToContainerPoint(latlng);
        let best = null, bd = 18 * 18;
        for (const p of data.points) {
          const q = map.latLngToContainerPoint([p[0], p[1]]);
          const d = (q.x - s.x) ** 2 + (q.y - s.y) ** 2;
          if (d < bd) { bd = d; best = p; }
        }
        if (!best) return null;
        const ago = Math.round(best[ia] + (Date.now() / 1000 - fetched) / 60);
        return [`Lightning ${ago < 1 ? "just now" : `${ago} min ago`}`, best[ik] != null ? `${Math.abs(Math.round(best[ik]))} kA` : ""];
      },
      legend() {
        return { unit: "min ago", gradient: `linear-gradient(90deg, #ffffff 0%, #ffffff 8%, #fde047 25%, #fb923c 50%, #ef4444 100%)`,
          ticks: [[0, 0], [8.3, 5], [25, 15], [50, 30], [100, 60]], note: "" };
      },
      summary() {
        if (!data) return null;
        const ia = 2 + data.fields.indexOf("ago");
        const recent = data.points.filter((p) => p[ia] < 15).length;
        return data.points.length ? `${data.points.length} strikes in the last hour, ${recent} in the last 15 min` : "No lightning in the last hour";
      },
    };
  }

  // --- waves: the colours and moving swell lines ---------------------------------------

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  // The waves as a "wind" for wind.js's moving lines: travelling the way the waves go,
  // faster where they are higher.
  function swellGrid(g) {
    const n = g.lats.length * g.lons.length;
    const out = { lats: g.lats, lons: g.lons, ms: new Float32Array(n).fill(NaN), u: new Float32Array(n).fill(NaN),
      v: new Float32Array(n).fill(NaN) };
    const h = g.f.wave;
    for (let k = 0; k < n; k++) {
      const hk = h?.[k], dir = g.f.wdir?.[k];
      if (hk == null || Number.isNaN(hk) || dir == null || Number.isNaN(dir)) continue;
      const ms = 2.5 + 4 * Math.min(hk, 3);
      const a = (dir * Math.PI) / 180;
      out.ms[k] = ms; out.u[k] = -ms * Math.sin(a); out.v[k] = -ms * Math.cos(a);
    }
    return out;
  }

  function wavesRenderer() {
    // The lines keep to the water the colours were cut to (undefined: not known yet).
    let lines = null, mapNow = null, on = store.get("fw-ov-waves-lines") !== "0", last = null, water;
    const base = fieldRenderer("waves", { onMask: (m) => { water = m; lines?.setMask(m); } });
    const show = () => {
      if (!on || !last || !mapNow || typeof FerryWind === "undefined") return;
      if (!lines) {
        lines = new (FerryWind.Lines())();
        lines.setMask(water);
      }
      lines.setGrid(swellGrid(base.grid), 0.85, "rgba(255,255,255,0.85)");
      if (!mapNow.hasLayer(lines)) lines.addTo(mapNow);
    };
    return {
      draw(group, data, opts) {
        base.draw(group, data, opts);
        mapNow = opts.map;
        last = data;
        show();
      },
      hide() { base.hide(); lines?.remove(); lines = null; last = null; water = undefined; },
      valueAt: (ll) => base.valueAt(ll),
      legend: () => base.legend(),
      extra() {
        const row = document.createElement("label");
        row.className = "ov-option small muted";
        const toggle = document.createElement("input");
        toggle.type = "checkbox";
        toggle.className = "switch";
        toggle.checked = on;
        toggle.addEventListener("change", () => {
          on = toggle.checked;
          store.set("fw-ov-waves-lines", on ? "1" : "0");
          if (!on) { lines?.remove(); lines = null; } else show();
        });
        row.append("Moving swell lines", toggle);
        return row;
      },
    };
  }

  // Light pollution comes as tiles from the add-on: only its legend and a tap's words are here.
  const LP_ZONES = [["0", 0.01, "#000000"], ["1a", 0.06, "#222222"], ["1b", 0.11, "#424242"], ["2a", 0.19, "#142f72"],
    ["2b", 0.33, "#2154d8"], ["3a", 0.58, "#0f5714"], ["3b", 1, "#1fa12a"], ["4a", 1.73, "#6e641e"], ["4b", 3, "#b8a625"],
    ["5a", 5.2, "#bf641e"], ["5b", 9, "#fd9650"], ["6a", 15.59, "#fb5a49"], ["6b", 27, "#fb998a"], ["7a", 46.77, "#a0a0a0"], ["7b", 150, "#f2f2f2"]];
  const lightPollution = {
    legend() {
      const n = LP_ZONES.length;
      const grad = LP_ZONES.map(([, , c], i) => `${c} ${((i + 0.5) / n * 100).toFixed(1)}%`).join(",");
      const ticks = [[0.5, "0"], [2.5, "1"], [4.5, "2"], [6.5, "3"], [8.5, "4"], [10.5, "5"], [12.5, "6"], [14.5, "7"]]
        .map(([i, z]) => [((i - 0.5 + 0.5) / n) * 100, z]);
      return { unit: "zone", gradient: `linear-gradient(90deg, ${grad})`, ticks, note: "darker is better" };
    },
    format(d) {
      return [`Zone ${d.zone}: ${d.text}`, `${d.sqm.toFixed(2)} mag/arcsec² · light ${d.ratio < 0.1 ? d.ratio.toFixed(2) : d.ratio.toFixed(1)}× the natural sky`];
    },
  };

  // --- sunset and sunrise: the sky's score, and spots with an open view when zoomed in --

  const SPOT_ZOOM = 11;
  const TERRAIN_Z = 12; // about 30 m a pixel here
  const RAY_KM = 10; // how far the view is checked towards the sun
  // The colours spread over the sky either side of the sun, and up here the sun slides
  // along the horizon as it sets: the view is checked in five directions.
  const RAYS = [[-16, 0.5], [-8, 0.8], [0, 1], [8, 0.8], [16, 0.5]];
  const terrain = new Map(); // "z/x/y" -> Float32Array of heights, or a promise while loading
  const TERRAIN_KEEP = 80;
  const SUN_WORDS = [[80, "Fiery"], [60, "Great"], [40, "Good"], [20, "Fair"], [0, "Dull"]];
  const sunWord = (v) => SUN_WORDS.find(([limit]) => v >= limit)[1];
  const hm = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const weekday = (ts) => new Date(ts * 1000).toLocaleDateString("en-GB", { weekday: "short" });

  // Why a sunset (or sunrise) got its score, from the run's reasons at a place:
  // [headline, when and where, why].
  function sunsetWhy(g, la, lo) {
    const v = sample(g, "score", la, lo);
    if (Number.isNaN(v)) return null;
    const kind = g.info?.kinds?.[String(g.time)] || "sunset";
    const n = (f) => nearest(g, f, la, lo), m = (f) => sample(g, f, la, lo);
    const at = g.time + n("at") * 60, glow = g.time + n("glow") * 60, az = n("az");
    const way = compass(az);
    const colours = kind === "sunset" ? `best colours about ${hm(at - 300)}–${hm(glow)}` : `best colours about ${hm(glow)}–${hm(at + 300)}`;
    const canvas = m("canvas"), high = m("high"), mid = m("mid"), gap = m("gap"), low = m("low");
    const vis = m("vis"), seen = m("seen"), rain = m("rain");
    const why = [];
    if (canvas < 10) {
      why.push(`clear sky: a clean ${kind} with little colour`);
      if (gap < 60) why.push(`low cloud on the horizon to the ${way} hides the sun`);
    } else {
      const amount = canvas < 30 ? "some" : canvas < 70 ? "plenty of" : "a lot of";
      const what = high >= mid ? "high cloud" : "middle cloud";
      why.push(`${amount} ${what} to light up (high ${Math.round(high)}%, middle ${Math.round(mid)}%)`);
      why.push(gap >= 70 ? `a clear way in for the light from the ${way}` : gap >= 40
        ? `low cloud towards the ${way} lets only part of the light in` : `low cloud towards the ${way} keeps the light out: grey`);
    }
    if (low >= 35) why.push(`${Math.round(low)}% low cloud over you hides ${low >= 70 ? "it" : "much of it"}`);
    if (rain >= 0.3) why.push(`rain (${r1(rain)} mm/h)`);
    if (vis < 5) why.push(vis < 1 ? "fog" : "haze");
    if (seen < 60) why.push("the forecast doesn't reach far enough towards the sun to be sure");
    const text = why.join("; ");
    return [`${sunWord(v)} ${kind} · ${Math.round(v)}/100`,
      `${weekday(at)} ${hm(at)} towards ${Math.round(az)}° (${way}), ${colours}`,
      `${text[0].toUpperCase()}${text.slice(1)}.`];
  }

  function terrainTile(z, x, y) {
    const key = `${z}/${x}/${y}`;
    const have = terrain.get(key);
    if (have !== undefined) { terrain.delete(key); terrain.set(key, have); return have; } // most recently used last
    const p = new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        const c = document.createElement("canvas");
        c.width = c.height = 256;
        const ctx = c.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(img, 0, 0);
        const d = ctx.getImageData(0, 0, 256, 256).data;
        const h = new Float32Array(256 * 256);
        for (let i = 0; i < h.length; i++) h[i] = d[i * 4] * 256 + d[i * 4 + 1] + d[i * 4 + 2] / 256 - 32768;
        terrain.set(key, h);
        resolve(h);
      };
      img.onerror = () => { terrain.set(key, null); resolve(null); };
      img.src = `api/tiles/terrain/${z}/${x}/${y}.png`;
    });
    terrain.set(key, p);
    while (terrain.size > TERRAIN_KEEP) terrain.delete(terrain.keys().next().value);
    return p;
  }

  // Height at a place from the tiles in memory (NaN where a tile isn't there).
  function heightAt(lat, lon) {
    const n = 2 ** TERRAIN_Z * 256;
    const x = ((lon + 180) / 360) * n;
    const s = Math.sin((lat * Math.PI) / 180);
    const y = (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n;
    const t = terrain.get(`${TERRAIN_Z}/${Math.floor(x / 256)}/${Math.floor(y / 256)}`);
    if (!(t instanceof Float32Array)) return NaN;
    return t[(Math.floor(y) % 256) * 256 + (Math.floor(x) % 256)];
  }

  // Load the terrain tiles a box needs; null when they're all in, else a promise for them.
  function loadTerrain(s, w, n, e) {
    const N = 2 ** TERRAIN_Z;
    const tx = (lon) => Math.floor(((lon + 180) / 360) * N);
    const ty = (lat) => { const r = Math.sin((lat * Math.PI) / 180); return Math.floor((0.5 - Math.log((1 + r) / (1 - r)) / (4 * Math.PI)) * N); };
    const jobs = [];
    for (let x = tx(w); x <= tx(e); x++) for (let y = ty(n); y <= ty(s); y++) {
      const t = terrainTile(TERRAIN_Z, x, y);
      if (t instanceof Promise) jobs.push(t);
    }
    return jobs.length ? Promise.all(jobs).then(() => true) : null;
  }

  // The box of terrain the view needs: the map's view, and RAY_KM beyond it towards the sun.
  function terrainBox(b, az) {
    const pad = 0.6 / 111.2, k = Math.cos((b.getCenter().lat * Math.PI) / 180);
    let s = b.getSouth() - pad, n = b.getNorth() + pad, w = b.getWest() - pad / k, e = b.getEast() + pad / k;
    for (const [db] of [RAYS[0], RAYS[RAYS.length - 1]]) {
      const r = ((az + db) * Math.PI) / 180;
      const dLat = (RAY_KM * Math.cos(r)) / 111.2, dLon = (RAY_KM * Math.sin(r)) / (111.2 * k);
      s = Math.min(s, b.getSouth() + dLat - pad); n = Math.max(n, b.getNorth() + dLat + pad);
      w = Math.min(w, b.getWest() + dLon - pad / k); e = Math.max(e, b.getEast() + dLon + pad / k);
    }
    return [s, w, n, e];
  }

  // How good the view is from a place towards a bearing: how open the horizon is (degrees,
  // with the earth's curve) in five directions around it, how much open water there is
  // that way, and how far above its surroundings the place stands.
  function view(lat, lon, bearing) {
    const h0 = heightAt(lat, lon);
    if (Number.isNaN(h0) || h0 <= 0.3) return null; // the sea itself
    const eye = h0 + 2;
    const kLon = 1 / (111.2 * Math.cos((lat * Math.PI) / 180));
    let open = 0, weights = 0, horizon = -90, waterKm = 0;
    for (const [db, w] of RAYS) {
      const b = ((bearing + db) * Math.PI) / 180, sb = Math.sin(b), cb = Math.cos(b);
      let top = -90;
      for (let d = 0.04; d <= RAY_KM; d *= 1.1) {
        const h = heightAt(lat + (d * cb) / 111.2, lon + d * sb * kLon);
        if (Number.isNaN(h)) continue;
        const drop = (d * d) / (2 * 6371) * 1000 * 0.87; // m, the curve less refraction
        const ang = Math.atan2(Math.max(h, 0) - drop - eye, d * 1000) * 180 / Math.PI;
        if (ang > top) top = ang;
        if (db === 0 && h <= 0.3) waterKm += d * 0.1; // this step's length
      }
      open += w * Math.max(0, Math.min(1, (1.2 - top) / 1.6));
      weights += w;
      if (db === 0) horizon = top;
    }
    let around = 0, k = 0;
    for (let a = 0; a < 360; a += 45) {
      const r = 0.4, b = (a * Math.PI) / 180;
      const h = heightAt(lat + (r * Math.cos(b)) / 111.2, lon + r * Math.sin(b) * kLon);
      if (!Number.isNaN(h)) { around += Math.max(h, 0); k++; }
    }
    const prominence = k ? h0 - around / k : 0;
    open /= weights;
    const water = Math.min(1, waterKm / RAY_KM);
    const score = 0.6 * open + 0.2 * water + 0.2 * Math.max(0, Math.min(1, prominence / 25));
    return { score, open, horizon, water, waterKm, prominence, height: h0 };
  }

  function viewText(v, az) {
    return [`view towards the ${compass(az)} ${v.open >= 0.8 ? "open" : v.open >= 0.5 ? "partly open" : "blocked"} (${Math.round(v.open * 100)}%)`,
      `horizon ${v.horizon <= 0 ? "below eye level" : `${v.horizon.toFixed(1)}° up`}`,
      v.waterKm >= 0.3 ? `${v.waterKm < 10 ? v.waterKm.toFixed(1) : Math.round(v.waterKm)} km of open water that way` : "",
      `${Math.round(v.height)} m high${v.prominence > 3 ? `, ${Math.round(v.prominence)} m above the area` : ""}`]
      .filter(Boolean).join(" · ");
  }

  function sunsetRenderer() {
    const base = fieldRenderer("sunset");
    let spots = [], layer = null, mapNow = null, opacityNow = 0.8, waiting = false, viewAz = null;
    const spotLayer = () => canvasLayer(function (ctx, w, h, map) {
      spots = [];
      viewAz = null;
      const g = base.grid;
      if (!g || map.getZoom() < SPOT_ZOOM) return;
      const c = map.getCenter();
      const az = nearest(g, "az", c.lat, c.lng);
      if (Number.isNaN(az)) return;
      const ready = loadTerrain(...terrainBox(map.getBounds(), az));
      if (ready) {
        if (!waiting) { waiting = true; ready.then(() => { waiting = false; layer?.redraw(); }); }
        return;
      }
      viewAz = az;
      const STEP_PX = 12;
      const cand = [];
      for (let y = STEP_PX / 2; y < h; y += STEP_PX) {
        for (let x = STEP_PX / 2; x < w; x += STEP_PX) {
          const ll = map.containerPointToLatLng([x, y]);
          const v = view(ll.lat, ll.lng, az);
          if (!v) continue;
          const sky = sample(g, "score", ll.lat, ll.lng);
          v.total = v.score * (Number.isNaN(sky) ? 0.5 : 0.35 + 0.65 * sky / 100);
          v.sky = sky; v.x = x; v.y = y; v.lat = ll.lat; v.lng = ll.lng;
          cand.push(v);
          if (v.score > 0.5) {
            ctx.fillStyle = `rgba(255,${Math.round(120 + 100 * v.score)},60,${((v.score - 0.5) * 1.2).toFixed(2)})`;
            ctx.beginPath();
            ctx.arc(x, y, STEP_PX * 0.75, 0, Math.PI * 2);
            ctx.fill();
          }
        }
      }
      // The best few, apart from each other.
      cand.sort((a, b) => b.total - a.total);
      for (const v of cand) {
        if (spots.length >= 6 || v.score < 0.6) break;
        if (spots.some((s) => (s.x - v.x) ** 2 + (s.y - v.y) ** 2 < 90 * 90)) continue;
        spots.push(v);
      }
      const rad = (az * Math.PI) / 180, spread = (RAYS[RAYS.length - 1][0] * Math.PI) / 180;
      for (const s of spots) {
        // The way to look: a soft wedge over the directions checked.
        const grad = ctx.createRadialGradient(s.x, s.y, 6, s.x, s.y, 64);
        grad.addColorStop(0, "rgba(251,146,60,0.55)");
        grad.addColorStop(1, "rgba(251,146,60,0)");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.arc(s.x, s.y, 64, rad - spread - Math.PI / 2, rad + spread - Math.PI / 2);
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = "rgba(124,45,18,0.9)";
        ctx.lineWidth = 2;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(s.x + Math.sin(rad) * 40, s.y - Math.cos(rad) * 40);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#f97316";
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(s.x, s.y, 8, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.strokeStyle = "#7c2d12";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(s.x, s.y + 2, 3.5, Math.PI, 0);
        ctx.moveTo(s.x - 5, s.y + 2); ctx.lineTo(s.x + 5, s.y + 2);
        ctx.stroke();
      }
    });

    // The coming sunsets and sunrises at a place, from the add-on (one line).
    async function comingUp(ll) {
      try {
        const res = await fetch(`api/overlay-value/sunset?lat=${ll.lat.toFixed(4)}&lon=${ll.lng.toFixed(4)}`);
        const data = res.ok ? await res.json() : null;
        const evs = (data?.events || []).filter((e) => e.glow > Date.now() / 1000 - 1800);
        if (evs.length < 2) return "";
        return `Here: ${evs.map((e) => `${weekday(e.at)} ${e.kind} ${hm(e.at)} ${e.word} ${Math.round(e.score)}`).join(" · ")}`;
      } catch { return ""; }
    }

    return {
      draw(group, data, opts) {
        base.draw(group, data, { ...opts, opacity: opts.opacity * 0.85 });
        mapNow = opts.map;
        opacityNow = opts.opacity;
        if (!layer) { layer = spotLayer(); layer.addTo(mapNow); } else layer.redraw();
        layer.setOpacity(Math.min(1, opacityNow + 0.15));
      },
      hide() { base.hide(); layer?.remove(); layer = null; spots = []; },
      async valueAt(ll, map) {
        const sky = base.valueAt(ll);
        let lines = null;
        if (map && spots.length) {
          const p = map.latLngToContainerPoint(ll);
          const s = spots.find((q) => (q.x - p.x) ** 2 + (q.y - p.y) ** 2 < 20 * 20);
          if (s) {
            lines = [`Good spot · view ${Math.round(s.score * 100)}%${Number.isNaN(s.sky) ? "" : `, sky ${Math.round(s.sky)}/100`}`,
              viewText(s, viewAz)];
          }
        }
        if (!lines && map && viewAz != null && map.getZoom() >= SPOT_ZOOM) {
          const v = view(ll.lat, ll.lng, viewAz);
          if (v) lines = sky ? null : [`View from here ${Math.round(v.score * 100)}%`, viewText(v, viewAz)];
          if (v && sky) sky.push(`From here: ${viewText(v, viewAz)}`);
        }
        const out = lines ? [...lines, ...(sky ? sky.slice(1).map((x, i) => (i ? x : `${sky[0]}: ${x}`)) : [])] : sky;
        if (!out) return null;
        const more = await comingUp(ll);
        return more ? [...out, more] : out;
      },
      legend: () => base.legend(),
      summary() {
        const g = base.grid;
        if (!g) return null;
        const c = mapNow ? mapNow.getCenter() : { lat: 60.17, lng: 24.94 };
        const lines = sunsetWhy(g, c.lat, c.lng);
        return lines ? `At the map's centre: ${lines[0].replace(" · ", ", ").toLowerCase()}, at ${lines[1].replace(/^\w+ /, "")}.` : null;
      },
      view,
    };
  }

  const renderers = { pressure: isobarRenderer(), lightning: lightningRenderer(), waves: wavesRenderer(),
    sunset: sunsetRenderer(),
    "light-pollution": lightPollution };
  for (const id of Object.keys(LAYERS)) if (!renderers[id] && id !== "wind") renderers[id] = fieldRenderer(id);

  return { SCALES, LAYERS, colourAt, lut, grid, cell, sample, nearest, fogVis, contours, extremes, legendFor, fieldRenderer,
    canvasLayer, renderers, cutTileToWater, compass, strikeColour, kpSummary, sunsetWhy, sunWord };
});
