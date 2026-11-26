/* Recent satellite pictures: the "Recent" map type (recentsat.py).

   One day's Sentinel-2 picture (10 m) from the last two months over today's satellite
   view, which is greyed out so it's plain where that day's picture stops. The bar shows
   the day big (and how long ago), with the pass time, how cloudy it was and how much of
   the view it covers; under it a line of the last two months with a mark for each day
   that has a picture here (tall and blue for clear days, short and pale for cloudy ones).
   ‹ › and the line step through the days; "Clear only" skips the cloudy ones. Moving
   the map asks for the days there again and, unless a day was picked by hand, shows
   the newest clear one. app.js makes one with FerryRecent.create. */
(() => {
  "use strict";

  const PANE = "recentPane";
  const BASE_PANE = "recentBasePane";
  const SHOW_Z = 8;  // further out there'd be hundreds of tiles per day; the bar says zoom in

  const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const DAY_MS = 86400e3;
  const dayMs = (d) => Date.parse(`${d}T12:00:00Z`);

  function longDate(d) {
    return new Date(dayMs(d)).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  }
  function ago(d, today) {
    const n = Math.round((dayMs(today) - dayMs(d)) / DAY_MS);
    return n <= 0 ? "today" : n === 1 ? "yesterday" : `${n} days ago`;
  }
  // The pass time (UTC in the catalogue) in the viewer's own time.
  function localTime(d, hhmm) {
    const t = new Date(`${d}T${hhmm}:00Z`);
    return Number.isNaN(t.getTime()) ? "" : t.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  function cloudWord(c) {
    return c <= 5 ? "clear" : c <= 30 ? `${c}% cloud` : c <= 70 ? `cloudy (${c}%)` : `overcast (${c}%)`;
  }

  function create({ map, store, base, labels }) {
    let active = false;
    let days = [];             // newest first, from the NAS
    let info = null;
    let current = null;        // the day shown (a row of days)
    let picked = "";           // a day picked by hand: kept while it has picture here
    let clearOnly = store.get("fw-recent-clear") !== "0";
    let opacity = Number(store.get("fw-recent-opacity") || 100);
    let seq = 0;
    let timer = 0;

    map.createPane(PANE).style.zIndex = 250;
    map.createPane(BASE_PANE).style.zIndex = 200;
    map.getPane(BASE_PANE).classList.add("recent-base");

    const group = L.layerGroup();
    const baseLayer = base(BASE_PANE);
    group.addLayer(baseLayer);
    if (labels) group.addLayer(labels());
    let dayLayer = null;
    let dayKey = "";

    group.on("add", () => { active = true; bar.hidden = false; render(); load(); });
    group.on("remove", () => { active = false; bar.hidden = true; });
    map.on("moveend", () => {
      if (!active) return;
      clearTimeout(timer);
      timer = setTimeout(load, 350);
    });
    map.on("zoomend", () => { if (active) render(); });

    // --- the bar --------------------------------------------------------------------
    const bar = document.createElement("div");
    bar.id = "recent-bar";
    bar.className = "histbar recentbar panel";
    bar.hidden = true;
    bar.innerHTML = `
      <div class="hb-top">
        <span class="rb-name small"><strong>Sentinel-2</strong> <span class="muted">· 10 m · last 2 months</span></span>
        <button type="button" class="hb-split rb-clear" aria-pressed="false" title="Skip the cloudy days">
          <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><circle cx="10" cy="10" r="3.4" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          <span>Clear only</span></button>
      </div>
      <div class="hb-main">
        <div class="hb-head">
          <button type="button" class="tb-step hb-prev" aria-label="Earlier day">‹</button>
          <div class="hb-title">
            <strong class="hb-year rb-date">–</strong>
            <span class="hb-what small muted rb-what"></span>
          </div>
          <button type="button" class="tb-step hb-next" aria-label="Later day">›</button>
        </div>
        <div class="hb-line rb-line" role="slider" tabindex="0" aria-label="Day">
          <div class="hb-track"></div>
          <div class="hb-marks"></div>
          <span class="hb-knob" aria-hidden="true"></span>
        </div>
        <div class="hb-scale small muted"></div>
      </div>
      <div class="hb-opts small">
        <span class="rb-note muted"></span>
        <label class="hb-op" title="See today's satellite view through it"><span class="muted">Fade</span><input type="range" class="player-range rb-opacity" min="15" max="100" step="5" aria-label="Opacity"></label>
      </div>
      <div class="hb-empty small" hidden></div>
      <p class="hb-credit small muted"></p>`;
    document.body.append(bar);
    const $b = (sel) => bar.querySelector(sel);
    L.DomEvent.disableClickPropagation(bar);
    L.DomEvent.disableScrollPropagation(bar);

    $b(".rb-clear").setAttribute("aria-pressed", String(clearOnly));
    $b(".rb-clear").addEventListener("click", () => {
      clearOnly = !clearOnly;
      store.set("fw-recent-clear", clearOnly ? "1" : "0");
      $b(".rb-clear").setAttribute("aria-pressed", String(clearOnly));
      if (clearOnly && current && !current.clear) {
        picked = "";
        current = choose();
      }
      render();
    });
    $b(".rb-opacity").value = String(opacity);
    $b(".rb-opacity").addEventListener("input", (e) => {
      opacity = Number(e.target.value);
      store.set("fw-recent-opacity", String(opacity));
      dayLayer?.setOpacity(opacity / 100);
    });

    // The days one can step through: all, or the clear ones (and the one shown).
    const steps = () => [...days].reverse().filter((d) => !clearOnly || d.clear || d === current);
    function step(dir) {
      const list = steps();
      const i = list.indexOf(current);
      const next = list[i + dir];
      if (next) pick(next);
    }
    $b(".hb-prev").addEventListener("click", () => step(-1));
    $b(".hb-next").addEventListener("click", () => step(1));

    function pick(d) {
      if (!d || d === current) return;
      current = d;
      picked = d.date;
      render();
    }

    // --- the line: the last two months ------------------------------------------------
    const line = $b(".rb-line");
    let span = [0, 1];
    const pos = (d) => ((dayMs(d) - span[0]) / Math.max(1, span[1] - span[0])) * 100;
    function nearestAt(clientX) {
      const r = line.getBoundingClientRect();
      const t = span[0] + ((clientX - r.left) / Math.max(1, r.width)) * (span[1] - span[0]);
      let best = null;
      let bestD = Infinity;
      for (const d of steps()) {
        const dist = Math.abs(dayMs(d.date) - t);
        if (dist < bestD) { bestD = dist; best = d; }
      }
      return best;
    }
    let drag = false;
    line.addEventListener("pointerdown", (e) => {
      if (!days.length) return;
      drag = true;
      line.setPointerCapture(e.pointerId);
      pick(nearestAt(e.clientX));
      e.preventDefault();
    });
    line.addEventListener("pointermove", (e) => { if (drag) pick(nearestAt(e.clientX)); });
    line.addEventListener("pointerup", () => { drag = false; });
    line.addEventListener("pointercancel", () => { drag = false; });
    line.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowDown") { step(-1); e.preventDefault(); }
      if (e.key === "ArrowRight" || e.key === "ArrowUp") { step(1); e.preventDefault(); }
    });

    function renderLine() {
      const today = info?.today || new Date().toISOString().slice(0, 10);
      span = [dayMs(today) - (info?.back || 60) * DAY_MS, dayMs(today)];
      $b(".hb-marks").innerHTML = days.map((d, i) => {
        const cls = `hb-mark rb-mark${d.clear ? " clear" : ""}${clearOnly && !d.clear ? " skip" : ""}${d === current ? " on" : ""}`;
        // Height: how much of the view is clear (cover × sky), so the good days stand tall.
        const good = Math.max(0.15, d.cover * (1 - d.cloud / 100));
        return `<span class="${cls}" data-i="${i}" style="left:${pos(d.date)}%;--good:${good.toFixed(2)}" title="${esc(`${longDate(d.date)}: ${cloudWord(d.cloud)}, covers ${Math.round(d.cover * 100)}%`)}"></span>`;
      }).join("");
      // Month starts under the line.
      const ticks = [];
      const t = new Date(span[0]);
      t.setUTCDate(1);
      for (t.setUTCMonth(t.getUTCMonth() + 1); t.getTime() <= span[1]; t.setUTCMonth(t.getUTCMonth() + 1)) {
        ticks.push([t.getTime(), t.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" })]);
      }
      $b(".hb-scale").innerHTML = ticks.map(([ms, label]) =>
        `<span style="left:${((ms - span[0]) / (span[1] - span[0])) * 100}%">${label}</span>`).join("")
        + `<span style="left:100%;transform:translateX(-100%)">Today</span>`;
      const knob = $b(".hb-knob");
      knob.hidden = !current;
      if (current) {
        knob.style.left = `${pos(current.date)}%`;
        line.setAttribute("aria-valuetext", longDate(current.date));
      }
    }

    // --- the picture ----------------------------------------------------------------------
    function showDay() {
      const key = current && map.getZoom() >= SHOW_Z ? current.date : "";
      if (key === dayKey) return;
      dayKey = key;
      if (dayLayer) group.removeLayer(dayLayer);
      dayLayer = null;
      if (!key) return;
      dayLayer = L.tileLayer(`api/recent/${key}/{z}/{x}/{y}.webp`, {
        pane: PANE, minZoom: SHOW_Z, minNativeZoom: info?.min_zoom || 9, maxNativeZoom: info?.max_zoom || 13,
        maxZoom: 19, opacity: opacity / 100, attribution: info?.attribution || "Contains modified Copernicus Sentinel data",
      });
      group.addLayer(dayLayer);
    }

    function choose() {
      if (picked) {
        const d = days.find((x) => x.date === picked);
        if (d) return d;
      }
      const best = days.find((d) => d.date === info?.best);
      return best || days[0] || null;
    }

    function render() {
      if (!active) return;
      const zoom = map.getZoom();
      const empty = $b(".hb-empty");
      const today = info?.today || new Date().toISOString().slice(0, 10);
      bar.classList.toggle("is-empty", false);
      empty.hidden = true;
      if (zoom < SHOW_Z) {
        bar.classList.add("is-empty");
        empty.hidden = false;
        empty.innerHTML = `<p class="hb-empty-title">Zoom in to see recent pictures</p><p class="muted">Sentinel-2's pictures show from a county's width in (zoom ${SHOW_Z}).</p>`;
      } else if (info && !days.length) {
        bar.classList.add("is-empty");
        empty.hidden = false;
        empty.innerHTML = info.error
          ? `<p class="hb-empty-title">Couldn't reach the satellite catalogue</p><p class="muted">${esc(info.error)}. Trying again when you move the map.</p>`
          : `<p class="hb-empty-title">No pictures here in the last two months</p><p class="muted">Sentinel-2 covers land and coastal waters; far out at sea it takes none.</p>`;
      }
      const d = current;
      $b(".rb-date").textContent = d ? longDate(d.date) : (info ? "–" : "Looking…");
      $b(".rb-what").textContent = d
        ? [ago(d.date, today), d.time && `${localTime(d.date, d.time)}`, cloudWord(d.cloud),
          d.cover < 0.98 && `covers ${Math.round(d.cover * 100)}% of the view`].filter(Boolean).join(" · ")
        : "";
      const list = steps();
      const i = list.indexOf(current);
      $b(".hb-prev").disabled = i <= 0;
      $b(".hb-next").disabled = i < 0 || i >= list.length - 1;
      const nClear = days.filter((x) => x.clear).length;
      $b(".rb-note").textContent = days.length
        ? `${days.length} day${days.length === 1 ? "" : "s"} with a picture here, ${nClear} clear`
        : "";
      $b(".hb-credit").textContent = zoom >= 15 ? "10 m pixels: zoom out a little for a crisp view" : (info?.attribution || "");
      renderLine();
      showDay();
    }

    async function load() {
      if (!active) return;
      if (map.getZoom() < SHOW_Z) { render(); return; }
      const b = map.getBounds();
      const box = [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((v) => v.toFixed(4)).join(",");
      const mine = ++seq;
      try {
        const res = await fetch(`api/recent/days?box=${box}`);
        if (!res.ok) throw new Error(await res.text() || res.statusText);
        const data = await res.json();
        if (mine !== seq) return;
        info = data;
        days = data.days || [];
      } catch (err) {
        if (mine !== seq) return;
        info = { error: String(err.message || err) };
        days = [];
      }
      // Keep showing the same day (the same object for the new list) when it is still here.
      const same = current && days.find((x) => x.date === current.date);
      current = picked ? choose() : (same && same.clear ? same : choose());
      render();
    }

    return { layer: group };
  }

  window.FerryRecent = { create };
})();
