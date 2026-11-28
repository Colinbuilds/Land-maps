// Data storefront: pick the area (a ZIP or a drawn shape) and the job size, see the exact homes and price,
// preview up to 15 free (up to 5 of theirs + up to 10 matching), buy exactly those homes.
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
    setArea("zip");
    $("result").classList.remove("hidden");
    if (window.__pvReset) window.__pvReset();
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
  let pricing = { rate_cents: 10, extra_type_cents: 5, min_order_usd: 25 };
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
    $("vsPer").textContent = `${cents(p.rate_cents)} a home (one measurement; +${cents(p.extra_type_cents)} per extra)`;
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
    const prem = product === "premier";
    $("typePick").classList.toggle("hidden", prem);
    $("stdIntro").classList.toggle("hidden", prem);
    $("premIntro").classList.toggle("hidden", !prem);
    $("exStd").classList.toggle("hidden", prem); $("exPrem").classList.toggle("hidden", !prem);
    (prem ? $("exPrem") : $("exStd")).open = true;  // the example drops down for the chosen list
    $("bBtn").textContent = prem ? "Buy Premier list" : "Buy sheet";
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
    if (!q) return;
    if (!extra.length) return selQuote();  // one ZIP or a drawn area: the server prices the exact homes
    if (!q.zip_total) return;
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

  // ---- free preview: up to 5 of the contractor's addresses + up to 10 matching homes
  const PV_CHOSEN = 5;
  const pv = Array.from({ length: PV_CHOSEN }, () => ({ text: "", state: "", msg: "", seq: 0 }));
  const normAddr = (t) => String(t || "").toUpperCase().replace(/[.,]/g, " ").replace(/\s+/g, " ").trim();
  let pvLast = null;
  async function postJSON(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let data = {}; try { data = await r.json(); } catch (e) {}
    return { ok: r.ok, status: r.status, data };
  }
  const areaReady = () => !!q && (areaMode === "zip" ? !!q.zip : !!drawPoly);
  function pvRender() {
    const box = $("pvList");
    if (!box.dataset.built) {
      box.dataset.built = "1";
      box.innerHTML = pv.map((_, i) => `<div class="pv-row" data-i="${i}"><span class="n">${i + 1}</span>
        <input type="text" list="pvSuggest" autocomplete="off" placeholder="${i ? "Another address (optional)" : "An address you service (optional)"}" aria-label="Address ${i + 1} you already service">
        <span class="st" aria-hidden="true"></span><div class="pv-msg" role="status"></div></div>`).join("");
      box.querySelectorAll(".pv-row").forEach((row) => {
        const i = +row.dataset.i, inp = row.querySelector("input");
        let t = null;
        inp.addEventListener("input", () => {
          pv[i] = { ...pv[i], text: inp.value, state: "", msg: "", checked: null, pin: null }; pvPaint();
          clearTimeout(t);
          t = setTimeout(() => pvSuggest(inp.value), 250);
        });
        inp.addEventListener("change", () => pvCheck(i));
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); pvCheck(i); } });
      });
    }
    pvPaint();
  }
  function pvPaint() {
    $("pvList").querySelectorAll(".pv-row").forEach((row) => {
      const s = pv[+row.dataset.i], inp = row.querySelector("input");
      if (document.activeElement !== inp && inp.value !== s.text) inp.value = s.text;
      inp.classList.toggle("bad", s.state === "bad");
      inp.classList.toggle("good", s.state === "good");
      row.querySelector(".st").textContent = s.state === "good" ? "✅" : s.state === "bad" ? "⚠️" : s.state === "busy" ? "…" : "";
      const m = row.querySelector(".pv-msg");
      m.className = "pv-msg " + (s.state === "bad" ? "err" : "ok");
      m.textContent = s.msg || "";
    });
    const filled = pv.filter((s) => s.text.trim());
    const ok = filled.every((s) => s.state === "good");
    $("sBtn").disabled = !areaReady() || !ok;
  }
  async function pvSuggest(text) {
    if (!q || text.trim().length < 3) return;
    try {
      const list = await api(`/api/suggest?q=${encodeURIComponent(text + (q.zip ? " " + q.zip : ""))}`);
      $("pvSuggest").innerHTML = list.map((s) => `<option value="${esc(s.text)}"></option>`).join("");
    } catch (e) {}
  }
  async function pvCheck(i) {
    if (!areaReady()) return;
    const text = pv[i].text.trim();
    if (!text) { pv[i] = { ...pv[i], state: "", msg: "" }; pvPaint(); return; }
    const dup = pv.findIndex((s, j) => j !== i && s.text.trim() && normAddr(s.text) === normAddr(text));
    if (dup >= 0) { pv[i] = { ...pv[i], state: "bad", msg: `Same as address ${dup + 1}. Pick different homes.` }; pvPaint(); return; }
    if (pv[i].checked === text && (pv[i].state === "good" || pv[i].state === "bad")) return;  // already checked
    const seq = ++pv[i].seq;
    pv[i] = { ...pv[i], state: "busy", msg: "Checking…", checked: text }; pvPaint();
    const r = await postJSON("/api/data/sample/address", { spec: spec(), address: text });
    if (seq !== pv[i].seq) return;
    if (r.ok) {
      const samePin = pv.findIndex((s, j) => j !== i && s.pin && s.pin === r.data.pin);
      pv[i] = samePin >= 0
        ? { ...pv[i], state: "bad", msg: `That's the same property as address ${samePin + 1}. Pick a different home.`, pin: null }
        : { ...pv[i], state: "good", msg: `Found: ${r.data.address}`, pin: r.data.pin };
    } else {
      const p = (r.data.problems || [])[0];
      pv[i] = { ...pv[i], state: "bad", pin: null,
                msg: (p ? p.message : (r.data.detail || "We couldn't check that address.")) + " Replace it or leave it blank." };
    }
    pvPaint();
  }
  function pvReset() {  // a new area: start the addresses over
    pv.forEach((s, i) => { pv[i] = { text: "", state: "", msg: "", seq: s.seq + 1 }; });
    $("sMsg").textContent = "";
    pvRender();
  }
  window.__pvReset = pvReset;
  pvRender();

  // ---- the contractor's illustrative price: rate per 1,000 sq ft, minimum, optional size tiers
  function addTier(from = "", rate = "") {
    const d = document.createElement("div");
    d.className = "tier";
    d.innerHTML = `<label class="small">From sq ft<input type="number" min="0" step="100" class="tFrom" value="${esc(from)}" placeholder="e.g. 8000"></label>
      <label class="small">$ per 1,000<input type="number" min="0" step="0.25" class="tRate" value="${esc(rate)}" placeholder="e.g. 4.75"></label>
      <button type="button" aria-label="Remove tier">×</button>`;
    d.querySelector("button").onclick = () => d.remove();
    $("qTiers").appendChild(d);
  }
  $("qAddTier").onclick = () => addTier();
  function quoteParams() {
    const rate = +$("qRateIn").value || 0, minimum = +$("qMinIn").value || 0;
    const tiers = [...document.querySelectorAll("#qTiers .tier")].map((t) => ({ from: +t.querySelector(".tFrom").value, rate: +t.querySelector(".tRate").value }))
      .filter((t) => t.from > 0 && t.rate > 0);
    return rate || minimum || tiers.length ? { rate, minimum, tiers } : null;
  }

  $("sBtn").addEventListener("click", async () => {
    if (!areaReady()) return;
    const email = $("sEmail").value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { $("sMsg").innerHTML = '<span class="err">Enter your email for the preview.</span>'; return; }
    remember(email);
    $("sBtn").disabled = true;
    $("sMsg").innerHTML = '<span class="spinner"></span>Pulling your homes…';
    const quote = quoteParams();
    try {
      const res = await postJSON("/api/data/sample", { email, spec: spec(), addresses: pv.map((s) => s.text.trim()).filter(Boolean), quote });
      if (res.status === 422 && res.data.problems) {
        res.data.problems.forEach((p) => { if (p.index != null) {
          const k = pv.findIndex((s) => s.text.trim() && normAddr(s.text) === normAddr(p.address));
          const i = k >= 0 ? k : p.index;
          pv[i] = { ...pv[i], state: "bad", pin: null, checked: pv[i].text.trim(), msg: p.message + " Replace it or leave it blank." }; } });
        pvPaint();
        $("sMsg").innerHTML = `<span class="err">${esc(res.data.problems.length === 1 && res.data.problems[0].index == null ? res.data.problems[0].message : "Fix the marked address" + (res.data.problems.length > 1 ? "es" : "") + " and try again.")} Your free preview hasn't been used.</span>`;
        return;
      }
      if (!res.ok) throw new Error(res.data.detail || "Something went wrong. Try again.");
      const r = res.data;
      pvLast = { r, quote };
      $("sMsg").innerHTML = `<span class="ok">Here are ${r.rows.length} homes: ${r.yours} of yours and ${r.more} matching.</span>`;
      $("pvTitle").textContent = `Your free preview: ${r.rows.length} homes`;
      $("pvNotes").innerHTML = [`${esc(r.area_label)} · ${esc(r.filter_label)}`].concat((r.notes || []).map(esc)).join("<br>");
      const num = (k) => /_sqft$|_ft$|_count$|^est_/.test(k);
      const sizeKey = SIZE_COL[r.measure] || "lot_sqft";
      const show = ["address", "owner", sizeKey].concat(["lawn_sqft", "driveway_sqft", "sidewalk_sqft", "other_hardscape_sqft", "beds_sqft", "wall_sqft"].filter((k) => k !== sizeKey && r.keys.includes(k))).concat(["quality"])
        .filter((k, i, a) => a.indexOf(k) === i && (r.keys.includes(k) || k === "owner"));
      const lab = Object.fromEntries(r.columns.map((c) => [c.key, c.label]));
      lab.owner = lab.owner || "Owner";
      const money = (v) => v == null ? "–" : "$" + (+v).toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 0 : 2 });
      const body = (rows) => rows.map((row) => `<tr>${show.map((k) => `<td class="${num(k) ? "num" : ""}">${num(k) ? fmt(row[k]) : esc(row[k] ?? "")}</td>`).join("")}${quote ? `<td class="num"><b>${money(row.quote)}</b></td>` : ""}</tr>`).join("");
      const cols = show.length + (quote ? 1 : 0);
      const group = (title, rows) => rows.length ? `<tr class="grp"><td colspan="${cols}">${title} (${rows.length})</td></tr>` + body(rows) : "";
      $("sampleTable").innerHTML = `<thead><tr>${show.map((k) => `<th class="${num(k) ? "num" : ""}">${esc(lab[k] || k)}${k === sizeKey && r.measure ? " ★" : ""}</th>`).join("")}${quote ? '<th class="num">Your price</th>' : ""}</tr></thead>
        <tbody>${group("Your properties", r.rows.filter((x) => x.group === "yours"))}${group("Matching homes", r.rows.filter((x) => x.group === "more"))}</tbody>`;
      $("sDl").href = URL.createObjectURL(new Blob([r.csv], { type: "text/csv" }));
      $("sDl").download = `preview_${(r.zip || "area")}.csv`;
      $("pcOut").classList.add("hidden"); $("pcOut").innerHTML = "";
      $("sampleCard").classList.remove("hidden");
      $("sampleCard").scrollIntoView({ behavior: "smooth" });
    } catch (e) {
      $("sMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    } finally { pvPaint(); }
  });
  $("pcBtn").addEventListener("click", async () => {
    if (!pvLast) return;
    const rows = pvLast.r.rows, row = rows.find((x) => x.group === "more") || rows[0];
    $("pcBtn").disabled = true;
    $("pcOut").classList.remove("hidden");
    $("pcOut").innerHTML = '<span class="small"><span class="spinner"></span>Making the postcard (about 10 seconds)…</span>';
    try {
      const r = await api("/api/data/preview/postcard", { method: "POST", json: { token: pvLast.r.token, row, quote: pvLast.quote } });
      $("pcOut").innerHTML = r.pages.map((src, i) => `<img src="${src}" alt="Postcard ${i ? "back" : "front"} for ${esc(row.address || "a preview home")}">`).join("")
        + `<p class="small muted" style="grid-column:1/-1;margin:0">${esc(row.address || "")}${r.price ? ` · your price ${"$" + r.price.toLocaleString()}` : " · add your rate above to print a price"}. Print-ready postcards for every home on a list come with a Premier list.</p>`;
    } catch (e) {
      $("pcOut").innerHTML = `<span class="err small">${esc(e.message)}</span>`;
    } finally { $("pcBtn").disabled = false; }
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
        : await api("/api/data/select/order", { method: "POST", json: { email, spec: spec(), coupon: ($("bCoupon").value || "").trim() || null } });
      location.href = r.checkout_url || r.order_url;
    } catch (e) {
      $("bMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
      $("bBtn").disabled = false;
    }
  });

  // ================================================ area + job size (one server-side selection)
  let areaMode = "zip", drawPoly = null, drawPts = [], drawLine = null, drawShape = null, drawing = false, S = null, selT = null, selSeq = 0;
  const SIZE_COL = { lawn: "lawn_sqft", driveway: "driveway_sqft", sidewalk: "sidewalk_sqft", patios: "other_hardscape_sqft",
                     structures: "structures_sqft", beds: "beds_sqft", building: "building_sqft", walls: "wall_sqft" };
  const typeLabel = (k) => ((q && q.type_catalog) || []).concat(drawCatalog).find((t) => t.key === k)?.label || k;
  let drawCatalog = [];
  try { drawCatalog = (await api("/api/data/types")).types.map((t) => ({ key: t.key, label: t.label })); } catch (e) {}
  // the job-size measurement list: every measurement that's sold (not the value add-on)
  $("fMeasure").innerHTML += drawCatalog.filter((t) => SIZE_COL[t.key]).map((t) => `<option value="${t.key}">${esc(t.label.split(" (")[0].split(" +")[0])}</option>`).join("");
  const fNum = (id) => { const v = $(id).value.trim(); return v === "" ? null : Math.max(0, +v); };
  function spec() {
    const m = $("fMeasure").value;
    const f = m && (fNum("fMin") != null || fNum("fMax") != null) ? { measure: m, min: fNum("fMin"), max: fNum("fMax") } : null;
    const vb = q && q.value_bands && q.value_bands.bands.length;
    const value = areaMode === "zip" && vb && (vLo > 0 || vHi < q.value_bands.bands.length - 1) ? [vLo, vHi] : null;
    return { area: areaMode === "draw" ? { polygon: drawPoly } : { zip: q && q.zip }, classes: $("cls").value.split(","),
             filter: f, types: picked, product: product === "premier" ? "premier" : "standard", value };
  }
  function explainFilter() {
    const m = $("fMeasure").value, lo = fNum("fMin"), hi = fNum("fMax");
    const name = m ? $("fMeasure").selectedOptions[0].textContent : "";
    const fmt = (v) => v.toLocaleString();
    const rng = lo != null && hi != null ? `${fmt(lo)}–${fmt(hi)} sq ft` : lo != null ? `${fmt(lo)} sq ft and up` : hi != null ? `up to ${fmt(hi)} sq ft` : "";
    const extras = measuresPicked().filter((k) => k !== m).map((k) => typeLabel(k).split(" (")[0].split(" +")[0]);
    $("fExplain").innerHTML = m && rng
      ? `<b>${esc(name)}: ${esc(rng)}</b> selects the homes (sizes exactly at a limit count). ${extras.length ? `${esc(extras.join(", "))} ${extras.length === 1 ? "is" : "are"} added as extra column${extras.length === 1 ? "" : "s"} in the spreadsheet.` : ""} Homes with no ${esc(name.toLowerCase())} measurement are left out.`
      : m ? "Enter a minimum, a maximum or both." : "Every home in the area. Pick a measurement to buy only one job size.";
  }
  function onFilter() {
    const m = $("fMeasure").value;
    if (m && !picked.includes(m)) {  // the job-size measurement is always a paid column
      picked = [m, ...picked];
      document.querySelectorAll("#typePick input").forEach((x) => { x.checked = picked.includes(x.value); x.closest("label").classList.toggle("on", x.checked); });
    }
    explainFilter();
    updatePrice();
  }
  ["fMeasure"].forEach((id) => $(id).addEventListener("change", onFilter));
  ["fMin", "fMax"].forEach((id) => $(id).addEventListener("input", onFilter));
  document.querySelectorAll(".js-presets button").forEach((b) => b.onclick = () => {
    $("fMeasure").value = b.dataset.m; $("fMin").value = b.dataset.min; $("fMax").value = b.dataset.max; onFilter();
  });
  document.getElementById("typePick").addEventListener("change", () => setTimeout(() => { explainFilter(); }, 0));
  explainFilter();

  function selQuote() {
    clearTimeout(selT);
    // show it's working straight away: never let anyone buy at a price that's about to change
    if (q && !(areaMode === "draw" && !drawPoly)) { $("qPrice").innerHTML = '<span class="spinner"></span>'; $("bBtn").disabled = true; }
    selT = setTimeout(selQuoteNow, 250);
  }
  async function selQuoteNow() {
    if (!q) return;
    if (areaMode === "draw" && !drawPoly) { renderSel(null, "Draw your area on the map to see the homes and the price."); return; }
    const seq = ++selSeq;
    $("qPrice").innerHTML = '<span class="spinner"></span>';
    let r;
    try { r = await api("/api/data/select/quote", { method: "POST", json: { spec: spec() } }); }
    catch (e) { if (seq === selSeq) renderSel(null, e.message); return; }
    if (seq !== selSeq) return;
    S = r; renderSel(r);
  }
  function renderSel(r, err) {
    $("selSum").dataset.n = (+$("selSum").dataset.n || 0) + 1;  // a fresh answer (the browser tests wait on this)
    const filtered = !!(r && (r.controls || areaMode === "draw"));
    document.querySelector(".zcart").classList.toggle("hidden", filtered || areaMode === "draw");
    if (!r) {
      $("selSum").innerHTML = err ? `<div class="note">${esc(err)}</div>` : "";
      $("qPrice").textContent = ""; $("qRate").textContent = "";
      $("bBtn").disabled = true; return;
    }
    const n = r.count || 0, p = r.price || {};
    q.count = n; q.price_cents = p.price_cents;
    $("qZip").textContent = r.area_label + (r.controls ? "" : ` · ${$("cls").selectedOptions[0].textContent}`);
    $("qCount").textContent = `${n.toLocaleString()} home${n === 1 ? "" : "s"} match`;
    $("qBy").textContent = r.filter_label;
    const cov = (r.coverage || []).map((c) => `ZIP ${c.zip}: ${c.excluded ? "left out (" + c.excluded + ")" : c.ready ? `${(c.homes || 0).toLocaleString()} measured homes${c.measured ? `, measured ${new Date(c.measured * 1000).toLocaleDateString()}` : ""}` : (r.measured_after ? "measured after you buy" : "not measured yet" + (c.measuring ? " (in line to be measured)" : ""))}`);
    const img = (r.imagery || []).map((i) => `${i.county}: ${i.imagery || "aerial photo"}${i.lidar_year ? `, LiDAR ${i.lidar_year}` : ""}`);
    const cols = (r.columns || []).map((c) => c.label);
    const per = p.base_cents_per_home != null ? `${+(+p.base_cents_per_home).toFixed(2)}¢` : "";
    const lines = [];
    if (n) {
      lines.push(`<div class="ln"><span>${n.toLocaleString()} homes × ${per} a home <span class="muted">(${(r.types || []).length - (r.types || []).includes("value")} measurement${(r.types || []).length === 1 ? "" : "s"}${r.types.includes("value") ? " + assessed value" : ""}${r.price && r.price.subtotal_cents > Math.ceil(n * (p.base_cents_per_home || 0)) + 1 ? ", higher-value homes priced up" : ""})</span></span><span>${usd(p.subtotal_cents || 0)}</span></div>`);
      if (p.minimum_applied) lines.push(`<div class="ln"><span>Minimum order</span><span>${usd(p.minimum_cents)}</span></div>`);
      if (p.member_discount) lines.push(`<div class="ln"><span>Member discount (${Math.round((p.discount || 0.5) * 100)}% off)</span><span>−${usd((p.full_price_cents || 0) - (p.price_cents || 0))}</span></div>`);
      lines.push(`<div class="ln tot"><span>Total</span><span>${usd(p.price_cents || 0)}</span></div>`);
    }
    $("selSum").innerHTML = `
      ${n ? `<div><b>Columns:</b> ${cols.map(esc).join(", ")}</div>` : ""}
      ${r.flagged ? `<div>${r.flagged.toLocaleString()} of these ${r.flagged === 1 ? "is" : "are"} flagged "review" (worth a second look, e.g. heavy tree cover). ${r.flagged === 1 ? "It's" : "They're"} included and marked in the spreadsheet.</div>` : ""}
      <div><b>Coverage:</b> ${cov.map(esc).join(" · ") || "–"}</div>
      ${img.length ? `<div class="muted"><b>Imagery:</b> ${img.map(esc).join(" · ")}</div>` : ""}
      ${(r.notes || []).map((x) => `<div class="note">${esc(x)}</div>`).join("")}
      ${n ? lines.join("") : `<div class="note">No homes match. Widen the size range or the area to buy.</div>`}`;
    $("qPrice").innerHTML = n ? (p.member_discount ? `${usd(p.price_cents)} <s class="muted" style="font-size:.5em;font-weight:500">${usd(p.full_price_cents)}</s>` : usd(p.price_cents)) : "";
    $("qRate").textContent = n ? `(${n.toLocaleString()} homes${p.minimum_applied ? `, $${(p.minimum_cents / 100).toFixed(0)} minimum order` : ""}${r.measured_after ? ", measured after you buy" : ""})` : "";
    $("memberLine").innerHTML = r.member ? '<span class="ok">Member price: half off.</span>'
      : `<span class="muted">Members pay half: ${n ? usd(Math.round((p.full_price_cents || 0) / 2)) : "–"} for this list. <a href="/join">Membership</a></span>`;
    $("bBtn").disabled = !r.can_buy;
    $("bBtn").textContent = !n ? "Nothing to buy" : product === "premier" ? `Buy Premier list · ${usd(p.price_cents)}` : `Buy ${n.toLocaleString()} homes · ${usd(p.price_cents)}`;
    $("cN").value = n || selCount(); calc();
  }

  // ---- area tabs and the drawing tool
  function setArea(mode) {
    areaMode = mode;
    document.querySelectorAll(".areatabs button").forEach((b) => { b.classList.toggle("on", b.dataset.area === mode); b.setAttribute("aria-selected", b.dataset.area === mode); });
    document.querySelector(".zipform").classList.toggle("hidden", mode !== "zip");
    $("drawIntro").classList.toggle("hidden", mode !== "draw");
    $("drawBar").classList.toggle("hidden", mode !== "draw");
    document.querySelector(".dp .valuepick").classList.toggle("hidden", mode === "draw");
    if (mode === "zip" && map) { clearDraw(false); }
  }
  document.querySelectorAll(".areatabs button").forEach((b) => b.onclick = () => {
    setArea(b.dataset.area);
    if (b.dataset.area === "draw") startDraw();
  });
  $("drawStart").onclick = () => startDraw();
  function ensureDrawQ() {  // a drawn area has no single ZIP: give the page the same shape of data
    if (!q || q.zip) {
      q = { zip: null, drawn: true, count: 0, zip_total: 0, pricing: pricing, type_catalog: drawCatalog,
            value_bands: { bands: [], labels: [] }, taken_types: [], locks: {}, columns: [] };
      buildTypePicker(drawCatalog); markLocks();
      if (window.__pvReset) window.__pvReset();
    }
  }
  function startDraw() {
    setArea("draw");
    ensureDrawQ();
    $("result").classList.remove("hidden");
    if (!map) map = makeMap("zmap", cfg);
    window.__selMap = map; window.__selSpec = spec;  // read by the browser tests
    bindDraw();
    if (zl) { map.removeLayer(zl); zl = null; }
    if (!drawPoly && !drawPts.length) {
      const c = cfg.home_center || [41.2565, -95.9345];
      map.setView(c, 14);
    }
    setTimeout(() => map.invalidateSize(), 50);
    clearDraw(true);
    $("zmap").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function clearDraw(begin) {
    drawPts = []; drawPoly = null; drawing = !!begin;
    if (drawLine) { map.removeLayer(drawLine); drawLine = null; }
    if (drawShape) { map.removeLayer(drawShape); drawShape = null; }
    $("drawHint").textContent = begin ? "Click the map to add corners. Click the first corner (or Finish) to close the shape." : "";
    $("zmap").classList.toggle("drawing", !!begin);
    if (begin) renderSel(null, "Draw your area on the map to see the homes and the price.");
  }
  function redrawLine() {
    if (drawLine) map.removeLayer(drawLine);
    drawLine = L.layerGroup([L.polyline(drawPts, { color: "#f59e0b", weight: 3, dashArray: "6 6" })]).addTo(map);
    if (drawPts.length) {  // the first corner: clicking it closes the shape
      const first = L.circleMarker(drawPts[0], { radius: 8, color: "#f59e0b", fillColor: "#fff", fillOpacity: 1, bubblingMouseEvents: false }).addTo(drawLine);
      first.on("click", (e) => { L.DomEvent.stop(e); if (drawing && drawPts.length >= 3) finishDraw(); });
    }
  }
  function finishDraw() {
    if (drawPts.length < 3) { $("drawHint").textContent = "Add at least 3 corners first."; return; }
    drawing = false;
    $("zmap").classList.remove("drawing");
    if (drawLine) { map.removeLayer(drawLine); drawLine = null; }
    const ring = drawPts.map((p) => [+p[1].toFixed(6), +p[0].toFixed(6)]);
    ring.push(ring[0]);
    drawPoly = { type: "Polygon", coordinates: [ring] };
    if (drawShape) map.removeLayer(drawShape);
    drawShape = L.polygon(drawPts, { color: "#f59e0b", weight: 3, fillOpacity: 0.12 }).addTo(map);
    $("drawHint").textContent = "Area drawn. Redraw or Clear to change it.";
    if (window.__pvReset) window.__pvReset();
    updatePrice();
  }
  function bindDraw() {
    if (!map || map.__drawBound) return;
    map.__drawBound = true;
    map.on("click", (e) => {
      if (areaMode !== "draw" || !drawing) return;
      const p = [e.latlng.lat, e.latlng.lng];
      if (drawPts.length >= 3) {  // clicked near the first corner: close it
        const a = map.latLngToContainerPoint(drawPts[0]), b = map.latLngToContainerPoint(p);
        if (a.distanceTo(b) < 14) return finishDraw();
      }
      drawPts.push(p); redrawLine();
      $("drawHint").textContent = `${drawPts.length} corner${drawPts.length === 1 ? "" : "s"}. ${drawPts.length >= 3 ? "Click the first corner or Finish to close." : "Keep clicking."}`;
    });
  }
  $("drawFinish").onclick = finishDraw;
  $("drawUndo").onclick = () => { if (drawing && drawPts.length) { drawPts.pop(); redrawLine(); } };
  $("drawRedo").onclick = () => { clearDraw(true); };
  $("drawClear").onclick = () => { clearDraw(false); renderSel(null, "Area cleared. Draw a new one, or switch to Search a ZIP."); drawing = true; $("zmap").classList.add("drawing"); };

  const pre = new URLSearchParams(location.search).get("zip");
  if (pre) { $("zip").value = pre; check(); }
})();
