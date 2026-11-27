"""Property estimator API + web front ends.

Run:  uvicorn app.main:app --reload     (from the estimator/ directory)
Customer page:  http://localhost:8000/
Business page:  http://localhost:8000/business
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import threading
import secrets
import os
import time
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import mapping, Point

from . import accounts, billing, bulk, corrections, counties, datastore, render, roofreport
from .roofpdf import render_pdf
from .gis import get_json, query_features
from .imagery import Mosaic
from .measure import default_measurer, measurer_for, MEASUREMENTS
from .pricing import DATA_DIR, estimate, load_settings, save_settings

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
LEADS = DATA_DIR / "leads.jsonl"
ADMIN_TOKEN = os.environ.get("ESTIMATOR_ADMIN_TOKEN", "").strip()  # pasted values often carry a space or newline

app = FastAPI(title="Property Estimator")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


from . import safety  # noqa: E402


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    """Phones (Safari especially) keep old copies of scripts, so a new page can
    end up running an old script. Make browsers re-check pages and scripts on
    every load; unchanged files still come back as a cheap 304.
    Also: security headers on every response, and the owner's audit log."""
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or not path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")  # only the command center may frame our pages
    h.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(self)")
    if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # owner actions: every change through /api/admin, owner sign-ins, and refused attempts
    if path.startswith("/api/admin/") and (request.method != "GET" or path == "/api/admin/check" or resp.status_code in (401, 429)):
        safety.log(_client_ip(request), request.method, path, resp.status_code)
    return resp


def admin(request: Request):
    if not ADMIN_TOKEN:
        return  # dev mode: open. Set ESTIMATOR_ADMIN_TOKEN in production.
    ip = _client_ip(request)
    wait = safety.locked("admin:" + ip)
    if wait:
        raise HTTPException(429, f"Too many wrong admin keys from this connection. Try again in {wait // 60 + 1} minutes.")
    tok = (request.headers.get("x-admin-token") or request.query_params.get("token") or "").strip()
    if not hmac.compare_digest(tok.encode(), ADMIN_TOKEN.encode()):
        if tok:  # a wrong key (not just a page that hasn't asked yet)
            safety.failed("admin:" + ip)
        raise HTTPException(401, "admin token required")
    safety.succeeded("admin:" + ip)


@app.on_event("startup")
def _start_backups():
    safety.start_nightly()


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------- pages
@app.get("/")
def page_home():
    """The customer site's front door: what the problem is, what membership is
    worth, and the ZIP code lookup to buy data, all on one page."""
    return FileResponse(STATIC / "data.html")


@app.get("/estimate")
def page_customer():
    """The homeowner lawn-estimate page (embeddable; not in the menus)."""
    return FileResponse(STATIC / "index.html")


@app.get("/business")
@app.get("/command/data")
@app.get("/command/tools")
@app.get("/command/settings")
def page_business():
    """Owner tools; the path picks the section (ZIP data, measure tools, settings)."""
    return FileResponse(STATIC / "business.html")


@app.get("/command/postcards")
def page_postcards():
    return FileResponse(STATIC / "postcards.html")


@app.get("/signin")
def page_signin():
    """One sign-in for the customer site: companies go to their reports or
    CRM; "admin" + the admin key goes to the command center."""
    return FileResponse(STATIC / "signin.html")


@app.get("/api/admin/check", dependencies=[Depends(admin)])
def admin_check():
    return {"ok": True, "open": not ADMIN_TOKEN}


@app.get("/reports")
def page_reports():
    return FileResponse(STATIC / "reports.html")


# --------------------------------------------------------------- public API
_GEOCODER = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"
_BASEMAP = {
    "tile_url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "attribution": "Esri, Maxar, Earthstar Geographics",
    "max_native_zoom": 19,
}


def _region_bbox():
    from shapely.ops import unary_union
    return unary_union(list(counties.boundaries().values())).bounds


@app.get("/api/config")
def config():
    s = load_settings()
    layers = []
    for cid, g in counties.boundaries().items():
        c = counties.get_county(cid)
        if c["imagery"]["tile_url"] == _BASEMAP["tile_url"]:
            continue
        w, so, e, n = g.bounds
        layers.append({"county": cid, "name": c["name"], "tile_url": c["imagery"]["tile_url"],
                       "year": c["imagery"].get("year"), "source": c["imagery"].get("source"),
                       "max_native_zoom": min(21, c["imagery"].get("max_zoom", 21)),
                       "bounds": [[so, w], [n, e]]})
    W, S, E, N = _region_bbox()
    return {
        "business": s["business"],
        "services": {k: {"label": v["label"], "unit": v["unit"], "finishes": {fk: fv["label"] for fk, fv in v.get("finishes", {}).items()}}
                     for k, v in s["services"].items() if v.get("enabled")},
        "region": {"name": counties.REGION_NAME, "bounds": [[S, W], [N, E]], "center": [(S + N) / 2, (W + E) / 2]},
        "counties": [{"id": cid, "name": counties.get_county(cid)["name"]} for cid in counties.COUNTIES],
        "basemap": _BASEMAP,
        "imagery_layers": layers,
        # kept for older pages
        "county": {"name": counties.REGION_NAME, "center": [(S + N) / 2, (W + E) / 2]},
        "imagery": {"tile_url": _BASEMAP["tile_url"], "year": None, "max_native_zoom": 19},
        "render_available": render.available(),
        "measurements": MEASUREMENTS,
    }


@app.get("/api/suggest")
def suggest(q: str):
    """Address autocomplete across the whole service area."""
    if len(q.strip()) < 3:
        return []
    W, S, E, N = _region_bbox()
    d = get_json(_GEOCODER + "/suggest", {"text": q, "maxSuggestions": 8, "f": "json", "countryCode": "USA",
                                          "searchExtent": f"{W},{S},{E},{N}", "category": "Address,Postal"},
                 ttl=86400)
    return [{"text": s_["text"], "magicKey": s_.get("magicKey")} for s_ in d.get("suggestions", [])]


@app.get("/api/locate")
def locate(q: str, magicKey: str | None = None):
    W, S, E, N = _region_bbox()
    params = {"SingleLine": q, "outSR": 4326, "maxLocations": 5, "f": "json", "countryCode": "USA",
              "searchExtent": f"{W},{S},{E},{N}", "forStorage": "false"}
    if magicKey:
        params["magicKey"] = magicKey
    d = get_json(_GEOCODER + "/findAddressCandidates", params, ttl=86400)
    out = [{"address": c["address"], "lat": c["location"]["y"], "lon": c["location"]["x"], "score": c["score"]}
           for c in d.get("candidates", []) if c.get("score", 0) >= 70]
    if not out:
        raise HTTPException(404, "We couldn't find that address.")
    best = out[0]
    cid = counties.county_at(best["lon"], best["lat"])
    if not cid:
        raise HTTPException(404, "That address is outside our service area (Douglas, Sarpy, Lancaster and Cass counties).")
    hit = _parcel_at(best["lat"], best["lon"], cid, best["address"])
    pid = counties.make_pid(cid, hit["attributes"][measurer_for(cid).pf["pin"]]) if hit else None
    return {"candidates": out, "best": best, "county": cid, "suggested_pin": pid}


def _parcel_at(lat: float, lon: float, cid: str | None = None, address: str | None = None):
    """The parcel at a geocoded point. Geocoders sometimes drop the point on
    the street in front of the lot, so look ~40 m around it and prefer the
    lot whose house number matches the address."""
    import re
    cid = cid or counties.county_at(lon, lat)
    if not cid:
        return None
    m = measurer_for(cid)
    pt = Point(lon, lat)
    num = None
    if address:
        mm = re.match(r"\s*(\d+)", address)
        num = mm.group(1) if mm else None
    for d in (0.00002, 0.0004):
        feats = query_features(m.c["parcels"]["url"], envelope=(lon - d, lat - d, lon + d, lat + d),
                               out_fields=f"{m.pf['pin']},{m.pf['address']}")
        feats = [f for f in feats if f["geometry"] is not None]
        if num:
            def same_number(f):
                a = str(f["attributes"].get(m.pf["address"]) or "")
                return re.match(r"\s*0*" + num + r"\b", a) is not None
            matched = [f for f in feats if same_number(f)]
            if matched:
                matched.sort(key=lambda f: f["geometry"].distance(pt))
                return matched[0]
        inside = [f for f in feats if f["geometry"].contains(pt)]
        if inside:
            inside.sort(key=lambda f: f["geometry"].area)
            return inside[0]
    return None


@app.get("/api/parcels")
def parcels(bbox: str):
    """Parcel outlines in the map view (every county the view touches)."""
    w, s, e, n = [float(v) for v in bbox.split(",")]
    if (e - w) * (n - s) > 0.0004:  # roughly 1.5 x 1.5 km: zoom in first
        raise HTTPException(400, "zoom in to load parcels")
    out = []
    for cid in counties.counties_in((w, s, e, n)):
        m = measurer_for(cid)
        pf = m.pf
        fields = ",".join(x for x in (pf["pin"], pf["address"], pf["class"]) if x)
        try:
            feats = query_features(m.c["parcels"]["url"], envelope=(w, s, e, n), out_fields=fields, limit=1500)
        except Exception:
            continue
        for f in feats:
            if f["geometry"] is None:
                continue
            a = m.parcel_attrs(f["attributes"])
            out.append({"type": "Feature", "geometry": mapping(f["geometry"]),
                        "properties": {"pin": counties.make_pid(cid, a["pin"]), "address": a.get("address") or "",
                                       "class": a.get("class"), "county": cid}})
    return {"type": "FeatureCollection", "features": out}


def _measurer_and_pin(pid: str):
    cid, pin = counties.split_pid(pid)
    return measurer_for(cid), pin


@lru_cache(maxsize=512)
def _measure_cached(pid: str) -> str:
    m, pin = _measurer_and_pin(pid)
    f = m.get_parcel(pin)
    if not f:
        raise HTTPException(404, "parcel not found")
    return json.dumps(m.measure(f, want_overlay=True))


@app.get("/api/measure/{pin}")
def measure(pin: str, finish: str | None = None):
    r = json.loads(_measure_cached(pin))
    r["estimate"] = estimate(r, finish=finish)
    return r


class Lead(BaseModel):
    pin: str
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=200)
    services: list[str] = []
    notes: str = Field(default="", max_length=2000)


@app.post("/api/lead")
def lead(lead: Lead):
    if not (lead.phone or lead.email):
        raise HTTPException(400, "phone or email required")
    try:
        meas = json.loads(_measure_cached(lead.pin))
        meas.pop("overlay", None)
        meas.pop("parcel_geojson", None)
    except Exception:
        meas = None
    rec = lead.model_dump() | {"ts": time.time(), "measurements": meas,
                               "estimate": estimate(meas) if meas else None}
    with open(LEADS, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return {"ok": True}


@app.post("/api/render")
async def render_endpoint(finish: str = Form("broom"), pin: str = Form(""), notes: str = Form(""),
                          photo: UploadFile | None = File(None)):
    if photo is not None and photo.filename:
        data = await photo.read()
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, "photo too large (15 MB max)")
        aerial = False
    elif pin:
        data = _aerial_png(pin)
        aerial = True
    else:
        raise HTTPException(400, "upload a photo or choose a property")
    try:
        out = render.render(data, finish, notes[:300], aerial=aerial)
    except render.RenderUnavailable as e:
        raise HTTPException(503, str(e))
    return Response(out, media_type="image/png")


@app.get("/api/aerial/{pin}.png")
def aerial(pin: str):
    return Response(_aerial_png(pin), media_type="image/png")


@lru_cache(maxsize=64)
def _aerial_png(pid: str) -> bytes:
    m, pin = _measurer_and_pin(pid)
    f = m.get_parcel(pin)
    if not f:
        raise HTTPException(404, "parcel not found")
    P = m.proj.to_local(f["geometry"]).buffer(25)
    env = m.proj.to_wgs(P.envelope).bounds
    mos = Mosaic(m.c["imagery"]["tile_url"], env, m.c["imagery"]["zoom"] + 1)
    x0, y0 = mos.to_px(env[0], env[3])
    x1, y1 = mos.to_px(env[2], env[1])
    im = mos.image.crop((int(x0), int(y0), int(x1), int(y1)))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------- admin API
@app.get("/api/admin/settings", dependencies=[Depends(admin)])
def get_settings():
    return load_settings()


@app.put("/api/admin/settings", dependencies=[Depends(admin)])
async def put_settings(request: Request):
    return save_settings(await request.json())


class BulkReq(BaseModel):
    zip: str = Field(pattern=r"^\d{5}$")
    classes: list[str] = ["R"]
    measures: list[str] = []
    limit: int | None = Field(default=None, ge=1, le=100000)
    include_owner: bool = False


@app.post("/api/admin/bulk/preview", dependencies=[Depends(admin)])
def bulk_preview(req: BulkReq):
    return bulk.preview(req.zip, req.classes)


@app.post("/api/admin/bulk", dependencies=[Depends(admin)])
def bulk_start(req: BulkReq):
    return bulk.start_job(req.zip, req.classes, req.measures, req.limit, req.include_owner)


