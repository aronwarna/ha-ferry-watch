/* Weather layers over the map (sea ice, rain radar, wind): a switch row each under
   Menu › On the map with an opacity slider, and one time bar along the bottom of the map
   shared by all of them. The add-on lists each overlay's times; the bar steps through all
   of them together and every overlay shows its own frame for that moment, while the next
   frame loads out of sight so playing doesn't flicker. Which are on, and how see-through,
   is remembered in this browser only. The time maths is kept apart from the map code so
   the tests can run it under Node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FerryOverlays = api;
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  // --- time maths --------------------------------------------------------------------

  // All the times of the overlays that are on, sorted, each once: the time bar's steps.
  function union(lists) {
    return [...new Set(lists.flat())].sort((a, b) => a - b);
  }

  // The frame an overlay shows at time t: its newest at or before t, as long as that is
  // under validFor old (a radar frame isn't shown for tomorrow); just before its first
  // time, that first one. null when it has nothing for t.
  function frameAt(times, t, validFor) {
    let best = null;
    for (const ft of times) {
      if (ft <= t) best = ft;
      else break;
    }
    if (best == null) return times.length && times[0] - t <= validFor ? times[0] : null;
    return t - best < validFor ? best : null;
  }

  // Index of the bar step for time t: the newest at or before it, else the first.
  function indexAt(times, t) {
    let i = 0;
    while (i + 1 < times.length && times[i + 1] <= t) i++;
    return i;
  }

  // Where the bar starts: the newest time that has happened (the latest radar frame),
  // or the first one if they are all still to come (a forecast).
  function startTime(times, now) {
    if (!times.length) return null;
    return times[0] > now ? times[0] : times[indexAt(times, now)];
  }

  function relative(seconds) {
    const a = Math.abs(seconds);
    if (a < 150) return "now";
    const text = a < 90 * 60 ? `${Math.round(a / 60)} min` : a < 48 * 3600 ? `${Math.round(a / 3600)} h`
      : `${Math.round(a / 86400)} days`;
    return seconds < 0 ? `${text} ago` : `in ${text}`;
  }

  // "14:35" today, "Sun 14:35" on another day; and how far that is from now.
  function timeLabel(ts, now) {
    const d = new Date(ts * 1000);
    const clock = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
    const today = new Date(now * 1000).toDateString() === d.toDateString();
    return {
      clock: today ? clock : `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${clock}`,
      rel: relative(ts - now),
    };
  }

  // --- drawing a points overlay -------------------------------------------------------

  // An overlay can bring its own drawing (the wind layer draws arrows and moving lines):
  // FerryOverlays.renderers[id] = { draw(group, data, { opacity, canvas, map }), hide(),
  // extra() }, where data is { time, fields, points: [[lat, lon, ...values]] }; hide()
  // (optional) takes away what draw() put outside the group, and extra() (optional) gives
  // controls for under the switch. Without one, each point is a dot coloured by its first
  // value.
  const renderers = {};
  const SCALE = ["#60a5fa", "#22c55e", "#eab308", "#f97316", "#dc2626"];
  const defaultRenderer = {
    draw(group, data, { opacity, canvas }) {
      const values = data.points.map((p) => p[2]).filter((v) => v != null);
      const lo = Math.min(...values), hi = Math.max(...values);
      for (const p of data.points) {
        if (p[2] == null) continue;
        const k = hi > lo ? (p[2] - lo) / (hi - lo) : 0.5;
        L.circleMarker([p[0], p[1]], {
          renderer: canvas(), radius: 5, stroke: false, interactive: false,
          fillColor: SCALE[Math.min(SCALE.length - 1, Math.floor(k * SCALE.length))], fillOpacity: opacity,
        }).addTo(group);
      }
    },
  };

  // --- the overlays on the map --------------------------------------------------------

  const esc = (text) => String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  const icon = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const ICONS = {
    play: icon('<path class="fill" d="M8 5.5v13l10.5-6.5z"/>'),
    pause: icon('<path class="fill" d="M7 5.5h3.5v13H7zM13.5 5.5H17v13h-3.5z"/>'),
    back: icon('<path d="M15 6l-6 6 6 6"/>'),
    next: icon('<path d="M9 6l6 6-6 6"/>'),
  };
  // Layer icons for the Windy-style chips (24 x 24, stroked).
  const LAYER_ICONS = {
    wind: '<path d="M3 8h11a3 3 0 1 0-3-3M3 12h15a3 3 0 1 1-3 3M3 16h8"/>',
    gust: '<path d="M3 7h9a2.5 2.5 0 1 0-2.5-2.5M3 11h14a3 3 0 1 1-3 3M3 15h6M14 19l3-5h-4l3-5"/>',
    temp: '<path d="M10 14.5V5a2 2 0 1 1 4 0v9.5a4 4 0 1 1-4 0z"/><path d="M12 9v8"/>',
    cloud: '<path d="M7 18h10a4 4 0 0 0 .4-8 5.5 5.5 0 0 0-10.6 1.2A3.4 3.4 0 0 0 7 18z"/>',
    rain: '<path d="M7 14h10a4 4 0 0 0 .4-8 5.5 5.5 0 0 0-10.6 1.2A3.4 3.4 0 0 0 7 14z"/><path d="M8 17l-1 3M12 17l-1 3M16 17l-1 3"/>',
    fog: '<path d="M4 9h16M3 13h18M5 17h14M8 5h8"/>',
    pressure: '<path d="M3 15c3-6 6-6 9 0s6 6 9 0"/><path d="M3 9c3-6 6-6 9 0s6 6 9 0" opacity=".5"/>',
    waves: '<path d="M2 15c2.5-2 4.5-2 7 0s4.5 2 7 0 4.5-2 6 0M2 19c2.5-2 4.5-2 7 0s4.5 2 7 0 4.5-2 6 0M6 11c1-4 4-6 8-5-2 1-3 3-2 5"/>',
    seatemp: '<path d="M8 12.5V4a2 2 0 1 1 4 0v8.5a3.5 3.5 0 1 1-4 0z"/><path d="M14 17c2-1.5 3.5-1.5 5 0s3 1.5 3.5 0M14 21c2-1.5 3.5-1.5 5 0"/>',
    sealevel: '<path d="M3 15c2.5-2 4.5-2 7 0s4.5 2 7 0 3-1.5 4 0M12 3v8M9 6l3-3 3 3"/>',
    aurora: '<path d="M3 20c2-8 4-12 6-14M8 20c1-6 3-10 6-13M13 20c1-5 3-8 7-11"/><circle class="fill" cx="18" cy="4" r="1"/>',
    lightpollution: '<path d="M12 3v2M5.6 5.6 7 7M3 12h2M19 12h2M17 7l1.4-1.4"/><path d="M9 14a3 3 0 1 1 6 0c0 1.5-1 2-1 3.5h-4C10 16 9 15.5 9 14z"/><path d="M10 20h4"/>',
    sunset: '<path d="M3 17h18M6 21h12M7 17a5 5 0 0 1 10 0M12 4v4M4.9 9.9l1.5 1.5M19.1 9.9l-1.5 1.5"/>',
    bolt: '<path d="M13 2 5 13h6l-1 9 8-11h-6z"/>',
    ice: '<path d="M12 2v20M3.3 7l17.4 10M3.3 17 20.7 7M9 3l3 3 3-3M9 21l3-3 3 3"/>',
    radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12l6-6"/>',
  };
  const layerIcon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${LAYER_ICONS[name] || LAYER_ICONS.cloud}</svg>`;
  const ICON_OF = { "rain-radar": "radar", "sea-ice": "ice", "ice-chart": "ice", wind: "wind" };

  const LOAD_WAIT = 4000; // ms playing waits at most for the next frame's tiles
  const LAST_HOLD = 1200; // ms the last frame stays up before playing starts over
  const MIN_REFRESH = 60; // s between time list refreshes while an overlay is on

  /* map: the Leaflet map; store: { get, set } for this browser; switchRow: the menu's row
     builder; canvas(): the shared canvas for points; onChange(): the list or its errors
     changed (re-render the menu); strip: the ship chips, which the bar sits above and
     gives way with (a card, a replay or the area editor has the bottom then). */
  function create({ map, store, switchRow, canvas, onChange = () => {}, strip = null, canTap = () => true }) {
    const pane = map.createPane("weather");
    pane.style.zIndex = 350; // over the chart tiles, under tracks and ships
    pane.style.pointerEvents = "none";

    const entries = new Map(); // id -> { ov, on, opacity, frames, current, ... }
    let loading = null, refreshTimer = 0, infoTimer = 0, menuShows = "";
    let skew = 0; // the add-on's clock minus this browser's, in seconds
    let t = null; // the time bar's time
    let playing = false, playTimer = 0;
    const now = () => Date.now() / 1000 + skew;

    const bar = document.createElement("div");
    bar.className = "timebar panel player";
    bar.hidden = true;
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Weather layer time");
    bar.innerHTML = `<button type="button" class="player-btn player-toggle" aria-label="Play">${ICONS.play}</button>
      <button type="button" class="player-btn tb-step" data-step="-1" aria-label="Earlier">${ICONS.back}</button>
      <div class="player-main">
        <div class="player-time"><strong class="player-clock"></strong><span class="player-info"></span></div>
        <input class="player-range" type="range" min="0" max="0" step="1" value="0" aria-label="Time">
        <div class="tb-days" aria-hidden="true"></div>
      </div>
      <button type="button" class="player-btn tb-step" data-step="1" aria-label="Later">${ICONS.next}</button>`;
    document.body.append(bar);
    const toggle = bar.querySelector(".player-toggle");
    const range = bar.querySelector(".player-range");
    const clockEl = bar.querySelector(".player-clock");
    const relEl = bar.querySelector(".player-info");
    const daysEl = bar.querySelector(".tb-days");

    // The colour scale of the layer on the map, bottom left like Windy's.
    const legendEl = document.createElement("div");
    legendEl.className = "wx-legend";
    legendEl.hidden = true;
    document.body.append(legendEl);

    // --- the list from the add-on ---

    const key = (id, what = "") => `fw-ov-${id}${what}`;

    function entryFor(ov) {
      let e = entries.get(ov.id);
      if (!e) {
        const saved = Number(store.get(key(ov.id, "-opacity")));
        e = {
          ov, on: store.get(key(ov.id)) === "1", opacity: saved > 0 && saved <= 1 ? saved : ov.opacity,
          frames: new Map(), current: null, visible: null, group: null, data: new Map(), loaded: new Set(),
        };
        entries.set(ov.id, e);
      }
      e.ov = ov;
      return e;
    }

    // Times are only asked for for the overlays that are on: an overlay that is off
    // costs the NAS nothing.
    function load(first = true) {
      if (loading) return loading;
      const on = [...entries.values()].filter((e) => e.on).map((e) => e.ov.id);
      loading = fetch(`api/overlays?times=${encodeURIComponent(on.join(","))}`)
        .then((res) => (res.ok ? res.json() : Promise.reject(new Error(res.statusText))))
        .then((data) => {
          const live = t != null && t === startTime(barTimes(), now()); // showing the latest
          skew = data.now - Date.now() / 1000;
          const ids = new Set(data.overlays.map((ov) => ov.id));
          for (const [id, e] of entries) {
            if (!ids.has(id)) { hide(e); entries.delete(id); } // out of season, or gone
          }
          for (const ov of data.overlays) entryFor(ov);
          const times = barTimes();
          if (times.length && (t == null || live || !times.includes(t))) {
            // Showing the latest keeps showing the latest as new frames come in.
            t = t == null || live ? startTime(times, now()) : times[indexAt(times, t)];
          }
          render();
          // The menu is drawn again only when what it shows changed (not under a dragged slider).
          const shown = JSON.stringify(data.overlays.map((ov) => [ov.id, ov.title, ov.note, ov.error, ov.check, ov.info]));
          if (shown !== menuShows) { menuShows = shown; onChange(); }
          // A day picker whose newest day is still being read on the NAS: asked again soon.
          if (data.overlays.some((ov) => ov.info && ov.info.latest_ice === null && entries.get(ov.id)?.on)) {
            clearTimeout(infoTimer);
            infoTimer = setTimeout(() => load(), 8000);
          }
          // The first list shows which switches this browser had on; their times come next.
          return [...entries.values()].some((e) => e.on && !e.ov.times);
        })
        .catch((err) => { console.error("Overlays:", err); return false; })
        .then((again) => {
          loading = null;
          if (again && first) return load(false);
          scheduleRefresh();
          return undefined;
        });
      return loading;
    }

    function scheduleRefresh() {
      clearTimeout(refreshTimer);
      const on = [...entries.values()].filter((e) => e.on);
      if (!on.length) return;
      const every = Math.max(MIN_REFRESH, Math.min(...on.map((e) => e.ov.refresh || 600)));
      refreshTimer = setTimeout(() => { if (!document.hidden) load(); else scheduleRefresh(); }, every * 1000);
    }

    const shown = () => [...entries.values()].filter((e) => e.on && e.ov.times?.length);
    // Static layers (light pollution, the latest lightning) show their one picture whatever the time.
    const active = () => shown().filter((e) => !e.ov.static);
    const barTimes = () => union(active().map((e) => e.ov.times));

    // --- frames ---

    function frameLayer(e, ft) {
      let layer = e.frames.get(ft);
      if (!layer) {
        const ov = e.ov;
        const b = ov.bounds;
        layer = L.tileLayer(`api/overlay/${ov.id}/${ft}/{z}/{x}/{y}.png`, {
          pane: "weather", opacity: 0, tileSize: ov.tile_size, minZoom: ov.min_zoom, maxZoom: 19,
          maxNativeZoom: ov.max_zoom, keepBuffer: 1, className: "overlay-frame", attribution: ov.attribution,
          ...(b ? { bounds: L.latLngBounds([b[0], b[1]], [b[2], b[3]]) } : {}),
        }).addTo(map);
        if (ov.kind === "local") {
          // A tile drawn here may wait for something (the ice chart for the chart's water):
          // asked for again a few times, a little later each time.
          layer.on("tileerror", ({ tile, coords }) => {
            const n = (tile._tries || 0) + 1;
            if (n > 6) return;
            setTimeout(() => {
              if (!layer._map) return;
              tile._tries = n;
              tile.src = `${layer.getTileUrl(coords)}?try=${n}`;
            }, 3000 * n);
          });
        }
        e.frames.set(ft, layer);
      }
      return layer;
    }

    // Frames other than the one showing, the one coming in and the next are let go.
    function trim(e, keep) {
      for (const [ft, layer] of e.frames) {
        if (!keep.includes(ft)) { layer.remove(); e.frames.delete(ft); }
      }
    }

    function showTiles(e, ft, nextFt) {
      if (ft == null) { trim(e, []); e.current = e.visible = null; return; }
      const layer = frameLayer(e, ft);
      e.current = ft;
      // The frame on screen stays until the new one has its tiles, then they swap.
      const reveal = () => {
        if (e.current !== ft || !e.on) return;
        layer.setOpacity(e.opacity);
        for (const [other, l] of e.frames) if (other !== ft) l.setOpacity(0);
        e.visible = ft;
        trim(e, [ft, nextFt]);
      };
      if (e.visible === ft || !layer.isLoading()) reveal();
      else layer.once("load", reveal);
      if (nextFt != null && nextFt !== ft) frameLayer(e, nextFt); // loads out of sight
      trim(e, [ft, nextFt, e.visible]);
    }

    function pointsData(e, ft) {
      if (!e.data.has(ft)) {
        e.data.set(ft, fetch(`api/overlay/${e.ov.id}/${ft}`)
          .then((res) => (res.ok ? res.json() : null))
          .catch(() => null)
          .then((data) => { e.loaded.add(ft); return data; }));
        for (const old of e.data.keys()) {
          if (e.data.size <= 4) break;
          if (old !== ft) { e.data.delete(old); e.loaded.delete(old); }
        }
      }
      return e.data.get(ft);
    }

    function showPoints(e, ft, nextFt) {
      e.current = ft;
      if (!e.group) {
        e.group = L.layerGroup().addTo(map);
        map.attributionControl?.addAttribution(e.ov.attribution);
      }
      if (ft == null) { e.group.clearLayers(); renderers[e.ov.id]?.hide?.(); return; }
      pointsData(e, ft).then((data) => {
        if (e.current !== ft || !e.on || !e.group) return;
        e.group.clearLayers();
        if (data) (renderers[e.ov.id] || defaultRenderer).draw(e.group, data, { opacity: e.opacity, canvas, map });
        renderLegend();
      });
      if (nextFt != null) pointsData(e, nextFt);
    }

    function hide(e) {
      trim(e, []);
      e.current = e.visible = null;
      if (e.group) {
        e.group.remove();
        e.group = null;
        map.attributionControl?.removeAttribution(e.ov.attribution);
        renderers[e.ov.id]?.hide?.();
      }
    }

    function ready(e, ft) {
      if (ft == null) return true;
      if (e.ov.kind === "points") return e.loaded.has(ft);
      if (e.ov.static) return true;
      const layer = e.frames.get(ft);
      return Boolean(layer) && !layer.isLoading();
    }

    // --- the time bar ---

    // A static layer's day: the newest, or one picked under its switch (the ice chart),
    // or the one the add-on suggests (last winter's while the newest has no ice).
    function pickFor(e) {
      const times = e.ov.times, latest = times[times.length - 1];
      if (e.pick != null && times.includes(e.pick)) return e.pick;
      const d = e.ov.info?.default;
      return d != null && times.includes(d) ? d : latest;
    }

    const dayText = (ts, opts = { weekday: "short", day: "numeric", month: "short", year: "numeric" }) =>
      new Date(ts * 1000).toLocaleDateString("en-GB", { timeZone: "UTC", ...opts });
    const isoDay = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

    // Under a pickable layer's switch: the day shown, a date field, and Newest / Last winter.
    function dayControl(e) {
      const box = document.createElement("div");
      box.className = "ov-days";
      const times = e.ov.times || [];
      if (!times.length) return box;
      const info = e.ov.info || {};
      const shownDay = pickFor(e), latest = times[times.length - 1];
      const status = document.createElement("p");
      status.className = "small ov-days-status";
      let text = `Showing ${dayText(shownDay)}`;
      if (shownDay === latest) text += " (the newest chart)";
      else if (info.latest_ice === false) text += `. The newest chart (${dayText(latest, { day: "numeric", month: "short" })}) has no ice yet.`;
      status.textContent = text;
      const row = document.createElement("div");
      row.className = "ov-days-row";
      const input = document.createElement("input");
      input.type = "date";
      input.min = isoDay(times[0]);
      input.max = isoDay(latest);
      input.value = isoDay(shownDay);
      input.setAttribute("aria-label", `${e.ov.title} day`);
      const choose = (ts) => { e.pick = ts; render(); onChange(); };
      input.addEventListener("change", () => {
        if (!input.value) return;
        const want = Date.parse(`${input.value}T00:00:00Z`) / 1000;
        // The nearest day the service has (it may skip some).
        let best = times[0];
        for (const d of times) if (Math.abs(d - want) < Math.abs(best - want)) best = d;
        choose(best);
      });
      const button = (label, ts) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chip-btn";
        b.textContent = label;
        b.disabled = ts == null || ts === shownDay;
        b.addEventListener("click", () => choose(ts));
        return b;
      };
      row.append(input, button("Newest", latest), button("Last winter", info.peak ?? null));
      box.append(status, row);
      return box;
    }

    function nextTime(times) {
      const i = indexAt(times, t);
      return times.length > 1 ? times[(i + 1) % times.length] : null;
    }

    function render() {
      const times = barTimes();
      const next = times.length ? nextTime(times) : null;
      for (const e of entries.values()) {
        if (!e.on || !e.ov.times?.length) { hide(e); continue; }
        const vf = e.ov.valid_for;
        const ft = e.ov.static ? pickFor(e) : t == null ? null : frameAt(e.ov.times, t, vf);
        const nft = e.ov.static ? null : next == null ? null : frameAt(e.ov.times, next, vf);
        if (e.ov.kind === "points") showPoints(e, ft, nft);
        else showTiles(e, ft, nft);
      }
      renderBar(times);
      renderLegend();
    }

    function renderBar(times = barTimes()) {
      const free = !strip || !strip.hidden;
      const show = times.length > 1 && free;
      bar.hidden = !show;
      if (!show) { pause(); return; }
      const i = indexAt(times, t);
      range.max = String(times.length - 1);
      range.value = String(i);
      range.style.setProperty("--p", `${(i / (times.length - 1)) * 100}%`);
      const label = timeLabel(t, now());
      clockEl.textContent = label.clock;
      relEl.textContent = label.rel;
      renderDays(times);
      bar.querySelector('[data-step="-1"]').disabled = i === 0;
      bar.querySelector('[data-step="1"]').disabled = i === times.length - 1;
    }

    // Day names under the slider where each day starts, like Windy's timeline.
    let daysShown = "";
    function renderDays(times) {
      const key = `${times[0]}:${times[times.length - 1]}:${times.length}`;
      if (key === daysShown) return;
      daysShown = key;
      const marks = [];
      let last = "";
      times.forEach((ts, i) => {
        const d = new Date(ts * 1000);
        const day = d.toDateString();
        if (day !== last) {
          marks.push([i, d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric" })]);
          last = day;
        }
      });
      const n = Math.max(1, times.length - 1);
      daysEl.replaceChildren(...(marks.length > 1 || times.length > 12 ? marks : []).map(([i, text]) => {
        const span = document.createElement("span");
        span.textContent = text;
        span.style.left = `${(i / n) * 100}%`;
        return span;
      }));
      daysEl.hidden = !daysEl.children.length;
    }

    function setTime(ts) {
      t = ts;
      render();
    }

    function step(by) {
      const times = barTimes();
      if (!times.length) return;
      const i = Math.max(0, Math.min(times.length - 1, indexAt(times, t) + by));
      setTime(times[i]);
    }

    const frameMs = () => Math.min(...active().map((e) => e.ov.frame_ms || 500));

    function tick() {
      clearTimeout(playTimer);
      if (!playing) return;
      const times = barTimes();
      if (times.length < 2) { pause(); return; }
      const last = indexAt(times, t) === times.length - 1;
      const next = last ? times[0] : times[indexAt(times, t) + 1];
      const started = performance.now();
      const advance = () => {
        if (!playing) return;
        // Wait for the next frame's tiles, so a slow phone shows every frame, not gaps.
        const waiting = active().some((e) => !ready(e, frameAt(e.ov.times, next, e.ov.valid_for)));
        if (waiting && performance.now() - started < LOAD_WAIT) { playTimer = setTimeout(advance, 100); return; }
        setTime(next);
        tick();
      };
      playTimer = setTimeout(advance, frameMs() + (last ? LAST_HOLD : 0));
    }

    function play() {
      const times = barTimes();
      if (times.length < 2) return;
      playing = true;
      toggle.innerHTML = ICONS.pause;
      toggle.setAttribute("aria-label", "Pause");
      if (indexAt(times, t) === times.length - 1) setTime(times[0]);
      tick();
    }

    function pause() {
      playing = false;
      clearTimeout(playTimer);
      toggle.innerHTML = ICONS.play;
      toggle.setAttribute("aria-label", "Play");
    }

    toggle.addEventListener("click", () => (playing ? pause() : play()));
    for (const b of bar.querySelectorAll(".tb-step")) {
      b.addEventListener("click", () => { pause(); step(Number(b.dataset.step)); });
    }
    range.addEventListener("input", () => {
      pause();
      const times = barTimes();
      setTime(times[Number(range.value)] ?? t);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) pause();
      else if (active().length) load();
    });
    // The bar sits above the ship chips and gives way when something else has the bottom.
    if (strip) new MutationObserver(() => renderBar()).observe(strip, { attributes: true, attributeFilter: ["hidden"] });

    // --- switches ---

    async function setOn(id, on) {
      const e = entries.get(id);
      if (!e) return;
      e.on = on;
      store.set(key(id), on ? "1" : "0");
      if (on && e.ov.group === "field") {
        // One colour layer at a time, as on Windy.
        for (const other of entries.values()) {
          if (other !== e && other.on && other.ov.group === "field") {
            other.on = false;
            store.set(key(other.ov.id), "0");
            hide(other);
          }
        }
      }
      if (!on) {
        hide(e);
        if (!active().length) { pause(); t = null; }
        render();
        scheduleRefresh();
        return;
      }
      await load(); // its times, and the bar's start
    }

    function setOpacity(id, value) {
      const e = entries.get(id);
      if (!e) return;
      e.opacity = value;
      store.set(key(id, "-opacity"), String(value));
      if (e.ov.kind === "points") {
        const ft = e.current;
        e.current = null;
        showPoints(e, ft, null);
      } else {
        e.frames.get(e.current)?.setOpacity(value);
      }
    }

    function opacityControl(e) {
      const label = document.createElement("label");
      label.className = "ov-opacity small muted";
      const input = document.createElement("input");
      input.type = "range";
      input.min = "0.1";
      input.max = "1";
      input.step = "0.05";
      input.value = String(e.opacity);
      input.className = "player-range";
      input.setAttribute("aria-label", `${e.ov.title} opacity`);
      const paint = () => input.style.setProperty("--p", `${((input.value - 0.1) / 0.9) * 100}%`);
      paint();
      input.addEventListener("input", () => { paint(); setOpacity(e.ov.id, Number(input.value)); });
      label.append("Opacity", input);
      return label;
    }

    // Under a switch that is on: the opacity, and the overlay's own controls.
    function extras(e) {
      const box = document.createElement("div");
      box.append(opacityControl(e));
      if (e.ov.pickable) box.append(dayControl(e));
      const own = renderers[e.ov.id]?.extra?.();
      if (own) box.append(own);
      return box;
    }

    // One switch row per overlay that goes on top, for the weather panel.
    function rows() {
      return [...entries.values()].filter((e) => e.ov.group !== "field").map((e) => switchRow({
        title: e.ov.title, color: e.ov.color, checked: e.on,
        note: e.on && e.ov.error
          ? `Couldn't reach it just now (${e.ov.error}). It tries again in a few minutes.`
          : e.ov.note,
        extra: e.on ? extras(e) : null,
        onChange: async (on) => { await setOn(e.ov.id, on); onChange(); },
      }));
    }

    // The colour layers as Windy-style chips, one on at a time; under them, the one that is
    // on: what it shows, its opacity and its own switches.
    function fieldChips() {
      const box = document.createElement("div");
      box.className = "wx-fields";
      const grid = document.createElement("div");
      grid.className = "wx-chips";
      grid.setAttribute("role", "group");
      grid.setAttribute("aria-label", "Colour layer");
      const fields = [...entries.values()].filter((e) => e.ov.group === "field");
      for (const e of fields) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "wx-chip";
        b.setAttribute("aria-pressed", String(e.on));
        b.style.setProperty("--c", e.ov.color);
        b.innerHTML = `${layerIcon(e.ov.icon || ICON_OF[e.ov.id])}<span>${esc(e.ov.title)}</span>`;
        if (e.on && e.ov.error) b.classList.add("failing");
        b.addEventListener("click", async () => { await setOn(e.ov.id, !e.on); onChange(); });
        grid.append(b);
      }
      box.append(grid);
      const on = fields.find((e) => e.on);
      if (on) {
        const sel = document.createElement("div");
        sel.className = "wx-selected";
        const note = document.createElement("p");
        note.className = "small muted";
        note.textContent = on.ov.error
          ? `Couldn't reach it just now (${on.ov.error}). It tries again in a few minutes.`
          : on.ov.note;
        if (on.ov.error) note.classList.add("problem");
        sel.append(note);
        const summary = renderers[on.ov.id]?.summary?.();
        if (summary) {
          const p = document.createElement("p");
          p.className = "small strong wx-summary";
          p.textContent = summary;
          sel.append(p);
        }
        sel.append(extras(on));
        box.append(sel);
      } else {
        const p = document.createElement("p");
        p.className = "small muted";
        p.textContent = "Pick a layer to colour the map. Tap the map for the value there.";
        box.append(p);
      }
      return box;
    }

    // --- the legend and a tap on the map ---

    function renderLegend() {
      const field = shown().find((e) => e.ov.group === "field") || shown().find((e) => renderers[e.ov.id]?.legend);
      const lg = field && renderers[field.ov.id]?.legend?.();
      legendEl.hidden = !lg;
      if (!lg) return;
      const ticks = lg.ticks.map(([p, v]) => `<span style="left:${p}%">${esc(String(v))}</span>`).join("");
      const html = `<div class="wx-legend-head"><strong>${esc(field.ov.title)}</strong><span>${esc(lg.unit)}${lg.note ? ` · ${esc(lg.note)}` : ""}</span></div>`
        + `<div class="wx-legend-bar" style="background:${lg.gradient}"></div><div class="wx-legend-ticks">${ticks}</div>`;
      if (legendEl.innerHTML !== html) legendEl.innerHTML = html;
    }

    let picker = null;
    async function showPicker(latlng) {
      const on = shown().filter((e) => renderers[e.ov.id]?.valueAt || e.ov.has_value)
        .sort((a, b) => (a.ov.group === "field" ? -1 : 0) - (b.ov.group === "field" ? -1 : 0));
      if (!on.length) return;
      const parts = [];
      for (const e of on) {
        let lines = null;
        try {
          if (renderers[e.ov.id]?.valueAt) lines = await renderers[e.ov.id].valueAt(latlng, map);
          if (!lines && e.ov.has_value) {
            const res = await fetch(`api/overlay-value/${e.ov.id}?lat=${latlng.lat.toFixed(5)}&lon=${latlng.lng.toFixed(5)}&t=${e.ov.static ? (e.current ?? "") : (t || "")}`);
            const data = res.ok ? await res.json() : null;
            lines = data && (renderers[e.ov.id]?.format ? renderers[e.ov.id].format(data) : [JSON.stringify(data)]);
          }
        } catch (err) { console.error(err); }
        if (lines && lines[0]) parts.push(lines);
      }
      if (!parts.length) return;
      const when = t != null && active().length ? `<div class="wx-pick-time">${esc(timeLabel(t, now()).clock)}</div>` : "";
      const html = parts.map(([main, ...rest], i) => `<div class="wx-pick${i ? " more" : ""}"><strong>${esc(main)}</strong>${rest.filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("")}</div>`).join("") + when;
      picker = L.popup({ className: "wx-popup", offset: [0, -4], autoPan: false, closeButton: false })
        .setLatLng(latlng).setContent(html).openOn(map);
    }

    map.on("click", (e) => {
      if (!shown().length || !canTap(e.latlng)) return;
      const before = map._popup;
      // A chart mark or station that opened its own popup for the same tap goes first.
      setTimeout(() => {
        if (map._popup && map._popup !== before && map._popup !== picker && map.hasLayer(map._popup)) return;
        showPicker(e.latlng);
      }, 0);
    });

    // Overlays whose source failed last time, for Settings.
    function problems() {
      return [...entries.values()].filter((e) => e.ov.error).map((e) => `${e.ov.title}: ${e.ov.error}`);
    }

    // What the overlays that are on say about themselves (the wind against the stations), for Settings.
    function checks() {
      return [...entries.values()].filter((e) => e.on && e.ov.check).map((e) => e.ov.check);
    }

    load();
    return { rows, fieldChips, problems, checks, load, setTime, play, pause, get time() { return t; }, entries };
  }

  return { union, frameAt, indexAt, startTime, timeLabel, relative, renderers, create };
});
