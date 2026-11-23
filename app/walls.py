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


def measure_walls(lidar, env, outline, segments=None, overhang: float = 1.0) -> dict | None:
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
        h[h < MIN_WALL_TOP] = 0.0  # deck / stoop floors and railings aren't walls
        area = float(h.sum() * (L / k))
        side = _side(*out)
        per[side] += area
        facets.append({"side": side, "kind": "wall", "length_ft": round(L, 1),
                       "height_ft": round(float(np.median(h)), 1), "area_sqft": round(area, 1)})
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