@app.get("/api/admin/jobs", dependencies=[Depends(admin)])
def jobs():
    return bulk.list_jobs()


@app.get("/api/admin/jobs/{job_id}", dependencies=[Depends(admin)])
def job(job_id: str):
    j = bulk.get_job(job_id)
    if not j:
        raise HTTPException(404)
    return j


@app.get("/api/admin/jobs/{job_id}/rows", dependencies=[Depends(admin)])
def job_rows(job_id: str, limit: int = 200):
    import csv
    p = bulk.csv_path(job_id)
    if not p.exists():
        return []
    with open(p) as fh:
        rows = list(csv.DictReader(fh))
    return rows[:limit]


@app.get("/api/admin/jobs/{job_id}/csv", dependencies=[Depends(admin)])
def job_csv(job_id: str):
    p = bulk.csv_path(job_id)
    if not p.exists():
        raise HTTPException(404)
    j = bulk.get_job(job_id) or {}
    return FileResponse(p, media_type="text/csv", filename=f"measurements_{j.get('zip', job_id)}_{job_id}.csv")


@app.get("/api/admin/corrections/{pin}", dependencies=[Depends(admin)])
def get_corrections(pin: str):
    cid, bare = counties.split_pid(pin)
    pid = counties.make_pid(cid, bare)
    c = corrections.get(pid) or (corrections.get(bare) if cid == counties.DEFAULT_COUNTY else None)
    return c or {"exclude": [], "include": []}


class Corrections(BaseModel):
    exclude: list[dict] = []
    include: list[dict] = []


@app.put("/api/admin/corrections/{pin}", dependencies=[Depends(admin)])
def put_corrections(pin: str, body: Corrections):
    for g in body.exclude + body.include:
        if g.get("type") != "Polygon" or not g.get("coordinates"):
            raise HTTPException(400, "corrections must be GeoJSON polygons")
    cid, bare = counties.split_pid(pin)
    out = corrections.put(counties.make_pid(cid, bare), body.exclude, body.include)
    _measure_cached.cache_clear()
    return out


@app.get("/api/admin/leads", dependencies=[Depends(admin)])
def leads():
    if not LEADS.exists():
        return []
    rows = [json.loads(l) for l in LEADS.read_text().splitlines() if l.strip()]
    return list(reversed(rows))


# ===================================================================== roofing
ROOF_COOKIE = "roof_session"


def _session_company(request: Request) -> dict | None:
    """The logged-in company (membership re-checked with Stripe if it lapsed)."""
    cid = accounts.read_session(request.cookies.get(ROOF_COOKIE))
    c = accounts.get_company(cid) if cid else None
    if c and c.get("disabled"):  # paused by the owner
        return None
    return billing.refresh(c) if c else None


def roof_company(request: Request) -> dict:
    c = _session_company(request)
    if not c:
        raise HTTPException(401, "please log in")
    return c


@app.get("/r/{rid}")
def page_report(rid: str):
    return FileResponse(STATIC / "report.html")


