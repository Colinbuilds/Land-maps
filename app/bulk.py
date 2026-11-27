"""Bulk measurement of every parcel in a ZIP code, written to CSV."""
from __future__ import annotations

import csv
import json
import math
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import counties
from .gis import _esri_to_geojson_geom, get_json, query_features
from .measure import MEASUREMENTS, measurer_for
from .pricing import DATA_DIR

JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

CLASS_LABELS = {"R": "Residential", "C": "Commercial", "I": "Industrial", "A": "Agricultural",
                "E": "Exempt", "M": "Mixed", "F": "Other"}

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


_ZCTA = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1"


def zip_polygon(zip_code: str):
    """The Census ZIP Code Tabulation Area for a 5-digit ZIP (WGS84)."""
    from shapely.geometry import shape
    z = "".join(ch for ch in zip_code if ch.isdigit())[:5]
    d = get_json(_ZCTA + "/query", {"where": f"ZCTA5='{z}'", "outFields": "ZCTA5", "outSR": 4326,
                                    "geometryPrecision": 6, "f": "json"}, ttl=365 * 86400)
    feats = d.get("features") or []
    if not feats:
        raise LookupError(f"ZIP {z} not found")
    return shape(_esri_to_geojson_geom(feats[0]["geometry"]))


# ZIP sheets skip parcels bigger than this (farmland, ranches, golf courses)
MAX_ACRES = 40.0


def _class_where(m, classes: list[str]) -> str:
    table = m.c["parcels"].get("classes", {})
    parts = [f"({table[c]})" for c in classes if c in table]
    return " OR ".join(parts) if parts else "1=1"


def _zip_parcels(zip_code: str, classes: list[str], limit: int | None = None, skipped: list | None = None):
    """[(county_id, feature)] for parcels whose centre lies in the ZIP, across
    every county the ZIP touches. A county whose parcel server is down is
    skipped (named in ``skipped``) so the rest of the ZIP still gets measured;
    if every county fails, the error is raised."""
    zp = zip_polygon(zip_code)
    out = []
    cids = counties.counties_in(zp.bounds)
    fails = []
    seen = set()
    for cid in cids:
        m = measurer_for(cid)
        geom = {"geometry": json.dumps({"rings": [list(map(list, r.coords)) for p in getattr(zp.simplify(0.0003), "geoms", [zp.simplify(0.0003)])
                                                  for r in [p.exterior]], "spatialReference": {"wkid": 4326}}),
                "geometryType": "esriGeometryPolygon", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}
        try:
            feats = query_features(m.c["parcels"]["url"], where=_class_where(m, classes), geometry=geom,
                                   limit=(limit - len(out)) if limit else None)
        except Exception as e:
            fails.append((m.c["name"], e))
            continue
        for f in feats:
            g = f["geometry"]
            if g is None or not zp.contains(g.representative_point()):
                continue
            c = g.centroid
            acres = g.area * (111_320.0 ** 2) * math.cos(math.radians(c.y)) / 4046.86
            if acres > MAX_ACRES:  # farms, ranches, golf courses: not a home's yard, and slow
                continue
            key = (round(c.x, 6), round(c.y, 6), round(acres, 3))
            if key in seen:  # the same lot listed twice (stacked records, split accounts)
                continue
            seen.add(key)
            out.append((cid, f))
        if limit and len(out) >= limit:
            break
    if fails and len(fails) == len(cids):
        raise fails[0][1]
    if skipped is not None:
        skipped += [name for name, _ in fails]
    return out[:limit] if limit else out


def owner_info(m, f) -> dict:
    """Owner name and mailing address from the assessor record. Empty for
    counties where using public-record names for marketing isn't allowed
    (``owner_names: False`` in the county config, e.g. Kansas, K.S.A. 45-230)."""
    if m.c.get("owner_names", True) is False:
        return {"owner": "", "owner_mailing": ""}
    a = m.parcel_attrs(f["attributes"])
    raw = f["attributes"]
    mf = {**{"mail_address": "ADDRESS1", "mail_city": "OWNER_CITY", "mail_state": "OWNER_STAT", "mail_zip": "OWNER_ZIP"},
          **{k: v for k, v in m.c["parcels"]["fields"].items() if k.startswith("mail_")}}
    parts = [str(raw.get(v) or "").strip() for v in (mf["mail_address"], mf["mail_city"], mf["mail_state"], mf["mail_zip"])]
    mailing = ", ".join(p for p in [parts[0], " ".join(p for p in parts[1:] if p)] if p)
    return {"owner": " ".join(str(a.get("owner") or "").split()), "owner_mailing": mailing}


