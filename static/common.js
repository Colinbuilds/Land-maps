// Shared helpers: map with county imagery, clickable parcels, address autocomplete.
const fmt = (n) => (n == null || n === "" ? "–" : Math.round(+n).toLocaleString());
const money = (n) => "$" + Math.round(+n).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const tok = localStorage.getItem("adminToken");
  if (tok) headers["X-Admin-Token"] = tok;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    // a stored admin key the server rejects (e.g. after changing it): forget it,
    // so the page asks again instead of repeating it into a lockout
    if (r.status === 401 && tok && /admin token/i.test(String(msg))) { try { localStorage.removeItem("adminToken"); } catch (e) {} }
    if (Array.isArray(msg)) msg = msg.map(friendlyError).join(" ");
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}

// Turn a server validation error into a sentence a person can act on.
function friendlyError(e) {
  const field = { name: "Company name", email: "Email", password: "Password", credits: "Credits",
    phone: "Phone", zip: "ZIP code" }[(e.loc || []).slice(-1)[0]] || (e.loc || []).slice(-1)[0] || "A field";
  if (e.type === "string_too_short") return `${field} must be at least ${e.ctx?.min_length} characters.`;
  if (e.type === "string_too_long") return `${field} is too long.`;
  if (e.type === "missing") return `${field} is required.`;
  if (/int|number/.test(e.type || "")) return `${field} must be a whole number.`;
  return `${field}: ${e.msg}`;
}

const MEASURE_COLORS = {
  lawn_sqft: "var(--lawn)", driveway_sqft: "var(--drive)", sidewalk_sqft: "var(--walk)",
  other_hardscape_sqft: "var(--other)", structures_sqft: "var(--struct)", beds_sqft: "var(--beds)",
  parkway_sqft: "var(--lawn)", grass_total: "var(--lawn)", building_sqft: "var(--bldg)", lot_sqft: "#cbd5e1", frontage_ft: "#94a3b8",
  tree_count: "var(--tree)", tree_canopy_sqft: "var(--tree)", lawn_under_trees_sqft: "var(--tree)",
  wall_sqft: "var(--bldg)", est_windows: "#0ea5e9",
};

function makeMap(el, cfg) {
  // Whole-region basemap, with each county's own (sharper, newer) photos
  // layered on top inside that county.
  const map = L.map(el, { zoomControl: true, maxZoom: 22 });
  const base = cfg.basemap || { tile_url: cfg.imagery.tile_url, max_native_zoom: cfg.imagery.max_native_zoom || 19 };
  L.tileLayer(base.tile_url, { maxNativeZoom: base.max_native_zoom || 19, maxZoom: 22,
    attribution: base.attribution || "" }).addTo(map);
  (cfg.imagery_layers || []).forEach((l) => {
    L.tileLayer(l.tile_url, {
      bounds: l.bounds, maxNativeZoom: l.max_native_zoom || 21, maxZoom: 22, minZoom: 13,
      attribution: esc(l.source || l.name),
    }).addTo(map);
  });
  if (cfg.region && cfg.region.bounds) map.fitBounds(cfg.region.bounds);
  else map.setView(cfg.county.center, 11);
  return map;
}

