"""Bulk measurement of every parcel in a ZIP code, written to CSV."""
from __future__ import annotations

import csv
import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from .gis import query_features, count_features
from .measure import default_measurer, MEASUREMENTS
from .pricing import DATA_DIR

JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

CLASS_LABELS = {"R": "Residential", "C": "Commercial", "I": "Industrial", "A": "Agricultural",
                "E": "Exempt", "M": "Mixed", "F": "Other"}

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _where(zip_code: str, classes: list[str]) -> str:
    m = default_measurer()
    f = m.pf
    z = "".join(ch for ch in zip_code if ch.isdigit())[:5]
    w = f"{f['zip']} LIKE '{z}%'"
    cls = [c for c in classes if c.isalpha() and len(c) == 1]
    if cls:
        w += " AND " + f"{f['class']} IN (" + ",".join(f"'{c}'" for c in cls) + ")"
    return w


def preview(zip_code: str, classes: list[str]) -> dict:
    m = default_measurer()
    w = _where(zip_code, classes)
    return {"where": w, "count": count_features(m.c["parcels"]["url"], w)}


def start_job(zip_code: str, classes: list[str], measures: list[str], limit: int | None = None,
              include_owner: bool = False, workers: int = 6) -> dict:
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
    threading.Thread(target=_run, args=(job, workers), daemon=True).start()
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


def csv_path(job_id: str):
    return JOBS_DIR / f"{job_id}.csv"


def _save(job):
    (JOBS_DIR / f"{job['id']}.json").write_text(json.dumps(job))


def _run(job: dict, workers: int):
    m = default_measurer()
    try:
        job["status"] = "loading parcels"
        feats = query_features(m.c["parcels"]["url"], where=_where(job["zip"], job["classes"]),
                               limit=job["limit"] or None)
        # Process in spatial order so neighbouring lots share cached downloads.
        def cell(f):
            c = f["geometry"].centroid
            return (round(c.y / 0.004), round(c.x / 0.004), c.x)
        feats = [f for f in feats if f["geometry"] is not None]
        feats.sort(key=cell)
        job["total"] = len(feats)
        job["status"] = "measuring"
        _save(job)

        cols = ["pin", "address", "city", "zip", "class"] + job["measures"] + ["driveway_method", "sidewalk_detected"]
        if job["include_owner"]:
            cols += ["owner", "mail_address", "mail_city", "mail_state", "mail_zip"]
        cols += ["lat", "lon"]
        with open(csv_path(job["id"]), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(m.measure, f): f for f in feats}
                for fut in as_completed(futs):
                    f = futs[fut]
                    a = f["attributes"]
                    try:
                        r = fut.result()
                        row = dict(r)
                        row["driveway_method"] = r["sources"]["driveway_method"]
                        row["sidewalk_detected"] = r["sources"]["sidewalk_detected"]
                        for k in job["measures"]:
                            job["totals"][k] += r.get(k) or 0
                    except Exception as e:
                        job["errors"] += 1
                        row = {"pin": a.get(m.pf["pin"]), "address": a.get(m.pf["address"]),
                               "driveway_method": f"error: {e}"[:200]}
                    c = f["geometry"].representative_point()
                    row["lat"], row["lon"] = round(c.y, 6), round(c.x, 6)
                    if job["include_owner"]:
                        row.update({"owner": a.get("OWNER_NAME"), "mail_address": a.get("ADDRESS1"),
                                    "mail_city": a.get("OWNER_CITY"), "mail_state": a.get("OWNER_STAT"),
                                    "mail_zip": a.get("OWNER_ZIP")})
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
