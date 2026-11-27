/* Passage weather in Stats: a compact row for each passage (wind, water, ice), the
   passages of a day tapped in the calendar, and the "Weather" breakdown of inner against
   outer route by wind strength, wind direction and, in winter, ice. */
window.FerryWeather = (() => {
  "use strict";

  const SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const BANDS = [[0, 5, "0–5"], [5, 10, "5–10"], [10, 15, "10–15"], [15, Infinity, "15+"]];
  const INNER = "#f97316", OUTER = "#8b5cf6";
  const sector = (deg) => Math.round(((deg % 360) + 360) % 360 / 45) % 8;
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  // "↗ 7 m/s (12) · 4 °C · Ice 28 cm": the arrow points the way the wind blows.
  function row(p) {
    const parts = [];
    if (p.wind_ms != null) {
      const dir = p.wind_dir != null ? `<svg viewBox="-8 -8 16 16" style="transform: rotate(${(p.wind_dir + 180) % 360}deg)" aria-hidden="true"><path d="M0 -6 L4 5 L0 2.5 L-4 5 Z"/></svg>` : "";
      const from = p.wind_dir != null ? `${SECTORS[sector(p.wind_dir)]} ` : "";
      const gust = p.gust_ms != null && p.gust_ms >= p.wind_ms + 1 ? `<span class="muted">(${Math.round(p.gust_ms)})</span>` : "";
      parts.push(`<span class="wx-wind" title="Wind ${from}${p.wind_ms.toFixed(1)} m/s at Mäkiluoto">${dir}${from}${Math.round(p.wind_ms)} m/s ${gust}</span>`);
    }
    if (p.water_c != null) {
      parts.push(`<span class="wx-water" title="Sea temperature"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5C6 5 3.5 7.5 3.5 10a4.5 4.5 0 0 0 9 0C12.5 7.5 10 5 8 1.5z"/></svg>${p.water_c.toFixed(p.water_c < 10 ? 1 : 0)} °C</span>`);
    }
    if (p.ice === "ice") {
      const bits = [p.ice_cm != null ? `${Math.round(p.ice_cm)} cm` : null, p.ice_conc != null ? `${Math.round(p.ice_conc)}%` : null].filter(Boolean);
      parts.push(`<span class="wx-ice ice" title="Sea ice near Porkkala">Ice${bits.length ? ` ${bits.join(" · ")}` : ""}</span>`);
    } else if (p.ice === "open") {
      parts.push('<span class="wx-ice open" title="Sea ice near Porkkala">No ice</span>');
    }
    if (!parts.length) return null;
    return el("span", "wx-row", parts.join(""));
  }

  // --- the day tapped in the calendar ---

  let dayToken = 0;
  async function dayList(box, since, until, ctx) {
    const token = ++dayToken;
    box.hidden = false;
    box.replaceChildren(el("p", "small muted", "Loading the day's passages…"));
    let rows;
    try {
      rows = await ctx.api(`api/passages?since=${since}&until=${until}&limit=200`);
    } catch (err) {
      if (token === dayToken) box.replaceChildren(el("p", "small muted", "Couldn't load the passages."));
      return;
    }
    if (token !== dayToken) return;
    rows.sort((a, b) => a.ts - b.ts);
    const list = el("ul", "ships-list passages day-passages");
    list.setAttribute("aria-label", "Passages on this day");
    for (const p of rows) {
      const li = el("li", "clickable");
      li.title = "Show this passage on the map";
      const dot = el("span", "dot");
      dot.style.background = p.color;
      const grow = el("div", "grow");
      const top = el("span", "day-top");
      top.innerHTML = `<strong>${ctx.clock(p.ts)}</strong> ${ctx.esc(p.name)}`;
      const sub = el("span", "small muted");
      sub.textContent = `${p.route === "inner" ? "Inner route" : "Outer route"}, ${ctx.DIRECTIONS[p.direction] || p.direction}${p.sog ? ` · ${p.sog.toFixed(0)} kn` : ""}`;
      grow.append(top, sub);
      const wx = row(p);
      if (wx) grow.append(wx);
      li.append(dot, grow);
      li.addEventListener("click", () => ctx.open(p));
      list.append(li);
    }
    if (!rows.length) list.append(el("li", "small muted", "No passages recorded on this day."));
    box.replaceChildren(list);
  }

  function hideDay(box) {
    dayToken += 1;
    box.hidden = true;
    box.replaceChildren();
  }

  // --- the breakdown ---

  // One bar: the inner share in orange, the outer in violet; or, with no outer passages
  // recorded at all, the number of inner passages against the biggest group.
  function bar(label, inner, total, max, compare) {
    const line = el("div", "wx-band");
    const width = compare ? 100 * (total / max) : 100 * (inner / max);
    const innerW = compare ? width * (inner / Math.max(1, total)) : width;
    line.innerHTML = `<span class="wx-label">${label}</span>
      <span class="wx-bar" role="img" aria-label="${compare ? `${inner} of ${total} on the inner route` : `${inner} inner passages`}">
        <i style="width: ${innerW.toFixed(1)}%; background: ${INNER}"></i>${compare ? `<i style="width: ${(width - innerW).toFixed(1)}%; background: ${OUTER}"></i>` : ""}
      </span>
      <span class="wx-val">${total ? (compare ? `<strong>${pct(inner, total)}%</strong> <span class="muted">${inner}/${total}</span>` : `<strong>${inner}</strong>`) : '<span class="muted">–</span>'}</span>`;
    return line;
  }

  function rose(rows, compare, caption) {
    const R = 66, C = 80;
    const by = SECTORS.map(() => ({ inner: 0, total: 0 }));
    for (const r of rows) {
      if (r.wind_dir == null) continue;
      const s = by[sector(r.wind_dir)];
      s.total += 1;
      if (r.route === "inner") s.inner += 1;
    }
    const max = Math.max(1, ...by.map((s) => (compare ? s.total : s.inner)));
    const wedge = (i, r) => {
      if (r <= 0) return "";
      const a0 = ((i * 45 - 20) * Math.PI) / 180, a1 = ((i * 45 + 20) * Math.PI) / 180;
      const p = (a, rr) => `${(C + rr * Math.sin(a)).toFixed(1)} ${(C - rr * Math.cos(a)).toFixed(1)}`;
      return `M${C} ${C} L${p(a0, r)} A${r.toFixed(1)} ${r.toFixed(1)} 0 0 1 ${p(a1, r)} Z`;
    };
    let svg = `<svg viewBox="0 0 160 160" class="wx-rose" role="img" aria-label="Passages by wind direction">`;
    for (const r of [R / 3, (2 * R) / 3, R]) svg += `<circle class="ring" cx="${C}" cy="${C}" r="${r.toFixed(1)}"/>`;
    by.forEach((s, i) => {
      const n = compare ? s.total : s.inner;
      if (!n) return;
      // Areas, not radii, follow the counts.
      const r = R * Math.sqrt(n / max);
      const rIn = compare ? r * Math.sqrt(s.inner / s.total) : r;
      svg += `<g class="sector" data-i="${i}"><path d="${wedge(i, r)}" fill="${compare ? OUTER : INNER}"/>`
        + `${compare && rIn ? `<path d="${wedge(i, rIn)}" fill="${INNER}"/>` : ""}</g>`;
    });
    SECTORS.forEach((name, i) => {
      if (i % 2) return;
      const a = (i * 45 * Math.PI) / 180;
      svg += `<text x="${(C + (R + 9) * Math.sin(a)).toFixed(1)}" y="${(C - (R + 9) * Math.cos(a) + 3.5).toFixed(1)}">${name}</text>`;
    });
    svg += "</svg>";
    const box = el("div", "wx-rose-box", svg);
    const text = (i) => {
      const s = by[i];
      if (!s.total) return `No passages recorded with ${SECTORS[i]} wind.`;
      return compare
        ? `Wind from ${SECTORS[i]}: ${s.inner} of ${s.total} on the inner route (${pct(s.inner, s.total)}%).`
        : `Wind from ${SECTORS[i]}: ${s.inner} inner passages.`;
    };
    const best = by.map((s, i) => [i, s]).filter(([, s]) => s.total >= 3)
      .sort((a, b) => b[1].inner / b[1].total - a[1].inner / a[1].total)[0];
    caption.textContent = best && compare ? `${text(best[0])} Tap a direction.` : "Tap a direction for its numbers.";
    box.addEventListener("click", (e) => {
      const g = e.target.closest(".sector");
      if (!g) return;
      box.querySelectorAll(".sector").forEach((x) => x.classList.toggle("dim", x !== g));
      caption.textContent = text(Number(g.dataset.i));
    });
    return box;
  }

  /* rows: passages in the scope Stats shows, as { route, wind_ms, wind_dir, ice }. */
  function breakdown(box, rows, scopeName) {
    const wind = rows.filter((r) => r.wind_ms != null);
    box.hidden = !rows.length;
    if (!rows.length) { box.replaceChildren(); return; }
    const compare = rows.some((r) => r.route === "outer");
    const head = el("div", "wx-head");
    head.innerHTML = `<h3>Weather</h3><span class="wx-key small"><i style="background: ${INNER}"></i>Inner${compare ? `<i style="background: ${OUTER}"></i>Outer` : ""}</span>`;
    const intro = el("p", "small muted");
    intro.textContent = compare
      ? `How often ships took the inner route${scopeName ? ` (${scopeName})` : ""}, by the wind at Mäkiluoto as they passed.`
      : "No outer-route passages are recorded, so these are inner passages by the wind at Mäkiluoto.";
    const parts = [head, intro];
    if (!wind.length) {
      parts.push(el("p", "small muted", "No wind was recorded with these passages yet. It's stored with each passage from now on."));
      box.replaceChildren(...parts);
      return;
    }
    const groups = BANDS.map(([lo, hi, label]) => {
      const inBand = wind.filter((r) => r.wind_ms >= lo && r.wind_ms < hi);
      return { label: `${label} m/s`, inner: inBand.filter((r) => r.route === "inner").length, total: inBand.length };
    });
    const max = Math.max(1, ...groups.map((x) => (compare ? x.total : x.inner)));
    const bands = el("div", "wx-bands");
    bands.append(el("h4", null, "Wind strength"), ...groups.map((x) => bar(x.label, x.inner, x.total, max, compare)));

    const caption = el("p", "small muted wx-caption");
    const dirs = el("div", "wx-dirs");
    dirs.append(el("h4", null, "Wind direction"), rose(wind, compare, caption), caption);

    const grid = el("div", "wx-grid");
    grid.append(bands, dirs);
    const iced = rows.filter((r) => r.ice);
    if (iced.length) {
      const ice = el("div", "wx-bands wx-ice-bands");
      const g = [["Ice", "ice"], ["Open water", "open"]].map(([label, state]) => {
        const these = iced.filter((r) => r.ice === state);
        return { label, inner: these.filter((r) => r.route === "inner").length, total: these.length };
      });
      const m = Math.max(1, ...g.map((x) => (compare ? x.total : x.inner)));
      ice.append(el("h4", null, "Sea ice (December to April)"), ...g.map((x) => bar(x.label, x.inner, x.total, m, compare)));
      bands.append(ice);
    }
    parts.push(grid);
    box.replaceChildren(...parts);
  }

  return { row, dayList, hideDay, breakdown };
})();
