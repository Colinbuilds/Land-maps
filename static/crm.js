// CRM portal: Today, Customers, Estimates, Schedule, Money. Phone first.
(async function () {
  const $ = (id) => document.getElementById(id);
  const usd = (c) => "$" + ((c || 0) / 100).toLocaleString(undefined, { minimumFractionDigits: (c || 0) % 100 ? 2 : 0, maximumFractionDigits: 2 });
  const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const today = iso(new Date());
  const dayName = (s) => new Date(s + "T12:00").toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  const view = (v) => document.querySelector(`[data-view="${v}"]`);
  let me = null, H = null, current = "today";

  // ------------------------------------------------------------ boot / gate
  try { me = await api("/api/roof/me"); } catch (e) { me = null; }
  if (!me) return gate(`<h1>Sign in</h1><p class="sub">Use your company account.</p>
      <div class="card stack"><input id="gEmail" type="email" placeholder="Email" autocomplete="username">
      <input id="gPass" type="password" placeholder="Password" autocomplete="current-password">
      <button id="gBtn">Sign in</button><div id="gMsg" class="small"></div></div>`, () => {
    $("gBtn").onclick = async () => {
      try { await api("/api/roof/login", { method: "POST", json: { email: $("gEmail").value, password: $("gPass").value } }); location.reload(); }
      catch (e) { $("gMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    };
  });
  document.documentElement.style.setProperty("--accent", me.brand_color || "#15803d");
  $("coName").textContent = me.name;
  if (!me.crm) {
    return gate(`<h1>Your CRM</h1><p class="sub">Estimates, scheduling, job photos and money, built on your property reports.</p>
      <div class="card stack">
        <div><b>One tap from report to estimate.</b> Every home you measure becomes a customer with an estimate already priced from your rates.</div>
        <div><b>Schedule and route.</b> Weekly mows and one-off jobs, ordered by stop, checked off from your phone.</div>
        <div><b>Photos and invoices.</b> Before/after photos per job; mark done and the invoice is made.</div>
        <div><b>See the money.</b> Paid this month, what's owed, what's booked and your win rate.</div>
        ${me.member ? `<button id="gOn">Turn on CRM · +$25/month</button>` : `<a class="btn" href="/join">Become a member to add the CRM</a>`}
        <div id="gMsg" class="small"></div></div>`, () => {
      const b = $("gOn");
      if (b) b.onclick = async () => {
        b.disabled = true;
        try { await api("/api/roof/crm", { method: "PUT", json: { on: true } }); location.reload(); }
        catch (e) { $("gMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; b.disabled = false; }
      };
    });
  }
  function gate(html, wire) { $("gate").hidden = false; $("gate").innerHTML = `<div class="gate">${html}</div>`; wire && wire(); }
  $("app").hidden = false; $("tabs").hidden = false;

  // ------------------------------------------------------------ tabs + sheet
  $("tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) go(b.dataset.v); });
  function go(v) {
    current = v;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
    document.querySelectorAll("[data-view]").forEach((s) => s.classList.toggle("hidden", s.dataset.view !== v));
    ({ today: drawToday, customers: drawCustomers, estimates: drawEstimates, schedule: drawSchedule, money: drawMoney })[v]();
    scrollTo(0, 0);
  }
  function sheet(html, wire) {
    close();
    const bg = document.createElement("div");
    bg.className = "sheet-bg"; bg.id = "sheetBg";
    bg.innerHTML = `<div class="sheet" role="dialog" aria-modal="true"><button class="x" id="sx">Close</button>${html}</div>`;
    bg.addEventListener("click", (e) => { if (e.target === bg) close(); });
    document.body.appendChild(bg);
    $("sx").onclick = close;
    wire && wire();
  }
  function close() { const s = $("sheetBg"); if (s) s.remove(); }
  const refresh = () => go(current);
  const pill = (s) => `<span class="pill ${esc(s)}">${esc(s)}</span>`;
  const mapLink = (a) => a ? `https://maps.google.com/?q=${encodeURIComponent(a)}` : "#";

  // ------------------------------------------------------------ Today
  async function drawToday() {
    const v = view("today");
    v.innerHTML = '<p class="sub"><span class="spinner"></span>Loading…</p>';
    H = await api("/api/crm/home");
    const m = H.money, st = H.stats;
    const left = H.today.filter((s) => s.status === "scheduled");
    v.innerHTML = `<h1>${greet()}</h1>
      <div class="sub">${dayName(today)} · ${left.length ? `${left.length} stop${left.length > 1 ? "s" : ""} left, ${usd(left.reduce((a, s) => a + s.price_cents, 0))}` : "Nothing left on today's schedule"}</div>
      <div class="kpis">
        <div class="kpi"><div class="l">Paid this month</div><div class="v">${usd(m.paid_this_month)}</div></div>
        <div class="kpi ${m.overdue ? "warn" : ""}"><div class="l">Owed to you</div><div class="v">${usd(m.owed)}</div></div>
        <div class="kpi"><div class="l">Booked next 30 days</div><div class="v">${usd(m.booked_next_30)}</div></div>
        <div class="kpi"><div class="l">Open estimates</div><div class="v">${usd(m.pipeline)}</div></div>
      </div>
      <div class="quick">
        <button id="qEst">📝<br>Estimate from a report</button>
        <button class="secondary" id="qJob">📅<br>Add a job</button>
        <button class="secondary" id="qCust">👤<br>Add a customer</button>
      </div>
      <div class="sec"><h2>Today's stops</h2><a class="small" href="#" id="toSched">Full week</a></div>
      <div class="list">${H.today.map(stopCard).join("") || '<div class="empty">No jobs today. Tap <b>Add a job</b> or schedule an accepted estimate.</div>'}</div>
      ${H.open_estimates.length ? `<div class="sec"><h2>Waiting on customers</h2></div><div class="list">${H.open_estimates.map(estCard).join("")}</div>` : ""}`;
    $("qEst").onclick = newFromReport;
    $("qJob").onclick = () => jobSheet();
    $("qCust").onclick = () => customerSheet();
    $("toSched").onclick = (e) => { e.preventDefault(); go("schedule"); };
    wireStops(v); wireEst(v);
  }
  const greet = () => { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"; };

  function stopCard(s) {
    const done = s.status !== "scheduled";
    return `<div class="item ${done ? "done" : ""}" data-job="${s.job_id}" data-day="${s.day}">
      <div class="top"><span class="t">${s.time ? esc(s.time) + " · " : ""}${esc(s.customer || "Customer")}</span><span class="p">${usd(s.price_cents)}</span></div>
      <div class="m">${esc(s.title)}${s.repeat !== "none" ? ` · ${esc(s.repeat)}` : ""} · <a href="${mapLink([s.address, s.city].filter(Boolean).join(", "))}" target="_blank">${esc(s.address || "")}</a></div>
      ${s.notes ? `<div class="m">${esc(s.notes)}</div>` : ""}
      <div class="acts">${done ? `${pill(s.status)}${s.invoice_id ? ' <span class="pill unpaid">invoiced</span>' : ""} <button class="secondary" data-act="undo">Undo</button>`
        : `<button data-act="done">✓ Done + invoice</button><button class="secondary" data-act="donenoinv">Done</button><button class="secondary" data-act="skip">Skip</button>`}
        <button class="secondary" data-act="photo">📷 Photo</button>${s.phone ? `<a class="btn secondary" href="tel:${esc(s.phone)}">Call</a>` : ""}</div>
    </div>`;
  }
  function wireStops(root) {
    root.querySelectorAll("[data-job] [data-act]").forEach((b) => b.addEventListener("click", async () => {
      const it = b.closest("[data-job]"), job_id = +it.dataset.job, day = it.dataset.day, act = b.dataset.act;
      if (act === "photo") return takePhoto({ job_id, day });
      const status = act === "skip" ? "skipped" : act === "undo" ? "scheduled" : "done";
      b.disabled = true;
      try { await api("/api/crm/visits", { method: "POST", json: { job_id, day, status, invoice: act === "done" } }); refresh(); }
      catch (e) { alert(e.message); b.disabled = false; }
    }));
  }

  // ------------------------------------------------------------ photos
  let photoCtx = null;
  function takePhoto(ctx) { photoCtx = ctx; $("photoIn").value = ""; $("photoIn").click(); }
  $("photoIn").addEventListener("change", async () => {
    const f = $("photoIn").files[0]; if (!f || !photoCtx) return;
    const fd = new FormData();
    fd.append("file", f);
    Object.entries(photoCtx).forEach(([k, v]) => v != null && fd.append(k, v));
    fd.append("kind", photoCtx.kind || "");
    try {
      const r = await fetch("/api/crm/photos", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json()).detail || "Upload failed");
      toast("Photo saved");
      if (photoCtx.customer_id && $("sheetBg")) openCustomer(photoCtx.customer_id);
    } catch (e) { alert(e.message); }
  });
  function toast(t) {
    const d = document.createElement("div");
    d.textContent = t;
    d.style.cssText = "position:fixed;left:50%;bottom:96px;transform:translateX(-50%);background:#0f172a;color:#fff;padding:10px 16px;border-radius:12px;z-index:99;font-weight:600";
    document.body.appendChild(d); setTimeout(() => d.remove(), 1800);
  }

  // ------------------------------------------------------------ Customers
  async function drawCustomers(q) {
    const v = view("customers");
    v.innerHTML = `<h1>Customers</h1>
      <div class="row search2"><input id="cq" placeholder="Search name, address, phone" value="${esc(q || "")}"><button id="cAdd" style="flex:0 0 auto">+ Add</button></div>
      <div class="list" id="cList" style="margin-top:10px"><p class="sub"><span class="spinner"></span></p></div>`;
    $("cAdd").onclick = () => customerSheet();
    let t = null;
    $("cq").oninput = () => { clearTimeout(t); t = setTimeout(() => load($("cq").value), 250); };
    const load = async (qq) => {
      const rows = await api(`/api/crm/customers${qq ? `?q=${encodeURIComponent(qq)}` : ""}`);
      $("cList").innerHTML = rows.map((c) => `<div class="item tap" data-c="${c.id}">
          <div class="top"><span class="t">${esc(c.name || "No name")}</span>${c.owed_cents ? `<span class="pill unpaid">owes ${usd(c.owed_cents)}</span>` : ""}</div>
          <div class="m">${esc([c.address, c.city].filter(Boolean).join(", "))}${c.phone ? ` · ${esc(c.phone)}` : ""}</div>
          <div class="m">${c.jobs} active job${c.jobs === 1 ? "" : "s"} · ${usd(c.paid_cents)} paid</div></div>`).join("")
        || '<div class="empty">No customers yet. Order a property report, then tap <b>Estimate from a report</b>: the home becomes a customer.</div>';
      $("cList").querySelectorAll("[data-c]").forEach((el) => el.onclick = () => openCustomer(+el.dataset.c));
    };
    load(q);
  }
  function customerSheet(c = {}) {
    sheet(`<h2>${c.id ? "Edit customer" : "New customer"}</h2>
      <label>Name<input id="fName" value="${esc(c.name || "")}" autocomplete="off"></label>
      <div class="two"><label>Phone<input id="fPhone" type="tel" value="${esc(c.phone || "")}"></label>
      <label>Email<input id="fEmail" type="email" value="${esc(c.email || "")}"></label></div>
      <label>Address<input id="fAddr" value="${esc(c.address || "")}"></label>
      <div class="two"><label>City<input id="fCity" value="${esc(c.city || "")}"></label><label>ZIP<input id="fZip" inputmode="numeric" value="${esc(c.zip || "")}"></label></div>
      <label>Notes (gate code, dog, etc.)<textarea id="fNotes">${esc(c.notes || "")}</textarea></label>
      <button id="fSave" style="width:100%;margin-top:14px">Save</button><div id="fMsg" class="small"></div>`, () => {
      $("fSave").onclick = async () => {
        const body = { name: $("fName").value, phone: $("fPhone").value, email: $("fEmail").value, address: $("fAddr").value,
          city: $("fCity").value, zip: $("fZip").value, notes: $("fNotes").value };
        try {
          const r = await api(c.id ? `/api/crm/customers/${c.id}` : "/api/crm/customers", { method: c.id ? "PUT" : "POST", json: body });
          close(); toast("Saved"); if (current === "customers") drawCustomers(); else refresh();
          if (!c.id) openCustomer(r.id);
        } catch (e) { $("fMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
      };
    });
  }
  async function openCustomer(id) {
    const d = await api(`/api/crm/customers/${id}`);
    const c = d.customer;
    sheet(`<h2>${esc(c.name || "Customer")}</h2>
      <div class="sub" style="margin:0">${esc([c.address, c.city, c.zip].filter(Boolean).join(", "))}</div>
      <div class="acts" style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0">
        ${c.phone ? `<a class="btn" href="tel:${esc(c.phone)}">Call</a><a class="btn secondary" href="sms:${esc(c.phone)}">Text</a>` : ""}
        ${c.address ? `<a class="btn secondary" target="_blank" href="${mapLink([c.address, c.city].filter(Boolean).join(", "))}">Map</a>` : ""}
        <button class="secondary" id="cEdit">Edit</button></div>
      ${c.notes ? `<div class="item"><div class="m">${esc(c.notes)}</div></div>` : ""}
      <div class="sec"><h2>Jobs</h2><button class="mini" id="cJob">+ Job</button></div>
      <div class="list">${d.jobs.map((j) => `<div class="item"><div class="top"><span class="t">${esc(j.title)}</span><span class="p">${usd(j.price_cents)}</span></div>
        <div class="m">${j.repeat === "none" ? "Once" : esc(j.repeat)} from ${esc(j.start)}${j.active ? "" : " · stopped"}</div>
        ${j.active ? `<div class="acts"><button class="secondary" data-stop="${j.id}">Stop job</button></div>` : ""}</div>`).join("") || '<div class="empty">No jobs yet.</div>'}</div>
      <div class="sec"><h2>Estimates</h2><button class="mini" id="cEst">+ Estimate</button></div>
      <div class="list">${d.estimates.map(estCard).join("") || '<div class="empty">No estimates.</div>'}</div>
      <div class="sec"><h2>Invoices</h2><button class="mini" id="cInv">+ Invoice</button></div>
      <div class="list">${d.invoices.map(invCard).join("") || '<div class="empty">No invoices.</div>'}</div>
      <div class="sec"><h2>Photos</h2><button class="mini" id="cPhoto">📷 Add</button></div>
      <div class="photos">${d.photos.map((p) => `<a href="/api/crm/photos/${p.id}.jpg" target="_blank"><img loading="lazy" src="/api/crm/photos/${p.id}.jpg" alt="${esc(p.caption || "Job photo")}"></a>`).join("") || '<div class="empty" style="grid-column:1/-1">No photos.</div>'}</div>
      ${d.reports.length ? `<div class="sec"><h2>Property reports</h2></div><div class="list">${d.reports.map((r) => `<div class="item"><div class="top"><span class="t">Report ${esc(r.id)}</span><span class="m">${new Date(r.created * 1000).toLocaleDateString()}</span></div>
        <div class="acts"><a class="btn secondary" target="_blank" href="/r/${r.id}">Open</a><a class="btn secondary" target="_blank" href="/r/${r.id}/pdf">PDF</a><a class="btn secondary" target="_blank" href="/r/${r.id}/postcard.pdf">Postcard</a></div></div>`).join("")}</div>` : ""}`, () => {
      $("cEdit").onclick = () => customerSheet(c);
      $("cJob").onclick = () => jobSheet({ customer_id: c.id });
      $("cEst").onclick = () => estimateSheet({ customer_id: c.id, items: [], customer: c.name });
      $("cInv").onclick = () => invoiceSheet({ customer_id: c.id });
      $("cPhoto").onclick = () => takePhoto({ customer_id: c.id });
      document.querySelectorAll("[data-stop]").forEach((b) => b.onclick = async () => {
        if (!confirm("Stop this job? Past visits stay in the history.")) return;
        await api(`/api/crm/jobs/${b.dataset.stop}`, { method: "PUT", json: { active: false } }); openCustomer(id);
      });
      wireEst($("sheetBg")); wireInv($("sheetBg"));
    });
  }

  // ------------------------------------------------------------ Estimates
  function estCard(e) {
    return `<div class="item" data-e="${e.id}">
      <div class="top"><span class="t">${esc(e.customer || "Customer")}</span><span class="p">${usd(e.total_cents)}</span></div>
      <div class="m">${esc(e.address || "")} · ${new Date(e.created * 1000).toLocaleDateString()} ${pill(e.status)}</div>
      <div class="acts"><button class="secondary" data-ea="edit">Open</button>${e.status === "draft" ? '<button data-ea="send">Send</button>' : ""}
        ${e.status === "sent" ? '<button class="secondary" data-ea="send">Resend</button>' : ""}${e.status === "accepted" || e.status === "sent" ? '<button data-ea="sched">Schedule it</button>' : ""}</div></div>`;
  }
  function wireEst(root) {
    root.querySelectorAll("[data-e] [data-ea]").forEach((b) => b.addEventListener("click", async () => {
      const id = +b.closest("[data-e]").dataset.e, a = b.dataset.ea;
      const all = await api("/api/crm/estimates"); const e = all.find((x) => x.id === id);
      if (a === "edit") estimateSheet(e);
      if (a === "send") sendSheet(`/api/crm/estimates/${id}/send`, "Estimate");
      if (a === "sched") scheduleSheet(e);
    }));
  }
  async function drawEstimates() {
    const v = view("estimates");
    v.innerHTML = `<h1>Estimates</h1><div class="sub">Priced from your rates for each home's real measurements.</div>
      <div class="row"><button id="eNew">📝 Estimate from a report</button><button class="secondary" id="eBlank">Blank estimate</button></div>
      <div class="list" id="eList" style="margin-top:12px"><p class="sub"><span class="spinner"></span></p></div>`;
    $("eNew").onclick = newFromReport;
    $("eBlank").onclick = async () => pickCustomer((c) => estimateSheet({ customer_id: c.id, customer: c.name, items: [] }));
    const rows = await api("/api/crm/estimates");
    $("eList").innerHTML = rows.map(estCard).join("") || '<div class="empty">No estimates yet.</div>';
    wireEst(v);
  }
  async function newFromReport() {
    const reps = await api("/api/roof/reports");
    sheet(`<h2>Estimate from a report</h2><div class="sub">Pick a home you've measured. It becomes a customer and an estimate priced from your rates.</div>
      <div class="list">${reps.slice(0, 40).map((r) => `<div class="item tap" data-r="${esc(r.id)}"><div class="t">${esc(r.address)}</div><div class="m">${new Date(r.created * 1000).toLocaleDateString()} · report ${esc(r.id)}</div></div>`).join("")
        || '<div class="empty">No reports yet. <a href="/reports">Order one</a> first.</div>'}</div>`, () => {
      document.querySelectorAll("[data-r]").forEach((el) => el.onclick = async () => {
        el.innerHTML = '<span class="spinner"></span>Pricing…';
        try { const e = await api("/api/crm/estimates/from-report", { method: "POST", json: { report_id: el.dataset.r } }); estimateSheet(e); }
        catch (x) { alert(x.message); }
      });
    });
  }
  function linesHtml(items) {
    return items.map((it) => `<div class="ln"><input data-f="desc" value="${esc(it.desc)}" placeholder="What"><input data-f="price" type="number" step="0.01" inputmode="decimal" value="${((it.price_cents || 0) * (it.qty || 1) / 100).toFixed(2)}" placeholder="$"><button type="button" aria-label="Remove line">✕</button></div>`).join("");
  }
  function readLines() {
    return [...document.querySelectorAll(".lines .ln")].map((r) => ({ desc: r.querySelector('[data-f="desc"]').value, qty: 1,
      price_cents: Math.round((+r.querySelector('[data-f="price"]').value || 0) * 100) })).filter((x) => x.desc || x.price_cents);
  }
  function wireLines(onChange) {
    const box = document.querySelector(".lines");
    const sum = () => { const t = readLines().reduce((a, i) => a + i.price_cents, 0); $("lTotal").textContent = usd(t); onChange && onChange(t); };
    box.addEventListener("input", sum);
    box.addEventListener("click", (e) => { if (e.target.closest("button")) { e.target.closest(".ln").remove(); sum(); } });
    $("lAdd").onclick = () => { box.insertAdjacentHTML("beforeend", linesHtml([{ desc: "", price_cents: 0 }])); };
    sum();
  }
  function estimateSheet(e) {
    sheet(`<h2>Estimate${e.id ? ` #${e.id}` : ""}</h2><div class="sub">${esc(e.customer || "")} ${e.status ? pill(e.status) : ""}</div>
      <div class="lines">${linesHtml(e.items || [])}</div>
      <button class="secondary mini" id="lAdd" style="margin-top:8px">+ Line</button>
      <div class="total"><span>Total</span><span id="lTotal"></span></div>
      <label>Note to the customer<textarea id="eNote">${esc(e.note || "")}</textarea></label>
      <div class="row" style="margin-top:14px"><button id="eSave" class="secondary">Save</button><button id="eSend">Save &amp; send</button></div>
      <div id="eMsg" class="small"></div>`, () => {
      wireLines();
      const save = async () => {
        const body = { items: readLines(), note: $("eNote").value };
        return e.id ? api(`/api/crm/estimates/${e.id}`, { method: "PUT", json: body })
          : api("/api/crm/estimates", { method: "POST", json: { ...body, customer_id: e.customer_id } });
      };
      $("eSave").onclick = async () => { try { e = await save(); toast("Saved"); close(); refresh(); } catch (x) { $("eMsg").innerHTML = `<span class="err">${esc(x.message)}</span>`; } };
      $("eSend").onclick = async () => { try { e = await save(); sendSheet(`/api/crm/estimates/${e.id}/send`, "Estimate"); } catch (x) { $("eMsg").innerHTML = `<span class="err">${esc(x.message)}</span>`; } };
    });
  }
  async function sendSheet(path, what) {
    const r = await api(path, { method: "POST" });
    sheet(`<h2>${what} ready to send</h2>
      <div class="sub">${r.emailed ? "We emailed it to the customer. " : ""}Text the link or copy it.</div>
      <div class="item"><div class="m" style="word-break:break-all">${esc(r.link)}</div></div>
      <div class="row" style="margin-top:10px"><a class="btn" href="sms:?&body=${encodeURIComponent(r.sms)}">Text it</a><button class="secondary" id="sCopy">Copy link</button><a class="btn secondary" target="_blank" href="${esc(r.link)}">Preview</a></div>`, () => {
      $("sCopy").onclick = async () => { try { await navigator.clipboard.writeText(r.link); toast("Copied"); } catch (e) { prompt("Copy this link", r.link); } };
    });
    if (current !== "today") refresh();
  }
  function repeatSel(v = "none") {
    return `<select id="jRepeat">${[["none", "Once"], ["weekly", "Every week"], ["biweekly", "Every 2 weeks"], ["monthly", "Every month"]]
      .map(([k, l]) => `<option value="${k}" ${k === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  }
  function scheduleSheet(e) {
    sheet(`<h2>Put it on the schedule</h2><div class="sub">${esc(e.customer || "")} · ${usd(e.total_cents)}. Each line becomes a job.</div>
      <div class="two"><label>First visit<input id="jStart" type="date" value="${today}"></label><label>Repeats${repeatSel("weekly")}</label></div>
      <div class="two"><label>Time (optional)<input id="jTime" type="time"></label><label>Until (optional)<input id="jUntil" type="date"></label></div>
      <button id="jGo" style="width:100%;margin-top:14px">Schedule</button><div id="jMsg" class="small"></div>`, () => {
      $("jGo").onclick = async () => {
        try {
          await api(`/api/crm/estimates/${e.id}/schedule`, { method: "POST", json: { start: $("jStart").value, repeat: $("jRepeat").value, time: $("jTime").value || null, until: $("jUntil").value || null } });
          close(); toast("Scheduled"); go("schedule");
        } catch (x) { $("jMsg").innerHTML = `<span class="err">${esc(x.message)}</span>`; }
      };
    });
  }
  async function pickCustomer(then) {
    const rows = await api("/api/crm/customers");
    sheet(`<h2>Which customer?</h2><div class="row search2"><input id="pq" placeholder="Search"><button class="secondary" id="pNew" style="flex:0 0 auto">+ New</button></div>
      <div class="list" id="pList" style="margin-top:8px"></div>`, () => {
      const draw = (q) => { $("pList").innerHTML = rows.filter((c) => !q || `${c.name} ${c.address} ${c.phone}`.toLowerCase().includes(q.toLowerCase()))
        .slice(0, 60).map((c) => `<div class="item tap" data-p="${c.id}"><div class="t">${esc(c.name || "No name")}</div><div class="m">${esc(c.address || "")}</div></div>`).join("") || '<div class="empty">No match.</div>';
        document.querySelectorAll("[data-p]").forEach((el) => el.onclick = () => then(rows.find((c) => c.id === +el.dataset.p))); };
      $("pq").oninput = () => draw($("pq").value); $("pNew").onclick = () => customerSheet(); draw("");
    });
  }

  // ------------------------------------------------------------ Schedule
  let weekStart = today;
  async function drawSchedule() {
    const v = view("schedule");
    const rows = await api(`/api/crm/schedule?start=${weekStart}&days=7`);
    const days = [...Array(7)].map((_, i) => { const d = new Date(weekStart + "T12:00"); d.setDate(d.getDate() + i); return iso(d); });
    const tot = rows.reduce((a, s) => a + (s.status !== "skipped" ? s.price_cents : 0), 0);
    v.innerHTML = `<h1>Schedule</h1><div class="sub">${rows.length} stop${rows.length === 1 ? "" : "s"} this week · ${usd(tot)}</div>
      <div class="week"><button class="secondary" id="wPrev">‹ Prev</button><button class="secondary" id="wToday">This week</button><button class="secondary" id="wNext">Next ›</button><button id="wAdd" style="margin-left:auto">+ Job</button></div>
      ${days.map((d) => { const st = rows.filter((s) => s.day === d); return `<div class="day"><h3>${dayName(d)}${st.length ? ` · ${usd(st.reduce((a, s) => a + s.price_cents, 0))}` : ""}</h3>
        <div class="list">${st.map(stopCard).join("") || '<div class="m" style="padding:0 4px">Nothing scheduled</div>'}</div></div>`; }).join("")}`;
    const shift = (n) => { const d = new Date(weekStart + "T12:00"); d.setDate(d.getDate() + n); weekStart = iso(d); drawSchedule(); };
    $("wPrev").onclick = () => shift(-7); $("wNext").onclick = () => shift(7); $("wToday").onclick = () => { weekStart = today; drawSchedule(); };
    $("wAdd").onclick = () => jobSheet();
    wireStops(v);
  }
  function jobSheet(j = {}) {
    const open = (cust) => sheet(`<h2>New job</h2><div class="sub">${esc(cust.name || "")} · ${esc(cust.address || "")}</div>
      <label>What<input id="jTitle" placeholder="Mow, edge and blow" value="${esc(j.title || "")}"></label>
      <div class="two"><label>Price $<input id="jPrice" type="number" step="0.01" inputmode="decimal"></label><label>Repeats${repeatSel("none")}</label></div>
      <div class="two"><label>Date<input id="jStart" type="date" value="${today}"></label><label>Time (optional)<input id="jTime" type="time"></label></div>
      <label>Notes<textarea id="jNotes"></textarea></label>
      <button id="jGo" style="width:100%;margin-top:14px">Add to schedule</button><div id="jMsg" class="small"></div>`, () => {
      $("jGo").onclick = async () => {
        try {
          await api("/api/crm/jobs", { method: "POST", json: { customer_id: cust.id, title: $("jTitle").value || "Job", price: +$("jPrice").value || 0,
            repeat: $("jRepeat").value, start: $("jStart").value, time: $("jTime").value || null, notes: $("jNotes").value } });
          close(); toast("Added"); refresh();
        } catch (x) { $("jMsg").innerHTML = `<span class="err">${esc(x.message)}</span>`; }
      };
    });
    if (j.customer_id) api(`/api/crm/customers/${j.customer_id}`).then((d) => open(d.customer));
    else pickCustomer(open);
  }

  // ------------------------------------------------------------ Money
  function invCard(i) {
    return `<div class="item" data-i="${i.id}">
      <div class="top"><span class="t">${esc(i.customer || "Invoice")} · #${i.id}</span><span class="p">${usd(i.total_cents)}</span></div>
      <div class="m">${esc(i.address || "")} · due ${esc(i.due || "")} ${pill(i.status)}${i.status === "unpaid" && i.due < today ? ' <span class="pill declined">overdue</span>' : ""}</div>
      <div class="acts">${i.status === "unpaid" ? `<button data-ia="paid">Mark paid</button><button class="secondary" data-ia="send">Send</button>` : ""}
        ${i.status === "paid" ? `<button class="secondary" data-ia="unpaid">Undo paid</button>` : ""}<a class="btn secondary" target="_blank" href="/i/${esc(i.token)}">View</a></div></div>`;
  }
  function wireInv(root) {
    root.querySelectorAll("[data-i] [data-ia]").forEach((b) => b.addEventListener("click", async () => {
      const id = +b.closest("[data-i]").dataset.i, a = b.dataset.ia;
      if (a === "send") return sendSheet(`/api/crm/invoices/${id}/send`, "Invoice");
      let method = null;
      if (a === "paid") { method = prompt("Paid how? (cash, check, card, Venmo, Zelle)", "card"); if (method === null) return; }
      await api(`/api/crm/invoices/${id}`, { method: "PUT", json: { status: a, method } });
      toast(a === "paid" ? "Marked paid" : "Updated");
      if ($("sheetBg")) close();
      refresh();
    }));
  }
  function invoiceSheet(o = {}) {
    const open = (cust) => sheet(`<h2>New invoice</h2><div class="sub">${esc(cust.name || "")} · ${esc(cust.address || "")}</div>
      <div class="lines">${linesHtml([{ desc: "", price_cents: 0 }])}</div>
      <button class="secondary mini" id="lAdd" style="margin-top:8px">+ Line</button>
      <div class="total"><span>Total</span><span id="lTotal"></span></div>
      <label>Due in<select id="iDue"><option value="0">On receipt</option><option value="7">7 days</option><option value="14" selected>14 days</option><option value="30">30 days</option></select></label>
      <div class="row" style="margin-top:14px"><button id="iSave" class="secondary">Save</button><button id="iSend">Save &amp; send</button></div><div id="iMsg" class="small"></div>`, () => {
      wireLines();
      const save = () => api("/api/crm/invoices", { method: "POST", json: { customer_id: cust.id, items: readLines(), due_days: +$("iDue").value } });
      $("iSave").onclick = async () => { try { await save(); close(); toast("Saved"); refresh(); } catch (x) { $("iMsg").innerHTML = `<span class="err">${esc(x.message)}</span>`; } };
      $("iSend").onclick = async () => { try { const i = await save(); sendSheet(`/api/crm/invoices/${i.id}/send`, "Invoice"); } catch (x) { $("iMsg").innerHTML = `<span class="err">${esc(x.message)}</span>`; } };
    });
    if (o.customer_id) api(`/api/crm/customers/${o.customer_id}`).then((d) => open(d.customer));
    else pickCustomer(open);
  }
  async function drawMoney() {
    const v = view("money");
    const [m, inv] = await Promise.all([api("/api/crm/money"), api("/api/crm/invoices")]);
    const mx = Math.max(1, ...m.months.map((x) => x.cents));
    const tl = { lawn: "Lawn", snow: "Snow", concrete: "Concrete", pressure: "Pressure washing", windows: "Windows", gutters: "Gutters", lights: "Lights",
      roofing: "Roofing", siding: "Siding", solar: "Solar", trees: "Trees", landscaping: "Landscaping", irrigation: "Irrigation", pest: "Pest", hvac: "HVAC", other: "Other" };
    v.innerHTML = `<h1>Money</h1>
      <div class="kpis">
        <div class="kpi"><div class="l">Paid this month</div><div class="v">${usd(m.paid_this_month)}</div></div>
        <div class="kpi"><div class="l">Last month</div><div class="v">${usd(m.paid_last_month)}</div></div>
        <div class="kpi ${m.overdue ? "warn" : ""}"><div class="l">Owed${m.overdue ? ` (${usd(m.overdue)} late)` : ""}</div><div class="v">${usd(m.owed)}</div></div>
        <div class="kpi"><div class="l">Estimate win rate</div><div class="v">${m.win_rate == null ? "–" : m.win_rate + "%"}</div></div>
      </div>
      ${m.months.length ? `<div class="item"><div class="t">Paid by month</div><div class="bars">${m.months.slice().reverse().map((x) => `<div class="bar"><span class="m">${esc(x.month)}</span><i style="width:${Math.max(2, 100 * x.cents / mx)}%"></i><b style="text-align:right">${usd(x.cents)}</b></div>`).join("")}</div></div>` : ""}
      ${Object.keys(m.by_trade).length ? `<div class="item" style="margin-top:8px"><div class="t">This month by trade</div>${Object.entries(m.by_trade).map(([k, c]) => `<div class="top"><span class="m">${esc(tl[k] || k)}</span><b>${usd(c)}</b></div>`).join("")}</div>` : ""}
      <div class="sec"><h2>Invoices</h2><button class="mini" id="iNew">+ Invoice</button></div>
      <div class="row" style="margin-bottom:8px"><button class="secondary mini" data-f="">All</button><button class="secondary mini" data-f="unpaid">Unpaid</button><button class="secondary mini" data-f="paid">Paid</button></div>
      <div class="list" id="iList"></div>`;
    const draw = (f) => { $("iList").innerHTML = inv.filter((i) => !f || i.status === f).map(invCard).join("") || '<div class="empty">No invoices. Mark a stop <b>Done + invoice</b> and it appears here.</div>'; wireInv($("iList")); };
    v.querySelectorAll("[data-f]").forEach((b) => b.onclick = () => draw(b.dataset.f));
    $("iNew").onclick = () => invoiceSheet();
    draw("");
  }

  // ------------------------------------------------------------ settings
  $("gear").onclick = () => sheet(`<h2>Settings</h2>
    <label>How customers pay you (shown on invoices)<textarea id="sPay" placeholder="Venmo @greenacres · Zelle 402-555-0142 · checks to Green Acres Lawn Co.">${esc(me.pay_note || "")}</textarea></label>
    <div class="two"><label>Phone<input id="sPhone" value="${esc(me.phone || "")}"></label><label>Brand color<input id="sColor" type="color" value="${esc(me.brand_color || "#15803d")}"></label></div>
    <button id="sSave" style="width:100%;margin-top:14px">Save</button>
    <p class="small muted">Your prices per trade are on the <a href="/reports">Reports</a> page under <b>Your prices</b>. Membership and CRM billing: <a href="/join">Membership</a>.</p>
    <div id="sMsg" class="small"></div>`, () => {
    $("sSave").onclick = async () => {
      try { me = await api("/api/roof/profile", { method: "PUT", json: { pay_note: $("sPay").value, phone: $("sPhone").value, brand_color: $("sColor").value } }); toast("Saved"); close(); }
      catch (e) { $("sMsg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    };
  });

  go(new URLSearchParams(location.search).get("tab") || "today");
})();
