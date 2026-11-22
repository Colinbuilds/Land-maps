"""Height-above-ground rasters from the county's classified LiDAR point cloud.

Each LAS tile (about 1,500 x 1,500 ft, ~6M points) is downloaded once and
reduced to small 1-ft rasters, cached as .npz, and the LAS file is deleted:

  obj   max height above ground of non-vegetation objects (ASPRS classes 1, 6, 19):
        decks, sheds, play sets, trampolines, pergolas, fences, parked cars
  veg   max height above ground of vegetation (classes 3, 4, 5)
  gnd   number of ground returns (0 under dense evergreens / solid roofs)

Heights are in feet.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

import numpy as np
from pyproj import Transformer
from scipy import ndimage as ndi

from .gis import CACHE_DIR, _client, query_features

_LIDAR_DIR = CACHE_DIR / "lidar"
_LIDAR_DIR.mkdir(parents=True, exist_ok=True)
_build_sem = threading.Semaphore(2)  # each build peaks around 0.5 GB of RAM
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_mem: dict[str, dict] = {}
_MEM_MAX = 24

OBJ_CLASSES = (1, 6, 19)
VEG_CLASSES = (3, 4, 5)
GROUND_CLASSES = (2, 11)


class _Shifted:
    """A pyproj-like ``transform(x, y)`` with a constant offset added (or
    removed) on the LAS side."""

    def __init__(self, tr, dx, dy, forward: bool):
        self.tr, self.dx, self.dy, self.forward = tr, dx, dy, forward

    def transform(self, x, y):
        if self.forward:
            X, Y = self.tr.transform(x, y)
            return X + self.dx, Y + self.dy
        return self.tr.transform(x - self.dx, y - self.dy)


class LidarSource:
    """Note on datums: when the county's ArcGIS servers hand out coordinates
    as WGS84 they apply a NAD83(2011)->WGS84 shift (~3.4 ft here). The LiDAR is
    plain NAD83(2011). We measure that shift once against the server and
    correct for it, so outlines, parcels and imagery line up with the LiDAR."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._fwd = Transformer.from_crs("EPSG:4326", cfg["crs_wkt"], always_xy=True)
        self._inv = Transformer.from_crs(cfg["crs_wkt"], "EPSG:4326", always_xy=True)
        dx, dy = self._calibrate()
        self.datum_shift_ft = (dx, dy)
        self.to_las = _Shifted(self._fwd, dx, dy, True)
        self.from_las = _Shifted(self._inv, dx, dy, False)

    def _calibrate(self) -> tuple[float, float]:
        cal = self.cfg.get("calibration")
        if not cal:
            return 0.0, 0.0
        try:
            from .gis import get_json
            q = {"where": cal["where"], "outFields": "*", "returnGeometry": "true", "f": "json",
                 "resultRecordCount": 1}
            wgs = get_json(cal["url"] + "/query", {**q, "outSR": 4326}, ttl=30 * 86400)
            nat = get_json(cal["url"] + "/query", {**q, "outSR": json.dumps({"wkt": self.cfg["crs_wkt"]})},
                           ttl=30 * 86400)
            a = wgs["features"][0]["geometry"]["rings"][0][0]
            b = nat["features"][0]["geometry"]["rings"][0][0]
            X, Y = self._fwd.transform(a[0], a[1])
            dx, dy = b[0] - X, b[1] - Y
            if abs(dx) < 20 and abs(dy) < 20:
                return float(dx), float(dy)
        except Exception:
            pass
        return tuple(cal.get("fallback_ft", (0.0, 0.0)))

    def tiles_for(self, env_wgs) -> list[str]:
        feats = query_features(self.cfg["index_url"], envelope=env_wgs, out_fields=self.cfg["las_field"],
                               return_geometry=False)
        return sorted({f["attributes"][self.cfg["las_field"]] for f in feats if f["attributes"].get(self.cfg["las_field"])})

    def tile(self, las_name: str) -> dict | None:
        if las_name in _mem:
            return _mem[las_name]
        with _locks_guard:
            lk = _locks.setdefault(las_name, threading.Lock())
        with lk:
            if las_name in _mem:
                return _mem[las_name]
            path = _LIDAR_DIR / (las_name.rsplit(".", 1)[0] + ".npz")
            if not path.exists():
                with _build_sem:
                    if not _build(self.cfg["las_base_url"] + las_name, path):
                        return None
            z = np.load(path)
            t = {k: z[k] for k in z.files}
            if len(_mem) >= _MEM_MAX:
                _mem.pop(next(iter(_mem)))
            _mem[las_name] = t
            return t

    def sample(self, env_wgs, xs_las: np.ndarray, ys_las: np.ndarray) -> dict | None:
        """Sample obj/veg/gnd at LAS-CRS coordinates (arrays of any shape)."""
        names = self.tiles_for(env_wgs)
        if not names:
            return None
        out = {"obj": np.full(xs_las.shape, -1.0, np.float32),
               "veg": np.full(xs_las.shape, -1.0, np.float32),
               "gnd": np.zeros(xs_las.shape, np.int16),
               "inten": np.zeros(xs_las.shape, np.int16),
               "covered": np.zeros(xs_las.shape, bool)}
        for n in names:
            t = self.tile(n)
            if t is None:
                continue
            x0, y1, cell = float(t["x0"]), float(t["y1"]), float(t["cell"])
            H, W = t["obj"].shape
            ci = np.floor((xs_las - x0) / cell).astype(int)
            ri = np.floor((y1 - ys_las) / cell).astype(int)
            ok = (ci >= 0) & (ci < W) & (ri >= 0) & (ri < H) & ~out["covered"]
            out["obj"][ok] = t["obj"][ri[ok], ci[ok]]
            out["veg"][ok] = t["veg"][ri[ok], ci[ok]]
            out["gnd"][ok] = t["gnd"][ri[ok], ci[ok]]
            if "inten" in t:
                out["inten"][ok] = t["inten"][ri[ok], ci[ok]]
            out["covered"] |= ok
        return out if out["covered"].any() else None


