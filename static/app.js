// Customer-facing estimator.
(async function () {
  const $ = (id) => document.getElementById(id);
  const cfg = await api("/api/config");
  document.documentElement.style.setProperty("--accent", cfg.business.accent || "#16a34a");
  if (cfg.business.name && cfg.business.name !== "Your Company") {
    $("bizName").textContent = cfg.business.name;
    document.title = `${cfg.business.name} | Instant Estimate`;
  }
  $("bizContact").textContent = [cfg.business.phone, cfg.business.email].filter(Boolean).join(" · ");

  const map = makeMap("map", cfg);
  const holder = {};
  const parcels = parcelLayer(map, (f) => pick(f.properties.pin));
  let current = null;
  let finish = null;

  const conc = cfg.services.concrete_driveway;
  if (conc && Object.keys(conc.finishes || {}).length) {
    $("finish").innerHTML = Object.entries(conc.finishes).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    finish = $("finish").value;
  }
  $("leadServices").innerHTML = Object.entries(cfg.services)
    .map(([k, v]) => `<label><input type="checkbox" value="${k}"> ${esc(v.label)}</label>`).join("");

  wireAutocomplete($("addr"), $("suggest"), async (s) => {
    try {
      const q = new URLSearchParams({ q: s.text });
      if (s.magicKey) q.set("magicKey", s.magicKey);
      const loc = await api(`/api/locate?${q}`);
      map.setView([loc.best.lat, loc.best.lon], 19);
      $("hint").classList.remove("hidden");
      await parcels.load();
      if (loc.suggested_pin) parcels.highlightSuggested(loc.suggested_pin);
    } catch (e) {
      alert(e.message || "We couldn't find that address.");
    }
  });

  async function pick(pin) {
    parcels.select(pin);
    $("hint").classList.add("hidden");
    $("panelEmpty").classList.add("hidden");
    $("panelResult").classList.remove("hidden");
    $("resAddr").innerHTML = '<span class="spinner"></span>Measuring your property…';
    $("resPin").textContent = "";
    $("meas").innerHTML = ""; $("prices").innerHTML = ""; $("resNote").textContent = "";
    try {
      current = await api(`/api/measure/${encodeURIComponent(pin)}${finish ? `?finish=${finish}` : ""}`);
    } catch (e) {
      $("resAddr").innerHTML = `<span class="err">Couldn't measure this lot: ${esc(e.message)}</span>`;
      return;
    }
    showOverlay(map, holder, current);
    renderResult();
    $("panelLead").classList.remove("hidden");
    $("panelRender").classList.toggle("hidden", !conc);
    if (window.innerWidth < 900) $("panelResult").scrollIntoView({ behavior: "smooth" });
  }

  function renderResult() {
    const r = current;
    $("resAddr").textContent = r.address || "Your property";
    $("resPin").textContent = `Parcel ${r.pin}`;  // bare parcel number (county shown separately)
    const notes = [];
    if (r.sources.driveway_method === "estimated") notes.push("Driveway is partly hidden in the aerial photo, so its size is estimated.");
    if (!r.sources.sidewalk_detected) notes.push("No public sidewalk detected along your frontage.");
    $("resNote").textContent = notes.join(" ");
    const grass = { ...r, grass_total: (r.lawn_sqft || 0) + (r.parkway_sqft || 0) };
    $("meas").innerHTML = measurementCards(grass, ["grass_total", "driveway_sqft", "sidewalk_sqft", "lot_sqft"],
      { ...cfg.measurements, grass_total: "Grass you'd have mowed (yard + street strip)" }) +
      `<p class="small muted" style="grid-column:1/-1;margin:0">Grass only. Your house, driveway, patios, decks, sheds, play sets and landscape beds are left out.</p>`;
    $("prices").innerHTML = Object.entries(r.estimate).map(([k, e]) => `
      <div class="svc">
        <div><div class="name">${esc(e.label)}</div>
          <div class="small muted">${fmt(e.sqft)} sq ft${e.finishes ? ` · ${esc(e.finishes[e.finish])}` : ""}</div></div>
        <div class="price">${money(e.low)}–${money(e.high)}<small>${esc(e.unit)}${e.annual ? ` · ${money(e.annual.low)}–${money(e.annual.high)}/yr (${esc(e.annual.note)})` : ""}</small></div>
      </div>`).join("") || '<p class="muted">No services apply to this property.</p>';
  }

  $("finish").addEventListener("change", async () => {
    finish = $("finish").value;
    if (current) {
      current = await api(`/api/measure/${encodeURIComponent(current.pin)}?finish=${finish}`);
      renderResult();
    }
  });

  $("btnRender").addEventListener("click", async () => {
    if (!current) return;
    if (!cfg.render_available) {
      $("renderStatus").innerHTML = '<span class="muted">AI renderings aren\'t switched on yet for this site.</span>';
      return;
    }
    const fd = new FormData();
    fd.append("finish", $("finish").value);
    fd.append("pin", current.pin);
    if ($("photo").files[0]) fd.append("photo", $("photo").files[0]);
    $("btnRender").disabled = true;
    $("renderStatus").innerHTML = '<span class="spinner"></span>Rendering. This takes about 30 to 60 seconds.';
    try {
      const r = await api("/api/render", { method: "POST", body: fd });
      const url = URL.createObjectURL(await r.blob());
      $("renderOut").innerHTML = `<img alt="AI rendering of new driveway" src="${url}">`;
      $("renderStatus").innerHTML = '<span class="muted">AI concept image. The actual finish and colour may differ.</span>';
    } catch (e) {
      $("renderStatus").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    } finally {
      $("btnRender").disabled = false;
    }
  });

  $("leadForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!current) return;
    const fd = new FormData(ev.target);
    const services = [...$("leadServices").querySelectorAll("input:checked")].map((i) => i.value);
    try {
      await api("/api/lead", { method: "POST", json: {
        pin: current.pin, name: fd.get("name"), phone: fd.get("phone"), email: fd.get("email"),
        notes: fd.get("notes"), services } });
      $("leadStatus").innerHTML = '<span class="ok">Thanks! We\'ll be in touch shortly.</span>';
      ev.target.querySelector("button").disabled = true;
    } catch (e) {
      $("leadStatus").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    }
  });
})();
