"""Build a roof measurement report for a parcel's structures."""
from __future__ import annotations

import base64
import io
import math
import os
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
        try:
            from .roofclean import fit_hidden_edges, orthogonalize
            traced = orthogonalize(traced)
            if trees is not None and trees.mask.any():
                # walls under trees: from where the scan's roof points stop
                bp = m.lidar.building_points(env, traced.buffer(8).bounds)
                traced, n_hidden = fit_hidden_edges(traced, bp, trees.at)
                stats["lidar_edges"] = n_hidden
            if os.environ.get("ROOF_CLAMP", "1") == "1":
                # no wall further out than the roof the scan sees (+ drip edge)
                bp = m.lidar.building_points(env, traced.buffer(8).bounds)
                traced, n_clamp = fit_hidden_edges(traced, bp, lambda xs, ys: np.ones(np.shape(xs), bool),
                                                   inward_only=True)
                stats["clamped_edges"] = n_clamp
                if n_clamp:
                    traced = orthogonalize(traced)  # re-join walls that now line up
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

    def tree_cover(s, pts=None, used=None):
        """Roof hidden under tree canopy: plan polygon (LAS ft) or None."""
        try:
            roof = used if used is not None else s["geometry_las"]
            tg = tree_grid(m.lidar, env, roof.buffer(2), roof_pts=pts)
            g = tg.polygon(clip=roof) if tg is not None else None
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
            used = res.get("outline") or s["geometry_las"]  # after any low-sliver trim
            outline = [[[round(x, 2), round(y, 2)] for x, y in p.exterior.coords]
                       for p in getattr(used, "geoms", [used])]
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
                "facet_polys": _facet_polys(res, used),
                "_model": (res["planes"], res.get("label_grid"), res.get("grid_x"), res.get("grid_y")),
            })
            tree_total += add_trees(out_structs[-1], tree_cover(s, pts, used), res["roof_area_sqft"], res["plan_area_sqft"])
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

    walls = _walls_total(m, env, out_structs)
    yard = _yard(m, info["parcel"])
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
        "walls": walls,
        "yard": yard,
    }


