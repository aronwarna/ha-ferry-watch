/* Blue-green algae on the map: SYKE's satellite classes recoloured, and the algae reports
   people and the national monitoring send, as dots coloured by how much algae they saw.
   The satellite tiles are cut to the sea by overlays.js (sea: true); this gives their
   colours, legend and tap text, and draws the reports. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.FerryAlgae = api;
    if (root.FerryOverlays) Object.assign(root.FerryOverlays.renderers, api.renderers);
  }
})(typeof self !== "undefined" ? self : this, function () {
  // SYKE's colours (its legend) -> ours: clear sea seen a faint blue, so cloud (nothing)
  // reads apart from "no algae"; possible, probable, certain from pale yellow to red.
  const SYKE = [[0x2b, 0x7e, 0xa8], [0xff, 0xff, 0xc9], [0xff, 0xdc, 0x28], [0xcd, 0x33, 0x01]];
  const OURS = [[56, 140, 190, 45], [253, 230, 138, 200], [245, 158, 11, 225], [220, 38, 38, 240]];
  const CLASS_TEXT = ["", "No surface algae seen", "Surface algae possible", "Surface algae probable", "Surface algae certain"];

  // The class of a pixel by its nearest SYKE colour (a little give for resampling); -1 none.
  function classOf(r, g, b) {
    let best = -1, bd = 40 * 40 * 3;
    for (let i = 0; i < SYKE.length; i++) {
      const c = SYKE[i], d = (r - c[0]) ** 2 + (g - c[1]) ** 2 + (b - c[2]) ** 2;
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  function recolour(img) {
    const px = img.data;
    for (let i = 0; i < px.length; i += 4) {
      if (px[i + 3] < 8) { px[i + 3] = 0; continue; }
      const k = classOf(px[i], px[i + 1], px[i + 2]);
      if (k < 0) continue;
      const o = OURS[k];
      px[i] = o[0]; px[i + 1] = o[1]; px[i + 2] = o[2];
      px[i + 3] = Math.round(o[3] * px[i + 3] / 255);
    }
    return img;
  }

  const rgba = (c) => `rgba(${c[0]},${c[1]},${c[2]},${(c[3] / 255).toFixed(2)})`;
  const satellite = {
    recolour,
    legend: () => ({
      unit: "surface algae",
      gradient: `linear-gradient(90deg, ${rgba(OURS[0])} 0 25%, ${rgba(OURS[1])} 25% 50%, ${rgba(OURS[2])} 50% 75%, ${rgba(OURS[3])} 75%)`,
      ticks: [[12.5, "none"], [37.5, "possible"], [62.5, "likely"], [87.5, "certain"]],
      note: "blank = cloud",
    }),
    format(data) {
      if (data.error) return null;
      const day = data.date ? new Date(`${data.date}T12:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "";
      if (!data.index) return ["No satellite view", `Cloud or no pass on ${day}`];
      return [CLASS_TEXT[data.index], `Satellite, ${day}`];
    },
  };

  // --- the reports ---
  const LEVEL_COLOURS = ["#22c55e", "#facc15", "#f97316", "#dc2626"];
  const LEVEL_TEXT = ["No algae", "Some algae", "Plenty of algae", "Very much algae"];
  let drawn = []; // [lat, lon, level, kind, age] of what is on the map now

  const reports = {
    draw(group, data, { opacity, canvas }) {
      const f = data.fields;
      const il = f.indexOf("level"), ik = f.indexOf("kind"), ia = f.indexOf("age");
      drawn = [];
      // The worst on top: none first, very much last.
      const rows = data.points.filter((p) => p[2 + il] != null).sort((a, b) => a[2 + il] - b[2 + il]);
      for (const p of rows) {
        const level = p[2 + il], kind = p[2 + ik], age = p[2 + ia] ?? 0;
        drawn.push([p[0], p[1], level, kind, age]);
        const fade = age <= 1 ? 1 : age <= 2 ? 0.75 : 0.5;
        L.circleMarker([p[0], p[1]], {
          renderer: canvas(), radius: kind === 2 ? 6.5 : 5, interactive: false,
          color: kind === 2 ? "#0f172a" : "#ffffff", weight: kind === 2 ? 2 : 1.2, opacity: opacity * fade,
          fillColor: LEVEL_COLOURS[level] || "#888", fillOpacity: opacity * fade,
        }).addTo(group);
      }
    },
    hide() { drawn = []; },
    legend: () => ({
      unit: "algae seen",
      gradient: `linear-gradient(90deg, ${LEVEL_COLOURS.map((c, i) => `${c} ${i * 25}% ${(i + 1) * 25}%`).join(", ")})`,
      ticks: [[12.5, "none"], [37.5, "some"], [62.5, "plenty"], [87.5, "very much"]],
      note: "ringed = monitoring site",
    }),
    // A tap near a drawn report: its note and who saw it, from the add-on.
    async valueAt(latlng, map, t) {
      if (!drawn.length) return null;
      const at = map.latLngToContainerPoint(latlng);
      let best = null, bd = 16 * 16;
      for (const r of drawn) {
        const p = map.latLngToContainerPoint([r[0], r[1]]);
        const d = (p.x - at.x) ** 2 + (p.y - at.y) ** 2;
        if (d < bd) { bd = d; best = r; }
      }
      if (!best) return null;
      let data = null;
      try {
        const res = await fetch(`api/overlay-value/algae-reports?lat=${best[0]}&lon=${best[1]}&t=${t ?? ""}`);
        data = res.ok ? await res.json() : null;
      } catch { data = null; }
      const r = data?.reports?.[0];
      if (!r) return [LEVEL_TEXT[best[2]] || "Algae report"];
      return reportLines(r);
    },
  };

  function reportLines(r) {
    const when = new Date(r.ts * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    return [LEVEL_TEXT[r.level] || r.level_text, `${r.kind_text}, ${when}`, r.note ? `“${r.note}”` : ""];
  }

  return { classOf, recolour, reportLines, renderers: { algae: satellite, "algae-reports": reports } };
});
