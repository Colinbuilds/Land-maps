// Roofing company portal: log in, pick a property and its buildings, order a report.
(async function () {
  const $ = (id) => document.getElementById(id);
  let me = null, map = null, parcels = null, structLayer = null, current = null;

  async function boot() {
    try { me = await api("/api/roof/me"); } catch (e) { me = null; }
    $("login").classList.toggle("hidden", !!me);
    $("app").classList.toggle("hidden", !me);
    $("logout").classList.toggle("hidden", !me);
    if (!me) { $("credits").textContent = ""; return; }
    $("coName").textContent = me.name;
    document.documentElement.style.setProperty("--accent", me.brand_color || "#1d4ed8");
    showCredits(me.credits);
    if (!map) initMap();
    loadReports();
  }
  const showCredits = (n) => { $("credits").textContent = `${n} credit${n === 1 ? "" : "s"}`; };

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
    current.structures.forEach((s) => {
      L.geoJSON(s.geojson, { style: { color: "#60a5fa", weight: 2, fillOpacity: 0.25 } })
        .bindTooltip(esc(s.name), { permanent: true, direction: "center", className: "parcel-label" }).addTo(structLayer);
    });
    if (!current.structures.length) {
      $("orderCard").innerHTML = `<h2>${esc(current.address)}</h2><p class="muted">No buildings are mapped on this lot.</p>`;
      return;
    }
    $("orderCard").innerHTML = `
      <h2 style="margin:0">${esc(current.address)}</h2>
      <div class="small muted">Parcel ${esc(current.pin)}</div>
      <div class="structs">${current.structures.map((s, i) => `
        <label><input type="checkbox" value="${s.id}" ${i === 0 || s.plan_sqft >= 250 ? "checked" : ""}>
          <span><b>${esc(s.name)}</b> <span class="muted small">· about ${fmt(s.plan_sqft)} sq ft footprint</span></span></label>`).join("")}</div>
      <button id="orderBtn">Get roof report (1 credit)</button>
      <div id="orderMsg" class="small"></div>`;
    $("orderBtn").addEventListener("click", order);
  }

  async function order() {
    const ids = [...document.querySelectorAll(".structs input:checked")].map((i) => +i.value);
    if (!ids.length) { $("orderMsg").innerHTML = '<span class="err">Pick at least one building.</span>'; return; }
    $("orderBtn").disabled = true;
    $("orderMsg").innerHTML = '<span class="spinner"></span>Measuring the roof. The first report in a new area can take about 30 seconds.';
    try {
      const r = await api("/api/roof/report", { method: "POST", json: { pin: current.pin, structure_ids: ids } });
      showCredits(r.credits);
      $("orderMsg").innerHTML = `<span class="ok">Done.</span> <a href="/r/${r.id}" target="_blank">Open report ${esc(r.id)}</a> · <a href="/r/${r.id}/pdf" target="_blank">PDF</a>`;
      window.open(`/r/${r.id}`, "_blank");
      loadReports();
    } catch (e) {
      $("orderMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    } finally { $("orderBtn").disabled = false; }
  }

  async function loadReports() {
    const rows = await api("/api/roof/reports");
    $("reports").innerHTML = `<thead><tr><th>Date</th><th>Address</th><th>Report</th></tr></thead><tbody>${
      rows.map((r) => `<tr><td>${new Date(r.created * 1000).toLocaleDateString()}</td><td>${esc(r.address)}</td>
        <td><a href="/r/${r.id}" target="_blank">${esc(r.id)}</a> · <a href="/r/${r.id}/pdf" target="_blank">PDF</a></td></tr>`).join("")
      || '<tr><td colspan="3" class="muted">No reports yet.</td></tr>'}</tbody>`;
  }

  boot();
})();
