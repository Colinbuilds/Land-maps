"""Roof measurement from LiDAR: facets, ridges, hips, valleys, eaves, rakes.

Method (per structure):
  1. Roof outline = county building footprint(s) (roof-line planimetrics),
     touching footprints merged so a house and attached garage are one roof.
  2. A 1-ft roof surface comes from the LiDAR "obj" raster (max height of
     building / unclassified returns above ground).
  3. The downslope direction of every cell is snapped to the building's four
     principal directions (or "flat"); connected areas become roof facets.
  4. Boundaries between facets are classified from geometry:
        opposite facets, convex  -> ridge
        perpendicular, convex    -> hip
        perpendicular / opposite, concave -> valley
        big height jump          -> step flashing (roof meets a wall)
     Outline edges are eaves where the facet drains toward them, rakes where
     the facet slopes along them.
  5. Plan lengths are converted to true lengths with ONE pitch for every
     facet (default 8/12): rakes x sqrt(1+p^2), hips/valleys x sqrt(1+p^2/2),
     ridges and eaves are level.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from shapely.geometry import LineString, Polygon, MultiPolygon, Point
from shapely.ops import transform as shp_transform, unary_union

LINE_TYPES = ["ridge", "hip", "valley", "rake", "eave", "step_flashing"]
LINE_LABELS = {
    "ridge": "Ridges", "hip": "Hips", "valley": "Valleys", "rake": "Rakes",
    "eave": "Eaves", "step_flashing": "Step flashing",
}
CELL = 1.0  # ft


def pitch_factors(pitch: float):
    p = pitch / 12.0
    return {
        "area": math.sqrt(1 + p * p),
        "rake": math.sqrt(1 + p * p),
        "hip": math.sqrt(1 + p * p / 2),
        "valley": math.sqrt(1 + p * p / 2),
        "ridge": 1.0, "eave": 1.0, "step_flashing": math.sqrt(1 + p * p),
    }


def _polys(g):
    if g is None or g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    out = []
    for x in getattr(g, "geoms", []):
        out.extend(_polys(x))
    return out


def principal_angle(poly: Polygon) -> float:
    """Dominant wall direction (radians, 0..pi/2) by length-weighted edges."""
    hist = np.zeros(90)
    for ring in [poly.exterior, *poly.interiors]:
        c = np.asarray(ring.coords)
        for (x0, y0), (x1, y1) in zip(c[:-1], c[1:]):
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 1:
                continue
            a = int(math.degrees(math.atan2(y1 - y0, x1 - x0)) % 90)
            hist[a] += L
    sm = ndi.uniform_filter1d(hist, 5, mode="wrap")
    return math.radians(float(np.argmax(sm)))


class RoofGrid:
    """A 1-ft grid in the LiDAR CRS around one roof outline."""

    def __init__(self, outline: Polygon, pad: float = 4.0):
        minx, miny, maxx, maxy = outline.bounds
        self.x0 = math.floor(minx - pad)
        self.y1 = math.ceil(maxy + pad)
        self.W = int(math.ceil(maxx + pad - self.x0))
        self.H = int(math.ceil(self.y1 - (miny - pad)))
        cols = self.x0 + (np.arange(self.W) + 0.5) * CELL
        rows = self.y1 - (np.arange(self.H) + 0.5) * CELL
        self.xs, self.ys = np.meshgrid(cols, rows)

    def to_rc(self, x, y):
        return (self.y1 - y) / CELL - 0.5, (x - self.x0) / CELL - 0.5

    def to_xy(self, r, c):
        return self.x0 + (c + 0.5) * CELL, self.y1 - (r + 0.5) * CELL

    def rasterize(self, poly) -> np.ndarray:
        im = Image.new("L", (self.W, self.H), 0)
        d = ImageDraw.Draw(im)
        for p in _polys(poly):
            d.polygon([((x - self.x0) / CELL, (self.y1 - y) / CELL) for x, y in p.exterior.coords], fill=1)
            for h in p.interiors:
                d.polygon([((x - self.x0) / CELL, (self.y1 - y) / CELL) for x, y in h.coords], fill=0)
        return np.asarray(im) > 0


def analyze_roof(outline: Polygon, sampler, pitch: float = 8.0) -> dict:
    """``sampler(xs, ys)`` returns LiDAR obj heights (ft above ground) at
    LAS-CRS coordinates, or None when there is no coverage."""
    g = RoofGrid(outline)
    inside = g.rasterize(outline)
    smp = sampler(g.xs, g.ys)
    fac = pitch_factors(pitch)
    plan_area = outline.area
    result = {
        "plan_area_sqft": plan_area,
        "pitch": pitch,
        "lines": {k: 0.0 for k in LINE_TYPES},
        "segments": [],
        "facets": [],
        "method": "lidar",
    }
    z = None if smp is None else smp.astype(np.float64)
    if z is None or (inside & (z > 3)).sum() < 0.4 * inside.sum():
        return _fallback(outline, result, fac)

    # ---- roof surface: keep roof returns inside the outline, fill the rest
    valid = inside & (z > 3)
    zz = np.where(valid, z, np.nan)
    idx = ndi.distance_transform_edt(~valid, return_distances=False, return_indices=True)
    zf = zz[tuple(idx)]
    zf = ndi.median_filter(zf, size=3)
    zs = ndi.gaussian_filter(zf, 1.0)

    gy, gx = np.gradient(zs, CELL)          # gy: change per row (southward)
    dzdx, dzdy = gx, -gy                    # north-up
    slope = np.hypot(dzdx, dzdy)
    down = np.arctan2(-dzdy, -dzdx)         # downslope azimuth (math angle)

    # ---- snap downslope directions to the building's 4 principal directions
    th0 = principal_angle(outline)
    rel = (down - th0) % (2 * math.pi)
    k = np.round(rel / (math.pi / 2)).astype(int) % 4    # 0..3
    label = np.where(slope < 0.15, 4, k)                 # 4 = flat
    label = np.where(inside, label, -1)
    # clean up: majority filter, then dissolve tiny pieces into neighbours
    for _ in range(2):
        label = _mode_filter(label, inside)
    facet_id, n_fac = _facets(label, inside, min_cells=12)

    # ---- facets
    facets = []
    for f in range(1, n_fac + 1):
        m = facet_id == f
        lab = int(np.bincount(label[m]).argmax())
        facets.append({"id": f, "dir": lab, "cells": int(m.sum()),
                       "flat": lab == 4})
    flat_cells = sum(f["cells"] for f in facets if f["flat"])
    tot_cells = max(1, sum(f["cells"] for f in facets))
    flat_area = plan_area * flat_cells / tot_cells
    sloped_area = plan_area - flat_area
    dirs = [th0 + i * math.pi / 2 for i in range(4)]

    # ---- interior lines between facets
    segs = []
    pairs = {}
    for dr, dc in ((0, 1), (1, 0)):
        a = facet_id[: g.H - dr, : g.W - dc]
        b = facet_id[dr:, dc:]
        m = (a > 0) & (b > 0) & (a != b)
        rr, cc = np.nonzero(m)
        for r, c in zip(rr, cc):
            i, j = int(a[r, c]), int(b[r, c])
            key = (min(i, j), max(i, j))
            pairs.setdefault(key, []).append((r + dr / 2.0, c + dc / 2.0))
    for (i, j), pts in pairs.items():
        fi, fj = facets[i - 1], facets[j - 1]
        pts = np.array(pts)
        for comp in _split_clusters(pts, gap=2.5):
            if len(comp) < 3:
                continue
            seg = _fit_segment(comp)
            if seg is None or seg["length"] < 3:
                continue
            kind = _classify_internal(seg, fi, fj, zs, zf, facet_id, i, j, g)
            if kind is None:
                continue
            segs.append({"type": kind, **seg})

    # ---- outline edges: eave vs rake
    ext_segs = []
    for poly in _polys(outline):
        for ri_, ring in enumerate([poly.exterior, *poly.interiors]):
            coords = np.asarray(ring.coords)
            ccw = ring.is_ccw
            for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
                L = math.hypot(x1 - x0, y1 - y0)
                if L < 0.5:
                    continue
                ux, uy = (x1 - x0) / L, (y1 - y0) / L
                # outward normal (exterior ring: ccw -> right-hand side is outside)
                nx, ny = (uy, -ux) if ccw else (-uy, ux)
                if ri_ > 0:  # holes: outside of the roof is inside the ring
                    nx, ny = -nx, -ny
                n_st = max(1, int(L // 1.0))
                kinds = []
                for s in range(n_st):
                    t = (s + 0.5) * L / n_st
                    # Read the facet a few feet in: right at the edge the
                    # surface is smeared by the fill outside the outline.
                    votes = {}
                    # Near a corner a deep inset crosses the hip line into
                    # the neighbouring plane, so stay shallower than the
                    # distance to the corner.
                    lim = max(0.6, 0.7 * min(t, L - t))
                    for inset in sorted({min(v, lim) for v in (2.5, 3.5, 4.5)}):
                        px, py = x0 + ux * t - nx * inset, y0 + uy * t - ny * inset
                        r, c = g.to_rc(px, py)
                        ri, ci = int(round(r)), int(round(c))
                        if 0 <= ri < g.H and 0 <= ci < g.W and facet_id[ri, ci] > 0:
                            f = facets[facet_id[ri, ci] - 1]
                            votes[f["dir"]] = votes.get(f["dir"], 0) + 1
                    if not votes:
                        kinds.append(None)
                        continue
                    lab = max(votes, key=votes.get)
                    if lab == 4:
                        kinds.append("eave")
                    else:
                        d = dirs[lab]
                        cosang = math.cos(d) * nx + math.sin(d) * ny
                        kinds.append("eave" if cosang > 0.5 else "rake")
                kinds = _merge_short_runs(kinds, min_len=4)
                step = L / n_st
                s = 0
                while s < len(kinds):
                    e = s
                    while e + 1 < len(kinds) and kinds[e + 1] == kinds[s]:
                        e += 1
                    kind = kinds[s] or "eave"
                    a0, a1 = s * step, (e + 1) * step
                    ext_segs.append({"type": kind, "x0": x0 + ux * a0, "y0": y0 + uy * a0,
                                     "x1": x0 + ux * a1, "y1": y0 + uy * a1, "length": a1 - a0})
                    s = e + 1
    segs.extend(ext_segs)

    for s in segs:
        s["true_length"] = s["length"] * fac[s["type"]]
        result["lines"][s["type"]] += s["true_length"]
    result["segments"] = segs
    result["facets"] = facets
    result["facet_count"] = len([f for f in facets if f["cells"] >= 20])
    result["flat_area_sqft"] = flat_area
    result["roof_area_sqft"] = sloped_area * fac["area"] + flat_area
    result["principal_angle"] = th0
    result["grid"] = g
    result["facet_raster"] = facet_id
    result["label_raster"] = label
    return result


def _fallback(outline, result, fac):
    """No usable LiDAR: area from the outline, all edges counted as eaves."""
    per = sum(p.exterior.length for p in _polys(outline))
    result["method"] = "outline_only"
    result["lines"]["eave"] = per
    result["roof_area_sqft"] = outline.area * fac["area"]
    result["flat_area_sqft"] = 0.0
    result["facet_count"] = None
    return result


def _merge_short_runs(kinds, min_len):
    """Fold runs shorter than ``min_len`` stations into the longer neighbour,
    so an eave doesn't flicker into 1-ft bits of rake."""
    kinds = [k or "eave" for k in kinds]
    while True:
        runs = []
        s = 0
        while s < len(kinds):
            e = s
            while e + 1 < len(kinds) and kinds[e + 1] == kinds[s]:
                e += 1
            runs.append([s, e, kinds[s]])
            s = e + 1
        short = [i for i, r in enumerate(runs) if r[1] - r[0] + 1 < min_len]
        if len(runs) <= 1 or not short:
            return kinds
        i = min(short, key=lambda i: runs[i][1] - runs[i][0])
        left = runs[i - 1] if i > 0 else None
        right = runs[i + 1] if i + 1 < len(runs) else None
        nb = max([x for x in (left, right) if x], key=lambda r: r[1] - r[0])
        for t in range(runs[i][0], runs[i][1] + 1):
            kinds[t] = nb[2]


