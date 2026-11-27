"""Learning from people's manual fixes.

Every time someone corrects a drawing, it becomes training data:

* Lawn fixes (an area marked "not lawn" or "is lawn" on the photo). The
  pixels inside say what grass and paving really look like on this county's
  photos; the rest of that lot, which the person left alone, is taken as
  confirmed too. Once a county/photo source has enough fixes, measuring there
  uses a small per-county model of "grass vs paving" (colour, greenness,
  texture) and flips a pixel only when it's clearly on the other side.

* Roof outline edits (a company moving or reshaping the outline before
  building a report). When edits in a county keep shifting outlines the same
  way, new outlines there start out shifted by that amount.

Nothing here replaces an exact fix: a lot that was corrected by hand keeps
its own correction. Learning only makes the next homes better.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import numpy as np
from scipy import ndimage as ndi

from .pricing import DATA_DIR

DB = DATA_DIR / "learning.db"
SAMPLES_DIR = DATA_DIR / "learning"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()

MIN_FIXES = 3            # lawn fixes in a county/photo source before its model is used
MIN_PIXELS = 400         # per class
MAX_PIXELS = 30000       # kept per class (newest win)
FLIP_P = 0.92            # how sure the model must be to overrule the colour rules
MIN_ROOF_EDITS = 5


def _db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS fixes (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, county TEXT, source TEXT,
            pin TEXT, grass_px INTEGER, paved_px INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS roof_edits (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, county TEXT, pin TEXT,
            dx_ft REAL, dy_ft REAL, area_ratio REAL);
        """)


init()


def _key(county: str, source: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in f"{county}__{source}")[:120]
    return safe


# ------------------------------------------------------------- pixel features
def features(rgb: np.ndarray) -> np.ndarray:
    """Per-pixel features (H, W, 6): blue deficit, saturation, brightness,
    excess green, texture, red-green balance. Same ones the colour rules use."""
    sm = ndi.uniform_filter(rgb[..., :3].astype(np.float32), size=(3, 3, 1))
    r, g, b = sm[..., 0], sm[..., 1], sm[..., 2]
    mx, mn = sm.max(-1), sm.min(-1)
    sat = (mx - mn) / (mx + 1e-6)
    bdef = ((r + g) / 2 - b) / (mx + 1e-6)
    exg = (2 * g - r - b) / (sm.sum(-1) + 1e-6)
    lum = sm.mean(-1)
    m1 = ndi.uniform_filter(lum, 5)
    tex = np.sqrt(np.maximum(ndi.uniform_filter(lum * lum, 5) - m1 * m1, 0))
    rg = (r - g) / (mx + 1e-6)
    return np.stack([bdef, sat, mx, exg, tex, rg], -1).astype(np.float32)


# ---------------------------------------------------------- model per county
_models: dict = {}


def _paths(key):
    return SAMPLES_DIR / f"{key}.npz", SAMPLES_DIR / f"{key}.json"


def _load_samples(key):
    p, _ = _paths(key)
    if p.exists():
        z = np.load(p)
        return z["grass"], z["paved"]
    return np.zeros((0, 6), np.float32), np.zeros((0, 6), np.float32)


def _fit(grass: np.ndarray, paved: np.ndarray) -> dict | None:
    if len(grass) < MIN_PIXELS or len(paved) < MIN_PIXELS:
        return None
    out = {}
    for name, x in (("grass", grass), ("paved", paved)):
        out[name] = {"mean": x.mean(0).tolist(), "var": (x.var(0) + 1e-4).tolist(), "n": int(len(x))}
    return out


def add_pixels(county: str, source: str, grass: np.ndarray, paved: np.ndarray, kind: str, pin: str) -> dict:
    """Store labelled pixels from a fix and refit that county's model."""
    key = _key(county, source)
    with _lock:
        g0, p0 = _load_samples(key)
        g = np.concatenate([g0, grass.astype(np.float32)])[-MAX_PIXELS:]
        p = np.concatenate([p0, paved.astype(np.float32)])[-MAX_PIXELS:]
        npz, js = _paths(key)
        np.savez_compressed(npz, grass=g, paved=p)
        model = _fit(g, p)
        js.write_text(json.dumps({"county": county, "source": source, "model": model, "updated": time.time()}))
        _models.pop(key, None)
    with _db() as con:
        con.execute("INSERT INTO fixes (ts, kind, county, source, pin, grass_px, paved_px) VALUES (?,?,?,?,?,?,?)",
                    (time.time(), kind, county, source, pin, int(len(grass)), int(len(paved))))
    return {"grass": int(len(g)), "paved": int(len(p)), "ready": model is not None}


def _model(county: str, source: str):
    key = _key(county, source)
    if key in _models:
        return _models[key]
    _, js = _paths(key)
    m = None
    if js.exists():
        try:
            d = json.loads(js.read_text())
            with _db() as con:
                n = con.execute("SELECT COUNT(*) FROM fixes WHERE kind LIKE 'lawn%' AND county=? AND source=?",
                                (county, source)).fetchone()[0]
            if d.get("model") and n >= MIN_FIXES:
                m = {k: (np.array(v["mean"], np.float32), np.array(v["var"], np.float32)) for k, v in d["model"].items()}
        except Exception:
            m = None
    _models[key] = m
    return m


