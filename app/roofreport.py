"""Build a roof measurement report for a parcel's structures."""
from __future__ import annotations

import base64
import io
import math
import time

from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform, unary_union

from .imagery import Mosaic
from .counties import make_pid, split_pid
from .measure import measurer_for
from .roof import analyze_roof, pitch_factors
from .roofplanes import LINE_TYPES, analyze_roof_points

LINE_LABELS = {
    "ridge": "Ridges", "hip": "Hips", "valley": "Valleys", "rake": "Rakes", "eave": "Eaves",
    "step_flashing": "Step flashing", "flashing": "Flashing",
}

DEFAULT_PITCH = 8.0
WASTE_STEPS = [0, 6, 7, 8, 9, 10, 11, 12]


def _to_las(m, g):
    return shp_transform(lambda x, y, z=None: m.lidar.to_las.transform(x, y), g)


def _to_wgs(m, g):
    return shp_transform(lambda x, y, z=None: m.lidar.from_las.transform(x, y), g)


_struct_cache: dict = {}


def _lean_ft(m, g_wgs):
    """(east, north) feet the roof appears shifted in the photo."""
    v = _lean_vector(m, g_wgs)
    if not v:
        return (0.0, 0.0)
    h = _eave_height(_neighbors_with_heights(m, g_wgs, radius_deg=0.0002, n=6), g_wgs)
    return (v[0] * h, v[1] * h) if h else (0.0, 0.0)


def _shift_las_to_photo_wgs(m, p_las, lean):
    from shapely.affinity import translate
    return _to_wgs(m, translate(p_las, lean[0], lean[1]))


def list_structures(pin: str) -> dict:
    """Roof outlines on a parcel, traced on the aerial photo.

    Footprints that touch are one structure (house + attached garage);
    detached buildings are separate. Each outline starts from the county
    footprint and is moved onto the roof edges visible in the photo, with the
    LiDAR making sure no real roof is cut off. ``geometry_las`` is the traced
    outline at its true position (for measuring); ``geojson`` is the same
    outline where it shows in the photo (for drawing and editing)."""
    import time as _t
    hit = _struct_cache.get(pin)
    if hit and _t.time() - hit[0] < 3600:
        return hit[1]
    import numpy as np
    from .photoedge import PhotoSampler, trace_outline
    cid, bare = split_pid(pin)
    m = measurer_for(cid)
    f = m.get_parcel(bare)
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

    def is_roof(xs, ys):
        r = m.lidar.sample(env, np.asarray(xs, float), np.asarray(ys, float))
        return r["obj"] > 7.5 if r is not None else np.zeros(len(xs), bool)

    from .trees import tree_grid

    out = []
    for i, p in enumerate(parts):
        g = _to_wgs(m, p)
        lean = (0.0, 0.0)
        traced, stats = p.simplify(0.6, preserve_topology=True), {}
        trees = None
        try:
            trees = tree_grid(m.lidar, env, p.buffer(8))
        except Exception:
            trees = None
        try:
            lean = _lean_ft(m, g)
            traced, stats = trace_outline(PhotoSampler(m, p, lean), p, is_roof,
                                          trees.at if trees is not None and trees.mask.any() else None)
        except Exception:
            pass
        out.append({
            "id": i,
            "name": "Main structure" if i == 0 else f"Structure {i + 1}",
            "plan_sqft": round(traced.area),
            "county_plan_sqft": round(p.area),
            "geometry_las": traced,
            "lean_ft": [round(lean[0], 2), round(lean[1], 2)],
            "trace": stats,
            "geojson": mapping(_shift_las_to_photo_wgs(m, traced, lean)),
        })
    a = m.parcel_attrs(f["attributes"])
    info = {"pin": make_pid(cid, a["pin"]), "address": a.get("address") or "",
            "city": a.get("city"), "zip": a.get("zip"), "parcel": f, "structures": out, "county": cid,
            "county_name": m.c["name"],
            "stories": f["attributes"].get("BLDG_STORY"), "year_built": a.get("year_built")}
    _struct_cache[pin] = (_t.time(), info)
    if len(_struct_cache) > 300:
        _struct_cache.pop(next(iter(_struct_cache)))
    return info


