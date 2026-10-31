/* The wind forecast on the map (card T5), drawn for the "wind" overlay of overlays.js.

   Each forecast hour comes as a grid of points with speed, gust and direction. Two pictures
   of it: an arrow per grid point on the shared canvas, pointing where the wind blows and
   coloured by speed (the weather stations' colours), thinned out when zoomed out; and
   moving wind lines on a canvas of their own, each following the wind interpolated
   between the grid points. The lines run at 30 frames a second, stop while the page is
   hidden or the map zooms, and can be switched off under the layer's switch.
   The grid maths is kept apart from the drawing so the tests can run it under Node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.FerryWind = api;
    if (root.FerryOverlays) root.FerryOverlays.renderers.wind = api.renderer;
  }
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  // Under 4 m/s light; orange from 11 (the small craft warning), red from 14 (gale).
  // Shared with the weather stations, so a forecast arrow and a station read alike.
  const STEPS = [[0, "#0ea5e9", "0–3"], [4, "#16a34a", "4–7"], [8, "#ca8a04", "8–10"], [11, "#ea580c", "11–13"],
    [14, "#dc2626", "14–20"], [21, "#7c3aed", "21+"]];
  const stepOf = (ms) => {
    let i = 0;
    while (i + 1 < STEPS.length && Math.round(ms) >= STEPS[i + 1][0]) i++;
    return i;
  };
  const color = (ms) => STEPS[stepOf(ms)][1];

  function legend() {
    const ul = document.createElement("ul");
    ul.className = "legend small";
    for (const [, c, label] of STEPS) {
      const li = document.createElement("li");
      const i = document.createElement("i");
      i.className = "round";
      i.style.background = c;
      li.append(i, `${label} m/s`);
      ul.append(li);
    }
    return ul;
  }

  // --- grid maths --------------------------------------------------------------------

  // Wind from `dir` degrees at `ms` as east and north components (where it blows to).
  function uv(ms, dir) {
    const a = (dir * Math.PI) / 180;
    return [-ms * Math.sin(a), -ms * Math.cos(a)];
  }

  // A forecast hour ({ fields, points: [[lat, lon, ms, gust, dir]] }) as a regular grid:
  // the rows' latitudes and columns' longitudes (both rising) and, per cell, row by row,
  // speed and the east/north components (NaN where a point has no value).
  function grid(data) {
    const f = data.fields || ["ms", "gust", "dir"];
    const iMs = 2 + f.indexOf("ms"), iGust = 2 + f.indexOf("gust"), iDir = 2 + f.indexOf("dir");
    const lats = [...new Set(data.points.map((p) => p[0]))].sort((a, b) => a - b);
    const lons = [...new Set(data.points.map((p) => p[1]))].sort((a, b) => a - b);
    const n = lats.length * lons.length;
    const g = {
      lats, lons, ms: new Float32Array(n).fill(NaN), gust: new Float32Array(n).fill(NaN),
      u: new Float32Array(n).fill(NaN), v: new Float32Array(n).fill(NaN), dir: new Float32Array(n).fill(NaN),
    };
    const row = new Map(lats.map((v, i) => [v, i])), col = new Map(lons.map((v, i) => [v, i]));
    for (const p of data.points) {
      const k = row.get(p[0]) * lons.length + col.get(p[1]);
      const ms = p[iMs], dir = p[iDir];
      if (p[iGust] != null) g.gust[k] = p[iGust];
      if (ms == null || dir == null) continue;
      const [u, v] = uv(ms, dir);
      g.ms[k] = ms; g.dir[k] = dir; g.u[k] = u; g.v[k] = v;
    }
    return g;
  }

  // Where v falls on a rising axis: [step, 0..1 along it], or null outside.
  function cell(axis, v) {
    const last = axis.length - 1;
    if (last < 1 || !(v >= axis[0] && v <= axis[last])) return null;
    let i = 0;
    while (i < last - 1 && v > axis[i + 1]) i++;
    return [i, (v - axis[i]) / (axis[i + 1] - axis[i])];
  }

  // The wind at a place, interpolated between the four grid points around it (those with
  // a value): [east, north, speed] in m/s, or null outside the grid. Speed is interpolated
  // on its own, so it doesn't drop where directions differ.
  function sample(g, lat, lon) {
    const a = cell(g.lats, lat), b = cell(g.lons, lon);
    if (!a || !b) return null;
    const w = g.lons.length;
    let u = 0, v = 0, s = 0, total = 0;
    for (let di = 0; di < 2; di++) {
      const wy = di ? a[1] : 1 - a[1];
      for (let dj = 0; dj < 2; dj++) {
        const wx = dj ? b[1] : 1 - b[1];
        const k = (a[0] + di) * w + b[0] + dj;
        const weight = wy * wx;
        if (weight <= 0 || Number.isNaN(g.u[k])) continue;
        u += g.u[k] * weight; v += g.v[k] * weight; s += g.ms[k] * weight; total += weight;
      }
    }
    return total > 0 ? [u / total, v / total, s / total] : null;
  }

  // Every k-th row and column is drawn so arrows stay at least `gap` pixels apart, given
  // how far apart grid points are on screen.
  const thin = (spacing, gap = MIN_GAP) => Math.max(1, Math.ceil(gap / Math.max(spacing, 0.01)));

  // --- arrows ------------------------------------------------------------------------

  const MIN_GAP = 34; // px between arrows
  const LABEL_ZOOM = 9; // speeds written beside the arrows from here in

  let gridNow = null; // the hour drawn
  const gaps = new Map(); // zoom -> grid spacing on screen (px)

  function spacing(map, g) {
    const z = map.getZoom();
    if (!gaps.has(z)) {
      const mid = Math.floor(g.lats.length / 2), c = Math.floor(g.lons.length / 2);
      const p = map.project([g.lats[mid], g.lons[c]], z);
      const east = map.project([g.lats[mid], g.lons[c + 1] ?? g.lons[c]], z);
      const north = map.project([g.lats[mid + 1] ?? g.lats[mid], g.lons[c]], z);
      gaps.set(z, Math.min(Math.abs(east.x - p.x) || Infinity, Math.abs(north.y - p.y) || Infinity));
    }
    return gaps.get(z);
  }

  let Arrow = null; // made once Leaflet is there
  function arrowClass() {
    if (Arrow) return Arrow;
    Arrow = L.CircleMarker.extend({
      _updatePath() {
        const r = this._renderer;
        if (!r._drawing || this._empty()) return;
        const o = this.options, map = this._map;
        const k = thin(spacing(map, o.grid));
        if (o.row % k || o.col % k) return;
        const ctx = r._ctx, p = this._point;
        const len = 15 + Math.min(o.ms, 20) * 0.6; // a little longer when it blows harder
        const c = color(o.ms);
        ctx.save();
        ctx.globalAlpha = o.alpha;
        ctx.translate(p.x, p.y);
        ctx.rotate((((o.dir + 180) % 360) * Math.PI) / 180); // up the screen is north: point downwind
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.beginPath();
        ctx.moveTo(0, len / 2);
        ctx.lineTo(0, -len / 2 + 5);
        ctx.strokeStyle = "rgba(255,255,255,0.9)";
        ctx.lineWidth = 5;
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(0, -len / 2 - 3);
        ctx.lineTo(5.5, -len / 2 + 6);
        ctx.lineTo(-5.5, -len / 2 + 6);
        ctx.closePath();
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.fillStyle = c;
        ctx.fill();
        ctx.beginPath();
        ctx.moveTo(0, len / 2);
        ctx.lineTo(0, -len / 2 + 5);
        ctx.strokeStyle = c;
        ctx.lineWidth = 2.5;
        ctx.stroke();
        ctx.restore();
        if (map.getZoom() >= LABEL_ZOOM) {
          // The speed (and gust) under the arrow, upright.
          const text = o.gust != null && !Number.isNaN(o.gust) && map.getZoom() >= LABEL_ZOOM + 1
            ? `${Math.round(o.ms)} (${Math.round(o.gust)})` : String(Math.round(o.ms));
          ctx.save();
          ctx.globalAlpha = o.alpha;
          ctx.font = "700 11px system-ui, -apple-system, sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.lineWidth = 3;
          ctx.strokeStyle = "rgba(255,255,255,0.95)";
          ctx.strokeText(text, p.x, p.y + len / 2 + 3);
          ctx.fillStyle = c;
          ctx.fillText(text, p.x, p.y + len / 2 + 3);
          ctx.restore();
        }
      },
    });
    return Arrow;
  }

  // --- moving wind lines -------------------------------------------------------------

  const FPS = 30;
  const TRAIL = 12; // positions kept per line
  const SPEED = 0.22; // px per frame for each m/s (the same on screen at every zoom)
  const CELL = 10; // px: the wind is looked up on a screen grid this fine
  const DENSITY = 1000; // px² of wind per line
  const MAX_LINES = 2000;
  const MAX_AGE = [50, 110]; // frames a line lives, at random between these

  let Lines = null;
  function linesClass() {
    if (Lines) return Lines;
    Lines = L.Layer.extend({
      onAdd(map) {
        this._canvas = L.DomUtil.create("canvas", "wind-lines");
        this._canvas.style.position = "absolute";
        this._canvas.style.pointerEvents = "none";
        map.getPane("weather").append(this._canvas);
        this._ctx = this._canvas.getContext("2d");
        // Still while the map moves, so panning stays smooth on a slow phone; new lines after.
        this._events = { movestart: this._stop, zoomstart: this._stop, moveend: this._reset, resize: this._reset };
        map.on(this._events, this);
        this._onVisible = () => (document.hidden ? this._stop() : this._reset());
        document.addEventListener("visibilitychange", this._onVisible);
        this._reset();
      },
      onRemove(map) {
        this._stop();
        map.off(this._events, this);
        document.removeEventListener("visibilitychange", this._onVisible);
        this._canvas.remove();
        this._canvas = this._ctx = null;
      },
      setGrid(g, alpha) {
        this._grid = g;
        if (this._canvas) this._canvas.style.opacity = String(alpha);
        if (this._map && this._w) this._field(); // the lines carry on in the new hour's wind
      },
      _stop() {
        cancelAnimationFrame(this._raf);
        this._raf = 0;
        if (this._ctx) this._ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
      },
      // After a pan or zoom: the canvas over the view again, the wind looked up for it, new lines.
      _reset() {
        const map = this._map;
        if (!map || !this._canvas) return;
        this._stop();
        if (document.hidden) return;
        const size = map.getSize(), dpr = window.devicePixelRatio || 1;
        L.DomUtil.setPosition(this._canvas, map.containerPointToLayerPoint([0, 0]));
        this._canvas.width = Math.round(size.x * dpr);
        this._canvas.height = Math.round(size.y * dpr);
        this._canvas.style.width = `${size.x}px`;
        this._canvas.style.height = `${size.y}px`;
        this._dpr = dpr;
        this._w = size.x;
        this._h = size.y;
        this._field();
        this._seed();
        this._last = 0;
        this._raf = requestAnimationFrame((t) => this._frame(t));
      },
      // The wind on a screen grid (px a frame), so a line's step needs no map maths.
      _field() {
        const g = this._grid, map = this._map;
        const cols = Math.ceil(this._w / CELL) + 1, rows = Math.ceil(this._h / CELL) + 1;
        const fx = new Float32Array(cols * rows).fill(NaN), fy = new Float32Array(cols * rows).fill(NaN);
        const fs = new Uint8Array(cols * rows);
        const live = [];
        if (g && map.getZoom() >= MIN_LINES_ZOOM) {
          for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
              const ll = map.containerPointToLatLng([c * CELL, r * CELL]);
              const w = sample(g, ll.lat, ll.lng);
              if (!w) continue;
              const k = r * cols + c;
              fx[k] = w[0] * SPEED;
              fy[k] = -w[1] * SPEED; // north is up the screen
              fs[k] = stepOf(w[2]);
              live.push(k);
            }
          }
        }
        Object.assign(this, { _cols: cols, _rows: rows, _fx: fx, _fy: fy, _fs: fs, _live: live });
        const want = Math.min(MAX_LINES, Math.round((live.length * CELL * CELL) / DENSITY));
        if (!this._xs || this._n !== want) this._seed(want);
      },
      _seed(n = this._n || 0) {
        this._n = n;
        this._xs = new Float32Array(n * TRAIL);
        this._ys = new Float32Array(n * TRAIL);
        this._len = new Uint8Array(n); // positions in the trail so far
        this._age = new Uint16Array(n);
        this._life = new Uint16Array(n);
        for (let i = 0; i < n; i++) this._spawn(i, true);
      },
      _spawn(i, scatter) {
        const live = this._live;
        if (!live.length) { this._len[i] = 0; return; }
        const k = live[Math.floor(Math.random() * live.length)];
        const x = (k % this._cols) * CELL + (Math.random() - 0.5) * CELL;
        const y = Math.floor(k / this._cols) * CELL + (Math.random() - 0.5) * CELL;
        this._xs[i * TRAIL] = x;
        this._ys[i * TRAIL] = y;
        this._len[i] = 1;
        this._life[i] = MAX_AGE[0] + Math.random() * (MAX_AGE[1] - MAX_AGE[0]);
        this._age[i] = scatter ? Math.random() * this._life[i] : 0; // not all at once
      },
      // The wind at a canvas point, between the four screen grid points around it.
      _at(x, y) {
        const cx = x / CELL, cy = y / CELL, c = Math.floor(cx), r = Math.floor(cy);
        if (c < 0 || r < 0 || c + 1 >= this._cols || r + 1 >= this._rows) return null;
        const k = r * this._cols + c, cols = this._cols, fx = this._fx, fy = this._fy;
        const a = k, b = k + 1, d = k + cols, e = k + cols + 1;
        if (Number.isNaN(fx[a]) || Number.isNaN(fx[b]) || Number.isNaN(fx[d]) || Number.isNaN(fx[e])) return null;
        const tx = cx - c, ty = cy - r;
        const top = fx[a] + (fx[b] - fx[a]) * tx, bottom = fx[d] + (fx[e] - fx[d]) * tx;
        const topY = fy[a] + (fy[b] - fy[a]) * tx, bottomY = fy[d] + (fy[e] - fy[d]) * tx;
        return [top + (bottom - top) * ty, topY + (bottomY - topY) * ty, this._fs[tx < 0.5 ? (ty < 0.5 ? a : d) : (ty < 0.5 ? b : e)]];
      },
      _frame(t) {
        this._raf = requestAnimationFrame((tt) => this._frame(tt));
        if (t - this._last < 1000 / FPS - 4) return; // 30 a second, also on a 120 Hz screen
        // Where frames come slower, each step is longer, so the wind looks as fast.
        const step = this._last ? Math.min(3, (t - this._last) / (1000 / FPS)) : 1;
        this._last = t;
        const started = performance.now();
        this.step(step);
        this.draw();
        this.cost = (this.cost || 0) * 0.95 + (performance.now() - started) * 0.05; // ms a frame, for checks
      },
      // Each line one step on: its trail shifts, a new head where the wind takes it.
      step(by = 1) {
        const xs = this._xs, ys = this._ys, len = this._len, age = this._age, life = this._life;
        this._colour = this._colour && this._colour.length === this._n ? this._colour : new Uint8Array(this._n);
        for (let i = 0; i < this._n; i++) {
          const o = i * TRAIL;
          if (!len[i] || ++age[i] > life[i]) { this._spawn(i, false); continue; }
          const head = o + len[i] - 1;
          const w = this._at(xs[head], ys[head]);
          if (!w) { this._spawn(i, false); continue; }
          let h = head;
          if (len[i] === TRAIL) { // drop the oldest point
            xs.copyWithin(o, o + 1, o + TRAIL);
            ys.copyWithin(o, o + 1, o + TRAIL);
            h = head - 1;
          } else len[i]++;
          xs[h + 1] = xs[h] + w[0] * by;
          ys[h + 1] = ys[h] + w[1] * by;
          this._colour[i] = w[2];
        }
      },
      // The lines by colour, the older half of each trail fainter: one path per colour and half.
      draw() {
        const ctx = this._ctx, dpr = this._dpr;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.lineWidth = 1.2;
        ctx.lineCap = "round";
        const xs = this._xs, ys = this._ys, len = this._len, colour = this._colour;
        if (!colour) return;
        const half = TRAIL / 2;
        for (let band = 0; band < 2; band++) {
          ctx.globalAlpha = band ? 0.85 : 0.35;
          for (let s = 0; s < STEPS.length; s++) {
            ctx.beginPath();
            let any = false;
            for (let i = 0; i < this._n; i++) {
              if (colour[i] !== s || len[i] < 2) continue;
              const o = i * TRAIL, n = len[i];
              // Older half: the first points up to the middle; newer: from there to the head.
              const from = band ? Math.max(0, n - half - 1) : 0, to = band ? n - 1 : Math.max(0, n - half - 1);
              if (to <= from) continue;
              ctx.moveTo(xs[o + from], ys[o + from]);
              for (let j = from + 1; j <= to; j++) ctx.lineTo(xs[o + j], ys[o + j]);
              any = true;
            }
            if (any) {
              ctx.strokeStyle = STEPS[s][1];
              ctx.stroke();
            }
          }
        }
        ctx.globalAlpha = 1;
      },
    });
    return Lines;
  }

  const MIN_LINES_ZOOM = 5;

  // --- the renderer overlays.js calls ------------------------------------------------

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  const LINES_KEY = "fw-ov-wind-lines";
  let linesOn = store.get(LINES_KEY) !== "0";
  let lines = null, lastMap = null, lastAlpha = 0.9;

  const renderer = {
    // A forecast hour: arrows into the group (cleared by overlays.js between hours), and
    // the moving lines told about the new wind.
    draw(group, data, { opacity, canvas, map }) {
      const g = grid(data);
      gridNow = g;
      lastMap = map;
      lastAlpha = opacity;
      gaps.clear();
      const ArrowMark = arrowClass();
      const w = g.lons.length;
      for (let k = 0; k < g.ms.length; k++) {
        if (Number.isNaN(g.ms[k])) continue;
        const row = Math.floor(k / w), col = k % w;
        new ArrowMark([g.lats[row], g.lons[col]], {
          renderer: canvas(), radius: 24, stroke: false, fill: false, interactive: false,
          ms: g.ms[k], gust: g.gust[k], dir: g.dir[k], row, col, grid: g, alpha: opacity,
        }).addTo(group);
      }
      if (linesOn) {
        if (!lines) lines = new (linesClass())();
        lines.setGrid(g, opacity);
        if (!map.hasLayer(lines)) lines.addTo(map);
      }
    },
    // The layer switched off (or no hour to show): the lines go too.
    hide() {
      lines?.remove();
      lines = null;
      gridNow = null;
    },
    // Under the switch: the lines on or off, and the colours.
    extra() {
      const row = document.createElement("label");
      row.className = "ov-option small muted";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.className = "switch";
      toggle.checked = linesOn;
      toggle.addEventListener("change", () => {
        linesOn = toggle.checked;
        store.set(LINES_KEY, linesOn ? "1" : "0");
        if (!linesOn) { lines?.remove(); lines = null; }
        else if (gridNow && lastMap) {
          lines = new (linesClass())();
          lines.setGrid(gridNow, lastAlpha);
          lines.addTo(lastMap);
        }
      });
      row.append("Moving wind lines", toggle);
      const box = document.createElement("div");
      box.append(row, legend());
      return box;
    },
    get lines() { return lines; },
  };

  return { STEPS, color, stepOf, legend, uv, grid, cell, sample, thin, renderer };
});