// Loads parcel outlines for the current view; calls onPick(feature) on click.
function parcelLayer(map, onPick) {
  const style = { color: "#fff", weight: 1.2, fillOpacity: 0.02, opacity: 0.85 };
  const layer = L.geoJSON(null, {
    style: () => style,
    onEachFeature: (f, l) => {
      l.on("mouseover", () => l.setStyle({ weight: 3, color: "#facc15" }));
      l.on("mouseout", () => { if (l !== state.selected && l !== state.suggested) l.setStyle(style); });
      l.on("click", () => onPick(f, l));
      if (f.properties.address) l.bindTooltip(esc(f.properties.address), { sticky: true });
    },
  }).addTo(map);
  const state = { selected: null, suggested: null, byPin: {} };
  let seq = 0;
  async function load() {
    if (map.getZoom() < 17) { layer.clearLayers(); state.byPin = {}; return; }
    const b = map.getBounds();
    const my = ++seq;
    try {
      const fc = await api(`/api/parcels?bbox=${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`);
      if (my !== seq) return;
      const keepSel = state.selected && state.selected.feature.properties.pin;
      const keepSug = state.suggested && state.suggested.feature.properties.pin;
      layer.clearLayers(); state.byPin = {};
      layer.addData(fc);
      layer.eachLayer((l) => { state.byPin[l.feature.properties.pin] = l; });
      if (keepSug && state.byPin[keepSug]) highlightSuggested(keepSug);
      if (keepSel && state.byPin[keepSel]) select(keepSel);
    } catch (e) { /* zoomed out or server busy */ }
  }
  function highlightSuggested(pin) {
    const l = state.byPin[pin];
    if (!l) return;
    state.suggested = l;
    l.setStyle({ color: "#facc15", weight: 3, dashArray: "6 5", fillOpacity: 0.08 });
  }
  function select(pin) {
    if (state.selected) state.selected.setStyle(style);
    const l = state.byPin[pin];
    state.selected = l || null;
    if (l) { l.setStyle({ color: "#ffffff", weight: 3.5, dashArray: null, fillOpacity: 0 }); l.bringToFront(); }
  }
  map.on("moveend", load);
  return { layer, load, highlightSuggested, select, state };
}

