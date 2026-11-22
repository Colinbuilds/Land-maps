"""Build a roof measurement report for a parcel's structures."""
from __future__ import annotations

import base64
import io
import math
import time

from shapely.geometry import mapping
from shapely.ops import transform as shp_transform, unary_union

from .imagery import Mosaic
from .measure import default_measurer
from .roof import LINE_LABELS, LINE_TYPES, analyze_roof, pitch_factors

DEFAULT_PITCH = 8.0
WASTE_STEPS = [0, 6, 7, 8, 9, 10, 11, 12]


def _to_las(m, g):
    return shp_transform(lambda x, y, z=None: m.lidar.to_las.transform(x, y), g)


def _to_wgs(m, g):
    return shp_transform(lambda x, y, z=None: m.lidar.from_las.transform(x, y), g)


def list_structures(pin: str) -> dict:
    """Roof outlines on a parcel. Footprints that touch are one structure
    (house + attached garage); detached buildings are separate."""
    m = default_measurer()
    f = m.get_parcel(pin)
    if not f:
        raise LookupError("parcel not found")
    P = f["geometry"]
    env = P.buffer(0.0003).bounds
    blds = []
    for b in m.buildings.query(env):
        g = b["geometry"]
        if g is None or g.is_empty:
            continue
        if g.intersection(P).area > 0.5 * g.area:
            blds.append(_to_las(m, g).buffer(0))
    merged = unary_union([b.buffer(0.75) for b in blds]).buffer(-0.75) if blds else None
    parts = [] if merged is None else list(getattr(merged, "geoms", [merged]))
    parts = [p for p in parts if p.area >= 60]
    parts.sort(key=lambda p: -p.area)
    out = []
    for i, p in enumerate(parts):
        out.append({
            "id": i,
            "name": "Main structure" if i == 0 else f"Structure {i + 1}",
            "plan_sqft": round(p.area),
            "geometry_las": p,
            "geojson": mapping(_to_wgs(m, p)),
        })
    a = f["attributes"]
    pf = m.pf
    return {"pin": a.get(pf["pin"]), "address": (a.get(pf["address"]) or "").strip(),
            "city": a.get(pf["city"]), "zip": a.get(pf["zip"]), "parcel": f, "structures": out,
            "stories": a.get("BLDG_STORY"), "year_built": a.get(pf["year_built"])}


def _snap(segs, outline, pitch, tol=4.0):
    """Join interior line ends that should meet (hip to ridge, valley to
    ridge) and pull them onto nearby roof corners, then re-measure."""
    import numpy as np
    fac = pitch_factors(pitch)
    inner = [s for s in segs if s["type"] not in ("eave", "rake")]
    corners = np.array([p for ring in outline for p in ring[:-1]]) if outline else np.zeros((0, 2))
    ends = [(i, k) for i in range(len(inner)) for k in ("a", "b")]
    pts = np.array([inner[i][k] for i, k in ends]) if ends else np.zeros((0, 2))
    # cluster interior endpoints
    group = list(range(len(pts)))
    for x in range(len(pts)):
        for y in range(x + 1, len(pts)):
            if np.hypot(*(pts[x] - pts[y])) < tol:
                gx, gy = group[x], group[y]
                group = [gx if g == gy else g for g in group]
    new = pts.copy()
    for g in set(group):
        idx = [i for i in range(len(pts)) if group[i] == g]
        c = pts[idx].mean(0)
        if len(corners):
            d = np.hypot(*(corners - c).T)
            if d.min() < tol * 1.6:
                c = corners[d.argmin()]
        new[idx] = c
    for (i, k), p in zip(ends, new):
        inner[i][k] = [round(float(p[0]), 2), round(float(p[1]), 2)]
    out = []
    for s_ in segs:
        L = float(np.hypot(s_["b"][0] - s_["a"][0], s_["b"][1] - s_["a"][1]))
        if s_["type"] not in ("eave", "rake"):
            s_["len"] = round(L * fac[s_["type"]], 1)
        if s_["len"] >= 1:
            out.append(s_)
    return out


def suggested_waste(totals: dict, area: float) -> int:
    """More hips and valleys means more cutting. Simple gable ~6%, cut-up hip ~12%."""
    if area <= 0:
        return 10
    cut = (totals.get("hip", 0) + totals.get("valley", 0)) / (area / 100.0)  # ft per square
    for lim, w in ((1.0, 6), (2.0, 7), (3.0, 8), (4.5, 9), (6.0, 10), (8.0, 11)):
        if cut < lim:
            return w
    return 12


