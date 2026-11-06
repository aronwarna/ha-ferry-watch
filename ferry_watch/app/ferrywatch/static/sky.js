/* The night sky: a star map for a place and time, what's worth looking at tonight, and a
   small note on the map when something is.

   Everything is worked out here, in the browser: the stars and constellations from
   sky-data.js, the planets from their orbits (JPL's approximate Keplerian elements, good to
   about an arcminute for 1800-2050), the Moon from the Astronomical Almanac's low-precision
   series (a few tenths of a degree), the Sun, and the year's main meteor showers. From the
   add-on only the clouds over the place (the weather model) and the Kp forecast (NOAA),
   when it has them.

   The map is the whole sky as seen lying on your back: the horizon round the edge, the
   point overhead in the middle, the way you face at the bottom. A slider and play button
   step through the night. The astronomy is kept apart from the drawing so the tests can
   run it under Node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FerrySky = api;
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  const RAD = Math.PI / 180;
  const sin = (d) => Math.sin(d * RAD), cos = (d) => Math.cos(d * RAD);
  const norm = (d) => ((d % 360) + 360) % 360;

  // --- time and coordinates -----------------------------------------------------------

  const jd = (ts) => ts / 86400 + 2440587.5;
  const centuries = (ts) => (jd(ts) - 2451545) / 36525;
  const gmst = (ts) => norm(280.46061837 + 360.98564736629 * (jd(ts) - 2451545));
  const OBLIQUITY = 23.43928;

  function eclToEq(lon, lat) {
    const e = OBLIQUITY;
    const ra = Math.atan2(sin(lon) * cos(e) - Math.tan(lat * RAD) * sin(e), cos(lon)) / RAD;
    const dec = Math.asin(sin(lat) * cos(e) + cos(lat) * sin(e) * sin(lon)) / RAD;
    return [norm(ra), dec];
  }

  // Right ascension and declination as altitude and azimuth (from north, clockwise).
  function horizontal(ra, dec, lat, lon, ts) {
    const ha = norm(gmst(ts) + lon - ra);
    const alt = Math.asin(sin(lat) * sin(dec) + cos(lat) * cos(dec) * cos(ha)) / RAD;
    const az = Math.atan2(-sin(ha) * cos(dec), cos(lat) * sin(dec) - sin(lat) * cos(dec) * cos(ha)) / RAD;
    return [alt, norm(az)];
  }

  function separation(ra1, dec1, ra2, dec2) {
    const c = sin(dec1) * sin(dec2) + cos(dec1) * cos(dec2) * cos(ra1 - ra2);
    return Math.acos(Math.max(-1, Math.min(1, c))) / RAD;
  }

  // --- the Sun and the Moon -----------------------------------------------------------

  function sun(ts) {
    const d = jd(ts) - 2451545;
    const g = norm(357.529 + 0.98560028 * d);
    const lon = norm(280.459 + 0.98564736 * d + 1.915 * sin(g) + 0.02 * sin(2 * g));
    const [ra, dec] = eclToEq(lon, 0);
    return { ra, dec, lon };
  }

  function moon(ts) {
    const T = centuries(ts);
    const lon = norm(218.32 + 481267.881 * T + 6.29 * sin(135.0 + 477198.87 * T) - 1.27 * sin(259.3 - 413335.36 * T)
      + 0.66 * sin(235.7 + 890534.22 * T) + 0.21 * sin(269.9 + 954397.74 * T) - 0.19 * sin(357.5 + 35999.05 * T)
      - 0.11 * sin(186.5 + 966404.03 * T));
    const lat = 5.13 * sin(93.3 + 483202.02 * T) + 0.28 * sin(228.2 + 960400.89 * T) - 0.28 * sin(318.3 + 6003.15 * T)
      - 0.17 * sin(217.6 - 407332.21 * T);
    const parallax = 0.9508 + 0.0518 * cos(135.0 + 477198.87 * T) + 0.0095 * cos(259.3 - 413335.36 * T)
      + 0.0078 * cos(235.7 + 890534.22 * T) + 0.0028 * cos(269.9 + 954397.74 * T);
    const [ra, dec] = eclToEq(lon, lat);
    const s = sun(ts);
    const elong = separation(ra, dec, s.ra, s.dec);
    const lit = (1 - cos(elong)) / 2;
    const waxing = norm(lon - s.lon) < 180;
    return { ra, dec, lon, parallax, lit, waxing, elong };
  }

  function phaseName(m) {
    const p = m.lit;
    if (p < 0.03) return "new Moon";
    if (p > 0.97) return "full Moon";
    if (Math.abs(p - 0.5) < 0.06) return m.waxing ? "first quarter" : "last quarter";
    return `${m.waxing ? "waxing" : "waning"} ${p < 0.5 ? "crescent" : "gibbous"}`;
  }

  // --- the planets --------------------------------------------------------------------
  // a (AU), e, I, L, long. perihelion, long. ascending node, each with its rate per century.

  const ELEMENTS = {
    Mercury: [0.38709927, 0.00000037, 0.20563593, 0.00001906, 7.00497902, -0.00594749, 252.2503235, 149472.67411175, 77.45779628, 0.16047689, 48.33076593, -0.12534081],
    Venus: [0.72333566, 0.0000039, 0.00677672, -0.00004107, 3.39467605, -0.0007889, 181.9790995, 58517.81538729, 131.60246718, 0.00268329, 76.67984255, -0.27769418],
    Earth: [1.00000261, 0.00000562, 0.01671123, -0.00004392, -0.00001531, -0.01294668, 100.46457166, 35999.37244981, 102.93768193, 0.32327364, 0, 0],
    Mars: [1.52371034, 0.00001847, 0.0933941, 0.00007882, 1.84969142, -0.00813131, -4.55343205, 19140.30268499, -23.94362959, 0.44441088, 49.55953891, -0.29257343],
    Jupiter: [5.202887, -0.00011607, 0.04838624, -0.00013253, 1.30439695, -0.00183714, 34.39644051, 3034.74612775, 14.72847983, 0.21252668, 100.47390909, 0.20469106],
    Saturn: [9.53667594, -0.0012506, 0.05386179, -0.00050991, 2.48599187, 0.00193609, 49.95424423, 1222.49362201, 92.59887831, -0.41897216, 113.66242448, -0.28867794],
    Uranus: [19.18916464, -0.00196176, 0.04725744, -0.00004397, 0.77263783, -0.00242939, 313.23810451, 428.48202785, 170.9542763, 0.40805281, 74.01692503, 0.04240589],
    Neptune: [30.06992276, 0.00026291, 0.00859048, 0.00005105, 1.77004347, 0.00035372, -55.12002969, 218.45945325, 44.96476227, -0.32241464, 131.78422574, -0.00508664],
  };
  // Magnitude at 1 AU and how it grows with the phase angle i (deg): H + a·i + b·i² + c·i³
  // (the Astronomical Almanac's older formulas; Saturn without its rings).
  const BRIGHTNESS = { Mercury: [-0.42, 0.038, -0.000273, 0.000002], Venus: [-4.4, 0.0009, 0.000239, -0.00000065],
    Mars: [-1.52, 0.016, 0, 0], Jupiter: [-9.4, 0.005, 0, 0], Saturn: [-8.88, 0.044, 0, 0], Uranus: [-7.19, 0.002, 0, 0],
    Neptune: [-6.87, 0, 0, 0] };
  const PLANET_COLOURS = { Mercury: "#cbd5e1", Venus: "#fef9c3", Mars: "#f87171", Jupiter: "#fde68a", Saturn: "#fcd34d",
    Uranus: "#99f6e4", Neptune: "#93c5fd" };

  function heliocentric(name, T) {
    const el = ELEMENTS[name];
    const a = el[0] + el[1] * T, e = el[2] + el[3] * T, I = el[4] + el[5] * T;
    const L = el[6] + el[7] * T, peri = el[8] + el[9] * T, node = el[10] + el[11] * T;
    const w = peri - node;
    let M = norm(L - peri);
    if (M > 180) M -= 360;
    let E = M + (e / RAD) * sin(M);
    for (let i = 0; i < 8; i++) {
      const dM = M - (E - (e / RAD) * sin(E));
      E += dM / (1 - e * cos(E));
    }
    const xp = a * (cos(E) - e), yp = a * Math.sqrt(1 - e * e) * sin(E);
    const x = (cos(w) * cos(node) - sin(w) * sin(node) * cos(I)) * xp + (-sin(w) * cos(node) - cos(w) * sin(node) * cos(I)) * yp;
    const y = (cos(w) * sin(node) + sin(w) * cos(node) * cos(I)) * xp + (-sin(w) * sin(node) + cos(w) * cos(node) * cos(I)) * yp;
    const z = sin(w) * sin(I) * xp + cos(w) * sin(I) * yp;
    return [x, y, z];
  }

  function planet(name, ts) {
    const T = centuries(ts);
    const [px, py, pz] = heliocentric(name, T), [ex, ey, ez] = heliocentric("Earth", T);
    const x = px - ex, y = py - ey, z = pz - ez;
    const dist = Math.hypot(x, y, z), r = Math.hypot(px, py, pz), R = Math.hypot(ex, ey, ez);
    const lon = norm(Math.atan2(y, x) / RAD), lat = Math.asin(z / dist) / RAD;
    const [ra, dec] = eclToEq(lon, lat);
    const phase = Math.acos(Math.max(-1, Math.min(1, (r * r + dist * dist - R * R) / (2 * r * dist)))) / RAD;
    const [H, a, b, c] = BRIGHTNESS[name];
    const mag = H + 5 * Math.log10(r * dist) + a * phase + b * phase ** 2 + c * phase ** 3;
    const s = sun(ts);
    return { name, ra, dec, mag, dist, elong: separation(ra, dec, s.ra, s.dec) };
  }

  const PLANETS = ["Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"];

  // --- meteor showers -----------------------------------------------------------------
  // name, peak (month, day), active from/to (month, day), radiant RA/Dec, ZHR at peak.
  const SHOWERS = [
    ["Quadrantids", [1, 3], [12, 28], [1, 12], 230, 49, 110],
    ["Lyrids", [4, 22], [4, 14], [4, 30], 271, 34, 18],
    ["Eta Aquariids", [5, 6], [4, 19], [5, 28], 338, -1, 50],
    ["Perseids", [8, 12], [7, 17], [8, 24], 48, 58, 100],
    ["Draconids", [10, 8], [10, 6], [10, 10], 262, 54, 10],
    ["Orionids", [10, 21], [10, 2], [11, 7], 95, 16, 20],
    ["Leonids", [11, 17], [11, 6], [11, 30], 152, 22, 15],
    ["Geminids", [12, 14], [12, 4], [12, 20], 112, 33, 150],
    ["Ursids", [12, 22], [12, 17], [12, 26], 217, 76, 10],
  ];

  function showersOn(ts) {
    const d = new Date(ts * 1000);
    const y = d.getFullYear();
    const day = (m, dd, yy = y) => Date.UTC(yy, m - 1, dd) / 1000;
    const out = [];
    for (const [name, peak, from, to, ra, dec, zhr] of SHOWERS) {
      for (const yy of [y - 1, y, y + 1]) {
        const start = day(...from, from[0] > to[0] ? yy - 1 : yy), end = day(...to, yy) + 86400;
        const p = day(...peak, peak[0] < from[0] ? yy : (from[0] > to[0] && peak[0] >= from[0] ? yy - 1 : yy));
        if (ts >= start && ts <= end) out.push({ name, ra, dec, zhr, peak: p, daysToPeak: (p - ts) / 86400 });
      }
    }
    return out;
  }

  // --- the night ----------------------------------------------------------------------

  const altOf = (body, lat, lon, ts) => {
    const [alt] = horizontal(body.ra, body.dec, lat, lon, ts);
    return body.parallax ? alt - body.parallax * cos(alt) : alt;
  };

  // The night that `ts` belongs to: from sunset (sun below -0.8) to sunrise, with the
  // darkest stretch (sun below -12, nautical) inside it; for the summer's light nights,
  // where the sun doesn't go that low, the darkest hour instead.
  function night(lat, lon, ts) {
    const STEP = 300;
    const sunAlt = (t) => altOf(sun(t), lat, lon, t);
    // Noon before: the start of the search.
    let t0 = ts - 12 * 3600;
    while (sunAlt(t0) < sunAlt(t0 + STEP) && t0 < ts + 12 * 3600) t0 += STEP; // climb to the day's highest
    let start = null, end = null, darkFrom = null, darkTo = null, lowest = t0, lowAlt = 99;
    for (let t = t0; t < t0 + 26 * 3600; t += STEP) {
      const a = sunAlt(t);
      if (a < lowAlt) { lowAlt = a; lowest = t; }
      if (start == null && a < -0.8) start = t;
      if (start != null && end == null && a > -0.8 && t > start + 3600) end = t;
      if (a < -12 && darkFrom == null) darkFrom = t;
      if (darkFrom != null && darkTo == null && a > -12 && t > darkFrom) darkTo = t;
    }
    if (start == null) start = lowest - 2 * 3600; // midnight sun
    if (end == null) end = lowest + 2 * 3600;
    return { start, end, darkFrom, darkTo, lowest, lowAlt };
  }

  // Everything above the horizon at a time: [{ kind, name, alt, az, mag, ... }].
  function bodies(lat, lon, ts) {
    const out = [];
    for (const name of PLANETS) {
      const p = planet(name, ts);
      const [alt, az] = horizontal(p.ra, p.dec, lat, lon, ts);
      out.push({ kind: "planet", ...p, alt, az, colour: PLANET_COLOURS[name] });
    }
    const m = moon(ts);
    const [malt, maz] = horizontal(m.ra, m.dec, lat, lon, ts);
    out.push({ kind: "moon", name: "Moon", ...m, alt: malt - m.parallax * cos(malt), az: maz, mag: -12.7 * m.lit });
    const s = sun(ts);
    const [salt, saz] = horizontal(s.ra, s.dec, lat, lon, ts);
    out.push({ kind: "sun", name: "Sun", ...s, alt: salt, az: saz, mag: -26.7 });
    return out;
  }

  const compass = (deg) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(norm(deg) / 45) % 8];
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

  // Bright stars that make good pairs with the Moon and planets.
  const BRIGHT = [["Aldebaran", 68.98, 16.51], ["Regulus", 152.09, 11.97], ["Spica", 201.3, -11.16], ["Antares", 247.35, -26.43],
    ["Pollux", 116.33, 28.03], ["Pleiades", 56.75, 24.12], ["Beehive cluster", 130.1, 19.67]];

  // What's worth seeing in the night of `ts` at a place: [{ score, icon, text, at }], best
  // first. score: 3 a rare treat, 2 worth stepping out for, 1 nice to know.
  function tonight(lat, lon, ts, { kp = [], clouds = [] } = {}) {
    const n = night(lat, lon, ts);
    const from = n.darkFrom ?? n.start, to = n.darkTo ?? n.end;
    const out = [];
    const samples = [];
    for (let t = from; t <= to; t += 1800) samples.push(t);
    if (!samples.length) samples.push(n.lowest);

    // Planets: when and where each is best (highest while dark).
    const seen = {};
    for (const name of PLANETS) {
      let best = null;
      for (const t of samples) {
        const p = planet(name, t);
        const [alt, az] = horizontal(p.ra, p.dec, lat, lon, t);
        if (alt > 8 && (!best || alt > best.alt)) best = { t, alt, az, mag: p.mag };
      }
      if (best && best.mag < 2.5) {
        seen[name] = best;
        const bright = best.mag < -1.5;
        out.push({ score: bright ? 1.2 : 0.8, icon: "planet", at: best.t,
          text: `${name} (mag ${best.mag.toFixed(1)}) best about ${clock(best.t)}, ${Math.round(best.alt)}° up in the ${compass(best.az)}` });
      }
    }
    // Close pairs: Moon and planets with each other and with bright stars, under 4 degrees.
    const things = (t) => {
      const list = PLANETS.filter((p) => seen[p] && planet(p, t).mag < 1.8).map((p) => ({ name: p, ...planet(p, t) }));
      const m = moon(t);
      list.push({ name: "the Moon", ra: m.ra, dec: m.dec, moon: true });
      return list;
    };
    const pairs = new Map();
    for (const t of samples) {
      const list = things(t);
      for (let i = 0; i < list.length; i++) {
        const a = list[i];
        const [alt] = horizontal(a.ra, a.dec, lat, lon, t);
        if (alt < 8) continue;
        for (let j = i + 1; j < list.length; j++) {
          const b = list[j];
          const sep = separation(a.ra, a.dec, b.ra, b.dec);
          const key = `${a.name}|${b.name}`;
          if (sep < 4 && (!pairs.has(key) || sep - alt / 200 < pairs.get(key).rank)) pairs.set(key, { a, b, sep, t, alt, rank: sep - alt / 200 });
        }
        if (!a.moon && a.name !== "the Moon") continue;
        for (const [name, ra, dec] of BRIGHT) {
          const sep = separation(a.ra, a.dec, ra, dec);
          const key = `${a.name}|${name}`;
          if (sep < 3 && (!pairs.has(key) || sep - alt / 200 < pairs.get(key).rank)) pairs.set(key, { a, b: { name }, sep, t, alt, rank: sep - alt / 200 });
        }
      }
    }
    for (const { a, b, sep, t, alt } of pairs.values()) {
      const names = a.name === "the Moon" ? `The Moon and ${b.name}` : `${a.name} and ${b.name === "the Moon" ? "the Moon" : b.name}`;
      const [, az] = horizontal(a.ra, a.dec, lat, lon, t);
      out.push({ score: sep < 1.5 ? 2.6 : 2, icon: "pair", at: t,
        text: `${names} ${sep < 1 ? "under a degree" : `${sep.toFixed(1)}°`} apart, best about ${clock(t)} in the ${compass(az)} (${Math.round(alt)}° up)` });
    }
    // The Moon.
    const mid = samples[Math.floor(samples.length / 2)];
    const m = moon(mid);
    const moonUp = samples.filter((t) => altOf(moon(t), lat, lon, t) > 0).length / samples.length;
    out.push({ score: m.lit > 0.97 || m.lit < 0.03 ? 1 : 0.3, icon: "moon", at: mid,
      text: `${phaseName(m)[0].toUpperCase()}${phaseName(m).slice(1)}, ${Math.round(m.lit * 100)}% lit${moonUp > 0.6 ? ", up most of the night" : moonUp < 0.1 ? ", down all night: a dark sky" : ""}` });
    // Meteor showers.
    for (const sh of showersOn(mid)) {
      const near = Math.abs(sh.daysToPeak);
      if (near > 3 && sh.zhr < 50) continue;
      const [alt] = horizontal(sh.ra, sh.dec, lat, lon, to - 3600);
      const moonly = m.lit > 0.6 && moonUp > 0.5 ? ", though the Moon washes out the faint ones" : "";
      out.push({ score: near <= 1.5 ? (sh.zhr >= 50 ? 2.8 : 1.6) : 0.9, icon: "meteor", at: to - 3600,
        text: `${sh.name}${near <= 1.5 ? " at their peak" : sh.daysToPeak > 0 ? `, peak in ${Math.round(sh.daysToPeak)} days` : ", past their peak"}: up to ${sh.zhr} an hour under a dark sky, best after midnight (radiant ${Math.round(alt)}° up)${moonly}` });
    }
    // Northern lights from the Kp forecast.
    const nightKp = kp.filter(([t]) => t + 3 * 3600 > from && t < to).map(([, k]) => k);
    if (nightKp.length) {
      const max = Math.max(...nightKp);
      const need = Math.max(1, (66 - lat) / 2 + 2); // roughly the Kp to see it low in the north from here: ~5 at Helsinki
      if (max >= need - 1) {
        out.push({ score: max >= need + 1 ? 2.8 : max >= need ? 2.2 : 1.1, icon: "aurora", at: from,
          text: `Northern lights ${max >= need ? "likely" : "possible"}: Kp up to ${max.toFixed(1)} tonight; look north once it's dark` });
      }
    }
    // Darkness.
    out.push({ score: 0.2, icon: "dark", at: from,
      text: n.darkFrom ? `Dark from ${clock(n.darkFrom)} to ${clock(n.darkTo ?? n.end)} (sun 12° down)` : `No real darkness tonight: the sun only goes ${Math.round(-n.lowAlt)}° down` });
    // Clouds over the night, from the weather model.
    const cl = clouds.filter(([t]) => t >= from - 1800 && t <= to + 1800);
    let sky = null;
    if (cl.length) {
      const avg = cl.reduce((s, c) => s + (c[1] ?? 0), 0) / cl.length;
      const best = cl.reduce((b, c) => ((c[1] ?? 100) < (b[1] ?? 100) ? c : b), cl[0]);
      sky = { avg, best };
      out.push({ score: 0.25, icon: "cloud", at: best[0],
        text: avg < 25 ? `Mostly clear (${Math.round(avg)}% cloud on average)` : avg > 80 ? `Cloudy (${Math.round(avg)}%); clearest about ${clock(best[0])} (${Math.round(best[1])}%)`
          : `${Math.round(avg)}% cloud on average; clearest about ${clock(best[0])} (${Math.round(best[1])}%)` });
    }
    out.sort((a, b) => b.score - a.score);
    return { night: n, items: out, sky };
  }

  // The one line for the note on the map, or null when nothing tonight is worth it (or
  // the clouds hide it all).
  function highlight(result) {
    const top = result.items.find((i) => i.score >= 2);
    if (!top) return null;
    if (result.sky && result.sky.avg > 85 && result.sky.best[1] > 70) return null;
    const cloud = result.sky ? (result.sky.avg < 30 ? " Clear skies forecast." : ` Clouds ${Math.round(result.sky.avg)}%.`) : "";
    return { ...top, text: `Tonight: ${top.text}.${cloud}` };
  }

  // --- the map ------------------------------------------------------------------------

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  function starColour(bv) {
    // B-V to a star's colour: blue-white to orange.
    const t = Math.max(-0.3, Math.min(1.8, bv));
    if (t < 0) return "#cfe0ff";
    if (t < 0.3) return "#eef3ff";
    if (t < 0.6) return "#fffbe8";
    if (t < 1.0) return "#ffe9c2";
    if (t < 1.4) return "#ffd29a";
    return "#ffb877";
  }

  const ICONS = { planet: "🪐", pair: "✨", moon: "🌙", meteor: "☄️", aurora: "🌌", dark: "🌑", cloud: "☁️" };

  /* opts: { where() -> { lat, lon, label } } (the phone, else the map's centre),
     esc(text) */
  function create({ where, esc, mapCentre }) {
    const data = typeof FerrySkyData !== "undefined" ? FerrySkyData : { stars: [], names: {}, lines: [], constellations: [] };
    let place = null, t = Date.now() / 1000, facing = Number(store.get("fw-sky-facing") || 180), playing = 0;
    let extra = { kp: [], clouds: [] }, result = null, hits = [];

    const box = document.createElement("section");
    box.className = "sky-view";
    box.hidden = true;
    box.setAttribute("aria-label", "Night sky");
    box.innerHTML = `
      <div class="sky-head">
        <div><h2>Night sky</h2><span class="sky-where small"></span></div>
        <button type="button" class="icon-button plain sky-close" aria-label="Close"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </div>
      <div class="sky-body">
        <div class="sky-dome"><canvas></canvas><div class="sky-caption small"></div></div>
        <div class="sky-side">
          <div class="sky-controls">
            <div class="segmented sky-facing" role="group" aria-label="Facing">
              ${["N", "E", "S", "W"].map((d, i) => `<button type="button" data-az="${i * 90}">${d}</button>`).join("")}
            </div>
            <div class="segmented sky-place" role="group" aria-label="Where">
              <button type="button" data-place="phone">Phone</button><button type="button" data-place="map">Map centre</button>
            </div>
          </div>
          <div class="player sky-player">
            <button type="button" class="player-btn player-toggle" aria-label="Play"><svg viewBox="0 0 24 24" aria-hidden="true"><path class="fill" d="M8 5.5v13l10.5-6.5z"/></svg></button>
            <div class="player-main">
              <div class="player-time"><strong class="player-clock"></strong><span class="player-info"></span><span class="sky-days"><button type="button" class="sky-day" data-day="-1" aria-label="Night before">‹</button><button type="button" class="sky-now small">Tonight</button><button type="button" class="sky-day" data-day="1" aria-label="Night after">›</button></span></div>
              <input class="player-range" type="range" min="0" max="100" step="1" value="0" aria-label="Time">
            </div>
          </div>
          <h3 class="wx-heading sky-heading">Tonight</h3>
          <ul class="sky-list"></ul>
          <label class="ov-option small muted">Show a note on the map when something's worth seeing<input type="checkbox" class="switch sky-tips"></label>
          <p class="small muted sky-credit">Stars: HYG database via d3-celestial. Planets: JPL's approximate orbits. Clouds: the weather model; Kp: NOAA.</p>
        </div>
      </div>`;
    document.body.append(box);
    const canvas = box.querySelector("canvas");
    const caption = box.querySelector(".sky-caption");
    const range = box.querySelector(".player-range");
    const toggle = box.querySelector(".player-toggle");
    const PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path class="fill" d="M8 5.5v13l10.5-6.5z"/></svg>';
    const PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path class="fill" d="M7 5.5h3.5v13H7zM13.5 5.5H17v13h-3.5z"/></svg>';
    const tips = box.querySelector(".sky-tips");
    tips.checked = store.get("fw-sky-tips") !== "0";
    tips.addEventListener("change", () => store.set("fw-sky-tips", tips.checked ? "1" : "0"));

    let placeMode = store.get("fw-sky-place") || "phone";
    function pickPlace() {
      const w = placeMode === "map" ? mapCentre() : where();
      place = w;
      box.querySelector(".sky-where").textContent = w.label;
      box.querySelectorAll("[data-place]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.place === placeMode)));
    }

    // The night's span on the slider.
    let span = null;
    function setNight() {
      const n = night(place.lat, place.lon, t);
      span = { from: n.start - 1800, to: n.end + 1800 };
      if (t < span.from || t > span.to) t = Math.max(span.from, Math.min(span.to, t));
    }

    async function loadExtra() {
      try {
        const res = await fetch(`api/sky?lat=${place.lat.toFixed(3)}&lon=${place.lon.toFixed(3)}&clouds=1`);
        if (res.ok) extra = await res.json();
      } catch { /* the sky works without */ }
    }

    function project(alt, az, cx, cy, R) {
      const r = ((90 - alt) / 90) * R;
      const a = (az - facing) * RAD;
      return [cx + r * Math.sin(a), cy + r * Math.cos(a)];
    }

    function draw() {
      if (box.hidden || !place) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const size = canvas.parentElement.clientWidth;
      canvas.width = canvas.height = Math.round(size * dpr);
      canvas.style.width = canvas.style.height = `${size}px`;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const cx = size / 2, cy = size / 2, R = size / 2 - 22;
      const font = (px, style = "") => `${style} ${Math.round(px * Math.min(1.5, Math.max(1, size / 480)))}px system-ui, sans-serif`;
      const { lat, lon } = place;
      const sunAlt = altOf(sun(t), lat, lon, t);
      // The sky's colour by how far down the sun is.
      const k = Math.max(0, Math.min(1, (sunAlt + 18) / 18));
      const top = [Math.round(6 + 30 * k), Math.round(10 + 60 * k), Math.round(28 + 110 * k)];
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
      g.addColorStop(0, `rgb(${top})`);
      g.addColorStop(1, `rgb(${top.map((v, i) => Math.round(v + [20, 25, 30][i] * k + 8))})`);
      ctx.fillStyle = "#05070d";
      ctx.fillRect(0, 0, size, size);
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.clip();
      // Altitude rings.
      ctx.strokeStyle = "rgba(148,163,184,0.18)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 4]);
      for (const a of [30, 60]) { ctx.beginPath(); ctx.arc(cx, cy, ((90 - a) / 90) * R, 0, Math.PI * 2); ctx.stroke(); }
      ctx.setLineDash([]);
      const starAlpha = Math.max(0.15, 1 - k * 1.1);
      // Constellation lines.
      ctx.strokeStyle = `rgba(125,160,220,${(0.35 * starAlpha).toFixed(2)})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (const line of data.lines) {
        let prev = null;
        for (let i = 0; i < line.length; i += 2) {
          const [alt, az] = horizontal(line[i], line[i + 1], lat, lon, t);
          const p = alt > -5 ? project(alt, az, cx, cy, R) : null;
          if (p && prev) { ctx.moveTo(prev[0], prev[1]); ctx.lineTo(p[0], p[1]); }
          prev = p;
        }
      }
      ctx.stroke();
      hits = [];
      // Stars.
      const s = data.stars;
      for (let i = 0; i < s.length; i += 4) {
        const [alt, az] = horizontal(s[i], s[i + 1], lat, lon, t);
        if (alt < 0) continue;
        const mag = s[i + 2];
        const [x, y] = project(alt, az, cx, cy, R);
        const r = Math.max(0.5, 2.9 - 0.5 * mag) * (size / 420 + 0.4);
        ctx.globalAlpha = Math.max(0.2, Math.min(1, (5.6 - mag) / 3.5)) * starAlpha;
        ctx.fillStyle = starColour(s[i + 3]);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
        const name = data.names[i / 4];
        if (name) hits.push({ x, y, text: `${name} · mag ${mag.toFixed(1)} · ${Math.round(alt)}° up in the ${compass(az)}` });
        if (name && mag < 1.6 && starAlpha > 0.4) {
          ctx.globalAlpha = 0.6 * starAlpha;
          ctx.fillStyle = "#cbd5e1";
          ctx.font = font(10);
          ctx.fillText(name, x + 4, y - 3);
        }
      }
      ctx.globalAlpha = 1;
      // Constellation names (the big ones).
      ctx.font = font(10, "italic");
      ctx.fillStyle = `rgba(148,180,230,${(0.45 * starAlpha).toFixed(2)})`;
      ctx.textAlign = "center";
      for (const [name, ra, dec, rank] of data.constellations) {
        if (rank > 1) continue;
        const [alt, az] = horizontal(ra, dec, lat, lon, t);
        if (alt < 5) continue;
        const [x, y] = project(alt, az, cx, cy, R);
        ctx.fillText(name.toUpperCase(), x, y);
      }
      // Meteor radiants.
      for (const sh of showersOn(t)) {
        const [alt, az] = horizontal(sh.ra, sh.dec, lat, lon, t);
        if (alt < 0) continue;
        const [x, y] = project(alt, az, cx, cy, R);
        ctx.strokeStyle = "rgba(251,191,36,0.7)";
        ctx.lineWidth = 1.2;
        for (let a = 0; a < 360; a += 45) {
          ctx.beginPath();
          ctx.moveTo(x + 4 * cos(a), y + 4 * sin(a));
          ctx.lineTo(x + 11 * cos(a), y + 11 * sin(a));
          ctx.stroke();
        }
        ctx.fillStyle = "rgba(251,191,36,0.85)";
        ctx.font = font(10);
        ctx.fillText(sh.name, x, y + 21);
        hits.push({ x, y, text: `${sh.name} radiant · ${Math.round(alt)}° up · up to ${sh.zhr} an hour at the peak` });
      }
      // Planets, the Moon and the Sun.
      ctx.textAlign = "left";
      const all = bodies(lat, lon, t);
      const sunAt = all.find((b) => b.kind === "sun");
      const [sx, sy] = project(sunAt.alt, sunAt.az, cx, cy, R); // off the dome when it's down: still the way to it
      for (const b of all) {
        if (b.alt < -1) continue;
        const [x, y] = project(b.alt, b.az, cx, cy, R);
        if (b.kind === "moon") {
          drawMoon(ctx, x, y, 9 * Math.min(1.5, Math.max(1, size / 480)), b, Math.atan2(sy - y, sx - x));
          hits.push({ x, y, text: `Moon · ${phaseName(b)}, ${Math.round(b.lit * 100)}% lit · ${Math.round(b.alt)}° up in the ${compass(b.az)}` });
          continue;
        }
        if (b.kind === "sun") {
          ctx.fillStyle = "#fde047";
          ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.fill();
          continue;
        }
        if (b.mag > 6) continue;
        const r = Math.max(2.2, 4.5 - 0.6 * b.mag);
        ctx.fillStyle = b.colour;
        ctx.shadowColor = b.colour;
        ctx.shadowBlur = 8;
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
        ctx.font = font(11, "600");
        ctx.fillText(b.name, x + r + 3, y + 4);
        hits.push({ x, y, text: `${b.name} · mag ${b.mag.toFixed(1)} · ${Math.round(b.alt)}° up in the ${compass(b.az)}` });
      }
      ctx.restore();
      // The horizon: directions round the edge.
      ctx.strokeStyle = "rgba(148,163,184,0.5)";
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = "#e2e8f0";
      ctx.font = font(12, "700");
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const [d, az] of [["N", 0], ["NE", 45], ["E", 90], ["SE", 135], ["S", 180], ["SW", 225], ["W", 270], ["NW", 315]]) {
        const a = (az - facing) * RAD;
        const x = cx + (R + 12) * Math.sin(a), y = cy + (R + 12) * Math.cos(a);
        ctx.globalAlpha = d.length > 1 ? 0.55 : 1;
        ctx.fillText(d, x, y);
      }
      ctx.globalAlpha = 1;
      ctx.textBaseline = "alphabetic";
      // The time.
      const lbl = new Date(t * 1000);
      box.querySelector(".player-clock").textContent = `${lbl.toLocaleDateString("en-GB", { weekday: "short" })} ${clock(t)}`;
      box.querySelector(".player-info").textContent = sunAlt > -0.8 ? "sun up" : sunAlt > -6 ? "twilight" : sunAlt > -12 ? "getting dark" : "dark";
      range.value = String(Math.round(((t - span.from) / (span.to - span.from)) * 100));
      range.style.setProperty("--p", `${range.value}%`);
    }

    // The Moon with its lit side towards the sun (`toSun`, radians on the screen).
    function drawMoon(ctx, x, y, r, m, toSun) {
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(toSun);
      ctx.fillStyle = "#334155";
      ctx.beginPath(); ctx.arc(0, 0, r, 0, Math.PI * 2); ctx.fill();
      // The lit part: the half disc facing the sun, and an ellipse for the terminator.
      const k = 1 - 2 * m.lit; // 1 new, -1 full
      ctx.fillStyle = "#f8fafc";
      ctx.beginPath();
      ctx.arc(0, 0, r, -Math.PI / 2, Math.PI / 2, false);
      ctx.ellipse(0, 0, Math.abs(k) * r, r, 0, Math.PI / 2, -Math.PI / 2, k > 0);
      ctx.fill();
      ctx.restore();
    }

    function renderList() {
      result = tonight(place.lat, place.lon, t, extra);
      const today = night(place.lat, place.lon, Date.now() / 1000).end === result.night.end;
      box.querySelector(".sky-heading").textContent = today ? "Tonight"
        : `The night of ${new Date(result.night.start * 1000).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}`;
      box.querySelector(".sky-list").innerHTML = result.items.map((i) => `<li class="${i.score >= 2 ? "strong" : ""}" data-at="${i.at}"><span>${ICONS[i.icon] || "•"}</span><span>${esc(i.text)}</span></li>`).join("");
    }

    async function open() {
      box.hidden = false;
      document.body.classList.add("sky-open");
      pickPlace();
      t = Date.now() / 1000;
      setNight();
      draw();
      renderList();
      await loadExtra();
      renderList();
    }

    function close() {
      pause();
      box.hidden = true;
      document.body.classList.remove("sky-open");
    }

    function pause() {
      clearInterval(playing);
      playing = 0;
      toggle.innerHTML = PLAY;
      toggle.setAttribute("aria-label", "Play");
    }

    function play() {
      if (t >= span.to - 600) t = span.from;
      toggle.innerHTML = PAUSE;
      toggle.setAttribute("aria-label", "Pause");
      playing = setInterval(() => {
        t += 300;
        if (t > span.to) { t = span.to; pause(); }
        draw();
      }, 60);
    }

    box.querySelector(".sky-close").addEventListener("click", close);
    toggle.addEventListener("click", () => (playing ? pause() : play()));
    range.addEventListener("input", () => { pause(); t = span.from + (Number(range.value) / 100) * (span.to - span.from); draw(); });
    box.querySelector(".sky-now").addEventListener("click", () => { pause(); t = Date.now() / 1000; setNight(); draw(); renderList(); });
    box.querySelectorAll(".sky-day").forEach((b) => b.addEventListener("click", () => {
      pause();
      t += Number(b.dataset.day) * 86400;
      setNight();
      draw();
      renderList();
    }));
    box.querySelectorAll("[data-az]").forEach((b) => {
      b.setAttribute("aria-pressed", String(Number(b.dataset.az) === facing));
      b.addEventListener("click", () => {
        facing = Number(b.dataset.az);
        store.set("fw-sky-facing", String(facing));
        box.querySelectorAll("[data-az]").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
        draw();
      });
    });
    box.querySelectorAll("[data-place]").forEach((b) => b.addEventListener("click", async () => {
      placeMode = b.dataset.place;
      store.set("fw-sky-place", placeMode);
      pickPlace();
      setNight();
      draw();
      renderList();
      await loadExtra();
      renderList();
    }));
    box.querySelector(".sky-list").addEventListener("click", (e) => {
      const li = e.target.closest("li[data-at]");
      if (!li) return;
      pause();
      t = Number(li.dataset.at);
      draw();
    });
    canvas.addEventListener("click", (e) => {
      const r = canvas.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      let best = null, bd = 22 * 22;
      for (const h of hits) {
        const d = (h.x - x) ** 2 + (h.y - y) ** 2;
        if (d < bd) { bd = d; best = h; }
      }
      caption.textContent = best ? best.text : "";
    });
    window.addEventListener("resize", () => draw());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !box.hidden) close(); });

    // The note on the map: once a night, when something's worth seeing and the sky isn't
    // all cloud. Closing it hides it until the next night.
    const toast = document.createElement("div");
    toast.className = "sky-note panel";
    toast.hidden = true;
    toast.setAttribute("role", "status");
    toast.innerHTML = `<span class="sky-note-icon" aria-hidden="true"></span><span class="sky-note-text"></span>
      <button type="button" class="sky-note-open">Sky map</button>
      <button type="button" class="icon-button plain sky-note-close" aria-label="Hide until tomorrow"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>`;
    document.body.append(toast);
    let noteKey = null;
    const hideNote = () => { toast.hidden = true; document.body.classList.remove("sky-note-on"); };
    toast.querySelector(".sky-note-close").addEventListener("click", () => { if (noteKey) store.set(noteKey, "1"); hideNote(); });
    toast.querySelector(".sky-note-open").addEventListener("click", () => { if (noteKey) store.set(noteKey, "1"); hideNote(); open(); });

    async function note() {
      if (store.get("fw-sky-tips") === "0" || !toast.hidden) return;
      const w = where();
      const now = Date.now() / 1000;
      const n = night(w.lat, w.lon, now);
      if (now > (n.darkTo ?? n.end) - 1800 || now < n.start - 6 * 3600) return; // only from the afternoon to the night's end
      noteKey = `fw-sky-note-${new Date(n.end * 1000).toDateString()}`;
      if (store.get(noteKey)) return;
      // The clouds and the Kp come from the NAS (one small call); without them, the sky alone.
      let r = tonight(w.lat, w.lon, now, {});
      try {
        const res = await fetch(`api/sky?lat=${w.lat.toFixed(3)}&lon=${w.lon.toFixed(3)}&clouds=1`);
        if (res.ok) r = tonight(w.lat, w.lon, now, await res.json());
      } catch { /* without clouds, then */ }
      const h = highlight(r);
      if (!h) return;
      toast.querySelector(".sky-note-icon").textContent = ICONS[h.icon] || "✨";
      toast.querySelector(".sky-note-text").textContent = h.text;
      toast.hidden = false;
      document.body.classList.add("sky-note-on");
    }

    return { open, close, note, get isOpen() { return !box.hidden; } };
  }

  return { jd, gmst, sun, moon, planet, horizontal, separation, eclToEq, night, bodies, tonight, highlight, showersOn,
    phaseName, compass, create };
});
