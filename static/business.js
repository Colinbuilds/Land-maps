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
  }));

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
      $("bMsg").innerHTML = `<b>${fmt(r.count)}</b> parcels match. Estimated run time: about ${mins} min.`;
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
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline"><h2 style="margin:0">${esc(r.address)}</h2><span class="tag">${esc(r.pin)}</span></div>
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
    try {
      await api("/api/admin/companies", { method: "POST", json: { name: $("cName").value, email: $("cEmail").value,
        password: $("cPass").value, phone: $("cPhone").value, credits: +$("cCredits").value || 0 } });
      $("cMsg").innerHTML = '<span class="ok">Company created.</span>';
      ["cName", "cEmail", "cPass", "cPhone", "cCredits"].forEach((i) => ($(i).value = ""));
      loadRoofing();
    } catch (e) { $("cMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
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