def outline_from_photo_geojson(m, gj: dict, lean) -> "Polygon":
    """An outline the user drew/edited on the photo -> true position (LAS)."""
    from shapely.affinity import translate
    g = shape(gj).buffer(0)
    if g.is_empty or g.area <= 0:
        raise ValueError("empty outline")
    return translate(_to_las(m, g), -lean[0], -lean[1]).buffer(0)


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


def _contrast_offset(rgb, outline_wgs, mos, lim):
    """Best pixel shift for one outline: maximise the colour difference
    between just inside and just outside the outline, all the way round."""
    import numpy as np
    from PIL import Image as _I, ImageDraw as _D
    from scipy import ndimage as ndi
    h, w = rgb.shape[:2]
    im = _I.new("L", (w, h), 0)
    d = _D.Draw(im)
    for p in getattr(outline_wgs, "geoms", [outline_wgs]):
        d.polygon([mos.to_px(*c[:2]) for c in p.exterior.coords], fill=1)
    mask = np.asarray(im) > 0
    if mask.sum() < 200:
        return None
    sd = ndi.distance_transform_edt(mask) - ndi.distance_transform_edt(~mask)
    edge = np.abs(sd) <= 1.0
    ys, xs = np.nonzero(edge)
    if len(xs) < 20:
        return None
    ny, nx = np.gradient(ndi.gaussian_filter(sd, 1.5))
    L = np.hypot(nx, ny) + 1e-6
    nx, ny = nx[ys, xs] / L[ys, xs], ny[ys, xs] / L[ys, xs]      # points inward
    k = 3
    ix_in, iy_in = np.round(xs + nx * k).astype(int), np.round(ys + ny * k).astype(int)
    ix_out, iy_out = np.round(xs - nx * k).astype(int), np.round(ys - ny * k).astype(int)

    def score(dx, dy):
        a_x, a_y, b_x, b_y = ix_in + dx, iy_in + dy, ix_out + dx, iy_out + dy
        ok = (a_x >= 0) & (a_x < w) & (a_y >= 0) & (a_y < h) & (b_x >= 0) & (b_x < w) & (b_y >= 0) & (b_y < h)
        if ok.mean() < 0.9:
            return 0.0
        diff = np.abs(rgb[a_y[ok], a_x[ok]] - rgb[b_y[ok], b_x[ok]]).sum(-1)
        return float(np.median(diff))
    base = score(0, 0)
    best, bd = base, (0, 0)
    for dy in range(-lim, lim + 1, 2):
        for dx in range(-lim, lim + 1, 2):
            sc = score(dx, dy)
            if sc > best:
                best, bd = sc, (dx, dy)
    cx, cy = bd
    for dy in range(cy - 2, cy + 3):
        for dx in range(cx - 2, cx + 3):
            sc = score(dx, dy)
            if sc > best:
                best, bd = sc, (dx, dy)
    return bd, best, base


_LEAN_CELL = 0.003  # deg; one photo frame covers several of these
_lean_mem: dict = {}