def _mode_filter(label, inside):
    out = label.copy()
    counts = np.stack([ndi.uniform_filter((label == v).astype(np.float32), size=5) for v in range(5)])
    best = counts.argmax(0)
    out[inside] = best[inside]
    return out


def _facets(label, inside, min_cells):
    facet_id = np.zeros(label.shape, int)
    n = 0
    for v in range(5):
        lab, k = ndi.label((label == v) & inside)
        for i in range(1, k + 1):
            n += 1
            facet_id[lab == i] = n
    # dissolve small facets into the neighbour they share most border with
    sizes = np.bincount(facet_id.ravel(), minlength=n + 1)
    for f in np.argsort(sizes):
        if f == 0 or sizes[f] >= min_cells or sizes[f] == 0:
            continue
        m = facet_id == f
        ring = ndi.binary_dilation(m) & ~m & (facet_id > 0)
        if ring.any():
            nb = np.bincount(facet_id[ring]).argmax()
            facet_id[m] = nb
            sizes[nb] += sizes[f]
            sizes[f] = 0
    # renumber
    ids = np.unique(facet_id[facet_id > 0])
    remap = np.zeros(facet_id.max() + 1, int)
    remap[ids] = np.arange(1, len(ids) + 1)
    return remap[facet_id], len(ids)


