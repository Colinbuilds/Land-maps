// Data storefront: quote a ZIP, free 25-property sample, buy the full sheet.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await api("/api/config");
  document.documentElement.style.setProperty("--accent", cfg.business.accent || "#16a34a");
  if (cfg.business.name && cfg.business.name !== "Your Company") $("bizName").textContent = cfg.business.name;
  $("bizContact").textContent = [cfg.business.phone, cfg.business.email].filter(Boolean).join(" · ");
  const usd = (c) => "$" + (c / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let map = null, zl = null, q = null;
  const saved = (() => { try { return localStorage.getItem("dataEmail") || ""; } catch (e) { return ""; } })();
  $("sEmail").value = saved; $("bEmail").value = saved;
  const remember = (e) => { try { localStorage.setItem("dataEmail", e); } catch (x) {} };

  async function check() {
    const zip = $("zip").value.trim();
    if (!/^\d{5}$/.test(zip)) { $("quoteMsg").innerHTML = '<span class="err">Enter a 5-digit ZIP code.</span>'; return; }
    $("quoteMsg").innerHTML = '<span class="spinner"></span>Counting properties…';
    try { q = await api(`/api/data/quote?zip=${zip}&classes=${encodeURIComponent($("cls").value)}`); }
    catch (e) { $("quoteMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
    history.replaceState(null, "", `?zip=${zip}`);
    if (!q.count) {
      $("result").classList.add("hidden");
      $("quoteMsg").innerHTML = '<span class="err">No matching properties in that ZIP inside our coverage area yet.</span>';
      return;
    }
    $("quoteMsg").textContent = "";
    $("result").classList.remove("hidden");
    $("qZip").textContent = `ZIP ${q.zip} · ${$("cls").selectedOptions[0].textContent}`;
    $("qCount").textContent = `${q.count.toLocaleString()} properties`;
    $("qBy").textContent = Object.entries(q.by_county || {}).map(([k, v]) => `${k}: ${v.toLocaleString()}`).join(" · ");
    $("qReady").innerHTML = q.ready
      ? `<span class="pill">Ready to download now</span> <span class="small muted">measured ${new Date(q.measured * 1000).toLocaleDateString()}</span>`
      : `<span class="small muted">Measured after purchase. About ${Math.max(5, Math.round(q.count * 0.5 / 60))} minutes; we'll have your download page update itself.</span>`;
    const p = q.pricing;
    $("qPrice").textContent = usd(q.price_cents);
    $("qRate").textContent = `(${p.rate_cents}¢ per property${q.count > p.bulk_threshold ? `, ${p.bulk_rate_cents}¢ after the first ${p.bulk_threshold.toLocaleString()}` : ""}${q.price_cents <= p.min_order_usd * 100 ? `, $${p.min_order_usd} minimum` : ""})`;
    $("rateLine").textContent = `${p.rate_cents}¢ per property, ${p.bulk_rate_cents}¢ each after ${p.bulk_threshold.toLocaleString()}. Download a spreadsheet (CSV).`;
    pricing = q.pricing || pricing;
    $("cN").value = q.count;
    calc();
    $("cols").innerHTML = "Columns: " + q.columns.map((c) => `<span>${esc(c)}</span>`).join("");
    if (!map) {
      map = makeMap("zmap", cfg);
    }
    setTimeout(() => map.invalidateSize(), 50);
    if (zl) map.removeLayer(zl);
    if (q.zip_geojson) {
      zl = L.geoJSON(q.zip_geojson, { style: { color: "#facc15", weight: 3, fillOpacity: 0.08 } }).addTo(map);
      map.fitBounds(zl.getBounds(), { padding: [10, 10] });
    }
  }
  // ------------------------------------------------ savings calculator
  let pricing = { rate_cents: 10, bulk_rate_cents: 5, bulk_threshold: 2000, min_order_usd: 25 };
  try { const cp = await api("/api/data/pricing"); if (cp && cp.rate_cents) pricing = cp; } catch (e) {}
  const money = (d) => "$" + Math.round(d).toLocaleString();
  function sheetPrice(n) {
    const p = pricing, t = p.bulk_threshold;
    const c = Math.min(n, t) * p.rate_cents + Math.max(0, n - t) * p.bulk_rate_cents;
    return Math.max(c / 100, p.min_order_usd);
  }
  function calc() {
    const n = Math.max(1, +$("cN").value || 0);
    const drive = $("cHow").value === "drive";
    $("cMilesBox").classList.toggle("hidden", !drive);
    const mins = Math.max(0, +$("cMin").value || 0), wage = Math.max(0, +$("cWage").value || 0);
    const hours = n * mins / 60;
    const hand = hours * wage + (drive ? n * Math.max(0, +$("cMiles").value || 0) : 0);
    const ours = (q && q.count === n) ? q.price_cents / 100 : sheetPrice(n);
    $("cHand").textContent = money(hand);
    $("cHandSub").textContent = `${Math.round(hours).toLocaleString()} hours of work · $${(hand / n).toFixed(2)} per property`;
    $("cOurs").textContent = money(ours);
    $("cOursSub").textContent = `ready in minutes · $${(ours / n).toFixed(3)} per property`;
    const save = hand - ours;
    $("cSave").textContent = save > 0 ? money(save) : "$0";
    $("cSaveSub").textContent = save > 0 ? `${Math.round(100 * save / hand)}% less, and ${Math.round(hours).toLocaleString()} hours back`
      : "Measuring yourself is cheaper at these numbers";
  }
  $("cHow").addEventListener("change", () => { $("cMin").value = $("cHow").value === "drive" ? 25 : 4; calc(); });
  ["cN", "cMin", "cWage", "cMiles"].forEach((id) => $(id).addEventListener("input", calc));
  calc();

  $("check").addEventListener("click", check);
  $("zip").addEventListener("keydown", (e) => { if (e.key === "Enter") check(); });

  $("sBtn").addEventListener("click", async () => {
    if (!q) return;
    const email = $("sEmail").value.trim();
    remember(email);
    $("sBtn").disabled = true;
    $("sMsg").innerHTML = q.ready ? '<span class="spinner"></span>Pulling your sample…'
      : '<span class="spinner"></span>Measuring 25 properties across the ZIP. This takes about 30 to 60 seconds.';
    try {
      const r = await api("/api/data/sample", { method: "POST", json: { email, zip: q.zip, classes: $("cls").value.split(",") } });
      $("sMsg").innerHTML = `<span class="ok">Here are ${r.rows.length} properties.</span>`;
      const num = (k) => /_sqft$|_ft$/.test(k);
      const show = ["address", "city", "lawn_sqft", "parkway_sqft", "driveway_sqft", "sidewalk_sqft", "building_sqft", "lot_sqft", "beds_sqft", "tree_count"];
      const lab = Object.fromEntries(r.columns.map((c) => [c.key, c.label]));
      $("sampleTable").innerHTML = `<thead><tr>${show.map((k) => `<th class="${num(k) ? "num" : ""}">${esc(lab[k])}</th>`).join("")}</tr></thead>
        <tbody>${r.rows.map((row) => `<tr>${show.map((k) => `<td class="${num(k) ? "num" : ""}">${num(k) ? fmt(row[k]) : esc(row[k])}</td>`).join("")}</tr>`).join("")}</tbody>`;
      $("sDl").href = URL.createObjectURL(new Blob([r.csv], { type: "text/csv" }));
      $("sDl").download = `sample_${r.zip}.csv`;
      $("sampleCard").classList.remove("hidden");
      $("sampleCard").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
      $("sMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    } finally { $("sBtn").disabled = false; }
  });

  $("bBtn").addEventListener("click", async () => {
    if (!q) return;
    const email = $("bEmail").value.trim();
    remember(email);
    $("bBtn").disabled = true;
    $("bMsg").innerHTML = '<span class="spinner"></span>Creating your order…';
    try {
      const r = await api("/api/data/order", { method: "POST", json: { email, zip: q.zip, classes: $("cls").value.split(",") } });
      location.href = r.checkout_url || r.order_url;
    } catch (e) {
      $("bMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
      $("bBtn").disabled = false;
    }
  });

  const pre = new URLSearchParams(location.search).get("zip");
  if (pre) { $("zip").value = pre; check(); }
})();