def _lean_vector(m, center_wgs):
    """Photo lean for this neighbourhood: ground feet of shift (east, north)
    per foot of building height, measured on nearby houses. Cached."""
    import json as _json
    import numpy as np
    from .gis import CACHE_DIR
    c = center_wgs.centroid
    key = (round(c.x / _LEAN_CELL), round(c.y / _LEAN_CELL))
    if key in _lean_mem:
        return _lean_mem[key]
    path = CACHE_DIR / "lean" / f"{key[0]}_{key[1]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        v = _json.loads(path.read_text())
        _lean_mem[key] = v
        return v
    neigh = _neighbors_with_heights(m, center_wgs, n=10)
    v = None
    if neigh:
        u = unary_union([n[0] for n in neigh])
        mos = Mosaic(m.c["imagery"]["tile_url"], u.buffer(0.00008).bounds, 19)
        rgb = np.asarray(mos.image, dtype=np.float32)
        lim = int(14 / mos.ft_per_px)
        per_ft = []
        for g, hgt in neigh:
            if not hgt or hgt < 6:
                continue
            r = _contrast_offset(rgb, g, mos, lim)
            if r and r[1] > r[2] * 1.1:
                # pixels -> ground feet (east, north)
                per_ft.append((r[0][0] * mos.ft_per_px / hgt, -r[0][1] * mos.ft_per_px / hgt))
        if len(per_ft) >= 3:
            v = [float(x) for x in np.median(np.array(per_ft), axis=0)]
    _lean_mem[key] = v
    path.write_text(_json.dumps(v))
    return v


def _neighbors_with_heights(m, center_wgs, radius_deg=0.0009, n=15):
    """Nearby building outlines with their eave/roof heights in feet, from
    the county's (or national) building layer."""
    from .gis import query_features
    b = m.c["buildings"]
    hf = b.get("height_field")
    oid = b.get("oid", "objectid")
    to_ft = 3.28084 if b.get("height_units") == "m" else 1.0
    c = center_wgs.centroid
    env = (c.x - radius_deg, c.y - radius_deg * 0.75, c.x + radius_deg, c.y + radius_deg * 0.75)
    feats = query_features(b["url"], envelope=env, out_fields=",".join(x for x in (oid, hf) if x))
    out = []
    for f in feats:
        g = f["geometry"]
        if g is None or g.area <= 0:
            continue
        h = f["attributes"].get(hf) if hf else None
        try:
            h = float(h) * to_ft if h else None
        except (TypeError, ValueError):
            h = None
        # national layers give roof height; eaves sit lower on pitched roofs
        if h and b.get("height_units") == "m":
            h *= 0.8
        out.append((g, h if h and h > 0 else 18.0))
    out.sort(key=lambda t: -t[0].area)
    return out[:n]


def _eave_height(neigh, g):
    hs = [h for ng, h in neigh if h and ng.intersects(g) and ng.intersection(g).area > 0.3 * ng.area]
    return max(hs) if hs else None


def lean_corrected(m, g_wgs):
    """``g_wgs`` moved to where the roof shows in the photo (drawing only)."""
    import math as _m
    from shapely.affinity import translate
    v = _lean_vector(m, g_wgs)
    if not v:
        return g_wgs
    neigh = _neighbors_with_heights(m, g_wgs, radius_deg=0.0002, n=6)
    h = _eave_height(neigh, g_wgs)
    if not h:
        return g_wgs
    dx_ft, dy_ft = v[0] * h, v[1] * h
    lat = g_wgs.centroid.y
    return translate(g_wgs, dx_ft * 0.3048 / (111320 * _m.cos(_m.radians(lat))), dy_ft * 0.3048 / 110574)


def suggested_waste(totals: dict, area: float) -> int:
    """More hips and valleys means more cutting. Simple gable ~6%, cut-up hip ~12%."""
    if area <= 0:
        return 10
    cut = (totals.get("hip", 0) + totals.get("valley", 0)) / (area / 100.0)  # ft per square
    for lim, w in ((1.0, 6), (2.0, 7), (3.0, 8), (4.5, 9), (6.0, 10), (8.0, 11)):
        if cut < lim:
            return w
    return 12


