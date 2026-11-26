/* Small panel helpers that stand on their own: Settings boxes that fold, the "Copy
   diagnostics" button, and the "What's new" card after an update. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  // --- Settings boxes fold; only Alerts starts open. Which are open is remembered. ---
  function folds() {
    let open;
    try { open = JSON.parse(store.get("fw-folds") || "null"); } catch { open = null; }
    if (!open || typeof open !== "object") open = { Alerts: true };
    document.querySelectorAll('[data-pane="settings"] > section.group').forEach((box) => {
      const head = box.querySelector(":scope > h3");
      if (!head) return;
      const name = head.textContent.trim();
      const set = (on) => {
        box.classList.toggle("folded", !on);
        head.setAttribute("aria-expanded", String(on));
      };
      head.classList.add("fold-head");
      head.tabIndex = 0;
      head.setAttribute("role", "button");
      set(Boolean(open[name]));
      const toggle = () => {
        open[name] = box.classList.contains("folded");
        set(open[name]);
        store.set("fw-folds", JSON.stringify(open));
      };
      head.addEventListener("click", toggle);
      head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
  }

  // --- Copy diagnostics ---
  function diagnostics() {
    const button = $("diag-copy"), result = $("diag-result"), box = $("diag-text");
    if (!button) return;
    button.addEventListener("click", async () => {
      result.textContent = "Collecting…";
      let text;
      try {
        const res = await fetch("api/diagnostics");
        if (!res.ok) throw new Error(await res.text());
        text = await res.text();
      } catch (err) {
        result.textContent = `Couldn't collect them: ${err.message || err}`;
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        box.hidden = true;
        result.textContent = "Copied. Paste it into a message.";
      } catch {
        // No clipboard here (Home Assistant's frame may not allow it): show it to copy by hand.
        box.value = text;
        box.hidden = false;
        box.focus();
        box.select();
        result.textContent = "Select all of it and copy.";
      }
    });
  }

  // --- What's new after an update ---
  function bullets(lines) {
    const items = [];
    for (const line of lines) {
      const m = /^(\s*)- (.*)$/.exec(line);
      if (m) items.push({ sub: m[1].length >= 2, text: m[2] });
      else if (items.length) items[items.length - 1].text += ` ${line.trim()}`;
      else items.push({ sub: false, text: line.trim() });
    }
    return items.map((it) => `<li${it.sub ? ' class="sub"' : ""}>${esc(it.text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>")}</li>`).join("");
  }
  async function whatsNew() {
    const seen = store.get("fw-seen-version");
    let data;
    try {
      const res = await fetch(`api/changelog${seen ? `?since=${encodeURIComponent(seen)}` : ""}`);
      if (!res.ok) return;
      data = await res.json();
    } catch { return; }
    if (!seen || seen === data.version || !data.sections?.length) {
      store.set("fw-seen-version", data.version);
      return;
    }
    const card = document.createElement("section");
    card.className = "whats-new panel";
    card.setAttribute("aria-label", "What's new");
    card.innerHTML = `<header><strong>What's new in Ferry Watch</strong>
        <button type="button" class="icon-button plain" aria-label="Close"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></header>
      <div class="whats-new-body">${data.sections.map((s) => `<h4>${esc(s.version)}</h4><ul>${bullets(s.lines)}</ul>`).join("")}</div>`;
    card.querySelector("button").addEventListener("click", () => {
      store.set("fw-seen-version", data.version);
      card.remove();
    });
    document.body.append(card);
  }

  folds();
  diagnostics();
  whatsNew();
})();