def adjust_hard(county: str, source: str, rgb: np.ndarray, hard: np.ndarray, area: np.ndarray) -> np.ndarray:
    """Paving mask after what people taught us for this county's photos:
    inside ``area`` (the lot), paving the model is sure is grass becomes
    grass, and grass it's sure is paving becomes paving."""
    m = _model(county, source)
    if m is None or not area.any():
        return hard
    f = features(rgb)[area]

    def loglik(cls):
        mu, var = m[cls]
        return -0.5 * (((f - mu) ** 2) / var + np.log(var)).sum(-1)
    lg, lp = loglik("grass"), loglik("paved")
    p_grass = 1.0 / (1.0 + np.exp(np.clip(lp - lg, -50, 50)))
    out = hard.copy()
    cur = hard[area]
    new = cur.copy()
    new[cur & (p_grass > FLIP_P)] = False
    new[~cur & (p_grass < 1 - FLIP_P)] = True
    out[area] = new
    # keep it tidy: no single-pixel speckle from the flips
    return ndi.binary_opening(out, iterations=1) | (hard & ~area)


# ----------------------------------------------------------------- lawn fixes
def learn_from_lawn_fix(pid: str, exclude: list, include: list) -> dict | None:
    """Re-run the measuring for this lot without its hand fix, and learn from
    the difference between what we drew and what the person drew."""
    from shapely.geometry import shape
    from . import counties
    from .measure import measurer_for
    cid, bare = counties.split_pid(pid)
    m = measurer_for(cid)
    feat = m.feature_by_pin(bare) if hasattr(m, "feature_by_pin") else None
    if feat is None:
        return None
    r = m.measure(feat, internals=True)
    it = r.pop("_internals", None)
    if not it:
        return None
    mos, lawn, yard = it["mos"], it["lawn"], it["yard"]
    F = features(mos.rgb)
    excl = np.zeros(lawn.shape, bool)
    incl = np.zeros(lawn.shape, bool)
    for g in exclude or []:
        excl |= mos.rasterize(shape(g))
    for g in include or []:
        incl |= mos.rasterize(shape(g))
    excl &= yard
    incl &= yard
    # what they said isn't grass, and what they said is
    paved = F[excl & lawn]
    grass = F[incl & ~lawn]
    # the rest of the lot they left alone counts as confirmed
    touched = ndi.binary_dilation(excl | incl, iterations=3)
    calm = yard & ~touched
    grass_ok = F[calm & lawn]
    paved_ok = F[calm & it["paved"]]
    rng = np.random.default_rng(0)
    pick = lambda a, n: a[rng.choice(len(a), min(n, len(a)), replace=False)] if len(a) else a
    grass_all = np.concatenate([grass, pick(grass_ok, max(len(paved), 600))])
    paved_all = np.concatenate([paved, pick(paved_ok, max(len(grass), 600))])
    kind = "lawn_exclude" if len(paved) >= len(grass) else "lawn_include"
    return add_pixels(cid, it["source"], grass_all, paved_all, kind, pid)


def learn_async(pid: str, exclude: list, include: list):
    def go():
        try:
            learn_from_lawn_fix(pid, exclude, include)
        except Exception:
            import traceback
            traceback.print_exc()
    threading.Thread(target=go, daemon=True, name="learn-fix").start()


# ----------------------------------------------------------------- roof edits
def record_roof_edit(county: str, pin: str, auto_gj: dict, edited_gj: dict):
    """How far and how much a roofer moved our outline (feet, local)."""
    from shapely.geometry import shape
    try:
        a, e = shape(auto_gj), shape(edited_gj)
        if a.is_empty or e.is_empty or a.area <= 0:
            return
        lat = a.centroid.y
        ft_x = 364567.0 * np.cos(np.radians(lat))
        ft_y = 364567.0
        dx = (e.centroid.x - a.centroid.x) * ft_x
        dy = (e.centroid.y - a.centroid.y) * ft_y
        if abs(dx) > 40 or abs(dy) > 40:  # a different building, not a nudge
            return
        sx, sy = roof_shift_ft(county)  # our outline was already shifted this much
        dx, dy = dx + sx, dy + sy
        with _db() as con:
            con.execute("INSERT INTO roof_edits (ts, county, pin, dx_ft, dy_ft, area_ratio) VALUES (?,?,?,?,?,?)",
                        (time.time(), county, pin, float(dx), float(dy), float(e.area / a.area)))
    except Exception:
        pass


def roof_shift_ft(county: str) -> tuple[float, float]:
    """A consistent nudge people keep making in this county (east, north ft):
    at least 5 edits, the same direction, and bigger than the scatter."""
    with _db() as con:
        rows = con.execute("SELECT dx_ft, dy_ft FROM roof_edits WHERE county=? ORDER BY id DESC LIMIT 50",
                           (county,)).fetchall()
    if len(rows) < MIN_ROOF_EDITS:
        return (0.0, 0.0)
    d = np.array([[r[0], r[1]] for r in rows])
    mean, sd = d.mean(0), d.std(0)
    out = []
    for k in range(2):
        out.append(float(mean[k]) if abs(mean[k]) >= 1.0 and abs(mean[k]) > sd[k] / np.sqrt(len(d)) * 2 else 0.0)
    return (round(out[0], 2), round(out[1], 2))


def summary() -> dict:
    with _db() as con:
        lawn = [dict(r) for r in con.execute(
            "SELECT county, source, COUNT(*) n, MAX(ts) last FROM fixes GROUP BY county, source ORDER BY n DESC")]
        roof = [dict(r) for r in con.execute(
            "SELECT county, COUNT(*) n, AVG(area_ratio) ratio, MAX(ts) last FROM roof_edits GROUP BY county ORDER BY n DESC")]
    for r in lawn:
        r["active"] = r["n"] >= MIN_FIXES and _model(r["county"], r["source"]) is not None
    for r in roof:
        r["shift_ft"] = roof_shift_ft(r["county"])
    return {"lawn": lawn, "roof": roof, "fixes": sum(r["n"] for r in lawn), "roof_edits": sum(r["n"] for r in roof)}
