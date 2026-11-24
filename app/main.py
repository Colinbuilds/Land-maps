"""Property estimator API + web front ends.

Run:  uvicorn app.main:app --reload     (from the estimator/ directory)
Customer page:  http://localhost:8000/
Business page:  http://localhost:8000/business
"""
from __future__ import annotations

import io
import json
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
ADMIN_TOKEN = os.environ.get("ESTIMATOR_ADMIN_TOKEN", "")

app = FastAPI(title="Property Estimator")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    """Phones (Safari especially) keep old copies of scripts, so a new page can
    end up running an old script. Make browsers re-check pages and scripts on
    every load; unchanged files still come back as a cheap 304."""
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or not path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def admin(request: Request):
    if not ADMIN_TOKEN:
        return  # dev mode: open. Set ESTIMATOR_ADMIN_TOKEN in production.
    tok = request.headers.get("x-admin-token") or request.query_params.get("token")
    if tok != ADMIN_TOKEN:
        raise HTTPException(401, "admin token required")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------- pages
@app.get("/")
def page_customer():
    return FileResponse(STATIC / "index.html")


@app.get("/business")
def page_business():
    return FileResponse(STATIC / "business.html")


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
    return billing.refresh(c) if c else None


def roof_company(request: Request) -> dict:
    c = _session_company(request)
    if not c:
        raise HTTPException(401, "please log in")
    return c


@app.get("/roof")
def page_roof():
    return FileResponse(STATIC / "roof.html")


@app.get("/r/{rid}")
def page_roof_report(rid: str):
    return FileResponse(STATIC / "roof-report.html")


class RoofLogin(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@app.post("/api/roof/login")
def roof_login(body: RoofLogin, request: Request):
    c = accounts.login(body.email, body.password)
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
    from . import mailer
    mailer.notify_owner(f"Property report {rid} ordered by {c['name']}",
                        f"{c['name']} ({c['email']}) ordered a property report for {rep.get('address')}.\n"
                        f"Credits left: {left}.", reply_to=c["email"])
    if left <= 2:
        mailer.send(c["email"], f"{left} report credit{'s' if left != 1 else ''} left",
                    f"Your company has {left} property report credit{'s' if left != 1 else ''} left. "
                    "Reply to this email to add more.")
    return {"id": rid, "credits": left}


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
    return {"id": rep["id"], "created": rep["created"], "share_token": rep["share_token"], "data": rep["data"],
            "company": {k: comp.get(k) for k in ("name", "phone", "email", "brand_color")}}


@app.get("/r/{rid}/pdf")
def roof_report_pdf(rid: str, request: Request):
    rep, comp = _report_access(rid, request)
    pdf = render_pdf(rep, comp)
    safe = "".join(ch if ch.isalnum() else "_" for ch in rep["data"].get("address", "roof"))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Roof_Report_{safe}_{rep["id"]}.pdf"'})


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
    return (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "")


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.get("/data")
def page_data():
    return FileResponse(STATIC / "data.html")


@app.get("/data/order/{oid}")
def page_data_order(oid: str):
    return FileResponse(STATIC / "data-order.html")


# ------------------------------------------------------------- membership
class JoinReq(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)
    phone: str = Field(default="", max_length=40)


@app.get("/join")
@app.get("/join/done")
def page_join():
    return FileResponse(STATIC / "join.html")


@app.get("/api/billing/plans")
def billing_plans(request: Request):
    c = _session_company(request)
    p = billing.plans()
    return {"member_price_cents": p["member_price_cents"], "credit_price_cents": p["credit_price_cents"],
            "list_discount": p["list_discount"], "packs": list(billing.CREDIT_PACKS),
            "stripe": billing.enabled(), "company": c}


@app.post("/api/join")
def join(body: JoinReq, request: Request):
    if accounts.company_by_email(body.email):
        raise HTTPException(409, "There's already an account with that email. Log in instead.")
    c = accounts.create_company(body.name, body.email, body.password, 0, body.phone)
    from . import mailer
    mailer.notify_owner(f"New company sign-up: {c['name']}",
                        f"{c['name']} ({c['email']}, {body.phone or 'no phone'}) created an account.", reply_to=c["email"])
    r = JSONResponse(c)
    r.set_cookie(ROOF_COOKIE, accounts.make_session(c["id"]), max_age=30 * 86400, httponly=True,
                 samesite="lax", secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.post("/api/billing/member")
def billing_member(request: Request, c: dict = Depends(roof_company)):
    if not billing.enabled():
        raise HTTPException(400, "Card payments aren't set up yet. Contact us to join.")
    if c["member"]:
        raise HTTPException(400, "You're already a member.")
    try:
        return {"checkout_url": billing.member_checkout(c, _base_url(request))}
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
                            lo=vlo, hi=vhi)
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
        rows = datastore.take_sample(body.email, _client_ip(request), z, _classes(body.classes))
    except LookupError as e:
        raise HTTPException(404, str(e))
    keys = [k for k, _ in datastore.COLUMNS]
    return {"zip": z, "columns": [{"key": k, "label": l} for k, l in datastore.COLUMNS], "rows": rows,
            "csv": datastore.to_csv(rows), "keys": keys}


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
        o = datastore.create_order(body.email, z, _classes(body.classes), body.types, site=_base_url(request),
                                   product=body.product, member=bool(c and c["member"]),
                                   lo=body.value_lo, hi=body.value_hi)
    except LookupError as e:
        raise HTTPException(404, str(e))
    out = {"id": o["id"], "token": o["token"], "amount_cents": o["amount_cents"], "count": o["count"],
           "order_url": f"/data/order/{o['id']}?t={o['token']}"}
    if datastore.stripe_enabled():
        try:
            out["checkout_url"] = datastore.stripe_checkout(o, _base_url(request))
        except Exception as e:
            raise HTTPException(502, f"Payment provider error: {str(e)[:200]}")
    return out


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
        return FileResponse(premier.path(o["zip"], classes, bands), media_type="application/zip",
                            filename=f"premier_{o['zip']}_{o['id']}.zip")
    if o["status"] != "ready" and not (o["status"] == "processing" and datastore.dataset_ready(o["zip"], classes)):
        raise HTTPException(409, "Your sheet isn't ready yet.")
    rows = datastore.rows_in_bands(datastore.read_dataset(o["zip"], classes), *datastore.order_bands(o))
    cols = datastore.columns_for(datastore.order_types(o))  # only what they paid for
    return Response(datastore.to_csv(rows, cols), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="properties_{o["zip"]}_{o["id"]}.csv"'})


# ---- owner
@app.get("/api/admin/data", dependencies=[Depends(admin)])
def admin_data():
    zips = []
    for p in sorted(datastore.ZIPS_DIR.glob("*.json")):
        meta = json.loads(p.read_text())
        zips.append({"key": p.stem, **meta})
    return {"pricing": datastore.pricing(), "orders": datastore.list_orders(), "zips": zips,
            "stripe": datastore.stripe_enabled()}


class DataPricing(BaseModel):
    rate_cents: float = Field(ge=0.5, le=100)          # first measurement type, per property
    extra_type_cents: float = Field(ge=0, le=100)      # each extra type, per property
    premier_cents: float = Field(default=25.0, ge=1, le=500)  # Premier list, per property
    min_order_usd: float = Field(ge=0, le=100_000)
    value_multipliers: list[float] = Field(default=[1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
                                           min_length=9, max_length=9)


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
