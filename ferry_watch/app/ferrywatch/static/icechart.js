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

  // As icemap.COLOURS: the chart (WMO) colours of how close the ice is, in tenths.
  const CLASSES = [["<1", "rgb(165,208,250)"], ["1–3", "rgb(150,232,170)"], ["4–6", "rgb(250,226,70)"],
    ["7–8", "rgb(246,142,48)"], ["9–10", "rgb(222,52,52)"], ["fast", "rgb(160,165,175)"]];

  // Hard steps, one block per class.
  function gradient() {
    const w = 100 / CLASSES.length;
    return `linear-gradient(90deg, ${CLASSES.map(([, c], i) => `${c} ${i * w}%, ${c} ${(i + 1) * w}%`).join(", ")})`;
  }

  function ticks() {
    const w = 100 / CLASSES.length;
    return CLASSES.map(([label], i) => [i === 0 ? 0 : i === CLASSES.length - 1 ? 100 : (i + 0.5) * w, label]);
  }

  // A tap on the ice: what it is, how it lies, how thick.
  function lines(d) {
    const out = [d.class];
    const what = [];
    if (d.src === "chart") {
      if (d.tenths) what.push(`${d.short} cover`);
      if (d.type && d.type !== d.class && !/drift ice|Open water/.test(d.type)) what.push(d.type.toLowerCase());
      if (d.deform) what.push(d.deform.toLowerCase());
    } else if (d.conc) what.push(`${d.conc} % ice`);
    if (what.length) out.push(what.join(", ").replace(/^./, (c) => c.toUpperCase()));
    const thick = d.min && d.max && d.min !== d.max ? `${d.min}–${d.max} cm thick` : d.max || d.thick ? `${d.max || d.thick} cm thick` : "";
    if (thick) out.push(thick);
    return out;
  }

  const renderer = {
    legend: () => ({ unit: "tenths of ice", note: "^ ridged · numbers: thickness cm", gradient: gradient(), ticks: ticks() }),
    format(d) {
      if (!d) return null;
      const day = d.day ? new Date(`${d.day}T00:00:00Z`).toLocaleDateString("en-GB", { timeZone: "UTC", day: "numeric", month: "short", year: "numeric" }) : "";
      if (d.short === "") return [`No ice`, `Ice chart ${day}`];
      return lines(d).concat(`Ice chart ${day}`);
    },
  };

  return { renderer, CLASSES, gradient, ticks, lines };
});