def build_report(pin: str, structure_ids: list[int] | None = None, pitch: float = DEFAULT_PITCH,
                 edited: dict | None = None) -> dict:
    """``edited`` maps structure id -> GeoJSON outline the roofer adjusted on
    the photo; those outlines are measured exactly as drawn."""
    m = measurer_for(split_pid(pin)[0])
    info = list_structures(pin)
    structs = info["structures"]
    if not structs:
        raise LookupError("no buildings found on this parcel")
    chosen = [dict(s) for s in structs if structure_ids is None or s["id"] in structure_ids] or [dict(structs[0])]
    for s in chosen:
        gj = (edited or {}).get(str(s["id"])) or (edited or {}).get(s["id"])
        if gj:
            s["geometry_las"] = outline_from_photo_geojson(m, gj, s.get("lean_ft") or (0, 0))
            s["geojson"] = gj
            s["edited"] = True
    env = info["parcel"]["geometry"].buffer(0.0004).bounds

    def sampler(xs, ys):
        smp = m.lidar.sample(env, xs, ys)
        return None if smp is None else smp["obj"]

    from .trees import tree_grid

    def tree_cover(s, pts=None):
        """Roof hidden under tree canopy: plan polygon (LAS ft) or None."""
        try:
            tg = tree_grid(m.lidar, env, s["geometry_las"].buffer(2), roof_pts=pts)
            g = tg.polygon(clip=s["geometry_las"]) if tg is not None else None
            return g if g is not None and g.area >= 15 else None
        except Exception:
            return None

    def add_trees(st, cover, roof_sqft, plan_sqft):
        st["tree_overhang_sqft"] = 0
        st["tree_cover"] = []
        if cover is None:
            return 0.0
        hidden = cover.area * (roof_sqft / plan_sqft if plan_sqft else 1.0)
        st["tree_overhang_sqft"] = round(hidden)
        st["tree_cover"] = [[[round(x, 2), round(y, 2)] for x, y in p.exterior.coords]
                            for p in getattr(cover, "geoms", [cover]) if p.geom_type == "Polygon"]
        near = cover.buffer(1.5)
        from shapely.geometry import LineString
        for g in st["segments"]:
            ln = LineString([g["a"], g["b"]])
            g["tree"] = bool(ln.length and ln.intersection(near).length >= 0.5 * ln.length)
        return hidden

    out_structs = []
    tree_total = 0.0
    totals = {k: 0.0 for k in LINE_TYPES}
    area = plan = 0.0
    facets = 0
    methods = set()
    pitch_areas: dict = {}
    for s in chosen:
        # Preferred: fit real roof planes to the raw LiDAR points (measured
        # pitch per plane). Fallback: the older 1-ft grid method at a fixed pitch.
        res = None
        try:
            pts = m.lidar.building_points(env, s["geometry_las"].buffer(3).bounds)
            r2 = analyze_roof_points(s["geometry_las"], pts)
            if r2.get("method") == "lidar_planes":
                res = r2
        except Exception:
            res = None
        if res is not None:
            segs = [{"type": x["type"], "a": [round(x["a"][0], 2), round(x["a"][1], 2)],
                     "b": [round(x["b"][0], 2), round(x["b"][1], 2)], "len": round(x["len"], 1)}
                    for x in res["segments"]]
            outline = [[[round(x, 2), round(y, 2)] for x, y in p.exterior.coords]
                       for p in getattr(s["geometry_las"], "geoms", [s["geometry_las"]])]
            for k, v in res["pitch_areas"].items():
                pitch_areas[k] = pitch_areas.get(k, 0) + v
            out_structs.append({
                "id": s["id"], "name": s["name"], "edited": bool(s.get("edited")),
                "plan_sqft": round(res["plan_area_sqft"]), "roof_sqft": round(res["roof_area_sqft"]),
                "facets": res["facet_count"], "method": "lidar",
                "predominant_pitch": res["predominant_pitch"],
                "lines": {k: round(float(v), 1) for k, v in res["lines"].items()},
                "segments": segs, "outline": outline,
                "planes": [{"pitch": q["pitch"], "plan_sqft": round(q["plan_sqft"], 1),
                            "area_sqft": round(q["area_sqft"], 1)} for q in res["planes"]],
            })
            tree_total += add_trees(out_structs[-1], tree_cover(s, pts), res["roof_area_sqft"], res["plan_area_sqft"])
            for k in LINE_TYPES:
                totals[k] += res["lines"].get(k, 0.0)
            area += res["roof_area_sqft"]
            plan += res["plan_area_sqft"]
            facets += res["facet_count"]
            methods.add("lidar")
            continue
        res = analyze_roof(s["geometry_las"], sampler, pitch)
        res["lines"].setdefault("flashing", 0.0)
        pitch_areas[int(pitch)] = pitch_areas.get(int(pitch), 0) + res["roof_area_sqft"]
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
        methods.add("outline_only" if res["method"] != "lidar" else "lidar_grid")
        out_structs.append({
            "id": s["id"], "name": s["name"], "edited": bool(s.get("edited")), "plan_sqft": round(res["plan_area_sqft"]),
            "roof_sqft": round(res["roof_area_sqft"]), "flat_sqft": round(res.get("flat_area_sqft") or 0),
            "facets": res.get("facet_count"), "method": res["method"],
            "lines": {k: round(float(v), 1) for k, v in res["lines"].items()},
            "segments": segs, "outline": outline,
        })
        tree_total += add_trees(out_structs[-1], tree_cover(s), res["roof_area_sqft"], res["plan_area_sqft"])
        for k in LINE_TYPES:
            totals[k] += res["lines"][k]
        area += res["roof_area_sqft"]
        plan += res["plan_area_sqft"]
        facets += res.get("facet_count") or 0

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
    pitch_table = [{"pitch": f"{k}/12" if k else "Flat", "area": round(v), "pct": float(round(100 * v / area, 1)) if area else 0.0}
                   for k, v in sorted(pitch_areas.items())]
    pred = max(pitch_areas, key=pitch_areas.get) if pitch_areas else pitch
    return {
        "pin": info["pin"], "address": info["address"], "city": info["city"], "zip": info["zip"],
        "county": info.get("county"), "county_name": info.get("county_name"),
        "stories": info["stories"], "year_built": info["year_built"],
        "pitch": pred, "pitch_label": f"{pred:g}/12" if pred else "Flat", "pitch_table": pitch_table,
        "pitch_measured": "lidar" in methods,
        "created": time.time(),
        "roof_sqft": round(area), "plan_sqft": round(plan), "squares": round(area / 100, 2),
        "facets": facets, "lines": t, "derived": derived,
        "waste": waste, "suggested_waste": sw,
        "tree_overhang_sqft": round(tree_total),
        "tree_overhang_pct": round(100 * tree_total / area, 1) if area else 0.0,
        "tree_note": (f"About {round(tree_total):,} sq ft ({100 * tree_total / area:.0f}%) of the roof is under tree "
                      "canopy (hatched on the diagram). Planes there are measured from the scan points that reach "
                      "the roof and the surrounding planes; lines marked as under trees should be checked on site."
                      if area and tree_total >= 25 else ""),
        "structures": out_structs,
        "method": "lidar" if methods == {"lidar"} else ("partial" if methods & {"lidar", "lidar_grid"} else "outline_only"),
        "sources": {"lidar": (m.c.get("lidar") or {}).get("source"), "outlines": m.c["buildings"].get("source"),
                    "imagery": m.c["imagery"].get("source"),
                    "lidar_year": (m.c.get("lidar") or {}).get("year")},
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
        im = mos.image.copy()
        from PIL import ImageDraw
        d = ImageDraw.Draw(im)
        for s in chosen:
            g = shape(s["geojson"])
            for p in getattr(g, "geoms", [g]):
                d.line([mos.to_px(*c[:2]) for c in p.exterior.coords], fill=(0, 229, 255), width=3)
        im = im.crop((int(x0), int(y0), int(x1), int(y1)))
        im.thumbnail((1100, 1100))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""
