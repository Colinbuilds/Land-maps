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
    { href: "/pricing", label: "Pricing", icon: "💲" },
    { href: "/crm", label: "CRM", icon: "📋" },
    { href: "/join", label: "Membership", icon: "⭐", cls: "member" },
    { href: "#menu", label: "Menu", icon: "☰", cls: "menu-btn" },
    { href: "/signin", label: "Sign in", icon: "👤", cls: "acct" },
  ];
  // Customer menu groups (desktop dropdowns; the phone "Menu" sheet)
  const T = (k) => "/?trade=" + k + "#zipbox";
  const groups = owner ? [] : [
    { label: "Services", items: [
      [T("lawn"), "Lawn care & fertilizer", "Grass-only lawn and parkway per home"],
      [T("irrigation"), "Sprinklers & irrigation", "Lawn plus parkway to size and price systems"],
      [T("snow"), "Snow removal", "Driveway and public sidewalk square feet"],
      [T("concrete"), "Concrete & sealing", "Driveway, patio and walk areas"],
      [T("siding"), "Siding & windows", "Wall area and an estimated window count"],
      [T("pressure"), "Pressure washing", "Flatwork plus house-wall area"],
    ] },
    { label: "How it works", items: [
      ["/how-it-works", "How it works", "From a ZIP code to customers in three steps"],
      ["/sample", "Sample data & postcard", "Real measured rows and a finished postcard"],
      ["/how-it-works#accuracy", "How we measure", "Aerial photos, LiDAR and county records"],
      ["/#try", "Try one address free", "A full report on a home you know"],
      ["/faq", "Questions", "Straight answers about the data and buying"],
    ] },
    { label: "Pricing", items: [
      ["/pricing", "Pricing", "Pay per home, $25 minimum, exact price up front"],
      ["/pricing#member-value", "What members save", "Half off every list, worked out on a real ZIP"],
      ["/pricing#savings", "Savings calculator", "Your hours and miles back, in dollars"],
      ["/join", "Membership", "Unlimited reports, the CRM and half-off lists"],
    ] },
    { label: "Tools", items: [
      ["/reports", "Property reports", "Measure any address: roof, walls and yard"],
      ["/crm", "CRM", "Customers, estimates, scheduling and invoices"],
      ["/", "Buy ZIP data", "Search a ZIP or draw an area"],
    ] },
  ];
  const pathOf = (h) => (h.split("#")[0].split("?")[0] || "/");
  const groupActive = (g) => g.items.some(([h]) => !/[#?]/.test(h) && pathOf(h) !== "/" && (here === pathOf(h) || here.startsWith(pathOf(h) + "/")));
  const tabNow = (location.hash || "#status").slice(1);
  const isActive = (h) => (h === "/" ? here === "/" : h.startsWith("/command#") ? h.slice(9) === tabNow
    : here === h || here.startsWith(h + "/") || (h === "/reports" && here.startsWith("/r/")) || (h === "/" && here === "/data"));
  const build = () => {
    const header = document.querySelector("header.top");
    if (header && !header.querySelector(".site-nav")) {
      const nav = document.createElement("nav");
      nav.className = "site-nav";
      nav.setAttribute("aria-label", "Site");
      if (owner) nav.innerHTML = links.map((l) => `<a href="${l.href}" class="${isActive(l.href) ? "active" : ""} ${l.cls || ""}">${l.label}</a>`).join("");
      else {
        nav.innerHTML = groups.map((g, i) => `<div class="dd">
            <button type="button" class="dd-btn ${groupActive(g) ? "active" : ""}" aria-expanded="false" aria-controls="dd${i}">${g.label}<span aria-hidden="true">▾</span></button>
            <div class="dd-menu" id="dd${i}">${g.items.map(([h, t, d]) => `<a href="${h}"><b>${t}</b><small>${d}</small></a>`).join("")}</div>
          </div>`).join("")
          + `<a href="/join" class="member ${isActive("/join") ? "active" : ""}">Membership</a><a href="/signin" class="acct ${isActive("/signin") ? "active" : ""}">Sign in</a>`;
        const close = (except) => nav.querySelectorAll(".dd.open").forEach((d) => { if (d !== except) { d.classList.remove("open"); d.querySelector(".dd-btn").setAttribute("aria-expanded", "false"); } });
        nav.querySelectorAll(".dd-btn").forEach((b) => b.addEventListener("click", (e) => {
          e.stopPropagation();
          const d = b.parentElement, on = !d.classList.contains("open");
          close(d); d.classList.toggle("open", on); b.setAttribute("aria-expanded", on ? "true" : "false");
        }));
        document.addEventListener("click", () => close());
        document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
        // a menu link to a section of this same page: close the menu
        nav.querySelectorAll(".dd-menu a").forEach((a) => a.addEventListener("click", () => close()));
      }
      header.insertBefore(nav, header.children[1] || null);
    }
    // the account button: "Sign in", or who's signed in (owner -> command center)
    if (!owner) {
      const acct = () => document.querySelectorAll(".site-nav a.acct, .tabbar a.acct");
      let ownerKey = "";
      try { ownerKey = localStorage.getItem("adminToken") || ""; } catch (e) {}
      fetch("/api/roof/me", { credentials: "same-origin" }).then((r) => r.ok ? r.json() : null).then((me) => {
        acct().forEach((a) => {
          const lbl = a.querySelector("span") ? a.lastChild : a;
          if (me && me.name) { a.href = me.crm ? "/crm" : "/reports"; lbl.textContent = me.name.length > 18 ? me.name.slice(0, 17) + "…" : me.name; a.title = "Signed in"; }
          else if (ownerKey) { a.href = "/command"; lbl.textContent = "Command"; a.title = "Owner command center"; }
        });
      }).catch(() => {});
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
      bar.innerHTML = links.map((l) => `<a href="${l.href}" class="${l.href[0] !== "#" && isActive(l.href) ? "active" : ""} ${l.cls || ""}"><span aria-hidden="true">${l.icon}</span>${l.label}</a>`).join("");
      document.body.appendChild(bar);
      if (groups.length) {  // phones: every menu group in one sheet
        const sh = document.createElement("div");
        sh.className = "menu-sheet"; sh.hidden = true;
        sh.innerHTML = `<div class="ms-card" role="dialog" aria-label="Menu"><button type="button" class="ms-x" aria-label="Close">×</button>
          ${groups.map((g) => `<h4>${g.label}</h4>${g.items.map(([h, t]) => `<a href="${h}">${t}</a>`).join("")}`).join("")}</div>`;
        document.body.appendChild(sh);
        const mb = bar.querySelector("a.menu-btn");
        mb.addEventListener("click", (e) => { e.preventDefault(); sh.hidden = false; });
        sh.addEventListener("click", (e) => { if (e.target === sh || e.target.closest(".ms-x") || e.target.closest("a")) sh.hidden = true; });
      }
    }
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();


// "What you get": membership and the CRM, from the live prices. Shown where
// signed-out visitors land (Reports, CRM). focus: "reports" | "crm".
window.planInfo = async function (el, focus) {
  if (!el) return;
  let P = { member_price_cents: 11500, credit_price_cents: 500, included_trades: 2, list_discount: 0.5, annual_discount: 0.25, crm_price_cents: 0 };
  try { P = Object.assign(P, await api("/api/billing/plans")); } catch (e) {}
  const usd = (c) => "$" + (c / 100).toLocaleString(undefined, { maximumFractionDigits: c % 100 ? 2 : 0 });
  const crm = [
    ["Estimates in one tap", "Every home you measure becomes a customer with an estimate already priced from your rates. Text the link; they accept online."],
    ["Scheduling", "Weekly, every-other-week or one-off jobs, in stop order, checked off from your phone."],
    ["Job photos & invoices", "Before/after photos on each job; mark it done and the invoice is made."],
    ["Your money at a glance", "Paid this month, what's owed, what's booked, and your win rate."],
  ];
  const member = [
    [`Unlimited property reports`, `For your trades (${P.included_trades || 2} included; more trades add a little). Pay-as-you-go is ${usd(P.credit_price_cents)} a report.`],
    [`The CRM, included`, (P.crm_price_cents ? `${usd(P.crm_price_cents)}/month add-on.` : "No extra charge for members.") + " Lists you buy drop straight in, every home already estimated at your rates."],
    [`${Math.round((P.list_discount || 0.5) * 100)}% off ZIP data`, "Every ZIP sheet, drawn-area list and Premier folder at half price."],
    ["Postcards for any home", "Print-ready cards with the home from above, its measurements and your price."],
  ];
  const li = (rows) => rows.map(([b, t]) => `<li><b>${esc(b)}</b><span>${esc(t)}</span></li>`).join("");
  el.innerHTML = `<div class="planinfo">
    <h2>${focus === "crm" ? "What the CRM does" : "What you get with membership"}</h2>
    ${focus === "crm" ? `<ul>${li(crm)}</ul><h3>Comes with membership</h3>` : ""}
    <ul>${li(focus === "crm" ? member.filter((m) => !/CRM/.test(m[0])) : member)}</ul>
    ${focus === "crm" ? "" : `<h3>The CRM, included</h3><ul>${li(crm.slice(0, 2))}</ul>`}
    <div class="pi-price"><b>${usd(P.member_price_cents)}</b> a month · ${Math.round((P.annual_discount || 0.25) * 100)}% off paid yearly · cancel any time</div>
    <a class="btn" href="/join">See membership</a>
  </div>`;
  if (!document.getElementById("planinfo-css")) {
    const st = document.createElement("style"); st.id = "planinfo-css";
    st.textContent = `.planinfo{background:#fff;border-radius:18px;padding:18px 20px;box-shadow:0 1px 2px rgba(0,0,0,.06);border:2px solid #bbe5c8}
      .planinfo h2{font-size:20px;margin:0 0 10px}.planinfo h3{font-size:15px;margin:14px 0 6px;color:#166534}
      .planinfo ul{list-style:none;margin:0;padding:0;display:grid;gap:8px}
      .planinfo li{display:grid;gap:1px;padding-left:22px;position:relative;font-size:14.5px}
      .planinfo li::before{content:"✓";position:absolute;left:0;top:0;color:#16a34a;font-weight:800}
      .planinfo li span{color:#55665c}.planinfo .pi-price{margin:14px 0 10px;font-size:15px}
      .planinfo .pi-price b{font-size:22px}.planinfo .btn{display:inline-flex;border-radius:99px}`;
    document.head.appendChild(st);
  }
};
