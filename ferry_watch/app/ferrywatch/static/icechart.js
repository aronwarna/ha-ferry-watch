/* The ice chart overlay ("ice-chart", icechart.py): its legend of drift ice classes and
   what a tap on the ice says. The tiles are drawn on the NAS; the day is picked under the
   layer's switch (overlays.js). */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.FerryIceChart = api;
    if (root.FerryOverlays) root.FerryOverlays.renderers["ice-chart"] = api.renderer;
  }
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  // As icechart.CLASSES: the WMO colours of total concentration, in tenths.
  const CLASSES = [["<1", "rgb(150,200,255)"], ["1–3", "rgb(140,255,160)"], ["4–6", "rgb(255,255,0)"],
    ["7–8", "rgb(255,125,7)"], ["9–10", "rgb(255,0,0)"], ["fast", "rgb(150,150,150)"]];

  // Hard steps, one block per class.
  function gradient() {
    const w = 100 / CLASSES.length;
    return `linear-gradient(90deg, ${CLASSES.map(([, c], i) => `${c} ${i * w}%, ${c} ${(i + 1) * w}%`).join(", ")})`;
  }

  function ticks() {
    const w = 100 / CLASSES.length;
    return CLASSES.map(([label], i) => [i === 0 ? 0 : i === CLASSES.length - 1 ? 100 : (i + 0.5) * w, label]);
  }

  const renderer = {
    legend: () => ({ unit: "tenths of ice", note: "numbers: thickness cm", gradient: gradient(), ticks: ticks() }),
    format(d) {
      if (!d) return null;
      const day = d.day ? new Date(`${d.day}T00:00:00Z`).toLocaleDateString("en-GB", { timeZone: "UTC", day: "numeric", month: "short", year: "numeric" }) : "";
      if (d.short === "") return [`No ice`, `Ice chart ${day}`];
      return [d.class, `${d.conc} % ice${d.thick ? `, ${d.thick} cm thick` : ""}`, `Ice chart ${day}`];
    },
  };

  return { renderer, CLASSES, gradient, ticks };
});
