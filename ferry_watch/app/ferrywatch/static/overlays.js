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

  const icon = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const ICONS = {
    play: icon('<path class="fill" d="M8 5.5v13l10.5-6.5z"/>'),
    pause: icon('<path class="fill" d="M7 5.5h3.5v13H7zM13.5 5.5H17v13h-3.5z"/>'),
    back: icon('<path d="M15 6l-6 6 6 6"/>'),
    next: icon('<path d="M9 6l6 6-6 6"/>'),
  };
  const LOAD_WAIT = 4000; // ms playing waits at most for the next frame's tiles
  const LAST_HOLD = 1200; // ms the last frame stays up before playing starts over
  const MIN_REFRESH = 60; // s between time list refreshes while an overlay is on

  /* map: the Leaflet map; store: { get, set } for this browser; switchRow: the menu's row
     builder; canvas(): the shared canvas for points; onChange(): the list or its errors
     changed (re-render the menu); strip: the ship chips, which the bar sits above and
     gives way with (a card, a replay or the area editor has the bottom then). */
  function create({ map, store, switchRow, canvas, onChange = () => {}, strip = null }) {
    const pane = map.createPane("weather");
    pane.style.zIndex = 350; // over the chart tiles, under tracks and ships
    pane.style.pointerEvents = "none";

    const entries = new Map(); // id -> { ov, on, opacity, frames, current, ... }
    let loading = null, refreshTimer = 0, menuShows = "";
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
      </div>
      <button type="button" class="player-btn tb-step" data-step="1" aria-label="Later">${ICONS.next}</button>`;
    document.body.append(bar);
    const toggle = bar.querySelector(".player-toggle");
    const range = bar.querySelector(".player-range");
    const clockEl = bar.querySelector(".player-clock");
    const relEl = bar.querySelector(".player-info");

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
          const shown = JSON.stringify(data.overlays.map((ov) => [ov.id, ov.title, ov.note, ov.error, ov.check]));
          if (shown !== menuShows) { menuShows = shown; onChange(); }
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

    const active = () => [...entries.values()].filter((e) => e.on && e.ov.times?.length);
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
      const layer = e.frames.get(ft);
      return Boolean(layer) && !layer.isLoading();
    }

    // --- the time bar ---

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
        const ft = t == null ? null : frameAt(e.ov.times, t, vf);
        const nft = next == null ? null : frameAt(e.ov.times, next, vf);
        if (e.ov.kind === "points") showPoints(e, ft, nft);
        else showTiles(e, ft, nft);
      }
      renderBar(times);
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
      bar.querySelector('[data-step="-1"]').disabled = i === 0;
      bar.querySelector('[data-step="1"]').disabled = i === times.length - 1;
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
      const own = renderers[e.ov.id]?.extra?.();
      if (own) box.append(own);
      return box;
    }

    // One switch row per overlay, for the On the map list.
    function rows() {
      return [...entries.values()].map((e) => switchRow({
        title: e.ov.title, color: e.ov.color, checked: e.on,
        note: e.on && e.ov.error
          ? `Couldn't reach it just now (${e.ov.error}). It tries again in a few minutes.`
          : e.ov.note,
        extra: e.on ? extras(e) : null,
        onChange: async (on) => { await setOn(e.ov.id, on); onChange(); },
      }));
    }

    // Overlays whose source failed last time, for Settings.
    function problems() {
      return [...entries.values()].filter((e) => e.ov.error).map((e) => `${e.ov.title}: ${e.ov.error}`);
    }

    // What the overlays that are on say about themselves (the wind against the stations), for Settings.
    function checks() {
      return [...entries.values()].filter((e) => e.on && e.ov.check).map((e) => e.ov.check);
    }

    load();
    return { rows, problems, checks, load, setTime, play, pause, get time() { return t; }, entries };
  }

  return { union, frameAt, indexAt, startTime, timeLabel, relative, renderers, create };
});
