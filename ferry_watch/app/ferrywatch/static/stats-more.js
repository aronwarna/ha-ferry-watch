/* More in Stats: highlights (the wait since the last inner passage, the longest wait, the
   busiest day, the fastest passage), how often ships took the inner route in wind like
   now, each ship's times past Porkkala lately, and the year at a glance. */
window.FerryStatsMore = (() => {
  "use strict";

  const DAY = 86400;
  const INNER = "#f97316";
  const SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const MONTHS = Array.from({ length: 12 }, (_, m) => new Date(2000, m, 1).toLocaleString("en", { month: "short" }));
  const DIRS = [["east", "To Helsinki"], ["west", "To Stockholm"]];
  const TIMING_DAYS = 60; // the times strip covers this many days back
  const SIMILAR_MS = 3; // m/s either side of the wind now
  const SIMILAR_DEG = 35; // degrees either side
  const SIMILAR_MIN = 8; // passages needed before a share is given

  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
  const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const hhmm = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(Math.round(m) % 60).padStart(2, "0")}`;
  const dayName = (ts) => new Date(ts * 1000).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  const shortDate = (ts) => new Date(ts * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const midnight = (ts) => { const d = new Date(ts * 1000); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000; };
  const daysBetween = (a, b) => Math.round((midnight(b) - midnight(a)) / DAY);
  const shortName = (name) => name.split(" ").slice(-1)[0];
  const arrowTo = (dir) => (dir === "east" ? "→ Hel" : dir === "west" ? "← Sto" : "");
  const angleDiff = (a, b) => Math.abs(((a - b + 540) % 360) - 180);

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function median(values) {
    const v = [...values].sort((a, b) => a - b);
    const mid = v.length >> 1;
    return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
  }

  function section(title, note) {
    const s = el("section", "sm-section");
    s.append(el("h3", null, esc(title)));
    if (note) s.append(el("p", "small muted sm-note", note));
    return s;
  }

  function card(value, label, sub, extra) {
    const c = el("div", "sm-card");
    c.innerHTML = `<span class="sm-label">${label}</span><strong>${value}</strong>${sub ? `<span class="sm-sub">${sub}</span>` : ""}`;
    if (extra) c.append(extra);
    return c;
  }

  // --- highlights ---

  // Waits between inner passages, in whole days: [days, from ts, to ts or null while it lasts].
  function waits(rows, now) {
    const inner = rows.filter((p) => p.route === "inner").sort((a, b) => a.ts - b.ts);
    const out = [];
    let from = rows.length ? Math.min(...rows.map((p) => p.ts)) : now;
    for (const p of inner) {
      out.push([daysBetween(from, p.ts), from, p.ts]);
      from = p.ts;
    }
    out.push([daysBetween(from, now), from, null]);
    return { inner, out };
  }

  function highlights(rows, ships, now) {
    const s = section("Highlights");
    const grid = el("div", "sm-cards");
    const name = (mmsi) => ships.get(mmsi)?.name || `MMSI ${mmsi}`;
    const { inner, out } = waits(rows, now);
    const last = inner[inner.length - 1];
    if (last) {
      const d = daysBetween(last.ts, now);
      grid.append(card(d === 0 ? "Today" : d === 1 ? "1 day" : `${d} days`, "Since the last inner passage",
        `${esc(shortName(name(last.mmsi)))} ${arrowTo(last.direction)}, ${dayName(last.ts)}`));
    } else {
      grid.append(card("–", "Since the last inner passage", "None recorded yet"));
    }

    const longest = out.reduce((a, b) => (b[0] > a[0] ? b : a), [0, now, null]);
    const current = out[out.length - 1];
    if (longest[0] > 0) {
      const meter = el("div", "sm-meter");
      const share = Math.min(100, (100 * current[0]) / longest[0]);
      meter.innerHTML = `<i style="width:${share.toFixed(1)}%"></i>`;
      meter.title = `The wait now: ${current[0]} of ${longest[0]} days`;
      const sub = longest[2] == null ? `${shortDate(longest[1])} until now, the longest yet`
        : `${shortDate(longest[1])} to ${shortDate(longest[2])}${current[0] && longest[2] != null ? ` · now ${current[0]}` : ""}`;
      grid.append(card(`${longest[0]} days`, inner.length ? "Longest wait for one" : "Waiting since the first passage", sub, meter));
    }

    // The busiest day for the inner route, or (with only single ones) how many days had one.
    const byDay = new Map();
    for (const p of inner) {
      const k = midnight(p.ts);
      byDay.set(k, [...(byDay.get(k) || []), p]);
    }
    const busiest = [...byDay.values()].sort((a, b) => b.length - a.length || b[0].ts - a[0].ts)[0];
    if (busiest && busiest.length > 1) {
      const dots = busiest.map((p) => `<i class="dot" style="background:${esc(ships.get(p.mmsi)?.color || "#64748b")}"></i>`).join("");
      grid.append(card(`${busiest.length} inner`, "Busiest day", `${dayName(busiest[0].ts)} <span class="sm-dots">${dots}</span>`));
    } else {
      const total = new Set(rows.map((p) => midnight(p.ts))).size;
      grid.append(card(`${byDay.size} of ${total}`, "Days with an inner passage", total ? `${pct(byDay.size, total)}% of the days recorded` : ""));
    }

    const timed = rows.filter((p) => p.sog != null && p.sog > 3);
    if (timed.length) {
      const fastest = timed.reduce((a, b) => (b.sog > a.sog ? b : a));
      const usual = median(timed.map((p) => p.sog));
      grid.append(card(`${fastest.sog.toFixed(1)} kn`, "Fastest past Porkkala",
        `${esc(shortName(name(fastest.mmsi)))} ${arrowTo(fastest.direction)}, ${shortDate(fastest.ts)} · usual ${usual.toFixed(1)}`));
    } else {
      const alerts = rows.filter((p) => p.notified).length;
      grid.append(card(String(alerts), "Alerts sent", alerts ? `the last ${dayName(rows.filter((p) => p.notified).pop().ts)}` : "None yet"));
    }
    s.append(grid);
    return s;
  }

  // --- in wind like now ---

  function windNow(rows, wind) {
    if (!wind || wind.ms == null) return null;
    const known = rows.filter((p) => p.wind_ms != null);
    if (known.length < SIMILAR_MIN) return null;
    const like = known.filter((p) => Math.abs(p.wind_ms - wind.ms) <= SIMILAR_MS
      && (wind.dir == null || p.wind_dir == null ? wind.dir == null : angleDiff(p.wind_dir, wind.dir) <= SIMILAR_DEG));
    const from = wind.dir == null ? "" : `${SECTORS[Math.round(wind.dir / 45) % 8]} `;
    const s = section("In wind like now");
    const innerLike = like.filter((p) => p.route === "inner").length;
    const innerAll = known.filter((p) => p.route === "inner").length;
    const arrow = wind.dir == null ? "" : `<svg viewBox="-8 -8 16 16" class="sm-arrow" style="transform: rotate(${(wind.dir + 180) % 360}deg)" aria-hidden="true"><path d="M0 -6 L4 5 L0 2.5 L-4 5 Z"/></svg>`;
    const box = el("div", "sm-wind");
    const head = `<div class="sm-wind-now">${arrow}<span><strong>${from}${Math.round(wind.ms)} m/s</strong><span class="muted"> at Mäkiluoto now</span></span></div>`;
    if (like.length < SIMILAR_MIN) {
      box.innerHTML = `${head}<p class="small muted">Only ${like.length} passage${like.length === 1 ? "" : "s"} so far in wind like this `
        + `(${from}${Math.max(0, Math.round(wind.ms - SIMILAR_MS))}–${Math.round(wind.ms + SIMILAR_MS)} m/s): too few to say. `
        + `Over all ${known.length}, ${pct(innerAll, known.length)}% took the inner route.</p>`;
    } else {
      const share = pct(innerLike, like.length), base = pct(innerAll, known.length);
      const verdict = share >= base * 2 && share - base >= 5 ? "more often than usual"
        : share <= base / 2 && base - share >= 5 ? "less often than usual" : "about as often as usual";
      box.innerHTML = `${head}<div class="sm-odds"><strong>${share}%</strong><span>took the inner route: ${innerLike} of ${like.length} passages `
        + `in ${from}${Math.max(0, Math.round(wind.ms - SIMILAR_MS))}–${Math.round(wind.ms + SIMILAR_MS)} m/s, ${verdict}.</span></div>`
        + `<div class="sm-scale" role="img" aria-label="${share}% in wind like now, ${base}% over all"><i class="fill" style="width:${share}%"></i>`
        + `<i class="mark" style="left:${base}%" title="All passages: ${base}%"></i></div>`
        + `<p class="small muted">The mark is all ${known.length} passages: ${base}%. This is what happened before, not a forecast.</p>`;
    }
    s.append(box);
    return s;
  }

  // --- times past Porkkala ---

  function timing(rows, ships, now) {
    const recent = rows.filter((p) => p.ts >= now - TIMING_DAYS * DAY);
    const s = section("Times past Porkkala", `Each dot a passage in the last ${TIMING_DAYS} days, the inner ones ringed in orange. `
      + "The line is the usual time, the big dot the latest.");
    let any = false;
    for (const [dir, title] of DIRS) {
      const these = recent.filter((p) => p.direction === dir);
      const byShip = new Map();
      for (const p of these) byShip.set(p.mmsi, [...(byShip.get(p.mmsi) || []), p]);
      const lines = [...byShip.entries()].filter(([, list]) => list.length >= 3)
        .sort((a, b) => median(a[1].map((p) => p.minute)) - median(b[1].map((p) => p.minute)));
      if (!lines.length) continue;
      any = true;
      // The strip spans most of the times (a tenth either side may fall outside: pinned to
      // its ends) and every ship's usual one.
      const mids = lines.map(([, list]) => median(list.map((p) => p.minute)));
      const times = lines.flatMap(([, list]) => list.map((p) => p.minute)).sort((a, b) => a - b);
      const q = (f) => times[Math.min(times.length - 1, Math.floor(f * times.length))];
      const lo = Math.floor(Math.min(q(0.1) - 3, Math.min(...mids) - 10) / 10) * 10;
      const hi = Math.ceil(Math.max(q(0.9) + 3, Math.max(...mids) + 10) / 10) * 10;
      const x = (m) => (100 * (Math.min(hi, Math.max(lo, m)) - lo)) / (hi - lo); // % along the strip
      const block = el("div", "sm-timing");
      block.append(el("h4", null, title));
      for (const [mmsi, list] of lines) {
        const ship = ships.get(mmsi) || { name: `MMSI ${mmsi}`, color: "#64748b" };
        const sorted = [...list].sort((a, b) => a.ts - b.ts);
        const latest = sorted[sorted.length - 1];
        const usual = median(list.map((p) => p.minute));
        const late = Math.round(latest.minute - usual);
        const speeds = list.map((p) => p.sog).filter((v) => v != null && v > 3);
        let strip = "";
        for (let m = lo; m <= hi; m += 10) strip += `<i class="tick" style="left:${x(m).toFixed(2)}%"></i>`;
        strip += `<i class="usual" style="left:${x(usual).toFixed(2)}%"></i>`;
        for (const p of sorted) {
          const cls = `pt${p.route === "inner" ? " inner" : ""}${p === latest ? " latest" : ""}${p.minute < lo || p.minute > hi ? " edge" : ""}`;
          strip += `<i class="${cls}" style="left:${x(p.minute).toFixed(2)}%;background:${esc(ship.color)}" title="${p === latest ? "Latest: " : ""}${dayName(p.ts)} ${hhmm(p.minute)}${p.route === "inner" ? ", inner route" : ""}"></i>`;
        }
        const off = Math.abs(late), span = off >= 90 ? `${Math.floor(off / 60)} h ${off % 60}` : `${off} min`;
        const lateText = off <= 1 ? "on time" : late > 0 ? `+${span}` : `−${span}`;
        const lateTitle = off <= 1 ? "on time" : `${span} ${late > 0 ? "late" : "early"}`;
        const row = el("div", "sm-trow");
        row.innerHTML = `<span class="sm-tname"><span><i class="dot" style="background:${esc(ship.color)}"></i>${esc(shortName(ship.name))}</span>`
          + `${speeds.length ? `<span class="muted" title="Usual speed past Porkkala">${median(speeds).toFixed(1)} kn</span>` : ""}</span>`
          + `<span class="sm-strip" role="img" aria-label="${esc(ship.name)} ${title.toLowerCase()}: usually ${hhmm(usual)}">${strip}</span>`
          + `<span class="sm-tval" title="Usually ${hhmm(usual)}; the latest, ${dayName(latest.ts)}, was ${lateTitle}"><strong>${hhmm(usual)}</strong>`
          + `<span class="${off <= 1 ? "muted" : late > 0 ? "late" : "early"}">last ${lateText}</span></span>`;
        block.append(row);
      }
      const axis = el("div", "sm-axis");
      const step = hi - lo <= 40 ? 10 : hi - lo <= 90 ? 20 : hi - lo <= 180 ? 30 : 60;
      let labels = "";
      for (let m = Math.ceil(lo / step) * step; m <= hi; m += step) labels += `<span style="left:${x(m).toFixed(2)}%">${hhmm(m)}</span>`;
      axis.innerHTML = `<span></span><span class="sm-axis-labels">${labels}</span><span></span>`;
      block.append(axis);
      s.append(block);
    }
    return any ? s : null;
  }

  // --- the year at a glance ---

  function yearGrid(rows, year, now, firstTs, onDay) {
    const s = section(`${year} at a glance`);
    const start = new Date(year, 0, 1);
    const lead = (start.getDay() + 6) % 7; // Monday first
    const days = Math.round((new Date(year + 1, 0, 1) - start) / (DAY * 1000));
    const byDay = new Map();
    for (const p of rows) {
      const d = new Date(p.ts * 1000);
      if (d.getFullYear() !== year) continue;
      const k = Math.round((new Date(year, d.getMonth(), d.getDate()) - start) / (DAY * 1000));
      const v = byDay.get(k) || { inner: 0, all: 0 };
      v.all += 1;
      if (p.route === "inner") v.inner += 1;
      byDay.set(k, v);
    }
    const first = firstTs == null ? Infinity : midnight(firstTs);
    const C = 11, G = 2, top = 14, weeks = Math.ceil((lead + days) / 7);
    const W = weeks * (C + G), H = top + 7 * (C + G);
    let svg = `<svg viewBox="0 0 ${W} ${H}" class="sm-year" role="img" aria-label="Inner passages each day of ${year}">`;
    for (let m = 0; m < 12; m++) {
      const k = Math.round((new Date(year, m, 1) - start) / (DAY * 1000));
      svg += `<text x="${(Math.floor((lead + k) / 7) * (C + G)).toFixed(0)}" y="9">${MONTHS[m]}</text>`;
    }
    for (let k = 0; k < days; k++) {
      const ts = new Date(year, 0, 1 + k).getTime() / 1000;
      const col = Math.floor((lead + k) / 7), row = (lead + k) % 7;
      const v = byDay.get(k);
      const cls = ts > now ? "future" : ts < first ? "none" : !v ? "quiet" : v.inner ? `in${Math.min(3, v.inner)}` : "outer";
      const what = !v ? (ts > now || ts < first ? "nothing recorded" : "no passages") : `${v.inner} inner of ${v.all} passages`;
      svg += `<rect class="${cls}" x="${col * (C + G)}" y="${top + row * (C + G)}" width="${C}" height="${C}" rx="2.5" data-ts="${ts}"><title>${dayName(ts)}: ${what}</title></rect>`;
    }
    svg += "</svg>";
    const wrap = el("div", "sm-year-wrap", svg);
    wrap.addEventListener("click", (e) => {
      const r = e.target.closest("rect");
      if (r && !r.classList.contains("future") && !r.classList.contains("none")) onDay(Number(r.dataset.ts));
    });
    const key = el("div", "sm-key small muted");
    key.innerHTML = '<span><i class="none"></i>Not recorded</span><span><i class="outer"></i>Outer route only</span>'
      + '<span><i class="in1"></i><i class="in2"></i><i class="in3"></i>1, 2, 3+ inner</span>';
    s.append(wrap, key, el("p", "small muted sm-note", "Tap a day for its passages."));
    // On a narrow screen the year scrolls sideways: start with the latest weeks in view.
    requestAnimationFrame(() => {
      const today = wrap.querySelector(`rect[data-ts="${midnight(now)}"]`);
      wrap.scrollLeft = today ? today.getBoundingClientRect().left - wrap.getBoundingClientRect().left - wrap.clientWidth * 0.75 : wrap.scrollWidth;
    });
    return s;
  }

  /* rows: the passages Stats counts (its ship and direction choices, every year), as
     { ts, mmsi, route, direction, wind_ms, wind_dir, sog, notified, minute }.
     ctx: { ships: [{ mmsi, name, color }], year, wind: { ms, dir } | null, firstTs, onDay(ts) } */
  function render(box, rows, ctx) {
    if (!rows.length) { box.replaceChildren(); return; }
    const now = Date.now() / 1000;
    const ships = new Map(ctx.ships.map((s) => [s.mmsi, s]));
    box.replaceChildren(...[
      highlights(rows, ships, now),
      windNow(rows, ctx.wind),
      timing(rows, ships, now),
      yearGrid(rows, ctx.year, now, ctx.firstTs, ctx.onDay),
    ].filter(Boolean));
  }

  return { render, waits };
})();
