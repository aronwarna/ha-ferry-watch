/* The route tool's plan: the user's stops and the legs between them. A leg goes straight
   or along the marked fairways; a fairway leg's path is the turns between its two stops,
   worked out by the add-on (api/autoroute). Changing a stop or a leg only re-plans the
   legs it touches, so a stretch moved off the fairways stays where the user put it. Kept
   apart from app.js so the tests can run it under Node. Planning only, not for navigation.

   stops: [[lat, lon], ...]; legs: [{ fw: bool, path: [[lat, lon], ...] }, ...], one fewer
   than the stops. Functions that change a plan change it in place and return the indexes
   of the legs that need planning again. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RoutePlan = api;
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  const newLeg = (fw) => ({ fw: Boolean(fw), path: [] });
  const fairwayLegs = (legs, list) => list.filter((i) => i >= 0 && i < legs.length && legs[i].fw);

  // Every point of the route in order: what is measured, drawn and saved.
  function points(stops, legs) {
    const out = [];
    stops.forEach((p, i) => {
      out.push(p);
      const leg = legs[i];
      if (leg && leg.fw && leg.path) out.push(...leg.path);
    });
    return out;
  }

  // One leg's points, its stops included.
  const legPoints = (stops, legs, i) => [stops[i], ...(legs[i].fw ? legs[i].path || [] : []), stops[i + 1]];

  // A new last stop, its leg straight or along the fairways.
  function addStop(stops, legs, p, fw) {
    stops.push(p);
    if (stops.length > 1) legs.push(newLeg(fw));
    return stops.length > 1 ? fairwayLegs(legs, [legs.length - 1]) : [];
  }

  // A new stop part way along leg i: it becomes stop i + 1 and splits the leg into two of
  // the same kind.
  function insertStop(stops, legs, i, p) {
    const fw = legs[i].fw;
    stops.splice(i + 1, 0, p);
    legs.splice(i, 1, newLeg(fw), newLeg(fw));
    return fairwayLegs(legs, [i, i + 1]);
  }

  // A stop moved: the fairway legs on either side are planned again.
  function moveStop(stops, legs, i, p) {
    stops[i] = p;
    const touched = fairwayLegs(legs, [i - 1, i]);
    for (const k of touched) legs[k] = newLeg(true);
    return touched;
  }

  // A stop removed. The two legs either side become one, along the fairways if both were.
  function removeStop(stops, legs, i) {
    stops.splice(i, 1);
    if (!legs.length) return [];
    if (i === 0) { legs.shift(); return []; }
    if (i >= legs.length) { legs.pop(); return []; }
    const fw = legs[i - 1].fw && legs[i].fw;
    legs.splice(i - 1, 2, newLeg(fw));
    return fairwayLegs(legs, [i - 1]);
  }

  // Leg i straight, or along the fairways.
  function setLegMode(legs, i, fw) {
    legs[i] = newLeg(fw);
    return fairwayLegs(legs, [i]);
  }

  // A fairway leg's turns become stops of their own, joined by straight legs, so each can be
  // moved or removed.
  function unpackLeg(stops, legs, i) {
    const path = legs[i].fw ? legs[i].path || [] : [];
    stops.splice(i + 1, 0, ...path);
    legs.splice(i, 1, ...Array.from({ length: path.length + 1 }, () => newLeg(false)));
    return [];
  }

  // For saving: the points, and where the stops are among them with which legs follow the
  // fairways. A route with only straight legs needs no plan.
  function toSaved(stops, legs) {
    const pts = points(stops, legs);
    if (!legs.some((l) => l.fw)) return { points: pts, plan: null };
    const wp = [];
    let n = 0;
    stops.forEach((_, i) => {
      wp.push(n);
      n += 1 + (legs[i] && legs[i].fw ? legs[i].path.length : 0);
    });
    return { points: pts, plan: { wp, fw: legs.map((l) => (l.fw ? 1 : 0)) } };
  }

  // Back from saved points and plan; without a plan (or a broken one) every point is a stop
  // with straight legs between.
  function fromSaved(pts, plan) {
    const copy = (p) => [p[0], p[1]];
    const wp = plan && plan.wp, fw = plan && plan.fw;
    const ok = Array.isArray(wp) && Array.isArray(fw) && wp.length >= 2 && fw.length === wp.length - 1
      && wp[0] === 0 && wp[wp.length - 1] === pts.length - 1 && wp.every((v, k) => k === 0 || v > wp[k - 1]);
    if (!ok) return { stops: pts.map(copy), legs: pts.slice(1).map(() => newLeg(false)) };
    const stops = wp.map((k) => copy(pts[k]));
    const legs = fw.map((f, k) => {
      const path = pts.slice(wp[k] + 1, wp[k + 1]).map(copy);
      return f ? { fw: true, path } : null;
    });
    // A straight leg with points between its stops (not saved by this tool): those are stops too.
    for (let k = legs.length - 1; k >= 0; k--) {
      if (legs[k]) continue;
      const inner = pts.slice(wp[k] + 1, wp[k + 1]).map(copy);
      stops.splice(k + 1, 0, ...inner);
      legs.splice(k, 1, ...Array.from({ length: inner.length + 1 }, () => newLeg(false)));
    }
    return { stops, legs };
  }

  return { points, legPoints, addStop, insertStop, moveStop, removeStop, setLegMode, unpackLeg, toSaved, fromSaved };
});
