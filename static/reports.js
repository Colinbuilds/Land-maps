// Roofing company portal: log in, pick a property and its buildings, order a report.
(async function () {
  const $ = (id) => document.getElementById(id);
  let me = null, map = null, parcels = null, structLayer = null, current = null, polys = {}, edited = {};

  async function boot() {
    try { me = await api("/api/roof/me"); } catch (e) { me = null; }
    $("login").classList.toggle("hidden", !!me);
    $("app").classList.toggle("hidden", !me);
    $("logout").classList.toggle("hidden", !me);
    if (!me) { $("credits").textContent = ""; return; }
    $("coName").textContent = me.name;
    document.documentElement.style.setProperty("--accent", me.brand_color || "#1d4ed8");
    member = !!me.member;
    showCredits(me.credits);
    drawTradeBar();
    drawRates();
    if (!map) initMap();
    loadReports();
  }
  let member = false;
  // trades whose numbers the reports show (any of the company's trades)
  let TR = [], showTr = [];
  async function drawTradeBar() {
    if (!TR.length) { try { TR = (await api("/api/trades")).trades; } catch (e) {} }
    const mine = (me.trades && me.trades.length) ? me.trades : [];
    try { showTr = JSON.parse(localStorage.getItem("showTrades") || "null") || mine; } catch (e) { showTr = mine; }
    showTr = showTr.filter((k) => mine.includes(k));
    if (!showTr.length) showTr = mine.slice();
    const lab = (k) => ((TR.find((t) => t.key === k) || {}).label || k).replace(" (beta)", "");
    $("tradeBar").innerHTML = mine.length
      ? mine.map((k) => `<button type="button" class="${showTr.includes(k) ? "on" : ""}" data-k="${esc(k)}">${esc(lab(k))}</button>`).join("")
      : '<span class="small muted">No trades picked yet. <a href="/join">Pick your trades</a> to see each one\'s numbers on every report.</span>';
    $("tradeBar").onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      const k = b.dataset.k;
      showTr = showTr.includes(k) ? showTr.filter((x) => x !== k) : [...showTr, k];
      if (!showTr.length) showTr = [k];
      try { localStorage.setItem("showTrades", JSON.stringify(showTr)); } catch (x) {}
      drawTradeBar(); if (lastRows) drawReports(lastRows);
    };
  }
  // ---- the company's prices: one rule per trade, applied to every home
  let RULES = [];
  async function drawRates() {
    if (!RULES.length) { try { RULES = (await api("/api/pricing-rules")).rules; } catch (e) { return; } }
    const mine = (me.trades || []).filter((k) => RULES.some((r) => r.key === k));
    const list = mine.length ? RULES.filter((r) => mine.includes(r.key)) : RULES.slice(0, 3);
    const rt = me.rates || {};
    $("ratesBox").innerHTML = list.map((r) => {
      const v = rt[r.key] || {};
      return `<div class="rate" data-k="${esc(r.key)}">
        <div class="rt"><b>${esc(r.label)}</b><div class="small muted">Priced on ${esc(r.measured)}, $ ${esc(r.basis)}</div></div>
        <label>$ ${esc(r.basis)}<input type="number" step="0.01" min="0" data-f="rate" value="${v.rate ?? ""}" placeholder="${r.rate}"></label>
        <label>Minimum $<input type="number" step="1" min="0" data-f="min" value="${v.min ?? ""}" placeholder="${r.min}"></label>
        <label>Shown as<input type="text" maxlength="30" data-f="label" value="${esc(v.label || "")}" placeholder="${esc(r.price_label)}"></label>
      </div>`;
    }).join("") + (mine.length ? "" : '<p class="small muted">Pick your trades on the <a href="/join">membership page</a> to price each of them.</p>');
  }
  $("ratesSave").addEventListener("click", async () => {
    const rates = {};
    document.querySelectorAll("#ratesBox .rate").forEach((row) => {
      const g = (f) => row.querySelector(`[data-f="${f}"]`).value.trim();
      if (g("rate") || g("min")) rates[row.dataset.k] = { rate: +g("rate") || 0, min: +g("min") || 0, label: g("label") };
    });
    try { me = await api("/api/roof/rates", { method: "PUT", json: { rates } }); $("ratesMsg").innerHTML = '<span class="ok">Saved. Reports, sheets and postcards now show your price.</span>'; }
    catch (e) { $("ratesMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
  const tq = () => showTr.length ? `?trades=${encodeURIComponent(showTr.join(","))}` : "";
  const pcq = () => showTr.length ? `?trade=${encodeURIComponent(showTr[0])}` : "";
  let lastRows = null;
  const showCredits = (n) => {
    $("credits").innerHTML = member ? `Member · unlimited reports`
      : `${n} credit${n === 1 ? "" : "s"} <a href="/join" style="margin-left:8px;font-weight:600">Get more</a>`;
  };

  $("lBtn").addEventListener("click", async () => {
    try {
      await api("/api/roof/login", { method: "POST", json: { email: $("lEmail").value, password: $("lPass").value } });
      boot();
    } catch (e) { $("lMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
  $("lPass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("lBtn").click(); });
  $("logout").addEventListener("click", async () => { await api("/api/roof/logout", { method: "POST" }); location.reload(); });

  async function initMap() {
    const cfg = await api("/api/config");
    map = makeMap("map", cfg);
    parcels = parcelLayer(map, (f) => pick(f.properties.pin));
    wireAutocomplete($("addr"), $("suggest"), async (s) => {
      const q = new URLSearchParams({ q: s.text });
      if (s.magicKey) q.set("magicKey", s.magicKey);
      try {
        const loc = await api(`/api/locate?${q}`);
        map.setView([loc.best.lat, loc.best.lon], 20);
        await parcels.load();
        if (loc.suggested_pin) pick(loc.suggested_pin);
      } catch (e) { alert(e.message); }
    });
  }

  async function pick(pin) {
    parcels.select(pin);
    $("orderCard").innerHTML = '<span class="spinner"></span>Finding buildings…';
    try { current = await api(`/api/roof/structures/${encodeURIComponent(pin)}`); }
    catch (e) { $("orderCard").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
    if (structLayer) map.removeLayer(structLayer);
    structLayer = L.layerGroup().addTo(map);
    polys = {};
    edited = {};
    const sqft = (layer) => L.GeometryUtil.geodesicArea(layer.getLatLngs()[0]) * 10.7639;
    current.structures.forEach((s) => {
      const ring = s.geojson.type === "Polygon" ? s.geojson.coordinates[0] : s.geojson.coordinates[0][0];
      const layer = L.polygon(ring.map(([x, y]) => [y, x]), { color: "#22d3ee", weight: 2, fillOpacity: 0.12 });
      layer.bindTooltip(esc(s.name), { direction: "center", className: "parcel-label" });
      layer.on("edit", () => {
        edited[s.id] = layer.toGeoJSON().geometry;
        const el = document.getElementById(`sq${s.id}`);
        if (el) el.textContent = `${fmt(sqft(layer))} sq ft footprint (adjusted)`;
      });
      layer.addTo(structLayer);
      polys[s.id] = { layer, original: ring };
    });
    if (!current.structures.length) {
      $("orderCard").innerHTML = `<h2>${esc(current.address)}</h2><p class="muted">No buildings are mapped on this lot.</p>`;
      return;
    }
    $("orderCard").innerHTML = `
      <h2 style="margin:0">${esc(current.address)}</h2>
      <div class="small muted">Parcel ${esc(String(current.pin).split(":").pop())}</div>
      <div class="structs">${current.structures.map((s, i) => `
        <label><input type="checkbox" value="${s.id}" ${i === 0 || s.plan_sqft >= 250 ? "checked" : ""}>
          <span><b>${esc(s.name)}</b> <span class="muted small" id="sq${s.id}">${fmt(s.plan_sqft)} sq ft footprint</span></span></label>`).join("")}</div>
      <div class="row">
        <button class="secondary" id="editBtn">Adjust outline</button>
        <button class="secondary" id="resetBtn">Reset</button>
      </div>
      <button id="orderBtn">Get report (1 credit)</button>
      <div id="orderMsg" class="small"></div>`;
    let editing = false;
    $("editBtn").addEventListener("click", () => {
      editing = !editing;
      Object.values(polys).forEach(({ layer }) => (editing ? layer.editing.enable() : layer.editing.disable()));
      $("editBtn").textContent = editing ? "Done adjusting" : "Adjust outline";
      if (editing) map.fitBounds(L.featureGroup(Object.values(polys).map((p) => p.layer)).getBounds(), { maxZoom: 21, padding: [30, 30] });
    });
    $("resetBtn").addEventListener("click", () => {
      Object.entries(polys).forEach(([id, p]) => {
        p.layer.editing.disable();
        p.layer.setLatLngs(p.original.map(([x, y]) => [y, x]));
        const s = current.structures.find((x) => String(x.id) === id);
        document.getElementById(`sq${id}`).textContent = `${fmt(s.plan_sqft)} sq ft footprint`;
      });
      edited = {}; editing = false; $("editBtn").textContent = "Adjust outline";
    });
    $("orderBtn").addEventListener("click", order);
  }

  async function order() {
    const ids = [...document.querySelectorAll(".structs input:checked")].map((i) => +i.value);
    if (!ids.length) { $("orderMsg").innerHTML = '<span class="err">Pick at least one building.</span>'; return; }
    $("orderBtn").disabled = true;
    $("orderMsg").innerHTML = '<span class="spinner"></span>Measuring the property. The first report in a new area can take about 30 seconds.';
    try {
      Object.values(polys).forEach(({ layer }) => layer.editing.disable());
      const ed = {};
      ids.forEach((i) => { if (edited[i]) ed[String(i)] = edited[i]; });
      const r = await api("/api/roof/report", { method: "POST", json: { pin: current.pin, structure_ids: ids, edited: ed } });
      showCredits(r.credits);
      $("orderMsg").innerHTML = `<span class="ok">Done.</span> <a href="/r/${r.id}${tq()}" target="_blank">Open report ${esc(r.id)}</a> · <a href="/r/${r.id}/pdf${tq()}" target="_blank">PDF</a> · <a href="/r/${r.id}/postcard.pdf${pcq()}" target="_blank">Postcard</a>`;
      window.open(`/r/${r.id}${tq()}`, "_blank");
      loadReports();
    } catch (e) {
      $("orderMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    } finally { $("orderBtn").disabled = false; }
  }

  async function loadReports() {
    drawReports(await api("/api/roof/reports"));
  }
  function drawReports(rows) {
    lastRows = rows;
    $("reports").innerHTML = `<thead><tr><th>Date</th><th>Address</th><th>Report</th></tr></thead><tbody>${
      rows.map((r) => `<tr><td>${new Date(r.created * 1000).toLocaleDateString()}</td><td>${esc(r.address)}</td>
        <td><a href="/r/${r.id}${tq()}" target="_blank">${esc(r.id)}</a> · <a href="/r/${r.id}/pdf${tq()}" target="_blank">PDF</a>
          · <a href="/r/${r.id}/postcard.pdf${pcq()}" target="_blank" title="Print-ready 9x6 postcard for this home">Postcard</a>
          · <a href="/r/${r.id}/photo.jpg" target="_blank" title="High-resolution aerial photo">Photo</a></td></tr>`).join("")
      || '<tr><td colspan="3" class="muted">No reports yet.</td></tr>'}</tbody>`;
  }

  boot();
})();