function wireAutocomplete(input, box, onChoose) {
  let t, items = [], active = -1;
  const close = () => { box.classList.add("hidden"); active = -1; };
  const render = () => {
    box.innerHTML = items.map((s, i) => `<div data-i="${i}" class="${i === active ? "active" : ""}">${esc(s.text)}</div>`).join("");
    box.classList.toggle("hidden", !items.length);
  };
  input.addEventListener("input", () => {
    clearTimeout(t);
    const q = input.value.trim();
    if (q.length < 3) { items = []; render(); return; }
    t = setTimeout(async () => {
      try { items = await api(`/api/suggest?q=${encodeURIComponent(q)}`); } catch (e) { items = []; }
      active = -1; render();
    }, 200);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); render(); e.preventDefault(); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const s = items[active >= 0 ? active : 0];
      close();
      onChoose(s || { text: input.value });
    } else if (e.key === "Escape") close();
  });
  box.addEventListener("mousedown", (e) => {
    const d = e.target.closest("[data-i]");
    if (!d) return;
    const s = items[+d.dataset.i];
    input.value = s.text; close(); onChoose(s);
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

// Shows the measurement overlay PNG returned by /api/measure.
function showOverlay(map, holder, r) {
  if (holder.overlay) map.removeLayer(holder.overlay);
  holder.overlay = null;
  if (r && r.overlay) {
    holder.overlay = L.imageOverlay(r.overlay.png, r.overlay.bounds, { opacity: 0.9, interactive: false }).addTo(map);
  }
}

function measurementCards(r, keys, labels) {
  return keys.map((k) => `
    <div class="m"><div class="k"><i class="sw" style="background:${MEASURE_COLORS[k] || "#999"}"></i>${esc(labels[k] || k)}</div>
    <div class="v">${fmt(r[k])} <span class="small muted">${k.endsWith("_ft") ? "ft" : (k.endsWith("_count") || k.startsWith("est_")) ? "" : "sq ft"}</span></div></div>`).join("");
}

// ---------------------------------------------------------------- site menu
// Two sites: the owner's command center, and the customer side (companies
// that buy reports, ZIP data and the CRM). Header links, and a bottom tab
// bar on phones.
(function siteMenu() {
  const here = location.pathname.replace(/\/+$/, "") || "/";
  const owner = /^\/(command|business|demo)(\/|$)/.test(here);
  // The command center is one page (/command) with a tab per section; the
  // other owner pages open inside it. Opened on their own, they forward there.
  const OWNER_TAB = { "/command/data": "data", "/command/postcards": "postcards", "/command/tools": "measure",
    "/command/settings": "settings", "/demo": "demo", "/business": "measure", "/command/safety": "safety", "/command/coverage": "coverage", "/command/map": "map" };
  let embedded = false;
  try { embedded = window.top !== window.self; } catch (e) { embedded = true; }
  if (owner && here !== "/command") {
    if (!embedded) { location.replace("/command#" + (OWNER_TAB[here] || "status")); return; }
    const st = document.createElement("style");  // inside the command center: its own menu is enough
    st.textContent = "header.top, .tabbar, .site-nav { display: none !important; } body { padding-bottom: 0 !important; }";
    document.documentElement.classList.add("embedded");
    document.head.appendChild(st);
    return;
  }
  const links = owner ? [
    { href: "/command#status", label: "Live", icon: "📡" },
    { href: "/command#companies", label: "Companies", icon: "🏢" },
    { href: "/command#map", label: "Map", icon: "🗺️" },
    { href: "/command#data", label: "ZIP data", icon: "🗂️" },
    { href: "/command#coverage", label: "Coverage", icon: "🌎" },
    { href: "/command#postcards", label: "Postcards", icon: "✉️" },
    { href: "/command#measure", label: "Measure", icon: "📏" },
    { href: "/command#demo", label: "Demo", icon: "🧪" },
    { href: "/command#safety", label: "Safety", icon: "🛡️" },
    { href: "/command#settings", label: "Settings", icon: "⚙️" },
  ] : [
    { href: "/", label: "Home", icon: "🏠" },
    { href: "/reports", label: "Reports", icon: "📐" },
    { href: "/crm", label: "CRM", icon: "📋" },
    { href: "/join", label: "Membership", icon: "⭐", cls: "member" },
  ];
  const tabNow = (location.hash || "#status").slice(1);
  const isActive = (h) => (h === "/" ? here === "/" : h.startsWith("/command#") ? h.slice(9) === tabNow
    : here === h || here.startsWith(h + "/") || (h === "/reports" && here.startsWith("/r/")) || (h === "/" && here === "/data"));
  const build = () => {
    const header = document.querySelector("header.top");
    if (header && !header.querySelector(".site-nav")) {
      const nav = document.createElement("nav");
      nav.className = "site-nav";
      nav.setAttribute("aria-label", "Site");
      nav.innerHTML = links.map((l) => `<a href="${l.href}" class="${isActive(l.href) ? "active" : ""} ${l.cls || ""}">${l.label}</a>`).join("");
      header.insertBefore(nav, header.children[1] || null);
    }
    // the logo is always the way home
    const brand = header && header.querySelector(".brand");
    if (brand && brand.tagName !== "A") {
      const a = document.createElement("a");
      a.className = brand.className;
      a.href = owner ? "/command" : "/";
      a.setAttribute("aria-label", "Home");
      a.innerHTML = brand.innerHTML;
      if (brand.id) a.id = brand.id;
      brand.replaceWith(a);
    }
    if (!owner && !document.querySelector(".site-foot")) {  // legal links on every customer page
      const f = document.createElement("footer");
      f.className = "site-foot";
      f.style.cssText = "text-align:center;font-size:13px;color:#64748b;padding:18px 16px 96px";
      f.innerHTML = '<a href="/terms" style="color:inherit">Terms</a> · <a href="/privacy" style="color:inherit">Privacy</a> · <a href="/refunds" style="color:inherit">Refunds</a>';
      document.body.appendChild(f);
    }
    if (!document.querySelector(".tabbar")) {
      const bar = document.createElement("nav");
      bar.className = "tabbar" + (links.length > 6 ? " many" : "");
      bar.setAttribute("aria-label", "Site");
      bar.innerHTML = links.map((l) => `<a href="${l.href}" class="${isActive(l.href) ? "active" : ""} ${l.cls || ""}"><span aria-hidden="true">${l.icon}</span>${l.label}</a>`).join("");
      document.body.appendChild(bar);
    }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
