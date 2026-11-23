"""Tree canopy over roofs.

A tree hanging over a roof hides it twice: in the photo (leaves cover the
roof edge) and in the scan (branches above the shingles). This module finds
where that happens, on a 1-ft grid in the LiDAR CRS, so the outline tracer
can leave those edges alone and the report can say how much of the roof was
hidden.

A cell is "tree over roof" when vegetation in it stands more than ~1.5-2 ft
above the roof (or, off the roof, above the nearest roof height). Comparing
with the roof is what keeps eave edges out: pulses that clip a gutter come
back as multi-echo "vegetation" at roof height, but never above it.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi
from shapely.geometry import box
from shapely.ops import unary_union

CELL = 1.0


class TreeGrid:
    def __init__(self, mask: np.ndarray, x0: float, y1: float, cell: float = CELL):
        self.mask, self.x0, self.y1, self.cell = mask, x0, y1, cell

    def at(self, xs, ys) -> np.ndarray:
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        H, W = self.mask.shape
        c = np.floor((xs - self.x0) / self.cell).astype(int)
        r = np.floor((self.y1 - ys) / self.cell).astype(int)
        ok = (c >= 0) & (c < W) & (r >= 0) & (r < H)
        out = np.zeros(xs.shape, bool)
        out[ok] = self.mask[r[ok], c[ok]]
        return out

    def polygon(self, clip=None):
        """Mask as a (simplified) polygon, optionally clipped."""
        boxes = []
        for r in range(self.mask.shape[0]):
            row = self.mask[r]
            if not row.any():
                continue
            d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
            y_top = self.y1 - r * self.cell
            for a, b in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]):
                boxes.append(box(self.x0 + a * self.cell, y_top - self.cell, self.x0 + b * self.cell, y_top))
        if not boxes:
            return None
        g = unary_union(boxes).buffer(1.0).buffer(-1.0).simplify(0.8, preserve_topology=True)
        if clip is not None:
            g = g.intersection(clip)
        return None if g.is_empty else g


def tree_grid(lidar, env_wgs, region, roof_pts: np.ndarray | None = None) -> TreeGrid | None:
    """Tree-above-roof cells over ``region`` (a LAS-CRS geometry)."""
    minx, miny, maxx, maxy = region.bounds
    x0, y1 = np.floor(minx), np.ceil(maxy)
    W = int(np.ceil((maxx - x0) / CELL)) + 1
    H = int(np.ceil((y1 - miny) / CELL)) + 1
    if W * H > 600_000:
        return None
    mask = np.zeros((H, W), bool)
    if hasattr(lidar, "tree_points"):
        # USGS points: compare each tree point with the roof right around it
        from scipy.spatial import cKDTree
        T = lidar.tree_points(env_wgs, (minx, miny, maxx, maxy))
        if roof_pts is None:
            roof_pts = lidar.building_points(env_wgs, (minx - 10, miny - 10, maxx + 10, maxy + 10))
        if len(T) == 0:
            return TreeGrid(mask, x0, y1)
        if len(roof_pts) >= 3:
            kd = cKDTree(roof_pts[:, :2])
            d, i = kd.query(T[:, :2], k=min(8, len(roof_pts)), distance_upper_bound=10.0)
            zz = np.where(np.isfinite(d), roof_pts[np.minimum(i, len(roof_pts) - 1), 2], -np.inf)
            ref = zz.max(axis=1)  # highest roof nearby; -inf = no roof within 10 ft
            # far from any roof a tree can't hide one: ignore it
            above = np.isfinite(ref) & (T[:, 2] > ref + 2.0)
        else:
            above = np.zeros(len(T), bool)
        T = T[above]
        c = np.floor((T[:, 0] - x0) / CELL).astype(int)
        r = np.floor((y1 - T[:, 1]) / CELL).astype(int)
        ok = (c >= 0) & (c < W) & (r >= 0) & (r < H)
        mask[r[ok], c[ok]] = True
        # points are 1.5-3 ft apart: grow each hit into the crown it samples
        mask = ndi.binary_dilation(mask, iterations=2)
        mask = ndi.binary_closing(mask, iterations=2)
        mask = ndi.binary_erosion(mask, iterations=1)
    else:
        # county rasters: vegetation and roof are separate classes already
        xs = x0 + (np.arange(W) + 0.5) * CELL
        ys = y1 - (np.arange(H) + 0.5) * CELL
        XX, YY = np.meshgrid(xs, ys)
        smp = lidar.sample(env_wgs, XX, YY)
        if smp is None:
            return None
        veg = np.nan_to_num(np.asarray(smp["veg"], np.float32), nan=-1)
        obj = np.nan_to_num(np.asarray(smp["obj"], np.float32), nan=-1)
        ref = ndi.maximum_filter(obj, size=9)  # roof height within ~4 ft
        mask = (veg > 6.0) & (veg > ref + 1.5)
        mask = ndi.binary_closing(mask, iterations=1)
    mask = ndi.binary_opening(mask, iterations=1)
    return TreeGrid(mask, x0, y1)
