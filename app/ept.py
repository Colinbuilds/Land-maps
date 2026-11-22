"""Read USGS 3DEP LiDAR (Entwine Point Tiles on public S3) for a small area.

EPT stores a project as an octree of LAZ files. To get every point in a box we
walk the hierarchy and download only the nodes whose cube overlaps the box,
at every depth (EPT spreads a project's points across all levels). Files are
cached on disk, so neighbouring lookups are fast.
"""
from __future__ import annotations

import io
import json
import threading

import numpy as np

from .gis import CACHE_DIR, get_bytes, get_json

_DIR = CACHE_DIR / "ept"
_DIR.mkdir(parents=True, exist_ok=True)
_meta: dict = {}
_hier: dict = {}
_lock = threading.Lock()


class EptProject:
    def __init__(self, url: str):
        self.url = url.rsplit("/", 1)[0]  # .../<project>
        with _lock:
            if url not in _meta:
                _meta[url] = get_json(url, ttl=90 * 86400)
        m = _meta[url]
        self.bounds = m["bounds"]  # cube xmin ymin zmin xmax ymax zmax
        self.srs = (m.get("srs") or {}).get("horizontal") or "3857"
        self.ok = m.get("dataType") == "laszip"

    def _hierarchy(self, key: str) -> dict:
        k = self.url + "#" + key
        with _lock:
            if k not in _hier:
                _hier[k] = get_json(f"{self.url}/ept-hierarchy/{key}.json", ttl=90 * 86400)
            return _hier[k]

    def points(self, box_xy, max_nodes: int = 400) -> dict:
        """All points with x,y inside ``box_xy`` (xmin, ymin, xmax, ymax) in
        the project's CRS. Returns arrays x, y, z, cls, inten."""
        import laspy

        bx0, by0, bx1, by1 = box_xy
        X0, Y0, Z0, X1, Y1, Z1 = self.bounds
        todo = [("0-0-0-0", self._hierarchy("0-0-0-0"))]
        keys = []
        while todo and len(keys) < max_nodes:
            root_key, hier = todo.pop()
            stack = [root_key]
            while stack:
                key = stack.pop()
                cnt = hier.get(key)
                if cnt is None:
                    continue
                d, x, y, z = map(int, key.split("-"))
                n = 2 ** d
                sx, sy = (X1 - X0) / n, (Y1 - Y0) / n
                nx0, ny0 = X0 + x * sx, Y0 + y * sy
                if nx0 > bx1 or nx0 + sx < bx0 or ny0 > by1 or ny0 + sy < by0:
                    continue
                if cnt == -1:  # sub-hierarchy in its own file
                    todo.append((key, self._hierarchy(key)))
                    continue
                if cnt > 0:
                    keys.append(key)
                for cx in (0, 1):
                    for cy in (0, 1):
                        for cz in (0, 1):
                            stack.append(f"{d + 1}-{2 * x + cx}-{2 * y + cy}-{2 * z + cz}")
        out = {k: [] for k in ("x", "y", "z", "cls", "inten", "nret")}
        for key in keys:
            data = get_bytes(f"{self.url}/ept-data/{key}.laz", ttl=365 * 86400)
            if not data:
                continue
            try:
                las = laspy.read(io.BytesIO(data))
            except Exception:
                continue
            x = np.asarray(las.x)
            y = np.asarray(las.y)
            m = (x >= bx0) & (x <= bx1) & (y >= by0) & (y <= by1)
            if not m.any():
                continue
            out["x"].append(x[m])
            out["y"].append(y[m])
            out["z"].append(np.asarray(las.z)[m])
            out["cls"].append(np.asarray(las.classification)[m])
            out["inten"].append(np.asarray(las.intensity)[m])
            out["nret"].append(np.asarray(las.number_of_returns)[m])
        return {k: (np.concatenate(v) if v else np.zeros(0)) for k, v in out.items()}
