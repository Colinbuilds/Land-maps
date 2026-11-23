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
            bld = _LIDAR_DIR / (las_name.rsplit(".", 1)[0] + "_bld.npz")
            if not path.exists() or not bld.exists():
                with _build_sem:
                    if not _build(self.cfg["las_base_url"] + las_name, path):
                        return None
            z = np.load(path)
            t = {k: z[k] for k in z.files}
            if len(_mem) >= _MEM_MAX:
                _mem.pop(next(iter(_mem)))
            _mem[las_name] = t
            return t

    def building_points(self, env_wgs, bounds_las) -> np.ndarray:
        """Raw building-class LiDAR points (x, y, z in LAS feet) inside a
        LAS-CRS bounding box. Used to fit roof planes precisely."""
        minx, miny, maxx, maxy = bounds_las
        out = []
        for n in self.tiles_for(env_wgs):
            if self.tile(n) is None:
                continue
            key = n + "#bld"
            if key not in _mem:
                p = _LIDAR_DIR / (n.rsplit(".", 1)[0] + "_bld.npz")
                if not p.exists():
                    continue
                z = np.load(p)
                _mem[key] = {"xyz": z["xyz"], "x0": float(z["x0"]), "y0": float(z["y0"])}
                if len(_mem) >= _MEM_MAX * 2:
                    _mem.pop(next(iter(_mem)))
            t = _mem[key]
            xyz = t["xyz"]
            x = xyz[:, 0].astype(np.float64) + t["x0"]
            y = xyz[:, 1].astype(np.float64) + t["y0"]
            m = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
            if m.any():
                out.append(np.column_stack([x[m], y[m], xyz[m, 2].astype(np.float64)]))
        return np.vstack(out) if out else np.zeros((0, 3))

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
        bpts = []
        with laspy.open(tmp) as f:
            for pts in f.chunk_iterator(2_000_000):
                c = np.asarray(pts.classification)
                li = idx(pts)
                h = np.asarray(pts.z) - gz[li]
                m = np.isin(c, OBJ_CLASSES)
                np.maximum.at(obj, li[m], h[m])
                m = np.isin(c, VEG_CLASSES)
                np.maximum.at(veg, li[m], h[m])
                b = c == 6  # building: keep raw points for roof planes
                if b.any():
                    bpts.append(np.column_stack([np.asarray(pts.x)[b] - x0, np.asarray(pts.y)[b] - mins[1],
                                                 np.asarray(pts.z)[b]]).astype(np.float32))
        np.savez_compressed(
            out_path,
            obj=obj.reshape(H, W).astype(np.float16),
            veg=veg.reshape(H, W).astype(np.float16),
            gnd=np.clip(gcnt, 0, 255).reshape(H, W).astype(np.uint8),
            # mean ground-return intensity, /256 to fit uint8; 0 = no ground hit
            inten=np.clip(np.where(gcnt > 0, isum / np.maximum(gcnt, 1) / 256.0, 0), 0, 255).reshape(H, W).astype(np.uint8),
            x0=x0, y1=y1, cell=cell,
        )
        bld_path = str(out_path).replace(".npz", "_bld.npz")
        np.savez_compressed(bld_path, xyz=np.vstack(bpts) if bpts else np.zeros((0, 3), np.float32),
                            x0=x0, y0=float(mins[1]))
        return True
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class EptLidar:
    """USGS 3DEP LiDAR (EPT on public S3) behind the same interface as the
    county tile source: ``sample()`` height rasters for yards and
    ``building_points()`` for roofs, in the county's survey feet.

    3DEP classifies only ground (2) and "unclassified" (1), so:
      * roof / structure points = unclassified, single-return, above ground
      * vegetation = unclassified points from multi-return pulses
    The point cloud and the county's own maps can disagree by a few feet
    (datum handling), so the offset is measured per neighbourhood by sliding
    the roof points onto the county building outlines.
    """

    def __init__(self, cfg: dict, crs: str, footprints_fn=None):
        from pyproj import Transformer
        from .ept import EptProject
        self.cfg = cfg
        urls = cfg.get("urls") or [cfg["url"]]
        self.projects = [EptProject(u) for u in urls]
        self._fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        self._inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        self._3857_to_las = Transformer.from_crs("EPSG:3857", crs, always_xy=True)
        self._wgs_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        self.to_las = _Shifted(self._fwd, 0.0, 0.0, True)
        self.from_las = _Shifted(self._inv, 0.0, 0.0, False)
        self.footprints_fn = footprints_fn
        self._cache: dict = {}
        self._offsets: dict = {}
        self.datum_shift_ft = (0.0, 0.0)

    # ------------------------------------------------------------- raw points
    def _raw(self, env_wgs, pad_m: float = 6.0):
        """Points in a WGS84 box, in county feet (x, y, z) + class/returns."""
        key = tuple(round(v, 5) for v in env_wgs)
        if key in self._cache:
            return self._cache[key]
        x0, y0 = self._wgs_to_3857.transform(env_wgs[0], env_wgs[1])
        x1, y1 = self._wgs_to_3857.transform(env_wgs[2], env_wgs[3])
        parts = [p.points((x0 - pad_m, y0 - pad_m, x1 + pad_m, y1 + pad_m)) for p in self.projects if p.ok]
        parts = [q for q in parts if len(q["x"])]
        if not parts:
            return None
        r = {k: np.concatenate([q[k] for q in parts]) for k in parts[0]}
        X, Y = self._3857_to_las.transform(r["x"], r["y"])
        r["x"], r["y"] = np.asarray(X), np.asarray(Y)
        r["z"] = r["z"] / 0.3048006096  # metres -> US survey feet
        dx, dy = self._offset(env_wgs, r)
        r["x"] = r["x"] + dx
        r["y"] = r["y"] + dy
        if len(self._cache) > 16:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = r
        return r

    def _ground_z(self, r, xs, ys):
        g = r["cls"] == 2
        if g.sum() < 3:
            return np.full(np.shape(xs), np.nan)
        from scipy.spatial import cKDTree
        t = cKDTree(np.column_stack([r["x"][g], r["y"][g]]))
        d, i = t.query(np.column_stack([np.ravel(xs), np.ravel(ys)]), k=min(6, int(g.sum())))
        z = r["z"][g][i]
        return np.median(z, axis=1).reshape(np.shape(xs))

    def _offset(self, env_wgs, r):
        """Shift (ft) that best puts above-ground single-return points inside
        the county building outlines. Cached per ~0.02 deg cell."""
        c = ((env_wgs[0] + env_wgs[2]) / 2, (env_wgs[1] + env_wgs[3]) / 2)
        cell = (round(c[0] / 0.02), round(c[1] / 0.02))
        if cell in self._offsets:
            return self._offsets[cell]
        best = (0.0, 0.0)
        try:
            polys = self.footprints_fn(env_wgs) if self.footprints_fn else []
            if polys:
                import shapely
                from shapely.ops import unary_union
                fp = unary_union(polys)
                h = r["z"] - self._ground_z(r, r["x"], r["y"])
                m = (r["cls"] != 2) & (r["nret"] <= 1) & (h > 8)
                px, py = r["x"][m], r["y"][m]
                if len(px) > 50:
                    def score(dx, dy):
                        return shapely.contains_xy(fp, px + dx, py + dy).mean()
                    top = (score(0, 0), 0.0, 0.0)
                    for dx in np.arange(-14, 14.01, 1.0):
                        for dy in np.arange(-14, 14.01, 1.0):
                            s_ = score(dx, dy)
                            if s_ > top[0]:
                                top = (s_, dx, dy)
                    _, bx, by = top
                    for dx in np.arange(bx - 1, bx + 1.01, 0.25):
                        for dy in np.arange(by - 1, by + 1.01, 0.25):
                            s_ = score(dx, dy)
                            if s_ > top[0]:
                                top = (s_, dx, dy)
                    best = (float(top[1]), float(top[2]))
        except Exception:
            pass
        self._offsets[cell] = best
        self.datum_shift_ft = best
        return best

    # ------------------------------------------------------------ interface
    def building_points(self, env_wgs, bounds_las) -> np.ndarray:
        r = self._raw(env_wgs)
        if r is None:
            return np.zeros((0, 3))
        minx, miny, maxx, maxy = bounds_las
        m = (r["x"] >= minx) & (r["x"] <= maxx) & (r["y"] >= miny) & (r["y"] <= maxy) & (r["cls"] != 2)
        if (r["cls"] == 6).any():
            m &= r["cls"] == 6
        else:
            h = r["z"] - self._ground_z(r, r["x"], r["y"])
            m &= (h > 6) & (r["nret"] <= 1) & (r["cls"] != 7) & (r["cls"] < 17)
            m &= ~self._tree_single(r, m, h)
        return np.column_stack([r["x"][m], r["y"][m], r["z"][m]])

    @staticmethod
    def _tree_single(r, cand, h):
        """Single-return points that are really branches.

        USGS scans here only separate ground from everything else, and a
        dense or leaf-off crown often sends back a single echo, which then
        looks like roof. A branch point is both rough (its neighbours don't
        lie on a plane) and next to multi-echo pulses (tree). Roof points
        pass one test or the other: ridges are rough but have no multi-echo
        neighbours, eave edges have multi-echo neighbours but are planar."""
        from scipy.spatial import cKDTree
        out = np.zeros(len(r["x"]), bool)
        ci = np.nonzero(cand)[0]
        multi = (r["cls"] != 2) & (r["nret"] > 1) & (h > 6)
        if len(ci) < 12 or not multi.any():
            return out
        P = np.column_stack([r["x"][ci], r["y"][ci], r["z"][ci]])
        M = np.column_stack([r["x"][multi], r["y"][multi], r["z"][multi]])
        near = cKDTree(M).query_ball_point(P, 4.0, return_length=True)
        sel = np.nonzero(near >= 2)[0]
        if not len(sel):
            return out
        k = min(10, len(ci))
        _, nb = cKDTree(P).query(P[sel], k=k)
        Q = P[nb] - P[nb].mean(axis=1, keepdims=True)  # (s, k, 3)
        cov = np.einsum("ski,skj->sij", Q, Q) / k
        rms = np.sqrt(np.clip(np.linalg.eigvalsh(cov)[:, 0], 0, None))  # off-plane spread
        out[ci[sel[rms > 0.6]]] = True
        return out

    def tree_points(self, env_wgs, bounds_las) -> np.ndarray:
        """Tree returns (x, y, z) in a LAS-CRS box: multi-echo pulses and
        single echoes flagged as branches, more than 6 ft above ground."""
        r = self._raw(env_wgs)
        if r is None:
            return np.zeros((0, 3))
        minx, miny, maxx, maxy = bounds_las
        box = (r["x"] >= minx) & (r["x"] <= maxx) & (r["y"] >= miny) & (r["y"] <= maxy)
        h = r["z"] - self._ground_z(r, r["x"], r["y"])
        ok = box & (r["cls"] != 2) & (r["cls"] != 7) & (r["cls"] < 17) & (h > 6)
        if (r["cls"] == 6).any():
            t = ok & (r["cls"] != 6)
        else:
            single = ok & (r["nret"] <= 1)
            t = (ok & (r["nret"] > 1)) | (single & self._tree_single(r, single, h))
        return np.column_stack([r["x"][t], r["y"][t], r["z"][t]])

    def sample(self, env_wgs, xs_las: np.ndarray, ys_las: np.ndarray) -> dict | None:
        """obj / veg heights and ground density, sampled at LAS-CRS points
        (same contract as the county tile source). USGS data is sparse
        (a point every 1.5-3 ft), so the grid is 2 ft, the ground comes from
        the nearest ground returns, and anything under 1.5 ft is ground noise."""
        from scipy.spatial import cKDTree
        r = self._raw(env_wgs)
        if r is None or len(r["x"]) < 50:
            return None
        cell = 2.0
        x0, y1 = np.floor(xs_las.min()) - 4, np.ceil(ys_las.max()) + 4
        W = int(np.ceil((xs_las.max() - x0) / cell)) + 4
        H = int(np.ceil((y1 - ys_las.min()) / cell)) + 4
        g = r["cls"] == 2
        if g.sum() < 10:
            return None
        cx = x0 + (np.arange(W) + 0.5) * cell
        cy = y1 - (np.arange(H) + 0.5) * cell
        CX, CY = np.meshgrid(cx, cy)
        t = cKDTree(np.column_stack([r["x"][g], r["y"][g]]))
        _, gi = t.query(np.column_stack([CX.ravel(), CY.ravel()]), k=min(6, int(g.sum())))
        gz = np.median(r["z"][g][gi], axis=1)  # (H*W,)
        ci = np.clip(((r["x"] - x0) / cell).astype(int), 0, W - 1)
        ri = np.clip(((y1 - r["y"]) / cell).astype(int), 0, H - 1)
        lin = ri * W + ci
        n = W * H
        h = r["z"] - gz[lin]
        nong = ~g & (r["cls"] != 7) & (r["cls"] < 17) & (h > 1.5)
        single = nong & (r["nret"] <= 1)
        multi = nong & (r["nret"] > 1)
        obj = np.full(n, -1.0)
        veg = np.full(n, -1.0)
        np.maximum.at(obj, lin[single], h[single])
        np.maximum.at(veg, lin[multi], h[multi])
        gc = np.bincount(lin[g], None, n).reshape(H, W)
        gnd = np.clip(ndi.uniform_filter(gc.astype(float), 5) * 25, 0, 255)  # ground hits within ~10 ft
        obj, veg = obj.reshape(H, W), veg.reshape(H, W)
        c = np.floor((xs_las - x0) / cell).astype(int)
        rr = np.floor((y1 - ys_las) / cell).astype(int)
        ok = (c >= 0) & (c < W) & (rr >= 0) & (rr < H)
        out = {"obj": np.full(xs_las.shape, -1.0, np.float32), "veg": np.full(xs_las.shape, -1.0, np.float32),
               "gnd": np.zeros(xs_las.shape, np.int16), "inten": None, "covered": ok, "sparse": True}
        # bare winter branches often give a single return and look solid:
        # anything within ~3 ft of a multi-return (tree) hit is tree, not deck
        treeish = ndi.binary_dilation(veg > 2.0, iterations=2)
        obj = np.where(treeish & (obj < 12), -1.0, obj)
        out["obj"][ok] = obj[rr[ok], c[ok]]
        out["veg"][ok] = veg[rr[ok], c[ok]]
        out["gnd"][ok] = gnd[rr[ok], c[ok]]
        return out