_owner_cache: dict = {}


def zip_owners(zip_code: str, classes: list[str]) -> dict:
    """{property id: owner info} for a ZIP, from the county parcel records
    (no measuring; cached for a day). Fills owner names into every sheet,
    old or new, at download time."""
    key = (zip_code, tuple(sorted(classes)))
    hit = _owner_cache.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    out = {}
    for cid, f in _zip_parcels(zip_code, classes):
        m = measurer_for(cid)
        pin = m.parcel_attrs(f["attributes"]).get("pin")
        if pin is not None:
            out[counties.make_pid(cid, pin)] = owner_info(m, f)
    _owner_cache[key] = (time.time(), out)
    return out


def preview(zip_code: str, classes: list[str]) -> dict:
    zp = zip_polygon(zip_code)
    from .gis import get_json as _gj
    total, by = 0, {}
    for cid in counties.counties_in(zp.bounds):
        m = measurer_for(cid)
        geom = {"geometry": json.dumps({"rings": [list(map(list, p.exterior.coords)) for p in getattr(zp.simplify(0.0003), "geoms", [zp.simplify(0.0003)])],
                                        "spatialReference": {"wkid": 4326}}),
                "geometryType": "esriGeometryPolygon", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}
        d = _gj(m.c["parcels"]["url"] + "/query", {"where": _class_where(m, classes), "returnCountOnly": "true",
                                                   "f": "json", **geom}, method="POST")
        n = int(d.get("count", 0))
        if n:
            by[m.c["name"]] = n
            total += n
    return {"count": total, "by_county": by}


# Assessed-value bands, every $250k: <250k, 250-500k, ..., 1.75-2M, 2M+
VALUE_STEP = 250_000
VALUE_TOP = 2_000_000
VALUE_EDGES = tuple(range(VALUE_STEP, VALUE_TOP + 1, VALUE_STEP))
_band_cache: dict = {}


def band_of(value) -> int:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    return sum(1 for e in VALUE_EDGES if v >= e)


def band_label(i: int) -> str:
    def k(v):
        return f"${v / 1e6:g}M" if v >= 1_000_000 else f"${v // 1000:g}k"
    lo = VALUE_EDGES[i - 1] if i > 0 else 0
    if i == 0:
        return f"Under {k(VALUE_EDGES[0])}"
    if i >= len(VALUE_EDGES):
        return f"{k(lo)}+"
    return f"{k(lo)}-{k(VALUE_EDGES[i])}"


def value_bands(zip_code: str, classes: list[str]) -> dict:
    """Homes in the ZIP per assessed-value band, from the county parcel
    records (no measuring). Counted on parcel centres, the same rule the
    sheets use, one count per parcel id. Cached for a day."""
    key = (zip_code, tuple(sorted(classes)))
    hit = _band_cache.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    counts = [0] * (len(VALUE_EDGES) + 1)
    unknown = 0
    seen = set()
    for cid, f in _zip_parcels(zip_code, classes):
        m = measurer_for(cid)
        a = m.parcel_attrs(f["attributes"])
        pid = (cid, a.get("pin"))
        if pid in seen:
            continue
        seen.add(pid)
        v = a.get("home_value")
        if not v:
            unknown += 1
        counts[band_of(v)] += 1
    out = {"bands": counts, "labels": [band_label(i) for i in range(len(counts))],
           "unknown_value": unknown, "edges": list(VALUE_EDGES), "total": sum(counts)}
    _band_cache[key] = (time.time(), out)
    return out


def start_job(zip_code: str, classes: list[str], measures: list[str], limit: int | None = None,
              include_owner: bool = False, workers: int = 6, on_done=None) -> dict:
    measures = [k for k in measures if k in MEASUREMENTS] or list(MEASUREMENTS)
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id, "zip": zip_code, "classes": classes, "measures": measures, "limit": limit,
        "include_owner": include_owner, "status": "queued", "total": 0, "done": 0, "errors": 0,
        "started": time.time(), "finished": None, "totals": {k: 0 for k in measures},
    }
    with _lock:
        _jobs[job_id] = job
    _save(job)
    threading.Thread(target=_run, args=(job, workers, on_done), daemon=True).start()
    return job


