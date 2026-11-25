// Business dashboard.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await api("/api/config");
  document.documentElement.style.setProperty("--accent", cfg.business.accent || "#16a34a");
  const LABELS = cfg.measurements;
  const NUM_KEYS = Object.keys(LABELS);

  // ---------------- tabs
  let mapInit = null;
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll("main > section").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + b.dataset.tab));
    if (b.dataset.tab === "measure") { if (!mapInit) mapInit = initMeasure(); else mapInit.map.invalidateSize(); }
    if (b.dataset.tab === "leads") loadLeads();
    if (b.dataset.tab === "pricing") loadSettings();
    if (b.dataset.tab === "roofing") loadRoofing();
    if (b.dataset.tab === "datasales") loadData();
  }));
  // the command-center path picks which tools show (companies live on /command)
  const GROUPS = { "/command/data": ["datasales"], "/command/tools": ["bulk", "measure", "leads"], "/command/settings": ["pricing"] };
  const TITLES = { "/command/data": "ZIP data", "/command/tools": "Measure tools", "/command/settings": "Settings" };
  const path = location.pathname.replace(/\/+$/, "");
  const group = GROUPS[path] || ["bulk", "measure", "leads", "pricing", "datasales"];
  document.querySelectorAll("#tabs button").forEach((b) => { if (!group.includes(b.dataset.tab)) b.remove(); });
  if (group.length === 1) $("tabs").classList.add("hidden");
  if (TITLES[path]) { document.title = TITLES[path]; const h = document.querySelector("header.top .brand span:last-child"); if (h) h.textContent = TITLES[path]; }
  document.querySelector(`#tabs button[data-tab="${group[0]}"]`).click();

  // ---------------- bulk
  $("bMeasures").innerHTML = NUM_KEYS.map((k) => `<label><input type="checkbox" value="${k}" ${k === "lot_sqft" || k === "frontage_ft" ? "" : "checked"}> ${esc(LABELS[k])}</label>`).join("");
  const bulkReq = () => ({
    zip: $("bZip").value.trim(),
    classes: [...$("bClasses").querySelectorAll("input:checked")].map((i) => i.value),
    measures: [...$("bMeasures").querySelectorAll("input:checked")].map((i) => i.value),
    limit: $("bLimit").value ? +$("bLimit").value : null,
    include_owner: $("bOwner").checked,
  });
  $("bPreview").addEventListener("click", async () => {
    try {
      const r = await api("/api/admin/bulk/preview", { method: "POST", json: bulkReq() });
      const mins = Math.max(1, Math.round(r.count * 0.6 / 60));
      const by = Object.entries(r.by_county || {}).map(([k, v]) => `${esc(k)}: ${fmt(v)}`).join(" · ");
      $("bMsg").innerHTML = `<b>${fmt(r.count)}</b> parcels match${by ? ` (${by})` : ""}. Estimated run time: about ${mins} min.`;
    } catch (e) { $("bMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
  $("bStart").addEventListener("click", async () => {
    try {
      const j = await api("/api/admin/bulk", { method: "POST", json: bulkReq() });
      $("bMsg").innerHTML = `<span class="ok">Started job ${esc(j.id)}.</span>`;
      viewing = j.id;
      pollJobs();
    } catch (e) { $("bMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });

  let viewing = null, pollTimer = null;
  async function pollJobs() {
    clearTimeout(pollTimer);
    let jobs = [];
    try { jobs = await api("/api/admin/jobs"); } catch (e) { $("jobs").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
    $("jobs").innerHTML = jobs.slice(0, 12).map((j) => {
      const pct = j.total ? Math.round((100 * j.done) / j.total) : 0;
      return `<div style="cursor:pointer" data-job="${esc(j.id)}">
        <div style="display:flex;justify-content:space-between;gap:8px"><b>ZIP ${esc(j.zip)}</b>
        <span class="small muted">${esc(j.status)} · ${fmt(j.done)}/${fmt(j.total)}${j.errors ? ` · ${j.errors} errors` : ""}</span></div>
        <div class="progress"><div style="width:${pct}%"></div></div></div>`;
    }).join("") || '<p class="muted">No jobs yet.</p>';
    $("jobs").querySelectorAll("[data-job]").forEach((d) => d.addEventListener("click", () => { viewing = d.dataset.job; showJob(); }));
    if (!viewing && jobs[0]) viewing = jobs[0].id;
    if (viewing) showJob(jobs.find((j) => j.id === viewing));
    if (jobs.some((j) => !j.finished)) pollTimer = setTimeout(pollJobs, 3000);
  }
  async function showJob(j) {
    if (!j) j = await api(`/api/admin/jobs/${viewing}`);
    $("rowsTitle").textContent = `Results: ZIP ${j.zip} (${fmt(j.done)} of ${fmt(j.total)})`;
    const tok = localStorage.getItem("adminToken");
    $("csvLink").href = `/api/admin/jobs/${j.id}/csv${tok ? `?token=${encodeURIComponent(tok)}` : ""}`;
    $("csvLink").classList.remove("hidden");
    $("totals").innerHTML = j.measures.map((k) => `<div class="m"><div class="k"><i class="sw" style="background:${MEASURE_COLORS[k]}"></i>Total ${esc(LABELS[k])}</div><div class="v">${fmt(j.totals[k])}</div></div>`).join("");
    const rows = await api(`/api/admin/jobs/${j.id}/rows?limit=300`);
    const cols = ["address", ...j.measures, "driveway_method"];
    $("rows").innerHTML = `<thead><tr>${cols.map((c) => `<th class="${NUM_KEYS.includes(c) ? "num" : ""}">${esc(LABELS[c] || c.replace(/_/g, " "))}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="${NUM_KEYS.includes(c) ? "num" : ""}">${NUM_KEYS.includes(c) ? fmt(r[c]) : esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody>`;
  }
  pollJobs();

  // ---------------- measure tool
  function initMeasure() {
    const map = makeMap("map", cfg);
    const holder = {};
    const parcels = parcelLayer(map, (f) => pick(f.properties.pin));
    let curPin = null;
    const fix = { exclude: [], include: [], group: new L.FeatureGroup().addTo(map) };
    const fixStyle = { exclude: { color: "#ef4444", weight: 2, fillOpacity: 0.25 }, include: { color: "#22c55e", weight: 2, fillOpacity: 0.25 } };
    function drawFix() {
      fix.group.clearLayers();
      ["exclude", "include"].forEach((k) => fix[k].forEach((g) => L.geoJSON(g, { style: fixStyle[k] }).addTo(fix.group)));
      const area = (g) => L.GeometryUtil.geodesicArea(g.coordinates[0].map(([x, y]) => L.latLng(y, x))) * 10.7639;
      $("fixList").innerHTML = [...fix.exclude.map((g) => `− ${fmt(area(g))} sq ft removed`), ...fix.include.map((g) => `+ ${fmt(area(g))} sq ft added`)].join("<br>") || "No corrections.";
    }
    let fixMode = null;
    const startFix = (mode) => {
      fixMode = mode;
      new L.Draw.Polygon(map, { shapeOptions: fixStyle[mode], allowIntersection: false }).enable();
    };
    $("fixExclude").addEventListener("click", () => startFix("exclude"));
    $("fixInclude").addEventListener("click", () => startFix("include"));
    $("fixClear").addEventListener("click", () => { fix.exclude = []; fix.include = []; drawFix(); });
    $("fixSave").addEventListener("click", async () => {
      if (!curPin) return;
      try {
        await api(`/api/admin/corrections/${curPin}`, { method: "PUT", json: { exclude: fix.exclude, include: fix.include } });
        $("fixMsg").innerHTML = '<span class="ok">Saved.</span>';
        pick(curPin);
      } catch (e) { $("fixMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    });

    async function pick(pin) {
      parcels.select(pin);
      curPin = pin;
      $("fixCard").classList.remove("hidden");
      try {
        const c = await api(`/api/admin/corrections/${pin}`);
        fix.exclude = c.exclude || []; fix.include = c.include || [];
      } catch (e) { fix.exclude = []; fix.include = []; }
      drawFix();
      $("mResult").innerHTML = '<span class="spinner"></span>Measuring…';
      try {
        const r = await api(`/api/measure/${encodeURIComponent(pin)}`);
        showOverlay(map, holder, r);
        $("mResult").innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline"><h2 style="margin:0">${esc(r.address)}</h2><span class="tag">${esc(r.pin)} · ${esc((r.sources && r.sources.county) || "")}</span></div>
          <p class="small muted">${esc(r.city || "")} ${esc(r.zip || "")} · class ${esc(r.class)} · ${esc(r.assessor.building || "")} ${r.assessor.year_built ? "built " + esc(r.assessor.year_built) : ""} · ${fmt(r.assessor.living_sqft)} sq ft living
          ${r.sources.driveway_method !== "imagery" ? ` · <span class="tag warn">driveway ${esc(r.sources.driveway_method)}</span>` : ""}
          ${r.sources.corrected ? ' · <span class="tag">hand-corrected</span>' : ""}
          ${r.sources.lidar_year ? "" : ' · <span class="tag warn">no LiDAR here</span>'}</p>
          <div class="meas">${measurementCards(r, NUM_KEYS, LABELS)}</div>
          <h2>Customer would see</h2>
          ${Object.values(r.estimate).map((e) => `<div class="svc"><div class="name">${esc(e.label)}</div><div class="price">${money(e.low)}–${money(e.high)}<small>${esc(e.unit)}</small></div></div>`).join("")}`;
      } catch (e) { $("mResult").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    }
    wireAutocomplete($("mAddr"), $("mSuggest"), async (s) => {
      const q = new URLSearchParams({ q: s.text });
      if (s.magicKey) q.set("magicKey", s.magicKey);
      try {
        const loc = await api(`/api/locate?${q}`);
        map.setView([loc.best.lat, loc.best.lon], 19);
        await parcels.load();
        if (loc.suggested_pin) pick(loc.suggested_pin);
      } catch (e) { alert(e.message); }
    });

    // free-hand area measuring
    const drawn = new L.FeatureGroup().addTo(map);
    map.addControl(new L.Control.Draw({
      edit: { featureGroup: drawn },
      draw: { polygon: { showArea: true, allowIntersection: false }, rectangle: { showArea: true },
              polyline: { metric: false, feet: true }, circle: false, marker: false, circlemarker: false },
    }));
    const listDrawn = () => {
      const items = [];
      drawn.eachLayer((l) => {
        if (l instanceof L.Polygon) {
          const sqft = L.GeometryUtil.geodesicArea(l.getLatLngs()[0]) * 10.7639;
          items.push(`Area: <b>${fmt(sqft)} sq ft</b>`);
          l.bindTooltip(`${fmt(sqft)} sq ft`, { permanent: true, direction: "center", className: "parcel-label" });
        } else if (l instanceof L.Polyline) {
          const ll = l.getLatLngs(); let m = 0;
          for (let i = 1; i < ll.length; i++) m += ll[i - 1].distanceTo(ll[i]);
          items.push(`Line: <b>${fmt(m * 3.28084)} ft</b>`);
          l.bindTooltip(`${fmt(m * 3.28084)} ft`, { permanent: true, className: "parcel-label" });
        }
      });
      $("drawn").innerHTML = items.length ? items.join("<br>") : "None yet.";
    };
    map.on(L.Draw.Event.CREATED, (e) => {
      if (fixMode) {
        fix[fixMode].push(e.layer.toGeoJSON().geometry);
        fixMode = null;
        drawFix();
        return;
      }
      drawn.addLayer(e.layer); listDrawn();
    });
    map.on(L.Draw.Event.EDITED, listDrawn);
    map.on(L.Draw.Event.DELETED, listDrawn);
    return { map };
  }

  // ---------------- leads
  async function loadLeads() {
    try {
      const leads = await api("/api/admin/leads");
      $("leads").innerHTML = `<thead><tr><th>When</th><th>Name</th><th>Contact</th><th>Address</th><th>Services</th>
        <th class="num">Lawn</th><th class="num">Driveway</th><th class="num">Sidewalk</th><th>Notes</th></tr></thead><tbody>` +
        (leads.map((l) => `<tr><td>${new Date(l.ts * 1000).toLocaleString()}</td><td>${esc(l.name)}</td>
          <td>${esc(l.phone)}<br>${esc(l.email)}</td><td>${esc(l.measurements?.address || l.pin)}</td><td>${esc((l.services || []).join(", "))}</td>
          <td class="num">${fmt(l.measurements?.lawn_sqft)}</td><td class="num">${fmt(l.measurements?.driveway_sqft)}</td>
          <td class="num">${fmt(l.measurements?.sidewalk_sqft)}</td><td style="white-space:normal;min-width:200px">${esc(l.notes)}</td></tr>`).join("")
          || '<tr><td colspan="9" class="muted">No leads yet.</td></tr>') + "</tbody>";
    } catch (e) { $("leads").innerHTML = `<tr><td class="err">${esc(e.message)}</td></tr>`; }
  }

  // ---------------- roofing companies & credits
  async function loadRoofing() {
    try {
      const cos = await api("/api/admin/companies");
      $("companies").innerHTML = `<thead><tr><th>Company</th><th>Login</th><th class="num">Credits</th><th class="num">Reports</th><th>Add credits</th></tr></thead><tbody>${
        cos.map((c) => `<tr><td>${esc(c.name)}<div class="small muted">${esc(c.phone || "")}</div></td><td>${esc(c.email)}</td>
          <td class="num"><b>${c.credits}</b></td><td class="num">${c.reports}</td>
          <td><div class="row" style="flex-wrap:nowrap"><input type="number" style="width:90px" data-cid="${c.id}" placeholder="+10">
          <button class="secondary" data-add="${c.id}">Add</button></div></td></tr>`).join("") || '<tr><td colspan="5" class="muted">No companies yet.</td></tr>'}</tbody>`;
      $("companies").querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", async () => {
        const v = +$("companies").querySelector(`input[data-cid="${b.dataset.add}"]`).value;
        if (!v) return;
        await api(`/api/admin/companies/${b.dataset.add}/credits`, { method: "POST", json: { delta: v, reason: "added by owner" } });
        loadRoofing();
      }));
      const reps = await api("/api/admin/roof-reports");
      const tok = localStorage.getItem("adminToken");
      $("rReports").innerHTML = `<thead><tr><th>Date</th><th>Company</th><th>Address</th><th>Report</th></tr></thead><tbody>${
        reps.slice(0, 50).map((r) => `<tr><td>${new Date(r.created * 1000).toLocaleDateString()}</td><td>${esc(r.company)}</td><td>${esc(r.address)}</td>
          <td><a target="_blank" href="/r/${r.id}?t=${r.share_token}">${esc(r.id)}</a></td></tr>`).join("") || '<tr><td colspan="4" class="muted">None yet.</td></tr>'}</tbody>`;
    } catch (e) { $("companies").innerHTML = `<tr><td class="err">${esc(e.message)}</td></tr>`; }
  }
  $("cCreate").addEventListener("click", async () => {
    const miss = [["cName", "company name"], ["cEmail", "login email"], ["cPass", "password"]]
      .filter(([id]) => !$(id).value.trim()).map(([, l]) => l);
    if (miss.length) { $("cMsg").innerHTML = `<span class="err">Please fill in the ${miss.join(", ")}.</span>`; return; }
    if ($("cPass").value.length < 6) { $("cMsg").innerHTML = '<span class="err">The password must be at least 6 characters.</span>'; return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test($("cEmail").value.trim())) { $("cMsg").innerHTML = '<span class="err">That email doesn\'t look right.</span>'; return; }
    $("cMsg").innerHTML = '<span class="spinner"></span>Creating…';
    try {
      await api("/api/admin/companies", { method: "POST", json: { name: $("cName").value, email: $("cEmail").value,
        password: $("cPass").value, phone: $("cPhone").value, credits: +$("cCredits").value || 0 } });
      $("cMsg").innerHTML = '<span class="ok">Company created.</span>';
      ["cName", "cEmail", "cPass", "cPhone", "cCredits"].forEach((i) => ($(i).value = ""));
      loadRoofing();
    } catch (e) { $("cMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });

  // ---------------- data sales
  async function loadData() {
    let d;
    try { d = await api("/api/admin/data"); } catch (e) { $("dOrders").innerHTML = `<tr><td class="err">${esc(e.message)}</td></tr>`; return; }
    $("dRate").value = d.pricing.rate_cents; $("dExtra").value = d.pricing.extra_type_cents;
    $("dMin").value = d.pricing.min_order_usd; $("dPremier").value = d.pricing.premier_cents;
    $("dExFirst").value = d.pricing.exclusive_cents; $("dExExtra").value = d.pricing.exclusive_extra_cents;
    $("dExMonths").value = d.pricing.exclusive_months;
    const pa = d.prebuild_all || {};
    $("dPreAllMsg").innerHTML = pa.zips ? `${pa.running ? "<b>Running</b>" : esc(pa.status)}: ${pa.done.length} of ${pa.zips.length} ZIPs measured`
      + (pa.failed && pa.failed.length ? `, ${pa.failed.length} failed (${esc(pa.failed.join(", "))})` : "")
      + (pa.current ? `. Now: ${esc(pa.current)}${pa.current_progress && pa.current_progress.total ? ` (${pa.current_progress.done}/${pa.current_progress.total})` : ""}` : "")
      : "Measures all ZIPs in the counties we cover, one at a time, skipping any measured in the last year. Keeps going after a redeploy.";
    const day = (t) => new Date(t * 1000).toLocaleDateString();
    $("dLocks").innerHTML = `<thead><tr><th>ZIP</th><th>Measurement</th><th>Company</th><th>Until</th><th></th></tr></thead><tbody>${
      (d.locks || []).map((l) => `<tr><td>${esc(l.zip)}</td><td>${esc(l.type)}</td><td>${esc(l.email)}</td><td>${l.until * 1000 < Date.now() ? "expired " : ""}${day(l.until)}</td>
        <td><button class="secondary" data-unlock="${esc(l.zip)}/${esc(l.type)}">Release</button></td></tr>`).join("") || '<tr><td colspan="5" class="muted">None yet.</td></tr>'}</tbody>`;
    $("dLocks").querySelectorAll("[data-unlock]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm(`Release ${b.dataset.unlock}? Others can buy it again right away.`)) return;
      await api(`/api/admin/data/locks/${b.dataset.unlock}`, { method: "DELETE" }); loadData();
    }));
    const BANDS = ["<$250k", "$250-500k", "$500-750k", "$750k-1M", "$1-1.25M", "$1.25-1.5M", "$1.5-1.75M", "$1.75-2M", "$2M+"];
    const mult = d.pricing.value_multipliers || [1, 1.25, 1.5, 2, 2.5, 3, 3.5, 4, 5];
    $("dMult").innerHTML = BANDS.map((b, i) => `<label class="small" style="flex:1 1 90px">${b}<input type="number" step="0.05" min="0.1" data-m="${i}" value="${mult[i] ?? 1}"></label>`).join("");
    $("dStripe").innerHTML = d.stripe ? "Card payments: <b>on</b> (Stripe)."
      : "Card payments: <b>off</b>. Set STRIPE_SECRET_KEY in Railway to take cards. Until then, orders wait for you to invoice and click <i>Mark paid</i>.";
    const usd = (c) => "$" + (c / 100).toFixed(2);
    $("dOrders").innerHTML = `<thead><tr><th>Date</th><th>Order</th><th>Email</th><th>ZIP</th><th class="num">Properties</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead><tbody>${
      d.orders.map((o) => `<tr><td>${new Date(o.created * 1000).toLocaleDateString()}</td><td>${esc(o.id)}</td><td>${esc(o.email)}</td><td>${esc(o.zip)}</td>
        <td class="num">${fmt(o.count)}</td><td class="num">${usd(o.amount_cents)}</td><td>${esc(o.status.replace("_", " "))}</td>
        <td>${o.status === "awaiting_payment" ? `<button class="secondary" data-paid="${esc(o.id)}">Mark paid</button>` : ""}
            <a href="/data/order/${esc(o.id)}?t=${esc(o.token)}" target="_blank">open</a></td></tr>`).join("") || '<tr><td colspan="8" class="muted">No orders yet.</td></tr>'}</tbody>`;
    $("dOrders").querySelectorAll("[data-paid]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/admin/data/orders/${b.dataset.paid}/paid`, { method: "POST" }); loadData();
    }));
    loadTotals();
    $("dZips").innerHTML = `<thead><tr><th>ZIP / type</th><th>Status</th><th class="num">Properties</th></tr></thead><tbody>${
      d.zips.map((z) => `<tr><td>${esc(z.key.replace("_", " · "))}</td><td>${esc(z.status)}</td><td class="num">${fmt(z.count)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">None yet.</td></tr>'}</tbody>`;
  }
  // square feet measured: per ZIP, and one ZIP by assessed value
  const SHOW = ["lawn_sqft", "driveway_sqft", "sidewalk_sqft", "other_hardscape_sqft", "beds_sqft", "building_sqft", "wall_sqft", "lot_sqft"];
  const big = (v) => v >= 1e6 ? (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M" : Math.round(v).toLocaleString();
  async function loadTotals() {
    let T;
    try { T = await api("/api/admin/data/totals"); } catch (e) { $("dTot").innerHTML = `<tr><td class="err">${esc(e.message)}</td></tr>`; return; }
    const lab = Object.fromEntries(T.columns.map((c) => [c.key, c.label]));
    const head = `<thead><tr><th>ZIP</th><th class="num">Homes</th>${SHOW.map((k) => `<th class="num">${esc(lab[k])}</th>`).join("")}<th></th></tr></thead>`;
    const row = (z, name, btn) => `<tr><td><b>${esc(name)}</b></td><td class="num">${fmt(z.homes)}</td>${SHOW.map((k) => `<td class="num">${big(z[k] || 0)}</td>`).join("")}<td>${btn}</td></tr>`;
    $("dTot").innerHTML = head + `<tbody>${T.zips.map((z) => row(z, z.zip, `<button class="secondary" data-band="${esc(z.key)}">By value</button>`)).join("")
      || '<tr><td colspan="11" class="muted">No ZIPs measured yet. Use Measure a ZIP ahead of time, or Measure every ZIP we cover.</td></tr>'}
      ${T.zips.length > 1 ? row(T.all, "All ZIPs", "") : ""}</tbody>`;
    $("dTot").querySelectorAll("[data-band]").forEach((b) => b.onclick = async () => {
      const t = await api(`/api/admin/data/totals?key=${encodeURIComponent(b.dataset.band)}`);
      const avg = (r, k) => r.homes ? Math.round(r[k] / r.homes).toLocaleString() : "–";
      $("dBand").innerHTML = `<h2>ZIP ${esc(t.zip)} by assessed value</h2>
        <div class="tablewrap"><table><thead><tr><th>Home value</th><th class="num">Homes</th>${SHOW.map((k) => `<th class="num">${esc(lab[k])}</th>`).join("")}</tr></thead><tbody>
        ${t.bands.concat([t.all]).map((r) => `<tr${r.band == null ? ' style="font-weight:700"' : ""}><td>${esc(r.label)}</td><td class="num">${fmt(r.homes)}</td>${SHOW.map((k) => `<td class="num" title="average ${avg(r, k)} per home">${big(r[k])}</td>`).join("")}</tr>`).join("")}
        </tbody></table></div><p class="small muted">Hover a number for the average per home.</p>`;
      $("dBand").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
  $("dSave").addEventListener("click", async () => {
    try {
      await api("/api/admin/data/pricing", { method: "PUT", json: { rate_cents: +$("dRate").value, extra_type_cents: +$("dExtra").value,
        premier_cents: +$("dPremier").value, min_order_usd: +$("dMin").value,
        exclusive_cents: +$("dExFirst").value || 100, exclusive_extra_cents: +$("dExExtra").value, exclusive_months: +$("dExMonths").value || 12,
        value_multipliers: [...document.querySelectorAll("#dMult input")].map((x) => +x.value || 1) } });
      $("dMsg").innerHTML = '<span class="ok">Saved.</span>';
    } catch (e) { $("dMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
  $("dPre").addEventListener("click", async () => {
    try { const r = await api("/api/admin/data/prebuild", { method: "POST", json: { zip: $("dZip").value.trim() } });
      $("dPreMsg").innerHTML = `<span class="ok">${r.status === "ready" ? "Already measured." : "Measuring started."}</span>`; loadData(); }
    catch (e) { $("dPreMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });

  $("dPreAll").addEventListener("click", async () => {
    if (!confirm("Measure every ZIP we cover? This runs for days in the background and uses server time.")) return;
    try { await api("/api/admin/data/prebuild-all", { method: "POST", json: { start: true } }); loadData(); }
    catch (e) { $("dPreAllMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
  $("dPreStop").addEventListener("click", async () => {
    try { await api("/api/admin/data/prebuild-all", { method: "POST", json: { start: false } }); loadData(); }
    catch (e) { $("dPreAllMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });

  // ---------------- settings
  let settings = null;
  async function loadSettings() {
    $("sToken").value = localStorage.getItem("adminToken") || "";
    try { settings = await api("/api/admin/settings"); } catch (e) { $("sMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
    $("sName").value = settings.business.name; $("sPhone").value = settings.business.phone;
    $("sEmail").value = settings.business.email; $("sAccent").value = settings.business.accent;
    $("sSpread").value = Math.round(settings.spread * 100);
    $("svcEditor").innerHTML = "<h2>Services</h2>" + Object.entries(settings.services).map(([k, s]) => `
      <div class="stack" style="border-top:1px solid var(--line);padding-top:10px" data-svc="${k}">
        <label><input type="checkbox" data-f="enabled" ${s.enabled ? "checked" : ""}> <b>${esc(s.label)}</b> <span class="small muted">(${esc(s.measures.map((m) => LABELS[m]).join(" + "))})</span></label>
        <div class="row">
          <label class="small">Rate $ ${esc(s.rate_unit)}<input type="number" step="0.01" data-f="rate" value="${s.rate}"></label>
          <label class="small">Minimum $<input type="number" step="1" data-f="minimum" value="${s.minimum}"></label>
        </div></div>`).join("");
  }
  $("sSave").addEventListener("click", async () => {
    localStorage.setItem("adminToken", $("sToken").value.trim());
    if (!settings) return loadSettings();
    settings.business = { name: $("sName").value, phone: $("sPhone").value, email: $("sEmail").value, accent: $("sAccent").value || "#16a34a" };
    settings.spread = (+$("sSpread").value || 0) / 100;
    document.querySelectorAll("[data-svc]").forEach((d) => {
      const s = settings.services[d.dataset.svc];
      s.enabled = d.querySelector('[data-f="enabled"]').checked;
      s.rate = +d.querySelector('[data-f="rate"]').value;
      s.minimum = +d.querySelector('[data-f="minimum"]').value;
    });
    try { settings = await api("/api/admin/settings", { method: "PUT", json: settings }); $("sMsg").innerHTML = '<span class="ok">Saved.</span>'; }
    catch (e) { $("sMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
})();