class RoofLogin(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@app.post("/api/roof/login")
def roof_login(body: RoofLogin, request: Request):
    ip, em = _client_ip(request), (body.email or "").strip().lower()
    wait = safety.locked("login:" + ip, "email:" + em)
    if wait:
        raise HTTPException(429, f"Too many wrong passwords. Try again in {wait // 60 + 1} minutes, or reset your password.")
    c = accounts.login(body.email, body.password)
    owner = (body.email or "").strip().lower() in ("", "admin")
    if not c and ADMIN_TOKEN and hmac.compare_digest(body.password.strip().encode(), ADMIN_TOKEN.encode()):
        c = _owner_login(body.email)
    if not c:
        safety.failed("login:" + ip, "email:" + em)
    else:
        safety.succeeded("login:" + ip, "email:" + em)
    if not c and owner:  # the owner signing in: say what's wrong
        if not ADMIN_TOKEN:
            raise HTTPException(401, "Owner sign-in is off: ESTIMATOR_ADMIN_TOKEN isn't set on the server. "
                                     "Add it in Railway (Variables) and wait for the redeploy.")
        raise HTTPException(401, "That isn't the admin key. Use the exact value of ESTIMATOR_ADMIN_TOKEN from Railway.")
    if not c:
        raise HTTPException(401, "wrong email or password")
    r = JSONResponse(c)
    r.set_cookie(ROOF_COOKIE, accounts.make_session(c["id"]), max_age=30 * 86400, httponly=True,
                 samesite="lax", secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.post("/api/roof/logout")
def roof_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(ROOF_COOKIE)
    return r


@app.get("/api/roof/me")
def roof_me(c: dict = Depends(roof_company)):
    return c


@app.get("/api/roof/structures/{pin}")
def roof_structures(pin: str, c: dict = Depends(roof_company)):
    try:
        info = roofreport.list_structures(pin)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"pin": info["pin"], "address": info["address"],
            "structures": [{k: v for k, v in s.items() if k != "geometry_las"} for s in info["structures"]]}



class RoofOrder(BaseModel):
    pin: str = Field(max_length=64)
    structure_ids: list[int] | None = None
    # structure id -> GeoJSON Polygon the roofer adjusted on the photo
    edited: dict[str, dict] | None = None


@app.post("/api/roof/report")
def roof_order(body: RoofOrder, c: dict = Depends(roof_company)):
    if not accounts.has_credit(c["id"]):
        raise HTTPException(402, "You're out of report credits.")
    try:
        for gj in (body.edited or {}).values():
            if gj.get("type") != "Polygon" or len(json.dumps(gj)) > 50000:
                raise HTTPException(400, "edited outlines must be GeoJSON polygons")
        rep = roofreport.build_report(body.pin, body.structure_ids, edited=body.edited)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        rid = accounts.spend_and_save(c["id"], rep)
    except PermissionError:
        raise HTTPException(402, "You're out of report credits.")
    left = accounts.get_company(c["id"])["credits"]
    cust = None
    if c.get("crm"):  # every report's homeowner becomes a CRM customer, named after the owner
        try:
            cust = _crm.customer_from_report(c["id"], {"id": rid, "data": rep})
        except Exception:
            cust = None
    from . import mailer
    mailer.notify_owner(f"Property report {rid} ordered by {c['name']}",
                        f"{c['name']} ({c['email']}) ordered a property report for {rep.get('address')}.\n"
                        f"Credits left: {left}.", reply_to=c["email"])
    if left <= 2:
        mailer.send(c["email"], f"{left} report credit{'s' if left != 1 else ''} left",
                    f"Your company has {left} property report credit{'s' if left != 1 else ''} left. "
                    "Reply to this email to add more.")
    return {"id": rid, "credits": left, "customer_id": cust["id"] if cust else None,
            "customer": cust["name"] if cust else None}


@app.get("/api/roof/reports")
def roof_reports(c: dict = Depends(roof_company)):
    return accounts.list_reports(c["id"])


def _report_access(rid: str, request: Request) -> tuple[dict, dict]:
    rep = accounts.get_report(rid.upper())
    if not rep:
        raise HTTPException(404, "report not found")
    tok = request.query_params.get("t")
    cid = accounts.read_session(request.cookies.get(ROOF_COOKIE))
    admin_ok = ADMIN_TOKEN and (request.headers.get("x-admin-token") == ADMIN_TOKEN or request.query_params.get("token") == ADMIN_TOKEN)
    if not (cid == rep["company_id"] or (tok and tok == rep["share_token"]) or admin_ok):
        raise HTTPException(403, "you don't have access to this report")
    return rep, accounts.get_company(rep["company_id"]) or {}


@app.get("/api/roof/report/{rid}")
def roof_report_json(rid: str, request: Request):
    rep, comp = _report_access(rid, request)
    from . import trades
    chosen = _report_trades(request, comp)
    sums = [{"key": t, "label": trades.label(t), "metrics": trades.metrics(t, rep["data"]),
             "price": trades.report_price(t, comp.get("rates"), rep["data"])} for t in chosen]
    return {"id": rep["id"], "created": rep["created"], "share_token": rep["share_token"], "data": _visible(rep, comp),
            "company": {k: comp.get(k) for k in ("name", "phone", "email", "brand_color")},
            "company_trades": comp.get("trades") or [], "trades": chosen,
            "trade_summaries": sums, "trade_summary": sums[0] if sums else None}


def _report_trades(request: Request, comp: dict) -> list[str]:
    """Trades to show on a report: ?trades=a,b (or ?trade=a) out of the
    company's trades, else all of the company's."""
    mine = _clean_trades(comp.get("trades") or [])
    raw = request.query_params.get("trades") or request.query_params.get("trade")
    if raw:
        pick = [t for t in _clean_trades(raw.split(",")) if t in mine or not mine]
        if pick:
            return pick
    return mine


def _visible(rep: dict, comp: dict) -> dict:
    """A report as its company may see it: only its trades' sections."""
    from . import trades
    return trades.filter_report(rep["data"], trades.sections_for(comp.get("trades")))


@app.get("/r/{rid}/pdf")
def roof_report_pdf(rid: str, request: Request):
    rep, comp = _report_access(rid, request)
    pdf = render_pdf({**rep, "data": _visible(rep, comp)}, comp, trades=_report_trades(request, comp))
    safe = "".join(ch if ch.isalnum() else "_" for ch in rep["data"].get("address", "property"))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Report_{safe}_{rep["id"]}.pdf"'})


@app.get("/r/{rid}/postcard.pdf")
def roof_report_postcard(rid: str, request: Request, trade: str | None = None,
                         headline: str | None = None, offer: str | None = None):
    """Print-ready 9x6 in postcard (with 1/8 in bleed) for door-to-door mail."""
    from . import postcard
    rep, comp = _report_access(rid, request)
    t = (_clean_trades([trade]) or _report_trades(request, comp) or [None])[0]
    pdf = postcard.render_postcard(rep["data"], comp, t, (headline or "")[:90] or None, (offer or "")[:220] or None)
    safe = "".join(ch if ch.isalnum() else "_" for ch in rep["data"].get("address", "home"))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Postcard_{safe}_{rep["id"]}.pdf"'})


@app.get("/r/{rid}/photo.jpg")
def roof_report_photo(rid: str, request: Request, trade: str | None = None, lines: int = 1):
    """High-resolution aerial photo of the property (for your own mail design)."""
    from . import postcard
    rep, comp = _report_access(rid, request)
    t = (_clean_trades([trade]) or _report_trades(request, comp) or [None])[0]
    try:
        jpg = postcard.property_photo(rep["data"], t, aspect=1.25, lines=bool(lines), min_px=2400)
    except Exception as e:
        raise HTTPException(502, f"Couldn't get the aerial photo: {str(e)[:120]}")
    safe = "".join(ch if ch.isalnum() else "_" for ch in rep["data"].get("address", "home"))
    return Response(jpg, media_type="image/jpeg",
                    headers={"Content-Disposition": f'inline; filename="Aerial_{safe}.jpg"'})


# ------------------------------------------------------------- owner (admin)
class NewCompany(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=6, max_length=200)
    credits: int = Field(default=0, ge=0, le=100000)
    phone: str = Field(default="", max_length=40)


@app.get("/api/admin/companies", dependencies=[Depends(admin)])
def admin_companies():
    return accounts.list_companies()


@app.post("/api/admin/companies", dependencies=[Depends(admin)])
def admin_create_company(body: NewCompany):
    try:
        return accounts.create_company(body.name, body.email, body.password, body.credits, body.phone)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(400, "a company with that email already exists")
        raise


class CreditChange(BaseModel):
    delta: int = Field(ge=-100000, le=100000)
    reason: str = Field(default="manual adjustment", max_length=200)


@app.post("/api/admin/companies/{cid}/credits", dependencies=[Depends(admin)])
def admin_credits(cid: int, body: CreditChange):
    if not accounts.get_company(cid):
        raise HTTPException(404)
    return {"credits": accounts.add_credits(cid, body.delta, body.reason)}


class CompanyEdit(BaseModel):
    name: str | None = None
    phone: str | None = None
    brand_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    password: str | None = Field(default=None, min_length=6)


@app.patch("/api/admin/companies/{cid}", dependencies=[Depends(admin)])
def admin_edit_company(cid: int, body: CompanyEdit):
    return accounts.update_company(cid, **body.model_dump())


@app.get("/api/admin/roof-reports", dependencies=[Depends(admin)])
def admin_roof_reports():
    return accounts.list_reports(None, 500)


# ================================================================ data sales
_CLASS_OK = {"R", "C", "I", "A", "E"}


def _classes(raw: str | list | None) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    out = sorted({c.strip().upper() for c in (raw or []) if c.strip().upper() in _CLASS_OK})
    return out or ["R"]


def _zip5(z: str) -> str:
    z = "".join(ch for ch in (z or "") if ch.isdigit())
    if len(z) != 5:
        raise HTTPException(400, "Enter a 5-digit ZIP code.")
    return z


def _client_ip(request: Request) -> str:
    """The visitor's address. Railway's proxy appends the real one last, so a
    visitor can't dodge lockouts by sending their own X-Forwarded-For."""
    xff = [x.strip() for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
    return xff[-1] if xff else (request.client.host if request.client else "")


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.get("/data")
def page_data(request: Request):
    """Buying ZIP data lives on the home page now; keep old links working."""
    q = request.url.query
    return RedirectResponse("/" + (f"?{q}" if q else "") + "#zipbox", status_code=301)


@app.get("/data/order/{oid}")
def page_data_order(oid: str):
    return FileResponse(STATIC / "data-order.html")


# ------------------------------------------------------------- membership
class JoinReq(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)
    phone: str = Field(default="", max_length=40)
    trade: str | None = Field(default=None, max_length=40)
    trades: list[str] | None = Field(default=None, max_length=30)


@app.get("/api/trades")
def trades_catalog():
    from . import trades
    return {"trades": trades.catalog()}


class TradeReq(BaseModel):
    trade: str = Field(max_length=40)


def _clean_trades(raw) -> list[str]:
    from . import trades
    out = []
    for t in raw or []:
        if t in trades.TRADE_KEYS and t not in out:
            out.append(t)
    return out


@app.put("/api/roof/trade")
def set_trade(body: TradeReq, c: dict = Depends(roof_company)):
    return set_trades(TradesReq(trades=[body.trade]), c)


class TradesReq(BaseModel):
    trades: list[str] = Field(max_length=30)


@app.put("/api/roof/trades")
def set_trades(body: TradesReq, c: dict = Depends(roof_company)):
    """Pick the trades your reports are set up for. Members: the monthly
    price follows the number of trades (2 included)."""
    ts = _clean_trades(body.trades)
    if not ts:
        raise HTTPException(400, "Pick at least one trade.")
    try:
        billing.update_member_price(c, ts)
    except Exception as e:
        raise HTTPException(502, f"Couldn't update your membership price: {str(e)[:200]}")
    return accounts.update_company(c["id"], trades=ts)


@app.get("/api/pricing-rules")
def pricing_rules():
    """What each trade's price is based on, with suggested starting rates."""
    from . import trades
    return {"rules": trades.price_catalog()}


class RatesReq(BaseModel):
    rates: dict


@app.put("/api/roof/rates")
def set_rates(body: RatesReq, c: dict = Depends(roof_company)):
    """Your prices per trade. Every report, sheet and postcard prices each home from them."""
    from . import trades
    return accounts.update_company(c["id"], rates=trades.clean_rates(body.rates))


@app.get("/api/billing/trade-price")
def trade_price(trades: str = ""):
    ts = _clean_trades(trades.split(","))
    return {"trades": ts, "paid_trades": billing.paid_trades(ts), "price_cents": billing.member_price_cents(ts),
            "addon_cents": billing.trade_addon_cents(len(billing.paid_trades(ts)))}


@app.get("/join")
@app.get("/join/done")
def page_join():
    return FileResponse(STATIC / "join.html")


@app.get("/api/billing/plans")
def billing_plans(request: Request):
    c = _session_company(request)
    p = billing.plans()
    return {"member_price_cents": p["member_price_cents"], "credit_price_cents": p["credit_price_cents"],
            "included_trades": p["included_trades"], "trade_cents": p["trade_cents"],
            "crm_price_cents": p["crm_price_cents"], "annual_discount": p["annual_discount"],
            "list_discount": p["list_discount"], "packs": list(billing.CREDIT_PACKS),
            "stripe": billing.enabled(), "company": c}


@app.post("/api/join")
def join(body: JoinReq, request: Request):
    if accounts.company_by_email(body.email):
        raise HTTPException(409, "There's already an account with that email. Log in instead.")
    c = accounts.create_company(body.name, body.email, body.password, 0, body.phone)
    from . import trades as _tr
    ts = _clean_trades((body.trades or []) + ([body.trade] if body.trade else []))
    if ts:
        c = accounts.update_company(c["id"], trades=ts)
    from . import mailer
    mailer.notify_owner(f"New company sign-up: {c['name']}",
                        f"{c['name']} ({c['email']}, {body.phone or 'no phone'}) created an account.", reply_to=c["email"])
    r = JSONResponse(c)
    r.set_cookie(ROOF_COOKIE, accounts.make_session(c["id"]), max_age=30 * 86400, httponly=True,
                 samesite="lax", secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.post("/api/billing/member")
def billing_member(request: Request, body: dict | None = None, c: dict = Depends(roof_company)):
    """Start a membership: monthly, or a year up front at the annual discount."""
    if not billing.enabled():
        raise HTTPException(400, "Card payments aren't set up yet. Contact us to join.")
    if c["member"]:
        raise HTTPException(400, "You're already a member.")
    try:
        return {"checkout_url": billing.member_checkout(c, _base_url(request), yearly=bool((body or {}).get("yearly")))}
    except Exception as e:
        raise HTTPException(502, f"Payment provider error: {str(e)[:200]}")


class CreditsReq(BaseModel):
    qty: int


@app.post("/api/billing/credits")
def billing_credits(body: CreditsReq, request: Request, c: dict = Depends(roof_company)):
    if not billing.enabled():
        raise HTTPException(400, "Card payments aren't set up yet. Contact us for credits.")
    try:
        return {"checkout_url": billing.credits_checkout(c, body.qty, _base_url(request))}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Payment provider error: {str(e)[:200]}")


@app.get("/api/billing/confirm")
def billing_confirm(session_id: str, c: dict = Depends(roof_company)):
    try:
        out = billing.confirm_session(session_id)
    except Exception:
        raise HTTPException(502, "Couldn't confirm the payment with Stripe yet. Refresh in a minute.")
    if not out or out["id"] != c["id"]:
        raise HTTPException(404, "payment not found")
    from . import mailer
    if out["member"]:
        mailer.notify_owner(f"New member: {out['name']}", f"{out['name']} ({out['email']}) started a membership.",
                            reply_to=out["email"])
    return out


@app.post("/api/billing/portal")
def billing_portal(request: Request, c: dict = Depends(roof_company)):
    try:
        return {"url": billing.portal_url(c, _base_url(request))}
    except Exception as e:
        raise HTTPException(400, f"Billing page unavailable: {str(e)[:200]}")


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    if not billing.verify_webhook(payload, request.headers.get("stripe-signature")):
        raise HTTPException(400, "bad signature")
    try:
        what = billing.handle_event(json.loads(payload))
    except Exception as e:
        raise HTTPException(500, f"webhook error: {str(e)[:200]}")
    return {"ok": True, "did": what}


# ------------------------------------------------------------- owner demo
_DEMO_EMAIL = "demo@owner.invalid"
_demo_jobs: dict = {}


def _label_of(k):
    from . import trades as _tr
    return _tr.label(k)


def _demo_company() -> dict:
    """An internal company the owner's demo reports are saved under (free)."""
    c = accounts.company_by_email(_DEMO_EMAIL)
    if not c:
        c = accounts.create_company("Demo (owner tests)", _DEMO_EMAIL, secrets.token_urlsafe(24))
    if not c["member"]:
        c = accounts.set_membership(c["id"], time.time() + 3650 * 86400, active=True)
    if not c.get("rates") or not c.get("trades") or any(r.get("label") == _label_of(k) for k, r in (c.get("rates") or {}).items()):
        # every trade, at the suggested prices, so tests show prices
        from . import trades as _tr
        c = accounts.update_company(c["id"], trades=[k for k in _tr.TRADE_KEYS if k in _tr.PRICE_RULES],
                                    rates=_tr.suggested_rates(),
                                    phone=c.get("phone") or "(402) 555-0100", name="Demo Home Services")
    return c


_TEST_EMAIL = "testdrive@owner.invalid"


def _owner_login(email: str) -> dict | None:
    """The owner signing in on the customer site with the admin key:
    a company's email = that company; "admin" or blank = the owner's own
    test company (member, CRM on)."""
    e = (email or "").strip().lower()
    if e and e != "admin":
        return accounts.company_by_email(e)
    c = accounts.company_by_email(_TEST_EMAIL)
    if not c:
        from . import trades as _tr
        c = accounts.create_company("Test drive (owner)", _TEST_EMAIL, secrets.token_urlsafe(24), phone="(402) 555-0199")
        accounts.update_company(c["id"], trades=["lawn"], rates=_tr.suggested_rates())
    accounts.update_company(c["id"], crm=1, disabled=0)
    if not (accounts.get_company(c["id"]) or {}).get("member"):
        accounts.set_membership(c["id"], time.time() + 3650 * 86400, active=True)
    return accounts.get_company(c["id"])


class TestDrive(BaseModel):
    trades: list[str] = Field(default_factory=lambda: ["lawn"], max_length=30)
    member: bool = True
    crm: bool = True


@app.post("/api/admin/test-drive", dependencies=[Depends(admin)])
def admin_test_drive(body: TestDrive, request: Request):
    """Use the customer site as a company would: signs this browser in to a
    private owner test company set up with the trades, plan and CRM you pick."""
    from . import trades as _tr
    c = accounts.company_by_email(_TEST_EMAIL)
    if not c:
        c = accounts.create_company("Test drive (owner)", _TEST_EMAIL, secrets.token_urlsafe(24),
                                    phone="(402) 555-0199")
    ts = _clean_trades(body.trades) or ["lawn"]
    accounts.update_company(c["id"], trades=ts, crm=1 if body.crm else 0,
                            rates=c.get("rates") or _tr.suggested_rates(), disabled=0)
    if body.member:
        accounts.set_membership(c["id"], time.time() + 3650 * 86400, active=True)
    else:  # see the pay-as-you-go side, with a few credits to try reports
        accounts.set_membership(c["id"], time.time(), active=False)
        if (accounts.get_company(c["id"]) or {}).get("credits", 0) < 3:
            accounts.add_credits(c["id"], 5, "owner test drive")
    r = JSONResponse({"ok": True, "go": "/", "company": accounts.get_company(c["id"])})
    r.set_cookie(ROOF_COOKIE, accounts.make_session(c["id"], days=1), max_age=86400, httponly=True, samesite="lax",
                 secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.get("/test-drive", dependencies=[Depends(admin)])
def test_drive_link(request: Request, trades: str = "lawn", member: int = 1, crm: int = 1, go: str = "/"):
    """One link for the owner's phone: /test-drive?token=KEY&trades=lawn,snow"""
    r = admin_test_drive(TestDrive(trades=trades.split(","), member=bool(member), crm=bool(crm)), request)
    dest = go if go in ("/", "/reports", "/crm", "/data", "/join") else "/"
    out = RedirectResponse(dest, status_code=303)
    out.headers["set-cookie"] = r.headers["set-cookie"]
    return out


@app.get("/demo")
def page_demo():
    return FileResponse(STATIC / "demo.html")


class DemoReq(BaseModel):
    pin: str = Field(max_length=64)


@app.post("/api/demo/report", dependencies=[Depends(admin)])
def demo_report(body: DemoReq):
    """A full property report for one address, free, plus every trade's numbers."""
    from . import trades
    try:
        info = roofreport.list_structures(body.pin)
        ids = [s_["id"] for s_ in info["structures"] if s_["id"] == 0 or s_["plan_sqft"] >= 250]
        d = roofreport.build_report(body.pin, ids or None)
    except LookupError as e:
        raise HTTPException(404, str(e))
    c = _demo_company()
    rid = accounts.spend_and_save(c["id"], d)
    rep = accounts.get_report(rid)
    y = d.get("yard") or {}
    checks = []
    if y.get("quality") == "review":
        checks.append(f"Yard flagged for review: {y.get('note') or 'check the shaded map'}")
    if d.get("tree_overhang_sqft", 0) > 100:
        checks.append(f"{d['tree_overhang_sqft']:,} sq ft of roof is under trees: check dashed lines on site")
    if d.get("method") != "lidar":
        checks.append("Part of the roof had no usable LiDAR; its edges are counted from the outline")
    for st in d.get("structures") or []:
        if st.get("edited"):
            checks.append(f"{st['name']}: outline was adjusted by hand")
    return {"id": rid, "token": rep["share_token"], "address": d.get("address"), "city": d.get("city"),
            "zip": d.get("zip"), "county": d.get("county_name"), "summaries": [{**x, "price": trades.report_price(x["key"], c.get("rates"), d)} for x in trades.summaries(d)], "checks": checks,
            "yard_photo": y.get("photo_jpg") or "", "aerial": d.get("aerial_jpg") or "",
            "roof_sqft": d.get("roof_sqft"), "structures": [s_["name"] for s_ in d.get("structures") or []]}


class DemoPremierReq(BaseModel):
    pin: str = Field(max_length=64)
    n: int = Field(default=5, ge=1, le=10)


@app.post("/api/demo/premier", dependencies=[Depends(admin)])
def demo_premier(body: DemoPremierReq):
    """Test a Premier package: this home and its nearest neighbours (max 10)."""
    from shapely.geometry import box as _box
    from .counties import split_pid
    from . import premier
    cid, pin = split_pid(body.pin)
    m = measurer_for(cid)
    f0 = m.get_parcel(pin)
    if not f0:
        raise HTTPException(404, "parcel not found")
    c0 = f0["geometry"].representative_point()
    near = [f for f in query_features(m.c["parcels"]["url"], envelope=_box(c0.x - 0.002, c0.y - 0.0015, c0.x + 0.002, c0.y + 0.0015).bounds)
            if f["geometry"] is not None and (m.parcel_attrs(f["attributes"]).get("address") or "").strip()]
    near.sort(key=lambda f: f["geometry"].representative_point().distance(c0))
    items = [(cid, f) for f in near[:body.n]]
    jid = secrets.token_hex(4)
    _demo_jobs[jid] = {"status": "building", "done": 0, "total": len(items), "started": time.time()}

    def run():
        import csv as _csv, io as _io, zipfile as _zf
        from concurrent.futures import ThreadPoolExecutor
        out = premier.PREMIER_DIR / f"demo_{jid}.zip"
        rows = []
        try:
            with _zf.ZipFile(out, "w") as zf, ThreadPoolExecutor(max_workers=4) as ex:
                used = set()
                for i, (row, pdf, extras) in enumerate(ex.map(lambda it: premier._one(*it), items), 1):
                    premier.add_home(zf, row, pdf, extras, used)
                    rows.append(row)
                    _demo_jobs[jid]["done"] = i
                buf = _io.StringIO()
                w = _csv.writer(buf)
                w.writerow([lab for _, lab in premier.INDEX_COLS])
                for r in rows:
                    w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in premier.INDEX_COLS])
                zf.writestr("index.csv", buf.getvalue())
            _demo_jobs[jid].update(status="ready", path=str(out), size=out.stat().st_size,
                                   rows=[{k: r.get(k) for k in ("address", "status", "roof_sqft", "lawn_sqft")} for r in rows])
        except Exception as e:
            _demo_jobs[jid].update(status="failed", error=str(e)[:300])
    threading.Thread(target=run, daemon=True).start()
    return {"job": jid, "total": len(items)}


@app.get("/api/demo/premier/{jid}", dependencies=[Depends(admin)])
def demo_premier_status(jid: str):
    j = _demo_jobs.get(jid)
    if not j:
        raise HTTPException(404, "no such test")
    return {k: v for k, v in j.items() if k != "path"}


@app.get("/api/demo/premier/{jid}/zip")
def demo_premier_zip(jid: str, token: str | None = None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:  # same rule as the other owner tools
        raise HTTPException(403, "owner only")
    j = _demo_jobs.get(jid)
    if not j or j.get("status") != "ready":
        raise HTTPException(409, "not ready")
    return FileResponse(j["path"], media_type="application/zip", filename=f"premier_test_{jid}.zip")


@app.get("/api/data/pricing")
def data_pricing():
    """Public per-property rates (for the savings calculator)."""
    return datastore.pricing()


@app.get("/api/data/types")
def data_types():
    """What a buyer can include in a sheet, and the rates."""
    return {"types": [{"key": k, "label": l, "columns": cols} for k, l, cols in datastore.MEASURE_TYPES],
            "pricing": datastore.pricing()}


@app.get("/api/data/quote")
def data_quote(request: Request, zip: str, classes: str = "R", types: str = "lawn", product: str = "standard",
               vlo: int | None = None, vhi: int | None = None):
    z = _zip5(zip)
    c = _session_company(request)
    try:
        q = datastore.quote(z, _classes(classes), types.split(","), product, member=bool(c and c["member"]),
                            lo=vlo, hi=vhi, email=(c or {}).get("email"))
    except LookupError as e:
        raise HTTPException(404, str(e))
    try:
        zp = bulk.zip_polygon(z)
        q["zip_geojson"] = mapping(zp.simplify(0.0002))
    except Exception:
        q["zip_geojson"] = None
    q["columns"] = [label for _, label in datastore.columns_for(q["types"])]
    q["type_catalog"] = [{"key": k, "label": l} for k, l, _ in datastore.MEASURE_TYPES]
    q["stripe"] = datastore.stripe_enabled()
    q["member"] = bool(c and c["member"])
    q["logged_in"] = bool(c)
    return q


class SampleReq(BaseModel):
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    zip: str
    classes: list[str] = ["R"]


@app.post("/api/data/sample")
def data_sample(body: SampleReq, request: Request):
    z = _zip5(body.zip)
    why = datastore.sample_allowed(body.email, _client_ip(request))
    if why:
        raise HTTPException(429, why)
    try:
        rows = datastore.add_owners(datastore.take_sample(body.email, _client_ip(request), z, _classes(body.classes)),
                                    z, _classes(body.classes))
    except LookupError as e:
        raise HTTPException(404, str(e))
    c = _session_company(request)
    rows, taken = datastore.strip_locked(rows, z, (c or {}).get("email"))
    taken_cols = {c for k, _, cs in datastore.MEASURE_TYPES if k in taken for c in cs}
    cols = [(k, l) for k, l in datastore.COLUMNS if k not in taken_cols]
    return {"zip": z, "columns": [{"key": k, "label": l} for k, l in cols], "rows": rows,
            "csv": datastore.to_csv(rows, cols), "keys": [k for k, _ in cols], "taken_types": taken}


class OrderReq(BaseModel):
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    zip: str
    classes: list[str] = ["R"]
    types: list[str] = ["lawn"]
    product: str = "standard"
    value_lo: int | None = None   # home-value band range (every $250k), inclusive
    value_hi: int | None = None


@app.post("/api/data/order")
def data_order(body: OrderReq, request: Request):
    z = _zip5(body.zip)
    try:
        c = _session_company(request)
        if body.product == "exclusive" and not c:
            raise PermissionError("Sign in (or create a free account) to buy an exclusive ZIP, so the lock is tied to your company.")
        o = datastore.create_order(body.email, z, _classes(body.classes), body.types, site=_base_url(request),
                                   product=body.product, member=bool(c and c["member"]),
                                   lo=body.value_lo, hi=body.value_hi, account_email=(c or {}).get("email"))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(409, str(e))
    out = {"id": o["id"], "token": o["token"], "amount_cents": o["amount_cents"], "count": o["count"],
           "order_url": f"/data/order/{o['id']}?t={o['token']}"}
    if datastore.stripe_enabled():
        try:
            out["checkout_url"] = datastore.stripe_checkout(o, _base_url(request))
        except Exception as e:
            raise HTTPException(502, f"Payment provider error: {str(e)[:200]}")
    return out


class GroupOrderReq(OrderReq):
    zip: str | None = None
    zips: list[str] = Field(min_length=1, max_length=25)


@app.post("/api/data/orders")
def data_orders(body: GroupOrderReq, request: Request):
    """Buy several ZIPs in one go: an order per ZIP, one checkout."""
    zs = [_zip5(z) for z in body.zips]
    c = _session_company(request)
    try:
        if body.product == "exclusive" and not c:
            raise PermissionError("Sign in (or create a free account) to buy an exclusive ZIP, so the lock is tied to your company.")
        g = datastore.create_group(body.email, zs, _classes(body.classes), body.types, site=_base_url(request),
                                   product=body.product, member=bool(c and c["member"]),
                                   lo=body.value_lo, hi=body.value_hi, account_email=(c or {}).get("email"))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(409, str(e))
    out = {"grp": g["grp"], "token": g["token"], "count": sum(o["count"] for o in g["orders"]),
           "amount_cents": sum(o["amount_cents"] for o in g["orders"]),
           "order_url": f"/data/orders/{g['grp']}?t={g['token']}"}
    if datastore.stripe_enabled():
        try:
            out["checkout_url"] = datastore.stripe_checkout_group(g["grp"], _base_url(request))
        except Exception as e:
            raise HTTPException(502, f"Payment provider error: {str(e)[:200]}")
    return out


def _group_access(grp: str, t: str | None) -> list[dict]:
    orders = datastore.group_orders(grp)
    if not orders or not t or not hmac.compare_digest(t, orders[0].get("grp_token") or ""):
        raise HTTPException(404, "order not found")
    return orders


@app.get("/data/orders/{grp}")
def page_group(grp: str):
    return FileResponse(STATIC / "data-orders.html")


@app.get("/api/data/orders/{grp}")
def data_group_status(grp: str, t: str | None = None, session_id: str | None = None):
    orders = _group_access(grp, t)
    if session_id and any(o["status"] == "awaiting_payment" for o in orders):
        datastore.stripe_confirm_group(grp, session_id)
        orders = datastore.group_orders(grp)
    views = []
    for o in orders:
        v = datastore.order_view(o)
        v["link"] = f"/data/order/{o['id']}?t={o['token']}"
        views.append(v)
    return {"grp": grp.upper(), "email": orders[0]["email"], "orders": views,
            "amount_cents": sum(o["amount_cents"] for o in orders), "count": sum(o["count"] or 0 for o in orders),
            "unpaid": any(o["status"] == "awaiting_payment" for o in orders), "stripe": datastore.stripe_enabled(),
            "contact": {k: load_settings()["business"].get(k) for k in ("name", "phone", "email")}}


@app.post("/api/data/orders/{grp}/checkout")
def data_group_checkout(grp: str, request: Request, t: str | None = None):
    _group_access(grp, t)
    if not datastore.stripe_enabled():
        raise HTTPException(400, "nothing to pay")
    try:
        return {"checkout_url": datastore.stripe_checkout_group(grp, _base_url(request))}
    except LookupError:
        raise HTTPException(400, "nothing to pay")


def _order_access(oid: str, t: str | None):
    o = datastore.get_order(oid)
    if not o or not t or t != o["token"]:
        raise HTTPException(404, "order not found")
    return o


@app.get("/api/data/order/{oid}")
def data_order_status(oid: str, t: str | None = None, session_id: str | None = None):
    o = _order_access(oid, t)
    if session_id and o["status"] == "awaiting_payment":
        datastore.stripe_confirm(o, session_id)
        o = datastore.get_order(oid)
    v = datastore.order_view(o)
    v["stripe"] = datastore.stripe_enabled()
    v["contact"] = {k: load_settings()["business"].get(k) for k in ("name", "phone", "email")}
    return v


@app.post("/api/data/order/{oid}/checkout")
def data_order_checkout(oid: str, request: Request, t: str | None = None):
    o = _order_access(oid, t)
    if o["status"] != "awaiting_payment" or not datastore.stripe_enabled():
        raise HTTPException(400, "nothing to pay")
    return {"checkout_url": datastore.stripe_checkout(o, _base_url(request))}


@app.get("/api/data/order/{oid}/csv")
def data_order_csv(oid: str, t: str | None = None):
    o = _order_access(oid, t)
    classes = o["classes"].split(",") if o["classes"] else ["R"]
    if (o.get("product") or "standard") == "premier":
        from . import premier
        bands = datastore.order_bands(o)
        if not premier.ready(o["zip"], classes, bands) or o["status"] not in ("ready", "processing"):
            raise HTTPException(409, "Your Premier list isn't ready yet.")
        datastore.log_download(o, "premier")
        return FileResponse(premier.path(o["zip"], classes, bands), media_type="application/zip",
                            filename=f"premier_{o['zip']}_{o['id']}.zip")
    if o["status"] != "ready" and not (o["status"] == "processing" and datastore.dataset_ready(o["zip"], classes)):
        raise HTTPException(409, "Your sheet isn't ready yet.")
    rows = datastore.rows_in_bands(datastore.read_dataset(o["zip"], classes), *datastore.order_bands(o))
    rows = datastore.add_owners(rows, o["zip"], classes)
    cols = datastore.columns_for(datastore.order_types(o))  # only what they paid for
    buyer = accounts.company_by_email(o["email"])
    if buyer:
        rows, cols = datastore.add_prices(rows, cols, (accounts.get_company(buyer["id"]) or {}).get("rates"))
    datastore.log_download(o, "sheet")
    return Response(datastore.to_csv(rows, cols), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="properties_{o["zip"]}_{o["id"]}.csv"'})


# ---- postcard mail files (Premier lists)
_mail_jobs: dict = {}


class MailReq(BaseModel):
    trade: str | None = Field(default=None, max_length=40)
    headline: str | None = Field(default=None, max_length=90)
    offer: str | None = Field(default=None, max_length=220)
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


def _start_mailing(key: str, src, body: MailReq, base: dict) -> dict:
    """Build a branded postcard mail file from a Premier folder in the background."""
    from . import postcard
    comp = {"name": body.name or base.get("name") or "", "phone": body.phone or base.get("phone") or "",
            "email": body.email or base.get("email") or "", "brand_color": body.color or base.get("brand_color") or "#1d4ed8",
            "rates": base.get("rates") or {}}
    trade = (_clean_trades([body.trade]) or _clean_trades(base.get("trades") or []) or [None])[0]
    sig = hashlib.sha1(json.dumps([comp, trade, body.headline, body.offer], sort_keys=True).encode()).hexdigest()[:10]
    out = postcard._mail_dir() / f"{key}_{sig}.zip"
    j = _mail_jobs.get(key)
    if j and j.get("sig") == sig and j["status"] in ("building", "ready"):
        return j
    j = {"status": "ready" if out.exists() else "building", "sig": sig, "path": str(out), "done": 0, "total": 0}
    _mail_jobs[key] = j
    if j["status"] == "ready":
        return j

    def run():
        try:
            r = postcard.build_mailing(src, out, comp, trade, body.headline, body.offer,
                                       progress=lambda i, n: j.update(done=i, total=n))
            if not r["homes"]:
                j.update(status="failed", error="This list was built before postcards were added. Ask us to rebuild it.")
                return
            j.update(status="ready", **r)
        except Exception as e:
            j.update(status="failed", error=str(e)[:200])
    threading.Thread(target=run, daemon=True, name=f"mail-{key}").start()
    return j


def _mail_view(j: dict | None) -> dict:
    return {k: v for k, v in (j or {"status": "none"}).items() if k not in ("path", "sig")}


def _premier_order(oid: str, t: str | None) -> tuple[dict, object]:
    from . import premier
    o = _order_access(oid, t)
    if (o.get("product") or "") != "premier":
        raise HTTPException(400, "Postcard mail files come with Premier lists.")
    classes = o["classes"].split(",") if o["classes"] else ["R"]
    bands = datastore.order_bands(o)
    if not premier.ready(o["zip"], classes, bands):
        raise HTTPException(409, "Your Premier list isn't ready yet.")
    return o, premier.path(o["zip"], classes, bands)


@app.post("/api/data/order/{oid}/mailing")
def data_order_mailing(oid: str, body: MailReq, t: str | None = None):
    o, src = _premier_order(oid, t)
    base = accounts.company_by_email(o["email"]) or {}
    if base:
        base = accounts.get_company(base["id"]) or {}
    return _mail_view(_start_mailing(f"order_{o['id']}", src, body, base))


@app.get("/api/data/order/{oid}/mailing")
def data_order_mailing_status(oid: str, t: str | None = None):
    o = _order_access(oid, t)
    return _mail_view(_mail_jobs.get(f"order_{o['id']}"))


@app.get("/api/data/order/{oid}/mailing.zip")
def data_order_mailing_zip(oid: str, t: str | None = None):
    o = _order_access(oid, t)
    j = _mail_jobs.get(f"order_{o['id']}")
    if not j or j["status"] != "ready":
        raise HTTPException(409, "Your mail file isn't ready yet.")
    return FileResponse(j["path"], media_type="application/zip", filename=f"postcards_{o['zip']}_{o['id']}.zip")


@app.post("/api/demo/premier/{jid}/mailing", dependencies=[Depends(admin)])
def demo_premier_mailing(jid: str, body: MailReq):
    j = _demo_jobs.get(jid)
    if not j or j.get("status") != "ready":
        raise HTTPException(409, "Build a Premier test first.")
    return _mail_view(_start_mailing(f"demo_{jid}", j["path"], body, _demo_company()))


@app.get("/api/demo/premier/{jid}/mailing", dependencies=[Depends(admin)])
def demo_premier_mailing_status(jid: str):
    return _mail_view(_mail_jobs.get(f"demo_{jid}"))


@app.get("/api/demo/premier/{jid}/mailing.zip")
def demo_premier_mailing_zip(jid: str, token: str | None = None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403)
    j = _mail_jobs.get(f"demo_{jid}")
    if not j or j["status"] != "ready":
        raise HTTPException(409, "not ready")
    return FileResponse(j["path"], media_type="application/zip", filename=f"demo_postcards_{jid}.zip")


# ---- owner
@app.get("/api/admin/data", dependencies=[Depends(admin)])
def admin_data():
    zips = []
    for p in sorted(datastore.ZIPS_DIR.glob("*.json")):
        meta = json.loads(p.read_text())
        zips.append({"key": p.stem, **meta})
    return {"pricing": datastore.pricing(), "orders": datastore.list_orders(), "zips": zips,
            "stripe": datastore.stripe_enabled(), "locks": datastore.all_locks(),
            "prebuild_all": datastore.prebuild_status()}


@app.get("/api/admin/data/totals", dependencies=[Depends(admin)])
def admin_data_totals(key: str | None = None):
    """Square feet measured: every stored ZIP, or one ZIP by value band."""
    if key:
        if not key.replace("_", "").isalnum():
            raise HTTPException(400)
        t = datastore.totals_for_key(key)
        if not t:
            raise HTTPException(404, "That ZIP isn't measured yet.")
        return t
    return datastore.totals_all()


class DataPricing(BaseModel):
    rate_cents: float = Field(ge=0.5, le=100)          # first measurement type, per property
    extra_type_cents: float = Field(ge=0, le=100)      # each extra type, per property
    premier_cents: float = Field(default=25.0, ge=1, le=500)  # Premier list, per property
    value_cents: float = Field(default=5.0, ge=0, le=500)     # add-on: assessed value column, per property
    min_order_usd: float = Field(ge=0, le=100_000)
    value_multipliers: list[float] = Field(default=[1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
                                           min_length=9, max_length=9)
    exclusive_cents: float = Field(default=100.0, ge=1, le=10_000)       # exclusive ZIP, first type
    exclusive_extra_cents: float = Field(default=50.0, ge=0, le=10_000)  # each extra type
    exclusive_months: int = Field(default=12, ge=1, le=60)


@app.put("/api/admin/data/pricing", dependencies=[Depends(admin)])
def admin_data_pricing(body: DataPricing):
    s = load_settings()
    s["data_sales"] = body.model_dump()
    save_settings(s)
    return datastore.pricing()


@app.post("/api/admin/data/orders/{oid}/paid", dependencies=[Depends(admin)])
def admin_data_paid(oid: str):
    try:
        return datastore.mark_paid(oid, note="marked paid by owner")
    except LookupError:
        raise HTTPException(404)


class Prebuild(BaseModel):
    zip: str
    classes: list[str] = ["R"]
    product: str = "standard"


@app.post("/api/admin/data/prebuild", dependencies=[Depends(admin)])
def admin_data_prebuild(body: Prebuild):
    """Measure a ZIP ahead of demand so buyers get it instantly."""
    if body.product == "premier":
        from . import premier
        return {"status": premier.ensure(_zip5(body.zip), _classes(body.classes))}
    return {"status": datastore.ensure_dataset(_zip5(body.zip), _classes(body.classes))}


class PrebuildAll(BaseModel):
    classes: list[str] = ["R"]
    start: bool = True


@app.post("/api/admin/data/prebuild-all", dependencies=[Depends(admin)])
def admin_data_prebuild_all(body: PrebuildAll):
    """Measure every ZIP in the service area, one after another (start/stop)."""
    return datastore.prebuild_all(_classes(body.classes), start=body.start)


@app.get("/api/admin/data/prebuild-all", dependencies=[Depends(admin)])
def admin_data_prebuild_all_status():
    return datastore.prebuild_status()


@app.delete("/api/admin/data/locks/{zip_code}/{type_key}", dependencies=[Depends(admin)])
def admin_data_release_lock(zip_code: str, type_key: str):
    """End an exclusive early (e.g. after a refund)."""
    datastore.release_lock(_zip5(zip_code), type_key)
    return {"locks": datastore.all_locks()}


@app.on_event("startup")
def _resume_prebuild():
    from . import coverage
    from . import gis as _gis
    coverage.load_saved_configs()  # counties approved in the Coverage tab (local files, quick)

    def later():  # network work after the site is up, so a deploy never misses its health check
        try:
            datastore.resume_prebuild()
        except Exception:
            pass
        if os.environ.get("COVERAGE_AUTO", "1") != "0":
            coverage.start_continuous()  # daily: new metros, county searches, auto-pull
        _gis.start_cache_pruner()
    threading.Thread(target=later, daemon=True, name="startup").start()


# ================================================================ CRM portal
from . import crm as _crm  # noqa: E402


def crm_company(request: Request) -> dict:
    """A signed-in company with the CRM add-on."""
    c = roof_company(request)
    accounts.touch(c["id"])
    if not c.get("crm"):
        raise HTTPException(402, "The CRM portal is a member add-on. Turn it on from your membership page.")
    return c


@app.get("/crm")
def page_crm():
    return FileResponse(STATIC / "crm.html")


class CrmToggle(BaseModel):
    on: bool


@app.put("/api/roof/crm")
def set_crm(body: CrmToggle, c: dict = Depends(roof_company)):
    """Turn the CRM add-on on or off (members; the monthly price follows)."""
    if body.on and not c["member"]:
        raise HTTPException(402, "The CRM portal is an add-on for members. Become a member first.")
    try:
        billing.update_member_price(c, c.get("trades"), crm=body.on)
    except Exception as e:
        raise HTTPException(502, f"Couldn't update your membership price: {str(e)[:200]}")
    return accounts.update_company(c["id"], crm=1 if body.on else 0)


class Profile(BaseModel):
    phone: str | None = Field(default=None, max_length=40)
    brand_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    pay_note: str | None = Field(default=None, max_length=300)


@app.put("/api/roof/profile")
def set_profile(body: Profile, c: dict = Depends(roof_company)):
    return accounts.update_company(c["id"], **body.model_dump())


@app.get("/api/crm/home")
def crm_home(c: dict = Depends(crm_company)):
    import datetime as _dt
    today = _dt.date.today().isoformat()
    return {"company": c, "today": _crm.schedule(c["id"], today, 1), "week": _crm.schedule(c["id"], today, 7),
            "money": _crm.money(c["id"]), "stats": _crm.stats(c["id"]),
            "open_estimates": _crm.list_estimates(c["id"], "sent")[:10],
            "recent_reports": accounts.list_reports(c["id"], 10)}


# ---- customers
class CustomerIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    zip: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=2000)


@app.get("/api/crm/customers")
def crm_customers(q: str | None = None, c: dict = Depends(crm_company)):
    return _crm.list_customers(c["id"], (q or "").strip()[:80] or None)


@app.post("/api/crm/customers")
def crm_add_customer(body: CustomerIn, c: dict = Depends(crm_company)):
    return _crm.add_customer(c["id"], **body.model_dump())


@app.put("/api/crm/customers/{cid}")
def crm_edit_customer(cid: int, body: CustomerIn, c: dict = Depends(crm_company)):
    if not _crm.get_customer(c["id"], cid):
        raise HTTPException(404)
    return _crm.update_customer(c["id"], cid, **{k: v for k, v in body.model_dump().items() if v is not None})


@app.get("/api/crm/customers/{cid}")
def crm_customer(cid: int, c: dict = Depends(crm_company)):
    cu = _crm.get_customer(c["id"], cid)
    if not cu:
        raise HTTPException(404)
    co = c["id"]
    return {"customer": cu,
            "estimates": [e for e in _crm.list_estimates(co) if e["customer_id"] == cid],
            "invoices": [i for i in _crm.list_invoices(co) if i["customer_id"] == cid],
            "jobs": _crm._all("SELECT * FROM jobs WHERE company_id=? AND customer_id=? ORDER BY start DESC", (co, cid)),
            "photos": _crm.list_photos(co, customer_id=cid),
            "reports": [r for r in accounts.list_reports(co, 500) if cu.get("pin") and r["pin"] == cu["pin"]]}


class FromReport(BaseModel):
    report_id: str = Field(max_length=20)
    trades: list[str] | None = None


def _own_report(c: dict, rid: str) -> dict:
    rep = accounts.get_report(rid.upper())
    if not rep or rep["company_id"] != c["id"]:
        raise HTTPException(404, "report not found")
    return rep


@app.post("/api/crm/estimates/from-report")
def crm_estimate_from_report(body: FromReport, c: dict = Depends(crm_company)):
    """One tap: the report's home becomes a customer and an estimate priced
    from your rates for each of your trades."""
    rep = _own_report(c, body.report_id)
    cu = _crm.customer_from_report(c["id"], rep)
    items = _crm.estimate_items_from_report(c, rep["data"], _clean_trades(body.trades) or None)
    return _crm.create_estimate(c["id"], cu["id"], items, report_id=rep["id"])


# ---- estimates
class EstimateIn(BaseModel):
    customer_id: int | None = None
    items: list[dict] | None = None
    note: str | None = Field(default=None, max_length=1000)
    status: str | None = None


@app.get("/api/crm/estimates")
def crm_estimates(status: str | None = None, c: dict = Depends(crm_company)):
    return _crm.list_estimates(c["id"], status)


@app.post("/api/crm/estimates")
def crm_add_estimate(body: EstimateIn, c: dict = Depends(crm_company)):
    if not body.customer_id or not _crm.get_customer(c["id"], body.customer_id):
        raise HTTPException(400, "Pick a customer.")
    return _crm.create_estimate(c["id"], body.customer_id, body.items or [], body.note or "")


@app.put("/api/crm/estimates/{eid}")
def crm_edit_estimate(eid: int, body: EstimateIn, c: dict = Depends(crm_company)):
    if not _crm.get_estimate(c["id"], eid):
        raise HTTPException(404)
    return _crm.update_estimate(c["id"], eid, body.items, body.note, body.status)


def _send(c: dict, to: str | None, subject: str, text: str) -> bool:
    if not to:
        return False
    from . import mailer
    mailer.send(to, subject, text, reply_to=c["email"])
    return mailer.enabled()


@app.post("/api/crm/estimates/{eid}/send")
def crm_send_estimate(eid: int, request: Request, c: dict = Depends(crm_company)):
    """Mark sent and return the homeowner's link (emailed when we have their email)."""
    e = _crm.get_estimate(c["id"], eid)
    if not e:
        raise HTTPException(404)
    cu = _crm.get_customer(c["id"], e["customer_id"])
    link = f"{_base_url(request)}/e/{e['token']}"
    _crm.update_estimate(c["id"], eid, status="sent")
    emailed = _send(c, cu.get("email"), f"Your estimate from {c['name']}",
                    f"Hi{(' ' + cu['name'].split()[0]) if cu.get('name') else ''},\n\n{c['name']} sent you an estimate "
                    f"for {cu.get('address') or 'your property'}: ${e['total_cents'] / 100:,.2f}.\n\n"
                    f"See it and accept it here:\n{link}\n\n{c['name']} {c.get('phone') or ''}")
    return {"link": link, "emailed": emailed,
            "sms": f"Hi, it's {c['name']}. Your estimate for {cu.get('address') or 'your property'} is ready: {link}"}


class ScheduleIn(BaseModel):
    start: str
    repeat: str = "none"
    time: str | None = None
    until: str | None = None


@app.post("/api/crm/estimates/{eid}/schedule")
def crm_schedule_estimate(eid: int, body: ScheduleIn, c: dict = Depends(crm_company)):
    """Turn an accepted estimate into jobs on the schedule: one job per line."""
    e = _crm.get_estimate(c["id"], eid)
    if not e:
        raise HTTPException(404)
    cu = _crm.get_customer(c["id"], e["customer_id"]) or {}
    job_name = cu.get("name") or cu.get("address") or "Job"  # jobs carry the owner's name
    out = []
    try:
        for it in e["items"] or [{"desc": "Job", "price_cents": e["total_cents"], "qty": 1}]:
            out.append(_crm.create_job(c["id"], e["customer_id"], job_name, body.start, body.repeat,
                                       int(it["price_cents"] * it.get("qty", 1)), body.time, body.until,
                                       it.get("trade"), e["id"]))
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-05-01.")
    if e["status"] in ("draft", "sent"):
        _crm.update_estimate(c["id"], eid, status="accepted")
    return out


# ---- public estimate and invoice pages (homeowner)
@app.get("/e/{tok}")
def page_estimate(tok: str):
    return FileResponse(STATIC / "estimate.html")


@app.get("/i/{tok}")
def page_invoice(tok: str):
    return FileResponse(STATIC / "estimate.html")


def _public_doc(kind: str, d: dict) -> dict:
    co = accounts.get_company(d["company_id"]) or {}
    cu = _crm.get_customer(d["company_id"], d["customer_id"]) or {}
    out = {"kind": kind, "id": d["id"], "items": d["items"], "total_cents": d["total_cents"], "note": d.get("note"),
           "status": d["status"], "created": d["created"], "due": d.get("due"),
           "company": {k: co.get(k) for k in ("name", "phone", "email", "brand_color", "pay_note")},
           "customer": {k: cu.get(k) for k in ("name", "address", "city", "zip")}}
    if kind == "estimate" and d.get("report_id"):
        rep = accounts.get_report(d["report_id"])
        if rep:
            out["aerial_jpg"] = (rep["data"].get("yard") or {}).get("photo_jpg") or rep["data"].get("aerial_jpg")
    return out


@app.get("/api/public/estimate/{tok}")
def public_estimate(tok: str):
    e = _crm.estimate_by_token(tok)
    if not e:
        raise HTTPException(404, "This estimate link isn't valid.")
    return _public_doc("estimate", e)


class Answer(BaseModel):
    accept: bool


@app.post("/api/public/estimate/{tok}")
def public_answer(tok: str, body: Answer):
    e = _crm.answer_estimate(tok, body.accept)
    if not e:
        raise HTTPException(404)
    co = accounts.get_company(e["company_id"]) or {}
    cu = _crm.get_customer(e["company_id"], e["customer_id"]) or {}
    from . import mailer
    word = "ACCEPTED" if body.accept else "declined"
    mailer.send(co.get("email"), f"Estimate {word}: {cu.get('address') or cu.get('name') or ''}",
                f"{cu.get('name') or 'Your customer'} {word} your estimate (${e['total_cents'] / 100:,.2f}) for "
                f"{cu.get('address') or 'their property'}.\n\nOpen your CRM to put it on the schedule.")
    return _public_doc("estimate", e)


@app.get("/api/public/invoice/{tok}")
def public_invoice(tok: str):
    i = _crm.invoice_by_token(tok)
    if not i:
        raise HTTPException(404, "This invoice link isn't valid.")
    return _public_doc("invoice", i)


# ---- jobs and schedule
class JobIn(BaseModel):
    customer_id: int
    title: str = Field(max_length=120)
    start: str
    repeat: str = "none"
    time: str | None = Field(default=None, max_length=10)
    until: str | None = None
    price: float = Field(default=0, ge=0, le=1_000_000)
    trade: str | None = None
    notes: str | None = Field(default="", max_length=1000)


@app.post("/api/crm/jobs")
def crm_add_job(body: JobIn, c: dict = Depends(crm_company)):
    if not _crm.get_customer(c["id"], body.customer_id):
        raise HTTPException(400, "Pick a customer.")
    try:
        return _crm.create_job(c["id"], body.customer_id, body.title, body.start, body.repeat,
                               int(round(body.price * 100)), body.time, body.until, body.trade, None, body.notes or "")
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-05-01.")


class JobEdit(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    start: str | None = None
    repeat: str | None = None
    time: str | None = None
    until: str | None = None
    price: float | None = Field(default=None, ge=0, le=1_000_000)
    notes: str | None = Field(default=None, max_length=1000)
    active: bool | None = None


@app.put("/api/crm/jobs/{jid}")
def crm_edit_job(jid: int, body: JobEdit, c: dict = Depends(crm_company)):
    if not _crm.get_job(c["id"], jid):
        raise HTTPException(404)
    f = {k: v for k, v in body.model_dump().items() if v is not None}
    if "price" in f:
        f["price_cents"] = int(round(f.pop("price") * 100))
    if "active" in f:
        f["active"] = 1 if f["active"] else 0
    return _crm.update_job(c["id"], jid, **f)


@app.get("/api/crm/schedule")
def crm_schedule(start: str | None = None, days: int = 7, c: dict = Depends(crm_company)):
    import datetime as _dt
    try:
        return _crm.schedule(c["id"], start or _dt.date.today().isoformat(), max(1, min(days, 62)))
    except ValueError:
        raise HTTPException(400, "bad date")


class VisitIn(BaseModel):
    job_id: int
    day: str
    status: str = Field(pattern=r"^(done|skipped|scheduled)$")
    invoice: bool = False
    note: str | None = Field(default="", max_length=500)


@app.post("/api/crm/visits")
def crm_visit(body: VisitIn, c: dict = Depends(crm_company)):
    try:
        return _crm.mark_visit(c["id"], body.job_id, body.day, body.status, body.note or "", body.invoice)
    except LookupError:
        raise HTTPException(404)
    except ValueError:
        raise HTTPException(400, "bad date")


# ---- photos
@app.post("/api/crm/photos")
async def crm_add_photo(file: UploadFile = File(...), customer_id: int | None = Form(None), job_id: int | None = Form(None),
                        day: str | None = Form(None), kind: str = Form(""), caption: str = Form(""),
                        c: dict = Depends(crm_company)):
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "Photos up to 15 MB.")
    if customer_id and not _crm.get_customer(c["id"], customer_id):
        raise HTTPException(404)
    if job_id:
        j = _crm.get_job(c["id"], job_id)
        if not j:
            raise HTTPException(404)
        customer_id = customer_id or j["customer_id"]
    try:
        return _crm.add_photo(c["id"], data, "jpg", customer_id, job_id, day, kind, caption)
    except Exception:
        raise HTTPException(400, "That file isn't a photo we can read.")


@app.get("/api/crm/photos")
def crm_photos(customer_id: int | None = None, job_id: int | None = None, c: dict = Depends(crm_company)):
    return _crm.list_photos(c["id"], customer_id, job_id)


@app.get("/api/crm/photos/{pid}.jpg")
def crm_photo(pid: int, c: dict = Depends(crm_company)):
    f = _crm.photo_file(c["id"], pid)
    if not f or not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")


@app.delete("/api/crm/photos/{pid}")
def crm_delete_photo(pid: int, c: dict = Depends(crm_company)):
    _crm.delete_photo(c["id"], pid)
    return {"ok": True}


# ---- invoices and money
class InvoiceIn(BaseModel):
    customer_id: int
    items: list[dict]
    note: str | None = Field(default="", max_length=1000)
    due_days: int = Field(default=14, ge=0, le=120)


@app.post("/api/crm/invoices")
def crm_add_invoice(body: InvoiceIn, c: dict = Depends(crm_company)):
    if not _crm.get_customer(c["id"], body.customer_id):
        raise HTTPException(400, "Pick a customer.")
    return _crm.create_invoice(c["id"], body.customer_id, body.items, body.note or "", None, body.due_days)


@app.get("/api/crm/invoices")
def crm_invoices(status: str | None = None, c: dict = Depends(crm_company)):
    return _crm.list_invoices(c["id"], status)


class InvoiceStatus(BaseModel):
    status: str = Field(pattern=r"^(paid|unpaid|void)$")
    method: str | None = Field(default=None, max_length=40)


@app.put("/api/crm/invoices/{iid}")
def crm_set_invoice(iid: int, body: InvoiceStatus, c: dict = Depends(crm_company)):
    if not _crm.get_invoice(c["id"], iid):
        raise HTTPException(404)
    return _crm.set_invoice(c["id"], iid, body.status, body.method)


@app.post("/api/crm/invoices/{iid}/send")
def crm_send_invoice(iid: int, request: Request, c: dict = Depends(crm_company)):
    i = _crm.get_invoice(c["id"], iid)
    if not i:
        raise HTTPException(404)
    cu = _crm.get_customer(c["id"], i["customer_id"])
    link = f"{_base_url(request)}/i/{i['token']}"
    emailed = _send(c, cu.get("email"), f"Invoice from {c['name']}: ${i['total_cents'] / 100:,.2f}",
                    f"Hi{(' ' + cu['name'].split()[0]) if cu.get('name') else ''},\n\nHere's your invoice from {c['name']} "
                    f"for ${i['total_cents'] / 100:,.2f}, due {i['due']}.\n\n{link}\n\n{c.get('pay_note') or ''}")
    return {"link": link, "emailed": emailed,
            "sms": f"Hi, it's {c['name']}. Your invoice for ${i['total_cents'] / 100:,.2f} is here: {link}"}


@app.get("/api/crm/money")
def crm_money(c: dict = Depends(crm_company)):
    return _crm.money(c["id"])


# ================================================================ owner command center
def _summary(c: dict, st: dict, cs: dict, orders: list[dict]) -> dict:
    """One company at a glance: a sentence, monthly value and flags."""
    from . import trades as T
    now = time.time()
    tl = [(T.label(t) or t).split(" &")[0].split(" (")[0] for t in c.get("trades") or []]
    mrr = billing.monthly_value_cents(c)
    paid_orders = [o for o in orders if o["status"] in ("processing", "ready")]
    parts = []
    parts.append((", ".join(tl[:4]) + (" +" + str(len(tl) - 4) if len(tl) > 4 else "") + " company") if tl else "No trades picked yet")
    if c["member"]:
        parts.append((f"member, paid yearly (${mrr * 12 / 100:,.0f}/yr)" if c.get("billing") == "year"
                      else f"member at ${mrr / 100:,.0f}/mo") + (" with CRM" if c.get("crm") else ""))
    else:
        parts.append(f"pay as you go, {c['credits']} credit{'s' if c['credits'] != 1 else ''} left")
    n = st.get("reports", 0)
    parts.append(f"{n} report{'' if n == 1 else 's'} ({st.get('reports_30d', 0)} in 30 days)")
    if paid_orders:
        parts.append(f"{len(paid_orders)} ZIP list{'s' if len(paid_orders) != 1 else ''} (${sum(o['amount_cents'] for o in paid_orders) / 100:,.0f})")
    if cs.get("customers"):
        parts.append(f"CRM: {cs['customers']} customer{'' if cs['customers'] == 1 else 's'}, ${cs['paid_cents'] / 100:,.0f} collected")
    flags = []
    if c.get("disabled"):
        flags.append(("Paused", "bad"))
    if c["member"] and c.get("member_until") and c["member_until"] - now < 7 * 86400:
        flags.append(("Renewal due in under 7 days", "warn"))
    if c.get("plan") == "member" and not c["member"]:
        flags.append(("Membership lapsed", "bad"))
    if not c["member"] and c["credits"] <= 0:
        flags.append(("Out of credits", "warn"))
    seen = c.get("last_seen") or st.get("last_report") or c["created"]
    if now - seen > 30 * 86400:
        flags.append(("Inactive 30+ days", "warn"))
    if any(o["status"] == "awaiting_payment" for o in orders):
        flags.append(("Unpaid ZIP order", "warn"))
    return {"text": ". ".join(parts) + ".", "mrr_cents": mrr, "flags": [{"text": t, "level": l} for t, l in flags],
            "orders_cents": sum(o["amount_cents"] for o in paid_orders), "last_active": seen}


def _orders_by_email() -> dict:
    out: dict = {}
    for o in datastore.list_orders(5000):
        out.setdefault(o["email"].lower(), []).append(o)
    return out


@app.get("/command")
def page_command():
    return FileResponse(STATIC / "command.html")


_BOOT = time.time()


def gis_cache_info():
    from . import gis as _g
    return _g.cache_info()


@app.get("/api/admin/status", dependencies=[Depends(admin)])
def admin_status():
    """Everything moving right now, for the command center's live screens."""
    import shutil as _sh
    from . import coverage as _cov
    from . import premier as _pr
    pre = datastore.prebuild_status()
    queue = pre.get("zips") or []
    pre_out = {"running": pre.get("running"), "status": pre.get("status", "idle"), "current": pre.get("current"),
               "total": len(queue), "done": len(pre.get("done") or []), "failed": len(pre.get("failed") or []),
               "next": datastore.queue_order([z for z in queue if z not in set(pre.get("done") or [])
                                              and z not in set(pre.get("failed") or []) and z != pre.get("current")], fetch=False)[:6],
               "started": pre.get("started")}
    orders = datastore.list_orders(400)
    day = time.time() - 86400
    def o_row(o):
        r = {k: o.get(k) for k in ("id", "zip", "count", "amount_cents", "status", "product", "created", "paid")}
        r["company"] = o["email"].split("@")[0][:2] + "…@" + o["email"].split("@")[-1]
        if o["status"] == "processing":
            cls = o["classes"].split(",") if o["classes"] else ["R"]
            r["progress"] = (_pr.progress(o["zip"], cls, datastore.order_bands(o)) if (o.get("product") or "") == "premier"
                             else datastore.dataset_progress(o["zip"], cls))
        return r
    st = accounts.company_stats()
    comps = [c for c in accounts.list_companies() if c["email"] not in (_DEMO_EMAIL, _TEST_EMAIL)]
    try:
        du = _sh.disk_usage(str(DATA_DIR))
        disk = {"used_gb": round((du.total - du.free) / 1e9, 1), "free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}
    except Exception:
        disk = None
    try:
        rss = int(next(l for l in open("/proc/self/status") if l.startswith("VmRSS")).split()[1]) // 1024
    except Exception:
        rss = None
    bks = safety.backups()
    return {
        "now": time.time(), "uptime_s": round(time.time() - _BOOT),
        "measuring": datastore.measuring_now(), "prebuild": pre_out, "sheets": datastore.recent_sheets(),
        "orders": {"processing": [o_row(o) for o in orders if o["status"] == "processing"][:10],
                   "awaiting": sum(1 for o in orders if o["status"] == "awaiting_payment"),
                   "recent": [o_row(o) for o in orders if o["status"] in ("processing", "ready")][:8],
                   "paid_today": sum(1 for o in orders if o["status"] in ("processing", "ready") and (o.get("paid") or 0) > day),
                   "cents_today": sum(o["amount_cents"] or 0 for o in orders if o["status"] in ("processing", "ready") and (o.get("paid") or 0) > day)},
        "downloads": datastore.recent_downloads(),
        "coverage": {**_cov.summary(), **_cov.status()},
        "companies": {"total": len(comps), "members": sum(1 for c in comps if c.get("member")),
                      "active_today": sum(1 for c in comps if (c.get("last_seen") or 0) > day),
                      "reports_today": sum((st.get(c["id"]) or {}).get("reports_today", 0) for c in comps),
                      "reports_30d": sum((st.get(c["id"]) or {}).get("reports_30d", 0) for c in comps)},
        "system": {"disk": disk, "memory_mb": rss, "cache": gis_cache_info(), "last_backup": bks[0]["created"] if bks else None,
                   "lockouts": len(safety.lockouts())},
    }


@app.get("/api/admin/library", dependencies=[Depends(admin)])
def admin_library():
    """Every company with a short summary, for the owner's command center."""
    stats = accounts.company_stats()
    orders = _orders_by_email()
    rows = []
    for c in accounts.list_companies():
        if c["email"] in (_DEMO_EMAIL, _TEST_EMAIL):
            continue
        st, cs = stats.get(c["id"], {}), _crm.stats(c["id"])
        rows.append({**{k: c.get(k) for k in ("id", "name", "email", "phone", "credits", "member", "plan", "member_until",
                                               "trades", "crm", "disabled", "created", "last_seen", "brand_color",
                                               "admin_notes")},
                     "stats": st, "crm_stats": cs, "summary": _summary(c, st, cs, orders.get(c["email"].lower(), []))})
    tot = {"companies": len(rows), "members": sum(1 for r in rows if r["member"]),
           "crm": sum(1 for r in rows if r["crm"] and r["member"]),
           "mrr_cents": sum(r["summary"]["mrr_cents"] for r in rows),
           "orders_cents": sum(r["summary"]["orders_cents"] for r in rows),
           "reports_30d": sum((r["stats"] or {}).get("reports_30d", 0) for r in rows),
           "flagged": sum(1 for r in rows if r["summary"]["flags"])}
    return {"companies": rows, "totals": tot}


@app.get("/api/admin/companies/{cid}/detail", dependencies=[Depends(admin)])
def admin_company_detail(cid: int):
    c = accounts.get_company(cid)
    if not c:
        raise HTTPException(404)
    st = accounts.company_stats(cid).get(cid, {})
    orders = _orders_by_email().get(c["email"].lower(), [])
    return {"company": c, "summary": _summary(c, st, _crm.stats(cid), orders), "stats": st,
            "crm_stats": _crm.stats(cid), "money": _crm.money(cid) if c.get("crm") else None,
            "ledger": accounts.ledger(cid, 30), "reports": accounts.list_reports(cid, 25),
            "orders": [{k: o.get(k) for k in ("id", "zip", "count", "amount_cents", "status", "product", "created")}
                       for o in orders[:25]],
            "plan_price_cents": billing.member_price_cents(c.get("trades"), crm=bool(c.get("crm")))}


class AdminCompanyEdit(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    brand_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    password: str | None = Field(default=None, min_length=6)
    trades: list[str] | None = None
    crm: bool | None = None
    disabled: bool | None = None
    admin_notes: str | None = Field(default=None, max_length=5000)
    comp_months: int | None = Field(default=None, ge=0, le=60)   # free membership for N months (0 ends it)


@app.patch("/api/admin/companies/{cid}/manage", dependencies=[Depends(admin)])
def admin_manage_company(cid: int, body: AdminCompanyEdit):
    """Everything about one company from the command center."""
    c = accounts.get_company(cid)
    if not c:
        raise HTTPException(404)
    f = body.model_dump()
    months = f.pop("comp_months")
    if f.get("trades") is not None:
        f["trades"] = _clean_trades(f["trades"])
    for k in ("crm", "disabled"):
        if f.get(k) is not None:
            f[k] = 1 if f[k] else 0
    accounts.update_company(cid, **{k: v for k, v in f.items() if v is not None})
    if months is not None:
        accounts.set_membership(cid, time.time() + months * 30.44 * 86400 if months else time.time(), active=bool(months))
    return admin_company_detail(cid)


@app.post("/api/admin/companies/{cid}/view-as", dependencies=[Depends(admin)])
def admin_view_as(cid: int, request: Request):
    """Open the company's portal as them (sets their session in your browser)."""
    c = accounts.get_company(cid)
    if not c:
        raise HTTPException(404)
    r = JSONResponse({"ok": True, "go": "/crm" if c.get("crm") else "/reports"})
    r.set_cookie(ROOF_COOKIE, accounts.make_session(cid, days=1), max_age=86400, httponly=True, samesite="lax",
                 secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


# ================================================================ postcard studio (owner)
@app.get("/api/admin/postcards", dependencies=[Depends(admin)])
def admin_postcards():
    from . import postcard, trades
    st = postcard.studio()
    return {**st, "defaults": {k: {"headline": h, "offer": o} for k, (h, o) in postcard.DEFAULT_COPY.items()},
            "priced_offer_default": postcard.PRICED_OFFER, "layout_defaults": postcard.LAYOUT_DEFAULTS,
            "trades": [t for t in trades.catalog() if t["key"] in trades.PRICE_RULES],
            "samples": [{"id": r["id"], "address": r["address"], "company": r["company"]} for r in accounts.list_reports(None, 30)],
            "companies": [{"id": c["id"], "name": c["name"]} for c in accounts.list_companies()
                          if c["email"] not in (_DEMO_EMAIL, _TEST_EMAIL)]}


class StudioIn(BaseModel):
    templates: dict = Field(default_factory=dict)
    layout: dict = Field(default_factory=dict)


@app.put("/api/admin/postcards", dependencies=[Depends(admin)])
def admin_postcards_save(body: StudioIn):
    from . import postcard, trades
    tpl = {}
    for k, v in body.templates.items():
        if k in trades.PRICE_RULES and isinstance(v, dict):
            t = {f: str(v.get(f) or "").strip()[:220] for f in ("headline", "offer", "priced_offer")}
            if any(t.values()):
                tpl[k] = t
    lay = {"photo_side": "right" if body.layout.get("photo_side") == "right" else "left",
           "show_facts": bool(body.layout.get("show_facts", True)),
           "show_address_tag": bool(body.layout.get("show_address_tag", True))}
    s = load_settings()
    s["postcards"] = {"templates": tpl, "layout": lay}
    save_settings(s)
    return postcard.studio()


class PreviewIn(BaseModel):
    report_id: str | None = Field(default=None, max_length=20)
    trade: str = Field(max_length=40)
    company_id: int | None = None
    headline: str | None = Field(default=None, max_length=90)
    offer: str | None = Field(default=None, max_length=220)
    layout: dict | None = None
    side: str = "both"   # front | back | both


@app.post("/api/admin/postcards/preview", dependencies=[Depends(admin)])
def admin_postcard_preview(body: PreviewIn):
    """A postcard for a sample report, as a company would send it."""
    from . import postcard, trades
    rid = body.report_id or (accounts.list_reports(None, 1) or [{}])[0].get("id")
    rep = accounts.get_report(rid) if rid else None
    if not rep:
        raise HTTPException(404, "No reports yet to preview with. Run one on the demo page first.")
    comp = accounts.get_company(body.company_id) if body.company_id else None
    if not comp:  # a sample company priced at the suggested rates
        comp = {"name": "Your Company Name", "phone": "(402) 555-0100", "email": "hello@example.com",
                "brand_color": "#15803d", "rates": trades.suggested_rates()}
    lay = {**postcard.studio()["layout"], **(body.layout or {})}
    pdf = postcard.render_postcard(rep["data"], comp, body.trade if body.trade in trades.PRICE_RULES else None,
                                   body.headline or None, body.offer or None, layout=lay)
    if body.side == "pdf":
        return Response(pdf, media_type="application/pdf")
    # images for the studio (PDFs don't show inside pages on phones)
    import base64 as _b64
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    pages = []
    for i in range(len(doc)):
        im = doc[i].render(scale=1.6).to_pil()
        b = io.BytesIO()
        im.save(b, "JPEG", quality=85)
        pages.append("data:image/jpeg;base64," + _b64.b64encode(b.getvalue()).decode())
    return {"pages": pages, "pdf": "data:application/pdf;base64," + _b64.b64encode(pdf).decode()}


@app.get("/api/admin/zip-folders", dependencies=[Depends(admin)])
def admin_zip_folders():
    """Every Premier folder on file (whole-ZIP reports), and whether it can make postcards."""
    import zipfile as _zf
    from . import premier
    out = []
    for m in sorted(premier.PREMIER_DIR.glob("*.json")):
        try:
            meta = json.loads(m.read_text())
        except ValueError:
            continue
        z = m.with_suffix(".zip")
        if meta.get("status") != "ready" or not z.exists():
            continue
        try:
            with _zf.ZipFile(z) as f:
                mail = sum(1 for n in f.namelist() if n.startswith("mail/data/"))
        except Exception:
            mail = 0
        out.append({"key": m.stem, "zip": m.stem.split("_")[0], "homes": meta.get("reports") or meta.get("total"),
                    "finished": meta.get("finished"), "size": z.stat().st_size, "postcards": mail})
    return out


class AdminMailIn(MailReq):
    folder: str = Field(max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    company_id: int | None = None


@app.post("/api/admin/postcards/mailing", dependencies=[Depends(admin)])
def admin_postcard_mailing(body: AdminMailIn):
    """Postcards for a whole stored ZIP, branded for a company (or your own)."""
    from . import premier
    src = premier.PREMIER_DIR / f"{body.folder}.zip"
    if not src.exists():
        raise HTTPException(404, "That ZIP folder isn't on file.")
    base = (accounts.get_company(body.company_id) if body.company_id else None) or {
        "name": (load_settings().get("business") or {}).get("name"), "phone": (load_settings().get("business") or {}).get("phone")}
    return _mail_view(_start_mailing(f"admin_{body.folder}_{body.company_id or 0}", src, body, base))


@app.get("/api/admin/postcards/mailing/{folder}/{cid}/status", dependencies=[Depends(admin)])
def admin_postcard_mailing_status(folder: str, cid: int):
    return _mail_view(_mail_jobs.get(f"admin_{folder}_{cid}"))


@app.get("/api/admin/postcards/mailing/{folder}/{cid}.zip")
def admin_postcard_mailing_zip(folder: str, cid: int, token: str | None = None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403)
    j = _mail_jobs.get(f"admin_{folder}_{cid}")
    if not j or j["status"] != "ready":
        raise HTTPException(409, "not ready")
    return FileResponse(j["path"], media_type="application/zip", filename=f"postcards_{folder}.zip")


# ================================================================ marketing hub (owner)
from . import marketing as _mk  # noqa: E402


@app.get("/command/marketing")
def page_marketing():
    return FileResponse(STATIC / "marketing.html")


@app.get("/api/admin/marketing", dependencies=[Depends(admin)])
def mk_list():
    _mk.seed_if_empty()
    from . import trades as _tr
    return {"items": _mk.list_items(), "kinds": _mk.KINDS, "statuses": _mk.STATUSES, "channels": _mk.CHANNELS,
            "trades": [t["label"].split(" &")[0].split(" (")[0] for t in _tr.catalog() if "beta" not in t["label"].lower()]
            + ["CRM", "Plan"]}


class MkIn(BaseModel):
    kind: str | None = None
    title: str | None = Field(default=None, max_length=160)
    body: str | None = Field(default=None, max_length=20000)
    tags: list[str] | None = None
    status: str | None = None


@app.post("/api/admin/marketing", dependencies=[Depends(admin)])
def mk_create(body: MkIn):
    return _mk.create(body.kind or "idea", body.title or "Untitled", body.body or "", body.tags or [], body.status or "draft")


@app.put("/api/admin/marketing/{iid}", dependencies=[Depends(admin)])
def mk_update(iid: int, body: MkIn):
    if not _mk.get(iid):
        raise HTTPException(404)
    return _mk.update(iid, **body.model_dump())


@app.post("/api/admin/marketing/{iid}/file", dependencies=[Depends(admin)])
async def mk_upload(iid: int, file: UploadFile = File(...)):
    try:
        return await _mk.attach(iid, file)
    except LookupError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(413, str(e))


@app.post("/api/admin/marketing/{iid}/share", dependencies=[Depends(admin)])
def mk_share(iid: int, on: bool = True):
    if not _mk.get(iid):
        raise HTTPException(404)
    return _mk.set_share(iid, on)


@app.delete("/api/admin/marketing/{iid}", dependencies=[Depends(admin)])
def mk_delete(iid: int):
    _mk.delete(iid)
    return {"ok": True}


@app.get("/api/admin/marketing/{iid}/media")
def mk_media(iid: int, token: str | None = None):
    """The item's file for the owner (token in the URL so <video> can play it)."""
    if ADMIN_TOKEN and not hmac.compare_digest((token or "").strip().encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(403)
    it = _mk.get(iid)
    p = _mk.file_path(it)
    if not p or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type=it["mime"])


@app.get("/m/{tok}")
def mk_public_page(tok: str):
    return FileResponse(STATIC / "share.html")


@app.get("/api/public/marketing/{tok}")
def mk_public(tok: str):
    it = _mk.by_token(tok)
    if not it:
        raise HTTPException(404, "This link isn't available.")
    return {k: it.get(k) for k in ("kind", "title", "body", "mime")} | {"has_file": bool(it.get("file"))}


@app.get("/m/{tok}/file")
def mk_public_file(tok: str):
    it = _mk.by_token(tok)
    p = _mk.file_path(it) if it else None
    if not p or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type=it["mime"])



# ================================================================ safety (owner) + password reset + legal pages
@app.get("/command/safety")
def page_safety():
    return FileResponse(STATIC / "safety.html")


@app.get("/api/admin/safety", dependencies=[Depends(admin)])
def admin_safety():
    return {"checklist": safety.checklist(bool(ADMIN_TOKEN)), "backups": safety.backups(),
            "lockouts": safety.lockouts(), "audit": safety.audit(200)}


@app.post("/api/admin/safety/backup", dependencies=[Depends(admin)])
def admin_backup_now():
    return safety.backup_now()


@app.get("/api/admin/safety/backup/{name}")
def admin_backup_download(name: str, request: Request, token: str | None = None):
    if ADMIN_TOKEN and not hmac.compare_digest((token or "").strip().encode(), ADMIN_TOKEN.encode()):
        safety.failed("admin:" + _client_ip(request))
        raise HTTPException(403)
    p = safety.backup_path(name)
    if not p:
        raise HTTPException(404)
    safety.log(_client_ip(request), "GET", f"/api/admin/safety/backup/{name}", 200, "backup downloaded")
    return FileResponse(p, media_type="application/zip", filename=name)


class ForgotIn(BaseModel):
    email: str = Field(max_length=200)


@app.post("/api/roof/forgot")
def forgot_password(body: ForgotIn, request: Request):
    """Email a reset link (always answers the same, so it can't be used to
    find out which emails have accounts)."""
    ip = _client_ip(request)
    if safety.locked("forgot:" + ip):
        raise HTTPException(429, "Too many requests. Try again later.")
    safety.failed("forgot:" + ip)  # counts requests: 10 per 15 minutes
    c = accounts.company_by_email(body.email.strip().lower())
    if c and not c.get("disabled") and not c["email"].endswith(".invalid"):
        from . import mailer
        link = f"{_base_url(request)}/reset?t={accounts.make_reset(c['id'])}"
        mailer.send(c["email"], "Reset your password",
                    f"Someone (hopefully you) asked to reset the password for {c['name']}.\n\n"
                    f"Set a new one here (the link works for 1 hour):\n{link}\n\n"
                    "If you didn't ask, ignore this email; your password stays the same.")
    return {"ok": True, "message": "If that email has an account, a reset link is on its way."}


class ResetIn(BaseModel):
    token: str = Field(max_length=300)
    password: str = Field(min_length=8, max_length=200)


@app.post("/api/roof/reset")
def reset_password(body: ResetIn, request: Request):
    cid = accounts.read_reset(body.token)
    if not cid:
        raise HTTPException(400, "This reset link has expired or was already used. Ask for a new one.")
    accounts.update_company(cid, password=body.password)
    safety.succeeded("email:" + (accounts.get_company(cid) or {}).get("email", ""))
    r = JSONResponse({"ok": True})
    r.set_cookie(ROOF_COOKIE, accounts.make_session(cid), max_age=30 * 86400, httponly=True, samesite="lax",
                 secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.get("/reset")
def page_reset():
    return FileResponse(STATIC / "reset.html")


@app.get("/terms")
@app.get("/privacy")
@app.get("/refunds")
def page_legal():
    return FileResponse(STATIC / "legal.html")



# ================================================================ coverage (owner): grow nationwide
@app.get("/command/coverage")
def page_coverage():
    return FileResponse(STATIC / "coverage.html")


@app.get("/api/admin/coverage", dependencies=[Depends(admin)])
def cov_overview():
    from . import coverage as cv
    builtin = {c["fips"]: c["id"] for c in counties.COUNTIES.values()}
    cos = cv.counties()
    for c in cos:
        if c["fips"] in builtin:
            c["status"] = "live"
        c.pop("config", None)
    return {"summary": cv.summary(), "worker": cv.status(), "metros": cv.metros(), "counties": cos,
            "queue": datastore.prebuild_status(), "settings": cv.auto_settings()}


@app.post("/api/admin/coverage/run", dependencies=[Depends(admin)])
def cov_run(limit: int | None = None):
    """Load the metro list, map every metro and search every county (background)."""
    from . import coverage as cv
    return cv.run_all(limit)


class CovAuto(BaseModel):
    auto_approve: bool


@app.put("/api/admin/coverage/auto", dependencies=[Depends(admin)])
def cov_auto(body: CovAuto):
    """Turn automatic approval of found counties on or off."""
    s = load_settings()
    s["coverage"] = {**(s.get("coverage") or {}), "auto_approve": body.auto_approve}
    save_settings(s)
    from . import coverage as cv
    return cv.auto_settings()


@app.post("/api/admin/coverage/approve-all", dependencies=[Depends(admin)])
def cov_approve_all():
    """Approve every found county that passes the checks, now (background)."""
    from . import coverage as cv
    threading.Thread(target=cv.approve_all_found, daemon=True, name="approve-all").start()
    return {"started": True}


@app.post("/api/admin/coverage/stop", dependencies=[Depends(admin)])
def cov_stop():
    from . import coverage as cv
    cv.stop()
    return cv.status()


@app.post("/api/admin/coverage/metro/{cbsa}/map", dependencies=[Depends(admin)])
def cov_map_metro(cbsa: str):
    from . import coverage as cv
    if not cv.metro(cbsa):
        raise HTTPException(404)
    return cv.map_metro(cbsa)


@app.post("/api/admin/coverage/county/{fips}/discover", dependencies=[Depends(admin)])
def cov_discover(fips: str):
    from . import coverage as cv
    if not cv.county(fips):
        raise HTTPException(404)
    return {"candidates": cv.discover(fips)}


class CovTest(BaseModel):
    url: str = Field(max_length=500, pattern=r"^https://")


@app.post("/api/admin/coverage/county/{fips}/test", dependencies=[Depends(admin)])
def cov_test(fips: str, body: CovTest):
    """Test a parcel layer URL you found yourself."""
    from . import coverage as cv
    g = cv._county_geom(fips)
    out = []
    for lurl, meta in cv._layer_candidates(body.url.rstrip("/")):
        r = cv.test_layer(lurl, meta, g)
        if r:
            out.append(r)
    if not out:
        raise HTTPException(400, "That layer didn't work: it needs parcel polygons with a parcel id and an address, inside this county.")
    c = cv.county(fips)
    cands = (c.get("candidates") or []) if isinstance(c.get("candidates"), list) else []
    cv._set("counties", "fips", fips, candidates=json.dumps(out + cands), status="found")
    return {"candidates": out}


@app.get("/api/admin/coverage/classes", dependencies=[Depends(admin)])
def cov_classes(url: str, field: str):
    from . import coverage as cv
    if not url.startswith("https://") or not field.replace("_", "").isalnum():
        raise HTTPException(400)
    return cv.class_values(url, field)


class CovApprove(BaseModel):
    url: str = Field(max_length=500, pattern=r"^https://")
    fields: dict
    oid: str = Field(default="OBJECTID", max_length=60)
    residential_where: str | None = Field(default=None, max_length=1000)
    imagery_url: str | None = Field(default=None, max_length=500)


@app.post("/api/admin/coverage/county/{fips}/approve", dependencies=[Depends(admin)])
def cov_approve(fips: str, body: CovApprove):
    """Make the county live with this parcel layer."""
    from . import coverage as cv
    if not cv.county(fips):
        raise HTTPException(404)
    if body.imagery_url and body.imagery_url != "esri" and not re.match(r"^https://[^ ]+\{z\}[^ ]*\{y\}[^ ]*\{x\}", body.imagery_url):
        raise HTTPException(400, "Imagery must be a tile URL with {z}, {y} and {x} in it.")
    if body.residential_where and re.search(r";|--|\b(drop|delete|insert|update)\b", body.residential_where, re.I):
        raise HTTPException(400, "That filter isn't allowed.")
    c = cv.county(fips)
    cands = c.get("candidates") if isinstance(c.get("candidates"), list) else []
    samples = next((k.get("samples") for k in cands if k.get("url") == body.url), None)
    cfg = cv.approve(fips, body.url, {k: v for k, v in body.fields.items() if isinstance(v, str)},
                     body.residential_where, body.imagery_url, body.oid, samples)
    return {"id": cfg["id"], "name": cfg["name"], "lidar": (cfg.get("lidar") or {}).get("source")}


@app.post("/api/admin/coverage/metro/{cbsa}/pull", dependencies=[Depends(admin)])
def cov_pull(cbsa: str):
    """Measure every ZIP within 15 miles of the metro (once its counties are live)."""
    from . import coverage as cv
    zs = cv.metro_ready_zips(cbsa)
    if not zs:
        raise HTTPException(409, "Every county in this metro's 15-mile circle has to be live first.")
    datastore.prebuild_all(["R"], zips=zs)
    cv._set("metros", "cbsa", cbsa, status="live", note=f"Measuring {len(zs)} ZIPs")
    return {"queued": len(zs), "queue": datastore.prebuild_status()}



# ================================================================ owner ZIP map
@app.get("/command/map")
def page_zipmap():
    return FileResponse(STATIC / "zipmap.html")


@app.get("/api/admin/zipmap", dependencies=[Depends(admin)])
def admin_zipmap():
    """Every covered ZIP with purchases, revenue, measured and locked."""
    return datastore.zip_heat()


@app.get("/api/admin/zipmap/outlines", dependencies=[Depends(admin)])
def admin_zipmap_outlines(zips: str = ""):
    return datastore.zip_outlines([z for z in zips.split(",") if len(z) == 5 and z.isdigit()])
