"""Trace a roof outline from the aerial photo.

Starts from the county outline placed where the roof shows in the photo
(building lean applied), then moves every edge in or out, up to a few feet,
onto the strongest straight colour edge parallel to it: the drip edge you can
see. Corners are rebuilt where the moved edges meet, so square roofs stay
square. Works in the LiDAR CRS (feet); the photo is sampled through the
lean offset.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage as ndi
from shapely.geometry import Polygon

from .imagery import Mosaic

SEARCH_FT = 4.0
STEP_FT = 0.25


class PhotoSampler:
    def __init__(self, m, outline_las: Polygon, lean_ft: tuple[float, float], zoom: int = 21):
        self.m, self.lean = m, lean_ft
        to_wgs = m.lidar.from_las
        minx, miny, maxx, maxy = outline_las.buffer(SEARCH_FT + 8).bounds
        lx, ly = lean_ft
        a = to_wgs.transform(minx + lx, miny + ly)
        b = to_wgs.transform(maxx + lx, maxy + ly)
        self.mos = Mosaic(m.c["imagery"]["tile_url"], (a[0], a[1], b[0], b[1]), zoom)
        rgb = ndi.gaussian_filter(np.asarray(self.mos.image, dtype=np.float32), (1.2, 1.2, 0))
        self.gx = sum(ndi.sobel(rgb[..., c], 1) for c in range(3))
        self.gy = sum(ndi.sobel(rgb[..., c], 0) for c in range(3))

    def px(self, xs, ys):
        """LAS-CRS points (true position) -> photo pixel coords (lean applied)."""
        lon, lat = self.m.lidar.from_las.transform(np.asarray(xs) + self.lean[0], np.asarray(ys) + self.lean[1])
        out = [self.mos.to_px(o, a) for o, a in zip(np.atleast_1d(lon), np.atleast_1d(lat))]
        return np.array(out).T  # (2, n): x, y

    def edge_strength(self, xs, ys, nx, ny):
        """|colour derivative across the edge| at LAS points, normal (nx, ny)."""
        px, py = self.px(xs, ys)
        gx = ndi.map_coordinates(self.gx, [py, px], order=1, mode="nearest")
        gy = ndi.map_coordinates(self.gy, [py, px], order=1, mode="nearest")
        # image y grows southward, LAS y northward
        return np.abs(gx * nx - gy * ny)


def trace_outline(sampler: PhotoSampler, outline: Polygon, is_roof=None, is_tree=None) -> tuple[Polygon, dict]:
    """Return the photo-traced outline (LAS CRS, true position) and stats.

    ``is_roof(xs, ys)`` (LiDAR: is there roof here?) is a safety net: an
    edge may not move inward past where the scan still shows roof, which
    stops dark roof-on-shadow sides from being cut off.

    ``is_tree(xs, ys)`` marks tree canopy above the roof. Where most of a
    side is under leaves, the photo edge is the crown, not the roof, so that
    side ignores the photo and follows the shift of its neighbouring sides
    (which usually carries the footprint's overall offset), like a side
    with no visible edge."""
    poly = outline.simplify(0.4, preserve_topology=True)
    if not isinstance(poly, Polygon):
        return outline, {"moved_edges": 0}
    ring = np.asarray(poly.exterior.coords)[:-1]
    if Polygon(ring).exterior.is_ccw is False:
        ring = ring[::-1]
    n = len(ring)
    offs = np.arange(-SEARCH_FT, SEARCH_FT + 1e-9, STEP_FT)
    moves = np.zeros(n)
    strong = np.zeros(n, bool)
    lens = np.zeros(n)
    treed = 0
    for i in range(n):
        p0, p1 = ring[i], ring[(i + 1) % n]
        d = p1 - p0
        L = float(np.hypot(*d))
        lens[i] = L
        if L < 3.0:
            continue
        u = d / L
        nrm = np.array([u[1], -u[0]])  # outward for a CCW ring
        ts = np.linspace(0.15, 0.85, max(6, int(L * 2)))
        base = p0 + np.outer(ts * L, u)
        hidden = False
        if is_tree is not None:
            band = np.vstack([base + o * nrm for o in (-2.0, 0.0, 2.0)])
            hidden = float(np.mean(is_tree(band[:, 0], band[:, 1]))) >= 0.4
            treed += int(hidden)
        scores = []
        for o in offs:
            pts = base + o * nrm
            s = sampler.edge_strength(pts[:, 0], pts[:, 1], nrm[0], nrm[1])
            # an edge has to run along most of the side, not just one spot
            scores.append(np.percentile(s, 40))
        scores = np.array(scores)
        scores = ndi.uniform_filter1d(scores, 3)
        # mild preference for small moves: the outline is already close
        weighted = scores * (1 - 0.04 * np.abs(offs))
        k = int(np.argmax(weighted))
        med = np.median(scores)
        if not hidden and scores[k] > 1.35 * med and scores[k] > 0:
            moves[i] = offs[k]
            strong[i] = True
        if is_roof is not None:
            # outermost offset that still has roof (LiDAR) just inside it
            floor = None
            for o in offs[::-1]:
                pts = base + (o - 1.0) * nrm
                if np.mean(is_roof(pts[:, 0], pts[:, 1])) >= 0.5:
                    floor = o - 0.5
                    break
            if floor is not None and moves[i] < floor:
                moves[i] = min(floor, SEARCH_FT)
                strong[i] = True
    # short or edge-less sides follow their neighbours
    for i in range(n):
        if not strong[i]:
            nb = [moves[j] for j in ((i - 1) % n, (i + 1) % n) if strong[j]]
            moves[i] = float(np.mean(nb)) if nb else 0.0
    # rebuild corners as intersections of moved edge lines
    lines = []
    for i in range(n):
        p0, p1 = ring[i], ring[(i + 1) % n]
        d = p1 - p0
        L = float(np.hypot(*d)) or 1.0
        u = d / L
        nrm = np.array([u[1], -u[0]])
        lines.append((p0 + moves[i] * nrm, u))
    new = []
    for i in range(n):
        (a, ua), (b, ub) = lines[i - 1], lines[i]
        cross = ua[0] * ub[1] - ua[1] * ub[0]
        if abs(cross) < 0.15:  # nearly parallel sides: average the shifts
            new.append(ring[i] + (a - ring[(i - 1) % n] + b - ring[i]) / 2)
            continue
        t = ((b[0] - a[0]) * ub[1] - (b[1] - a[1]) * ub[0]) / cross
        new.append(a + t * ua)
    traced = Polygon(new).buffer(0)
    if isinstance(traced, Polygon):  # merge corners closer than ~1 ft (cleaner editing)
        traced = traced.simplify(0.6, preserve_topology=True)
    ok = (isinstance(traced, Polygon) and traced.is_valid
          and 0.8 * outline.area < traced.area < 1.25 * outline.area
          and max(np.hypot(*(np.array(new) - ring).T)) < SEARCH_FT * 1.8)
    if not ok:
        return outline, {"moved_edges": 0, "rejected": True}
    return traced, {"moved_edges": int(strong.sum()), "edges": n, "tree_edges": treed,
                    "mean_move_ft": float(np.mean(np.abs(moves[strong]))) if strong.any() else 0.0}
