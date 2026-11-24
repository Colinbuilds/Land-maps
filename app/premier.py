"""Premier lists: a full property report PDF for every home in a ZIP.

Built once per ZIP (and property type) in the background and kept, like
the standard sheets, so later buyers download it straight away. The
download is one .zip "folder": one PDF per home named by its address,
plus index.csv with the headline numbers and the file name for each.
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

from .pricing import DATA_DIR, load_settings

PREMIER_DIR = DATA_DIR / "premier"
PREMIER_DIR.mkdir(parents=True, exist_ok=True)
WORKERS = 4
MAX_AGE_DAYS = 365

_lock = threading.Lock()
_building: dict = {}

INDEX_COLS = [("address", "Address"), ("city", "City"), ("zip", "ZIP"), ("owner", "Owner name"),
              ("owner_mailing", "Owner mailing address"), ("file", "Report file"),
              ("roof_sqft", "Roof sq ft"), ("squares", "Squares"), ("pitch", "Predominant pitch"),
              ("wall_sqft", "Wall area sq ft"), ("gutter_ft", "Gutters ft"),
              ("lawn_sqft", "Lawn sq ft"), ("driveway_sqft", "Driveway sq ft"), ("sidewalk_sqft", "Sidewalk sq ft"),
              ("lot_sqft", "Lot sq ft"), ("pin", "Property id"), ("status", "Status")]


def _key(zip_code: str, classes: list[str], bands=None) -> str:
    z = "".join(ch for ch in zip_code if ch.isdigit())[:5]
    k = f"{z}_{''.join(sorted(set(classes))) or 'ALL'}"
    if bands is not None:
        from .datastore import band_range
        from .bulk import VALUE_EDGES
        lo, hi = band_range(*bands)
        if (lo, hi) != (0, len(VALUE_EDGES)):
            k += f"_v{lo}-{hi}"
    return k


def path(zip_code, classes, bands=None):
    return PREMIER_DIR / f"{_key(zip_code, classes, bands)}.zip"


def meta(zip_code, classes, bands=None) -> dict:
    p = PREMIER_DIR / f"{_key(zip_code, classes, bands)}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _write_meta(zip_code, classes, m: dict, bands=None):
    p = PREMIER_DIR / f"{_key(zip_code, classes, bands)}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m))
    tmp.replace(p)


def ready(zip_code, classes, bands=None) -> bool:
    m = meta(zip_code, classes, bands)
    return bool(m.get("status") == "ready" and path(zip_code, classes, bands).exists()
                and time.time() - m.get("finished", 0) < MAX_AGE_DAYS * 86400)


def progress(zip_code, classes, bands=None) -> dict:
    m = meta(zip_code, classes, bands)
    return {"status": m.get("status", "none"), "done": m.get("done", 0), "total": m.get("total", 0)}


def _safe(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 .#-]+", "", name).strip()
    return re.sub(r"\s+", " ", s)[:80] or "property"


def _company() -> dict:
    b = load_settings().get("business") or {}
    return {"name": b.get("name") or "Property Report", "phone": b.get("phone") or "",
            "email": b.get("email") or "", "brand_color": b.get("accent") or "#1d4ed8"}


def slim(d: dict) -> dict:
    """The parts of a report the trade numbers and postcards need (kept per
    home in the folder so buyers' mail files build without re-measuring)."""
    out = {k: d.get(k) for k in ("pin", "address", "city", "zip", "county_name", "roof_sqft", "squares",
                                 "pitch_label", "facets", "lines", "tree_overhang_sqft", "walls", "yard")}
    if out.get("walls"):
        out["walls"] = {k: v for k, v in out["walls"].items() if k != "elevations"}
    if out.get("yard"):
        out["yard"] = {k: v for k, v in out["yard"].items() if k != "photo_jpg"}
    out["structures"] = [{"facet_polys": [{k: fp.get(k) for k in ("down", "pitch", "area_sqft")}
                                          for fp in st.get("facet_polys") or []]} for st in d.get("structures") or []]
    return out


def _one(cid, f):
    """Build one home's report and PDF, plus its mail photo. Returns
    (row, pdf_bytes|None, extras: {"photo": jpg, "data": slim report})."""
    from .counties import make_pid
    from .measure import measurer_for
    from .roofpdf import render_pdf
    from .roofreport import build_report, list_structures
    m = measurer_for(cid)
    a = m.parcel_attrs(f["attributes"])
    from .bulk import owner_info
    row = {"address": (a.get("address") or "").strip(), "city": a.get("city"), "zip": a.get("zip"),
           "pin": make_pid(cid, a["pin"]), "status": "ok", **owner_info(m, f)}
    try:
        info = list_structures(row["pin"])
        ids = [s["id"] for s in info["structures"] if s["id"] == 0 or s["plan_sqft"] >= 250]
        d = build_report(row["pin"], ids or None)
        rep = {"data": d, "id": a["pin"][-8:], "created": time.time()}
        pdf = render_pdf(rep, _company())
        extras = {"data": {**slim(d), "owner": row.get("owner") or "", "owner_mailing": row.get("owner_mailing") or ""}}
        try:
            from .postcard import PH, PHOTO_FRAC, PW, property_photo
            extras["photo"] = property_photo(d, None, aspect=PW * PHOTO_FRAC / PH, min_px=1500)
        except Exception:
            pass
        w, y = d.get("walls") or {}, d.get("yard") or {}
        row.update({"roof_sqft": d.get("roof_sqft"), "squares": d.get("squares"), "pitch": d.get("pitch_label"),
                    "wall_sqft": w.get("gross_wall_sqft"), "gutter_ft": w.get("gutter_ft"),
                    "lawn_sqft": y.get("lawn_sqft"), "driveway_sqft": y.get("driveway_sqft"),
                    "sidewalk_sqft": y.get("sidewalk_sqft"), "lot_sqft": y.get("lot_sqft")})
        return row, pdf, extras
    except Exception as e:
        row["status"] = f"not measured ({str(e)[:60]})"
        return row, None, {}


def add_home(zf, row: dict, pdf, extras: dict, used: set) -> None:
    """Write one home into a Premier folder: its report PDF, and for mail
    files its photo and slim data."""
    if not pdf:
        return
    base = _safe(f"{row['address']} {row.get('city') or ''}")
    name, k = f"{base}.pdf", 2
    while name in used:
        name, k = f"{base} ({k}).pdf", k + 1
    used.add(name)
    row["file"] = name
    stem = name[:-4]
    zf.writestr(f"reports/{name}", pdf)
    if extras.get("photo"):
        zf.writestr(f"mail/photos/{stem}.jpg", extras["photo"])
        row["photo"] = f"{stem}.jpg"
    if extras.get("data"):
        zf.writestr(f"mail/data/{stem}.json", json.dumps(extras["data"]), compress_type=zipfile.ZIP_DEFLATED)


def build(zip_code: str, classes: list[str], bands=None) -> None:
    """Measure every home in the ZIP (in the value bands, if given) and write
    the folder. Blocking."""
    from .bulk import _zip_parcels, band_of
    from .measure import measurer_for
    items = _zip_parcels(zip_code, classes)
    items = [it for it in items if (measurer_for(it[0]).parcel_attrs(it[1]["attributes"]).get("address") or "").strip()]
    if bands is not None:
        from .datastore import band_range
        lo, hi = band_range(*bands)
        items = [it for it in items
                 if lo <= band_of(measurer_for(it[0]).parcel_attrs(it[1]["attributes"]).get("home_value")) <= hi]
    items.sort(key=lambda it: measurer_for(it[0]).parcel_attrs(it[1]["attributes"]).get("address") or "")
    total = len(items)
    m = {"status": "building", "started": time.time(), "done": 0, "total": total}
    _write_meta(zip_code, classes, m, bands)
    final = path(zip_code, classes, bands)
    tmp = final.with_suffix(".building")
    rows, used = [], set()
    zlock = threading.Lock()
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for i, (row, pdf, extras) in enumerate(ex.map(lambda it: _one(*it), items), 1):
                with zlock:
                    add_home(zf, row, pdf, extras, used)
                rows.append(row)
                if i % 10 == 0 or i == total:
                    m.update(done=i)
                    _write_meta(zip_code, classes, m, bands)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([label for _, label in INDEX_COLS])
        for r in rows:
            w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in INDEX_COLS])
        zf.writestr("index.csv", buf.getvalue())
    tmp.replace(final)
    m.update(status="ready", finished=time.time(), done=total,
             reports=sum(1 for r in rows if r.get("file")), size=final.stat().st_size)
    _write_meta(zip_code, classes, m, bands)


def ensure(zip_code: str, classes: list[str], on_ready=None, bands=None) -> str:
    """Start building unless ready or running. Returns 'ready' or 'building'."""
    key = _key(zip_code, classes, bands)
    if ready(zip_code, classes, bands):
        if on_ready:
            on_ready()
        return "ready"
    with _lock:
        if key in _building:
            if on_ready:
                _building[key].append(on_ready)
            return "building"
        _building[key] = [on_ready] if on_ready else []

    def run():
        ok = True
        try:
            build(zip_code, classes, bands)
        except Exception as e:
            ok = False
            _write_meta(zip_code, classes, {"status": "failed", "error": str(e)[:300], "finished": time.time()}, bands)
            from . import mailer
            mailer.notify_owner(f"Premier list for ZIP {zip_code} failed", f"{e}\nRe-run it from Business > Data sales.")
        with _lock:
            cbs = _building.pop(key, [])
        if ok:
            for cb in cbs:
                try:
                    cb()
                except Exception:
                    pass
    threading.Thread(target=run, daemon=True, name=f"premier-{key}").start()
    return "building"
