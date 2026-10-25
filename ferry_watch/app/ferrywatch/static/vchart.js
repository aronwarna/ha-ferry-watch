/* Ferry Watch: the vector nautical chart.
 *
 * Draws the chart tiles the add-on cuts from Traficom's depth data, Väylävirasto's
 * fairway data, the Land Survey's rocks, names and topography and Museovirasto's wreck
 * register (see vchart.py), in the inks, symbols and lettering of Traficom's paper charts.
 *
 * Geometry, symbols and soundings are drawn into the map tiles. Names, light characters
 * and other labels are placed once for the whole screen on a canvas above the tiles, so
 * they never collide across tile edges: each tile hands over its label candidates and the
 * boxes of its symbols, and the overlay places the labels by importance, each at the best
 * free spot round its symbol. Exposes window.FerryChart.
 */
(function () {
  "use strict";

  // The inks of Traficom's paper chart.
  const C = {
    land: "#f9efca",
    coast: "#000000",
    depth: ["#c9e0b4", "#e4f1fb", "#e4f1fb", "#e4f1fb", "#ffffff", "#ffffff"], // drying, 0-3, 3-6, 6-10, deeper, unsurveyed
    contour: "#00a3e7", // 10 m and deeper, and their labels
    contourShallow: "#008bc5", // 3 m dotted, 6 m dashed
    sounding: "#000000",
    fairway: "#000000",
    fairwayArea: "#eceded",
    builtUp: "#eceded",
    band: "#80ce51", // under the small-craft trunk route, the key's green
    magenta: "#9d3289",
    magentaLight: "#c47db5",
    red: "#c63541",
    green: "#5fa86c",
    yellow: "#f2d100",
    black: "#000000",
    brown: "#bea26a",
    fort: "#6b3a4f", // coastal defence sites ashore
    height: "#c8b07c", // land contours
    heightText: "#9c7a45",
    lake: "#d6ebf8",
    buildArea: "#ddd3b4",
    tssLane: "rgba(157, 50, 137, 0.07)",
    tssZone: "rgba(157, 50, 137, 0.26)",
    tssSep: "rgba(157, 50, 137, 0.5)",
    unsurveyed: "#a9cce8", // the key's lines across an unsurveyed area
    // Lights in the key's own inks: flares and sectors; a white light's arcs and ring in
    // orange-yellow with a thin dark edge.
    lightY: "#fcd205",
    lightR: "#ee294a",
    lightG: "#33b875",
    lightArc: "#f6b10a",
    wreckFill: "#d6eefa", // inside the dotted ring of a dangerous wreck
  };
  const SANS = '"Helvetica Neue", Helvetica, Arial, "Liberation Sans", Roboto, sans-serif';
  const SERIF = '"Times New Roman", Times, "Liberation Serif", "Noto Serif", serif';

  // Symbol and text sizes below are in "chart pixels"; SCALE says how many CSS pixels one
  // is at each zoom. Up to zoom 13 the paper chart's own size (1:50 000 shown at about its
  // printed scale); closer in it grows, as the paper chart does when magnified, but gentler.
  // Built-up area tints by kind (home and other areas take C.buildArea).
  const BUILD_AREA = { industry: "#d8d4c8", public: "#d6c9a6", church: "#d6c9a6" };

  const SCALE = { 8: 0.42, 9: 0.45, 10: 0.5, 11: 0.54, 12: 0.62, 13: 0.78, 14: 1.0, 15: 1.18, 16: 1.34, 17: 1.46 };
  const scaleAt = (z) => SCALE[z] ?? (z < 8 ? 0.42 : 1.55);
  const MIN_TEXT = 10; // CSS px: names stay readable when zoomed out
  const OVERVIEW_Z = 9; // at and below this, the overview chart's look: no clutter, big names

  const LIGHT = { W: C.lightY, R: C.lightR, G: C.lightG, Y: C.lightY };
  const ARC = { W: C.lightArc, R: C.lightR, G: C.lightG, Y: C.lightArc }; // sector arcs and rings
  // A sector light zoomed out, as the key draws it on small-scale charts: one flare per
  // colour from the star, red to the lower left, yellow straight down, green to the lower right.
  const FAN = { R: 215, W: 180, G: 145, Y: 180 };
  const SECTOR = { v: "W", p: "R", vi: "G", k: "Y" };

  const TYPE_NAMES = {
    v: "spar buoy", p: "buoy", r: "beacon", l: "leading mark", k: "cairn", m: "lighthouse",
    s: "sector light", d: "directional light", a: "minor light", t: "radar beacon", o: "mark",
    b: "beacon tower", u: "radar mark",
  };
  const CATEGORY_NAMES = {
    1: "Port (red)", 2: "Starboard (green)", 3: "North cardinal", 4: "South cardinal",
    5: "West cardinal", 6: "East cardinal", 7: "Isolated danger", 8: "Safe water", 9: "Special",
  };

  // Towns are lettered large, as on the chart; other settlements get small capitals.
  const TOWNS = new Set(["Hanko", "Hangö", "Tammisaari", "Ekenäs", "Inkoo", "Ingå", "Kirkkonummi", "Kyrkslätt",
    "Espoo", "Esbo", "Helsinki", "Helsingfors", "Porvoo", "Borgå", "Loviisa", "Lovisa", "Kotka", "Hamina",
    "Fredrikshamn", "Paldiski", "Tallinn", "Porkkala", "Porkala"]);
  const OPEN_WATER = /(fjärd|fjärden|selkä|selät|bukt|bukten|lahti|viken)$/i; // lettered larger than other waters

  // Museovirasto's register, in English for the tap popup.
  const HERITAGE_CLASSES = ["protected ancient monument", "other heritage site", "other site",
    "possible ancient monument", "find spot", "observation"];
  const HERITAGE_KINDS = { 1: "Wreck (wooden)", 2: "Wreck (metal)", 3: "Wreck", 4: "Underwater site", 5: "Coastal defence site" };
  const DATINGS = {
    historiallinen: "historic", keskiaikainen: "medieval", moderni: "modern", ajoittamaton: "undated",
    "ei määritelty": "undated", esihistoriallinen: "prehistoric", kivikautinen: "Stone Age",
    varhaismetallikautinen: "Early Metal Age", pronssikautinen: "Bronze Age", rautakautinen: "Iron Age",
    "rautakautinen ja/tai keskiaikainen": "Iron Age or medieval", moniperiodinen: "several periods",
    "ensimmäinen maailmansota": "First World War", "toinen maailmansota": "Second World War",
  };

  const options = { heritage: true };
  let serverMinZoom = 8; // the lowest zoom the server cuts tiles for (state.vchart.min_zoom)

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
    // A hairline of the same colour hides the seams between neighbouring pieces. Not along
    // the straight edges where a piece was cut at a chart block's edge: there an island
    // cut in two has its edge on top of the area's own, and a hairline would show as a
    // thin channel across the land (zoomed out, where block edges fall inside tiles).
    ctx.beginPath();
    for (const ring of rings) {
      for (let i = 0, n = ring.length; i < n; i += 2) {
        const x0 = ring[i], y0 = ring[i + 1], x1 = ring[(i + 2) % n], y1 = ring[(i + 3) % n];
        if (x0 === x1 || y0 === y1) continue;
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
      }
    }
    const cap = ctx.lineCap;
    ctx.lineCap = "round";
    ctx.strokeStyle = color;
    ctx.lineWidth = 0.6;
    ctx.stroke();
    ctx.lineCap = cap;
  }

  // A line, optionally left out between two distances along it (where a label goes).
  function strokeLine(ctx, flat, color, width, dash, gap) {
    ctx.beginPath();
    if (gap) gapPath(ctx, flat, gap.from, gap.to);
    else path(ctx, flat, false);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function strokeRings(ctx, rings, color, width, dash) {
    ctx.beginPath();
    for (const ring of rings) path(ctx, ring, true);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function gapPath(ctx, flat, from, to) {
    let d = 0;
    let phase = from > 0 ? 0 : 1; // 0 before the gap, 1 in it, 2 after it
    if (phase === 0) ctx.moveTo(flat[0], flat[1]);
    for (let i = 2; i < flat.length; i += 2) {
      const x0 = flat[i - 2], y0 = flat[i - 1], x1 = flat[i], y1 = flat[i + 1];
      const len = Math.hypot(x1 - x0, y1 - y0);
      if (!len) continue;
      if (phase === 0) {
        if (d + len < from) ctx.lineTo(x1, y1);
        else {
          const t = (from - d) / len;
          ctx.lineTo(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t);
          phase = 1;
        }
      }
      if (phase === 1) {
        if (d + len > to) {
          const t = Math.max(0, (to - d) / len);
          ctx.moveTo(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t);
          ctx.lineTo(x1, y1);
          phase = 2;
        }
      } else if (phase === 2) ctx.lineTo(x1, y1);
      d += len;
    }
  }

  function lineLength(flat) {
    let d = 0;
    for (let i = 2; i < flat.length; i += 2) d += Math.hypot(flat[i] - flat[i - 2], flat[i + 1] - flat[i - 1]);
    return d;
  }

  // The longest nearly straight stretch of a line that is at least `need` long: its ends
  // and where it starts and ends along the line.
  function straightRun(flat, need) {
    const n = flat.length >> 1;
    let best = null;
    let i = 0, di = 0;
    while (i < n - 1) {
      let j = i + 1;
      let arc = Math.hypot(flat[2 * j] - flat[2 * i], flat[2 * j + 1] - flat[2 * i + 1]);
      while (j + 1 < n) {
        const step = Math.hypot(flat[2 * j + 2] - flat[2 * j], flat[2 * j + 3] - flat[2 * j + 1]);
        const chord = Math.hypot(flat[2 * j + 2] - flat[2 * i], flat[2 * j + 3] - flat[2 * i + 1]);
        if (chord < 0.985 * (arc + step)) break;
        arc += step;
        j++;
      }
      if (arc >= need && (!best || arc > best.d1 - best.d0)) best = { i, j, d0: di, d1: di + arc };
      di += arc;
      i = j;
    }
    if (!best) return null;
    return {
      x0: flat[2 * best.i], y0: flat[2 * best.i + 1], x1: flat[2 * best.j], y1: flat[2 * best.j + 1],
      d0: best.d0, d1: best.d1,
    };
  }

  function ringArea(flat) {
    let a = 0;
    for (let i = 0, n = flat.length; i < n; i += 2) {
      const j = (i + 2) % n;
      a += flat[i] * flat[j + 1] - flat[j] * flat[i + 1];
    }
    return a / 2;
  }

  function ringCentroid(flat) {
    let a = 0, cx = 0, cy = 0;
    for (let i = 0, n = flat.length; i < n; i += 2) {
      const j = (i + 2) % n;
      const f = flat[i] * flat[j + 1] - flat[j] * flat[i + 1];
      a += f;
      cx += (flat[i] + flat[j]) * f;
      cy += (flat[i + 1] + flat[j + 1]) * f;
    }
    return a ? [cx / (3 * a), cy / (3 * a)] : null;
  }

  function insideRings(rings, x, y) {
    let inside = false;
    for (const r of rings) {
      for (let i = 0, n = r.length, j = n - 2; i < n; j = i, i += 2) {
        if ((r[i + 1] > y) !== (r[j + 1] > y) && x < ((r[j] - r[i]) * (y - r[i + 1])) / (r[j + 1] - r[i + 1]) + r[i]) {
          inside = !inside;
        }
      }
    }
    return inside;
  }

  // A spot inside an area with room for a w x h symbol or label within the tile: the
  // area's centroid when that will do, else the nearest point of a coarse grid that does.
  function labelPoint(rings, e, w, h) {
    const fits = (x, y) => x - w / 2 >= 0 && x + w / 2 <= e && y - h / 2 >= 0 && y + h / 2 <= e && insideRings(rings, x, y);
    const c = ringCentroid(rings[0]);
    if (c && fits(c[0], c[1])) return c;
    let best = null, bestD = Infinity;
    for (let gx = e / 16; gx < e; gx += e / 8) {
      for (let gy = e / 16; gy < e; gy += e / 8) {
        if (!fits(gx, gy)) continue;
        const d = c ? Math.hypot(gx - c[0], gy - c[1]) : 0;
        if (d < bestD) { best = [gx, gy]; bestD = d; }
      }
    }
    return best;
  }

  // Canvases reused by every tile, instead of two new full-size ones per tile.
  const scratchCanvases = [];
  function scratch(i, size) {
    let c = scratchCanvases[i];
    if (!c) c = scratchCanvases[i] = document.createElement("canvas");
    if (c.width !== size) {
      c.width = size; // also clears it
      c.height = size;
    }
    const g = c.getContext("2d");
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalCompositeOperation = "source-over";
    g.clearRect(0, 0, size, size);
    return c;
  }

  const overlaps = (a, b) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];

  // --- text -----------------------------------------------------------------------

  const HAS_SPACING = typeof CanvasRenderingContext2D !== "undefined" && "letterSpacing" in CanvasRenderingContext2D.prototype;

  const widths = new Map(); // font + text -> width, for the labels measured on every redraw
  function textWidth(ctx, text, spacing) {
    const key = ctx.font + "\u0000" + text;
    let w = widths.get(key);
    if (w === undefined) {
      if (widths.size > 8000) widths.clear();
      w = ctx.measureText(text).width;
      widths.set(key, w);
    }
    return w + spacing * (text.length - 1);
  }

  // Text drawn rightwards from (x, y), with extra space between the letters as the
  // chart prints its capitals.
  // With `halo`, a thin pale outline first keeps it readable over lines and figures.
  function spacedText(ctx, text, x, y, spacing, halo) {
    ctx.textAlign = "left";
    const put = (t, px) => {
      if (halo) ctx.strokeText(t, px, y);
      ctx.fillText(t, px, y);
    };
    if (halo) {
      ctx.strokeStyle = HALO;
      ctx.lineWidth = halo;
      ctx.lineJoin = "round";
    }
    if (!spacing) {
      put(text, x);
    } else if (HAS_SPACING) {
      ctx.letterSpacing = `${spacing}px`;
      put(text, x);
      ctx.letterSpacing = "0px";
    } else {
      for (const ch of text) {
        put(ch, x);
        x += ctx.measureText(ch).width + spacing;
      }
    }
  }
  const HALO = "rgba(255, 255, 255, 0.8)";

  // A label along a line, on its longest straight stretch, kept whole inside the tile.
  // Returns the stretch of line to leave out under it (null if it's beside the line), or
  // false if it found no room. These stay in the tiles, as they break the line under them.
  // `o.draw(ctx, w)` with `o.width` draws something other than plain text, centred on 0, 0.
  function alongLine(ctx, label, flat, e, o) {
    ctx.font = o.font;
    const w = o.width ?? textWidth(ctx, o.text, o.spacing || 0);
    const run = straightRun(flat, Math.max(o.need, w + 2 * (o.pad || 0)));
    if (!run) return false;
    const at = o.at ?? 0.5;
    let cx = run.x0 + (run.x1 - run.x0) * at;
    let cy = run.y0 + (run.y1 - run.y0) * at;
    if (o.offset) {
      cx += o.offset[0];
      cy += o.offset[1];
    }
    // Along a line crossing many tiles, only every other tile labels it: in the tile
    // column (or row, for a steep line) parity given.
    if (o.alternate && (Math.abs(run.x1 - run.x0) >= Math.abs(run.y1 - run.y0) ? o.alternate.x : o.alternate.y) % 2) return false;
    let a = Math.atan2(run.y1 - run.y0, run.x1 - run.x0);
    if (a > Math.PI / 2 || a < -Math.PI / 2) a += Math.PI; // keep it upright
    const h = o.size * 0.9;
    const hx = (Math.abs(Math.cos(a)) * w + Math.abs(Math.sin(a)) * h) / 2;
    const hy = (Math.abs(Math.sin(a)) * w + Math.abs(Math.cos(a)) * h) / 2;
    const box = [cx - hx, cy - hy, cx + hx, cy + hy];
    if (box[0] < 1 || box[1] < 1 || box[2] > e - 1 || box[3] > e - 1 || !label.free(box)) return false;
    label.claim(box, true, () => {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(a);
      ctx.font = o.font;
      ctx.fillStyle = ctx.strokeStyle = o.color;
      ctx.textBaseline = "alphabetic";
      if (o.draw) o.draw(ctx, w);
      else spacedText(ctx, o.text, -w / 2, 0.35 * o.size, o.spacing || 0);
      ctx.restore();
    });
    if (o.offset) return null;
    const d = run.d0 + (run.d1 - run.d0) * at;
    return { from: d - w / 2 - (o.pad || 0), to: d + w / 2 + (o.pad || 0) };
  }

  // Soundings as on Finnish charts: sloping figures, the decimal smaller, lower and to the
  // right. (x, y) is the middle of the whole metres.
  function sounding(ctx, x, y, depth, s) {
    const whole = Math.floor(depth + 1e-6);
    const tenth = Math.round((depth - whole) * 10);
    const big = 21 * s, small = 13.5 * s, base = y + 0.36 * big;
    ctx.fillStyle = C.sounding;
    ctx.textBaseline = "alphabetic";
    ctx.font = `italic ${big}px ${SANS}`;
    const text = String(tenth === 10 ? whole + 1 : whole);
    if (depth >= 20 || tenth === 0 || tenth === 10) {
      ctx.textAlign = "center";
      ctx.fillText(text, x, base);
      return;
    }
    const w = ctx.measureText(text).width;
    ctx.textAlign = "left";
    ctx.fillText(text, x - w / 2, base);
    ctx.font = `italic ${small}px ${SANS}`;
    ctx.fillText(String(tenth), x + w / 2 - 0.04 * big, base + 0.18 * big);
  }

  function soundingBox(x, y, depth, s) {
    const big = 21 * s;
    const whole = String(Math.round(depth >= 20 ? depth : Math.floor(depth + 1e-6)));
    const decimal = depth < 20 && Math.round((depth - Math.floor(depth + 1e-6)) * 10) % 10 !== 0;
    // A little room round each figure, so neighbouring soundings never run together.
    const w = 0.58 * big * whole.length, pad = 0.14 * big;
    return [x - w / 2 - pad, y - 0.45 * big, x + w / 2 + (decimal ? 0.5 * big : 0) + pad, y + (decimal ? 0.62 : 0.45) * big];
  }

  const fmtDeg = (v) => `${v.toFixed(1).padStart(5, "0").replace(".", ",")}°`;
  // A leading line's bearing as the key prints it, both ways with a long dash: 269.3 ->
  // "269,3° — 089,3°"; a directional light's with a short one: "255,5° - 075,5°".
  const bearingText = (b, dash = "—") => `${fmtDeg(b)} ${dash} ${fmtDeg((b + 180) % 360)}`;

  // A fairway's draught as the key sets it into the line: the figure, its decimal smaller,
  // between two angle brackets whose points the line runs into: ⟨4,5⟩. A boat route's
  // advisory draught slopes. Returns the alongLine options for it.
  function draughtLabel(ctx, draught, boat, size) {
    const whole = `${Math.floor(draught + 1e-6)},`;
    const tenth = String(Math.round((draught - Math.floor(draught + 1e-6)) * 10) % 10);
    const big = `${boat ? "italic " : ""}${size}px ${SANS}`, small = `${boat ? "italic " : ""}${0.7 * size}px ${SANS}`;
    ctx.font = big;
    const wb = textWidth(ctx, whole, 0);
    ctx.font = small;
    const ws = textWidth(ctx, tenth, 0);
    const depth = 0.34 * size, room = 0.16 * size;
    const width = wb + ws + 2 * (depth + room);
    return {
      text: whole + tenth, font: big, size, width, need: 3 * width, pad: 0,
      draw(g, w) {
        const x0 = -w / 2 + depth + room, base = 0.36 * size;
        g.font = big; g.textAlign = "left";
        g.fillText(whole, x0, base);
        g.font = small;
        g.fillText(tenth, x0 + wb - 0.04 * size, base + (boat ? 0.14 * size : 0)); // a boat route's sits lower
        g.beginPath();
        for (const sign of [-1, 1]) {
          g.moveTo(sign * (w / 2 - depth), -0.42 * size); g.lineTo(sign * w / 2, 0); g.lineTo(sign * (w / 2 - depth), 0.42 * size);
        }
        g.lineWidth = 0.075 * size; g.lineJoin = "miter"; g.stroke();
      },
    };
  }

  // A light's character as charts print it: ("Fl (2) 10 s", "WRG") -> "Fl(2)WRG.10s", and
  // ("Iso WRG 4s", "WRG") -> "Iso.WRG.4s" when the character names its colours itself.
  function lightText(character, colours) {
    const m = character.match(/^(.*?)\s*(\d+(?:[.,]\d+)?)\s*s$/);
    const parts = (m ? m[1] : character).trim().split(/\s+/);
    let own = parts.length > 1 && /^[WRGY]+$/.test(parts[parts.length - 1]) ? parts.pop() : null;
    let rhythm = parts.join(" ").replace(/\s+\(/g, "(").replace(/\s+/g, "");
    const glued = rhythm.match(/^(.*\))([WRGY]+)$/); // "Fl(2)WRG"
    if (glued && !own) [, rhythm, own] = glued;
    const given = own ?? colours;
    const col = given === "W" ? "" : given; // a single white light has no letter, as on charts
    let text = rhythm;
    if (col) text += (/\)$/.test(text) ? "" : ".") + col;
    if (m) text += (/\)$/.test(text) && !col ? "" : ".") + `${m[2].replace(".", ",")}s`;
    return text;
  }

  // --- navigation aids --------------------------------------------------------------
  // Shapes after Traficom's symbol key (merkkienselitys): every buoy and beacon stands
  // upright on its position, a small circle on a short base line.

  // A light's flare as the key prints it: a teardrop, pointed at the light and round at its
  // far end, yellow (white light, edged black), red or green. `bearing` is the compass
  // direction it points to.
  function flare(ctx, x, y, s, lw, color, bearing) {
    const L = 46 * s, r = 7 * s, cy = L - r;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(((bearing - 180) * Math.PI) / 180); // local +y points along the bearing
    ctx.beginPath();
    ctx.moveTo(0, 0.5 * s);
    ctx.bezierCurveTo(-0.25 * r, 0.3 * L, -r, cy - 1.6 * r, -r, cy);
    ctx.arc(0, cy, r, Math.PI, 0, true); // round far end
    ctx.bezierCurveTo(r, cy - 1.6 * r, 0.25 * r, 0.3 * L, 0, 0.5 * s);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    if (color === C.lightY) {
      ctx.lineWidth = 0.9 * lw;
      ctx.strokeStyle = C.black;
      ctx.stroke();
    }
    ctx.restore();
  }

  // Where a flare lies, for the labels to keep clear of: a box round its far half.
  function flareBox(x, y, s, bearing) {
    const a = (bearing * Math.PI) / 180;
    const d = 30 * s, r = 10 * s;
    const cx = x + Math.sin(a) * d, cy = y - Math.cos(a) * d;
    return [cx - r, cy - r, cx + r, cy + r];
  }

  // The compass bearing out to sea of the leading line a mark stands on: the tile says so
  // (aid[7]); a tile from before that only has the lines, so the nearest one's far end.
  function leadBearing(aid, leads) {
    if (aid.length > 7) return aid[7] ?? null;
    for (const row of leads) {
      const line = Array.isArray(row[1]) ? row[1] : row[0];
      if (!Array.isArray(line)) continue;
      let near = Infinity;
      for (let i = 0; i < line.length; i += 2) near = Math.min(near, Math.hypot(line[i] - aid[0], line[i + 1] - aid[1]));
      if (near > 6) continue;
      const n = line.length;
      const dA = Math.hypot(line[0] - aid[0], line[1] - aid[1]);
      const dB = Math.hypot(line[n - 2] - aid[0], line[n - 1] - aid[1]);
      const [fx, fy] = dA > dB ? [line[0], line[1]] : [line[n - 2], line[n - 1]];
      return ((Math.atan2(fx - aid[0], aid[1] - fy) * 180) / Math.PI + 360) % 360;
    }
    return null;
  }

  const lightColours = (aid) => [...new Set(String(aid[6] || "").replace(/[^WRGY]/g, ""))];
  const norm = (deg) => ((deg % 360) + 360) % 360;
  // Whether a sector (from° clockwise to°, bearings towards the light) holds a bearing.
  const holds = (from, to, b) => norm(b - from) <= norm(to - from);

  // How a light is drawn, after the key:
  // - on a leading line, a leading light: only its flare along the line, out to sea (no arc
  //   and no bar, whatever narrow sector Väylä gives it); sectors well off the line stay;
  // - a directional light, or a light with nothing but a narrow beam: a flare and a solid
  //   black line out along the beam, its bearing in red ("Suuntaloisto");
  // - a sector light: coloured arcs, a narrow white sector a yellow wedge;
  // - else one flare in its colour.
  // `rows` are its sector rows. Returns { lead?, dir?, beam?, arcs }: arcs as { from, to,
  // letter, span, a1, a2 } with a1..a2 canvas angles round the light.
  const NARROW = 8; // degrees: a white sector this narrow is the light's beam
  // Tile units: how far round a light its arcs (and the dotted limits, 1.3 times as far) and
  // a directional light's beam reach, all within the 448 units round the tile the server
  // sends lights and sectors for, so a light outside the tile draws the same in both.
  const ARC_MAX = 330, BEAM_MAX = 420;
  // Where along a directional light's beam its bearing may go, nearest the middle first.
  const BEAM_AT = Array.from({ length: 17 }, (_, i) => 0.5 + (i % 2 ? 1 : -1) * Math.ceil(i / 2) * 0.05);
  function lightPlan(aid, rows, leads) {
    const type = aid ? aid[2] : "s";
    const arcs = rows.map(([, , from, to, colour]) => {
      const a1 = ((from + 90) * Math.PI) / 180;
      let a2 = ((to + 90) * Math.PI) / 180;
      if (a2 <= a1) a2 += 2 * Math.PI;
      return { from, to, letter: SECTOR[colour] || "W", span: norm(to - from) || 360, a1, a2 };
    });
    const lead = aid && "lsda".includes(type) ? leadBearing(aid, leads) : null;
    if (lead != null) {
      const toward = norm(lead + 180);
      return { lead, arcs: arcs.filter((a) => a.span > 15 && !holds(a.from, a.to, toward)) };
    }
    const narrow = arcs.filter((a) => a.letter === "W" && a.span <= NARROW);
    const total = arcs.reduce((sum, a) => sum + a.span, 0);
    if (arcs.length && (type === "d" || (narrow.length && total <= 30))) {
      const beam = narrow[0] || arcs.reduce((m, a) => (a.span < m.span ? a : m));
      const toward = norm(beam.from + beam.span / 2);
      return { dir: norm(toward + 180), toward, beam, arcs: [] };
    }
    return { arcs };
  }

  // Flares for a light, by its plan: along its leading line or beam, a fan for a sector
  // light zoomed out, else one. Returns the compass bearings drawn.
  function drawFlares(ctx, aid, s, lw, plan, close) {
    const colours = lightColours(aid);
    const main = LIGHT[colours.includes("W") ? "W" : colours[0]] || C.lightY;
    const along = plan.lead ?? plan.dir;
    if (along != null) {
      flare(ctx, aid[0], aid[1], s, lw, plan.beam ? LIGHT[plan.beam.letter] : main, along);
      return [along];
    }
    if (close && plan.arcs.length) return []; // its arcs show its colours
    if (colours.length > 1 || plan.arcs.length) {
      const fan = colours.length ? colours : [...new Set(plan.arcs.map((a) => a.letter))];
      for (const c of fan) flare(ctx, aid[0], aid[1], s, lw, LIGHT[c], FAN[c]);
      return fan.map((c) => FAN[c]);
    }
    flare(ctx, aid[0], aid[1], s, lw, main, 135);
    return [135];
  }

  // The position of a buoy or beacon: a small circle on a short base line.
  const FOOT = 2.1; // the position circle's radius
  function base(ctx, s, lw, half = 6.5) {
    const f = FOOT * s;
    ctx.lineWidth = lw;
    ctx.strokeStyle = C.black;
    ctx.beginPath();
    ctx.moveTo(-half * s, 0); ctx.lineTo(-f, 0);
    ctx.moveTo(f, 0); ctx.lineTo(half * s, 0);
    ctx.moveTo(f, 0); ctx.arc(0, 0, f, 0, 7);
    ctx.stroke();
  }

  // Topmarks as the key draws them, standing on (x, y): port an open square, starboard a
  // cone, cardinals two cones, isolated danger two balls, safe water a ring, special an X.
  // Returns the topmark's height.
  function topmark(ctx, cat, x, y, s, lw) {
    const w = 4.8 * s, h = 7.6 * s, gap = 0.9 * s;
    const cone = (yb, up) => {
      ctx.beginPath();
      ctx.moveTo(x - w, yb); ctx.lineTo(x + w, yb); ctx.lineTo(x, up ? yb - h : yb + h);
      ctx.closePath();
      ctx.fill();
    };
    const ball = (cy, r) => { ctx.beginPath(); ctx.arc(x, cy, r, 0, 7); ctx.fill(); };
    ctx.fillStyle = C.black;
    ctx.strokeStyle = C.black;
    let top = 0;
    switch (cat) {
      case 1: ctx.lineWidth = 1.5 * s; ctx.strokeRect(x - 3 * s, y - 9 * s, 6 * s, 8.3 * s); top = 9; break;
      case 2: ctx.beginPath(); ctx.moveTo(x - w, y); ctx.lineTo(x + w, y); ctx.lineTo(x, y - 9.5 * s); ctx.closePath(); ctx.fill(); top = 10; break;
      case 3: cone(y, true); cone(y - h - gap, true); top = 16; break; // north: both up
      case 4: cone(y - h, false); cone(y - 2 * h - gap, false); top = 16; break; // south: both down
      case 5: cone(y, true); cone(y - 2 * h - gap, false); top = 16; break; // west: points together
      case 6: cone(y - h, false); cone(y - h - gap, true); top = 16; break; // east: bases together
      case 7: ball(y - 3.3 * s, 3.3 * s); ball(y - 10 * s, 3.3 * s); top = 13; break;
      case 8: ctx.lineWidth = 1.4 * s; ctx.beginPath(); ctx.arc(x, y - 3.9 * s, 3.2 * s, 0, 7); ctx.stroke(); top = 8; break;
      case 9:
        ctx.lineWidth = 1.7 * s;
        ctx.beginPath();
        ctx.moveTo(x - 3.8 * s, y - 7.6 * s); ctx.lineTo(x + 3.8 * s, y);
        ctx.moveTo(x + 3.8 * s, y - 7.6 * s); ctx.lineTo(x - 3.8 * s, y);
        ctx.stroke();
        top = 8;
        break;
      default: break;
    }
    ctx.lineWidth = lw;
    return top * s;
  }

  // How far each kind of symbol reaches round its point (chart px: left, top, right,
  // bottom), for labels and soundings to keep clear of.
  const EXTENT = {
    v: [-7, -46, 7, 3], p: [-8, -42, 18, 3], r: [-9, -38, 9, 3], u: [-11, -38, 11, 3], l: [-7, -17, 7, 3],
    k: [-7, -6, 7, 1], b: [-8, -14, 8, 3], m: [-10, -10, 10, 10], s: [-7, -7, 7, 7], d: [-5, -5, 5, 5],
    a: [-5, -5, 5, 5], t: [-4, -4, 4, 4], o: [-4, -4, 4, 4],
  };
  // A spar buoy's or buoy's lean (degrees clockwise from upright): the tile's, else the
  // key's usual way (a buoy leans right).
  const USUAL_LEAN = { v: 0, p: 23 };
  const leanOf = (aid) => (aid[2] in USUAL_LEAN ? aid[8] ?? USUAL_LEAN[aid[2]] : 0);
  function markExtent(aid) {
    const ext = EXTENT[aid[2]] || EXTENT.o;
    if (!(aid[2] in USUAL_LEAN)) return ext;
    // Its symbol turned round its foot: the box round the turned top and the foot.
    const a = (leanOf(aid) * Math.PI) / 180, h = -ext[1], w = 8;
    const tx = Math.sin(a) * h, ty = -Math.cos(a) * h;
    return [Math.min(-w, tx - w), Math.min(-3, ty - w), Math.max(w, tx + w), 3];
  }
  function markBox(aid, s) {
    const [l, t, r, b] = markExtent(aid);
    return [aid[0] + l * s, aid[1] + t * s, aid[0] + r * s, aid[1] + b * s];
  }

  // A star of n points round (0, 0), its points R out and its notches `inner` (of R) in.
  function starPath(ctx, n, R, inner) {
    ctx.beginPath();
    for (let i = 0; i < 2 * n; i++) {
      const r = i % 2 ? inner * R : R, a = -Math.PI / 2 + (i * Math.PI) / n;
      ctx.lineTo(r * Math.cos(a), r * Math.sin(a));
    }
    ctx.closePath();
  }

  // A light's star as the key draws it: an open five-pointed star with a bold outline.
  function lightStar(ctx, R, s) {
    starPath(ctx, 5, R, 0.45);
    ctx.fillStyle = "#ffffff";
    ctx.fill();
    ctx.lineWidth = Math.max(1.2 * s, 0.17 * R);
    ctx.lineJoin = "miter";
    ctx.strokeStyle = C.black;
    ctx.stroke();
  }

  // The black symbol of a buoy, beacon or light, as the key draws them.
  function drawMark(ctx, aid, s, lw, zoom) {
    const [x, y, type, cat] = aid;
    ctx.save();
    ctx.translate(x, y);
    ctx.fillStyle = C.black;
    ctx.strokeStyle = C.black;
    ctx.lineWidth = lw;
    ctx.lineJoin = "miter";
    const tops = zoom >= 12;
    if (type === "v") {
      // Spar buoy: a thin stick with its topmark (lit or not; a light adds its flare), upright
      // or leaning as the tile says.
      base(ctx, s, lw);
      ctx.rotate((leanOf(aid) * Math.PI) / 180);
      ctx.beginPath();
      ctx.moveTo(0, -FOOT * s); ctx.lineTo(0, -28 * s);
      ctx.lineWidth = 1.5 * s;
      ctx.stroke();
      if (tops) topmark(ctx, cat, 0, -28.5 * s, s, lw);
    } else if (type === "p") {
      // Buoy: a black bell leaning (to the right unless the tile says otherwise), its
      // topmark leaning with it.
      base(ctx, s, lw);
      ctx.rotate((leanOf(aid) * Math.PI) / 180);
      const top = -24 * s;
      ctx.beginPath();
      ctx.moveTo(-6.8 * s, -FOOT * s); ctx.quadraticCurveTo(-3 * s, -8 * s, -2.4 * s, top + 2.4 * s);
      ctx.arc(0, top + 2.4 * s, 2.4 * s, Math.PI, 0);
      ctx.quadraticCurveTo(3 * s, -8 * s, 6.8 * s, -FOOT * s);
      ctx.closePath();
      ctx.fill();
      ctx.beginPath(); ctx.moveTo(0, top); ctx.lineTo(0, top - 3 * s); ctx.lineWidth = 1.5 * s; ctx.stroke();
      if (tops) topmark(ctx, cat, 0, top - 3 * s, s, lw);
    } else if (type === "r" || type === "u") {
      // Edge mark: a short black bar on its circle, a thin stick and the topmark. A radar
      // mark: the bar under a radar reflector's arc.
      base(ctx, s, lw, 9);
      if (type === "r") {
        ctx.fillRect(-2.4 * s, -14 * s, 4.8 * s, 14 * s - FOOT * s);
        ctx.beginPath(); ctx.moveTo(0, -14 * s); ctx.lineTo(0, -21 * s); ctx.lineWidth = 1.4 * s; ctx.stroke();
        if (tops) topmark(ctx, cat, 0, -21 * s, s, lw);
      } else {
        ctx.fillRect(-2.4 * s, -14 * s, 4.8 * s, 14 * s - FOOT * s);
        ctx.beginPath();
        ctx.arc(0, -12 * s, 11 * s, (215 * Math.PI) / 180, (325 * Math.PI) / 180);
        for (const deg of [222, 246, 270, 294, 318]) {
          const a = (deg * Math.PI) / 180;
          ctx.moveTo(Math.cos(a) * 11 * s, -12 * s + Math.sin(a) * 11 * s);
          ctx.lineTo(Math.cos(a) * 14.5 * s, -12 * s + Math.sin(a) * 14.5 * s);
        }
        ctx.lineWidth = 1.5 * s; ctx.lineCap = "round"; ctx.stroke();
      }
    } else if (type === "l") {
      // Leading beacon: a thin upright black bar on its circle, always upright.
      base(ctx, s, lw);
      ctx.fillRect(-1.8 * s, -16 * s, 3.6 * s, 16 * s - FOOT * s);
    } else if (type === "k") {
      // Cairn: a small black mound on a base line.
      ctx.beginPath(); ctx.moveTo(-7 * s, 0); ctx.lineTo(7 * s, 0); ctx.lineWidth = lw; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(-4.8 * s, 0); ctx.lineTo(-3 * s, -4.8 * s); ctx.lineTo(3 * s, -4.8 * s); ctx.lineTo(4.8 * s, 0);
      ctx.closePath(); ctx.fill();
    } else if (type === "b") {
      // Beacon tower (båk): a tall open tower over its circle.
      base(ctx, s, lw);
      ctx.beginPath();
      ctx.moveTo(-5.8 * s, 0); ctx.lineTo(-3.2 * s, -13 * s); ctx.lineTo(3.2 * s, -13 * s); ctx.lineTo(5.8 * s, 0);
      ctx.lineWidth = 1.2 * lw; ctx.stroke();
    } else if (type === "m") {
      // Lighthouse: a black six-pointed star with thin rays round a small open centre.
      const R = 10 * s;
      starPath(ctx, 6, R, 0.3);
      ctx.fill();
      ctx.beginPath(); ctx.arc(0, 0, 0.17 * R, 0, 7); ctx.fillStyle = "#ffffff"; ctx.fill();
    } else if (type === "s") {
      lightStar(ctx, 7 * s, s);
    } else if (type === "d" || type === "a") {
      lightStar(ctx, 5 * s, s);
    } else if (type === "t") {
      // Radar beacon: a small ringed dot; its magenta ring is drawn with the flares.
      ctx.beginPath(); ctx.arc(0, 0, FOOT * s, 0, 7); ctx.lineWidth = lw; ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 1 * s, 0, 7); ctx.fill();
    } else {
      // Other marks: a dot in a ring.
      ctx.beginPath(); ctx.arc(0, 0, 2.8 * s, 0, 7); ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 1.2 * s, 0, 7); ctx.fill();
    }
    ctx.restore();
  }

  // --- rocks, wrecks and heritage ---------------------------------------------------

  // Rocks as the key draws them, the bar being the water line: T under water, + awash, ⊥
  // above water. A cluster of rocks (kivikko) is the same sign with dots: two beside the
  // foot of a T, two beside the top of a ⊥, four round a +.
  const ROCK_REACH = 9; // chart px round a rock's point that its symbol covers
  function drawRock(ctx, x, y, kind, s, group) {
    const a = 8 * s, h = 11 * s;
    ctx.lineWidth = 1.9 * s;
    ctx.lineCap = "butt";
    ctx.strokeStyle = ctx.fillStyle = C.black;
    const dot = (dx, dy) => { ctx.beginPath(); ctx.arc(x + dx * s, y + dy * s, 1.2 * s, 0, 7); ctx.fill(); };
    ctx.beginPath();
    if (kind === 2) {
      const v = 0.85 * a;
      ctx.moveTo(x - a, y); ctx.lineTo(x + a, y);
      ctx.moveTo(x, y - v); ctx.lineTo(x, y + v);
      ctx.stroke();
      if (group) for (const [dx, dy] of [[-4.6, -4.4], [4.6, -4.4], [-4.6, 4.4], [4.6, 4.4]]) dot(dx, dy);
      return;
    }
    // The symbol stands on (or hangs from) its point, centred on it.
    const above = kind === 3;
    const y0 = above ? y + h / 2 : y - h / 2;
    ctx.moveTo(x - a, y0); ctx.lineTo(x + a, y0);
    ctx.moveTo(x, y0); ctx.lineTo(x, above ? y0 - h : y0 + h);
    ctx.stroke();
    if (group) for (const dx of [-5.6, 5.6]) dot(dx, above ? -h / s / 2 + 1.2 : h / s / 2 - 1.2);
  }

  // Wrecks as the key draws them: one not dangerous to surface navigation the wreck sign (a
  // line with three ticks); a dangerous one, depth unknown, the sign in a dotted ring filled
  // pale blue; one showing a sunken hull's silhouette on its position.
  function drawWreck(ctx, x, y, kind, s, color) {
    const t = 1.3 * s;
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = ctx.fillStyle = color;
    ctx.lineCap = "butt";
    if (kind === 3) {
      ctx.beginPath(); ctx.moveTo(-7 * t, 0); ctx.lineTo(7 * t, 0); ctx.lineWidth = 1.2 * s; ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(-6 * t, -0.2 * t); ctx.lineTo(-6.6 * t, -3.6 * t); ctx.lineTo(-3 * t, -1.9 * t); ctx.lineTo(1 * t, -1.2 * t);
      ctx.lineTo(1 * t, -6 * t); ctx.lineTo(2.1 * t, -5.8 * t); ctx.lineTo(1.9 * t, -1 * t); ctx.lineTo(6.6 * t, -0.2 * t);
      ctx.closePath(); ctx.fill();
      ctx.beginPath(); ctx.arc(0, 0, 0.9 * t, 0, 7); ctx.fillStyle = "#ffffff"; ctx.fill(); ctx.lineWidth = 0.8 * s; ctx.stroke();
    } else {
      if (kind === 1) {
        ctx.beginPath(); ctx.ellipse(0, 0, 9 * t, 5 * t, 0, 0, 7);
        ctx.fillStyle = C.wreckFill; ctx.fill();
        ctx.lineWidth = 1.2 * s; ctx.lineCap = "round";
        ctx.setLineDash([0.01, 2.6 * s]); ctx.stroke(); ctx.setLineDash([]);
        ctx.lineCap = "butt";
      }
      ctx.lineWidth = 1.15 * s;
      ctx.beginPath();
      ctx.moveTo(-7 * t, 0); ctx.lineTo(7 * t, 0);
      for (const dx of [-3.6, 0, 3.6]) { ctx.moveTo(dx * t, -2.4 * t); ctx.lineTo(dx * t, 2.4 * t); }
      ctx.stroke();
    }
    ctx.restore();
  }

  // Positions only known to within 100 m or worse.
  const vague = (accuracy) => /^\s*(suuntaa|>|ei tiedossa)/i.test(String(accuracy || ""));

  // Museovirasto's wrecks and underwater sites, in magenta: protected wrecks ringed, other
  // wrecks paler, other sites a diamond. A dashed ring marks a vague position.
  function drawHeritage(ctx, site, s, lw, unit) {
    const [x, y, , , cls, kind, , accuracy, dup] = site;
    const color = cls === 0 ? C.magenta : C.magentaLight;
    ctx.save();
    ctx.lineCap = "butt";
    if (vague(accuracy) && !dup) {
      ctx.beginPath(); ctx.arc(x, y, 16 * s, 0, 7);
      ctx.strokeStyle = C.magentaLight; ctx.lineWidth = lw;
      ctx.setLineDash([3 * unit, 3 * unit]); ctx.stroke(); ctx.setLineDash([]);
    }
    if (kind === 5) {
      // A coastal defence site ashore: a small bastioned fort.
      ctx.beginPath();
      for (let i = 0; i < 8; i++) {
        const r = (i % 2 ? 3.6 : 6.5) * s, a = Math.PI / 4 + (i * Math.PI) / 4;
        ctx.lineTo(x + r * Math.cos(a), y + r * Math.sin(a));
      }
      ctx.closePath();
      ctx.strokeStyle = C.fort; ctx.lineWidth = 1.3 * lw; ctx.lineJoin = "miter"; ctx.stroke();
      ctx.beginPath(); ctx.arc(x, y, 1.2 * s, 0, 7); ctx.fillStyle = C.fort; ctx.fill();
    } else if (!dup) {
      if (kind === 4) {
        const d = 4 * s;
        ctx.beginPath(); ctx.moveTo(x, y - d); ctx.lineTo(x + d, y); ctx.lineTo(x, y + d); ctx.lineTo(x - d, y); ctx.closePath();
        ctx.fillStyle = color; ctx.fill();
      } else {
        drawWreck(ctx, x, y, 2, s, color);
      }
    }
    // A chart wreck at the same spot keeps its own symbol; a protected one gets the ring.
    if (cls === 0 && kind <= 3) {
      ctx.beginPath(); ctx.arc(x, y, 10 * s, 0, 7);
      ctx.strokeStyle = C.magenta; ctx.lineWidth = lw; ctx.stroke();
    }
    ctx.restore();
  }

  function anchor(ctx, x, y, s, lw, color = C.black) {
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5 * lw;
    ctx.lineCap = "butt";
    ctx.beginPath();
    ctx.moveTo(0, -8 * s); ctx.lineTo(0, 11 * s); // shank
    ctx.moveTo(-4 * s, -4.5 * s); ctx.lineTo(4 * s, -4.5 * s); // stock
    ctx.moveTo(-6 * s, 5 * s); ctx.quadraticCurveTo(-5 * s, 11 * s, 0, 11 * s); ctx.quadraticCurveTo(5 * s, 11 * s, 6 * s, 5 * s);
    ctx.stroke();
    ctx.beginPath(); ctx.arc(0, -9.5 * s, 1.5 * s, 0, 7); ctx.stroke(); // ring
    ctx.restore();
  }

  // A restricted area's limit (INT N2.1): magenta dashes, each ending in a tick into the area.
  function restrictedLimit(ctx, rings, unit) {
    const dash = 14 * unit, period = 24 * unit, tick = 3 * unit;
    const sign = ringArea(rings[0]) < 0 ? -1 : 1; // which side is inside (holes run the other way)
    strokeRings(ctx, rings, C.magenta, 1.2 * unit, [dash, period - dash]);
    ctx.beginPath();
    for (const ring of rings) {
      let d = 0, next = dash;
      for (let i = 0, n = ring.length; i < n; i += 2) {
        const x0 = ring[i], y0 = ring[i + 1], x1 = ring[(i + 2) % n], y1 = ring[(i + 3) % n];
        const len = Math.hypot(x1 - x0, y1 - y0);
        if (!len) continue;
        const ux = (x1 - x0) / len, uy = (y1 - y0) / len;
        for (; next <= d + len; next += period) {
          const px = x0 + ux * (next - d), py = y0 + uy * (next - d);
          ctx.moveTo(px, py);
          ctx.lineTo(px - uy * sign * tick, py + ux * sign * tick);
        }
        d += len;
      }
    }
    ctx.stroke();
  }

  // A submarine cable (INT L30.1): a magenta wavy line, its waves `wave` long and `amp` high.
  function wavyLine(ctx, flat, wave, amp) {
    ctx.beginPath();
    let phase = 0;
    ctx.moveTo(flat[0], flat[1]);
    for (let i = 2; i < flat.length; i += 2) {
      const x0 = flat[i - 2], y0 = flat[i - 1], x1 = flat[i], y1 = flat[i + 1];
      const len = Math.hypot(x1 - x0, y1 - y0);
      if (!len) continue;
      const ux = (x1 - x0) / len, uy = (y1 - y0) / len, step = wave / 8;
      for (let d = step; d <= len; d += step) {
        const off = amp * Math.sin(((phase + d) / wave) * 2 * Math.PI);
        ctx.lineTo(x0 + ux * d - uy * off, y0 + uy * d + ux * off);
      }
      phase += len;
    }
  }

  // Points every `period` along a line, starting `first` in: fn(x, y).
  function alongEvery(flat, first, period, fn) {
    let d = 0, next = first;
    for (let i = 2; i < flat.length; i += 2) {
      const x0 = flat[i - 2], y0 = flat[i - 1], x1 = flat[i], y1 = flat[i + 1];
      const len = Math.hypot(x1 - x0, y1 - y0);
      if (!len) continue;
      for (; next <= d + len; next += period) fn(x0 + ((x1 - x0) * (next - d)) / len, y0 + ((y1 - y0) * (next - d)) / len);
      d += len;
    }
  }

  function drawCables(ctx, rows, s, unit) {
    for (const [kind, line] of rows) {
      if (!Array.isArray(line)) continue;
      if (kind === "pipe") {
        // The key's pipeline: magenta dashes, each ending in a dot.
        strokeLine(ctx, line, C.magenta, 1.6 * unit, [7 * unit, 5 * unit]);
        ctx.beginPath();
        alongEvery(line, 7 * unit, 12 * unit, (x, y) => { ctx.moveTo(x + 2.3 * unit, y); ctx.arc(x, y, 2.3 * unit, 0, 7); });
        ctx.fillStyle = C.magenta;
        ctx.fill();
        continue;
      }
      wavyLine(ctx, line, Math.max(7 * unit, 13 * s), Math.max(1.4 * unit, 2.6 * s));
      ctx.strokeStyle = C.magenta;
      ctx.lineWidth = 1.15 * unit;
      ctx.lineJoin = "round";
      ctx.stroke();
    }
  }

  // --- traffic separation and other routeing areas ------------------------------------

  // An outlined arrow along a compass bearing, centred on (x, y), length len.
  function tssArrow(ctx, x, y, bearing, len, unit, both) {
    const a = ((bearing - 90) * Math.PI) / 180;
    const hw = 0.11 * len, head = 0.3 * len, hh = 0.24 * len;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(a);
    ctx.beginPath();
    const h = len / 2;
    ctx.moveTo(both ? -h + head : -h, -hw);
    ctx.lineTo(h - head, -hw); ctx.lineTo(h - head, -hh); ctx.lineTo(h, 0); ctx.lineTo(h - head, hh); ctx.lineTo(h - head, hw);
    if (both) {
      ctx.lineTo(-h + head, hw); ctx.lineTo(-h + head, hh); ctx.lineTo(-h, 0); ctx.lineTo(-h + head, -hh);
    } else ctx.lineTo(-h, hw);
    ctx.closePath();
    ctx.fillStyle = "rgba(255, 255, 255, 0.55)";
    ctx.fill();
    ctx.strokeStyle = C.magenta;
    ctx.lineWidth = 1.3 * unit;
    ctx.lineJoin = "miter";
    ctx.stroke();
    ctx.restore();
  }

  // Arrows at points of the tile's own grid where the whole arrow falls inside the area
  // and inside the tile (so none is cut at a tile edge), spaced well apart.
  function areaArrows(ctx, rings, bearing, e, s, unit, both, boxes) {
    const len = Math.min(e / 2 - 16, Math.max(48 * unit, 80 * s));
    const a = (bearing * Math.PI) / 180, ux = Math.sin(a), uy = -Math.cos(a);
    const half = 0.55 * len, wide = 0.3 * len;
    const kept = [];
    for (let i = 0; i < 16; i++) {
      for (let j = 0; j < 16; j++) {
        const gx = ((i + 0.5) * e) / 16, gy = ((j + 0.5) * e) / 16;
        const pts = [[gx, gy], [gx - ux * half, gy - uy * half], [gx + ux * half, gy + uy * half],
          [gx - uy * wide, gy + ux * wide], [gx + uy * wide, gy - ux * wide]];
        if (!pts.every(([x, y]) => x > 2 && y > 2 && x < e - 2 && y < e - 2 && insideRings(rings, x, y))) continue;
        if (kept.some(([x, y]) => Math.hypot(x - gx, y - gy) < 1.9 * len)) continue;
        kept.push([gx, gy]);
        tssArrow(ctx, gx, gy, bearing, len, unit, both);
        const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
        boxes?.push([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
      }
    }
  }

  // An unsurveyed area as the key draws it: thin light blue lines across, on the water's white.
  function hatch(ctx, rings, e, unit) {
    ctx.save();
    ctx.beginPath();
    for (const ring of rings) path(ctx, ring, true);
    ctx.clip("evenodd");
    ctx.beginPath();
    const step = 3 * unit;
    for (let y = step / 2 - e * 0.1; y < e * 1.1; y += step) { ctx.moveTo(-e * 0.1, y); ctx.lineTo(e * 1.1, y); }
    ctx.strokeStyle = C.unsurveyed;
    ctx.lineWidth = 0.6 * unit;
    ctx.stroke();
    ctx.restore();
  }

  // Routeing measures: [kind, orient, ...rings]. The arrows' boxes go into `boxes`.
  function drawTss(ctx, rows, e, s, unit, boxes) {
    for (const [kind, orient, ...rings] of rows) {
      if (!rings.length) continue;
      if (kind === "lane") {
        fillRings(ctx, rings, C.tssLane);
        if (orient != null) areaArrows(ctx, rings, orient, e, s, unit, false, boxes);
      } else if (kind === "zone") {
        fillRings(ctx, rings, C.tssZone);
      } else if (kind === "prec" || kind === "deep" || kind === "twoway") {
        // Their "!" and "DW" are lettering, placed with the other labels.
        strokeRings(ctx, rings, C.magenta, 1.3 * unit, [10 * unit, 6 * unit]);
        if (kind === "twoway" && orient != null) areaArrows(ctx, rings, orient, e, s, unit, true, boxes);
      } else if (kind === "dredged") {
        // The water under it is in the band of its least depth (painted with the depths).
        strokeRings(ctx, rings, "#7b8184", 0.8 * unit, [5 * unit, 3 * unit]);
      } else if (kind === "unsurveyed") {
        hatch(ctx, rings, e, unit);
        strokeRings(ctx, rings, C.black, 0.8 * unit, [6 * unit, 3 * unit]);
      } else {
        strokeRings(ctx, rings, C.magenta, unit);
      }
    }
  }

  function drawTssLines(ctx, rows, unit) {
    for (const [kind, line] of rows) {
      if (!Array.isArray(line)) continue;
      if (kind === "sep") strokeLine(ctx, line, C.tssSep, 5 * unit);
      else if (kind === "bnd") strokeLine(ctx, line, C.magenta, 1.3 * unit, [10 * unit, 6 * unit]);
      else if (kind === "vts") strokeLine(ctx, line, C.magenta, 1.6 * unit, [16 * unit, 4 * unit, 2 * unit, 4 * unit]);
      else if (kind === "call") strokeLine(ctx, line, C.magenta, 1.6 * unit, [8 * unit, 5 * unit]);
      else strokeLine(ctx, line, C.magenta, unit, [6 * unit, 4 * unit]);
    }
  }

  // --- land: topography, roads, buildings, landmarks ------------------------------------

  function drawRoads(ctx, roads, rails, zoom, unit) {
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    // Major roads as the chart's double line: black, with land colour inside.
    const major = roads.filter((r) => r[0] === 1 && Array.isArray(r[1]));
    if (zoom >= 12) {
      for (const [, line] of major) strokeLine(ctx, line, C.black, 3.4 * unit);
      for (const [, line] of major) strokeLine(ctx, line, C.land, 1.6 * unit);
    } else {
      for (const [, line] of major) strokeLine(ctx, line, C.black, 1.1 * unit);
    }
    ctx.lineCap = "butt";
    for (const [cls, line] of roads) {
      if (!Array.isArray(line)) continue;
      if (cls === 2) strokeLine(ctx, line, C.black, 0.9 * unit);
      else if (cls !== 1) strokeLine(ctx, line, C.black, 0.8 * unit, [4 * unit, 2.5 * unit]);
    }
    // Railways: a line with cross ticks.
    for (const [line] of rails) {
      if (!Array.isArray(line)) continue;
      strokeLine(ctx, line, C.black, 1.4 * unit);
      if (zoom < 12) continue;
      ctx.beginPath();
      let next = 6 * unit, d = 0;
      for (let i = 2; i < line.length; i += 2) {
        const x0 = line[i - 2], y0 = line[i - 1], x1 = line[i], y1 = line[i + 1];
        const len = Math.hypot(x1 - x0, y1 - y0);
        if (!len) continue;
        const ux = (x1 - x0) / len, uy = (y1 - y0) / len;
        for (; next <= d + len; next += 12 * unit) {
          const px = x0 + ux * (next - d), py = y0 + uy * (next - d);
          ctx.moveTo(px - uy * 3 * unit, py + ux * 3 * unit);
          ctx.lineTo(px + uy * 3 * unit, py - ux * 3 * unit);
        }
        d += len;
      }
      ctx.lineWidth = 1.1 * unit;
      ctx.stroke();
    }
  }

  // A landmark (x, y at its foot): mast (radio waves for a radio mast), chimney, wind
  // turbine, tower or church, in the chart's black line symbols. Returns its box.
  function drawLandmark(ctx, x, y, kind, s, lw) {
    const k = String(kind || "").toLowerCase();
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = ctx.fillStyle = C.black;
    ctx.lineWidth = 1.2 * lw;
    ctx.lineJoin = "miter";
    ctx.lineCap = "butt";
    const foot = () => { ctx.beginPath(); ctx.arc(0, 0, FOOT * s, 0, 7); ctx.stroke(); };
    let box = [-6, -28, 6, 3];
    if (k.includes("wind")) {
      foot();
      ctx.beginPath(); ctx.moveTo(0, -2.6 * s); ctx.lineTo(0, -20 * s); ctx.stroke();
      ctx.beginPath();
      for (const a of [-90, 30, 150]) {
        const r = (a * Math.PI) / 180;
        ctx.moveTo(0, -20 * s); ctx.lineTo(Math.cos(r) * 9 * s, -20 * s + Math.sin(r) * 9 * s);
      }
      ctx.lineWidth = 1.6 * lw; ctx.stroke();
      box = [-9, -30, 9, 3];
    } else if (k.includes("chimn") || k.includes("savu")) {
      foot();
      ctx.beginPath(); ctx.moveTo(-2.5 * s, -2.6 * s); ctx.lineTo(-1.5 * s, -18 * s); ctx.lineTo(1.5 * s, -18 * s); ctx.lineTo(2.5 * s, -2.6 * s);
      ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, -20 * s); ctx.bezierCurveTo(3 * s, -23 * s, 6 * s, -19 * s, 9 * s, -23 * s); ctx.stroke();
      box = [-5, -25, 10, 3];
    } else if (k.includes("church") || k.includes("kirk")) {
      ctx.beginPath(); ctx.arc(0, 0, 4 * s, 0, 7); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, -4 * s); ctx.lineTo(0, -16 * s); ctx.moveTo(-4 * s, -11.5 * s); ctx.lineTo(4 * s, -11.5 * s);
      ctx.lineWidth = 1.6 * lw; ctx.stroke();
      box = [-5, -17, 5, 5];
    } else if (k.includes("tower") || k.includes("torni")) {
      // The key's tower: a slender tapering outline over its circle.
      foot();
      ctx.beginPath();
      ctx.moveTo(-3.2 * s, -2 * s); ctx.lineTo(-2 * s, -15 * s); ctx.lineTo(2 * s, -15 * s); ctx.lineTo(3.2 * s, -2 * s);
      ctx.moveTo(-3.4 * s, -15 * s); ctx.lineTo(3.4 * s, -15 * s);
      ctx.stroke();
      box = [-5, -17, 5, 3];
    } else if (k.includes("mast") || k.includes("masto") || k.includes("radio")) {
      // A tall narrow lattice mast; a radio mast sends out waves from its top.
      foot();
      ctx.beginPath(); ctx.moveTo(-3 * s, -2 * s); ctx.lineTo(0, -26 * s); ctx.lineTo(3 * s, -2 * s);
      ctx.moveTo(-2.2 * s, -9 * s); ctx.lineTo(2.2 * s, -9 * s); ctx.moveTo(-1.2 * s, -17 * s); ctx.lineTo(1.2 * s, -17 * s);
      ctx.stroke();
      if (k.includes("radio")) {
        ctx.beginPath();
        for (const r of [4.5, 7.5]) {
          ctx.moveTo(-r * s * Math.cos(0.6), -24 * s - r * s * Math.sin(0.6));
          ctx.arc(0, -24 * s, r * s, Math.PI + 0.6, Math.PI - 0.6, true);
          ctx.moveTo(r * s * Math.cos(0.6), -24 * s - r * s * Math.sin(0.6));
          ctx.arc(0, -24 * s, r * s, -0.6, 0.6);
        }
        ctx.lineWidth = lw; ctx.stroke();
        box = [-9, -32, 9, 3];
      }
    } else {
      foot();
      ctx.beginPath(); ctx.arc(0, 0, 1 * s, 0, 7); ctx.fill();
      box = [-4, -4, 4, 4];
    }
    ctx.restore();
    return [x + box[0] * s, y + box[1] * s, x + box[2] * s, y + box[3] * s];
  }

  // A pilot boarding place as the key draws it: a magenta circle round a diamond.
  function drawPilot(ctx, x, y, s, lw) {
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = ctx.fillStyle = C.magenta;
    ctx.lineWidth = 1.5 * lw;
    ctx.beginPath(); ctx.arc(0, 0, 8 * s, 0, 7); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(0, -6.4 * s); ctx.quadraticCurveTo(2.2 * s, -2 * s, 3 * s, 0); ctx.quadraticCurveTo(2.2 * s, 2 * s, 0, 6.4 * s);
    ctx.quadraticCurveTo(-2.2 * s, 2 * s, -3 * s, 0); ctx.quadraticCurveTo(-2.2 * s, -2 * s, 0, -6.4 * s);
    ctx.fill();
    ctx.restore();
    return [x - 9 * s, y - 9 * s, x + 9 * s, y + 9 * s];
  }

  // A radio calling-in point: a magenta circle with an arrow the way ships report.
  function drawCall(ctx, x, y, orient, s, lw) {
    const r = 8 * s;
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = ctx.fillStyle = C.magenta;
    ctx.lineWidth = 1.5 * lw;
    ctx.beginPath(); ctx.arc(0, 0, r, 0, 7); ctx.stroke();
    if (orient != null && Number.isFinite(+orient)) {
      ctx.rotate((+orient * Math.PI) / 180);
      ctx.beginPath(); ctx.moveTo(0, 0.6 * r); ctx.lineTo(0, -2 * r); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, -2.5 * r); ctx.lineTo(-0.45 * r, -1.7 * r); ctx.lineTo(0.45 * r, -1.7 * r); ctx.closePath(); ctx.fill();
    } else {
      ctx.beginPath(); ctx.arc(0, 0, 1.6 * s, 0, 7); ctx.fill();
    }
    ctx.restore();
    const reach = orient != null ? 2.5 * r : r;
    return [x - reach, y - reach, x + reach, y + reach];
  }

  // Signs in the water: a speed limit as a red-ringed disc with its number, no wake as a
  // crossed-out wave, no anchoring as a crossed-out magenta anchor, others a notice board.
  function drawSign(ctx, x, y, type, value, s, lw) {
    const r = 9 * s;
    ctx.save();
    ctx.translate(x, y);
    const disc = () => {
      ctx.beginPath(); ctx.arc(0, 0, r, 0, 7);
      ctx.fillStyle = "#ffffff"; ctx.fill();
      ctx.lineWidth = 2.2 * s; ctx.strokeStyle = C.red; ctx.stroke();
    };
    if (type === "speed") {
      disc();
      ctx.fillStyle = C.black;
      ctx.font = `bold ${(String(value ?? "").length > 2 ? 7.5 : 9.5) * s}px ${SANS}`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(value != null ? String(Math.round(value)) : "!", 0, 0.5 * s);
    } else if (type === "nowake") {
      disc();
      ctx.beginPath();
      ctx.moveTo(-5.5 * s, 1 * s);
      ctx.bezierCurveTo(-3.5 * s, -2 * s, -1.5 * s, -2 * s, 0, 1 * s);
      ctx.bezierCurveTo(1.5 * s, 4 * s, 3.5 * s, 4 * s, 5.5 * s, 1 * s);
      ctx.lineWidth = 1.4 * s; ctx.strokeStyle = C.black; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(-0.7 * r, -0.7 * r); ctx.lineTo(0.7 * r, 0.7 * r);
      ctx.lineWidth = 2 * s; ctx.strokeStyle = C.red; ctx.stroke();
    } else if (type === "noanchor") {
      anchor(ctx, 0, 0, 0.75 * s, lw, C.magenta);
      ctx.beginPath(); ctx.moveTo(-7 * s, -9 * s); ctx.lineTo(7 * s, 9 * s);
      ctx.lineWidth = 1.6 * lw; ctx.strokeStyle = C.magenta; ctx.stroke();
    } else if (type === "nomoor" || type === "nopass") {
      disc();
      ctx.strokeStyle = ctx.fillStyle = C.black;
      ctx.lineWidth = 1.3 * s;
      ctx.beginPath();
      if (type === "nomoor") {
        // a bollard with a rope
        ctx.fillRect(-1.6 * s, -1 * s, 3.2 * s, 5 * s);
        ctx.fillRect(-3.4 * s, -2.6 * s, 6.8 * s, 1.8 * s);
        ctx.moveTo(-5.5 * s, 3 * s); ctx.quadraticCurveTo(-2 * s, -4 * s, 1.6 * s, 0.4 * s);
      } else {
        // two ships side by side, one up, one down
        for (const [dx, dir] of [[-2.6, -1], [2.6, 1]]) {
          ctx.moveTo(dx * s, dir * 5 * s); ctx.lineTo((dx - 1.4) * s, dir * 1.5 * s);
          ctx.lineTo((dx - 1.4) * s, -dir * 4.5 * s); ctx.lineTo((dx + 1.4) * s, -dir * 4.5 * s);
          ctx.lineTo((dx + 1.4) * s, dir * 1.5 * s); ctx.closePath();
        }
      }
      ctx.stroke();
      ctx.beginPath(); ctx.moveTo(-0.7 * r, -0.7 * r); ctx.lineTo(0.7 * r, 0.7 * r);
      ctx.lineWidth = 2 * s; ctx.strokeStyle = C.red; ctx.stroke();
    } else if (type === "height") {
      // headroom: a white board with the clearance, arrows above and below
      const text = value != null ? String(Math.round(value * 10) / 10).replace(".", ",") : "?";
      ctx.font = `bold ${8.5 * s}px ${SANS}`;
      const w = Math.max(14 * s, ctx.measureText(text).width + 5 * s);
      ctx.fillStyle = "#ffffff"; ctx.fillRect(-w / 2, -6 * s, w, 12 * s);
      ctx.lineWidth = 1.2 * lw; ctx.strokeStyle = C.black; ctx.strokeRect(-w / 2, -6 * s, w, 12 * s);
      ctx.fillStyle = C.black;
      for (const dir of [-1, 1]) {
        ctx.beginPath(); ctx.moveTo(0, dir * 6 * s); ctx.lineTo(-2.4 * s, dir * 9.5 * s); ctx.lineTo(2.4 * s, dir * 9.5 * s); ctx.closePath(); ctx.fill();
      }
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(text, 0, 0.5 * s);
      ctx.restore();
      return [x - w / 2, y - 10 * s, x + w / 2, y + 10 * s];
    } else {
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(-7 * s, -7 * s, 14 * s, 14 * s);
      ctx.lineWidth = 1.4 * lw; ctx.strokeStyle = C.magenta; ctx.strokeRect(-7 * s, -7 * s, 14 * s, 14 * s);
      ctx.fillStyle = C.magenta; ctx.font = `bold ${11 * s}px ${SANS}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("!", 0, 0.5 * s);
    }
    ctx.restore();
    return [x - r, y - r, x + r, y + r];
  }

  // --- labels -------------------------------------------------------------------------

  // Lettering styles, in chart px. spacing is in em.
  // Names as the paper chart letters them: towns in a large upright serif, land and islands
  // in an upright sans, waters in sloping sans; all in ordinary capitals and small letters.
  const STYLES = {
    town: { size: 40, family: SERIF, min: 15 },
    sea: { size: 27, italic: true, min: 12 },
    water: { size: 22, italic: true },
    island: { size: 21 },
    litIsland: { size: 18, weight: 700, spacing: 0.07, upper: true },
    land: { size: 25 },
    settlement: { size: 18 },
    light: { size: 16 },
    lightName: { size: 17, weight: 700, spacing: 0.07, upper: true },
    racon: { size: 16, color: C.magenta },
    call: { size: 14, color: C.magenta },
    mark: { size: 18 },
    heritage: { size: 14, italic: true, color: C.magenta },
    fort: { size: 14, italic: true, color: C.fort },
    speed: { size: 13, color: C.magenta },
    tss: { size: 21, color: C.magenta, min: 12 },
    precSign: { size: 22, weight: 700, color: C.magenta, min: 13 },
  };
  // Placement order: towns, sea areas, lights, islands, heritage, other land.
  const PRIO = { town: 0, sea: 1, light: 2, island: 3, heritage: 4, land: 5 };

  // A label's lines and the spots it may take round its anchor, in units of k per CSS px
  // (the tile's units, or 1 on the overlay). Spots are top-left corners relative to the
  // anchor, best first.
  function layoutLabel(ctx, cand, k, sc) {
    const lines = [];
    let w = 0, h = 0;
    for (const [text, name] of cand.lines) {
      const st = STYLES[name] || STYLES.island;
      const size = Math.max(st.min ?? MIN_TEXT, st.size * sc) * k;
      const caps = st.upper || (name === "sea" && text === text.toUpperCase());
      const t = st.upper ? text.toUpperCase() : text;
      const spacing = (st.spacing ?? (caps ? 0.2 : 0)) * size;
      const font = `${st.italic ? "italic " : ""}${st.weight ? `${st.weight} ` : ""}${size}px ${st.family || SANS}`;
      ctx.font = font;
      const lw = textWidth(ctx, t, spacing);
      lines.push({ text: t, font, size, spacing, color: st.color || C.black, w: lw });
      w = Math.max(w, lw);
      h += size;
    }
    if (cand.frame) w = h = 1.9 * h; // a sign: the text in a triangle round it
    const spots = [];
    if (cand.sym) {
      const f = sc * k;
      const [l, t, r, b] = cand.sym.map((v) => v * f);
      const g = 2.5 * f + k;
      const cy = t + 0.42 * (b - t);
      spots.push([r + g, cy - h / 2, "left"], [l - g - w, cy - h / 2, "right"],
        [r + g, t - 0.7 * h, "left"], [r + g, b - 0.3 * h, "left"],
        [l - g - w, t - 0.7 * h, "right"], [l - g - w, b - 0.3 * h, "right"],
        [-w / 2, t - g - h, "center"], [-w / 2, b + g, "center"]);
    } else {
      spots.push([-w / 2, -h / 2, "center"]);
      if (cand.shift) spots.push([-w / 2, -1.3 * h, "center"], [-w / 2, 0.3 * h, "center"]);
    }
    const top = 0.1 * lines[0].size, bottom = 0.1 * lines[lines.length - 1].size;
    return {
      lines, w, h, frame: cand.frame,
      spots: spots.map(([dx, dy, align]) => ({ dx, dy, align, box: [dx - k, dy + top, dx + w + k, dy + h - bottom] })),
    };
  }

  function drawLabel(ctx, lay, x0, y0, align) {
    ctx.textBaseline = "alphabetic";
    if (lay.frame === "triangle") {
      const line = lay.lines[0], cx = x0 + lay.w / 2;
      ctx.beginPath();
      ctx.moveTo(cx, y0); ctx.lineTo(x0 + lay.w, y0 + 0.9 * lay.h); ctx.lineTo(x0, y0 + 0.9 * lay.h); ctx.closePath();
      ctx.strokeStyle = line.color; ctx.lineWidth = 1.4; ctx.lineJoin = "miter"; ctx.stroke();
      ctx.font = line.font; ctx.fillStyle = line.color;
      spacedText(ctx, line.text, cx - line.w / 2, y0 + 0.78 * lay.h, 0);
      return;
    }
    let y = y0;
    for (const line of lay.lines) {
      ctx.font = line.font;
      ctx.fillStyle = line.color;
      const x = align === "right" ? x0 + lay.w - line.w : align === "center" ? x0 + (lay.w - line.w) / 2 : x0;
      spacedText(ctx, line.text, x, y + 0.82 * line.size, line.spacing, Math.max(2, 0.17 * line.size));
      y += line.size;
    }
  }

  // A long name on two lines, as the chart sets it: a compound broken before its last
  // part with a hyphen ("Källviklandet" -> "Källvik-", "landet"), or at the space nearest
  // its middle.
  const NAME_ENDS = /^(.{4,}?)(landet|länningen|niemi|fjärden|selkä|selät|holmen|holmarna|udden|saari|saaret|lahti|viken|grundet|skäret|näset|sundet|salmi|luoto|luodot|kallio|vuori|harun|bådan|klobben|fladan|ören|gården)$/i;
  function splitName(text) {
    if (text.length < 11) return [text];
    if (!text.includes(" ")) {
      const m = text.match(NAME_ENDS);
      return m && m[2].length >= 4 ? [`${m[1]}-`, m[2]] : [text];
    }
    if (text.length < 14) return [text];
    let best = -1;
    for (let i = text.indexOf(" "); i >= 0; i = text.indexOf(" ", i + 1)) {
      if (best < 0 || Math.abs(i - text.length / 2) < Math.abs(best - text.length / 2)) best = i;
    }
    return [text.slice(0, best), text.slice(best + 1)];
  }
  const nameLines = (texts, style) => texts.flatMap((t) => splitName(t)).map((t) => [t, style]);

  // "Rönnskär 6", "Gustavsvärn ylempi" -> "rönnskär", "gustavsvärn": the place an aid is named after.
  const placeKey = (name) => String(name || "").toLowerCase()
    .replace(/\s+(\d.*|ylempi|alempi|etu|taka|övre|nedre|n|s|e|w|[ivx]+)$/i, "").trim();

  // Every label of a tile, each { prio, rank, x, y, lines: [[text, style]], sym?, shift? },
  // with `inside` set for those whose point is the tile's own (the overlay takes those;
  // the rest only keep soundings clear near the tile's edges).
  function labelCandidates(data, zoom, aidAt) {
    const out = [];
    const e = data.e;
    const own = (x, y) => x >= 0 && y >= 0 && x < e && y < e;
    const add = (c) => { c.inside = own(c.x, c.y); out.push(c); };

    // Lights that will show their character, and which island names carry one.
    const lit = [];
    for (const [x, y, character] of data.light || []) {
      const aid = aidAt.get(`${x},${y}`);
      const type = aid ? aid[2] : "";
      if (zoom < 14 && !(type === "s" || type === "m" || type === "d")) continue;
      if (zoom <= OVERVIEW_Z && type !== "m" && type !== "s") continue;
      lit.push({ x, y, character, aid, type, key: aid ? placeKey(aid[5]) : "", merged: null });
    }
    const litAids = (data.aid || []).filter((a) => a[4] && a[5]);
    const sectored = new Set((data.sector || []).map((r) => `${r[0]},${r[1]}`));
    const names = [];
    for (const [x, y, text, kind] of data.name || []) {
      if (!text) continue;
      // The Finnish and Swedish names of a place are set one under the other, as on the chart.
      const twin = names.find((n) => n.kind === kind && n.texts.length === 1 && Math.hypot(n.x - x, n.y - y) < (kind === "s" ? 80 : 40));
      if (twin) {
        if (twin.texts[0].toUpperCase() !== text.toUpperCase()) twin.texts.push(text);
        continue;
      }
      names.push({ x, y, kind, texts: [text] });
    }

    for (const n of names) {
      const { x, y, kind, texts } = n;
      if (kind === "s" && texts.some((t) => TOWNS.has(t))) {
        add({ prio: PRIO.town, rank: 0, x, y, lines: texts.map((t) => [t, "town"]) });
      } else if (kind === "s") {
        add({ prio: PRIO.land, rank: 0, x, y, lines: nameLines(texts, "settlement"), shift: true });
      } else if (kind === "w") {
        const open = OPEN_WATER.test(texts[0]) || texts[0] === texts[0].toUpperCase();
        add({ prio: PRIO.sea, rank: open ? 0 : 1, x, y, lines: nameLines(texts, open ? "sea" : "water"), shift: true });
      } else {
        // An island or rock with a light on it is lettered in capitals, as the chart does,
        // together with the light's character when that shows.
        const key = placeKey(texts[0]);
        const isLit = zoom >= 12 && litAids.some((a) => Math.hypot(a[0] - x, a[1] - y) < 120 && placeKey(a[5]) === key);
        if (isLit) {
          let best = null;
          for (const l of lit) {
            if (l.key !== key || l.merged) continue;
            const d = Math.hypot(l.x - x, l.y - y);
            if (d < 120 && (!best || d < best.d)) best = { l, d };
          }
          if (best) { best.l.merged = texts[0]; continue; }
          add({ prio: PRIO.island, rank: 0, x, y, lines: [[texts[0], "litIsland"]], shift: true });
        } else {
          add({ prio: kind === "i" ? PRIO.island : PRIO.land, rank: 1, x, y, lines: nameLines(texts, kind === "i" ? "island" : "land"), shift: true });
        }
      }
    }

    // Light characters beside their lights; major lights not named after an island also
    // get their name above.
    const islandKeys = new Set(names.filter((n) => n.kind === "i" || n.kind === "l").map((n) => placeKey(n.texts[0])));
    // A racon on a lit mark is lettered under the light's character, as the key does.
    const racons = zoom >= 13 ? (data.aid || []).filter((a) => a[2] === "t") : [];
    const withLight = new Set();
    for (const l of lit) {
      const lines = [];
      if (l.merged) lines.push([l.merged, "litIsland"]);
      else if ((l.type === "s" || l.type === "m") && l.aid[5] && !islandKeys.has(l.key)) lines.push([l.aid[5], "lightName"]);
      lines.push([lightText(l.character, l.aid ? l.aid[6] || "" : ""), "light"]);
      const racon = racons.find((a) => Math.abs(a[0] - l.x) <= 1 && Math.abs(a[1] - l.y) <= 1);
      if (racon) { lines.push(["Racon", "racon"]); withLight.add(racon); }
      const rank = { m: 0, s: 1, d: 2 }[l.type] ?? 3;
      let sym = l.aid ? markExtent(l.aid) : EXTENT.o;
      // A lighthouse's colour ring up close: its name and character outside the ring, as in the key.
      if (zoom >= 14 && l.type === "m" && l.aid[7] == null && !sectored.has(`${l.x},${l.y}`)) sym = [-50, -50, 50, 50];
      add({ prio: PRIO.light, rank, x: l.x, y: l.y, lines, sym });
    }
    for (const aid of racons) {
      if (withLight.has(aid)) continue;
      add({ prio: PRIO.light, rank: 4, x: aid[0], y: aid[1], lines: [["Racon", "racon"]], sym: [-24, -24, 24, 24] });
    }
    // Heights of masts, chimneys and the like: "(131)".
    if (zoom >= 13) {
      for (const [x, y, kind, height] of data.landmark || []) {
        if (height == null || height === "") continue;
        const ext = LANDMARK_EXTENT(kind);
        add({ prio: PRIO.light, rank: 5, x, y, lines: [[`(${Math.round(height)})`, "mark"]], sym: ext });
      }
    }

    // Radio calling-in points: their VHF channel, and the name up close.
    for (const [x, y, name, channel] of data.call || []) {
      const lines = [];
      if (zoom >= 13 && name) lines.push([name, "call"]);
      if (channel) lines.push([/^\d/.test(channel) ? `Ch ${channel}` : channel, "call"]);
      if (lines.length) add({ prio: PRIO.light, rank: 4, x, y, lines, sym: [-9, -9, 9, 9] });
    }

    // Precautionary areas' "!" and deep-water routes' "DW", once in a while across the area.
    for (const [kind, , ...rings] of data.tss || []) {
      if ((kind !== "prec" && kind !== "deep") || !rings.length) continue;
      const p = labelPoint(rings, e, 40, 40);
      if (!p) continue;
      add({ prio: PRIO.sea, rank: 3, x: p[0], y: p[1], once: 320, ...(kind === "prec"
        ? { lines: [["!", "precSign"]], frame: "triangle" } : { lines: [["DW", "tss"]] }) });
    }

    // Speed limits in restricted areas, unless the tile has its own speed signs.
    if (zoom >= 14 && !(data.sign || []).some((r) => r[2] === "speed")) {
      for (const [types, speed, ...rings] of data.restrict || []) {
        if (speed == null || !/(^|\D)01(\D|$)/.test(String(types || ""))) continue;
        const p = labelPoint(rings, e, 60, 20);
        if (!p) continue;
        add({ prio: PRIO.heritage, rank: 0, x: p[0], y: p[1], lines: [[`${Math.round(speed)} km/h`, "speed"]], once: 250 });
      }
    }

    // Wreck and site names only up close: protected wrecks from zoom 14, the rest from 15.
    if (zoom >= 14 && options.heritage) {
      for (const [x, y, , name, cls, kind] of data.heritage || []) {
        if (!name || (zoom < 15 && !(cls === 0 && kind <= 3))) continue;
        add({ prio: PRIO.heritage, rank: cls === 0 ? 1 : 2, x, y, lines: [[name, kind === 5 ? "fort" : "heritage"]], sym: [-11, -9, 11, 9] });
      }
    }
    return out;
  }

  function LANDMARK_EXTENT(kind) {
    const k = String(kind || "").toLowerCase();
    if (k.includes("radio")) return [-9, -32, 9, 3];
    if (k.includes("wind")) return [-9, -30, 9, 3];
    if (k.includes("mast") || k.includes("masto")) return [-6, -28, 6, 3];
    return [-6, -20, 6, 4];
  }

  // Boxes claimed in a tile: by the labels' first spots (so soundings keep clear of them)
  // and by labels along lines and symbols (handed to the overlay as obstacles).
  function claims() {
    const boxes = [];
    const draws = [];
    const free = (b) => boxes.every((o) => !overlaps(b, o));
    return {
      free,
      hard: [], // for the overlay: [x0, y0, x1, y1] in tile units
      soft: [],
      claim(box, hard, draw) {
        boxes.push(box);
        if (hard) this.hard.push(box);
        if (draw) draws.push(draw);
      },
      flush() { for (const draw of draws) draw(); },
    };
  }

  // --- a tile -------------------------------------------------------------------------

  function drawTile(canvas, data, coords, dpr) {
    const zoom = coords.z;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const e = data.e;
    const k = canvas.width / e;
    ctx.setTransform(k, 0, 0, k, 0, 0);
    // Lines are in CSS pixels (`unit`; tile units are half a CSS pixel); symbols and text
    // in chart pixels (`s`); symbol outlines thin out a little when zoomed out.
    const unit = e / 256;
    const sc = scaleAt(zoom);
    const s = unit * sc;
    const lw = unit * Math.min(1.3, Math.max(0.7, sc));
    const overview = zoom <= OVERVIEW_Z;
    ctx.lineCap = "butt";
    ctx.lineJoin = "round";

    // A light and an unlit mark at one point (a lighthouse with its racon): the light.
    const aidAt = new Map();
    for (const aid of data.aid || []) {
      const key = `${aid[0]},${aid[1]}`;
      if (!aidAt.has(key) || (aid[4] && !aidAt.get(key)[4])) aidAt.set(key, aid);
    }
    // Labels claim their first spots first, so soundings and labels along lines keep clear.
    const label = claims();
    const cands = labelCandidates(data, zoom, aidAt);
    const spotBox = (cand, spot) => [cand.x + spot.box[0], cand.y + spot.box[1], cand.x + spot.box[2], cand.y + spot.box[3]];
    const lays = cands.map((cand) => layoutLabel(ctx, cand, unit, sc));
    cands.forEach((cand, i) => label.claim(spotBox(cand, lays[i].spots[0]), false));
    // Soundings, shallowest first as charts keep those, in the same order in every tile.
    // Those across the tile's edge are placed now, before the figures set into contour
    // and fairway lines (which differ from tile to tile): both tiles then keep (or both
    // leave out) each one, and none is drawn cut in half.
    const edgeSoundings = [], soundings = [];
    if (zoom >= 12) {
      const sorted = (data.sounding || []).slice().sort((a, b) => a[2] - b[2] || a[0] - b[0] || a[1] - b[1]);
      for (const row of sorted) {
        const b = soundingBox(row[0], row[1], row[2], s);
        if ((b[0] < 0 && b[2] > 0) || (b[0] < e && b[2] > e) || (b[1] < 0 && b[3] > 0) || (b[1] < e && b[3] > e)) {
          if (!label.free(b)) continue;
          label.claim(b, false);
          edgeSoundings.push([row[0], row[1], row[2], b]);
        } else soundings.push(row);
      }
    }
    const inside = (x, y) => x >= 0 && y >= 0 && x < e && y < e;
    // Symbols keep soundings off them; those in the tile also keep the screen's labels off.
    const marks = [], symbols = [];
    // Marks placed from a tile's own cut of an area (TSS arrows, rock-cluster signs): they
    // differ between neighbouring tiles, so soundings across the edge ignore them.
    const localMarks = new Set();
    const symbol = (box, x, y) => { marks.push(box); symbols.push(box); if (inside(x, y)) label.hard.push(box); };

    // 1. Land: the Finnish area, painted under the water.
    if (data.land) {
      ctx.beginPath();
      for (const ring of data.land) path(ctx, ring, true);
      ctx.fillStyle = C.land;
      ctx.fill(); // nonzero: the pieces of neighbouring blocks overlap a little
    }
    // Built-up areas, under the water so the shore stays crisp; industry greyer, public
    // buildings and churches a shade darker.
    for (const [kind, ...rings] of data.buildarea || []) {
      if (rings.length && Array.isArray(rings[0])) fillRings(ctx, rings, BUILD_AREA[kind] || C.buildArea);
    }

    // Lakes, height contours, roads and railways, buildings: all under the water, so
    // none of it shows over the depth areas.
    for (const rings of data.lake || []) {
      if (!Array.isArray(rings) || !rings.length) continue;
      fillRings(ctx, rings, C.lake);
      strokeRings(ctx, rings, C.contour, 0.8 * unit);
    }
    const heightsLabelled = new Set();
    for (const [metres, line] of data.height || []) {
      if (!Array.isArray(line)) continue;
      const major = metres % 20 === 0;
      let gap = null;
      if (zoom >= 13 && major && (coords.x + coords.y) % 2 === 1 && !heightsLabelled.has(metres)) {
        const size = Math.max(9 * unit, 11 * s);
        gap = alongLine(ctx, label, line, e, {
          text: String(metres), font: `${size}px ${SANS}`, size, color: C.heightText, need: 50 * unit, pad: 1.5 * unit,
        }) || null;
        if (gap) heightsLabelled.add(metres);
      }
      strokeLine(ctx, line, C.height, (major ? 0.9 : 0.6) * unit, null, gap);
    }
    if (data.road || data.rail) drawRoads(ctx, data.road || [], data.rail || [], zoom, unit);
    if (data.building?.length) {
      ctx.fillStyle = C.black;
      for (const [x, y, size] of data.building) {
        const side = (size === 2 ? 9 : 6.5) * s; // 1 small, 2 large
        ctx.fillRect(x - side / 2, y - side / 2, side, side);
      }
    }


    // 2. Water on its own canvas, then a crisp coastline: the water grown by a pixel,
    // painted black, with the water on top. Outside Finland the depths come nested (the
    // water deeper than 0, 3, 6 and 10 m, shallow first), each painted over the one before,
    // so no gap can open between two bands; the Finnish depth areas go over them.
    if (data.depth?.length || data.xdepth?.length) {
      const size = canvas.width;
      const water = scratch(0, size);
      const w = water.getContext("2d");
      w.setTransform(k, 0, 0, k, 0, 0);
      for (const [band, ...rings] of data.xdepth || []) fillRings(w, rings, C.depth[band] ?? C.depth[4]);
      for (const [band, ...rings] of data.depth || []) fillRings(w, rings, C.depth[band] ?? C.depth[4]);
      const edge = scratch(1, size);
      const g = edge.getContext("2d");
      const d = Math.max(1, Math.round(0.9 * dpr));
      for (const [dx, dy] of [[d, 0], [-d, 0], [0, d], [0, -d]]) g.drawImage(water, dx, dy);
      g.globalCompositeOperation = "source-in";
      g.fillStyle = C.coast;
      g.fillRect(0, 0, size, size);
      g.globalCompositeOperation = "source-over";
      g.drawImage(water, 0, 0);
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.drawImage(edge, 0, 0);
      ctx.restore();
    }

    // 4. Main merchant fairways (class 1) in grey over the depths, edged as the key's swept
    // fairway area in magenta dashes: every piece's edge stroked twice as wide, then all the
    // pieces filled over it, so only the outer half of the outline shows (and none where
    // two pieces meet). Anchorages outlined.
    const anchors = [];
    const main = [];
    for (const row of data.farea || []) {
      const [code, draught, vl, ...rings] = row;
      if (Array.isArray(vl)) rings.unshift(vl); // a tile from before fairway classes
      const cls = Array.isArray(vl) ? null : vl;
      if (code === 1 || code === 11) {
        if (cls === 1 || (cls == null && draught >= 7.5)) main.push(rings);
      } else if (code === 2 && !overview) {
        // The key's anchorage area: magenta dashes with small anchors along them, a big one inside.
        strokeRings(ctx, rings, C.magenta, 1.2 * unit, [8 * unit, 5 * unit]);
        if (zoom >= 13) {
          const m = 7 * s;
          for (const ring of rings) {
            alongEvery([...ring, ring[0], ring[1]], 50 * unit, 130 * unit, (x, y) => {
              if (x > m && y > m && x < e - m && y < e - m) anchor(ctx, x, y, 0.45 * s, 0.7 * lw, C.magenta);
            });
          }
        }
        const p = labelPoint(rings, e, 14 * s, 24 * s);
        if (p) anchors.push(p);
      }
    }
    if (!overview) for (const rings of main) strokeRings(ctx, rings, C.magenta, 2.4 * unit, [9 * unit, 5 * unit]);
    for (const rings of main) {
      ctx.beginPath();
      for (const ring of rings) path(ctx, ring, true);
      ctx.fillStyle = C.fairwayArea;
      ctx.fill("evenodd");
    }

    // 5. Traffic separation schemes and other routeing measures.
    if (data.tss) {
      const arrows = [];
      drawTss(ctx, data.tss, e, s, unit, arrows);
      for (const b of arrows) { marks.push(b); localMarks.add(b); } // soundings keep off the arrows
    }
    if (data.tssline) drawTssLines(ctx, data.tssline, unit);

    // 6. The small-craft trunk route's green band.
    for (const [line] of data.band || []) strokeLine(ctx, line, C.band, 12 * s);

    // 7. Contours: 3 m dotted, 6 m dashed, 10 m and deeper solid with a figure in a break.
    const labelled = new Set();
    for (const [depth, line] of data.contour || []) {
      if (depth <= 3) {
        ctx.lineCap = "round";
        strokeLine(ctx, line, C.contourShallow, 1.6 * unit, [0.01 * unit, 5 * unit]);
        ctx.lineCap = "butt";
      } else if (depth <= 6) {
        strokeLine(ctx, line, C.contourShallow, 1.1 * unit, [6 * unit, 3.5 * unit]);
      } else {
        let gap = null;
        // One figure per depth in every other tile (a checkerboard): about every 350 px.
        if (zoom >= 13 && (coords.x + coords.y) % 2 === 0 && !labelled.has(depth)) {
          const size = Math.max(MIN_TEXT * unit, 14 * s);
          gap = alongLine(ctx, label, line, e, {
            text: String(depth), font: `${size}px ${SANS}`, size, color: C.contour, need: 40 * unit, pad: 2 * unit,
          }) || null;
          if (gap) labelled.add(depth);
        }
        strokeLine(ctx, line, C.contour, unit, null, gap);
      }
    }

    // 8. Restricted areas: magenta T-dashed limits.
    if (!overview) for (const [, , ...rings] of data.restrict || []) if (rings.length) restrictedLimit(ctx, rings, unit);
    // Submarine cables and pipelines.
    if (data.cable) drawCables(ctx, data.cable, s, unit);

    // 9. Leading lines as the key draws them: dashed through the marks and on out to sea,
    // solid only where they are the fairway (the tile says where), the bearing in red beside
    // the solid part. A tile without that is all solid.
    for (const row of data.lead || []) {
      let [bearing, line, given] = row;
      if (!line) { line = bearing; bearing = null; } // a tile from before bearings
      const solid = Array.isArray(given) && given.every(Array.isArray) ? given.filter((p) => p.length >= 4) : [line];
      if (solid[0] !== line) strokeLine(ctx, line, C.black, 1.1 * unit, [11 * unit, 6 * unit]);
      for (const piece of solid) strokeLine(ctx, piece, C.black, 1.4 * unit);
      if (zoom >= 13 && bearing != null && solid.length) {
        const size = Math.max(MIN_TEXT * unit, 19 * s);
        const b = (bearing * Math.PI) / 180;
        const longest = solid.reduce((m, p) => (lineLength(p) > lineLength(m) ? p : m));
        // Beside the line, to the right of a ship heading along it, where it fits in the tile.
        for (const at of [0.5, 0.35, 0.65, 0.2, 0.8]) {
          if (alongLine(ctx, label, longest, e, {
            text: bearingText(bearing), font: `${size}px ${SANS}`, size, color: C.lightR, need: 0, at, alternate: coords,
            offset: [Math.cos(b) * 0.8 * size, Math.sin(b) * 0.8 * size],
          }) !== false) break;
        }
      }
    }

    // 10. Piers, quays and breakwaters.
    for (const [line] of data.shore || []) strokeLine(ctx, line, C.black, 2 * unit);

    // 11. Fairway lines, with their draught "⟨9,0⟩" set into the line once per tile;
    // boat routes dashed, their advisory draught in italics.
    const draughts = new Set();
    for (const row of data.fline || []) {
      let [draught, vl, line] = row;
      if (!line) { line = vl; vl = null; } // a tile from before fairway classes
      const boat = draught == null || vl >= 5;
      if (overview && boat) continue;
      let gap = null;
      if (zoom >= 12 && draught != null) {
        const size = Math.max(MIN_TEXT * unit, 22 * s);
        const o = draughtLabel(ctx, draught, boat, size);
        if (!draughts.has(o.text)) {
          gap = alongLine(ctx, label, line, e, { ...o, color: C.fairway, alternate: zoom >= 14 ? coords : null }) || null;
          if (gap) draughts.add(o.text);
        }
      }
      if (boat) strokeLine(ctx, line, C.fairway, (zoom >= 12 ? 1.6 : 1.2) * unit, [14 * unit, 8 * unit], gap);
      else strokeLine(ctx, line, C.fairway, (zoom >= 13 ? 2.2 : 1.6) * unit, null, gap);
    }

    // 12. Rocks, rock clusters, wrecks, heritage sites and anchorages.
    if (!overview) {
      // Zoomed out the shores are thick with rocks: smaller there, as on the overview charts.
      const rs = zoom <= 12 ? 0.8 * s : s;
      const rr = ROCK_REACH * rs;
      for (const [x, y, kind] of data.rock || []) {
        drawRock(ctx, x, y, kind, rs, false);
        marks.push([x - 0.7 * rr, y - 0.7 * rr, x + 0.7 * rr, y + 0.7 * rr]);
        if (inside(x, y)) label.soft.push([x - rr, y - rr, x + rr, y + rr]);
      }
      for (const row of data.rockarea || []) {
        const [kind, ...rings] = row;
        if (Array.isArray(kind)) rings.unshift(kind); // a tile from before rock-cluster kinds
        const rock = Array.isArray(kind) || !kind ? 1 : kind;
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        for (const ring of rings) {
          for (let i = 0; i < ring.length; i += 2) {
            x0 = Math.min(x0, ring[i]); x1 = Math.max(x1, ring[i]);
            y0 = Math.min(y0, ring[i + 1]); y1 = Math.max(y1, ring[i + 1]);
          }
        }
        if (Math.max(x1 - x0, y1 - y0) >= 24 * unit) {
          // A wide cluster: a dotted danger line round it.
          ctx.lineCap = "round";
          strokeRings(ctx, rings, C.black, 1.6 * unit, [0.01 * unit, 3.4 * unit]);
          ctx.lineCap = "butt";
        }
        if (data.rockpt) continue; // the signs come with their own points
        // A tile from before "rockpt": the sign in the middle of the tile's own cut.
        const c = ringCentroid(rings[0]) || [(x0 + x1) / 2, (y0 + y1) / 2];
        drawRock(ctx, c[0], c[1], rock, rs, true);
        const cb = [c[0] - 0.7 * rr, c[1] - 0.7 * rr, c[0] + 0.7 * rr, c[1] + 0.7 * rr];
        marks.push(cb);
        localMarks.add(cb);
      }
      for (const [x, y, kind] of data.rockpt || []) {
        drawRock(ctx, x, y, kind || 1, rs, true);
        marks.push([x - 0.7 * rr, y - 0.7 * rr, x + 0.7 * rr, y + 0.7 * rr]);
        if (inside(x, y)) label.soft.push([x - rr, y - rr, x + rr, y + rr]);
      }
      for (const [x, y, kind] of data.wreck || []) {
        drawWreck(ctx, x, y, kind, s, C.black);
        symbol([x - 10 * s, y - 8 * s, x + 10 * s, y + 6 * s], x, y);
      }
      if (options.heritage) {
        for (const site of data.heritage || []) {
          drawHeritage(ctx, site, s, lw, unit);
          const r = site[4] === 0 && site[5] <= 3 ? 10 * s : 7 * s;
          symbol([site[0] - r, site[1] - r, site[0] + r, site[1] + r], site[0], site[1]);
        }
      }
      for (const [x, y] of anchors) {
        anchor(ctx, x, y, s, lw, C.magenta);
        symbol([x - 7 * s, y - 12 * s, x + 7 * s, y + 12 * s], x, y);
      }
    }

    // 13. Lights as the key draws them (see lightPlan): sector arcs up close, a narrow white
    // sector a yellow wedge, a directional light's beam, a lighthouse's colour ring. A sector
    // "from 200° to 238°" is seen from ships bearing 200-238° towards the light, so it lies
    // on the opposite side.
    const sectorRows = new Map();
    for (const row of data.sector || []) {
      const key = `${row[0]},${row[1]}`;
      if (!sectorRows.has(key)) sectorRows.set(key, []);
      sectorRows.get(key).push(row);
    }
    const plans = new Map(); // lit aid -> its plan
    const taken = new Set(); // sector keys an aid has
    for (const aid of data.aid || []) {
      if (!aid[4]) continue;
      let rows = [];
      for (const dx of [0, -1, 1]) {
        for (const dy of [0, -1, 1]) {
          const key = `${aid[0] + dx},${aid[1] + dy}`;
          if (!rows.length && sectorRows.has(key) && !taken.has(key)) { rows = sectorRows.get(key); taken.add(key); }
        }
      }
      plans.set(aid, lightPlan(aid, rows, data.lead || []));
    }
    const lights = [...plans].map(([aid, plan]) => ({ x: aid[0], y: aid[1], aid, plan }));
    for (const [key, rows] of sectorRows) {
      if (!taken.has(key)) lights.push({ x: rows[0][0], y: rows[0][1], aid: null, plan: lightPlan(null, rows, []) });
    }
    const r = Math.min(80 * s, ARC_MAX), ringR = Math.min(46 * s, ARC_MAX), width = 4.5 * s;
    // Soundings keep off the arcs, wedges and beams; labels too where they can.
    const keepOff = (x, y, px, py) => {
      const box = [px - 4 * s, py - 4 * s, px + 4 * s, py + 4 * s];
      marks.push(box);
      if (inside(x, y)) label.soft.push(box);
    };
    const narrow = (arc) => arc.letter === "W" && arc.span <= NARROW;
    if (zoom >= 14) {
      // The limits, dotted from the light to a little past the arcs.
      ctx.lineCap = "round";
      ctx.setLineDash([0.01 * unit, 4 * unit]);
      ctx.beginPath();
      for (const { x, y, plan } of lights) {
        for (const arc of plan.arcs) {
          for (const a of [arc.a1, arc.a2]) {
            ctx.moveTo(x + Math.cos(a) * 7 * s, y + Math.sin(a) * 7 * s);
            ctx.lineTo(x + Math.cos(a) * 1.3 * r, y + Math.sin(a) * 1.3 * r);
          }
        }
      }
      ctx.strokeStyle = C.black;
      ctx.lineWidth = 1.1 * unit;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineCap = "butt";
      for (const { x, y, plan } of lights) {
        for (const arc of plan.arcs) {
          if (narrow(arc)) {
            // A narrow white sector: a yellow wedge out to the arcs, no edge.
            ctx.beginPath(); ctx.moveTo(x, y); ctx.arc(x, y, r, arc.a1, arc.a2); ctx.closePath();
            ctx.fillStyle = C.lightY; ctx.fill();
            const mid = (arc.a1 + arc.a2) / 2;
            for (let d = 8 * s; d <= r; d += 8 * s) keepOff(x, y, x + Math.cos(mid) * d, y + Math.sin(mid) * d);
          } else {
            for (let a = arc.a1; a <= arc.a2; a += (8 * s) / r) keepOff(x, y, x + Math.cos(a) * r, y + Math.sin(a) * r);
          }
        }
      }
      // Thin coloured arcs, a white light's orange-yellow with a thin dark edge; an all-round
      // lighthouse a ring in its light's colour.
      const rings = lights.filter((l) => l.aid && l.aid[2] === "m" && l.plan.lead == null && l.plan.dir == null && !l.plan.arcs.length);
      for (const l of rings) l.plan.ring = lightColours(l.aid)[0] || "W";
      for (const pass of [0, 1]) {
        for (const { x, y, plan } of lights) {
          const parts = plan.ring ? [{ a1: 0, a2: 2 * Math.PI, letter: plan.ring }] : plan.arcs.filter((arc) => !narrow(arc));
          for (const { a1, a2, letter } of parts) {
            const pale = ARC[letter] === C.lightArc;
            if (pass === 0 && !pale) continue;
            ctx.beginPath();
            ctx.arc(x, y, plan.ring ? ringR : r, a1, a2);
            ctx.strokeStyle = pass === 0 ? C.black : ARC[letter];
            ctx.lineWidth = pass === 0 ? width + 1.1 * unit : width;
            ctx.stroke();
          }
        }
      }
      for (const { x, y } of rings) {
        for (let a = 0; a < 2 * Math.PI; a += (8 * s) / ringR) keepOff(x, y, x + Math.cos(a) * ringR, y + Math.sin(a) * ringR);
      }
      // Each sector's colour letter just outside its arc, turned to follow it.
      ctx.font = `${13 * s}px ${SANS}`;
      ctx.fillStyle = C.black;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const { x, y, plan } of lights) {
        const placed = [];
        for (const { a1, a2, letter } of plan.arcs) {
          if (a2 - a1 < 0.03) continue;
          // Narrow neighbours' letters would touch: the later one steps further out.
          const a = (a1 + a2) / 2;
          let d = r + 9 * s, lx, ly;
          for (let tries = 0; tries < 4; tries++, d += 12 * s) {
            lx = x + Math.cos(a) * d;
            ly = y + Math.sin(a) * d;
            if (!placed.some(([px, py]) => Math.hypot(px - lx, py - ly) < 12 * s)) break;
          }
          placed.push([lx, ly]);
          ctx.save();
          ctx.translate(lx, ly);
          ctx.rotate(a + Math.PI / 2); // the letter's top outwards, as in the key
          ctx.fillText(letter, 0, 0);
          ctx.restore();
          marks.push([lx - 6 * s, ly - 6 * s, lx + 6 * s, ly + 6 * s]);
        }
      }
    }
    // A directional light's beam from zoom 13: a solid black line out along it, its bearing
    // in red beside it ("255,5° - 075,5°", towards the light first).
    if (zoom >= 13) {
      const len = Math.min(3.2 * r, BEAM_MAX);
      for (const { x, y, plan } of lights) {
        if (plan.dir == null) continue;
        const a = (plan.dir * Math.PI) / 180, ux = Math.sin(a), uy = -Math.cos(a);
        const beam = [x + ux * 8 * s, y + uy * 8 * s, x + ux * len, y + uy * len];
        strokeLine(ctx, beam, C.black, 1.4 * unit);
        for (let d = 50 * s; d <= len; d += 8 * s) keepOff(x, y, x + ux * d, y + uy * d);
        const size = Math.max(MIN_TEXT * unit, 17 * s);
        const b = (plan.toward * Math.PI) / 180;
        const text = bearingText(plan.toward, "-"), font = `${size}px ${SANS}`;
        const run = [x + ux * 30 * s, y + uy * 30 * s, x + ux * len, y + uy * len];
        const off = [Math.cos(b) * 0.8 * size, Math.sin(b) * 0.8 * size];
        // Every tile round the light draws the same beam: the label goes at the first spot
        // along it that lies wholly in one tile, and that tile draws it.
        ctx.font = font;
        const w = textWidth(ctx, text, 0), h = 0.9 * size;
        const hx = (Math.abs(ux) * w + Math.abs(uy) * h) / 2, hy = (Math.abs(uy) * w + Math.abs(ux) * h) / 2;
        const oneTile = (lo, hi) => Math.floor((lo - 1) / e) === Math.floor((hi + 1) / e);
        const at = BEAM_AT.find((t) => {
          const cx = run[0] + (run[2] - run[0]) * t + off[0], cy = run[1] + (run[3] - run[1]) * t + off[1];
          return oneTile(cx - hx, cx + hx) && oneTile(cy - hy, cy + hy);
        });
        if (at != null) alongLine(ctx, label, run, e, { text, font, size, color: C.lightR, need: 0, at, offset: off });
      }
    }

    // 14. Navigation aids: every flare first, then the black symbols over them. Floating
    // marks before fixed ones, lights last. Radar beacons get their magenta ring.
    const order = { v: 0, p: 0, k: 1, l: 1, b: 1, r: 2, u: 2, o: 2, t: 2, a: 3, d: 3, s: 3, m: 4 };
    const aids = (data.aid || []).slice().sort((a, b) => (order[a[2]] ?? 2) - (order[b[2]] ?? 2));
    for (const aid of aids) {
      if (!aid[4]) continue;
      const plan = plans.get(aid) || { arcs: [] };
      if (plan.ring) continue; // its ring shows its light
      for (const b of drawFlares(ctx, aid, s, lw, plan, zoom >= 14)) {
        const box = flareBox(aid[0], aid[1], s, b);
        marks.push(box);
        if (inside(aid[0], aid[1])) label.soft.push(box);
      }
    }
    for (const aid of aids) {
      if (aid[2] !== "t") continue;
      ctx.beginPath(); ctx.arc(aid[0], aid[1], 22 * s, 0, 7);
      ctx.lineWidth = 1.3 * lw; ctx.strokeStyle = C.magenta; ctx.stroke();
    }
    for (const aid of aids) {
      drawMark(ctx, aid, s, lw, zoom);
      symbol(markBox(aid, s), aid[0], aid[1]);
    }

    // 15. Landmarks, pilot boarding places and signs.
    for (const [x, y, kind] of data.landmark || []) {
      symbol(drawLandmark(ctx, x, y, kind, s, lw), x, y);
    }
    for (const [x, y] of data.pilot || []) {
      symbol(drawPilot(ctx, x, y, s, lw), x, y);
    }
    for (const [x, y, , , orient] of data.call || []) {
      symbol(drawCall(ctx, x, y, orient, s, lw), x, y);
    }
    for (const [x, y, type, value] of data.sign || []) {
      symbol(drawSign(ctx, x, y, type, value, s, lw), x, y);
    }

    // 16. Soundings where no label or symbol is; then the labels along lines.
    if (zoom >= 12) {
      // The overlay puts a label in its first spot clear of symbols with the least clutter
      // round it: keep soundings off that spot too, as far as the tile can tell.
      cands.forEach((cand, i) => {
        let best = null, fewest = Infinity;
        for (const spot of lays[i].spots) {
          const box = spotBox(cand, spot);
          if (symbols.some((b) => overlaps(box, b)) || label.hard.some((b) => overlaps(box, b))) continue;
          const n = cand.prio <= PRIO.sea ? 0 : marks.reduce((sum, b) => sum + overlaps(box, b), 0);
          if (n < fewest) { best = { spot, box }; fewest = n; }
          if (n === 0) break;
        }
        if (best && best.spot !== lays[i].spots[0]) label.claim(best.box, false);
      });
      // Those across the tile's edge were placed first (their room kept); the symbols,
      // which both tiles have, may still rule one out.
      for (const [x, y, depth, box] of edgeSoundings) {
        if (marks.some((m) => !localMarks.has(m) && overlaps(box, m))) continue;
        if (inside(x, y)) label.soft.push(box);
        sounding(ctx, x, y, depth, s);
      }
      for (const [x, y, depth] of soundings) {
        const box = soundingBox(x, y, depth, s);
        if (!label.free(box) || marks.some((m) => overlaps(box, m))) continue;
        label.claim(box, false);
        if (inside(x, y)) label.soft.push(box);
        sounding(ctx, x, y, depth, s);
      }
    }
    label.flush();

    // What the overlay needs: the tile's own labels and its obstacles.
    return { z: zoom, x: coords.x, y: coords.y, e, cands: cands.filter((c) => c.inside), hard: label.hard, soft: label.soft };
  }

  // --- the screen's labels --------------------------------------------------------------

  // Boxes in a coarse grid, for quick overlap tests.
  class BoxGrid {
    constructor(cell) { this.cell = cell; this.cells = new Map(); }
    _keys(b, fn) {
      const c = this.cell;
      const x0 = Math.floor(b[0] / c), x1 = Math.floor(b[2] / c), y0 = Math.floor(b[1] / c), y1 = Math.floor(b[3] / c);
      for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) if (fn((x + 4096) * 8192 + y + 4096)) return true;
      return false;
    }
    insert(b) {
      this._keys(b, (key) => {
        let list = this.cells.get(key);
        if (!list) this.cells.set(key, (list = []));
        list.push(b);
      });
    }
    hits(b) {
      return this._keys(b, (key) => (this.cells.get(key) || []).some((o) => overlaps(b, o)));
    }
    count(b) {
      let n = 0;
      const seen = new Set();
      this._keys(b, (key) => {
        for (const o of this.cells.get(key) || []) if (!seen.has(o) && overlaps(b, o)) { seen.add(o); n++; }
      });
      return n;
    }
  }

  // Canvases are drawn at the screen's own pixel ratio, up to 3x: ben tried 2x tiles on his
  // S22's 3x screen (0.19.0, half the pixels) and preferred the sharper look.
  const MAX_DPR = 3;
  const pixelRatio = () => Math.min(window.devicePixelRatio || 1, MAX_DPR);

  const PANE = "fwChartLabels";
  const MAX_LABELS = 1200; // candidates looked at per redraw

  // A canvas over the whole map (and a margin round it) with the chart's lettering, placed
  // for the screen as a whole: redrawn after every move or zoom and as tiles arrive.
  const LabelOverlay = L.Layer.extend({
    options: { padding: 0.25 },

    initialize(chart) { this._chart = chart; },

    onAdd(map) {
      if (!map.getPane(PANE)) {
        const pane = map.createPane(PANE);
        pane.style.zIndex = 350; // over the tiles, under paths, markers and popups
        pane.style.pointerEvents = "none";
      }
      this._canvas = L.DomUtil.create("canvas", "leaflet-zoom-animated", map.getPane(PANE));
      this._canvas.style.pointerEvents = "none";
      this._reset();
    },

    onRemove() {
      clearTimeout(this._timer);
      cancelAnimationFrame(this._frame);
      L.DomUtil.remove(this._canvas);
      this._canvas = null;
    },

    getEvents() {
      return { viewreset: this._reset, moveend: this._reset, zoom: this._onZoom, zoomanim: this._onAnimZoom, resize: this._reset };
    },

    _onAnimZoom(e) { this._updateTransform(e.center, e.zoom); },
    _onZoom() { this._updateTransform(this._map.getCenter(), this._map.getZoom()); },

    // While zooming, the drawn labels are scaled with the map, as Leaflet's own canvas does.
    _updateTransform(center, zoom) {
      if (!this._center) return;
      const map = this._map;
      const scale = map.getZoomScale(zoom, this._zoom);
      const viewHalf = map.getSize().multiplyBy(0.5 + this.options.padding);
      const centerPoint = map.project(this._center, zoom);
      const topLeft = viewHalf.multiplyBy(-scale).add(centerPoint).subtract(map._getNewPixelOrigin(center, zoom));
      L.DomUtil.setTransform(this._canvas, topLeft, scale);
    },

    // Called by the chart as tiles arrive: redraw soon, not for every tile.
    schedule() {
      if (!this._map || this._timer) return;
      this._timer = setTimeout(() => {
        this._timer = null;
        if (!this._map || this._map._animatingZoom) return; // zoomend redraws
        // Mid-pan (a finger on the map, or the glide after it): moveend redraws, once.
        if (this._map.dragging?.moving() || this._map._panAnim?._inProgress) return;
        cancelAnimationFrame(this._frame);
        this._frame = requestAnimationFrame(() => (this._stale ? this._reset() : this._draw()));
      }, 90);
    },

    // Whether any tile of the zoom shown has arrived.
    _ready() {
      const chart = this._chart, tz = chart._tileZoom;
      if (tz == null) return true; // out of the chart's zooms: clear
      for (const key in chart._tiles) {
        const tile = chart._tiles[key];
        if (tile.coords.z === tz && tile.el._fw) return true;
      }
      return false;
    },

    _reset() {
      const map = this._map;
      if (!map || !this._canvas) return;
      // Just zoomed and no tile of the new zoom here yet: keep the old labels (scaled with
      // the map) until some arrive, rather than blanking them.
      this._stale = !this._ready();
      if (this._stale) return;
      const p = this.options.padding;
      const size = map.getSize();
      const min = map.containerPointToLayerPoint(size.multiplyBy(-p)).round();
      this._bounds = L.bounds(min, min.add(size.multiplyBy(1 + 2 * p)).round());
      this._center = map.getCenter();
      this._zoom = map.getZoom();
      L.DomUtil.setPosition(this._canvas, min);
      const dpr = pixelRatio();
      const w = this._bounds.max.x - min.x, h = this._bounds.max.y - min.y;
      if (this._canvas.width !== Math.round(w * dpr) || this._canvas.height !== Math.round(h * dpr)) {
        this._canvas.width = Math.round(w * dpr);
        this._canvas.height = Math.round(h * dpr);
        this._canvas.style.width = `${w}px`;
        this._canvas.style.height = `${h}px`;
      }
      this._dpr = dpr;
      cancelAnimationFrame(this._frame);
      this._draw();
    },

    _draw() {
      const map = this._map, chart = this._chart, canvas = this._canvas;
      if (!map || !canvas || !this._bounds) return;
      const t0 = performance.now();
      const ctx = canvas.getContext("2d");
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const tz = chart._tileZoom;
      if (tz == null || !chart._tiles) return;
      ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
      const W = this._bounds.max.x - this._bounds.min.x, H = this._bounds.max.y - this._bounds.min.y;
      const zoom = map.getZoom();
      const zscale = map.getZoomScale(zoom, tz);
      const origin = map.getPixelOrigin().add(this._bounds.min);
      const ts = chart.getTileSize().x;
      const sc = scaleAt(tz);

      const hard = new BoxGrid(64), soft = new BoxGrid(64);
      const cands = [];
      const margin = 120;
      for (const key in chart._tiles) {
        const tile = chart._tiles[key];
        const fw = tile.el._fw;
        if (!fw || tile.coords.z !== tz) continue;
        const f = (ts / fw.e) * zscale;
        const ox = fw.x * ts * zscale - origin.x, oy = fw.y * ts * zscale - origin.y;
        if (ox > W + margin || oy > H + margin || ox + ts * zscale < -margin || oy + ts * zscale < -margin) continue;
        const conv = (b) => [ox + b[0] * f, oy + b[1] * f, ox + b[2] * f, oy + b[3] * f];
        for (const b of fw.hard) hard.insert(conv(b));
        for (const b of fw.soft) soft.insert(conv(b));
        for (const c of fw.cands) {
          const x = ox + c.x * f, y = oy + c.y * f;
          if (x < -margin || y < -margin || x > W + margin || y > H + margin) continue;
          cands.push({ c, x, y, gx: Math.round(x + origin.x), gy: Math.round(y + origin.y) });
        }
      }
      cands.sort((a, b) => a.c.prio - b.c.prio || a.c.rank - b.c.rank || a.gy - b.gy || a.gx - b.gx);
      let placed = 0;
      const draws = [];
      const once = new Map(); // text -> where it was placed, for labels repeated across an area
      for (let i = 0; i < cands.length && i < MAX_LABELS; i++) {
        const { c, x, y } = cands[i];
        const onceKey = c.once && c.lines[0][0];
        if (onceKey && (once.get(onceKey) || []).some(([px, py]) => Math.hypot(px - x, py - y) < c.once)) continue;
        const lay = layoutLabel(ctx, c, 1, sc);
        let best = null, bestSoft = Infinity;
        for (const spot of lay.spots) {
          const box = [x + spot.box[0], y + spot.box[1], x + spot.box[2], y + spot.box[3]];
          if (hard.hits(box)) continue;
          const n = c.prio <= PRIO.sea ? 0 : soft.count(box);
          if (n < bestSoft) { best = { spot, box }; bestSoft = n; }
          if (n === 0) break;
        }
        // Names of land and sites give way to soundings and rocks; the rest only shift.
        if (!best || (bestSoft > 0 && c.prio >= PRIO.island && bestSoft > 1)) continue;
        hard.insert(best.box);
        if (onceKey) once.set(onceKey, [...(once.get(onceKey) || []), [x, y]]);
        placed++;
        draws.push([lay, x + best.spot.dx, y + best.spot.dy, best.spot.align]);
      }
      for (const [lay, x0, y0, align] of draws) drawLabel(ctx, lay, x0, y0, align);
      window.__fwLabelStats = { ms: Math.round((performance.now() - t0) * 10) / 10, candidates: cands.length, placed };
    },
  });

  // --- the Leaflet layer -------------------------------------------------------------

  const tileAids = new Map(); // tile key -> [{ latlng, aid } or { latlng, site }]
  const RETRY_MS = 2500;
  let waitingTiles = 0; // tiles whose data is still downloading
  let waitingChanged = null;
  let rev = ""; // changes when the server's tiles do (e.g. rocks added), so cached tiles are dropped
  const layers = new Set();

  const ChartLayer = L.GridLayer.extend({
    options: { minZoom: 8, maxZoom: 19, updateWhenZooming: false, keepBuffer: 2, zIndex: 3 },

    createTile(coords, done) {
      const dpr = pixelRatio();
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
      let shown = false;
      const show = (err) => {
        if (shown) return;
        shown = true;
        done(err, canvas);
      };
      controller.signal.addEventListener("abort", () => setWaiting(false));
      // The server answers 202 while an area's data is still downloading; ask again shortly.
      const load = () => {
        fetch(`api/vchart/${coords.z}/${coords.x}/${coords.y}?r=${rev}`, { signal: controller.signal })
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
          .then((data) => {
            if (data.pending) {
              // Left clear meanwhile: the map underneath shows until the chart is drawn.
              if (!waiting && !shown && coords.z > OVERVIEW_Z) show(null);
              setWaiting(true);
              canvas._retry = setTimeout(load, RETRY_MS);
              return;
            }
            setWaiting(false);
            canvas._fw = drawTile(canvas, data, coords, dpr);
            this._rememberTaps(coords, data);
            show(null);
            this._labels?.schedule();
          })
          .catch((err) => {
            if (err.name !== "AbortError") console.error("Chart tile", coords, err);
            setWaiting(false);
            show(err.name === "AbortError" ? null : err);
          });
      };
      load();
      return canvas;
    },

    // Aids and heritage sites in the tile, for taps.
    _rememberTaps(coords, data) {
      const size = this.getTileSize();
      const nw = coords.scaleBy(size);
      const at = (x, y) => this._map.unproject(nw.add([x * size.x / data.e, y * size.y / data.e]), coords.z);
      const inside = (x, y) => x >= 0 && y >= 0 && x < data.e && y < data.e;
      const list = [];
      for (const aid of data.aid || []) if (inside(aid[0], aid[1])) list.push({ latlng: at(aid[0], aid[1]), aid });
      for (const site of data.heritage || []) if (inside(site[0], site[1])) list.push({ latlng: at(site[0], site[1]), site });
      const key = this._tileCoordsToKey(coords);
      if (list.length) tileAids.set(key, list);
      else tileAids.delete(key);
    },

    // Browser zoom or a move to another screen changes the pixel ratio: draw the tiles
    // again at the new one (they come back from the browser's cache).
    _watchPixelRatio() {
      if (!window.matchMedia) return;
      this._ratioChanged = this._ratioChanged || (() => {
        this._watchPixelRatio();
        this.redraw();
        this._labels?._reset();
      });
      this._ratioQuery?.removeEventListener?.("change", this._ratioChanged);
      this._ratioQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
      this._ratioQuery.addEventListener?.("change", this._ratioChanged);
    },

    _tileUnload(e) {
      clearTimeout(e.tile._retry);
      e.tile._abort?.abort();
      e.tile._fw = null;
      tileAids.delete(this._tileCoordsToKey(e.coords));
    },

    onRemove(map) {
      layers.delete(this);
      this._ratioQuery?.removeEventListener?.("change", this._ratioChanged);
      this._ratioQuery = null;
      if (this._labels) map.removeLayer(this._labels);
      L.GridLayer.prototype.onRemove.call(this, map);
    },

    onAdd(map) {
      layers.add(this);
      L.GridLayer.prototype.onAdd.call(this, map);
      this.off("tileunload", this._tileUnload, this);
      this.on("tileunload", this._tileUnload, this);
      this._watchPixelRatio();
      this._labels = this._labels || new LabelOverlay(this);
      map.addLayer(this._labels);
    },

    // The lowest zoom the server has tiles for.
    _setMinZoom(z) {
      if (this.options.minZoom === z) return;
      this.options.minZoom = z;
      if (this._map) {
        this._resetView?.();
        this.redraw();
      }
    },
  });

  // --- Traficom's raster charts -----------------------------------------------------------

  // Traficom's chart service fills the parts of some tiles outside its charts with solid
  // black (seen zoomed out, round the overview charts' edges), which showed as big black
  // shapes over the sea. Its tiles are drawn through a canvas that makes such areas
  // see-through: black pixels inside a solid 7 x 7 black block, and the dark pixels next to
  // those. Chart ink (lettering, lines, small symbols) is never that solid, so it stays.
  function clearNoData(ctx, w, h) {
    const image = ctx.getImageData(0, 0, w, h), d = image.data, n = w * h;
    const black = new Uint8Array(n);
    let count = 0;
    for (let i = 0, p = 0; i < n; i++, p += 4) {
      if (d[p + 3] > 200 && d[p] < 24 && d[p + 1] < 24 && d[p + 2] < 24) { black[i] = 1; count++; }
    }
    const R = 3, full = (2 * R + 1) ** 2;
    if (count < full) return;
    const W = w + 1;
    const table = (m) => { // summed-area table
      const t = new Int32Array(W * (h + 1));
      for (let y = 0; y < h; y++) {
        let row = 0;
        for (let x = 0; x < w; x++) { row += m[y * w + x]; t[(y + 1) * W + x + 1] = t[y * W + x + 1] + row; }
      }
      return t;
    };
    const box = (t, x0, y0, x1, y1) => t[y1 * W + x1] - t[y0 * W + x1] - t[y1 * W + x0] + t[y0 * W + x0];
    const sat = table(black);
    const core = new Uint8Array(n);
    let cores = 0;
    for (let y = R; y < h - R; y++) {
      for (let x = R; x < w - R; x++) {
        if (black[y * w + x] && box(sat, x - R, y - R, x + R + 1, y + R + 1) === full) { core[y * w + x] = 1; cores++; }
      }
    }
    if (!cores) return;
    const near = table(core);
    for (let y = 0, i = 0; y < h; y++) {
      for (let x = 0; x < w; x++, i++) {
        const p = 4 * i;
        if (d[p] + d[p + 1] + d[p + 2] > 180) continue;
        if (box(near, Math.max(0, x - R), Math.max(0, y - R), Math.min(w, x + R + 1), Math.min(h, y + R + 1))) d[p + 3] = 0;
      }
    }
    ctx.putImageData(image, 0, 0);
  }

  const RasterLayer = L.GridLayer.extend({
    initialize(url, opts) {
      this._url = url;
      L.GridLayer.prototype.initialize.call(this, opts);
      this.on("tileunload", (e) => {
        const img = e.tile._img;
        if (img) { img.onload = img.onerror = null; img.src = ""; }
      });
    },

    createTile(coords, done) {
      const canvas = document.createElement("canvas");
      const img = new Image();
      canvas._img = img;
      img.onload = () => {
        canvas._img = null;
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(img, 0, 0);
        try {
          clearNoData(ctx, canvas.width, canvas.height);
        } catch (err) {
          // A canvas the browser won't let us read: show the tile as it is.
        }
        done(null, canvas);
      };
      img.onerror = () => { canvas._img = null; done(new Error("no tile"), canvas); };
      img.src = L.Util.template(this._url, { x: coords.x, y: coords.y, z: coords.z });
      return canvas;
    },
  });

  function datingText(dating) {
    return String(dating).split(",").map((d) => d.trim()).filter(Boolean)
      .map((d) => DATINGS[d.toLowerCase()] || d).join(", ");
  }

  function accuracyText(accuracy) {
    const a = String(accuracy).trim();
    const low = a.toLowerCase();
    if (low.startsWith("tarkka")) return "Position accurate to 10 m";
    if (low.startsWith("ohjeellinen")) return "Position within 10–100 m";
    if (low.startsWith("suuntaa")) return "Position approximate, within 100–1000 m";
    if (low.startsWith(">")) return "Position rough, more than 1 km out";
    if (low.startsWith("ei tiedossa")) return "Position accuracy unknown";
    return `Position: ${a}`;
  }

  function heritageInfo(item) {
    const [, , id, name, cls, kind, dating, accuracy, , sub] = item.site;
    const lines = [];
    if (sub) lines.push(String(sub)[0].toUpperCase() + String(sub).slice(1));
    if (dating) lines.push(`Dating: ${datingText(dating)}`);
    if (accuracy) lines.push(accuracyText(accuracy));
    if (cls === 0 && kind <= 3) lines.push("Wrecks over 100 years old are protected: diving is allowed, disturbing them is not.");
    return {
      latlng: item.latlng,
      name: name || null,
      description: `${HERITAGE_KINDS[kind] || "Underwater site"} · ${HERITAGE_CLASSES[cls] || "heritage site"}`,
      lines,
      link: { href: `https://www.kyppi.fi/to.aspx?id=112.${encodeURIComponent(id)}`, text: "Museovirasto register entry" },
    };
  }

  // The navigation aid or heritage site nearest a tap, if one is within reach.
  function aidAt(map, latlng, maxPx = 18) {
    const p = map.latLngToContainerPoint(latlng);
    let best = null, bestD = maxPx;
    for (const list of tileAids.values()) {
      for (const item of list) {
        if (item.site && !options.heritage) continue;
        const d = p.distanceTo(map.latLngToContainerPoint(item.latlng));
        if (d < bestD) { best = item; bestD = d; }
      }
    }
    if (!best) return null;
    if (best.site) return heritageInfo(best);
    const [, , type, cat, lit, name] = best.aid;
    const kind = TYPE_NAMES[type] || "mark";
    const category = CATEGORY_NAMES[cat];
    return {
      latlng: best.latlng,
      name: name || null,
      description: `${category ? `${category} ${kind}` : kind[0].toUpperCase() + kind.slice(1)}${lit ? ", lit" : ""}`,
    };
  }

  // For the simulator's symbol sheet (/dev/symbols): a tile drawn with its own labels, each
  // in its first spot clear of the symbols, as the screen's labels mostly are.
  function drawTileWithLabels(canvas, data, coords) {
    const res = drawTile(canvas, data, coords, 1);
    const ctx = canvas.getContext("2d");
    const taken = [...res.hard, ...res.soft];
    for (const cand of res.cands) {
      const lay = layoutLabel(ctx, cand, data.e / 256, scaleAt(coords.z));
      const box = (sp) => [cand.x + sp.box[0], cand.y + sp.box[1], cand.x + sp.box[2], cand.y + sp.box[3]];
      const spot = lay.spots.find((sp) => !taken.some((b) => overlaps(box(sp), b))) || lay.spots[0];
      taken.push(box(spot));
      drawLabel(ctx, lay, cand.x + spot.dx, cand.y + spot.dy, spot.align);
    }
  }

  window.FerryChart = {
    drawTileWithLabels,
    layer: (opts) => new ChartLayer({ minZoom: serverMinZoom, ...opts }),
    // A tile layer for Traficom's raster charts (as L.tileLayer), without their black no-data.
    rasterLayer: (url, opts) => new RasterLayer(url, opts),
    aidAt,
    colors: C,
    onWaiting: (fn) => { waitingChanged = fn; },
    // The server's tile revision (and the lowest zoom it cuts tiles for, if it says).
    setRev: (r, minZoom) => {
      if (Number.isInteger(minZoom) && minZoom !== serverMinZoom) {
        serverMinZoom = minZoom;
        for (const layer of layers) layer._setMinZoom(minZoom);
      }
      if (!r || r === rev) return;
      const first = rev === "";
      rev = r;
      if (!first) for (const layer of layers) layer.redraw();
    },
    // { heritage: bool }: show Museovirasto's wrecks and underwater sites (on by default).
    setOptions: (opts) => {
      let changed = false;
      if (opts && "heritage" in opts && !!opts.heritage !== options.heritage) {
        options.heritage = !!opts.heritage;
        changed = true;
      }
      if (changed) for (const layer of layers) layer.redraw();
    },
  };
})();
