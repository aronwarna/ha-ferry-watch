/* Preferences that follow you between the phone and the desktop. The panel keeps what is
   switched on in the browser's own storage (localStorage, keys fw-…), which each browser
   has for itself. The NAS keeps a copy of the shared ones: the page arrives with it
   (window.FW_PREFS), this puts it in place before the other scripts read their settings,
   and every later change is sent back. Things that belong to one device stay there. */
(() => {
  "use strict";
  const SHARED = new Set(["fw-aerial", "fw-chart-style", "fw-contours", "fw-faults", "fw-harbours", "fw-heritage",
    "fw-reserves-all", "fw-route-boat", "fw-route-draught", "fw-route-speed", "fw-route-mode", "fw-sky-place",
    "fw-sky-tips", "fw-stations", "fw-traffic", "fw-folds"]);
  const shared = (key) => typeof key === "string" && (SHARED.has(key) || key.startsWith("fw-ov-") || key.startsWith("fw-stats-"));
  let ls;
  try { ls = window.localStorage; ls.getItem("fw-tab"); } catch { return; }
  const saved = window.FW_PREFS && typeof window.FW_PREFS === "object" ? window.FW_PREFS : {};
  const rawSet = Storage.prototype.setItem, rawRemove = Storage.prototype.removeItem;
  const pending = {};
  let timer = 0;

  function flush() {
    timer = 0;
    const body = JSON.stringify(pending);
    for (const key of Object.keys(pending)) delete pending[key];
    if (body === "{}") return;
    fetch("api/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true })
      .catch(() => { /* this device still has them; sent again with the next change */ });
  }
  function queue(key, value) {
    pending[key] = value;
    if (!timer) timer = setTimeout(flush, 800);
  }

  try {
    // What the NAS has wins here; what only this device has is handed to the NAS.
    for (const [key, value] of Object.entries(saved)) {
      if (shared(key) && typeof value === "string" && ls.getItem(key) !== value) rawSet.call(ls, key, value);
    }
    for (let i = 0; i < ls.length; i++) {
      const key = ls.key(i);
      if (shared(key) && !(key in saved)) queue(key, ls.getItem(key));
    }
    Storage.prototype.setItem = function (key, value) {
      rawSet.call(this, key, value);
      if (this === ls && shared(key)) queue(key, String(value));
    };
    Storage.prototype.removeItem = function (key) {
      rawRemove.call(this, key);
      if (this === ls && shared(key)) queue(key, null);
    };
    addEventListener("pagehide", () => { if (timer) { clearTimeout(timer); flush(); } });
  } catch { /* storage switched off: nothing to share */ }
})();
