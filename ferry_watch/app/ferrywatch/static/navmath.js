/* Route maths for the measure and route tool, in nautical miles: great-circle (haversine)
   distances and rhumb-line true bearings, the way a chart plotter shows a leg. Kept apart from
   app.js so the tests can run it under Node. Planning only, not for navigation. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.NavMath = api;
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  const EARTH_NM = 6371008.8 / 1852; // mean Earth radius
  const RAD = Math.PI / 180;

  // [lat, lon] in degrees.
  function distanceNm(a, b) {
    const p1 = a[0] * RAD, p2 = b[0] * RAD;
    const dp = p2 - p1, dl = (b[1] - a[1]) * RAD;
    const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * EARTH_NM * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  // The constant true course from a to b (0–360°): what you'd steer on a Mercator chart.
  function rhumbBearing(a, b) {
    const p1 = a[0] * RAD, p2 = b[0] * RAD;
    let dl = (b[1] - a[1]) * RAD;
    if (Math.abs(dl) > Math.PI) dl -= Math.sign(dl) * 2 * Math.PI;
    const dpsi = Math.log(Math.tan(Math.PI / 4 + p2 / 2) / Math.tan(Math.PI / 4 + p1 / 2));
    return ((Math.atan2(dl, dpsi) / RAD) + 360) % 360;
  }

  function legs(points) {
    const out = [];
    for (let i = 1; i < points.length; i++) {
      out.push({ nm: distanceNm(points[i - 1], points[i]), bearing: rhumbBearing(points[i - 1], points[i]) });
    }
    return out;
  }

  const totalNm = (points) => legs(points).reduce((sum, leg) => sum + leg.nm, 0);

  // Seconds at a speed in knots (null for no speed).
  const etaSeconds = (nm, knots) => (knots > 0 ? (nm / knots) * 3600 : null);

  const nm = (value) => `${value.toFixed(1)} NM`;
  const degrees = (value) => `${String(Math.round(value) % 360).padStart(3, "0")}°`;

  function duration(seconds) {
    const m = Math.round(seconds / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  }

  // 59°58.998′N 24°25.500′E, degrees and decimal minutes as on a chart.
  function position(lat, lon) {
    const part = (v, pos, neg, width) => {
      const a = Math.abs(v);
      let d = Math.floor(a), m = (a - d) * 60;
      if (m >= 59.9995) { d += 1; m = 0; }
      return `${String(d).padStart(width, "0")}°${m.toFixed(3).padStart(6, "0")}′${v < 0 ? neg : pos}`;
    };
    return `${part(lat, "N", "S", 2)} ${part(lon, "E", "W", 3)}`;
  }

  return { distanceNm, rhumbBearing, legs, totalNm, etaSeconds, nm, degrees, duration, position };
});
