// Data storefront: quote a ZIP, free 25-property sample, buy the full sheet.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await api("/api/config");
  document.documentElement.style.setProperty("--accent", cfg.business.accent || "#16a34a");
  if (cfg.business.name && cfg.business.name !== "Your Company") $("bizName").textContent = cfg.business.name;
  $("bizContact").textContent = [cfg.business.phone, cfg.business.email].filter(Boolean).join(" · ");
  const usd = (c) => "$" + (c / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let map = null, zl = null, q = null;
  // measurements picked for the full sheet (declared first: the calculator reads it on load)
  let picked = (() => { try { return JSON.parse(localStorage.getItem("dataTypes") || "null") || ["lawn"]; } catch (e) { return ["lawn"]; } })();
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
    buildTypePicker(q.type_catalog || []);
    updatePrice();
    $("rateLine").textContent = `${p.rate_cents}¢ a home for your first measurement, +${p.extra_type_cents}¢ for each extra one. Pay by card, download a spreadsheet, and quote before you visit.`;
    pricing = q.pricing || pricing;
    pricingCard();
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
    const per = pricing.rate_cents + pricing.extra_type_cents * (Math.max(1, picked.length) - 1);
    return Math.max(n * per / 100, pricing.min_order_usd);
  }
  const cents = (c) => (c >= 1 ? `${+c.toFixed(1)}¢` : `${+c.toFixed(2)}¢`);
  function pricingCard() {
    const p = pricing;
    $("pTier1Lbl").textContent = "Your first measurement";
    $("pTier1").textContent = cents(p.rate_cents);
    $("pTier2Lbl") && ($("pTier2Lbl").textContent = "Each extra measurement");
    $("pTier2").textContent = "+" + cents(p.extra_type_cents);
    $("pMin").textContent = `per home · $${p.min_order_usd} minimum order`;
    $("vsPer").textContent = cents(100 * sheetPrice(5000) / 5000);
  }
  function payback(price) {
    const v = Math.max(0, +$("cVal").value || 0);
    if (!v) return null;
    return Math.max(1, Math.ceil(price / v));
  }
  function calc() {
    const n = Math.max(1, +$("cN").value || 0);
    const drive = $("cHow").value === "drive";
    $("cMilesBox").classList.toggle("hidden", !drive);
    const mins = Math.max(0, +$("cMin").value || 0), wage = Math.max(0, +$("cWage").value || 0);
    const hours = n * mins / 60;
    const hand = hours * wage + (drive ? n * Math.max(0, +$("cMiles").value || 0) : 0);
    const ours = sheetPrice(n);
    $("cHand").textContent = money(hand);
    $("cHandSub").textContent = `${Math.round(hours).toLocaleString()} hours of work · $${(hand / n).toFixed(2)} a home`;
    $("cOurs").textContent = money(ours);
    $("cOursSub").textContent = `ready in minutes · ${cents(100 * ours / n)} a home`;
    const save = hand - ours;
    $("cSave").textContent = save > 0 ? money(save) : "$0";
    $("cSaveSub").textContent = save > 0 ? `${Math.round(100 * save / hand)}% less, and ${Math.round(hours).toLocaleString()} hours back`
      : "Measuring yourself is cheaper at these numbers";
    const k = payback(ours);
    $("cPay").textContent = k == null ? "–" : `${k} customer${k === 1 ? "" : "s"}`;
    $("cPaySub").textContent = k == null ? "Enter what a customer is worth"
      : `out of ${n.toLocaleString()} homes on the sheet`;
    if (q && q.count) {
      const kq = payback(q.price_cents / 100);
      $("qPayback").innerHTML = kq == null ? "" :
        `<span class="ok">Pays for itself with ${kq} new customer${kq === 1 ? "" : "s"}</span> <span class="muted">at $${(+$("cVal").value).toLocaleString()} a year each</span>`;
    }
  }
  $("cHow").addEventListener("change", () => { $("cMin").value = $("cHow").value === "drive" ? 25 : 4; calc(); });
  ["cN", "cMin", "cWage", "cMiles", "cVal"].forEach((id) => $(id).addEventListener("input", calc));
  pricingCard();
  calc();
  $("ctaBtn").addEventListener("click", (e) => {
    e.preventDefault();
    $("zipbox").scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => $("zip").focus({ preventScroll: true }), 400);
  });

  // ------------------------------------------------ measurement picker
  function buildTypePicker(cat) {
    const box = $("typePick");
    if (!box || box.dataset.built) return;
    box.dataset.built = "1";
    box.innerHTML = cat.map((t, i) => `<label class="${picked.includes(t.key) ? "on" : ""}"><input type="checkbox" value="${esc(t.key)}" ${picked.includes(t.key) ? "checked" : ""}><span>${esc(t.label)}</span><span class="c" data-c="${esc(t.key)}"></span></label>`).join("");
    box.addEventListener("change", () => {
      picked = [...box.querySelectorAll("input:checked")].map((x) => x.value);
      if (!picked.length) { box.querySelector('input[value="lawn"]').checked = true; picked = ["lawn"]; }
      box.querySelectorAll("label").forEach((l) => l.classList.toggle("on", l.querySelector("input").checked));
      try { localStorage.setItem("dataTypes", JSON.stringify(picked)); } catch (e) {}
      updatePrice();
      calc();
    });
  }
  let product = "standard";
  document.querySelectorAll(".prodswitch button").forEach((btn) => btn.addEventListener("click", () => {
    product = btn.dataset.prod;
    document.querySelectorAll(".prodswitch button").forEach((x) => { x.classList.toggle("on", x === btn); x.setAttribute("aria-checked", x === btn); });
    const prem = product === "premier";
    $("typePick").classList.toggle("hidden", prem);
    $("stdIntro").classList.toggle("hidden", prem);
    $("premIntro").classList.toggle("hidden", !prem);
    updatePrice();
  }));
  const perHome = () => product === "premier" ? (pricing.premier_cents || 25)
    : pricing.rate_cents + pricing.extra_type_cents * (Math.max(1, picked.length) - 1);
  function updatePrice() {
    if (!q || !q.count) return;
    const full = Math.max(Math.ceil(q.count * perHome() - 1e-9), Math.round(pricing.min_order_usd * 100));
    const cents = q.member ? Math.round(full / 2) : full;
    q.price_cents = cents;
    $("qPrice").innerHTML = q.member ? `${usd(cents)} <s class="muted" style="font-size:.5em;font-weight:500">${usd(full)}</s>` : usd(cents);
    const atMin = full <= pricing.min_order_usd * 100;
    const what = product === "premier" ? "Premier report" : `${picked.length} measurement${picked.length === 1 ? "" : "s"}`;
    $("qRate").textContent = `(${what} × ${q.count.toLocaleString()} homes at ${+(perHome() / (q.member ? 2 : 1)).toFixed(2)}¢ each${atMin ? `, $${pricing.min_order_usd} minimum` : ""})`;
    $("memberLine").innerHTML = q.member ? '<span class="ok">Member price: half off.</span>'
      : `<span class="muted">Members pay half. <a href="/join">$99/month</a> includes unlimited single-property reports.</span>`;
    // what each unticked option would add
    document.querySelectorAll("#typePick [data-c]").forEach((el) => {
      const on = picked.includes(el.dataset.c);
      el.textContent = on ? (picked[0] === el.dataset.c ? `${pricing.rate_cents}¢` : `+${pricing.extra_type_cents}¢`) : `+${pricing.extra_type_cents}¢`;
    });
  }

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
      const num = (k) => /_sqft$|_ft$|_count$|^est_/.test(k);
      const show = ["address", "city", "lawn_sqft", "parkway_sqft", "driveway_sqft", "sidewalk_sqft", "building_sqft", "lot_sqft", "beds_sqft", "tree_count", "wall_sqft", "est_windows"];
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
      const r = await api("/api/data/order", { method: "POST", json: { email, zip: q.zip, classes: $("cls").value.split(","), types: picked, product } });
      location.href = r.checkout_url || r.order_url;
    } catch (e) {
      $("bMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
      $("bBtn").disabled = false;
    }
  });

  const pre = new URLSearchParams(location.search).get("zip");
  if (pre) { $("zip").value = pre; check(); }
})();