def _facet_polys(res, used) -> list:
    """Roof facets as polygons (LAS ft), lettered smallest to largest like a
    measurement report: label, pitch, sloped area, downslope direction."""
    import numpy as np
    from shapely.geometry import box as _box
    L, xs, ys = res.get("label_grid"), res.get("grid_x"), res.get("grid_y")
    if L is None:
        return []
    from scipy import ndimage as ndi
    g = float(abs(xs[1] - xs[0])) if len(xs) > 1 else 0.5
    L = L.copy()
    by_id = {p["id"]: p for p in res["planes"]}
    # scraps (wall / fascia hits, < 50 sq ft sloped) join the facet next to them
    keep = [p["id"] for p in res["planes"] if p["area_sqft"] >= 50]
    if keep:
        big = np.isin(L, keep)
        scrap = (L >= 0) & ~big
        if scrap.any() and big.any():
            idx = ndi.distance_transform_edt(~big, return_distances=False, return_indices=True)
            L = np.where(scrap, L[tuple(idx)], L)
    planes = [by_id[k] for k in keep] or list(res["planes"])
    cells = {p["id"]: int((L == p["id"]).sum()) for p in planes}
    raw = {k: c * g * g * (1 + by_id[k]["slope"] ** 2) ** 0.5 for k, c in cells.items()}
    scale = float(res["roof_area_sqft"]) / max(1e-6, sum(raw.values()))
    faces = _faces_from_lines(res, used, L, xs, ys)
    taken = unary_union([f for f, _ in faces]) if faces else None
    claimed = {lab for _, lab in faces} if faces else set()
    out = []
    for p in planes:
        if not cells.get(p["id"]):
            continue
        if faces is not None:
            polys = [f for f, lab in faces if lab == p["id"]]
            if polys:
                poly = unary_union(polys).buffer(0.05).buffer(-0.05)
                polys = [q for q in getattr(poly, "geoms", [poly]) if q.geom_type == "Polygon" and q.area > 2]
            if polys:
                big = max(polys, key=lambda q: q.area)
                c = big.representative_point()
                down = p.get("down")
                out.append({"pitch": float(p["pitch"]), "area_sqft": round(float(raw[p["id"]] * scale), 1),
                            "plan_sqft": round(float(cells[p["id"]] * g * g * scale), 1),
                            "poly": [[[round(x, 2), round(y, 2)] for x, y in q.exterior.coords] for q in polys],
                            "c": [round(c.x, 2), round(c.y, 2)],
                            "down": [round(float(down[0]), 3), round(float(down[1]), 3)] if down is not None and not p.get("flat") else None})
                continue
        mask = L == p["id"]
        boxes = []
        for r in np.nonzero(mask.any(1))[0]:
            row = mask[r]
            d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
            for c0, c1 in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
                boxes.append(_box(xs[c0] - g / 2, ys[r] - g / 2, xs[c1 - 1] + g / 2, ys[r] + g / 2))
        if not boxes:
            continue
        poly = unary_union(boxes).buffer(0.35).buffer(-0.35).simplify(0.6).intersection(used)
        if taken is not None and claimed:
            # the line-built facets own their area: this one only fills gaps
            poly = poly.difference(unary_union([f for f, lab in faces if lab != p["id"]]))
        polys = [q for q in getattr(poly, "geoms", [poly]) if q.geom_type == "Polygon" and q.area > 2]
        if not polys:
            continue
        big = max(polys, key=lambda q: q.area)
        c = big.representative_point()
        down = p.get("down")
        out.append({"pitch": float(p["pitch"]), "area_sqft": round(float(raw[p["id"]] * scale), 1),
                    "plan_sqft": round(float(cells[p["id"]] * g * g * scale), 1),
                    "poly": [[[round(x, 2), round(y, 2)] for x, y in q.exterior.coords] for q in polys],
                    "c": [round(c.x, 2), round(c.y, 2)],
                    "down": [round(float(down[0]), 3), round(float(down[1]), 3)] if down is not None and not p.get("flat") else None})
    out.sort(key=lambda f: f["area_sqft"])
    for i, f in enumerate(out):
        f["label"] = (chr(65 + i) if i < 26 else chr(65 + i // 26 - 1) + chr(65 + i % 26))
    return out


def _faces_from_lines(res, used, L, xs, ys):
    """Split the outline along the drawn roof lines (ridges, hips, valleys,
    step lines) so facet borders are the same straight lines as the length
    diagram. Each face gets the facet the LiDAR says covers it; a face the
    lines don't separate is split along the LiDAR facet border instead."""
    import numpy as np
    import shapely
    from shapely.geometry import LineString, box as _box
    from shapely.ops import polygonize
    try:
        lines = [used.exterior]
        for sg in res.get("segments") or []:
            if sg.get("edge"):  # outline edges are already in the ring
                continue
            a, b = np.array(sg["a"], float), np.array(sg["b"], float)
            d = b - a
            Ln = float(np.hypot(*d))
            if Ln < 1.0:
                continue
            u = d / Ln
            lines.append(LineString([a - u * 0.8, b + u * 0.8]))  # reach the line it meets
        faces = [f.intersection(used) for f in polygonize(unary_union(lines))]
        faces = [f for f in faces if not f.is_empty and f.area > 4]
        if not faces:
            return None
        g = float(abs(xs[1] - xs[0])) if len(xs) > 1 else 0.5
        X, Y = np.meshgrid(xs, ys)
        out = []
        for f in faces:
            fs = [q for q in getattr(f, "geoms", [f]) if q.geom_type == "Polygon"]
            for q in fs:
                inside = shapely.contains_xy(q, X, Y) & (L >= 0)
                labs = L[inside]
                if not len(labs):
                    continue
                vals, cnt = np.unique(labs, return_counts=True)
                share = cnt / cnt.sum()
                if share.max() >= 0.6 or q.area < 40:  # trust the drawn lines
                    out.append((q, int(vals[share.argmax()])))
                    continue
                # two facets in one face: cut it with one straight line fitted to
                # the LiDAR border between them, squared to the house
                main2 = vals[np.argsort(-share)[:2]]
                if len(vals) >= 2 and share[np.argsort(-share)[:2]].sum() >= 0.9:
                    cut = _straight_cut(q, L, X, Y, inside, int(main2[0]), int(main2[1]), used)
                    if cut:
                        out.extend(cut)
                        continue
                # otherwise: cut it along the LiDAR borders
                for v, sh in zip(vals, share):
                    if sh < 0.08:
                        continue
                    m = inside & (L == v)
                    boxes = []
                    for r in np.nonzero(m.any(1))[0]:
                        row = m[r]
                        dd = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
                        for c0, c1 in zip(np.nonzero(dd == 1)[0], np.nonzero(dd == -1)[0]):
                            boxes.append(_box(xs[c0] - g / 2, ys[r] - g / 2, xs[c1 - 1] + g / 2, ys[r] + g / 2))
                    piece = unary_union(boxes).buffer(0.6).buffer(-0.6).simplify(0.8).intersection(q)
                    if not piece.is_empty:
                        out.append((piece, int(v)))
        return out or None
    except Exception:
        return None


def _straight_cut(q, L, X, Y, inside, la, lb, used):
    """Split polygon q between facets la and lb with one straight line."""
    import math
    import numpy as np
    from shapely.geometry import LineString
    from shapely.ops import split
    from .roofclean import main_axis
    ma, mb = inside & (L == la), inside & (L == lb)
    # border cells: a-cells with a b-cell next to them
    from scipy import ndimage as ndi
    near_b = ndi.binary_dilation(mb, iterations=1)
    bd = ma & near_b
    if bd.sum() < 6:
        return None
    P = np.column_stack([X[bd], Y[bd]])
    c = P.mean(0)
    _, _, vt = np.linalg.svd(P - c, full_matrices=False)
    u = vt[0]
    ax = main_axis(used)
    for k in range(4):  # square it to the house when it's close
        t = ax + k * math.pi / 2
        v = np.array([math.cos(t), math.sin(t)])
        if abs(u @ v) > math.cos(math.radians(12)):
            u = v
            break
    big = max(q.bounds[2] - q.bounds[0], q.bounds[3] - q.bounds[1]) * 3
    line = LineString([c - u * big, c + u * big])
    try:
        parts = [g for g in split(q, line).geoms if g.area > 1]
    except Exception:
        return None
    if len(parts) != 2:
        return None
    out = []
    import shapely
    for g in parts:
        m = shapely.contains_xy(g, X, Y) & (L >= 0)
        labs = L[m]
        if not len(labs):
            return None
        va, cn = np.unique(labs, return_counts=True)
        out.append((g, int(va[cn.argmax()])))
    if out[0][1] == out[1][1]:
        return None
    return out


def _yard(m, parcel) -> dict | None:
    """The lot page: yard measurements and the shaded aerial."""
    try:
        r = m.measure(parcel, want_overlay=True, want_photo=True)
    except Exception:
        return None
    keys = ["lot_sqft", "lawn_sqft", "parkway_sqft", "driveway_sqft", "sidewalk_sqft", "other_hardscape_sqft",
            "structures_sqft", "beds_sqft", "building_sqft", "tree_count", "tree_canopy_sqft", "frontage_ft"]
    return {**{k: r.get(k) for k in keys}, "photo_jpg": r.get("photo_jpg") or "",
            "quality": (r.get("sources") or {}).get("quality"), "note": (r.get("sources") or {}).get("note")}


def _walls_total(m, env, structs) -> dict | None:
    """Siding / exterior measurements for the chosen structures, summed."""
    from shapely.geometry import Polygon
    from .walls import SIDES, measure_walls
    tot = None
    for st in structs:
        try:
            poly = Polygon(st["outline"][0])
            mdl = st.pop("_model", None)
            model = None
            if mdl is not None and mdl[1] is not None:
                from .walls import RoofModel
                model = RoofModel(*mdl)
            w = measure_walls(m.lidar, env, poly, st.get("segments"), model=model)
        except Exception:
            w = None
        st["walls"] = w
        if w is None:
            continue
        if tot is None:
            tot = {k: (dict(v) if isinstance(v, dict) else v) for k, v in w.items() if k != "facets"}
            tot["elevations"] = {}
            continue
        for k, v in w.items():
            if k == "by_side":
                for sd in SIDES:
                    tot["by_side"][sd] += v[sd]
            elif isinstance(v, (int, float)) and k != "overhang_ft":
                tot[k] = round(tot[k] + v, 1)
    if tot:
        tot["elevations"] = _elevations(structs)
        g = tot["gross_wall_sqft"]
        net = tot["est_net_wall_sqft"]
        tot["siding_squares"] = [{"pct": p, "sqft": round(net * (1 + p / 100)),
                                  "squares": round(net * (1 + p / 100) / 100, 1)} for p in (0, 10, 15, 20)]
        tot["note"] = ("Wall areas are measured from the LiDAR roof heights along each wall (gables included). "
                       "Windows and doors can't be seen from above: openings, net wall area and the window count "
                       "are estimates. Siding vs brick/stone is not separated.")
        tot["gross_wall_sqft"] = round(g)
    return tot


_RIGHT = {"North": (-1.0, 0.0), "East": (0.0, 1.0), "South": (1.0, 0.0), "West": (0.0, -1.0)}


def _elevations(structs) -> dict:
    """Each side's outer walls as seen standing in front of it: wall
    silhouettes (x along the side, measured height every foot)."""
    import numpy as np
    out = {}
    for st in structs:
        w = st.get("walls") or {}
        for f in w.get("facets") or []:
            if f.get("kind") != "wall" or not f.get("profile"):
                continue
            if f["length_ft"] < 2.5:  # corner returns: counted in the areas, not drawn
                continue
            side = f["side"]
            r = np.array(_RIGHT[side])
            a, b = np.array(f["a"]), np.array(f["b"])
            L = float(np.hypot(*(b - a)))
            k = len(f["profile"])
            ts = (np.arange(k) + 0.5) * (L / k)
            u = (b - a) / L
            xs = [float((a + u * t) @ r) for t in ts]
            # the wall's ends, so the silhouette starts and stops at its corners
            x_a, x_b = float(a @ r), float(b @ r)
            if f.get("shape"):
                pts = [(float((a + u * t) @ r), h) for t, h in f["shape"]]
            else:
                prof = list(f["profile"])
                pts = [(x_a, prof[0])] + list(zip(xs, prof)) + [(x_b, prof[-1])]
            pts.sort()
            out.setdefault(side, []).append({"pts": [[round(x, 2), round(h, 2)] for x, h in pts],
                                             "area": f["area_sqft"], "length": f["length_ft"],
                                             "depth": round(float(((a + b) / 2) @ np.array([-r[1], r[0]])), 2)})
    for side, walls in out.items():
        x0 = min(p[0] for wl in walls for p in wl["pts"])
        for wl in walls:
            wl["pts"] = [[round(x - x0, 2), h] for x, h in wl["pts"]]
        # far walls first, so nearer walls are drawn in front of them
        walls.sort(key=lambda wl: -wl["depth"])
        for i, wl in enumerate(sorted(walls, key=lambda wl: wl["pts"][0][0])):
            wl["label"] = side[0] + str(i + 1)
    return out


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