def get_job(job_id: str) -> dict | None:
    with _lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    p = JOBS_DIR / f"{job_id}.json"
    return json.loads(p.read_text()) if p.exists() else None


def list_jobs() -> list[dict]:
    jobs = {}
    for p in JOBS_DIR.glob("*.json"):
        try:
            j = json.loads(p.read_text())
            jobs[j["id"]] = j
        except Exception:
            pass
    with _lock:
        for k, v in _jobs.items():
            jobs[k] = dict(v)
    return sorted(jobs.values(), key=lambda j: j["started"], reverse=True)


def list_running() -> list[dict]:
    with _lock:
        return [dict(j) for j in _jobs.values() if not j.get("finished")]


def csv_path(job_id: str):
    return JOBS_DIR / f"{job_id}.csv"


def _save(job):
    (JOBS_DIR / f"{job['id']}.json").write_text(json.dumps(job))


def _run(job: dict, workers: int, on_done=None):
    try:
        job["status"] = "loading parcels"
        skipped: list = []
        items = _zip_parcels(job["zip"], job["classes"], job["limit"] or None, skipped)
        if skipped:
            job["skipped"] = skipped  # those counties' homes are missing until their server is back
        # Process in spatial order so neighbouring lots share cached downloads.
        def cell(item):
            c = item[1]["geometry"].centroid
            return (round(c.y / 0.004), round(c.x / 0.004), c.x)
        items.sort(key=cell)
        job["total"] = len(items)
        job["status"] = "measuring"
        job["measure_started"] = time.time()
        _save(job)

        cols = (["pin", "county", "address", "city", "zip", "class", "home_value", "year_built", "living_sqft"]
                + job["measures"] + ["driveway_method", "sidewalk_detected", "quality", "notes"])
        if job["include_owner"]:
            cols += ["owner", "mail_address", "mail_city", "mail_state", "mail_zip"]
        cols += ["lat", "lon"]
        with open(csv_path(job["id"]), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(measurer_for(cid).measure, f): (cid, f) for cid, f in items}
                for fut in as_completed(futs):
                    cid, f = futs[fut]
                    m = measurer_for(cid)
                    a = m.parcel_attrs(f["attributes"])
                    try:
                        r = fut.result()
                        row = dict(r)
                        row["pin"] = r.get("pid") or r["pin"]
                        row["county"] = m.c["name"]
                        row["driveway_method"] = r["sources"]["driveway_method"]
                        row["sidewalk_detected"] = r["sources"]["sidewalk_detected"]
                        row["quality"] = r["sources"].get("quality")
                        row["notes"] = r["sources"].get("note")
                        for k in job["measures"]:
                            job["totals"][k] += r.get(k) or 0
                    except Exception as e:
                        job["errors"] += 1
                        row = {"pin": counties.make_pid(cid, a.get("pin")), "county": m.c["name"],
                               "address": a.get("address"), "driveway_method": f"error: {e}"[:200]}
                    c = f["geometry"].representative_point()
                    row["lat"], row["lon"] = round(c.y, 6), round(c.x, 6)
                    if job["include_owner"]:
                        raw = f["attributes"]
                        mf = {**{"mail_address": "ADDRESS1", "mail_city": "OWNER_CITY", "mail_state": "OWNER_STAT",
                                 "mail_zip": "OWNER_ZIP"}, **{k: v for k, v in m.c["parcels"]["fields"].items()
                                                              if k.startswith("mail_")}}
                        row.update({"owner": a.get("owner"), **{k: raw.get(v) for k, v in mf.items()}})
                    w.writerow(row)
                    job["done"] += 1
                    if job["done"] % 25 == 0:
                        fh.flush()
                        _save(job)
        job["status"] = "done"
    except Exception as e:
        job["status"] = f"failed: {e}"
        traceback.print_exc()
    job["finished"] = time.time()
    _save(job)
    if on_done:
        try:
            on_done(job)
        except Exception:
            traceback.print_exc()