def _build(url: str, out_path) -> bool:
    import laspy

    fd, tmp = tempfile.mkstemp(suffix=".las", dir=_LIDAR_DIR)
    os.close(fd)
    try:
        with _client.stream("GET", url, timeout=600) as r:
            if r.status_code != 200:
                return False
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        cell = 1.0
        with laspy.open(tmp) as f:
            mins, maxs = f.header.mins, f.header.maxs
        x0, y1 = np.floor(mins[0]), np.ceil(maxs[1])
        W = int(np.ceil(maxs[0] - x0) / cell) + 1
        H = int(np.ceil(y1 - mins[1]) / cell) + 1
        n = W * H

        def idx(pts):
            ci = np.clip(((np.asarray(pts.x) - x0) / cell).astype(np.int64), 0, W - 1)
            ri = np.clip(((y1 - np.asarray(pts.y)) / cell).astype(np.int64), 0, H - 1)
            return ri * W + ci

        # pass 1: ground surface
        gsum = np.zeros(n)
        gcnt = np.zeros(n)
        isum = np.zeros(n)
        with laspy.open(tmp) as f:
            for pts in f.chunk_iterator(2_000_000):
                c = np.asarray(pts.classification)
                g = np.isin(c, GROUND_CLASSES)
                li = idx(pts)[g]
                gsum += np.bincount(li, np.asarray(pts.z)[g], n)
                gcnt += np.bincount(li, None, n)
                isum += np.bincount(li, np.asarray(pts.intensity, dtype=np.float64)[g], n)
        gz = np.where(gcnt > 0, gsum / np.maximum(gcnt, 1), np.nan).reshape(H, W)
        holes = np.isnan(gz)
        if holes.all():
            return False
        near = ndi.distance_transform_edt(holes, return_distances=False, return_indices=True)
        gz = gz[tuple(near)].ravel()
        # pass 2: heights above ground
        obj = np.full(n, -1.0)
        veg = np.full(n, -1.0)
        with laspy.open(tmp) as f:
            for pts in f.chunk_iterator(2_000_000):
                c = np.asarray(pts.classification)
                li = idx(pts)
                h = np.asarray(pts.z) - gz[li]
                m = np.isin(c, OBJ_CLASSES)
                np.maximum.at(obj, li[m], h[m])
                m = np.isin(c, VEG_CLASSES)
                np.maximum.at(veg, li[m], h[m])
        np.savez_compressed(
            out_path,
            obj=obj.reshape(H, W).astype(np.float16),
            veg=veg.reshape(H, W).astype(np.float16),
            gnd=np.clip(gcnt, 0, 255).reshape(H, W).astype(np.uint8),
            # mean ground-return intensity, /256 to fit uint8; 0 = no ground hit
            inten=np.clip(np.where(gcnt > 0, isum / np.maximum(gcnt, 1) / 256.0, 0), 0, 255).reshape(H, W).astype(np.uint8),
            x0=x0, y1=y1, cell=cell,
        )
        return True
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
