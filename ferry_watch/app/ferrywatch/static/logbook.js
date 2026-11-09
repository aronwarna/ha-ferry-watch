/* Trip logbook: ben's own trips, from the tracks his Garmin plotter saves to its card
   (GPX, uploaded through the Logbook tab). The add-on cuts the plotter's long track into
   trips; this lists them with distance, time underway, speeds, depth and water temperature,
   and draws one on the map coloured by speed. Tap the line for the time, speed and depth
   there. The number formatting is kept apart so the tests can run it under Node. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FerryLogbook = api;
})(typeof self !== "undefined" ? self : this, () => {
  "use strict";

  // Speed colours, slow to fast (knots): drifting grey, displacement blue, planing orange/red.
  const SPEED_STEPS = [[0, "#9ca3af"], [3, "#38bdf8"], [7, "#2563eb"], [12, "#16a34a"], [18, "#f59e0b"], [25, "#dc2626"]];
  const speedColour = (kn) => {
    let c = SPEED_STEPS[0][1];
    for (const [k, col] of SPEED_STEPS) if (kn != null && kn >= k) c = col;
    return c;
  };

  const nm = (v) => (v == null ? "–" : v < 10 ? v.toFixed(1) : Math.round(v).toString());
  const hours = (s) => {
    const m = Math.round(s / 60);
    return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  };
  const range = (st, unit) => (st ? (st.min === st.max ? `${st.min} ${unit}` : `${st.min}–${st.max} ${unit}`) : null);

  // Runs of points with one speed colour, so the line is a handful of polylines, not thousands.
  function colourRuns(points) {
    const runs = [];
    let cur = null;
    for (const p of points) {
      const col = speedColour(p[3]);
      if (!cur || cur.colour !== col) {
        if (cur) cur.latlngs.push([p[0], p[1]]); // join up with the next run
        cur = { colour: col, latlngs: [] };
        runs.push(cur);
      }
      cur.latlngs.push([p[0], p[1]]);
    }
    return runs;
  }

  // A small profile: speed as an area, depth as a line under it (both scaled to their own top).
  function profileSvg(points) {
    const timed = points.filter((p) => p[2] != null);
    if (timed.length < 2) return "";
    const W = 320, H = 70, t0 = timed[0][2], t1 = timed[timed.length - 1][2] || t0 + 1;
    const x = (t) => ((t - t0) / (t1 - t0 || 1)) * W;
    const top = Math.max(1, ...timed.map((p) => p[3] || 0));
    const deep = Math.max(0, ...timed.map((p) => p[4] || 0));
    const sp = timed.map((p) => `${x(p[2]).toFixed(1)},${(H - ((p[3] || 0) / top) * (H - 6)).toFixed(1)}`).join(" ");
    let depth = "";
    if (deep) {
      const d = timed.filter((p) => p[4] != null).map((p) => `${x(p[2]).toFixed(1)},${(4 + (p[4] / deep) * (H - 8)).toFixed(1)}`).join(" ");
      depth = `<polyline class="lb-depth" points="${d}"/>`;
    }
    return `<svg class="lb-profile" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
      + `<polygon class="lb-speed" points="0,${H} ${sp} ${W},${H}"/>${depth}</svg>`
      + `<div class="lb-profile-key small muted"><span><i class="lb-key-speed"></i>Speed (top ${top.toFixed(1)} kn)</span>`
      + (deep ? `<span><i class="lb-key-depth"></i>Depth (down to ${deep.toFixed(1)} m)</span>` : "") + "</div>";
  }

  function create({ map, api, esc, fmtDay, clock, pane, pill, onShow, sideWidth }) {
    let data = { trips: [], years: {} };
    let open = null;              // trip id whose details are open in the list
    let shown = null;             // {id, layer, points}
    let note = "";
    let busy = false;

    const tripTitle = (t) => {
      if (t.name) return t.name;
      if (t.from_name && t.to_name && t.from_name !== t.to_name) return `${t.from_name} → ${t.to_name}`;
      if (t.from_name) return t.to_name === t.from_name ? `Round trip from ${t.from_name}` : `From ${t.from_name}`;
      if (t.to_name) return `To ${t.to_name}`;
      if (!t.timed) return "Saved track";
      const d = new Date(t.start * 1000), h = d.getHours();
      const part = h < 5 ? "night" : h < 12 ? "morning" : h < 17 ? "afternoon" : h < 22 ? "evening" : "night";
      return `${d.toLocaleDateString("en-GB", { weekday: "long" })} ${part} trip`;
    };

    function totals() {
      const years = Object.keys(data.years).sort().reverse();
      if (!years.length) return "";
      return `<div class="stats-tiles lb-tiles">${years.slice(0, 2).map((y) => {
        const r = data.years[y];
        return `<div class="tile"><strong>${r.trips}</strong><span>trips in ${esc(y)}</span></div>`
          + `<div class="tile"><strong>${nm(r.distance_nm)}</strong><span>NM in ${esc(y)}</span></div>`;
      }).join("")}</div>`
        + `<p class="small muted lb-hours">${years.slice(0, 2).map((y) => `${esc(y)}: ${hours(data.years[y].moving_s)} underway`).join(" · ")}</p>`;
    }

    function row(t) {
      const when = t.timed ? `${fmtDay(t.start)} · ${clock(t.start)}–${clock(t.end)}` : "No times (a saved track)";
      const facts = [
        `${nm(t.distance_nm)} NM`,
        t.timed && hours(t.moving_s),
        t.avg_kn != null && `avg ${t.avg_kn} kn`,
        t.top_kn != null && `top ${t.top_kn} kn`,
      ].filter(Boolean).join(" · ");
      const extra = [
        t.depth && `<span class="lb-badge" title="Depth under the transducer">⤓ ${esc(range(t.depth, "m"))}</span>`,
        t.wtemp && `<span class="lb-badge" title="Water temperature">${esc(range(t.wtemp, "°C"))}</span>`,
      ].filter(Boolean).join("");
      const isOpen = open === t.id;
      return `<li class="lb-trip${isOpen ? " open" : ""}${shown?.id === t.id ? " shown" : ""}" data-trip="${esc(t.id)}">
        <button type="button" class="lb-row" aria-expanded="${isOpen}">
          <span class="strong lb-title">${esc(tripTitle(t))}</span>
          <span class="small muted">${esc(when)}</span>
          <span class="small">${esc(facts)}${extra ? ` ${extra}` : ""}</span>
        </button>
        ${isOpen ? details(t) : ""}
      </li>`;
    }

    function details(t) {
      return `<div class="lb-details">
        <div class="lb-profile-box" data-profile>${shown?.id === t.id ? profileSvg(shown.points) : ""}</div>
        ${t.note ? `<p class="small lb-note">${esc(t.note)}</p>` : ""}
        <div class="row-buttons">
          <button type="button" data-act="show">${shown?.id === t.id ? "Fit on map" : "Show on map"}</button>
          <button type="button" data-act="edit">Name &amp; note</button>
          <button type="button" data-act="delete" class="danger-text">Remove</button>
        </div>
        <form class="lb-edit" hidden>
          <input name="name" maxlength="80" placeholder="${esc(tripTitle(t))}" value="${esc(t.name || "")}" aria-label="Trip name">
          <textarea name="note" rows="3" maxlength="2000" placeholder="A note: who came along, fish, weather…" aria-label="Note">${esc(t.note || "")}</textarea>
          <div class="row-buttons"><button type="submit">Save</button><button type="button" data-act="cancel">Cancel</button></div>
        </form>
        ${t.timed ? `<p class="small muted">${t.points.toLocaleString()} track points · ${hours(t.duration_s)} from first to last${t.moving_s < t.duration_s - 120 ? `, ${hours(t.moving_s)} of it underway` : ""}</p>` : ""}
      </div>`;
    }

    function render() {
      const list = data.trips;
      pane.innerHTML = `
        <div class="lb-upload">
          <label class="lb-upload-button${busy ? " busy" : ""}">
            <input type="file" id="lb-file" multiple accept=".gpx,.zip,.adm,.fit,.rsd,application/gpx+xml,application/zip" ${busy ? "disabled" : ""}>
            <span>${busy ? "Reading…" : "Add tracks from the plotter"}</span>
          </label>
          <p class="small lb-result" role="status">${note}</p>
        </div>
        <details class="lb-help"${list.length ? "" : " open"}>
          <summary class="strong">How to get the track off the Garmin</summary>
          <ol class="small">
            <li>Put a microSD card in the plotter.</li>
            <li>Open <b>Info › User Data</b> (some models: <b>Vessel › Manage Data</b>), then <b>Data Transfer › Save to Card</b>. If it asks, choose <b>GPX</b>.</li>
            <li>Upload the .gpx file from the card here (a .zip of the card's Garmin folder works too).</li>
          </ol>
          <p class="small muted">The plotter's active track has the times; trips are cut from it where the boat lay still or the plotter was off. Upload the same card again whenever you like: only new stretches are added. Depth and water temperature come along when the sounder was on.</p>
        </details>
        ${totals()}
        <h3 class="group-head">Trips</h3>
        ${list.length ? `<ul class="lb-list">${list.map(row).join("")}</ul>` : '<p class="small muted">No trips yet.</p>'}`;
      pane.querySelector("#lb-file")?.addEventListener("change", (e) => upload([...e.target.files]));
    }

    pane.addEventListener("click", async (e) => {
      const li = e.target.closest("[data-trip]");
      if (!li) return;
      const t = data.trips.find((x) => x.id === li.dataset.trip);
      if (!t) return;
      if (e.target.closest(".lb-row")) {
        open = open === t.id ? null : t.id;
        render();
        if (open && t.timed && shown?.id !== t.id) loadProfile(t);
        return;
      }
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "show") show(t, true);
      else if (act === "edit") { li.querySelector(".lb-edit").hidden = false; li.querySelector(".lb-edit input").focus(); }
      else if (act === "cancel") li.querySelector(".lb-edit").hidden = true;
      else if (act === "delete") {
        if (!confirm(`Remove “${tripTitle(t)}” from the logbook? It stays out even if the same card is uploaded again.`)) return;
        await api(`api/logbook/${encodeURIComponent(t.id)}`, { method: "DELETE" });
        if (shown?.id === t.id) hide();
        open = null;
        await load();
      }
    });
    pane.addEventListener("submit", async (e) => {
      const form = e.target.closest(".lb-edit");
      if (!form) return;
      e.preventDefault();
      const id = form.closest("[data-trip]").dataset.trip;
      await api(`api/logbook/${encodeURIComponent(id)}`, { method: "POST", body: { name: form.name.value, note: form.note.value } });
      await load();
    });

    const cache = new Map();
    async function points(t) {
      if (!cache.has(t.id)) cache.set(t.id, (await api(`api/logbook/${encodeURIComponent(t.id)}`)).points);
      return cache.get(t.id);
    }

    async function loadProfile(t) {
      const pts = await points(t);
      const box = pane.querySelector(`[data-trip="${CSS.escape(t.id)}"] [data-profile]`);
      if (box) box.innerHTML = profileSvg(pts);
    }

    async function show(t, fit) {
      const pts = await points(t);
      if (shown?.id !== t.id) {
        hide(true);
        const group = L.featureGroup();
        const timed = pts.length && pts[0].length > 2;
        if (timed) {
          for (const r of colourRuns(pts)) {
            L.polyline(r.latlngs, { color: r.colour, weight: 4, opacity: 0.95, interactive: false }).addTo(group);
          }
        } else {
          L.polyline(pts, { color: "#7c3aed", weight: 4, opacity: 0.9, interactive: false }).addTo(group);
        }
        // A wide see-through line on top takes the taps.
        const hit = L.polyline(pts.map((p) => [p[0], p[1]]), { weight: 18, opacity: 0, className: "lb-hit" }).addTo(group);
        if (timed) hit.on("click", (e) => { L.DomEvent.stop(e); pointPopup(pts, e.latlng); });
        L.circleMarker(pts[0].slice(0, 2), { radius: 6, color: "#fff", weight: 2, fillColor: "#16a34a", fillOpacity: 1 })
          .bindTooltip(timed ? `Left ${clock(pts[0][2])}` : "Start").addTo(group);
        const last = pts[pts.length - 1];
        L.circleMarker(last.slice(0, 2), { radius: 6, color: "#fff", weight: 2, fillColor: "#dc2626", fillOpacity: 1 })
          .bindTooltip(timed ? `Arrived ${clock(last[2])}` : "End").addTo(group);
        group.addTo(map);
        shown = { id: t.id, layer: group, points: pts };
        if (pill) {
          pill.hidden = false;
          pill.firstElementChild.textContent = `${tripTitle(t)} shown`;
        }
        render();
      }
      if (fit) {
        onShow?.();
        // Beside the menu on a wide screen, not under it.
        const side = sideWidth?.() || 0;
        map.fitBounds(shown.layer.getBounds(), { paddingTopLeft: [30, 70], paddingBottomRight: [30 + side, 90], maxZoom: 15 });
      }
    }

    function pointPopup(pts, latlng) {
      let best = pts[0], bd = Infinity;
      for (const p of pts) {
        const d = (p[0] - latlng.lat) ** 2 + ((p[1] - latlng.lng) * 0.5) ** 2;
        if (d < bd) { bd = d; best = p; }
      }
      const lines = [
        `<b>${esc(fmtDay(best[2]))}, ${esc(clock(best[2]))}</b>`,
        best[3] != null && `${best[3].toFixed(1)} kn`,
        best[4] != null && `Depth ${best[4].toFixed(1)} m`,
        best[5] != null && `Water ${best[5].toFixed(1)} °C`,
      ].filter(Boolean);
      L.popup({ className: "aid-popup", offset: [0, -4] }).setLatLng([best[0], best[1]]).setContent(lines.join("<br>")).openOn(map);
    }

    function hide(quiet) {
      if (shown) map.removeLayer(shown.layer);
      shown = null;
      if (pill) pill.hidden = true;
      if (!quiet) render();
    }
    pill?.addEventListener("click", () => hide());

    async function upload(files) {
      if (!files.length) return;
      busy = true;
      note = "";
      render();
      const form = new FormData();
      for (const f of files) form.append("file", f, f.name);
      try {
        const res = await fetch("api/logbook/upload", { method: "POST", body: form });
        if (!res.ok) throw new Error(res.status === 413 ? "That file is too big (200 MB at most)." : await res.text());
        const out = (await res.json()).files;
        note = out.map((r) => {
          if (r.error) return `<span class="warn-text">${esc(r.file)}: ${esc(r.error)}</span>`;
          const bits = [];
          if (r.new_trips) bits.push(`${r.new_trips} new ${r.new_trips === 1 ? "trip" : "trips"}`);
          else if (r.new_points) bits.push(`${r.new_points.toLocaleString()} new track points (added to trips already here)`);
          if (r.new_untimed) bits.push(`${r.new_untimed} saved ${r.new_untimed === 1 ? "track" : "tracks"} without times`);
          if (!bits.length) bits.push(r.points || r.untimed ? "nothing new: all of it was here already" : "no tracks in it");
          const has = [r.depth && "depth", r.wtemp && "water temperature"].filter(Boolean);
          return `${esc(r.file)}: ${bits.join(", ")}${has.length ? ` (with ${has.join(" and ")})` : ""}.`;
        }).join("<br>") || "Nothing to read in that.";
      } catch (err) {
        note = `<span class="warn-text">Upload failed: ${esc(err.message || err)}</span>`;
      }
      busy = false;
      cache.clear();
      await load();
    }

    async function load() {
      try {
        data = await api("api/logbook");
      } catch (err) {
        note = `<span class="warn-text">Couldn't read the logbook: ${esc(err.message || err)}</span>`;
      }
      if (shown && !data.trips.some((t) => t.id === shown.id)) hide(true);
      render();
    }

    // pointOnScreen: where a point of the shown trip is in the map's box (for tests).
    const pointOnScreen = (i) => shown && map.latLngToContainerPoint(shown.points[i].slice(0, 2));
    return { load, hide, pointOnScreen, get shown() { return shown?.id || null; } };
  }

  return {
    speedColour, colourRuns, profileSvg, nm, hours,
    create(opts) { return (this.last = create(opts)); },
  };
});
