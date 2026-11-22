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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import mapping, Point

from . import bulk, corrections, render
from .gis import get_json, query_features
from .imagery import Mosaic
from .measure import default_measurer, MEASUREMENTS
from .pricing import DATA_DIR, estimate, load_settings, save_settings

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
LEADS = DATA_DIR / "leads.jsonl"
ADMIN_TOKEN = os.environ.get("ESTIMATOR_ADMIN_TOKEN", "")

app = FastAPI(title="Property Estimator")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def admin(request: Request):
    if not ADMIN_TOKEN:
        return  # dev mode: open. Set ESTIMATOR_ADMIN_TOKEN in production.
    tok = request.headers.get("x-admin-token") or request.query_params.get("token")
    if tok != ADMIN_TOKEN:
        raise HTTPException(401, "admin token required")


# ------------------------------------------------------------------- pages
@app.get("/")
def page_customer():
    return FileResponse(STATIC / "index.html")


@app.get("/business")
def page_business():
    return FileResponse(STATIC / "business.html")


# --------------------------------------------------------------- public API
@app.get("/api/config")
def config():
    m = default_measurer()
    s = load_settings()
    return {
        "business": s["business"],
        "services": {k: {"label": v["label"], "unit": v["unit"], "finishes": {fk: fv["label"] for fk, fv in v.get("finishes", {}).items()}}
                     for k, v in s["services"].items() if v.get("enabled")},
        "county": {"name": m.c["name"], "center": m.c["center"]},
        "imagery": {"tile_url": m.c["imagery"]["tile_url"],
                    "year": m.c["imagery"]["year"], "max_native_zoom": m.c["imagery"].get("max_zoom", 21)},
        "render_available": render.available(),
        "measurements": MEASUREMENTS,
    }


@app.get("/api/suggest")
def suggest(q: str):
    m = default_measurer()
    if len(q.strip()) < 3:
        return []
    d = get_json(m.c["geocoder"] + "/suggest", {"text": q, "maxSuggestions": 8, "f": "json"}, ttl=86400)
    return [{"text": s["text"], "magicKey": s.get("magicKey")} for s in d.get("suggestions", [])]


@app.get("/api/locate")
def locate(q: str, magicKey: str | None = None):
    m = default_measurer()
    params = {"SingleLine": q, "outSR": 4326, "maxLocations": 5, "f": "json"}
    if magicKey:
        params["magicKey"] = magicKey
    d = get_json(m.c["geocoder"] + "/findAddressCandidates", params, ttl=86400)
    out = [{"address": c["address"], "lat": c["location"]["y"], "lon": c["location"]["x"], "score": c["score"]}
           for c in d.get("candidates", [])]
    if not out:
        raise HTTPException(404, "Address not found in this county.")
    best = out[0]
    hit = _parcel_at(best["lat"], best["lon"])
    return {"candidates": out, "best": best, "suggested_pin": hit["attributes"][m.pf["pin"]] if hit else None}


def _parcel_at(lat: float, lon: float):
    m = default_measurer()
    d = 0.00002
    feats = query_features(m.c["parcels"]["url"], envelope=(lon - d, lat - d, lon + d, lat + d),
                           out_fields=f"{m.pf['pin']},{m.pf['address']}")
    pt = Point(lon, lat)
    feats = [f for f in feats if f["geometry"] is not None]
    feats.sort(key=lambda f: (not f["geometry"].contains(pt), f["geometry"].area))
    return feats[0] if feats else None


@app.get("/api/parcels")
def parcels(bbox: str):
    """Parcel outlines in the map view, so the customer can click theirs."""
    m = default_measurer()
    w, s, e, n = [float(v) for v in bbox.split(",")]
    if (e - w) * (n - s) > 0.0004:  # roughly 1.5 x 1.5 km: zoom in first
        raise HTTPException(400, "zoom in to load parcels")
    pf = m.pf
    feats = query_features(m.c["parcels"]["url"], envelope=(w, s, e, n),
                           out_fields=",".join([pf["pin"], pf["address"], pf["class"]]), limit=1500)
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": mapping(f["geometry"]),
         "properties": {"pin": f["attributes"].get(pf["pin"]), "address": (f["attributes"].get(pf["address"]) or "").strip(),
                        "class": f["attributes"].get(pf["class"])}}
        for f in feats if f["geometry"] is not None]}


@lru_cache(maxsize=512)
def _measure_cached(pin: str) -> str:
    m = default_measurer()
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
def _aerial_png(pin: str) -> bytes:
    m = default_measurer()
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
    return corrections.get(pin) or {"exclude": [], "include": []}


class Corrections(BaseModel):
    exclude: list[dict] = []
    include: list[dict] = []


@app.put("/api/admin/corrections/{pin}", dependencies=[Depends(admin)])
def put_corrections(pin: str, body: Corrections):
    for g in body.exclude + body.include:
        if g.get("type") != "Polygon" or not g.get("coordinates"):
            raise HTTPException(400, "corrections must be GeoJSON polygons")
    out = corrections.put(pin, body.exclude, body.include)
    _measure_cached.cache_clear()
    return out


@app.get("/api/admin/leads", dependencies=[Depends(admin)])
def leads():
    if not LEADS.exists():
        return []
    rows = [json.loads(l) for l in LEADS.read_text().splitlines() if l.strip()]
    return list(reversed(rows))
