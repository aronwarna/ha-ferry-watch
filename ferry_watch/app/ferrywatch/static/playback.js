/* Track playback, like MarineTraffic's: the ship sails its stored track again while the
   track is drawn behind it in speed colours. Used for the ship card's current voyage and
   for a passage replayed from Stats. One player at a time; it owns its map layers and
   the controls it puts in `mount`. */
window.FerryPlayback = (() => {
  "use strict";

  const SPEEDS = [60, 300, 1200, 3600];
  const icon = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const ICONS = {
    play: icon('<path class="fill" d="M8 5.5v13l10.5-6.5z"/>'),
    pause: icon('<path class="fill" d="M7 5.5h3.5v13H7zM13.5 5.5H17v13h-3.5z"/>'),
    close: icon('<path d="M6 6l12 12M18 6L6 18"/>'),
  };

  function bearing(a, b) {
    const r = Math.PI / 180;
    const y = Math.sin((b[1] - a[1]) * r) * Math.cos(b[0] * r);
    const x = Math.cos(a[0] * r) * Math.sin(b[0] * r) - Math.sin(a[0] * r) * Math.cos(b[0] * r) * Math.cos((b[1] - a[1]) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }

  // The last point at or before time t (points are sorted by time).
  function indexAt(pts, t) {
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (pts[mid][2] <= t) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  function timeText(ts, multiDay) {
    const d = new Date(ts * 1000);
    const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    return multiDay ? `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${hm}` : hm;
  }

  /* points: [lat, lon, ts, sog?]; cover(): pixels at the bottom of the map hidden by the
     panel, so the ship is kept clear of it; onState(playing) for the caller's own button. */
  function create({ map, points, color, mount, shipSvg, speedColor, cover = () => 0, onState = () => {}, onClose = () => {} }) {
    const pts = points.filter((p) => p[2] != null).sort((a, b) => a[2] - b[2]);
    if (pts.length < 2) return null;
    const t0 = pts[0][2], t1 = pts[pts.length - 1][2];
    const multiDay = new Date(t0 * 1000).toDateString() !== new Date(t1 * 1000).toDateString();
    let speed = SPEEDS.find((s) => (t1 - t0) / s <= 75) || SPEEDS[SPEEDS.length - 1];
    let t = t0, playing = false, frame = 0, lastFrame = 0, lastFollow = 0;

    // --- map layers: the whole track faintly, the sailed part in colour, the ship ---
    const group = L.layerGroup().addTo(map);
    L.polyline(pts.map((p) => [p[0], p[1]]), {
      color: "#475569", weight: 3, opacity: 0.5, dashArray: "2 7", lineCap: "round", interactive: false,
    }).addTo(group);
    const outline = L.polyline([], { color: "#111827", weight: 8, opacity: 0.35, lineCap: "round", lineJoin: "round", interactive: false }).addTo(group);
    const runs = L.layerGroup().addTo(group);
    const colors = pts.map((p) => speedColor(p[3]));
    const ship = L.marker([pts[0][0], pts[0][1]], {
      icon: L.divIcon({ className: "ship-icon playback-ship", html: shipSvg(color, 0, 30), iconSize: [30, 30], iconAnchor: [15, 15] }),
      interactive: false, keyboard: false, zIndexOffset: 2000,
    }).addTo(group);
    let drawn = 0, run = null, runColor = null, runPts = [], sailed = [];

    function newRun(from, c) {
      runColor = c;
      runPts = [from];
      run = L.polyline([], { color: c, weight: 4.5, opacity: 1, lineCap: "round", lineJoin: "round", interactive: false }).addTo(runs);
    }
    function restart() {
      runs.clearLayers();
      drawn = 0;
      sailed = [[pts[0][0], pts[0][1]]];
      newRun(sailed[0], colors[1]);
    }
    restart();

    // Draw up to time t: whole points as runs of one colour, then the moving tip.
    function draw() {
      const k = indexAt(pts, t);
      if (k < drawn) restart();
      while (drawn < k) {
        drawn += 1;
        const ll = [pts[drawn][0], pts[drawn][1]];
        if (colors[drawn] !== runColor) {
          run.setLatLngs(runPts);
          newRun(runPts[runPts.length - 1], colors[drawn]);
        }
        runPts.push(ll);
        sailed.push(ll);
      }
      const a = pts[k], b = pts[Math.min(k + 1, pts.length - 1)];
      const f = b[2] > a[2] ? Math.min(1, (t - a[2]) / (b[2] - a[2])) : 0;
      const tip = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
      run.setLatLngs([...runPts, tip]);
      outline.setLatLngs([...sailed, tip]);
      ship.setLatLng(tip);
      const svg = ship.getElement()?.querySelector("svg");
      if (svg && (a[0] !== b[0] || a[1] !== b[1])) svg.style.transform = `rotate(${bearing(a, b)}deg)`;
      const sog = a[3] != null && b[3] != null ? a[3] + (b[3] - a[3]) * f : a[3];
      clockEl.textContent = timeText(t, multiDay);
      infoEl.textContent = sog != null ? `${sog.toFixed(1)} kn` : "";
      const p = ((t - t0) / (t1 - t0)) * 1000;
      if (Math.abs(range.value - p) >= 1) range.value = String(Math.round(p));
      range.style.setProperty("--p", `${(p / 10).toFixed(1)}%`);
      return tip;
    }

    // Keep the ship in view, clear of the top bar and the panel at the bottom.
    function follow(tip, force) {
      const now = performance.now();
      if (!force && now - lastFollow < 400) return;
      lastFollow = now;
      const pt = map.latLngToContainerPoint(tip), size = map.getSize();
      const bottom = size.y - cover() - 30;
      if (pt.x < 40 || pt.x > size.x - 40 || pt.y < 110 || pt.y > bottom) {
        const target = map.containerPointToLatLng([size.x / 2, (110 + bottom) / 2]);
        map.panBy([pt.x - map.latLngToContainerPoint(target).x, pt.y - map.latLngToContainerPoint(target).y], { duration: 0.5 });
      }
    }

    function tick(now) {
      const dt = Math.min(0.1, (now - lastFrame) / 1000);
      lastFrame = now;
      t = Math.min(t1, t + dt * speed);
      follow(draw());
      if (t >= t1) setPlaying(false);
      else frame = requestAnimationFrame(tick);
    }

    function setPlaying(on) {
      if (on && t >= t1) t = t0; // play again from the start
      playing = on;
      cancelAnimationFrame(frame);
      if (on) {
        lastFrame = performance.now();
        frame = requestAnimationFrame(tick);
      }
      toggle.innerHTML = on ? ICONS.pause : ICONS.play;
      toggle.setAttribute("aria-label", on ? "Pause" : "Play");
      onState(on);
    }

    // --- controls ---
    const el = document.createElement("div");
    el.className = "player";
    el.innerHTML = `<button type="button" class="player-btn player-toggle"></button>
      <div class="player-main">
        <div class="player-time"><strong class="player-clock"></strong><span class="player-info"></span><span class="player-end"></span></div>
        <input class="player-range" type="range" min="0" max="1000" step="1" value="0" aria-label="Time in the track">
      </div>
      <button type="button" class="player-speed" title="Playback speed"></button>
      <button type="button" class="player-btn player-close" aria-label="Stop playback">${ICONS.close}</button>`;
    const toggle = el.querySelector(".player-toggle");
    const range = el.querySelector(".player-range");
    const clockEl = el.querySelector(".player-clock");
    const infoEl = el.querySelector(".player-info");
    const speedEl = el.querySelector(".player-speed");
    el.querySelector(".player-end").textContent = timeText(t1, multiDay);
    const showSpeed = () => {
      speedEl.textContent = `×${speed}`;
      speedEl.setAttribute("aria-label", `Speed: ${speed} times real time`);
    };
    showSpeed();
    toggle.addEventListener("click", () => setPlaying(!playing));
    speedEl.addEventListener("click", () => {
      speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length];
      showSpeed();
    });
    range.addEventListener("input", () => {
      t = t0 + (Number(range.value) / 1000) * (t1 - t0);
      follow(draw(), true);
    });
    el.querySelector(".player-close").addEventListener("click", () => onClose());
    mount.replaceChildren(el);
    mount.hidden = false;
    draw();

    return {
      play: () => setPlaying(true),
      pause: () => setPlaying(false),
      toggle: () => setPlaying(!playing),
      get playing() { return playing; },
      destroy() {
        cancelAnimationFrame(frame);
        group.remove();
        mount.replaceChildren();
        mount.hidden = true;
      },
    };
  }

  return { create, ICONS };
})();
