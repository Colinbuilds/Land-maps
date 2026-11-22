"""Measure a parcel: lot, building, lawn, driveway, sidewalk, other hardscape.

Sources (Douglas County):
  * Lot and neighbouring lot lines ........ county parcel layer (vector, exact)
  * Building / roof footprint ............. 2022 planimetric footprints (vector)
  * Street edge of pavement ............... 2022 LiDAR pavement (vector)
  * Driveway, sidewalk, patios, lawn ...... 2025 orthoimagery, classified per pixel
    and clipped to the parcel, since no public layer maps them.

All results are square feet (lengths in feet).
"""
from __future__ import annotations

import base64
import io
import math
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from shapely.geometry import box, LineString, MultiLineString, Point, Polygon, shape
from shapely.ops import unary_union

from .counties import get_county
from .gis import Projector, ChunkedPolygonLayer, query_features
from .imagery import Mosaic
from .lidar import LidarSource
from . import corrections

MEASUREMENTS = {
    "lot_sqft": "Lot (total parcel)",
    "building_sqft": "Building footprint / roof",
    "lawn_sqft": "Lawn / grass only",
    "parkway_sqft": "Parkway grass (between sidewalk and curb)",
    "driveway_sqft": "Driveway (incl. apron to street)",
    "sidewalk_sqft": "Public sidewalk along frontage",
    "other_hardscape_sqft": "Patios, walks & other paving",
    "structures_sqft": "Decks, sheds, play sets & other structures",
    "beds_sqft": "Landscape beds & shrubs",
    "frontage_ft": "Street frontage (linear ft)",
}

_CTX_FT = 70.0           # how far around the parcel to look
_MIN_BLOB_SQFT = 20.0    # ignore paved specks smaller than this


class _BlockCache:
    """Caches parcel/building queries by grid cell so neighbours in a bulk run
    share downloads."""

    CELL = 0.004  # degrees (~1,100 ft N-S)

    def __init__(self, url: str, out_fields: str, maxsize: int = 256):
        self.url, self.out_fields, self.maxsize = url, out_fields, maxsize
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._cell_locks: dict = {}

    def _cell(self, cx: int, cy: int):
        key = (cx, cy)
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            lk = self._cell_locks.setdefault(key, threading.Lock())
        with lk:
            with self._lock:
                if key in self._d:
                    return self._d[key]
            env = (cx * self.CELL, cy * self.CELL, (cx + 1) * self.CELL, (cy + 1) * self.CELL)
            feats = query_features(self.url, envelope=env, out_fields=self.out_fields)
            with self._lock:
                self._d[key] = feats
                while len(self._d) > self.maxsize:
                    self._d.popitem(last=False)
            return feats

    def query(self, env):
        minx, miny, maxx, maxy = env
        seen, out = set(), []
        for cx in range(math.floor(minx / self.CELL), math.floor(maxx / self.CELL) + 1):
            for cy in range(math.floor(miny / self.CELL), math.floor(maxy / self.CELL) + 1):
                for f in self._cell(cx, cy):
                    oid = f["attributes"].get("OBJECTID", f["attributes"].get("objectid"))
                    k = oid if oid is not None else id(f)
                    if k in seen or f["geometry"] is None:
                        continue
                    seen.add(k)
                    out.append(f)
        return out


