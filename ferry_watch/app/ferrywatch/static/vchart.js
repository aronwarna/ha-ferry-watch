/* Ferry Watch: the vector nautical chart.
 *
 * Draws the chart tiles the add-on cuts from Traficom's depth data and
 * Väylävirasto's fairway data (see vchart.py), in the colours and symbols of
 * Finnish paper charts. Exposes window.FerryChart.
 */
(function () {
  "use strict";

  const C = {
    land: "#f6e7ad",
    coast: "rgba(70, 64, 40, 0.95)",
    depth: ["#c9e0b4", "#a6d1f0", "#c4e2f6", "#e1f0fa", "#ffffff"], // drying, 0-3, 3-6, 6-10, deeper
    contour: "#3b8fcc",
    sounding: "#24405c",
    fairway: "#1a1a1a",
    fairwayArea: "rgba(110, 110, 110, 0.10)",
    magenta: "#c0399f",
    red: "#d42a2a",
    green: "#1d9a3c",
    yellow: "#f2c200",
    black: "#1a1a1a",
  };

  const TYPE_NAMES = {
    v: "spar buoy", p: "buoy", r: "beacon", l: "leading mark", k: "cairn", m: "lighthouse",
    s: "sector light", d: "directional light", a: "minor light", t: "radar beacon", o: "mark",
  };
  const CATEGORY_NAMES = {
    1: "Port (red)", 2: "Starboard (green)", 3: "North cardinal", 4: "South cardinal",
    5: "West cardinal", 6: "East cardinal", 7: "Isolated danger", 8: "Safe water", 9: "Special",
  };

  // --- drawing helpers ------------------------------------------------------------

  function path(ctx, flat, close) {
    ctx.moveTo(flat[0], flat[1]);
    for (let i = 2; i < flat.length; i += 2) ctx.lineTo(flat[i], flat[i + 1]);
    if (close) ctx.closePath();
  }

  function fillRings(ctx, rings, color) {
    ctx.beginPath();
    for (const ring of rings) path(ctx, ring, true);
    ctx.fillStyle = color;
    ctx.fill("evenodd");
    // A hairline of the same colour hides the seams between neighbouring pieces.
    ctx.strokeStyle = color;
    ctx.lineWidth = 0.6;
    ctx.stroke();
  }

  function strokeLine(ctx, flat, color, width, dash) {
    ctx.beginPath();
    path(ctx, flat, false);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Midpoint and direction of the longest segment, for a label along a line.
  function labelSpot(flat) {
    let best = null;
    for (let i = 0; i + 3 < flat.length; i += 2) {
      const dx = flat[i + 2] - flat[i], dy = flat[i + 3] - flat[i + 1];
      const len = Math.hypot(dx, dy);
      if (!best || len > best.len) best = { len, x: flat[i] + dx / 2, y: flat[i + 1] + dy / 2, a: Math.atan2(dy, dx) };
    }
    if (best && (best.a > Math.PI / 2 || best.a < -Math.PI / 2)) best.a += Math.PI;
    return best;
  }

  function haloText(ctx, text, x, y, font, color) {
    ctx.font = font;
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
    ctx.strokeText(text, x, y);
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }

  // Soundings as on Finnish charts: whole metres, with the decimal as a small subscript.
  function sounding(ctx, x, y, depth, s) {
    const whole = Math.floor(depth);
    const tenth = Math.round((depth - whole) * 10);
    ctx.fillStyle = C.sounding;
    ctx.font = `italic ${10 * s}px system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    if (depth >= 20 || tenth === 0 || tenth === 10) {
      ctx.fillText(String(tenth === 10 ? whole + 1 : whole), x, y);
      return;
    }
    const w = ctx.measureText(String(whole)).width;
    ctx.fillText(String(whole), x - 2 * s, y);
    ctx.font = `italic ${7 * s}px system-ui, sans-serif`;
    ctx.fillText(String(tenth), x + w / 2, y + 3 * s);
  }

  // --- navigation aids --------------------------------------------------------------

  function lightFlare(ctx, x, y, s) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(Math.PI * 0.75);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.bezierCurveTo(-4 * s, 5 * s, -3 * s, 13 * s, 0, 14 * s);
    ctx.bezierCurveTo(3 * s, 13 * s, 4 * s, 5 * s, 0, 0);
    ctx.fillStyle = C.magenta;
    ctx.globalAlpha = 0.85;
    ctx.fill();
    ctx.restore();
  }

  // Colour bands (top to bottom) and topmark for a buoyage category.
  function marking(cat) {
    switch (cat) {
      case 1: return { bands: [C.red], top: null };
      case 2: return { bands: [C.green], top: null };
      case 3: return { bands: [C.black, C.yellow], top: "N" };
      case 4: return { bands: [C.yellow, C.black], top: "S" };
      case 5: return { bands: [C.yellow, C.black, C.yellow], top: "W" };
      case 6: return { bands: [C.black, C.yellow, C.black], top: "E" };
      case 7: return { bands: [C.black, C.red, C.black], top: "I" };
      case 8: return { bands: [C.red, "#ffffff"], top: "SW" };
      case 9: return { bands: [C.yellow], top: "X" };
      default: return { bands: [C.black], top: null };
    }
  }

  function bandedShape(ctx, outline, bands, box) {
    // Fill a shape with horizontal colour bands, then outline it.
    ctx.save();
    ctx.beginPath();
    outline();
    ctx.clip();
    const h = box.h / bands.length;
    bands.forEach((color, i) => {
      ctx.fillStyle = color;
      ctx.fillRect(box.x, box.y + i * h, box.w, h + 0.5);
    });
    ctx.restore();
    ctx.beginPath();
    outline();
    ctx.lineWidth = 0.8;
    ctx.strokeStyle = C.black;
    ctx.stroke();
  }

  function topmark(ctx, kind, x, y, s) {
    // Drawn above (x, y), stacking upwards.
    const t = 3.2 * s;
    const tri = (cx, cy, up) => {
      ctx.beginPath();
      if (up) { ctx.moveTo(cx - t, cy); ctx.lineTo(cx + t, cy); ctx.lineTo(cx, cy - 1.7 * t); }
      else { ctx.moveTo(cx - t, cy - 1.7 * t); ctx.lineTo(cx + t, cy - 1.7 * t); ctx.lineTo(cx, cy); }
      ctx.closePath();
      ctx.fill();
    };
    ctx.fillStyle = C.black;
    ctx.strokeStyle = C.black;
    const gap = 1.9 * t;
    switch (kind) {
      case "N": tri(x, y, true); tri(x, y - gap, true); break;
      case "S": tri(x, y, false); tri(x, y - gap, false); break;
      case "E": tri(x, y, false); tri(x, y - gap, true); break;
      case "W": tri(x, y, true); tri(x, y - gap, false); break;
      case "I":
        ctx.beginPath(); ctx.arc(x, y - t, t * 0.8, 0, 7); ctx.fill();
        ctx.beginPath(); ctx.arc(x, y - 3 * t, t * 0.8, 0, 7); ctx.fill();
        break;
      case "SW":
        ctx.fillStyle = C.red;
        ctx.beginPath(); ctx.arc(x, y - t, t * 0.9, 0, 7); ctx.fill();
        break;
      case "X":
        ctx.strokeStyle = C.yellow; ctx.lineWidth = 1.6 * s;
        ctx.beginPath();
        ctx.moveTo(x - t, y - 2 * t); ctx.lineTo(x + t, y);
        ctx.moveTo(x + t, y - 2 * t); ctx.lineTo(x - t, y);
        ctx.stroke();
        break;
      default: break;
    }
  }

  function drawAid(ctx, aid, s, showName) {
    const [x, y, type, cat, lit, name] = aid;
    const { bands, top } = marking(cat);
    if (lit) lightFlare(ctx, x, y, s);
    ctx.save();
    ctx.lineJoin = "round";
    if (type === "v" || type === "p") {
      // Spar buoy: a slim slanted spar. Buoy: a squat pillar buoy. Both on a small base circle.
      const tall = type === "v" ? 13 * s : 10 * s;
      const half = type === "v" ? 1.5 * s : 3.4 * s;
      ctx.translate(x, y);
      ctx.rotate(0.3);
      bandedShape(ctx, () => {
        ctx.moveTo(-half, 0);
        ctx.lineTo(-half * (type === "p" ? 0.55 : 1), -tall);
        ctx.lineTo(half * (type === "p" ? 0.55 : 1), -tall);
        ctx.lineTo(half, 0);
        ctx.closePath();
      }, bands, { x: -half, y: -tall, w: 2 * half, h: tall });
      ctx.rotate(-0.3);
      if (top && s >= 0.8) {
        ctx.rotate(0.3);
        topmark(ctx, top, 0, -tall - 1.5 * s, s * 0.85);
        ctx.rotate(-0.3);
      }
      ctx.beginPath();
      ctx.arc(0, 0, 1.6 * s, 0, 7);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.lineWidth = 0.8;
      ctx.strokeStyle = C.black;
      ctx.stroke();
    } else if (type === "r") {
      // Fixed beacon: an upright tower on a base line.
      ctx.translate(x, y);
      const tall = 12 * s, half = 2.6 * s;
      bandedShape(ctx, () => {
        ctx.moveTo(-half, 0); ctx.lineTo(-half * 0.6, -tall); ctx.lineTo(half * 0.6, -tall); ctx.lineTo(half, 0); ctx.closePath();
      }, bands, { x: -half, y: -tall, w: 2 * half, h: tall });
      ctx.beginPath(); ctx.moveTo(-4 * s, 0); ctx.lineTo(4 * s, 0);
      ctx.lineWidth = 1.2; ctx.strokeStyle = C.black; ctx.stroke();
      if (top && s >= 0.8) topmark(ctx, top, 0, -tall - 1.5 * s, s * 0.85);
    } else if (type === "l") {
      // Leading mark: a board on a post.
      ctx.translate(x, y);
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = C.black;
      ctx.lineWidth = 0.9;
      ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(0, -6 * s); ctx.stroke();
      ctx.beginPath(); ctx.rect(-3.5 * s, -14 * s, 7 * s, 8 * s); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, -14 * s); ctx.lineTo(0, -6 * s); ctx.lineWidth = 2 * s; ctx.stroke();
    } else if (type === "k") {
      // Cairn: a small open triangle.
      ctx.translate(x, y);
      ctx.beginPath(); ctx.moveTo(-4 * s, 0); ctx.lineTo(4 * s, 0); ctx.lineTo(0, -7 * s); ctx.closePath();
      ctx.fillStyle = "#fff"; ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = C.black; ctx.stroke();
    } else if (type === "m") {
      // Lighthouse: a five-pointed star with a dot.
      ctx.translate(x, y);
      ctx.beginPath();
      for (let i = 0; i < 10; i++) {
        const r = (i % 2 ? 2.6 : 7) * s, a = -Math.PI / 2 + i * Math.PI / 5;
        ctx.lineTo(r * Math.cos(a), r * Math.sin(a));
      }
      ctx.closePath();
      ctx.fillStyle = "#fff"; ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = C.black; ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 1.4 * s, 0, 7); ctx.fillStyle = C.black; ctx.fill();
    } else if (type === "t") {
      ctx.translate(x, y);
      ctx.beginPath(); ctx.arc(0, 0, 5 * s, 0, 7);
      ctx.lineWidth = 1.2; ctx.strokeStyle = C.magenta; ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 1.4 * s, 0, 7); ctx.fillStyle = C.black; ctx.fill();
    } else {
      // Lights and other marks: a dot in a ring.
      ctx.translate(x, y);
      ctx.beginPath(); ctx.arc(0, 0, 2.6 * s, 0, 7);
      ctx.fillStyle = "#fff"; ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = C.black; ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 1.1 * s, 0, 7); ctx.fillStyle = C.black; ctx.fill();
    }
    ctx.restore();
    if (showName && name) {
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      haloText(ctx, name, x + 7 * s, y + 2 * s, `italic ${9.5 * s}px system-ui, sans-serif`, "#333");
    }
  }

  // --- a tile -------------------------------------------------------------------------

  function drawTile(canvas, data, zoom, dpr) {
    const ctx = canvas.getContext("2d");
    const k = (canvas.width / data.e);
    ctx.setTransform(k, 0, 0, k, 0, 0);
    // Symbol and text sizes are in CSS pixels; tile units are half a CSS pixel.
    const unit = data.e / 256;
    const s = unit * (zoom >= 13 ? 1 : zoom >= 12 ? 0.85 : 0.7);

    if (data.land) {
      ctx.beginPath();
      for (const ring of data.land) path(ctx, ring, true);
      ctx.fillStyle = C.land;
      ctx.fill("evenodd");
    }

    // Water on its own canvas, so a soft shadow around it draws the coastline.
    if (data.depth?.length) {
      const water = document.createElement("canvas");
      water.width = canvas.width;
      water.height = canvas.height;
      const w = water.getContext("2d");
      w.setTransform(k, 0, 0, k, 0, 0);
      for (const [band, ...rings] of data.depth) fillRings(w, rings, C.depth[band] ?? C.depth[4]);
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.shadowColor = C.coast;
      ctx.shadowBlur = 1.2 * dpr;
      ctx.drawImage(water, 0, 0);
      ctx.restore();
    }

    for (const [code, , ...rings] of data.farea || []) {
      if (code === 1 || code === 11) {
        ctx.beginPath();
        for (const ring of rings) path(ctx, ring, true);
        ctx.fillStyle = C.fairwayArea;
        ctx.fill("evenodd");
      } else {
        for (const ring of rings) strokeLine(ctx, ring, C.magenta, 0.9 * unit, [4 * unit, 3 * unit]);
      }
    }

    for (const [depth, line] of data.contour || []) {
      const dash = depth <= 3 ? [2 * unit, 2 * unit] : depth <= 6 ? [5 * unit, 2 * unit] : null;
      strokeLine(ctx, line, C.contour, (depth >= 20 ? 0.5 : 0.75) * unit, dash);
    }

    for (const line of data.lead || []) strokeLine(ctx, line, C.fairway, 0.8 * unit);

    for (const [draught, line] of data.fline || []) {
      if (draught == null) strokeLine(ctx, line, C.fairway, 0.9 * unit, [5 * unit, 3.5 * unit]);
      else strokeLine(ctx, line, C.fairway, (zoom >= 12 ? 1.3 : 1) * unit);
    }
    if (zoom >= 12) {
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      for (const [draught, line] of data.fline || []) {
        if (draught == null) continue;
        const spot = labelSpot(line);
        // Only where the whole label fits in this tile, so it is never cut at an edge.
        const m = 30 * unit;
        if (!spot || spot.len < 70 * unit || spot.x < m || spot.y < m || spot.x > data.e - m || spot.y > data.e - m) continue;
        ctx.save();
        ctx.translate(spot.x, spot.y);
        ctx.rotate(spot.a);
        haloText(ctx, `${draught.toFixed(1)} m`, 0, -2 * unit, `600 ${9.5 * unit}px system-ui, sans-serif`, C.fairway);
        ctx.restore();
      }
    }

    if (zoom >= 13) for (const [x, y, depth] of data.sounding || []) sounding(ctx, x, y, depth, unit);

    // Light sectors: coloured arcs round the light. A sector "from 200° to 238°" is seen
    // from ships bearing 200-238° towards the light, so it lies on the opposite side.
    if (zoom >= 12) {
      const r = (zoom >= 14 ? 22 : 16) * unit;
      const sectorColor = { v: C.yellow, k: C.yellow, p: C.red, vi: C.green };
      for (const [x, y, from, to, colour] of data.sector || []) {
        const a1 = ((from + 180 - 90) * Math.PI) / 180;
        const a2 = ((to + 180 - 90) * Math.PI) / 180;
        ctx.beginPath();
        ctx.arc(x, y, r, a1, a2);
        ctx.strokeStyle = sectorColor[colour] || C.yellow;
        ctx.lineWidth = 3.2 * unit;
        ctx.stroke();
        // Thin dashed limits, like the chart's sector lines.
        ctx.beginPath();
        for (const a of [a1, a2]) {
          ctx.moveTo(x + Math.cos(a) * 3 * unit, y + Math.sin(a) * 3 * unit);
          ctx.lineTo(x + Math.cos(a) * (r + 6 * unit), y + Math.sin(a) * (r + 6 * unit));
        }
        ctx.strokeStyle = "rgba(26, 26, 26, 0.7)";
        ctx.lineWidth = 0.6 * unit;
        ctx.setLineDash([3 * unit, 2 * unit]);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // Floating marks before fixed ones, lights last so their flares stay visible.
    const order = { v: 0, p: 0, k: 1, l: 1, r: 2, o: 2, t: 2, a: 3, d: 3, s: 3, m: 4 };
    const aids = (data.aid || []).slice().sort((a, b) => (order[a[2]] ?? 2) - (order[b[2]] ?? 2));
    for (const aid of aids) drawAid(ctx, aid, s, zoom >= 14);
  }

  // --- the Leaflet layer -------------------------------------------------------------

  const tileAids = new Map(); // tile key -> [{ latlng, aid }]
  const RETRY_MS = 2500;
  let waitingTiles = 0; // tiles whose data is still downloading
  let waitingChanged = null;

  const ChartLayer = L.GridLayer.extend({
    options: { minZoom: 10, maxZoom: 19, updateWhenZooming: false, keepBuffer: 2, zIndex: 3 },

    createTile(coords, done) {
      const dpr = Math.min(window.devicePixelRatio || 1, 3);
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 256 * dpr;
      const controller = new AbortController();
      canvas._abort = controller;
      let waiting = false;
      const setWaiting = (on) => {
        if (on === waiting) return;
        waiting = on;
        waitingTiles += on ? 1 : -1;
        waitingChanged?.(waitingTiles);
      };
      controller.signal.addEventListener("abort", () => setWaiting(false));
      // The server answers 202 while an area's data is still downloading; ask again shortly.
      const load = () => {
        fetch(`api/vchart/${coords.z}/${coords.x}/${coords.y}`, { signal: controller.signal })
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
          .then((data) => {
            if (data.pending) {
              setWaiting(true);
              canvas._retry = setTimeout(load, RETRY_MS);
              return;
            }
            setWaiting(false);
            drawTile(canvas, data, coords.z, dpr);
            this._rememberAids(coords, data);
            done(null, canvas);
          })
          .catch((err) => {
            setWaiting(false);
            done(err.name === "AbortError" ? null : err, canvas);
          });
      };
      load();
      return canvas;
    },

    _rememberAids(coords, data) {
      if (!data.aid?.length) return;
      const size = this.getTileSize();
      const nw = coords.scaleBy(size);
      const list = data.aid.filter(([x, y]) => x >= 0 && y >= 0 && x < data.e && y < data.e).map((aid) => ({
        latlng: this._map.unproject(nw.add([aid[0] * size.x / data.e, aid[1] * size.y / data.e]), coords.z),
        aid,
      }));
      tileAids.set(this._tileCoordsToKey(coords), list);
    },

    onAdd(map) {
      L.GridLayer.prototype.onAdd.call(this, map);
      this.on("tileunload", (e) => {
        clearTimeout(e.tile._retry);
        e.tile._abort?.abort();
        tileAids.delete(this._tileCoordsToKey(e.coords));
      });
    },
  });

  // The navigation aid nearest a tap, if one is within reach.
  function aidAt(map, latlng, maxPx = 18) {
    const p = map.latLngToContainerPoint(latlng);
    let best = null, bestD = maxPx;
    for (const list of tileAids.values()) {
      for (const item of list) {
        const d = p.distanceTo(map.latLngToContainerPoint(item.latlng));
        if (d < bestD) { best = item; bestD = d; }
      }
    }
    if (!best) return null;
    const [, , type, cat, lit, name] = best.aid;
    const kind = TYPE_NAMES[type] || "mark";
    const category = CATEGORY_NAMES[cat];
    return {
      latlng: best.latlng,
      name: name || null,
      description: `${category ? `${category} ${kind}` : kind[0].toUpperCase() + kind.slice(1)}${lit ? ", lit" : ""}`,
    };
  }

  window.FerryChart = {
    layer: (options) => new ChartLayer(options),
    aidAt,
    colors: C,
    onWaiting: (fn) => { waitingChanged = fn; },
  };
})();
