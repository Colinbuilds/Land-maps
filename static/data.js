// Data storefront: quote a ZIP, free 25-property sample, buy the full sheet.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await api("/api/config");
  document.documentElement.style.setProperty("--accent", cfg.business.accent || "#16a34a");
  if (cfg.business.name && cfg.business.name !== "Your Company") $("bizName").textContent = cfg.business.name;
  $("bizContact").textContent = [cfg.business.phone, cfg.business.email].filter(Boolean).join(" · ");
  const usd = (c) => "$" + (c / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let map = null, zl = null, q = null;
  let extra = [];  // more ZIPs in the same order (declared early: other code reads it on load)
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
    extra = extra.filter((x) => x.zip !== zip);
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
    markLocks();
    buildValuePicker(q.value_bands || { bands: [], labels: [] });
    updatePrice();
    $("rateLine").textContent = `${p.rate_cents}¢ a home for your first measurement, +${p.extra_type_cents}¢ for each extra one. Pay by card, download a spreadsheet, and quote before you visit.`;
    pricing = q.pricing || pricing;
    pricingCard();
    $("cN").value = selCount();
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
    const m = picked.filter((k) => k !== "value").length;  // the value column is a flat add-on
    const per = pricing.rate_cents + pricing.extra_type_cents * (Math.max(1, m) - 1) + (picked.includes("value") ? (pricing.value_cents ?? 5) : 0);
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
      if (!picked.filter((k) => k !== "value").length) {  // the value column comes with at least one measurement
        const f = box.querySelector('input:not(:disabled):not([value="value"])'); if (f) { f.checked = true; picked = [f.value, ...picked]; }
      }
      box.querySelectorAll("label").forEach((l) => l.classList.toggle("on", l.querySelector("input").checked));
      try { localStorage.setItem("dataTypes", JSON.stringify(picked)); } catch (e) {}
      updatePrice();
      calc();
    });
  }
  // exclusive ZIPs: types another company locked can't be bought here
  const taken = () => (q && q.taken_types) || [];
  const held = () => Object.entries((q && q.locks) || {}).filter(([, l]) => l.yours).map(([k]) => k);
  function markLocks() {
    const box = $("typePick");
    const t = taken();
    box.querySelectorAll("label").forEach((l) => {
      const inp = l.querySelector("input"), k = inp.value;
      const isT = t.includes(k);
      l.classList.toggle("taken", isT); l.classList.toggle("held", held().includes(k));
      inp.disabled = isT;
      if (isT) { inp.checked = false; l.classList.remove("on"); }
    });
    picked = picked.filter((k) => !t.includes(k));
    if (!picked.length) {
      const free = [...box.querySelectorAll("input")].find((x) => !x.disabled);
      if (free) { free.checked = true; free.closest("label").classList.add("on"); picked = [free.value]; }
    }
    const pb = document.querySelector('.prodswitch button[data-prod="premier"]');
    pb.disabled = t.length > 0;
    pb.querySelector("span").textContent = t.length ? "Not available: part of this ZIP is exclusive to another company" : "Full report PDF for every home";
    if (t.length && product === "premier") document.querySelector('.prodswitch button[data-prod="standard"]').click();
    const months = (q.pricing && q.pricing.exclusive_months) || 12;
    $("exclMonths").textContent = months;
    $("exclRate").textContent = `${usd(q.pricing.exclusive_cents || 100)} a home for your first measurement and ${cents(q.pricing.exclusive_extra_cents || 50)} for each extra one`;
    $("exclLogin").innerHTML = q.logged_in ? "" : 'You need a free account so the lock is tied to your company: <a href="/join">sign up or sign in</a>.';
  }
  // ------------------------------------------------ home-value range
  let vLo = 0, vHi = 8;
  function buildValuePicker(vb) {
    const n = vb.bands.length;
    if (!n) return;
    vHi = Math.min(vHi, n - 1);
    vLo = Math.min(vLo, vHi);
    const from = vb.labels.map((l, i) => i === 0 ? "Any value" : (l.split("-")[0].replace("+", "")) + "+");
    const to = vb.labels.map((l, i) => i === n - 1 ? "No limit" : "up to " + (l.split("-")[1] || l.replace("Under ", "")));
    $("vLo").innerHTML = from.map((t, i) => `<option value="${i}" ${i === vLo ? "selected" : ""}>${esc(t)}</option>`).join("");
    $("vHi").innerHTML = to.map((t, i) => `<option value="${i}" ${i === vHi ? "selected" : ""}>${esc(t)}</option>`).join("");
    const max = Math.max(1, ...vb.bands);
    const draw = () => {
      $("vBars").innerHTML = vb.bands.map((c, i) => `<div class="vbar ${i >= vLo && i <= vHi ? "in" : ""}" data-i="${i}" title="Click to start the range here">
          <span class="l">${esc(vb.labels[i])}</span><span class="t"><i style="width:${(100 * c / max).toFixed(1)}%"></i></span><span class="n">${c.toLocaleString()}</span></div>`).join("");
    };
    const onChange = () => {
      vLo = +$("vLo").value; vHi = Math.max(vLo, +$("vHi").value); $("vHi").value = vHi;
      draw(); updatePrice(); calc();
    };
    $("vLo").onchange = onChange; $("vHi").onchange = onChange;
    $("vBars").onclick = (e) => { const r = e.target.closest(".vbar"); if (!r) return; $("vLo").value = r.dataset.i; if (+$("vHi").value < +r.dataset.i) $("vHi").value = n - 1; onChange(); };
    draw();
  }
  const selCount = () => (q && q.value_bands && q.value_bands.bands.length)
    ? q.value_bands.bands.slice(vLo, vHi + 1).reduce((a, b) => a + b, 0) : (q ? q.count : 0);

  // trade presets: one tap picks a trade's measurements (or Premier for roof trades)
  (async () => {
    let TR = [];
    try { TR = (await api("/api/trades")).trades.filter((t) => !/beta/i.test(t.label)); } catch (e) { return; }
    const box = $("tradeChips");
    if (!box) return;
    box.innerHTML = TR.map((t) => `<button type="button" data-k="${esc(t.key)}" title="${esc(t.pitch)}">${esc(t.label)}</button>`).join("");
    box.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      const t = TR.find((x) => x.key === b.dataset.k);
      box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      const prodBtn = document.querySelector(`.prodswitch button[data-prod="${t.premier ? "premier" : "standard"}"]`);
      if (!t.premier && t.types.length) {
        picked = t.types.filter((k) => !taken().includes(k));
        if (!picked.length) picked = t.types.slice(0, 1);
        document.querySelectorAll("#typePick input").forEach((x) => { x.checked = picked.includes(x.value); x.closest("label").classList.toggle("on", x.checked); });
        try { localStorage.setItem("dataTypes", JSON.stringify(picked)); } catch (e2) {}
      }
      if (prodBtn) prodBtn.click(); else { updatePrice(); calc(); }
    });
  })();

  let product = "standard";
  document.querySelectorAll(".prodswitch button").forEach((btn) => btn.addEventListener("click", () => {
    product = btn.dataset.prod;
    document.querySelectorAll(".prodswitch button").forEach((x) => { x.classList.toggle("on", x === btn); x.setAttribute("aria-checked", x === btn); });
    const prem = product === "premier", excl = product === "exclusive";
    $("typePick").classList.toggle("hidden", prem);
    $("stdIntro").classList.toggle("hidden", prem || excl);
    $("premIntro").classList.toggle("hidden", !prem);
    $("exclIntro").classList.toggle("hidden", !excl);
    document.querySelector(".dp .valuepick").classList.toggle("hidden", excl);  // whole ZIP only
    $("bBtn").textContent = excl ? "Lock this ZIP" : "Buy sheet";
    updatePrice();
  }));
  // the assessed-value column is an add-on (+5¢), never the "first measurement"
  const ADDON = ["value"];
  const measuresPicked = () => picked.filter((k) => !ADDON.includes(k));
  const addonCents = () => picked.includes("value") ? (pricing.value_cents ?? 5) : 0;
  const perHome = () => product === "premier" ? (pricing.premier_cents || 25)
    : pricing.rate_cents + pricing.extra_type_cents * (Math.max(1, measuresPicked().length) - 1);  // + addonCents(), flat, added per home
  const flatAddon = () => product === "premier" ? 0 : addonCents();
  function exclusivePrice() {
    const p = q.pricing, h = held(), fresh = measuresPicked().filter((k) => !h.includes(k));
    const per = (h.length ? (p.exclusive_extra_cents || 50) * fresh.length
      : fresh.length ? (p.exclusive_cents || 100) + (p.exclusive_extra_cents || 50) * (fresh.length - 1) : 0) + (fresh.length ? addonCents() : 0);
    const n = q.zip_total;
    const c = fresh.length ? Math.max(Math.ceil(n * per - 1e-9), Math.round(p.min_order_usd * 100)) : 0;
    q.count = n; q.price_cents = c;
    $("qCount").textContent = `${n.toLocaleString()} properties`;
    $("qPrice").textContent = usd(c);
    $("qRate").textContent = fresh.length
      ? `(${fresh.length} measurement${fresh.length === 1 ? "" : "s"} locked × all ${n.toLocaleString()} homes at ${usd(per)} each${h.length ? ", added to your exclusive" : ""})`
      : "(You already hold these in this ZIP.)";
    $("memberLine").innerHTML = '<span class="muted">Exclusive pricing is the same for members and non-members.</span>';
    document.querySelectorAll("#typePick [data-c]").forEach((el) => {
      const k = el.dataset.c;
      el.textContent = k === "value" ? `+${cents(p.value_cents ?? 5)}` : taken().includes(k) ? "Taken" : h.includes(k) ? "Yours"
        : (!h.length && fresh[0] === k) ? usd(p.exclusive_cents || 100) : `+${cents(p.exclusive_extra_cents || 50)}`;
    });
  }
  // ---- several ZIPs in one order: each priced on its own, per property, with the same choices
  function priceOf(qq) {
    const p = qq.pricing || pricing;
    if (product === "exclusive") {
      const h = Object.entries(qq.locks || {}).filter(([, l]) => l.yours).map(([k]) => k);
      const blocked = picked.filter((k) => (qq.taken_types || []).includes(k));
      const fresh = measuresPicked().filter((k) => !h.includes(k));
      const per = (h.length ? (p.exclusive_extra_cents || 50) * fresh.length
        : fresh.length ? (p.exclusive_cents || 100) + (p.exclusive_extra_cents || 50) * (fresh.length - 1) : 0) + (fresh.length ? addonCents() : 0);
      const n = qq.zip_total;
      return { n, cents: fresh.length ? Math.max(Math.ceil(n * per - 1e-9), Math.round(p.min_order_usd * 100)) : 0,
               blocked: blocked.length ? "Another company holds this ZIP for " + blocked.join(", ") : "" };
    }
    if (product === "premier" && (qq.taken_types || []).length) return { n: 0, cents: 0, blocked: "Premier lists are paused in this ZIP (it's exclusive to another company)." };
    const vb = qq.value_bands || { bands: [] }, mult = p.value_multipliers || [1];
    let raw = 0, n = 0;
    if (vb.bands.length) for (let i = vLo; i <= vHi; i++) { n += vb.bands[i] || 0; raw += (vb.bands[i] || 0) * perHome() * (mult[Math.min(i, mult.length - 1)] || 1); }
    else { n = qq.zip_total; raw = n * perHome(); }
    raw += n * flatAddon();
    const full = n ? Math.max(Math.ceil(raw - 1e-9), Math.round(p.min_order_usd * 100)) : 0;
    const blocked = picked.filter((k) => (qq.taken_types || []).includes(k));
    return { n, cents: qq.member ? Math.round(full / 2) : full, blocked: blocked.length ? "Another company holds this ZIP for " + blocked.join(", ") : "" };
  }
  function drawCart() {
    if (!q) return;
    const all = [q, ...extra];
    const parts = all.map(priceOf);
    $("cartRows").innerHTML = all.map((qq, i) => `<div class="zr"><b>ZIP ${esc(qq.zip)}</b><span class="muted">${parts[i].n.toLocaleString()} homes · ${usd(parts[i].cents)}</span>
        ${i ? `<button type="button" class="x" data-rm="${i - 1}" aria-label="Remove ZIP ${esc(qq.zip)}">×</button>` : "<span></span>"}
        ${parts[i].blocked ? `<div class="blk">${esc(parts[i].blocked)}</div>` : ""}</div>`).join("");
    $("cartRows").querySelectorAll("[data-rm]").forEach((b) => b.onclick = () => { extra.splice(+b.dataset.rm, 1); updatePrice(); });
    if (extra.length) {
      const tot = parts.reduce((a, x) => a + x.cents, 0), homes = parts.reduce((a, x) => a + x.n, 0);
      $("qPrice").textContent = usd(tot);
      $("qRate").textContent = `(${all.length} ZIP codes, ${homes.toLocaleString()} homes, each ZIP priced per property)`;
      $("bBtn").textContent = product === "exclusive" ? `Lock ${all.length} ZIPs` : `Buy ${all.length} ZIPs`;
    } else {
      $("bBtn").textContent = product === "exclusive" ? "Lock this ZIP" : "Buy sheet";
    }
  }
  async function addZip() {
    const z = $("addZip").value.trim();
    if (!/^\d{5}$/.test(z)) { $("cartMsg").innerHTML = '<span class="err">Enter a 5-digit ZIP.</span>'; return; }
    if ([q, ...extra].some((x) => x.zip === z)) { $("cartMsg").innerHTML = '<span class="err">That ZIP is already in this order.</span>'; return; }
    if (extra.length >= 24) { $("cartMsg").innerHTML = '<span class="err">Up to 25 ZIPs per order.</span>'; return; }
    $("cartMsg").innerHTML = '<span class="spinner"></span>Counting properties…';
    try {
      const qq = await api(`/api/data/quote?zip=${z}&classes=${encodeURIComponent($("cls").value)}`);
      if (!qq.count) throw new Error(`No matching properties in ZIP ${z} inside our coverage area yet.`);
      extra.push(qq); $("addZip").value = ""; $("cartMsg").textContent = ""; updatePrice();
    } catch (e) { $("cartMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  }
  $("addZipBtn").addEventListener("click", addZip);
  $("addZip").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addZip(); } });

  function updatePrice() {
    if (!q || !q.zip_total) return;
    if (product === "exclusive") { exclusivePrice(); return drawCart(); }
    const vb = q.value_bands || { bands: [] };
    const mult = pricing.value_multipliers || [1];
    const n = selCount();
    let raw = 0;
    if (vb.bands.length) for (let i = vLo; i <= vHi; i++) raw += vb.bands[i] * perHome() * (mult[Math.min(i, mult.length - 1)] || 1);
    else raw = n * perHome();
    raw += n * flatAddon();
    const full = n ? Math.max(Math.ceil(raw - 1e-9), Math.round(pricing.min_order_usd * 100)) : 0;
    q.count = n;
    $("qCount").textContent = `${n.toLocaleString()} properties`;
    $("vSel").textContent = n === q.zip_total ? `all ${n.toLocaleString()} homes` : `${n.toLocaleString()} of ${q.zip_total.toLocaleString()} homes`;
    const cents = q.member ? Math.round(full / 2) : full;
    q.price_cents = cents;
    $("qPrice").innerHTML = q.member ? `${usd(cents)} <s class="muted" style="font-size:.5em;font-weight:500">${usd(full)}</s>` : usd(cents);
    const atMin = full <= pricing.min_order_usd * 100;
    const nm = measuresPicked().length;
    const what = product === "premier" ? "Premier report" : `${nm} measurement${nm === 1 ? "" : "s"}${picked.includes("value") ? " + assessed value" : ""}`;
    const avg = n ? raw / n : perHome() + flatAddon();
    const banded = vb.bands.length && Math.abs(avg - perHome() - flatAddon()) > 0.01;
    $("qRate").textContent = `(${what} × ${n.toLocaleString()} homes at ${banded ? "about " : ""}${+(avg / (q.member ? 2 : 1)).toFixed(2)}¢ each${banded ? ", priced by home value" : ""}${atMin ? `, $${pricing.min_order_usd} minimum` : ""})`;
    $("memberLine").innerHTML = q.member ? '<span class="ok">Member price: half off.</span>'
      : `<span class="muted">Members pay half. <a href="/join">$115/month</a> includes unlimited single-property reports and the CRM.</span>`;
    drawCart();
    // what each unticked option would add
    document.querySelectorAll("#typePick [data-c]").forEach((el) => {
      const on = picked.includes(el.dataset.c);
      el.textContent = el.dataset.c === "value" ? `+${+(pricing.value_cents ?? 5)}¢`
        : taken().includes(el.dataset.c) ? "Taken" : on ? (measuresPicked()[0] === el.dataset.c ? `${pricing.rate_cents}¢` : `+${pricing.extra_type_cents}¢`) : `+${pricing.extra_type_cents}¢`;
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
      const show = ["address", "city", "lawn_sqft", "parkway_sqft", "driveway_sqft", "sidewalk_sqft", "building_sqft", "lot_sqft", "beds_sqft", "wall_sqft", "est_windows"].filter((k) => r.keys.includes(k));
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
      const body = { email, classes: $("cls").value.split(","), types: picked, product, value_lo: vLo, value_hi: vHi };
      const r = extra.length
        ? await api("/api/data/orders", { method: "POST", json: { ...body, zips: [q.zip, ...extra.map((x) => x.zip)] } })
        : await api("/api/data/order", { method: "POST", json: { ...body, zip: q.zip } });
      location.href = r.checkout_url || r.order_url;
    } catch (e) {
      $("bMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
      $("bBtn").disabled = false;
    }
  });

  const pre = new URLSearchParams(location.search).get("zip");
  if (pre) { $("zip").value = pre; check(); }
})();