class Measurer:
    def __init__(self, county_id: str | None = None):
        self.c = get_county(county_id)
        self.proj = Projector(self.c["crs"])
        pf = self.c["parcels"]["fields"]
        self.pf = pf
        self.parcels = _BlockCache(self.c["parcels"]["url"], f"OBJECTID,{pf['pin']}")
        self.buildings = _BlockCache(self.c["buildings"]["url"], "objectid")
        self.pavement = ChunkedPolygonLayer(self.c["pavement"]["url"], self.proj)
        self.lidar = LidarSource(self.c["lidar"]) if self.c.get("lidar") else None

    # ------------------------------------------------------------------ data
    def parcel_attrs(self, a: dict) -> dict:
        pf = self.pf
        return {k: a.get(v) for k, v in pf.items()}

    def get_parcel(self, pin: str) -> dict | None:
        pin = "".join(ch for ch in pin if ch.isalnum())
        feats = query_features(self.c["parcels"]["url"], where=f"{self.pf['pin']}='{pin}'")
        return feats[0] if feats else None

    def _lidar_layers(self, mos, env, ft_px, bldg_m=None):
        """LiDAR obj/veg/ground rasters resampled onto the imagery pixel grid."""
        try:
            h, w = mos.shape
            # pixel -> LAS CRS is affine to well under an inch over a few hundred feet
            src = np.array([[0, 0], [w, 0], [0, h]], float)
            dst = np.array([self.lidar.to_las.transform(*mos.px_to_lonlat(x, y)) for x, y in src])
            ex = (dst[1] - dst[0]) / w
            ey = (dst[2] - dst[0]) / h
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
            xs = dst[0][0] + xx * ex[0] + yy * ey[0]
            ys = dst[0][1] + xx * ex[1] + yy * ey[1]
            smp = self.lidar.sample(env, xs, ys)
        except Exception:
            return None
        if smp is None:
            return None
        return _lidar_masks(smp["obj"], smp["veg"], smp["gnd"], ft_px, ft_px * ft_px,
                            inten=smp.get("inten"), bldg=bldg_m)

    # --------------------------------------------------------------- measure
    def measure(self, parcel_feature: dict, want_overlay: bool = False) -> dict:
        attrs = self.parcel_attrs(parcel_feature["attributes"])
        P_wgs = parcel_feature["geometry"]
        P = self.proj.to_local(P_wgs).buffer(0)
        ctx = P.buffer(_CTX_FT)
        ctx_box = box(*ctx.bounds)
        env = self.proj.to_wgs(ctx_box).bounds

        # neighbours, buildings, pavement (local feet)
        pin = attrs["pin"]
        neigh = [self.proj.to_local(f["geometry"]) for f in self.parcels.query(env)
                 if f["attributes"].get(self.pf["pin"]) != pin]
        neigh = [g.buffer(0) for g in neigh if g.intersects(ctx_box)]
        # Stacked condo parcels can duplicate this lot's shape; drop those.
        neigh = [g for g in neigh if not (g.intersection(P).area > 0.8 * P.area)]
        N = unary_union(neigh) if neigh else None
        bldgs = [self.proj.to_local(f["geometry"]).buffer(0) for f in self.buildings.query(env)]
        B = unary_union([b for b in bldgs if b.intersects(ctx_box)]) if bldgs else None
        pave = self.pavement.get_local(env, ctx_box)
        V = unary_union(pave) if pave else None

        row = ctx_box.difference(P)
        if N is not None:
            row = row.difference(N)

        building_in = B.intersection(P) if B is not None else None
        building_sqft = building_in.area if building_in is not None else 0.0

        # frontage: lot lines that face public right-of-way near a street
        frontage = P.exterior.intersection(row.buffer(2.0)) if not row.is_empty else None
        frontage = _lines(frontage)
        if V is not None and frontage:
            near = V.buffer(60)
            frontage = [seg for seg in _lines(unary_union(frontage).intersection(near)) if seg.length > 8]
        else:
            frontage = [seg for seg in frontage if seg.length > 8] if V is not None else []
        frontage_ft = sum(s.length for s in frontage)

        # ---------------------------------------------------------- imagery
        img = self.c["imagery"]
        mos = Mosaic(img["tile_url"], env, img["zoom"])
        to_mask = lambda g_local: mos.rasterize(self.proj.to_wgs(g_local)) if g_local is not None and not g_local.is_empty else np.zeros(mos.shape, bool)
        parcel_m = to_mask(P)
        bldg_m = to_mask(B.buffer(1.0)) if B is not None else np.zeros(mos.shape, bool)
        pave_m = to_mask(V.buffer(2.0)) if V is not None else np.zeros(mos.shape, bool)
        npx = int(parcel_m.sum())
        sqft_px = P.area / npx if npx else mos.sqft_per_px  # calibrate raster to vector area
        ft_px = math.sqrt(sqft_px)

        hard = classify_hard(mos.rgb)
        hard &= ~bldg_m

        def px_of(pt_local):
            lon, lat = self.proj.inv.transform(pt_local.x, pt_local.y)
            return mos.to_px(lon, lat)

        H, W = hard.shape

        def sample(mask, pt):
            x, y = px_of(pt)
            xi, yi = int(round(x)), int(round(y))
            return 0 <= xi < W and 0 <= yi < H and bool(mask[yi, xi])

        # -------------------------------------------------- sidewalk stations
        step = 2.0
        stations = []  # (point, outward normal, dist_to_pavement, run_start, run_end)
        for seg in frontage:
            n_st = max(1, int(seg.length // step))
            for i in range(n_st):
                d0 = (i + 0.5) * seg.length / n_st
                p = seg.interpolate(d0)
                q = seg.interpolate(min(seg.length, d0 + 0.5))
                q0 = seg.interpolate(max(0, d0 - 0.5))
                dx, dy = q.x - q0.x, q.y - q0.y
                L = math.hypot(dx, dy) or 1
                nx, ny = -dy / L, dx / L
                if P.contains(Point(p.x + nx * 2, p.y + ny * 2)):
                    nx, ny = -nx, -ny  # point outward, into the ROW
                if not row.contains(Point(p.x + nx * 4, p.y + ny * 4)):
                    continue  # a sliver between neighbouring lot lines, not a street
                dist_pave = V.distance(p) if V is not None else 0.0
                reach = min(max(dist_pave, 0.0), 30.0)
                # walk from 8 ft inside the lot out to the curb
                offs = np.arange(-8.0, reach + 0.01, 0.5)
                vals = [sample(hard, Point(p.x + nx * o, p.y + ny * o)) and not sample(pave_m, Point(p.x + nx * o, p.y + ny * o)) for o in offs]
                run = _longest_run(vals)
                rs = re = None
                if run:
                    rs, re = offs[run[0]], offs[run[1]]
                stations.append((p, (nx, ny), dist_pave, rs, re))

        if frontage:
            frontage_ft = len(stations) * step
        sw_w = float(self.c.get("sidewalk_width_ft", 4.0))
        sw_stations = [s for s in stations if s[3] is not None and (s[4] - s[3]) >= 2.5]
        sidewalk_ft = len(sw_stations) * step if stations else 0.0
        # a sidewalk on only a sliver of frontage is usually a driveway apron, not a walk
        if frontage_ft and sidewalk_ft < 0.35 * frontage_ft:
            sidewalk_ft = 0.0
            sw_stations = []
        sidewalk_sqft = sidewalk_ft * sw_w
        # Is the walk inside the lot line? (some subdivisions put the lot line at the curb)
        sw_inside = False
        if sw_stations:
            centers = [((s[3] + s[4]) / 2) for s in sw_stations if (s[4] - s[3]) < 10]
            sw_inside = bool(centers) and float(np.median(centers)) < 0
        sw_band_m = np.zeros(hard.shape, bool)
        if sw_inside and frontage:
            sw_band_m = to_mask(unary_union(frontage).buffer(sw_w + 1.5).intersection(P))

        # ------------------------------------------------------------ driveway
        lot_hard = hard & parcel_m & ~sw_band_m
        lot_hard = _drop_small(lot_hard, _MIN_BLOB_SQFT / sqft_px)
        labels, nlab = ndi.label(lot_hard, structure=np.ones((3, 3)))
        front_zone = to_mask(unary_union(frontage).buffer(sw_w + 8.0 if sw_inside else 8.0).intersection(P)) if frontage else np.zeros(hard.shape, bool)
        touching = np.unique(labels[front_zone & lot_hard])
        touching = touching[touching > 0]
        # keep the biggest frontage-touching blobs (2 = circle drives / side-load)
        if len(touching):
            sizes = ndi.sum(lot_hard, labels, touching)
            order = np.argsort(sizes)[::-1]
            keep = [touching[i] for i in order[:2] if sizes[i] * sqft_px >= 60]
        else:
            keep = []
        drive_m = np.isin(labels, keep) if keep else np.zeros(hard.shape, bool)
        if drive_m.any() and stations and bldg_m.any():
            # Driveways run from the street to the garage. Where the house's
            # shadow or a parked car hides part of it, carry the detected slab
            # on toward the building, but never across sunlit lawn.
            ins = [(-nx, -ny) for p, (nx, ny), *_ in stations
                   if sample(drive_m, Point(p.x - nx * 1.5, p.y - ny * 1.5))]
            if ins:
                ax, ay = np.mean([d[0] for d in ins]), np.mean([d[1] for d in ins])
                # local-feet direction -> pixel direction (y flips)
                L = math.hypot(ax, ay) or 1
                dpx = np.array([ax / L, -ay / L])
                ext = _extend_to_building(drive_m, bldg_m, parcel_m & ~sw_band_m, _sunlit_lawn(mos.rgb), dpx,
                                          max_steps=int(60 / ft_px))
                drive_m |= ext
        drive_lot_sqft = float(drive_m.sum()) * sqft_px

        # apron: driveway width at the lot line x distance to curb (minus walk)
        apron_sqft = 0.0
        if drive_m.any() and V is not None:
            for p, (nx, ny), dist_pave, _, _ in stations:
                inside = Point(p.x - nx * 1.5, p.y - ny * 1.5)
                if sample(drive_m, inside):
                    gap = min(dist_pave, 30.0) - (0 if sw_inside or not sw_stations else sw_w)
                    apron_sqft += max(0.0, gap) * step
        driveway_sqft = drive_lot_sqft + apron_sqft
        driveway_method = "imagery"
        if driveway_sqft < 120 and building_in is not None and not building_in.is_empty and V is not None and frontage:
            # Shadowed or tree-covered driveway: fall back to a typical-width slab
            # from the house to the curb, and say so.
            gap = building_in.distance(V)
            if 5 < gap < 120:
                dw = float(self.c.get("default_driveway_width_ft", 16))
                driveway_sqft = dw * gap
                driveway_method = "estimated"
                est_in_lot = dw * min(gap, building_in.distance(unary_union(frontage)))
                drive_m = np.zeros(hard.shape, bool)
        elif driveway_sqft < 120:
            driveway_method = "none_found"

        other_m = lot_hard & ~drive_m
        other_sqft = float(other_m.sum()) * sqft_px

        # --------------------------------------------------------------- lawn
        # Grass only: the lot minus buildings, every paved surface, raised
        # structures (decks, sheds, play sets, trampolines), landscape beds,
        # dense evergreens and pools.
        yard = parcel_m & ~bldg_m
        lid = self._lidar_layers(mos, env, ft_px, bldg_m) if self.lidar else None
        struct_m = bed_m = canopy_m = np.zeros(hard.shape, bool)
        if lid is not None:
            struct_m, bed_m, canopy_m = lid
            struct_m &= yard
            canopy_m &= yard & ~struct_m
            bed_m &= yard & ~hard & ~struct_m & ~canopy_m
        pool_m = _pools(mos.rgb) & yard & ~struct_m
        lawn_m = yard & ~hard & ~struct_m & ~bed_m & ~canopy_m & ~pool_m
        lawn_m = ndi.binary_opening(lawn_m, iterations=1)
        lawn_m = _drop_small(lawn_m, 4.0 / sqft_px)
        est_cut = 0.0
        if driveway_method == "estimated":
            est_cut = est_in_lot
        # hand corrections saved by the business for this property
        corr = corrections.get(pin)
        if corr:
            for g in corr.get("exclude", []):
                lawn_m &= ~mos.rasterize(shape(g))
            for g in corr.get("include", []):
                lawn_m |= mos.rasterize(shape(g)) & parcel_m
        lawn_sqft = max(0.0, float(lawn_m.sum()) * sqft_px - est_cut)

        # Parkway: the city-owned grass strip along the street that owners mow.
        parkway_m = np.zeros(hard.shape, bool)
        if stations and V is not None:
            # strip straight out from this lot's frontage to the curb
            quads = []
            for p, (nx, ny), dist_pave, _, _ in stations:
                d = min(dist_pave, 30.0)
                if d <= 1:
                    continue
                tx, ty = -ny * step * 0.6, nx * step * 0.6
                quads.append(Polygon([(p.x - tx, p.y - ty), (p.x + tx, p.y + ty),
                                      (p.x + tx + nx * d, p.y + ty + ny * d), (p.x - tx + nx * d, p.y - ty + ny * d)]))
            if quads:
                zone = unary_union(quads).intersection(row)
                parkway_m = to_mask(zone) & ~parcel_m & ~pave_m & ~hard & ~drive_m
                if lid is not None:
                    parkway_m &= ~lid[0] & ~lid[1] & ~lid[2]
                parkway_m = ndi.binary_opening(parkway_m, iterations=2)
                parkway_m = _drop_small(parkway_m, 10.0 / sqft_px)
        parkway_sqft = float(parkway_m.sum()) * sqft_px
        structures_sqft = float((struct_m | pool_m).sum()) * sqft_px
        beds_sqft = float((bed_m | canopy_m).sum()) * sqft_px

        result = {
            "pin": pin,
            "address": (attrs.get("address") or "").strip(),
            "city": attrs.get("city"),
            "zip": attrs.get("zip"),
            "class": attrs.get("class"),
            "lot_sqft": round(P.area),
            "building_sqft": round(building_sqft),
            "lawn_sqft": round(lawn_sqft),
            "parkway_sqft": round(parkway_sqft),
            "driveway_sqft": round(driveway_sqft),
            "sidewalk_sqft": round(sidewalk_sqft),
            "other_hardscape_sqft": round(other_sqft),
            "structures_sqft": round(structures_sqft),
            "beds_sqft": round(beds_sqft),
            "frontage_ft": round(frontage_ft),
            "assessor": {
                "lot_sqft": attrs.get("lot_sqft"),
                "living_sqft": attrs.get("bldg_sqft"),
                "building": attrs.get("bldg_desc"),
                "year_built": attrs.get("year_built"),
            },
            "sources": {
                "imagery_year": img.get("year"),
                "imagery_missing_tiles": mos.missing,
                "sidewalk_detected": bool(sw_stations),
                "driveway_method": driveway_method,
                "lidar_year": self.c["lidar"]["year"] if lid is not None else None,
                "corrected": bool(corr),
            },
        }
        if want_overlay:
            result["overlay"] = _overlay_png(mos, parcel_m, lawn_m | parkway_m, drive_m, other_m, struct_m | pool_m, bed_m | canopy_m,
                                             frontage, sw_stations, self, step, sw_w)
            result["parcel_geojson"] = _geojson(P_wgs)
        return result


# --------------------------------------------------------------------- helpers
def _lidar_masks(obj, veg, gnd, ft_px, sqft_px, inten=None, bldg=None):
    """Turn sampled LiDAR heights into structure / bed / evergreen masks."""
    r = lambda ft: max(1, int(round(ft / ft_px)))
    # Raised non-tree objects. The opening drops anything under ~2.5 ft wide
    # (fences, posts, mailboxes) so mowable lawn along a fence isn't lost.
    struct = obj > 0.8
    # Close small gaps first so ring-shaped things (trampoline frames,
    # above-ground pool walls, gazebo rails) become solid outlines.
    struct = ndi.binary_closing(struct, iterations=r(1.0))
    filled = ndi.binary_fill_holes(struct)
    holes, n = ndi.label(filled & ~struct)
    if n:
        sizes = ndi.sum(np.ones_like(holes), holes, range(1, n + 1))
        small = np.zeros(n + 1, bool)
        small[1:] = sizes * sqft_px < 600  # trampolines / pools, not a fenced yard
        struct |= small[holes]
    struct = ndi.binary_opening(struct, iterations=r(1.5))
    struct = _drop_small(struct, 10.0 / sqft_px)  # fence posts, yard ornaments
    struct = ndi.binary_dilation(struct, iterations=r(0.5))
    # Shrubs 0.8-8 ft tall; merge neighbours into bed outlines and add the
    # mulch edge that extends past the foliage.
    shrub = (veg > 0.8) & (veg < 8.0)
    shrub = ndi.binary_opening(shrub, iterations=r(0.6))
    shrub = _drop_small(shrub, 6.0 / sqft_px)
    bed = ndi.binary_closing(shrub, iterations=r(2.0))
    bed = ndi.binary_dilation(bed, iterations=r(1.0))
    # Foundation beds (mulch / rock with no shrubs): ground returns right next
    # to the house that are darker than turf. Grass returns ~190-210, mulch,
    # rock and soil come back well below that.
    if inten is not None and bldg is not None and bldg.any():
        ok = gnd > 0
        k = r(2.5)
        sm = ndi.uniform_filter(np.where(ok, inten, 0).astype(np.float32), size=k) / \
            np.maximum(ndi.uniform_filter(ok.astype(np.float32), size=k), 1e-3)
        near_house = ndi.binary_dilation(bldg, iterations=r(8.0)) & ~bldg
        dark = (sm < 160) & near_house & (veg < 6.0) & (ndi.uniform_filter(ok.astype(np.float32), size=k) > 0.3)
        dark = ndi.binary_opening(dark, iterations=r(0.75))
        bed |= _drop_small(dark, 15.0 / sqft_px)
    # Dense evergreens: tall vegetation with no ground returns underneath.
    no_ground = ndi.maximum_filter(gnd, size=r(3.0)) == 0
    canopy = (veg > 6.0) & no_ground
    canopy = ndi.binary_opening(canopy, iterations=r(1.0))
    return struct, bed, canopy



def classify_hard(rgb: np.ndarray) -> np.ndarray:
    """Pixel classifier for paved / concrete surfaces on leaf-off imagery.

    Concrete and asphalt are near-neutral grey; dormant lawn, mulch and soil are
    noticeably saturated (tan/brown/green). Shadowed concrete is darker but
    stays neutral, while shadowed grass gets *more* saturated.
    """
    sm = ndi.uniform_filter(rgb, size=(3, 3, 1))
    mx = sm.max(-1)
    mn = sm.min(-1)
    sat = (mx - mn) / (mx + 1e-6)
    hard = ((sat < 0.27) & (mx > 0.42)) | ((sat < 0.20) & (mx > 0.18))
    hard = ndi.binary_opening(hard, iterations=2)
    hard = ndi.binary_closing(hard, iterations=3)
    hard = ndi.binary_fill_holes(hard)  # parked cars
    return hard


def _pools(rgb: np.ndarray) -> np.ndarray:
    """Pool water and most pool covers read blue/teal; grass and soil never do."""
    sm = ndi.uniform_filter(rgb, size=(5, 5, 1))
    r, g, b = sm[..., 0], sm[..., 1], sm[..., 2]
    mx = sm.max(-1)
    sat = (mx - sm.min(-1)) / (mx + 1e-6)
    m = (b > r * 1.08) & (b >= g * 0.97) & (sat > 0.15) & (mx > 0.15)
    return ndi.binary_opening(m, iterations=2)


def _sunlit_lawn(rgb: np.ndarray) -> np.ndarray:
    """Clearly-vegetated pixels in sun: tan/brown/green with real saturation."""
    sm = ndi.uniform_filter(rgb, size=(5, 5, 1))
    r, g, b = sm[..., 0], sm[..., 1], sm[..., 2]
    mx = sm.max(-1)
    mn = sm.min(-1)
    sat = (mx - mn) / (mx + 1e-6)
    yellowish = (b <= g) & (b <= r) & (g >= 0.75 * r)  # hue roughly 20-150 deg
    return (mx > 0.33) & (sat > 0.28) & yellowish


def _extend_to_building(drive, bldg, allowed, lawn, d, max_steps):
    """March every driveway pixel along direction ``d`` (pixel units). Paths
    that reach the building without crossing sunlit lawn are added."""
    ys, xs = np.nonzero(drive)
    if len(xs) == 0:
        return np.zeros_like(drive)
    # only pixels on the inner edge need to march
    H, W = drive.shape
    nxt_x = np.clip(np.round(xs + d[0] * 2).astype(int), 0, W - 1)
    nxt_y = np.clip(np.round(ys + d[1] * 2).astype(int), 0, H - 1)
    edge = ~drive[nxt_y, nxt_x]
    xs, ys = xs[edge].astype(float), ys[edge].astype(float)
    alive = np.ones(len(xs), bool)
    hit = np.zeros(len(xs), bool)
    hit_k = np.full(len(xs), max_steps + 1)
    path_x = np.zeros((max_steps, len(xs)), int)
    path_y = np.zeros((max_steps, len(xs)), int)
    for k in range(1, max_steps + 1):
        px = np.round(xs + d[0] * k).astype(int)
        py = np.round(ys + d[1] * k).astype(int)
        inb = (px >= 0) & (px < W) & (py >= 0) & (py < H)
        pxc, pyc = np.clip(px, 0, W - 1), np.clip(py, 0, H - 1)
        path_x[k - 1], path_y[k - 1] = pxc, pyc
        at_bldg = inb & bldg[pyc, pxc] & alive & ~hit
        hit_k[at_bldg] = k
        hit |= at_bldg
        bad = ~inb | ~allowed[pyc, pxc] | lawn[pyc, pxc]
        alive &= ~(bad & ~hit)
        if not (alive & ~hit).any():
            break
    out = np.zeros_like(drive)
    ok = hit & alive
    if ok.sum() < 0.25 * len(xs):  # too little reached the house: don't guess
        return out
    for k in range(path_x.shape[0]):
        sel = ok & (k + 1 < hit_k) & ~bldg[path_y[k], path_x[k]]
        out[path_y[k][sel], path_x[k][sel]] = True
    out = ndi.binary_closing(out, iterations=2) & allowed & ~bldg
    return out


def _drop_small(mask, min_px):
    lab, n = ndi.label(mask)
    if n == 0:
        return mask
    sizes = ndi.sum(mask, lab, range(1, n + 1))
    keep = np.zeros(n + 1, bool)
    keep[1:] = sizes >= min_px
    return keep[lab]


def _longest_run(vals):
    best, cur, start = None, 0, 0
    for i, v in enumerate(vals):
        if v:
            if cur == 0:
                start = i
            cur += 1
            if best is None or cur > best[1] - best[0] + 1:
                best = (start, i)
        else:
            cur = 0
    return best


def _lines(g):
    if g is None or g.is_empty:
        return []
    if isinstance(g, LineString):
        return [g]
    if isinstance(g, MultiLineString):
        return list(g.geoms)
    if hasattr(g, "geoms"):
        out = []
        for x in g.geoms:
            out.extend(_lines(x))
        return out
    return []


def _geojson(g):
    from shapely.geometry import mapping
    return mapping(g)


def _overlay_png(mos, parcel_m, lawn_m, drive_m, other_m, struct_m, bed_m, frontage, sw_stations, meas, step, sw_w):
    h, w = parcel_m.shape
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[lawn_m] = (34, 197, 94, 110)
    rgba[other_m] = (234, 179, 8, 150)
    rgba[struct_m] = (239, 68, 68, 160)
    rgba[bed_m] = (168, 85, 247, 150)
    rgba[drive_m] = (59, 130, 246, 170)
    # sidewalk band (drawn along detected stations)
    if sw_stations:
        sw = np.zeros((h, w), bool)
        for p, (nx, ny), _, rs, re in sw_stations:
            for o in np.arange(rs, re + 0.01, 0.5):
                lon, lat = meas.proj.inv.transform(p.x + nx * o, p.y + ny * o)
                x, y = mos.to_px(lon, lat)
                xi, yi = int(x), int(y)
                if 0 <= xi < w and 0 <= yi < h:
                    sw[max(0, yi - 2):yi + 3, max(0, xi - 2):xi + 3] = True
        rgba[sw & ~drive_m] = (236, 72, 153, 170)
    edge = parcel_m ^ ndi.binary_erosion(parcel_m, iterations=2)
    rgba[edge] = (250, 250, 250, 255)
    im = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    W_, S_, E_, N_ = mos.bounds_wgs
    return {
        "png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "bounds": [[S_, W_], [N_, E_]],
    }


_default: Measurer | None = None
_default_lock = threading.Lock()


def default_measurer() -> Measurer:
    global _default
    with _default_lock:
        if _default is None:
            _default = Measurer()
        return _default
