// Touch gestures the way Google Maps does them, on top of Leaflet's own drag and pinch:
// - double-tap and hold, then slide the finger down to zoom in, up to zoom out, around the
//   place first tapped; let go to settle on the nearest zoom step.
// - double-tap without sliding: one step in, around the tapped place (as before).
// - tap with two fingers at once: one step out.
// Mouse and pen are left to Leaflet (double-click, wheel, the +/- buttons).
(function () {
  "use strict";

  const TAP_MS = 280; // a tap is shorter than this
  const GAP_MS = 320; // and the second one starts this soon after the first ends
  const SLOP_PX = 12; // a tap moves less than this
  const NEAR_PX = 40; // the second tap lands this close to the first
  const PX_PER_ZOOM = 130; // finger travel for one zoom step

  function oneFingerZoom(map) {
    const box = map.getContainer();
    let lastTap = null; // { t, x, y } of the last quick one-finger tap
    let down = null; // the current one-finger touch: { t, x, y, moved }
    let two = null; // a two-finger touch: { t, moved, pts }
    let g = null; // the double-tap gesture under way
    let raf = 0;

    const pt = (touch) => L.point(touch.clientX, touch.clientY).subtract(boxOrigin());
    function boxOrigin() { const r = box.getBoundingClientRect(); return L.point(r.left, r.top); }
    function onMap(e) {
      const t = e.target;
      if (!(t instanceof Element) || !box.contains(t)) return false;
      // Leave buttons, popups and the map's own controls alone.
      return !t.closest(".leaflet-control-container, .leaflet-popup, button, a, input, select, textarea");
    }
    function eat(e) { e.preventDefault(); e.stopPropagation(); }

    // Keep the tapped place under the finger's first spot while the zoom changes.
    function centreFor(zoom, gesture = g) {
      const offset = gesture.anchor.subtract(map.getSize().divideBy(2));
      return map.unproject(map.project(gesture.latlng, zoom).subtract(offset), zoom);
    }
    function clampZoom(z) { return Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), z)); }

    function start(e) {
      if (!onMap(e)) { down = two = null; return; }
      const now = performance.now();
      if (e.touches.length === 2) {
        down = null;
        two = { t: now, moved: false, pts: [...e.touches].map(pt) };
        return;
      }
      if (e.touches.length !== 1) { down = two = null; return; }
      const p = pt(e.touches[0]);
      if (lastTap && now - lastTap.t < GAP_MS && p.distanceTo(lastTap.p) < NEAR_PX) {
        // Second tap: this one is ours, Leaflet's drag and double-tap don't see it.
        eat(e);
        lastTap = null;
        map.stop?.();
        map.fire("doubletapstart", { containerPoint: p });
        g = { anchor: p, latlng: map.containerPointToLatLng(p), y: p.y, z0: map.getZoom(), z: map.getZoom(), moved: false, t: now };
        return;
      }
      down = { t: now, p, moved: false };
    }

    function move(e) {
      if (g) {
        eat(e);
        if (e.touches.length !== 1) return;
        const dy = pt(e.touches[0]).y - g.y;
        if (!g.moved && Math.abs(dy) < SLOP_PX) return;
        if (!g.moved) {
          g.moved = true;
          map._moveStart?.(true, false);
          document.body.classList.add("fw-zooming");
        }
        g.z = clampZoom(g.z0 + dy / PX_PER_ZOOM);
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => map._move(centreFor(g.z), g.z, { pinch: true, round: false }));
        return;
      }
      if (down && e.touches.length === 1 && pt(e.touches[0]).distanceTo(down.p) > SLOP_PX) down.moved = true;
      if (two && e.touches.length === 2) {
        const now = [...e.touches].map(pt);
        if (now.some((q, i) => q.distanceTo(two.pts[i]) > SLOP_PX)) two.moved = true;
      }
    }

    function end(e) {
      const now = performance.now();
      if (g) {
        eat(e);
        if (e.touches.length) return; // wait for the last finger
        cancelAnimationFrame(raf);
        const gesture = g;
        g = null;
        document.body.classList.remove("fw-zooming");
        if (gesture.moved) {
          const z = map._limitZoom(gesture.z);
          const centre = centreFor(z, gesture);
          if (map.options.zoomAnimation) map._animateZoom(centre, z, true, map.options.zoomSnap);
          else map._resetView(centre, z);
        } else if (performance.now() - gesture.t < 500) {
          // A plain double tap: one step in around where it was tapped (held still longer: nothing).
          map.setZoomAround(gesture.anchor, clampZoom(Math.round(map.getZoom()) + 1));
        }
        suppressClicks();
        return;
      }
      if (two) {
        // Both fingers lifted, quickly and without moving: one step out.
        if (!e.touches.length) {
          if (!two.moved && now - two.t < TAP_MS) {
            const mid = two.pts[0].add(two.pts[1]).divideBy(2);
            map.setZoomAround(mid, clampZoom(Math.round(map.getZoom()) - 1));
          }
          two = null;
        }
        return;
      }
      if (down && !e.touches.length) {
        if (!down.moved && now - down.t < TAP_MS) {
          lastTap = { t: now, p: down.p };
        } else {
          lastTap = null;
        }
        down = null;
      }
    }

    // The browser may still send a click (and a long-press menu) for a touch we used.
    let quietUntil = 0;
    function suppressClicks() { quietUntil = performance.now() + 450; }
    function quiet(e) { if (performance.now() < quietUntil && box.contains(e.target)) eat(e); }

    // Listen on the window, ahead of everything, so a gesture we take never reaches Leaflet.
    const opts = { capture: true, passive: false };
    window.addEventListener("touchstart", start, opts);
    window.addEventListener("touchmove", move, opts);
    window.addEventListener("touchend", end, opts);
    window.addEventListener("touchcancel", () => {
      if (g) {
        cancelAnimationFrame(raf);
        if (g.moved) map._resetView(map.getCenter(), map._limitZoom(map.getZoom()));
        g = null;
        document.body.classList.remove("fw-zooming");
      }
      down = two = null;
      lastTap = null;
    }, opts);
    for (const type of ["click", "dblclick", "contextmenu"]) window.addEventListener(type, quiet, { capture: true });
  }

  window.FerryGestures = { oneFingerZoom };
})();
