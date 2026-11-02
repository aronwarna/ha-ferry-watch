/* The route tool's plan: the user's stops and the legs between them. A leg goes straight
   or along the marked fairways; a fairway leg's path is the turns between its two stops,
   worked out by the add-on (api/autoroute). Kept apart from app.js so the tests can run it
   under Node. Planning only, not for navigation.

   stops: [[lat, lon], ...], where [lat, lon, 1] is a bend: a point the user pulled the line
   to, or a fairway turn they moved, rather than a stop they set. The route's own numbered
   stops cut it into sections; a section is one fairway leg, one straight leg, or a line of
   straight legs through bends.
   legs: [{ fw: bool, path: [[lat, lon], ...] }, ...], one fewer than the stops.

   A fairway leg stays automatic until the user changes something inside it: then its
   turns become bends joined by straight legs and are never planned again by themselves, so
   a stretch pulled off the fairway stays where it was put. Moving one of the user's stops
   plans the automatic fairway legs either side of it again.

   Functions that change a plan change it in place and return the indexes of the legs that
   need planning again, unless they say otherwise. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RoutePlan = api;
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  const newLeg = (fw) => ({ fw: Boolean(fw), path: [] });
  const fairwayLegs = (legs, list) => list.filter((i) => i >= 0 && i < legs.length && legs[i].fw);
  const bend = (p) => [p[0], p[1], 1];
  const plain = (p) => [p[0], p[1]];
  const isBend = (p) => p.length > 2 && p[2] === 1;

  // The ends of the route are always stops of their own.
  function fixEnds(stops) {
    if (stops.length && isBend(stops[0])) stops[0] = plain(stops[0]);
    const n = stops.length - 1;
    if (n > 0 && isBend(stops[n])) stops[n] = plain(stops[n]);
  }

  // Every point of the route in order, as [lat, lon]: what is measured, drawn and saved.
  function points(stops, legs) {
    const out = [];
    stops.forEach((p, i) => {
      out.push(plain(p));
      const leg = legs[i];
      if (leg && leg.fw && leg.path) out.push(...leg.path.map(plain));
    });
    return out;
  }

  // One leg's points, its stops included.
  const legPoints = (stops, legs, i) => [stops[i], ...(legs[i].fw ? legs[i].path || [] : []), stops[i + 1]].map(plain);

  // The sections between the user's own stops: [[first stop, last stop], ...].
  function sections(stops) {
    const out = [];
    let a = 0;
    for (let i = 1; i < stops.length; i++) {
      if (i === stops.length - 1 || !isBend(stops[i])) {
        out.push([a, i]);
        a = i;
      }
    }
    return out;
  }

  // The section a leg is in.
  const sectionOf = (stops, leg) => sections(stops).find(([a, b]) => a <= leg && leg < b);

  // All of a section's points, from its first stop to its last.
  function sectionPoints(stops, legs, a, b) {
    const out = [];
    for (let i = a; i < b; i++) out.push(...legPoints(stops, legs, i).slice(i > a ? 1 : 0));
    return out;
  }

  // What a section is: "fw" (one automatic fairway leg), "line" (one straight leg) or
  // "own" (straight legs through bends).
  function sectionKind(legs, a, b) {
    if (b - a > 1) return "own";
    return legs[a].fw ? "fw" : "line";
  }

  // A new last stop, its leg straight or along the fairways.
  function addStop(stops, legs, p, fw) {
    stops.push(plain(p));
    if (stops.length > 1) legs.push(newLeg(fw));
    return stops.length > 1 ? fairwayLegs(legs, [legs.length - 1]) : [];
  }

  // A stop moved. When it's one of the user's own, the automatic fairway legs either side
  // are planned again; a bend's legs are straight.
  function moveStop(stops, legs, i, p) {
    stops[i] = isBend(stops[i]) ? bend(p) : plain(p);
    const touched = fairwayLegs(legs, [i - 1, i]);
    for (const k of touched) legs[k] = newLeg(true);
    return touched;
  }

  // A stop removed. The two legs either side become one, along the fairways if both were.
  function removeStop(stops, legs, i) {
    stops.splice(i, 1);
    fixEnds(stops);
    if (!legs.length) return [];
    if (i === 0) { legs.shift(); return []; }
    if (i >= legs.length) { legs.pop(); return []; }
    const fw = legs[i - 1].fw && legs[i].fw;
    legs.splice(i - 1, 2, newLeg(fw));
    return fairwayLegs(legs, [i - 1]);
  }

  // A fairway leg's turns become bends joined by straight legs, so each can be moved or
  // removed; the line stays where it was. Returns how many stops were added.
  function unpackLeg(stops, legs, i) {
    const path = legs[i].fw ? legs[i].path || [] : [];
    if (!legs[i].fw) return 0;
    stops.splice(i + 1, 0, ...path.map(bend));
    legs.splice(i, 1, ...Array.from({ length: path.length + 1 }, () => newLeg(false)));
    return path.length;
  }

  // Turn k of fairway leg i as a stop of its own (its leg unpacked): the stop's index.
  function turnStop(stops, legs, i, k) {
    unpackLeg(stops, legs, i);
    return i + 1 + k;
  }

  // A bend at p on piece k of leg i (piece k runs from the leg's k-th point to the next,
  // its stops counted): the leg is unpacked if it follows the fairways, so nothing is
  // planned again. Returns the new bend's stop index.
  function insertBend(stops, legs, i, k, p) {
    if (legs[i].fw) {
      unpackLeg(stops, legs, i);
      i += k;
    }
    stops.splice(i + 1, 0, bend(p));
    legs.splice(i, 1, newLeg(false), newLeg(false));
    return i + 1;
  }

  // Everything between stops a and b (a < b) becomes one leg, straight or along the
  // fairways. Stops and bends between them go.
  function setSection(stops, legs, a, b, fw) {
    if (a > b) [a, b] = [b, a];
    stops.splice(a + 1, b - a - 1);
    legs.splice(a, b - a, newLeg(fw));
    return fairwayLegs(legs, [a]);
  }

  // Leg i straight, or along the fairways.
  function setLegMode(legs, i, fw) {
    legs[i] = newLeg(fw);
    return fairwayLegs(legs, [i]);
  }

  // For saving: the points; where the stops are among them (wp), which legs follow the
  // fairways (fw) and which stops are bends (bend, as indexes into wp). A route with only
  // straight legs between stops of the user's own needs no plan.
  function toSaved(stops, legs) {
    const pts = points(stops, legs);
    const bends = [];
    stops.forEach((p, i) => { if (isBend(p)) bends.push(i); });
    if (!legs.some((l) => l.fw) && !bends.length) return { points: pts, plan: null };
    const wp = [];
    let n = 0;
    stops.forEach((_, i) => {
      wp.push(n);
      n += 1 + (legs[i] && legs[i].fw ? legs[i].path.length : 0);
    });
    const plan = { wp, fw: legs.map((l) => (l.fw ? 1 : 0)) };
    if (bends.length) plan.bend = bends;
    return { points: pts, plan };
  }

  // Back from saved points and plan; without a plan (or a broken one) every point is a stop
  // with straight legs between.
  function fromSaved(pts, plan) {
    const wp = plan && plan.wp, fw = plan && plan.fw;
    const ok = Array.isArray(wp) && Array.isArray(fw) && wp.length >= 2 && fw.length === wp.length - 1
      && wp[0] === 0 && wp[wp.length - 1] === pts.length - 1 && wp.every((v, k) => k === 0 || v > wp[k - 1]);
    if (!ok) return { stops: pts.map(plain), legs: pts.slice(1).map(() => newLeg(false)) };
    const bends = new Set(Array.isArray(plan.bend) ? plan.bend : []);
    const stops = wp.map((k, i) => (bends.has(i) ? bend(pts[k]) : plain(pts[k])));
    const legs = fw.map((f, k) => (f ? { fw: true, path: pts.slice(wp[k] + 1, wp[k + 1]).map(plain) } : null));
    // A straight leg with points between its stops (not saved by this tool): those are stops too.
    for (let k = legs.length - 1; k >= 0; k--) {
      if (legs[k]) continue;
      const inner = pts.slice(wp[k] + 1, wp[k + 1]).map(plain);
      stops.splice(k + 1, 0, ...inner);
      legs.splice(k, 1, ...Array.from({ length: inner.length + 1 }, () => newLeg(false)));
    }
    fixEnds(stops);
    return { stops, legs };
  }

  return {
    points, legPoints, sections, sectionOf, sectionPoints, sectionKind, isBend, addStop, moveStop, removeStop,
    unpackLeg, turnStop, insertBend, setSection, setLegMode, toSaved, fromSaved,
  };
});
