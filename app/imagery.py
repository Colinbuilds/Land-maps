"""Fetch and mosaic orthoimagery tiles, and rasterize geometries onto them."""
from __future__ import annotations

import io
import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageDraw

from .gis import get_bytes

_pool = ThreadPoolExecutor(max_workers=16)


def _lonlat_to_global_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2 ** z * 256
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


class Mosaic:
    """An RGB image covering a WGS84 bbox at a given zoom, plus helpers to
    map geometries (in WGS84) into pixel space."""

    def __init__(self, tile_url: str, bbox_wgs, zoom: int):
        self.z = zoom
        minx, miny, maxx, maxy = bbox_wgs
        X0, Y0 = _lonlat_to_global_px(minx, maxy, zoom)
        X1, Y1 = _lonlat_to_global_px(maxx, miny, zoom)
        self.tx0, self.ty0 = int(X0 // 256), int(Y0 // 256)
        tx1, ty1 = int(X1 // 256), int(Y1 // 256)
        w, h = (tx1 - self.tx0 + 1) * 256, (ty1 - self.ty0 + 1) * 256
        img = Image.new("RGB", (w, h), (0, 0, 0))
        jobs = {}
        for ty in range(self.ty0, ty1 + 1):
            for tx in range(self.tx0, tx1 + 1):
                url = tile_url.format(z=zoom, y=ty, x=tx)
                jobs[(tx, ty)] = _pool.submit(get_bytes, url)
        self.missing = 0
        for (tx, ty), fut in jobs.items():
            data = fut.result()
            if not data:
                self.missing += 1
                continue
            try:
                tile = Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:
                self.missing += 1
                continue
            img.paste(tile, ((tx - self.tx0) * 256, (ty - self.ty0) * 256))
        self.image = img
        self.rgb = np.asarray(img).astype(np.float32) / 255.0
        lat = (miny + maxy) / 2
        m_per_px = 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)
        self.ft_per_px = m_per_px / 0.3048
        self.sqft_per_px = self.ft_per_px ** 2

    @property
    def bounds_wgs(self):
        """(west, south, east, north) of the full mosaic."""
        h, w = self.shape
        n = 2 ** self.z * 256

        def lonlat(X, Y):
            lon = X / n * 360.0 - 180.0
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * Y / n))))
            return lon, lat

        w_, n_ = lonlat(self.tx0 * 256, self.ty0 * 256)
        e_, s_ = lonlat(self.tx0 * 256 + w, self.ty0 * 256 + h)
        return w_, s_, e_, n_

    @property
    def shape(self):
        return self.rgb.shape[:2]

    def px_to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        n = 2 ** self.z * 256
        X, Y = x + self.tx0 * 256, y + self.ty0 * 256
        lon = X / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * Y / n))))
        return lon, lat

    def to_px(self, lon: float, lat: float) -> tuple[float, float]:
        X, Y = _lonlat_to_global_px(lon, lat, self.z)
        return X - self.tx0 * 256, Y - self.ty0 * 256

    def rasterize(self, geom_wgs, value: int = 1) -> np.ndarray:
        """Boolean mask of a (multi)polygon given in WGS84."""
        h, w = self.shape
        im = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(im)
        for poly in _polys(geom_wgs):
            d.polygon([self.to_px(*c[:2]) for c in poly.exterior.coords], fill=value)
            for hole in poly.interiors:
                d.polygon([self.to_px(*c[:2]) for c in hole.coords], fill=0)
        return np.asarray(im) > 0


def _polys(g):
    if g is None or g.is_empty:
        return []
    t = g.geom_type
    if t == "Polygon":
        return [g]
    if t in ("MultiPolygon", "GeometryCollection"):
        out = []
        for p in g.geoms:
            out.extend(_polys(p))
        return out
    return []


def ndvi_for(mos: "Mosaic", cfg: dict) -> np.ndarray | None:
    """Vegetation index (NDVI, -1..1) on the mosaic's exact pixel grid, from a
    4-band (red + near-infrared) image service such as NAIP. Grass and trees
    are strongly positive; concrete, asphalt, roofs and bare soil are near 0."""
    import httpx
    if not cfg:
        return None
    h, w = mos.shape
    n = 2 ** mos.z * 256
    R = 6378137.0
    world = 2 * math.pi * R

    def merc(X, Y):
        return X / n * world - world / 2, world / 2 - Y / n * world

    x0, y1 = merc(mos.tx0 * 256, mos.ty0 * 256)
    x1, y0 = merc(mos.tx0 * 256 + w, mos.ty0 * 256 + h)
    params = {"bbox": f"{x0},{y0},{x1},{y1}", "bboxSR": 3857, "imageSR": 3857, "size": f"{w},{h}",
              "bandIds": f"{cfg.get('red', 0)},{cfg.get('nir', 3)},{cfg.get('green', 1)}", "format": "png",
              "interpolation": "RSP_BilinearInterpolation", "f": "image"}
    from .gis import get_bytes
    url = cfg["url"] + "/exportImage?" + "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    data = get_bytes(url)
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        a = np.asarray(im).astype(np.float32)
    except Exception:
        return None
    if a.ndim != 3 or a.shape[2] < 2:
        return None
    if a.shape[:2] != (h, w):  # server capped the size: stretch back to the grid
        a = np.asarray(im.convert("RGB").resize((w, h), Image.BILINEAR)).astype(np.float32)
    if a.shape[:2] != (h, w):
        return None
    red, nir = a[..., 0], a[..., 1]
    if nir.max() <= 0:
        return None
    return (nir - red) / np.maximum(nir + red, 1.0)
