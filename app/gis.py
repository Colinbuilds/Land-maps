"""Thin client for public ArcGIS REST services, with an on-disk cache.

Everything comes back as shapely geometries in the county's projected CRS
(feet), so areas are square feet and lengths are feet.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import httpx
from pyproj import Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

CACHE_DIR = Path(os.environ.get("ESTIMATOR_CACHE", Path(__file__).resolve().parent.parent / ".cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_client = httpx.Client(timeout=60, headers={"User-Agent": "property-estimator/0.1"})
_lock = threading.Lock()


def _cache_path(kind: str, key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()
    d = CACHE_DIR / kind
    d.mkdir(exist_ok=True)
    return d / h


def get_json(url: str, params: dict | None = None, method: str = "GET", ttl: float = 7 * 86400) -> dict:
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    p = _cache_path("json", key)
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    last = None
    for attempt in range(4):
        try:
            if method == "POST":
                r = _client.post(url, data=params)
            else:
                r = _client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error from {url}: {data['error']}")
            p.write_text(json.dumps(data))
            return data
        except Exception as e:  # network blips are common on county servers
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed: {url}: {last}")


def get_bytes(url: str, ttl: float = 90 * 86400) -> bytes | None:
    p = _cache_path("bin", url)
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        return p.read_bytes()
    for attempt in range(3):
        try:
            r = _client.get(url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            p.write_bytes(r.content)
            return r.content
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None


class Projector:
    """WGS84 <-> county feet."""

    def __init__(self, crs: str):
        self.fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        self.inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    def to_local(self, geom):
        return shp_transform(lambda x, y, z=None: self.fwd.transform(x, y), geom)

    def to_wgs(self, geom):
        return shp_transform(lambda x, y, z=None: self.inv.transform(x, y), geom)


def _esri_to_geojson_geom(g: dict) -> dict | None:
    if not g:
        return None
    if "rings" in g:
        # Esri rings: outer rings clockwise, holes counter-clockwise.
        from shapely.geometry import Polygon, MultiPolygon, LinearRing
        outers, holes = [], []
        for ring in g["rings"]:
            ring2 = [tuple(c[:2]) for c in ring]
            if len(ring2) < 4:
                continue
            (outers if not LinearRing(ring2).is_ccw else holes).append(ring2)
        polys = [Polygon(o) for o in outers]
        for h in holes:
            hp = Polygon(h)
            for i, p in enumerate(polys):
                if p.contains(hp.representative_point()):
                    polys[i] = Polygon(p.exterior.coords, list(p.interiors) + [h])
                    break
        geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        return mapping(geom.buffer(0))
    if "paths" in g:
        paths = [[tuple(c[:2]) for c in p] for p in g["paths"]]
        return {"type": "MultiLineString", "coordinates": paths}
    if "x" in g:
        return {"type": "Point", "coordinates": [g["x"], g["y"]]}
    return None


def query_features(layer_url: str, *, where: str = "1=1", envelope=None, geometry=None,
                   out_fields: str = "*", return_geometry: bool = True, max_records: int = 1000,
                   order_by: str | None = None, limit: int | None = None) -> list[dict]:
    """Query a layer, paging through results. Geometry is returned in WGS84.

    ``envelope`` is (xmin, ymin, xmax, ymax) in WGS84. Returns a list of
    {"attributes": {...}, "geometry": shapely geom (WGS84) or None}.
    """
    base = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "outSR": 4326,
        "f": "json",
        "geometryPrecision": 7,
    }
    if envelope is not None:
        base.update({
            "geometry": ",".join(f"{v:.7f}" for v in envelope),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
        })
    if geometry is not None:
        base.update(geometry)
    if order_by:
        base["orderByFields"] = order_by
    out: list[dict] = []
    offset = 0
    while True:
        params = dict(base)
        page = max_records if limit is None else min(max_records, limit - len(out))
        params["resultOffset"] = offset
        params["resultRecordCount"] = page
        data = get_json(layer_url + "/query", params, method="POST")
        feats = data.get("features", [])
        for f in feats:
            gj = _esri_to_geojson_geom(f.get("geometry")) if return_geometry else None
            out.append({"attributes": f.get("attributes", {}), "geometry": shape(gj) if gj else None})
        offset += len(feats)
        if not feats or not data.get("exceededTransferLimit") or (limit and len(out) >= limit):
            break
    return out


def count_features(layer_url: str, where: str) -> int:
    data = get_json(layer_url + "/query", {"where": where, "returnCountOnly": "true", "f": "json"}, method="POST")
    return int(data.get("count", 0))


class ChunkedPolygonLayer:
    """Layers like LiDAR pavement come as a few hundred enormous multipart
    polygons. Download each chunk once, cache it, and keep a spatial index."""

    def __init__(self, layer_url: str, projector: Projector):
        self.url = layer_url
        self.proj = projector
        self._geoms: dict[int, object] = {}
        self._lock = threading.Lock()

    def _ids_for(self, envelope) -> list[int]:
        data = get_json(self.url + "/query", {
            "geometry": ",".join(f"{v:.7f}" for v in envelope),
            "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "returnIdsOnly": "true", "f": "json",
        }, method="POST")
        return data.get("objectIds") or []

    def _load(self, oid: int):
        p = _cache_path("chunks", f"{self.url}#{oid}")
        if p.exists():
            gj = json.loads(p.read_text())
        else:
            feats = query_features(self.url, where=f"objectid={oid}", out_fields="objectid")
            gj = mapping(feats[0]["geometry"]) if feats and feats[0]["geometry"] is not None else None
            p.write_text(json.dumps(gj))
        if gj is None:
            return None
        g = self.proj.to_local(shape(gj))
        # split into parts so the index stays useful
        parts = list(getattr(g, "geoms", [g]))
        return STRtree(parts), parts

    def get_local(self, envelope_wgs, clip_local=None):
        """Return the local-CRS polygons intersecting ``clip_local``."""
        out = []
        for oid in self._ids_for(envelope_wgs):
            with self._lock:
                if oid not in self._geoms:
                    self._geoms[oid] = self._load(oid)
            entry = self._geoms[oid]
            if not entry:
                continue
            tree, parts = entry
            for i in tree.query(clip_local):
                g = parts[i]
                if g.intersects(clip_local):
                    out.append(g.intersection(clip_local))
        return out


# ------------------------------------------------------------ cache upkeep
_PRUNE_DIRS = ("bin", "lidar", "json", "chunks", "ept", "lean")
_cache_info: dict = {"bytes": 0, "limit": 0, "pruned": 0, "at": 0}


def cache_limit_bytes() -> int:
    """How big the download cache may grow: CACHE_MAX_GB, else 40% of the
    disk (at most 40 GB). Measured sheets and orders live elsewhere and are
    never touched."""
    env = os.environ.get("CACHE_MAX_GB")
    if env:
        try:
            return int(float(env) * 1e9)
        except ValueError:
            pass
    import shutil
    try:
        total = shutil.disk_usage(str(CACHE_DIR)).total
    except Exception:
        total = 20e9
    return int(min(40e9, total * 0.4))


def prune_cache() -> dict:
    """Delete the oldest cached downloads (map tiles, LiDAR, county answers)
    once the cache is over its limit, down to 75% of it. Anything deleted is
    simply downloaded again if it's needed."""
    files = []
    for d in _PRUNE_DIRS:
        root = CACHE_DIR / d
        if not root.exists():
            continue
        for dirpath, _, names in os.walk(root):
            for nm in names:
                fp = os.path.join(dirpath, nm)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, fp))
    size = sum(f[1] for f in files)
    limit = cache_limit_bytes()
    freed = 0
    if size > limit:
        files.sort()
        target = size - int(limit * 0.75)
        for _, sz, fp in files:
            if freed >= target:
                break
            try:
                os.remove(fp)
                freed += sz
            except OSError:
                pass
    _cache_info.update(bytes=size - freed, limit=limit, pruned=_cache_info["pruned"] + freed, at=time.time())
    return dict(_cache_info)


def start_cache_pruner(every_s: int = 900) -> None:
    import threading

    def loop():
        while True:
            try:
                prune_cache()
            except Exception:
                pass
            time.sleep(every_s)
    threading.Thread(target=loop, daemon=True, name="cache-pruner").start()


def cache_info() -> dict:
    return dict(_cache_info)