def build_report(pin: str, structure_ids: list[int] | None = None, pitch: float = DEFAULT_PITCH) -> dict:
    m = default_measurer()
    info = list_structures(pin)
    structs = info["structures"]
    if not structs:
        raise LookupError("no buildings found on this parcel")
    chosen = [s for s in structs if structure_ids is None or s["id"] in structure_ids] or structs[:1]
    env = info["parcel"]["geometry"].buffer(0.0004).bounds

    def sampler(xs, ys):
        smp = m.lidar.sample(env, xs, ys)
        return None if smp is None else smp["obj"]

    out_structs = []
    totals = {k: 0.0 for k in LINE_TYPES}
    area = plan = 0.0
    facets = 0
    methods = set()
    for s in chosen:
        res = analyze_roof(s["geometry_las"], sampler, pitch)
        g = res.get("grid")
        segs = []
        for seg in res["segments"]:
            if "x0" in seg:
                a, b = (seg["x0"], seg["y0"]), (seg["x1"], seg["y1"])
            else:
                a, b = g.to_xy(seg["r0"], seg["c0"]), g.to_xy(seg["r1"], seg["c1"])
            segs.append({"type": seg["type"], "a": [round(float(a[0]), 2), round(float(a[1]), 2)],
                         "b": [round(float(b[0]), 2), round(float(b[1]), 2)], "len": round(float(seg["true_length"]), 1)})
        outline = [[[round(x, 2), round(y, 2)] for x, y in p.exterior.coords]
                   for p in getattr(s["geometry_las"], "geoms", [s["geometry_las"]])]
        if res["method"] == "lidar":
            segs = _snap(segs, outline, pitch)
            res["lines"] = {k: sum(x["len"] for x in segs if x["type"] == k) for k in LINE_TYPES}
        out_structs.append({
            "id": s["id"], "name": s["name"], "plan_sqft": round(res["plan_area_sqft"]),
            "roof_sqft": round(res["roof_area_sqft"]), "flat_sqft": round(res.get("flat_area_sqft") or 0),
            "facets": res.get("facet_count"), "method": res["method"],
            "lines": {k: round(float(v), 1) for k, v in res["lines"].items()},
            "segments": segs, "outline": outline,
        })
        for k in LINE_TYPES:
            totals[k] += res["lines"][k]
        area += res["roof_area_sqft"]
        plan += res["plan_area_sqft"]
        facets += res.get("facet_count") or 0
        methods.add(res["method"])

    sw = suggested_waste(totals, area)
    waste = [{"pct": w, "area": round(area * (1 + w / 100)),
              "squares": math.ceil(area * (1 + w / 100) / 100 * 3) / 3,  # bundles: 1/3 square
              "suggested": w == sw} for w in WASTE_STEPS]
    t = {k: round(float(v), 1) for k, v in totals.items()}
    derived = {
        "drip_edge": round(t["eave"] + t["rake"], 1),
        "starter": round(t["eave"] + t["rake"], 1),
        "ridge_cap": round(t["ridge"] + t["hip"], 1),
        "ice_water_valleys": round(t["valley"], 1),
    }
    return {
        "pin": info["pin"], "address": info["address"], "city": info["city"], "zip": info["zip"],
        "stories": info["stories"], "year_built": info["year_built"],
        "pitch": pitch, "pitch_label": f"{pitch:g}/12",
        "created": time.time(),
        "roof_sqft": round(area), "plan_sqft": round(plan), "squares": round(area / 100, 2),
        "facets": facets, "lines": t, "derived": derived,
        "waste": waste, "suggested_waste": sw,
        "structures": out_structs,
        "method": "lidar" if methods == {"lidar"} else ("partial" if "lidar" in methods else "outline_only"),
        "sources": {"lidar": "Douglas County 2022 QL1 LiDAR", "outlines": "Douglas County 2022 building footprints",
                    "imagery": "Douglas County 2025 orthoimagery"},
        "aerial_jpg": _aerial(m, chosen),
        "line_labels": LINE_LABELS,
    }


def _aerial(m, chosen) -> str:
    try:
        u = unary_union([s["geometry_las"] for s in chosen]).buffer(18)
        env = _to_wgs(m, u.envelope).bounds
        mos = Mosaic(m.c["imagery"]["tile_url"], env, 21)
        x0, y0 = mos.to_px(env[0], env[3])
        x1, y1 = mos.to_px(env[2], env[1])
        im = mos.image.crop((int(x0), int(y0), int(x1), int(y1)))
        im.thumbnail((1100, 1100))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""
