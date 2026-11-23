"""Wall (siding) measurements from the roof outline and the LiDAR.

The walls stand about one overhang inside the roof edge. A wall's area is the
height of the roof directly above it (roof height above the ground outside
it), summed along its length: that gets the gable triangles, walk-out
basements and split levels right without modelling them. Where an upper wall
rises above a lower roof (the step-flashing / flashing lines of the roof
report), its area is the height difference between the two roofs.

Everything is in the LiDAR CRS (feet). Windows and doors can't be seen from
above, so openings are estimated from wall area and labelled as estimates.
"""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import Polygon

SIDES = ("North", "East", "South", "West")
STEP = 1.0                # sample every foot along a wall
ROOF_THICKNESS = 0.0      # calibrated: LiDAR roof-edge heights already read slightly low
OPENING_SHARE = 0.135     # windows + doors, share of gross wall area (typical)
SQFT_PER_OPENING = 165.0  # one window/door per this much gross wall (typical)
MIN_WALL_TOP = 6.5        # ft: no real wall top (eave) is lower than this
STEP_DZ = 2.0             # ft: a roof-height jump bigger than this is a wall


def _side(nx, ny) -> str:
    az = (math.degrees(math.atan2(nx, ny)) + 360) % 360  # 0 = north, 90 = east
    return SIDES[int(((az + 45) % 360) // 90)]


class _HeightGrid:
    """Roof heights above ground on a 1-ft grid over the building, fetched
    from the LiDAR in one call (cheap even for the sparse USGS source)."""

    def __init__(self, lidar, env, bounds, pad=6.0):
        minx, miny, maxx, maxy = bounds
        self.x0, self.y1 = minx - pad, maxy + pad
        W = int((maxx - minx + 2 * pad) / STEP) + 1
        H = int((maxy - miny + 2 * pad) / STEP) + 1
        xs = self.x0 + (np.arange(W) + 0.5) * STEP
        ys = self.y1 - (np.arange(H) + 0.5) * STEP
        XX, YY = np.meshgrid(xs, ys)
        smp = lidar.sample(env, XX, YY)
        g = np.full((H, W), np.nan)
        if smp is not None:
            g = np.asarray(smp["obj"], float)
            g[g <= 0] = np.nan
        self.g = g

    def __call__(self, xs, ys):
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        H, W = self.g.shape
        c = np.floor((xs - self.x0) / STEP).astype(int)
        r = np.floor((self.y1 - ys) / STEP).astype(int)
        ok = (c >= 0) & (c < W) & (r >= 0) & (r < H)
        out = np.full(xs.shape, np.nan)
        out[ok] = self.g[r[ok], c[ok]]
        return out


def _step_walls(lidar, env, walls_poly):
    """Height jumps inside the wall outline, on a grid aligned with the house
    (so a straight wall is counted once per foot, not as a staircase)."""
    import shapely
    from .roofclean import main_axis
    inner = walls_poly.buffer(-1.0, join_style=2)
    if inner.is_empty:
        return []
    ang = main_axis(walls_poly)
    u = np.array([math.cos(ang), math.sin(ang)])
    v = np.array([-u[1], u[0]])
    ring = np.asarray(walls_poly.exterior.coords)
    su, sv = ring @ u, ring @ v
    ss = np.arange(su.min(), su.max() + STEP, STEP)
    tt = np.arange(sv.min(), sv.max() + STEP, STEP)
    S, T = np.meshgrid(ss, tt)
    X = S * u[0] + T * v[0]
    Y = S * u[1] + T * v[1]
    smp = lidar.sample(env, X, Y)
    if smp is None:
        return []
    from scipy import ndimage as ndi
    raw = np.asarray(smp["obj"], float)
    valid = np.isfinite(raw) & (raw > 0)
    if valid.sum() < 20:
        return []
    # cells the laser missed take their nearest neighbour's height
    idx = ndi.distance_transform_edt(~valid, return_distances=False, return_indices=True)
    h = raw[tuple(idx)]
    h = ndi.median_filter(h, size=3)  # single-cell spikes (vents, noise)
    # low areas (deck / porch floors, notches) count only if they're real areas
    low = h < MIN_WALL_TOP
    lab, k = ndi.label(low)
    if k:
        sizes = ndi.sum(np.ones_like(h), lab, range(1, k + 1))
        small = np.zeros(k + 1, bool)
        small[1:] = sizes < 25
        hi_idx = ndi.distance_transform_edt(low, return_distances=False, return_indices=True)
        h = np.where(small[lab] & low, h[tuple(hi_idx)], h)
    h = np.where(h >= MIN_WALL_TOP, h, 0.0)
    ins = shapely.contains_xy(inner, X, Y)
    out = []
    for axis_vec, a_sl, b_sl in ((u, (slice(None), slice(0, -1)), (slice(None), slice(1, None))),
                                 (v, (slice(0, -1), slice(None)), (slice(1, None), slice(None)))):
        ha, hb = h[a_sl], h[b_sl]
        ok = ins[a_sl] & ins[b_sl]
        dz = hb - ha  # + : higher toward +axis, so the wall faces -axis
        m = ok & (np.abs(dz) > STEP_DZ)
        if not m.any():
            continue
        for sgn, face in ((1, -axis_vec), (-1, axis_vec)):
            mm = m & (np.sign(dz) == sgn)
            if mm.sum() < 3:  # a chimney or vent, not a wall
                continue
            area = float(np.abs(dz[mm]).sum() * STEP)
            out.append((_side(*face), area, float(mm.sum() * STEP), float(np.median(np.abs(dz[mm])))))
    return out


class RoofModel:
    """Roof surface from the fitted planes: absolute roof height at any point
    (the plane covering it, from the facet label grid)."""

    def __init__(self, planes, L, xs, ys):
        from scipy import ndimage as ndi
        # real facets only: scrap planes (fascia / wall hits) would bend wall tops
        big = [p for p in planes if float(p.get("area_sqft", 0)) >= 50] or list(planes)
        self.coef = {int(p["id"]): (float(p["a"]), float(p["b"]), float(p["c"])) for p in big}
        self.xs, self.ys = np.asarray(xs, float), np.asarray(ys, float)
        self.g = float(abs(self.xs[1] - self.xs[0])) if len(self.xs) > 1 else 0.5
        valid = np.isin(L, list(self.coef))
        if not valid.any():
            self.L = None
            return
        idx = ndi.distance_transform_edt(~valid, return_distances=False, return_indices=True)
        self.L = np.where(valid, L, L[tuple(idx)])  # every cell takes its nearest real plane

    def z(self, P):
        if self.L is None:
            return np.full(len(P), np.nan)
        H, W = self.L.shape
        c = np.clip(np.round((P[:, 0] - self.xs[0]) / self.g).astype(int), 0, W - 1)
        r = np.clip(np.round((self.ys[0] - P[:, 1]) / self.g).astype(int), 0, H - 1)
        lab = self.L[r, c]
        out = np.full(len(P), np.nan)
        for k, (a, b, cc) in self.coef.items():
            m = lab == k
            out[m] = a * P[m, 0] + b * P[m, 1] + cc
        return out


def _trim_end(pts, L, end, reach=2.0, jump=1.5):
    """Replace a spike / drop in the last ``reach`` ft of a wall (a corner
    picking up the next roof, or a gap) by continuing the wall top inside
    it straight out to the corner."""
    segs = list(zip(pts, pts[1:]))
    if not segs:
        return pts
    order = range(len(segs)) if end == 0 else range(len(segs) - 1, -1, -1)
    xin = reach if end == 0 else L - reach
    # first segment reaching past the end zone that isn't itself a cliff
    pick = None
    for i in order:
        (x0, y0), (x1, y1) = segs[i]
        inside = x1 > xin if end == 0 else x0 < xin
        if not inside:
            continue
        if x1 > x0 and abs(y1 - y0) / (x1 - x0) <= 1.0:
            pick = i
            break
    if pick is None:
        return pts
    (x0, y0), (x1, y1) = segs[pick]
    slope = (y1 - y0) / (x1 - x0)
    if end == 0:
        if pick == 0 and x0 <= 0.01:
            return pts
        tail = [list(q) for q in pts[pick + 1:]]
        h0 = y0 - slope * x0
        if all(abs(q[1] - (y0 + slope * (q[0] - x0))) <= jump for q in pts[:pick + 1]):
            return pts
        return [[0.0, h0]] + tail
    if pick == len(segs) - 1 and x1 >= L - 0.01:
        return pts
    head = [list(q) for q in pts[:pick + 1]]
    h1 = y1 + slope * (L - x1)
    if all(abs(q[1] - (y1 + slope * (q[0] - x1))) <= jump for q in pts[pick + 1:]):
        return pts
    return head + [[L, h1]]


def _clean_shape(ts, h, L, tol=0.35):
    """A wall's top as a few straight pieces: [(t, h), ...] from 0 to L."""
    from shapely.geometry import LineString
    t = np.concatenate([[0.0], ts, [L]])
    hh = np.concatenate([[h[0]], h, [h[-1]]])
    line = LineString(np.column_stack([t, hh])).simplify(tol, preserve_topology=False)
    pts = [list(map(float, c)) for c in line.coords]
    # a corner picking up the next roof (or a gap) shows as a short spike or
    # drop at a wall end: carry the height from 2 ft in out to the corner
    if L > 6 and len(pts) > 2:
        pts = _trim_end(pts, L, 0)
        pts = _trim_end(pts, L, 1)
        pts = [[x, max(0.0, y)] for x, y in pts]
    # a short piece between two longer slopes is the peak (or valley) where
    # those slopes meet: put the corner at their intersection
    changed = True
    while changed and len(pts) >= 4:
        changed = False
        for i in range(1, len(pts) - 2):
            (xa, ya), (xb, yb), (xc, yc), (xd, yd) = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
            if xc - xb < 2.0 and xb - xa > 3.0 and xd - xc > 3.0:
                s1 = (yb - ya) / (xb - xa)
                s2 = (yd - yc) / (xd - xc)
                if abs(s1 - s2) > 0.05:
                    xi = (yc - ya + s1 * xa - s2 * xc) / (s1 - s2)
                    if xb - 2.0 <= xi <= xc + 2.0:
                        pts = pts[:i] + [[xi, ya + s1 * (xi - xa)]] + pts[i + 2:]
                        changed = True
                        break
    # near-level pieces are level (an eave is level)
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        if x1 > x0 and abs(y1 - y0) / (x1 - x0) < 0.06:
            m = (y0 + y1) / 2
            pts[i][1] = pts[i + 1][1] = m
    # drop vertices left on a straight line
    clean = [pts[0]]
    for q in pts[1:]:
        if len(clean) >= 2:
            (x0, y0), (x1, y1) = clean[-2], clean[-1]
            if x1 > x0 and q[0] > x1 and abs((y1 - y0) / (x1 - x0) - (q[1] - y1) / (q[0] - x1)) < 0.02:
                clean[-1] = q
                continue
        clean.append(q)
    return [[round(x, 2), round(y, 2)] for x, y in clean]


def measure_walls(lidar, env, outline, segments=None, overhang: float = 1.0, model: "RoofModel | None" = None) -> dict | None:
    """Wall areas per side for one structure.

    ``outline``: the roof outline (Polygon, LAS feet). ``segments``: the roof
    report's line segments (dicts with type, a, b), used for upper walls
    above lower roofs and for gutter / fascia lengths."""
    if outline is None or outline.is_empty:
        return None
    poly = max(getattr(outline, "geoms", [outline]), key=lambda g: g.area)
    walls = poly.buffer(-overhang, join_style=2)
    if walls.is_empty:
        walls = poly
    walls = max(getattr(walls, "geoms", [walls]), key=lambda g: g.area)
    ring = np.asarray(walls.exterior.coords)[:-1]
    if Polygon(ring).exterior.is_ccw is False:
        ring = ring[::-1]
    n = len(ring)
    heights = _HeightGrid(lidar, env, poly.bounds)
    if not np.isfinite(heights.g).any():
        return None
    per = {s: 0.0 for s in SIDES}
    facets = []
    edge_h = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        d = b - a
        L = float(np.hypot(*d))
        if L < 0.5:
            edge_h.append((np.nan, np.nan))
            continue
        u = d / L
        out = np.array([u[1], -u[0]])  # outward for a CCW ring
        k = max(2, int(round(L / STEP)))
        ts = (np.arange(k) + 0.5) * (L / k)
        P = a + np.outer(ts, u) - out * 0.4  # just inside the wall line: roof above it
        h = heights(P[:, 0], P[:, 1]) - ROOF_THICKNESS
        ok = np.isfinite(h)
        if ok.sum() < max(2, 0.4 * k):
            edge_h.append((np.nan, np.nan))
            continue
        h = np.where(ok, h, np.nanmedian(h))
        h = np.clip(h, 0, 60)
        if len(h) >= 5:  # one-foot gaps and spikes along the wall top
            from scipy.ndimage import median_filter
            h = median_filter(h, size=5, mode="nearest")
        h[h < MIN_WALL_TOP] = 0.0  # deck / stoop floors and railings aren't walls
        shape = None
        if model is not None:
            # wall top from the roof planes (straight eaves, exact gables);
            # the LiDAR only sets where the ground is along this wall
            zr = model.z(P)
            okm = np.isfinite(zr) & (h > 0)
            if okm.sum() >= max(2, 0.5 * k):
                g0 = float(np.median(zr[okm] - h[okm]))
                hm = np.where(np.isfinite(zr), zr - g0, h)
                hm = np.clip(hm, 0, 60)
                hm[h <= 0] = 0.0
                h = hm
                shape = _clean_shape(ts, h, L)
        area = float(h.sum() * (L / k))
        side = _side(*out)
        per[side] += area
        facets.append({"side": side, "kind": "wall", "length_ft": round(L, 1),
                       "height_ft": round(float(np.median(h)), 1), "area_sqft": round(area, 1),
                       "a": [round(float(a[0]), 2), round(float(a[1]), 2)], "b": [round(float(b[0]), 2), round(float(b[1]), 2)],
                       "profile": [round(float(x), 1) for x in h],
                       "shape": shape or _clean_shape(ts, h, L, tol=0.6)})
        edge_h.append((float(h[0]), float(h[-1])))
    # corners: convex = outside corner, concave = inside corner; corner height
    # is the lower of the two walls meeting there (the trim stops at the eave)
    outside = inside = 0.0
    for i in range(n):
        p0, p1, p2 = ring[i - 1], ring[i], ring[(i + 1) % n]
        v1, v2 = p1 - p0, p2 - p1
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        if np.hypot(*v1) < 0.5 or np.hypot(*v2) < 0.5:
            continue
        cosang = (v1 @ v2) / (np.hypot(*v1) * np.hypot(*v2))
        if cosang > 0.94:  # nearly straight: not a corner
            continue
        hs = [x for x in (edge_h[i - 1][1], edge_h[i][0]) if np.isfinite(x)]
        if not hs:
            continue
        hc = min(hs)
        if cross > 0:
            outside += hc
        else:
            inside += hc
    # walls inside the outline: an upper wall above a lower roof, or the
    # house wall behind a deck / porch floor that the outline includes
    for side, area, L, hmed in _step_walls(lidar, env, walls):
        per[side] += area
        facets.append({"side": side, "kind": "upper wall", "length_ft": round(L, 1),
                       "height_ft": round(hmed, 1), "area_sqft": round(area, 1)})
    gross = sum(per.values())
    if gross <= 0:
        return None
    lines = {}
    for sgm in segments or []:
        lines[sgm.get("type")] = lines.get(sgm.get("type"), 0.0) + float(sgm.get("len") or 0)
    eave = lines.get("eave", 0.0)
    rake = lines.get("rake", 0.0)
    openings = max(1, int(round(gross / SQFT_PER_OPENING)))
    return {
        "gross_wall_sqft": round(gross),
        "by_side": {s: round(v) for s, v in per.items()},
        "est_openings": openings,
        "est_opening_sqft": round(gross * OPENING_SHARE),
        "est_net_wall_sqft": round(gross * (1 - OPENING_SHARE)),
        "outside_corners_ft": round(outside, 1),
        "inside_corners_ft": round(inside, 1),
        "wall_perimeter_ft": round(walls.length, 1),
        "gutter_ft": round(eave, 1),
        "downspouts": int(math.ceil(eave / 30.0)) if eave else 0,
        "fascia_ft": round(eave + rake, 1),
        "soffit_sqft": round((eave + rake) * overhang),
        "facets": facets,
        "overhang_ft": overhang,
    }