def _split_clusters(pts, gap):
    if len(pts) == 0:
        return []
    from scipy.spatial import cKDTree
    t = cKDTree(pts)
    pairs = t.query_pairs(gap)
    parent = list(range(len(pts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for i in range(len(pts)):
        groups.setdefault(find(i), []).append(i)
    return [pts[v] for v in groups.values()]


def _fit_segment(pts):
    c = pts.mean(0)
    u, s, vt = np.linalg.svd(pts - c, full_matrices=False)
    d = vt[0]
    proj = (pts - c) @ d
    a, b = proj.min(), proj.max()
    spread = np.abs((pts - c) @ vt[1]).mean() if len(pts) > 2 else 0
    if spread > 2.5:  # a blob, not a line
        return None
    p0, p1 = c + d * a, c + d * b
    return {"r0": p0[0], "c0": p0[1], "r1": p1[0], "c1": p1[1], "length": (b - a) * CELL + CELL}


def _classify_internal(seg, fi, fj, zs, zraw, facet_id, i, j, g):
    if fi["flat"] and fj["flat"]:
        return None
    dr, dc = seg["r1"] - seg["r0"], seg["c1"] - seg["c0"]
    L = math.hypot(dr, dc) or 1
    nr, nc = -dc / L, dr / L
    H, W = zs.shape

    def at(arr, r, c):
        rr, cc = int(round(r)), int(round(c))
        return arr[rr, cc] if 0 <= rr < H and 0 <= cc < W else np.nan

    jumps, zl, zside = [], [], []
    for t in np.linspace(0.15, 0.85, 9):
        r = seg["r0"] + dr * t
        c = seg["c0"] + dc * t
        # a wall shows up as a sharp jump over 3 ft in the raw surface; on an
        # 8/12 plane the most you'd see is 2 ft
        jumps.append(abs(at(zraw, r + nr * 1.5, c + nc * 1.5) - at(zraw, r - nr * 1.5, c - nc * 1.5)))
        zl.append(at(zs, r, c))
        zside.append((at(zs, r + nr * 3, c + nc * 3) + at(zs, r - nr * 3, c - nc * 3)) / 2)
    jumps, zl, zside = np.array(jumps), np.array(zl), np.array(zside)
    ok = ~np.isnan(jumps) & ~np.isnan(zl) & ~np.isnan(zside)
    if ok.sum() < 3:
        return None
    if np.median(jumps[ok]) > 3.0:
        return "step_flashing"
    if fi["flat"] or fj["flat"]:
        return None
    bulge = float(np.median(zl[ok] - zside[ok]))
    convex, concave = bulge > 0.25, bulge < -0.25
    rel = (fi["dir"] - fj["dir"]) % 4
    if rel == 2:
        return "ridge" if convex else ("valley" if concave else None)
    if rel in (1, 3):
        return "hip" if convex else ("valley" if concave else None)
    return None
