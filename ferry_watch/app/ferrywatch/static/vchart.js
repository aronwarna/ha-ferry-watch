/* Ferry Watch: the vector nautical chart.
 *
 * Draws the chart tiles the add-on cuts from Traficom's depth data, Väylävirasto's
 * fairway data, the Land Survey's rocks and names and Museovirasto's wreck register
 * (see vchart.py), in the inks, symbols and lettering of Traficom's scanned paper
 * charts. Exposes window.FerryChart.
 */
(function () {
  "use strict";

  // The inks of Traficom's scanned paper chart.
  const C = {
    land: "#f9efca",
    coast: "#000000",
    depth: ["#c9e0b4", "#e4f1fb", "#e4f1fb", "#e4f1fb", "#ffffff"], // drying, 0-3, 3-6, 6-10, deeper
    contour: "#00a3e7", // 10 m and deeper, and their labels
    contourShallow: "#008bc5", // 3 m dotted, 6 m dashed
    sounding: "#000000",
    fairway: "#000000",
    fairwayArea: "#eceded",
    builtUp: "#eceded",
    band: "#bed389", // under the small-craft trunk route
    magenta: "#9d3289",
    magentaLight: "#c47db5",
    red: "#c63541",
    green: "#64a96f",
    yellow: "#f2d100",
    black: "#000000",
    brown: "#bea26a",
    fort: "#6b3a4f", // coastal defence sites ashore
  };
  const SANS = '"Helvetica Neue", Helvetica, Arial, "Liberation Sans", Roboto, sans-serif';
  const SERIF = '"Times New Roman", Times, "Liberation Serif", "Noto Serif", serif';

  // Symbol and text sizes below are the scanned chart's at zoom 14, in its pixels. The
  // scanned chart is a picture, so its symbols double with every zoom step; these grow
  // more gently. CHART_SCALE is the one number to tune: 1 is the scanned chart's size.
  const CHART_SCALE = 0.75;
  const ZOOM_SCALE = { 10: 0.45, 11: 0.5, 12: 0.6, 13: 0.8, 14: 1, 15: 1.1 };
  const MIN_TEXT = 10; // CSS px: names stay readable when zoomed out

  const LIGHT = { W: C.yellow, R: C.red, G: C.green, Y: C.yellow };
  // A sector light zoomed out: one flare per colour, fanned out (compass bearings).
  const FAN = { R: 190, W: 160, G: 130, Y: 220 };
  const SECTOR = { v: "W", p: "R", vi: "G", k: "Y" };

  const TYPE_NAMES = {
    v: "spar buoy", p: "buoy", r: "beacon", l: "leading mark", k: "cairn", m: "lighthouse",
    s: "sector light", d: "directional light", a: "minor light", t: "radar beacon", o: "mark",
  };
  const CATEGORY_NAMES = {
    1: "Port (red)", 2: "Starboard (green)", 3: "North cardinal", 4: "South cardinal",
    5: "West cardinal", 6: "East cardinal", 7: "Isolated danger", 8: "Safe water", 9: "Special",
  };

  // Towns are lettered large, as on the chart; other settlements get small capitals.
  const TOWNS = new Set(["Hanko", "Hangö", "Tammisaari", "Ekenäs", "Inkoo", "Ingå", "Kirkkonummi", "Kyrkslätt",
    "Espoo", "Esbo", "Helsinki", "Helsingfors", "Porvoo", "Borgå", "Loviisa", "Lovisa", "Kotka", "Hamina",
    "Fredrikshamn", "Paldiski", "Tallinn"]);
  const OPEN_WATER = /(fjärd|fjärden|selkä|selät|bukt|bukten)$/i; // lettered larger than other waters

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

  // How far past its edges a tile carries each kind of point (tile units, as vchart.py
  // sends them). A label may run over an edge only if the tile beyond has its point too.
  const BUFFER = { name: 256, light: 128, aid: 128, heritage: 64 };
  function edgeSafe(box, x, y, e, buf) {
    return (box[0] >= 0 || x < buf) && (box[2] <= e || x > e - buf) && (box[1] >= 0 || y < buf) && (box[3] <= e || y > e - buf);
  }

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

  // --- text -----------------------------------------------------------------------

  const HAS_SPACING = typeof CanvasRenderingContext2D !== "undefined" && "letterSpacing" in CanvasRenderingContext2D.prototype;

  function textWidth(ctx, text, spacing) {
    return ctx.measureText(text).width + spacing * (text.length - 1);
  }

  // Text drawn rightwards from (x, y), with extra space between the letters as the
  // chart prints its capitals.
  function spacedText(ctx, text, x, y, spacing) {
    ctx.textAlign = "left";
    if (!spacing) {
      ctx.fillText(text, x, y);
    } else if (HAS_SPACING) {
      ctx.letterSpacing = `${spacing}px`;
      ctx.fillText(text, x, y);
      ctx.letterSpacing = "0px";
    } else {
      for (const ch of text) {
        ctx.fillText(ch, x, y);
        x += ctx.measureText(ch).width + spacing;
      }
    }
  }

  // Lines of text, each { text, font, size, spacing, color }, left-aligned at x0 with the
  // block's top at y0. Text sits on its baseline, 0.35 em below the middle of its line.
  function textBlock(ctx, lines, x0, y0) {
    ctx.textBaseline = "alphabetic";
    let y = y0;
    for (const line of lines) {
      ctx.font = line.font;
      ctx.fillStyle = line.color;
      spacedText(ctx, line.text, x0, y + 0.5 * line.size + 0.35 * line.size, line.spacing);
      y += line.size;
    }
  }

  // Labels that don't overlap each other within a tile. Each claims its box first; all
  // the lettering is drawn at the end, over everything else, as charts print it.
  function labeller() {
    const boxes = [];
    const draws = [];
    const free = (b) => boxes.every((o) => b[0] >= o[2] || b[2] <= o[0] || b[1] >= o[3] || b[3] <= o[1]);
    return {
      free,
      claim(box, draw) {
        boxes.push(box);
        if (draw) draws.push(draw);
      },
      // The first free spot of a label's possible spots, if any.
      first(spots) {
        for (const spot of spots) {
          if (free(spot.box)) {
            boxes.push(spot.box);
            draws.push(spot.draw);
            return true;
          }
        }
        return false;
      },
      flush() {
        for (const draw of draws) draw();
      },
    };
  }

  // A label along a line, on its longest straight stretch, kept whole inside the tile.
  // Returns the stretch of line to leave out under it (null if it's beside the line), or
  // false if it found no room.
  function alongLine(ctx, label, flat, e, o) {
    ctx.font = o.font;
    const w = textWidth(ctx, o.text, o.spacing || 0);
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
    label.claim(box, () => {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(a);
      ctx.font = o.font;
      ctx.fillStyle = o.color;
      ctx.textBaseline = "alphabetic";
      spacedText(ctx, o.text, -w / 2, 0.35 * o.size, o.spacing || 0);
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
    const big = 22 * s, small = 14 * s, base = y + 0.36 * big;
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
    const big = 22 * s;
    const whole = String(Math.round(depth >= 20 ? depth : Math.floor(depth + 1e-6)));
    const decimal = depth < 20 && Math.round((depth - Math.floor(depth + 1e-6)) * 10) % 10 !== 0;
    const w = 0.56 * big * whole.length;
    return [x - w / 2, y - 0.4 * big, x + w / 2 + (decimal ? 0.5 * big : 0), y + (decimal ? 0.62 : 0.4) * big];
  }

  const fmtDraught = (d) => `(${d.toFixed(1).replace(".", ",")})`; // 9 -> "(9,0)"
  const fmtDeg = (v) => `${v.toFixed(1).padStart(5, "0").replace(".", ",")}°`;
  const bearingText = (b) => `${fmtDeg(b)} - ${fmtDeg((b + 180) % 360)}`; // 273.7 -> "273,7° - 093,7°"

  // A light's character as charts print it: ("Fl (2) 10 s", "WRG") -> "Fl(2)WRG.10s".
  function lightText(character, colours) {
    const m = character.match(/^(.*?)\s*(\d+(?:[.,]\d+)?)\s*s$/);
    const rhythm = (m ? m[1] : character).replace(/\s+\(/g, "(").replace(/\s+/g, "");
    const col = colours === "W" ? "" : colours; // a single white light has no letter, as on charts
    let text = rhythm;
    if (col) text += (/\)$/.test(text) ? "" : ".") + col;
    if (m) text += (/\)$/.test(text) && !col ? "" : ".") + `${m[2].replace(".", ",")}s`;
    return text;
  }

  // --- navigation aids --------------------------------------------------------------

  // A light's flare: a teardrop pointing away from the light, yellow (white light, edged
  // black), red or green. `bearing` is the compass direction it points to.
  function flare(ctx, x, y, s, lw, color, bearing) {
    const L = 40 * s, r = 5.5 * s, g = 3 * s, cy = L - r;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(((bearing - 180) * Math.PI) / 180); // local +y points along the bearing
    ctx.beginPath();
    ctx.moveTo(0, g);
    ctx.quadraticCurveTo(-0.9 * r, cy - 1.2 * r, -r, cy);
    ctx.arc(0, cy, r, Math.PI, 0, true); // round far end
    ctx.quadraticCurveTo(0.9 * r, cy - 1.2 * r, 0, g);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    if (color === C.yellow) {
      ctx.lineWidth = 0.8 * lw;
      ctx.strokeStyle = C.black;
      ctx.stroke();
    }
    ctx.restore();
  }

  // One flare in the light's colour, or for a sector light a fan of one per colour.
  function drawFlares(ctx, aid, s, lw) {
    const colours = [...new Set(String(aid[6] || "").replace(/[^WRGY]/g, ""))];
    if (colours.length <= 1) flare(ctx, aid[0], aid[1], s, lw, LIGHT[colours[0]] || C.yellow, 135);
    else for (const c of colours) flare(ctx, aid[0], aid[1], s, lw, LIGHT[c], FAN[c]);
  }

  // The position of a buoy or beacon: a small circle on a short base line.
  function base(ctx, s, lw) {
    ctx.lineWidth = lw;
    ctx.strokeStyle = C.black;
    ctx.beginPath();
    ctx.moveTo(-6 * s, 0); ctx.lineTo(-2.5 * s, 0);
    ctx.moveTo(2.5 * s, 0); ctx.lineTo(6 * s, 0);
    ctx.moveTo(2.5 * s, 0); ctx.arc(0, 0, 2.5 * s, 0, 7);
    ctx.stroke();
  }

  // Topmarks as on Finnish charts, standing on (x, y): port an open square, starboard a
  // cone, cardinals two cones.
  function topmark(ctx, cat, x, y, s, lw) {
    const w = 3.5 * s, h = 6 * s, gap = 2 * s;
    // A cone with its base at yb, pointing up or down.
    const cone = (yb, up) => {
      ctx.beginPath();
      ctx.moveTo(x - w, yb); ctx.lineTo(x + w, yb); ctx.lineTo(x, up ? yb - h : yb + h);
      ctx.closePath();
      ctx.fill();
    };
    const ball = (cy, r) => { ctx.beginPath(); ctx.arc(x, cy, r, 0, 7); ctx.fill(); };
    ctx.fillStyle = C.black;
    ctx.strokeStyle = C.black;
    switch (cat) {
      case 1: ctx.lineWidth = 1.5 * s; ctx.strokeRect(x - 2.5 * s, y - 8 * s, 5 * s, 8 * s); break;
      case 2: ctx.beginPath(); ctx.moveTo(x - w, y); ctx.lineTo(x + w, y); ctx.lineTo(x, y - 7 * s); ctx.closePath(); ctx.fill(); break;
      case 3: cone(y, true); cone(y - h - gap, true); break; // north: both up
      case 4: cone(y - h, false); cone(y - 2 * h - gap, false); break; // south: both down
      case 5: cone(y, true); cone(y - 2 * h - gap, false); break; // west: points together
      case 6: cone(y - h, false); cone(y - h - gap, true); break; // east: bases together
      case 7: ball(y - 2.6 * s, 2.4 * s); ball(y - 8 * s, 2.4 * s); break;
      case 8: ctx.fillStyle = C.red; ball(y - 2.8 * s, 2.6 * s); break;
      case 9:
        ctx.strokeStyle = C.yellow; ctx.lineWidth = 1.8 * s;
        ctx.beginPath();
        ctx.moveTo(x - w, y - 7 * s); ctx.lineTo(x + w, y);
        ctx.moveTo(x + w, y - 7 * s); ctx.lineTo(x - w, y);
        ctx.stroke();
        break;
      default: break;
    }
    ctx.lineWidth = lw;
  }

  // The black symbol of a buoy, beacon or light, as the scanned chart draws them.
  function drawMark(ctx, aid, s, lw, zoom) {
    const [x, y, type, cat, lit] = aid;
    ctx.save();
    ctx.translate(x, y);
    ctx.fillStyle = C.black;
    ctx.strokeStyle = C.black;
    ctx.lineWidth = lw;
    ctx.lineJoin = "miter";
    if (type === "v" || type === "p") {
      // Floating marks lean to the right. A spar buoy is a thin stem, or a black wedge
      // when lit; a buoy is a slim bell, filled when lit.
      base(ctx, s, lw);
      ctx.rotate(0.3);
      let top;
      ctx.beginPath();
      if (type === "v") {
        top = -30 * s;
        if (lit) {
          ctx.moveTo(-4.5 * s, -2.5 * s); ctx.lineTo(-1.5 * s, top); ctx.lineTo(1.5 * s, top); ctx.lineTo(4.5 * s, -2.5 * s);
          ctx.closePath();
          ctx.fill();
        } else {
          ctx.moveTo(0, -2.5 * s); ctx.lineTo(0, top);
          ctx.lineWidth = 1.5 * s;
          ctx.stroke();
        }
      } else {
        top = -17 * s;
        ctx.moveTo(-5 * s, -2.5 * s); ctx.lineTo(-2 * s, top + 2 * s);
        ctx.arc(0, top + 2 * s, 2 * s, Math.PI, 0);
        ctx.lineTo(5 * s, -2.5 * s);
        ctx.closePath();
        if (lit) ctx.fill();
        else ctx.stroke();
      }
      if (zoom >= 12) topmark(ctx, cat, 0, top - 0.5 * s, s, lw);
    } else if (type === "r") {
      // Fixed beacon: a black bar on a base line, with its topmark.
      ctx.beginPath(); ctx.moveTo(-6 * s, 0); ctx.lineTo(6 * s, 0); ctx.stroke();
      ctx.fillRect(-2.5 * s, -22 * s, 5 * s, 22 * s);
      if (zoom >= 12) topmark(ctx, cat, 0, -23 * s, s, lw);
    } else if (type === "l") {
      // Leading mark: a notched black board on a post.
      base(ctx, s, lw);
      ctx.beginPath(); ctx.moveTo(0, -2.5 * s); ctx.lineTo(0, -8 * s);
      ctx.lineWidth = 1.5 * s; ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(-2.5 * s, -8 * s); ctx.lineTo(0, -11 * s); ctx.lineTo(2.5 * s, -8 * s);
      ctx.lineTo(2.5 * s, -26 * s); ctx.lineTo(-2.5 * s, -26 * s);
      ctx.closePath();
      ctx.fill();
    } else if (type === "k") {
      // Cairn: a small open triangle.
      ctx.beginPath(); ctx.moveTo(-4.5 * s, 0); ctx.lineTo(4.5 * s, 0); ctx.lineTo(0, -9 * s); ctx.closePath();
      ctx.stroke();
    } else if (type === "m") {
      // Lighthouse: a black five-pointed star.
      ctx.beginPath();
      for (let i = 0; i < 10; i++) {
        const r = (i % 2 ? 3.2 : 8) * s, a = -Math.PI / 2 + (i * Math.PI) / 5;
        ctx.lineTo(r * Math.cos(a), r * Math.sin(a));
      }
      ctx.closePath();
      ctx.fill();
    } else if (type === "t") {
      // Radar beacon: a magenta ring.
      ctx.beginPath(); ctx.arc(0, 0, 9 * s, 0, 7);
      ctx.lineWidth = 1.2 * lw; ctx.strokeStyle = C.magenta; ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 1.6 * s, 0, 7); ctx.fill();
    } else if (type === "s" || type === "d" || type === "a") {
      // Lights: a black dot (the flare shows it is a light).
      ctx.beginPath(); ctx.arc(0, 0, Math.max(2 * s, 0.8 * lw), 0, 7); ctx.fill();
    } else {
      // Other marks: a dot in a ring.
      ctx.beginPath(); ctx.arc(0, 0, 2.6 * s, 0, 7); ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, 1.1 * s, 0, 7); ctx.fill();
    }
    ctx.restore();
  }

  // Where a symbol stands, so soundings keep clear of it.
  function markBox(aid, s) {
    const [x, y, type] = aid;
    if ("vprl".includes(type)) return [x - 7 * s, y - 34 * s, x + 16 * s, y + 3 * s];
    return [x - 8 * s, y - 9 * s, x + 8 * s, y + 8 * s];
  }

  // --- rocks, wrecks and heritage ---------------------------------------------------

  // Rocks as on Finnish charts, the bar being the water line: T under water, + at the
  // surface, ⊥ above water. A rock cluster adds dots.
  function drawRock(ctx, x, y, kind, s, group) {
    const a = 6 * s, h = 8 * s;
    ctx.lineWidth = 2 * s;
    ctx.lineCap = "butt";
    ctx.strokeStyle = ctx.fillStyle = C.black;
    ctx.beginPath();
    ctx.moveTo(x - a, y); ctx.lineTo(x + a, y);
    if (kind === 2) {
      ctx.moveTo(x, y - a); ctx.lineTo(x, y + a);
    } else {
      ctx.moveTo(x, y); ctx.lineTo(x, kind === 3 ? y - h : y + h);
    }
    ctx.stroke();
    if (!group) return;
    const dy = kind === 3 ? -h / s : h / s;
    const dots = kind === 2 ? [[-3.5, -3.5], [3.5, -3.5], [-3.5, 3.5], [3.5, 3.5]] : [[-4, dy], [4, dy]];
    for (const [dx, ddy] of dots) ctx.fillRect(x + dx * s - s, y + ddy * s - s, 2 * s, 2 * s);
  }

  // Wrecks: hull showing as a black hull; others as the wreck sign, dangerous ones ringed.
  function drawWreck(ctx, x, y, kind, s, color) {
    const t = 1.3 * s;
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = ctx.fillStyle = color;
    ctx.lineCap = "butt";
    if (kind === 3) {
      ctx.beginPath();
      ctx.moveTo(-6 * t, -1 * t); ctx.lineTo(6 * t, -1 * t); ctx.lineTo(4 * t, 2.5 * t); ctx.lineTo(-4 * t, 2.5 * t);
      ctx.closePath(); ctx.fill();
      ctx.beginPath(); ctx.moveTo(0, -1 * t); ctx.lineTo(0, -5 * t); ctx.lineWidth = 2 * s; ctx.stroke();
    } else {
      ctx.lineWidth = 1.6 * s;
      ctx.beginPath();
      ctx.moveTo(-6 * t, 0); ctx.lineTo(6 * t, 0);
      for (const dx of [-3, 0, 3]) { ctx.moveTo(dx * t, -2.2 * t); ctx.lineTo(dx * t, 2.2 * t); }
      ctx.stroke();
      if (kind === 1) {
        ctx.beginPath(); ctx.ellipse(0, 0, 9 * t, 5 * t, 0, 0, 7);
        ctx.lineWidth = 1.2 * s; ctx.lineCap = "round";
        ctx.setLineDash([0.01, 2.6 * s]); ctx.stroke(); ctx.setLineDash([]);
      }
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

  function anchor(ctx, x, y, s, lw) {
    ctx.save();
    ctx.translate(x, y);
    ctx.strokeStyle = C.black;
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

  // --- labels -------------------------------------------------------------------------

  // Every label but those along lines, each with the spots it may take, in the order they
  // are placed: towns, waters, islands, lights, other land, then heritage names.
  function labelCandidates(ctx, data, zoom, s, unit, aidAt) {
    const px = (n) => Math.max(MIN_TEXT * unit, n * s);
    const out = [];
    const line = (text, size, spacing, font, color) => ({ text, size, spacing, font, color });
    // A block of lines either side of a symbol at (x, y), vertically centred on cy; with dy,
    // also centred above and below it, for long names that would cross a tile edge beside it.
    const beside = (lines, x, cy, dxRight, dxLeft, buf, dy) => {
      let w = 0, h = 0;
      for (const l of lines) {
        ctx.font = l.font;
        w = Math.max(w, textWidth(ctx, l.text, l.spacing));
        h += l.size;
      }
      const places = [[x + dxRight, cy - h / 2], [x - dxLeft - w, cy - h / 2]];
      if (dy) places.push([x - w / 2, cy - dy - h], [x - w / 2, cy + dy]);
      return places.map(([x0, top]) => ({
        box: [x0 - unit, top + 0.1 * h, x0 + w + unit, top + 0.95 * h],
        draw: () => textBlock(ctx, lines, x0, top),
      })).filter((spot) => edgeSafe(spot.box, x, cy, data.e, buf));
    };

    // Names. A town's Finnish and Swedish names close together share one label.
    const names = [];
    for (const [x, y, text, kind] of data.name || []) {
      if (!text) continue;
      if (kind === "s") {
        const twin = names.find((n) => n.kind === "s" && n.texts.length === 1 && Math.hypot(n.x - x, n.y - y) < 40 * unit);
        if (twin) {
          if (twin.texts[0].toUpperCase() !== text.toUpperCase()) twin.texts.push(text);
          continue;
        }
      }
      names.push({ x, y, kind, texts: [text] });
    }
    for (const { x, y, kind, texts } of names) {
      let size, font, spacing = 0, upper = false, prio;
      if (kind === "s" && texts.some((t) => TOWNS.has(t))) {
        size = px(46); font = `${size}px ${SERIF}`; spacing = 0.04 * size; upper = true; prio = 0;
      } else if (kind === "w") {
        size = px(OPEN_WATER.test(texts[0]) ? 27 : 22); font = `italic ${size}px ${SANS}`; prio = 1;
      } else if (kind === "i") {
        size = px(18); font = `${size}px ${SANS}`; prio = 2;
      } else {
        size = px(18); font = `${size}px ${SANS}`; spacing = 2 * s; upper = true; prio = 4;
      }
      const lines = texts.map((t) => line(upper ? t.toUpperCase() : t, size * (texts.length > 1 ? 0.95 : 1), spacing, font, C.black));
      ctx.font = font;
      const w = Math.max(...lines.map((l) => textWidth(ctx, l.text, spacing)));
      const h = lines.reduce((sum, l) => sum + l.size, 0);
      const draw = () => {
        let top = y - h / 2;
        for (const l of lines) {
          ctx.font = font;
          textBlock(ctx, [l], x - textWidth(ctx, l.text, spacing) / 2, top);
          top += l.size;
        }
      };
      const box = [x - w / 2 - unit, y - 0.42 * h, x + w / 2 + unit, y + 0.42 * h];
      if (edgeSafe(box, x, y, data.e, BUFFER.name)) out.push({ prio, x, y, spots: [{ box, draw }] });
    }

    // Light characters beside their lights; major lights also get their name above.
    for (const [x, y, character] of data.light || []) {
      const aid = aidAt.get(`${x},${y}`);
      const type = aid ? aid[2] : "";
      if (zoom < 14 && !(type === "s" || type === "m" || type === "d")) continue;
      const lines = [];
      if ((type === "s" || type === "m") && aid[5]) lines.push(line(aid[5].toUpperCase(), px(22), 3 * s, `${px(22)}px ${SANS}`, C.black));
      lines.push(line(lightText(character, aid ? aid[6] || "" : ""), px(19), 0, `${px(19)}px ${SANS}`, C.black));
      const tall = !!type && "vprl".includes(type);
      out.push({ prio: 3, x, y, spots: beside(lines, x, y - (tall ? 12 : 0) * s, (tall ? 14 : 10) * s, (tall ? 8 : 10) * s, BUFFER.light) });
    }
    for (const aid of data.aid || []) {
      if (aid[2] !== "t" || zoom < 13) continue;
      const lines = [line("Racon", px(19), 0, `${px(19)}px ${SANS}`, C.magenta)];
      out.push({ prio: 3, x: aid[0], y: aid[1], spots: beside(lines, aid[0], aid[1] + 14 * s, 4 * s, 4 * s, BUFFER.aid) });
    }

    // Speed limits in restricted areas.
    if (zoom >= 14) {
      for (const [types, speed, ...rings] of data.restrict || []) {
        if (speed == null || !/(^|\D)01(\D|$)/.test(String(types || ""))) continue;
        const size = px(14), font = `${size}px ${SANS}`;
        const text = `${Math.round(speed)} km/h`;
        ctx.font = font;
        const w = textWidth(ctx, text, 0);
        const p = labelPoint(rings, data.e, w + 4 * unit, size + 4 * unit);
        if (!p) continue;
        const lines = [line(text, size, 0, font, C.magenta)];
        out.push({
          prio: 4, x: p[0], y: p[1],
          spots: [{ box: [p[0] - w / 2 - unit, p[1] - 0.45 * size, p[0] + w / 2 + unit, p[1] + 0.45 * size], draw: () => textBlock(ctx, lines, p[0] - w / 2, p[1] - size / 2) }],
        });
      }
    }

    // Wreck and site names, from zoom 14.
    if (zoom >= 14 && options.heritage) {
      for (const [x, y, , name, , kind] of data.heritage || []) {
        if (!name) continue;
        const size = px(16);
        const lines = [line(name, size, 0, `italic ${size}px ${SANS}`, kind === 5 ? C.fort : C.magenta)];
        out.push({ prio: 5, x, y, spots: beside(lines, x, y, 12 * s, 12 * s, BUFFER.heritage, 10 * s) });
      }
    }
    // In the same order in every tile, so a label near an edge is placed the same in both.
    return out.sort((a, b) => a.prio - b.prio || a.x - b.x || a.y - b.y);
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
    // in scanned-chart pixels (`s`); symbol outlines thin out a little when zoomed out.
    const unit = e / 256;
    const zs = ZOOM_SCALE[zoom] ?? 1.2;
    const s = unit * CHART_SCALE * zs;
    const lw = unit * Math.min(1, zs);
    ctx.lineCap = "butt";
    ctx.lineJoin = "round";

    const aidAt = new Map((data.aid || []).map((aid) => [`${aid[0]},${aid[1]}`, aid]));
    // Labels claim their places first, so lines can be broken under the ones along them.
    const label = labeller();
    for (const cand of labelCandidates(ctx, data, zoom, s, unit, aidAt)) label.first(cand.spots);

    // 1. Land: the Finnish area, painted under the water.
    if (data.land) {
      ctx.beginPath();
      for (const ring of data.land) path(ctx, ring, true);
      ctx.fillStyle = C.land;
      ctx.fill("evenodd");
    }

    // 2. Water on its own canvas, then a crisp coastline: the water grown by a pixel,
    // painted black, with the water on top.
    if (data.depth?.length) {
      const size = canvas.width;
      const water = scratch(0, size);
      const w = water.getContext("2d");
      w.setTransform(k, 0, 0, k, 0, 0);
      for (const [band, ...rings] of data.depth) fillRings(w, rings, C.depth[band] ?? C.depth[4]);
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

    // 3. Main merchant fairways (class 1) in grey over the depths; anchorages outlined.
    const anchors = [];
    for (const row of data.farea || []) {
      const [code, draught, vl, ...rings] = row;
      if (Array.isArray(vl)) rings.unshift(vl); // a tile from before fairway classes
      const cls = Array.isArray(vl) ? null : vl;
      if (code === 1 || code === 11) {
        if (cls === 1 || (cls == null && draught >= 7.5)) {
          ctx.beginPath();
          for (const ring of rings) path(ctx, ring, true);
          ctx.fillStyle = C.fairwayArea;
          ctx.fill("evenodd");
        }
      } else if (code === 2) {
        strokeRings(ctx, rings, C.magenta, 1.2 * unit, [8 * unit, 5 * unit]);
        const p = labelPoint(rings, e, 14 * s, 24 * s);
        if (p) anchors.push(p);
      }
    }

    // 5. The small-craft trunk route's green band.
    for (const [line] of data.band || []) strokeLine(ctx, line, C.band, 18 * s);

    // 6. Contours: 3 m dotted, 6 m dashed, 10 m and deeper solid with a figure in a break.
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

    // 7. Restricted areas: magenta T-dashed limits.
    for (const [, , ...rings] of data.restrict || []) if (rings.length) restrictedLimit(ctx, rings, unit);

    // 8. Leading lines, dashed; the fairway line drawn over one makes it solid there.
    for (const row of data.lead || []) {
      let [bearing, line] = row;
      if (!line) { line = bearing; bearing = null; } // a tile from before bearings
      strokeLine(ctx, line, C.black, 1.1 * unit, [8 * unit, 5 * unit]);
      if (zoom >= 13 && bearing != null) {
        const size = Math.max(MIN_TEXT * unit, 25 * s);
        const b = (bearing * Math.PI) / 180;
        // Beside the line, to the right of a ship heading along it.
        alongLine(ctx, label, line, e, {
          text: bearingText(bearing), font: `${size}px ${SANS}`, size, color: C.red, need: 220 * unit, at: 0.35, alternate: coords,
          offset: [Math.cos(b) * 0.85 * size, Math.sin(b) * 0.85 * size],
        });
      }
    }

    // 9. Piers, quays and breakwaters.
    for (const [line] of data.shore || []) strokeLine(ctx, line, C.black, 2 * unit);

    // 10. Fairway lines, with their draught "(9,0)" set into the line once per tile;
    // boat routes dashed, their advisory draught in italics.
    const draughts = new Set();
    for (const row of data.fline || []) {
      let [draught, vl, line] = row;
      if (!line) { line = vl; vl = null; } // a tile from before fairway classes
      const boat = draught == null || vl >= 5;
      let gap = null;
      if (zoom >= 12 && draught != null) {
        const text = fmtDraught(draught);
        if (!draughts.has(text)) {
          const size = Math.max(MIN_TEXT * unit, 26 * s);
          ctx.font = `${boat ? "italic " : ""}${size}px ${SANS}`;
          const w = textWidth(ctx, text, 2 * s);
          gap = alongLine(ctx, label, line, e, {
            text, font: ctx.font, size, spacing: 2 * s, color: C.fairway, need: 3 * w, pad: -0.05 * size,
            alternate: zoom >= 14 ? coords : null,
          }) || null;
          if (gap) draughts.add(text);
        }
      }
      if (boat) strokeLine(ctx, line, C.fairway, (zoom >= 12 ? 1.6 : 1.2) * unit, [12 * unit, 6 * unit], gap);
      else strokeLine(ctx, line, C.fairway, (zoom >= 13 ? 2.2 : 1.6) * unit, null, gap);
    }

    // 11. Rocks, rock clusters, wrecks, heritage sites and anchorages.
    for (const [x, y, kind] of data.rock || []) drawRock(ctx, x, y, kind, s, false);
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
        strokeRings(ctx, rings, C.black, unit, [0.01 * unit, 3 * unit]);
        ctx.lineCap = "butt";
      }
      const c = ringCentroid(rings[0]) || [(x0 + x1) / 2, (y0 + y1) / 2];
      drawRock(ctx, c[0], c[1], rock, s, true);
    }
    for (const [x, y, kind] of data.wreck || []) drawWreck(ctx, x, y, kind, s, C.black);
    if (options.heritage) for (const site of data.heritage || []) drawHeritage(ctx, site, s, lw, unit);
    for (const [x, y] of anchors) anchor(ctx, x, y, s, lw);

    // 12. Light sectors up close: coloured arcs round the light, their limits dotted, a
    // colour letter outside each. A sector "from 200° to 238°" is seen from ships bearing
    // 200-238° towards the light, so it lies on the opposite side.
    const arcsAt = new Set();
    if (zoom >= 14 && data.sector?.length) {
      const r = 60 * s, width = 7 * s;
      const arcs = data.sector.map(([x, y, from, to, colour]) => {
        arcsAt.add(`${x},${y}`);
        const a1 = ((from + 90) * Math.PI) / 180;
        let a2 = ((to + 90) * Math.PI) / 180;
        if (a2 <= a1) a2 += 2 * Math.PI;
        return { x, y, a1, a2, letter: SECTOR[colour] || "W" };
      });
      ctx.lineCap = "round";
      ctx.setLineDash([0.01 * unit, 4 * unit]);
      ctx.beginPath();
      for (const { x, y, a1, a2 } of arcs) {
        for (const a of [a1, a2]) {
          ctx.moveTo(x + Math.cos(a) * 4 * s, y + Math.sin(a) * 4 * s);
          ctx.lineTo(x + Math.cos(a) * 2.2 * r, y + Math.sin(a) * 2.2 * r);
        }
      }
      ctx.strokeStyle = C.black;
      ctx.lineWidth = 1.3 * unit;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineCap = "butt";
      for (const pass of [0, 1]) {
        for (const { x, y, a1, a2, letter } of arcs) {
          const yellow = LIGHT[letter] === C.yellow;
          if (pass === 0 && !yellow) continue;
          ctx.beginPath();
          ctx.arc(x, y, r, a1, a2);
          ctx.strokeStyle = pass === 0 ? C.black : LIGHT[letter];
          ctx.lineWidth = pass === 0 ? width + 1.5 * unit : width;
          ctx.stroke();
        }
      }
      ctx.font = `${13 * s}px ${SANS}`;
      ctx.fillStyle = C.black;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const { x, y, a1, a2, letter } of arcs) {
        const a = (a1 + a2) / 2;
        ctx.fillText(letter, x + Math.cos(a) * (r + 10 * s), y + Math.sin(a) * (r + 10 * s));
      }
    }

    // 13. Navigation aids: every flare first, then the black symbols over them. Floating
    // marks before fixed ones, lights last.
    const order = { v: 0, p: 0, k: 1, l: 1, r: 2, o: 2, t: 2, a: 3, d: 3, s: 3, m: 4 };
    const aids = (data.aid || []).slice().sort((a, b) => (order[a[2]] ?? 2) - (order[b[2]] ?? 2));
    for (const aid of aids) if (aid[4] && !arcsAt.has(`${aid[0]},${aid[1]}`)) drawFlares(ctx, aid, s, lw);
    const marks = [];
    for (const aid of aids) {
      drawMark(ctx, aid, s, lw, zoom);
      marks.push(markBox(aid, s));
    }

    // 14. Soundings where no label or symbol is, then all the lettering.
    if (zoom >= 13) {
      // Shallowest first, as charts keep those; in the same order in every tile.
      const soundings = (data.sounding || []).slice().sort((a, b) => a[2] - b[2] || a[0] - b[0] || a[1] - b[1]);
      for (const [x, y, depth] of soundings) {
        const box = soundingBox(x, y, depth, s);
        if (!label.free(box) || marks.some((m) => box[0] < m[2] && box[2] > m[0] && box[1] < m[3] && box[3] > m[1])) continue;
        label.claim(box);
        sounding(ctx, x, y, depth, s);
      }
    }
    label.flush();
  }

  // --- the Leaflet layer -------------------------------------------------------------

  const tileAids = new Map(); // tile key -> [{ latlng, aid } or { latlng, site }]
  const RETRY_MS = 2500;
  let waitingTiles = 0; // tiles whose data is still downloading
  let waitingChanged = null;
  let rev = ""; // changes when the server's tiles do (e.g. rocks added), so cached tiles are dropped
  const layers = new Set();

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
              if (!waiting && !shown) {
                // Open water meanwhile, so the map underneath doesn't show its "no data" text.
                const ctx = canvas.getContext("2d");
                ctx.fillStyle = C.depth[4];
                ctx.fillRect(0, 0, canvas.width, canvas.height);
                show(null);
              }
              setWaiting(true);
              canvas._retry = setTimeout(load, RETRY_MS);
              return;
            }
            setWaiting(false);
            drawTile(canvas, data, coords, dpr);
            this._rememberTaps(coords, data);
            show(null);
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
      });
      this._ratioQuery?.removeEventListener?.("change", this._ratioChanged);
      this._ratioQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
      this._ratioQuery.addEventListener?.("change", this._ratioChanged);
    },

    _tileUnload(e) {
      clearTimeout(e.tile._retry);
      e.tile._abort?.abort();
      tileAids.delete(this._tileCoordsToKey(e.coords));
    },

    onRemove(map) {
      layers.delete(this);
      this._ratioQuery?.removeEventListener?.("change", this._ratioChanged);
      this._ratioQuery = null;
      L.GridLayer.prototype.onRemove.call(this, map);
    },

    onAdd(map) {
      layers.add(this);
      L.GridLayer.prototype.onAdd.call(this, map);
      this.off("tileunload", this._tileUnload, this);
      this.on("tileunload", this._tileUnload, this);
      this._watchPixelRatio();
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

  window.FerryChart = {
    layer: (opts) => new ChartLayer(opts),
    aidAt,
    colors: C,
    onWaiting: (fn) => { waitingChanged = fn; },
    setRev: (r) => {
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
