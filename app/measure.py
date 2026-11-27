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

import os

import base64
import io
import math
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from shapely.geometry import box, LineString, MultiLineString, Point, Polygon, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

from .counties import get_county
from .gis import Projector, ChunkedPolygonLayer, query_features
from .imagery import Mosaic, ndvi_for
from .lidar import LidarSource
from . import corrections

# Trees (count, canopy, lawn under trees) are switched off: not measured,
# not sold. MEASURE_TREES=1 brings them back.
MEASURE_TREES = os.environ.get("MEASURE_TREES", "0") == "1"
FRONTAGE_MAX_FT = 40.0

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
    "wall_sqft": "House wall area (siding, gross)",
    "est_windows": "Windows & doors (estimate)",
    "frontage_ft": "Street frontage (linear ft)",
}

_CTX_FT = 70.0           # how far around the parcel to look
_ACREAGE_SQFT = 43560.0  # lots bigger than an acre: measure the home site only
_HOMESITE_FT = 150.0
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


def _split_situs(addr: str, city=None, zip_=None) -> dict:
    """Split a one-line situs address into street / city / ZIP.
    "21200 SW 72ND ST, CRETE, NE, 68368" (commas), "1101 FENWICK ST  PAPILLION NE 68046"
    (two spaces before the city), "16920 6TH AVE PLATTSMOUTH NE 68048" (city known)."""
    import re
    addr = re.sub(r"\s+$", "", addr)
    m = re.match(r"^(.*?)\s+NE[,\s]+(\d{5})(?:-\d{4})?$", addr.replace(",", " ").strip()) if "," not in addr else None
    if "," in addr:
        parts = [p.strip() for p in addr.split(",") if p.strip()]
        zip_m = next((p for p in parts if re.fullmatch(r"\d{5}(-\d{4})?", p)), None)
        parts = [p for p in parts if p not in ("NE", zip_m)]
        street = ", ".join(p for p in parts[:-1]) if len(parts) > 1 else parts[0]
        return {"address": street, "city": city or (parts[-1].title() if len(parts) > 1 else None),
                "zip": zip_ or (zip_m[:5] if zip_m else None)}
    if m:
        head, z = m.group(1), m.group(2)
        if city and head.upper().endswith(str(city).upper()):
            return {"address": head[: -len(city)].strip(), "city": city, "zip": zip_ or z}
        if "  " in head:
            street, c = head.rsplit("  ", 1)
            return {"address": street.strip(), "city": city or c.strip(), "zip": zip_ or z}
        return {"address": head.strip(), "city": city, "zip": zip_ or z}
    return {"address": addr.strip(), "city": city, "zip": zip_}


class Measurer:
    def __init__(self, county_id: str | None = None):
        self.c = get_county(county_id)
        self.cid = self.c["id"]
        self.proj = Projector(self.c["crs"])
        pc = self.c["parcels"]
        self.pf = {k: (v[0] if isinstance(v, tuple) else v) for k, v in pc["fields"].items()}
        self._mult = {k: v[1] for k, v in pc["fields"].items() if isinstance(v, tuple)}
        oid = pc.get("oid", "OBJECTID")
        self.parcels = _BlockCache(pc["url"], f"{oid},{self.pf['pin']}")
        b = self.c["buildings"]
        bfields = ",".join(x for x in (b.get("oid", "objectid"), b.get("height_field")) if x)
        self.buildings = _BlockCache(b["url"], bfields)
        pv = self.c.get("pavement")
        self.pavement = ChunkedPolygonLayer(pv["url"], self.proj) if pv else None
        st = self.c.get("streets")
        self.streets = _BlockCache(st["url"], "OBJECTID") if st and not pv else None
        lc = self.c.get("lidar")
        if not lc:
            self.lidar = None
        elif lc.get("type") == "ept":
            from .lidar import EptLidar
            self.lidar = EptLidar(lc, self.c["crs"], footprints_fn=self._footprints_las)
        else:
            self.lidar = LidarSource(lc)

    def _footprints_las(self, env_wgs):
        """Building outlines in an area, in LiDAR CRS feet (for alignment)."""
        out = []
        for f in self.buildings.query(env_wgs):
            g = f["geometry"]
            if g is not None and not g.is_empty:
                out.append(shp_transform(lambda x, y, z=None: self.lidar.to_las.transform(x, y), g).buffer(0))
        return out

    # ------------------------------------------------------------------ data
    def parcel_attrs(self, a: dict) -> dict:
        out = {}
        for k, field in self.pf.items():
            v = a.get(field) if field else None
            if v is not None and k in self._mult:
                try:
                    v = float(v) * self._mult[k]
                except (TypeError, ValueError):
                    v = None
            out[k] = v
        if self.c["parcels"].get("address_has_city_zip") and out.get("address"):
            out.update(_split_situs(str(out["address"]), out.get("city"), out.get("zip")))
        if out.get("address"):
            out["address"] = str(out["address"]).strip()
            if self.c["parcels"].get("address_trim_comma") and "," in out["address"]:
                # "2933 E UNIVERSITY DR , MCKINNEY, TX 75069": city/ZIP have their own fields
                out["address"] = out["address"].split(",")[0].strip()
        return out

    def get_parcel(self, pin: str) -> dict | None:
        pin = "".join(ch for ch in str(pin) if ch.isalnum() or ch in "-.")
        feats = query_features(self.c["parcels"]["url"], where=f"{self.pf['pin']}='{pin}'")
        return feats[0] if feats else None

    def _house_walls(self, P_wgs, env, have_lidar) -> dict:
        """Siding-area estimate for the main house: gross wall area and an
        estimated window/door count, from the county footprint (drawn at the
        roof edge) and LiDAR roof heights."""
        out = {"wall_sqft": None, "est_windows": None}
        if not have_lidar or self.lidar is None:
            return out
        try:
            from shapely.ops import transform as _tf
            from .walls import measure_walls
            cands = [f["geometry"] for f in self.buildings.query(P_wgs.bounds)
                     if f["geometry"] is not None and not f["geometry"].is_empty]
            cands = [g for g in cands if g.intersection(P_wgs).area > 0.5 * g.area]
            if not cands:
                return out
            main = max(cands, key=lambda g: g.area)
            las = _tf(lambda x, y, z=None: self.lidar.to_las.transform(x, y), main).buffer(0)
            if las.area < 400:  # sheds and garages alone aren't a house
                return out
            w = measure_walls(self.lidar, env, las)
            if w:
                out = {"wall_sqft": w["gross_wall_sqft"], "est_windows": w["est_openings"]}
        except Exception:
            pass
        return out

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
        struct, bed, canopy = _lidar_masks(smp["obj"], smp["veg"], smp["gnd"], ft_px, ft_px * ft_px,
                                           inten=smp.get("inten"), bldg=bldg_m)
        if smp.get("sparse"):  # USGS data: only keep solid, deck-sized objects
            struct = _drop_small(struct, 30.0 / (ft_px * ft_px))
        veg = np.asarray(smp["veg"], np.float32)
        r_ = lambda ft: max(1, int(round(ft / ft_px)))
        # roof-height returns: where the photo and the building outline disagree
        # by a few feet, the roof edge must not be read as a patio
        high = ndi.binary_opening(np.asarray(smp["obj"], np.float32) > 7.0, iterations=1)
        if not MEASURE_TREES:  # trees aren't counted or sold: skip the work
            fence = _fences(np.asarray(smp["obj"], np.float32), np.asarray(smp["veg"], np.float32), ft_px, bldg_m)
            none = np.zeros(veg.shape, bool)
            return struct, bed, canopy, none, [], fence, high
        if bldg_m is not None and bldg_m.any():
            # pulses that clip a gutter come back as "vegetation" at roof
            # height: right around a house only taller growth is a tree
            roof_h = ndi.maximum_filter(np.where(bldg_m, smp["obj"], 0).astype(np.float32), size=2 * r_(6.0) + 1)
            near = ndi.binary_dilation(bldg_m, iterations=r_(4.0))
            veg = np.where(near & (veg <= roof_h + 2.0), -1.0, veg)
        crowns, tops = _trees(veg, ft_px, ft_px * ft_px, sparse=bool(smp.get("sparse")))
        if crowns.any() and struct.any():
            # swing sets, pergolas and the like echo like branches; a crown
            # that is mostly a solid raised structure isn't a tree
            lab, n = ndi.label(crowns)
            frac = ndi.mean(struct, lab, range(1, n + 1))
            bad = np.zeros(n + 1, bool)
            bad[1:] = np.asarray(frac) > 0.4
            crowns = crowns & ~bad[lab]
            tops = [t for t in tops if crowns[t[0], t[1]]]
        fence = _fences(np.asarray(smp["obj"], np.float32), np.asarray(smp["veg"], np.float32), ft_px, bldg_m)
        return struct, bed, canopy, crowns, tops, fence, high

    # --------------------------------------------------------------- measure
    def feature_by_pin(self, pin: str):
        return self.get_parcel(pin)

    def measure(self, parcel_feature: dict, want_overlay: bool = False, want_photo: bool = False,
                internals: bool = False) -> dict:
        """``internals``: also return the photo and masks, measured without this
        lot's hand fix (used to learn from that fix)."""
        attrs = self.parcel_attrs(parcel_feature["attributes"])
        P_wgs = parcel_feature["geometry"]
        P = self.proj.to_local(P_wgs).buffer(0)
        lot_area = P.area
        note = ""
        rest_of_lot = None
        if lot_area > _ACREAGE_SQFT:
            # Acreages: the mowed yard is the home site, not the whole farm.
            # Measure within ~150 ft of the buildings; treat the rest of the
            # lot like a neighbour so its edges aren't taken for streets.
            penv = self.proj.to_wgs(box(*P.bounds)).bounds
            bl = [self.proj.to_local(f["geometry"]).buffer(0) for f in self.buildings.query(penv)]
            bl = [b for b in bl if b.intersection(P).area > 0.5 * b.area and b.area > 200]
            if bl:
                site = P.intersection(unary_union([b.buffer(_HOMESITE_FT) for b in bl])).buffer(0)
                if site.geom_type == "MultiPolygon":
                    site = max(site.geoms, key=lambda g: g.area)
                if site.geom_type == "Polygon" and site.area < 0.9 * lot_area:
                    rest_of_lot = P.difference(site).buffer(-0.5)
                    P = site
                    note = f"acreage: yard measured within {int(_HOMESITE_FT)} ft of the buildings"
            else:
                note = "acreage with no mapped buildings"
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
        if self.c["parcels"].get("row_parcels"):
            # Some counties map the street right-of-way as parcels too: long,
            # thin and much bigger than a lot. They aren't neighbours, so the
            # street side of the lot can still be found.
            neigh = [g for g in neigh
                     if not (g.area > 2.0 * P.area and 4 * math.pi * g.area / max(g.length, 1.0) ** 2 < 0.08)]
        if self.streets and neigh:
            # A "neighbour" a street centreline runs down the middle of is the
            # right-of-way mapped as a parcel (common in Iowa and Texas data):
            # dropping it lets the street side of the lot be found.
            try:
                cl = [self.proj.to_local(f["geometry"]) for f in self.streets.query(env)]
                cl = unary_union(cl) if cl else None
                if cl is not None and not cl.is_empty:
                    neigh = [g for g in neigh if g.buffer(-3.0).intersection(cl).length < 40.0
                             or g.intersection(P.buffer(1.0)).area > 0.5 * g.area]
            except Exception:
                pass
        if rest_of_lot is not None and not rest_of_lot.is_empty:
            neigh.append(rest_of_lot)
        N = unary_union(neigh) if neigh else None
        bldgs = [self.proj.to_local(f["geometry"]).buffer(0) for f in self.buildings.query(env)]
        B = unary_union([b for b in bldgs if b.intersects(ctx_box)]) if bldgs else None
        street_lines = None
        if self.pavement:
            pave = self.pavement.get_local(env, ctx_box)
        elif self.streets:
            hw = float(self.c["streets"].get("half_width_ft", 15.0))
            lines = [self.proj.to_local(f["geometry"]) for f in self.streets.query(env)]
            lines = [g for g in lines if g.intersects(ctx_box.buffer(hw))]
            street_lines = [g.intersection(ctx_box.buffer(5)) for g in lines]
            pave = [g.buffer(hw, cap_style=2).intersection(ctx_box) for g in lines]
            pave = [g for g in pave if not g.is_empty]
        else:
            pave = []
        if self.pavement or not self.streets:
            street_lines = None
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
            # front lot lines sit 10-25 ft from the curb; a road 40+ ft past the
            # back fence (homes backing onto an arterial) isn't this lot's street
            near = V.buffer(FRONTAGE_MAX_FT)
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

        # Line the photo up with the map: national photos (NAIP) can sit 10-20
        # ft off the county's lot and building lines, which puts roof on the
        # driveway and lawn on the roof. Slide the photo so its roof edges
        # land on the building outlines.
        coarse = "NAIP" in str(img.get("source", "")).upper() or str(img.get("tile_url", "")).startswith("export:")
        shift_px = _photo_shift(mos.rgb, bldg_m, max(2, int(round(15.0 / ft_px)))) if coarse else (0, 0)
        if shift_px != (0, 0):
            mos.rgb = ndi.shift(mos.rgb, (-shift_px[0], -shift_px[1], 0), order=0, mode="nearest")

        hard = classify_hard(mos.rgb)
        ndvi = None
        try:
            ndvi = ndvi_for(mos, self.c.get("infrared"))
            if ndvi is not None and shift_px != (0, 0):
                ndvi = ndi.shift(ndvi, (-shift_px[0], -shift_px[1]), order=0, mode="nearest")
        except Exception:
            ndvi = None
        if ndvi is not None:
            # paving must also be "not growing" in the infrared: kills grass
            # that a washed-out colour photo makes look like gravel
            veg = ndi.uniform_filter(ndvi, 5) > 0.22
            # NAIP is flown in summer: a tree's leaves can cover a driveway
            # that is plainly bright, colourless concrete in the leaf-off photo
            sm = ndi.uniform_filter(mos.rgb, size=(5, 5, 1))
            mx_, mn_ = sm.max(-1), sm.min(-1)
            plain_concrete = (mx_ > 0.70) & ((mx_ - mn_) / (mx_ + 1e-6) < 0.10)
            hard &= ~veg | plain_concrete
            hard = ndi.binary_opening(hard, iterations=1)
        hard &= ~bldg_m
        if True:  # what people's fixes taught us about this county's photos
            try:
                from . import learning
                hard = learning.adjust_hard(self.cid, str(img.get("source", "")), mos.rgb, hard, parcel_m & ~bldg_m)
            except Exception:
                pass
        if not self.pavement and street_lines:
            # No edge-of-pavement layer: the street is the paving outside the
            # lots that connects to a street centreline (handles wide
            # arterials and cul-de-sac bulbs a fixed width can't).
            centre = to_mask(unary_union(street_lines).buffer(4.0))
            outside = hard & ~parcel_m
            if N is not None:
                outside &= ~to_mask(N)
            lab, nl = ndi.label(outside)
            if nl:
                keep = np.unique(lab[centre & outside])
                keep = keep[keep > 0]
                if len(keep):
                    road = np.isin(lab, keep)
                    # keep only street-width paving: sidewalks and aprons
                    # (< ~7 ft wide) hang off the street but aren't street
                    k = max(1, int(round(3.5 / ft_px)))
                    road = ndi.binary_opening(road, iterations=k)
                    lab2, n2 = ndi.label(road)
                    if n2:
                        keep2 = np.unique(lab2[centre & road])
                        road = np.isin(lab2, keep2[keep2 > 0])
                    pave_m = ndi.binary_dilation(road, iterations=2) | pave_m

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
        out_hard = []  # per station: feet of paving seen beyond the lot line
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
                if not self.pavement and V is not None:
                    # walk out to where the (photo-detected) street starts
                    for o in np.arange(0.5, 60.0, 0.5):
                        if sample(pave_m, Point(p.x + nx * o, p.y + ny * o)):
                            dist_pave = float(o)
                            break
                reach = min(max(dist_pave, 0.0), 30.0)
                # walk from 8 ft inside the lot out to the curb
                offs = np.arange(-8.0, reach + 0.01, 0.5)
                vals = [sample(hard, Point(p.x + nx * o, p.y + ny * o)) and not sample(pave_m, Point(p.x + nx * o, p.y + ny * o)) for o in offs]
                run = _longest_run(vals)
                rs = re = None
                if run:
                    rs, re = offs[run[0]], offs[run[1]]
                stations.append((p, (nx, ny), dist_pave, rs, re))
                # visible paving between the lot line and the street (apron)
                out_hard.append(0.5 * sum(1 for o, v in zip(offs, vals) if v and o > 0))

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
        # Station by station: a walk/trail strip just inside the lot line (a
        # 2.5-10 ft run of paving that ends near the line) is a sidewalk, not
        # the start of a driveway, even where other sides have it outside.
        strips = []
        for p, (nx, ny), _, rs, re in stations:
            # must have lawn on its inner side (rs > -7.5): a driveway runs
            # on past the 8-ft look-in window and is not a strip
            if rs is None or not (-7.5 < rs < -1.0 and re <= 1.5 and 2.5 <= re - rs <= 10):
                continue
            tx, ty = -ny * step * 0.6, nx * step * 0.6
            a0, a1 = rs - 0.5, re + 0.5
            strips.append(Polygon([(p.x - tx + nx * a0, p.y - ty + ny * a0), (p.x + tx + nx * a0, p.y + ty + ny * a0),
                                   (p.x + tx + nx * a1, p.y + ty + ny * a1), (p.x - tx + nx * a1, p.y - ty + ny * a1)]))
        if len(strips) >= 4:  # a real strip runs along the line, not one spot
            sw_band_m |= to_mask(unary_union(strips).intersection(P))

        # ------------------------------------------------------------ driveway
        lot_hard = hard & parcel_m & ~sw_band_m
        lot_hard = _drop_small(lot_hard, _MIN_BLOB_SQFT / sqft_px)
        front_zone = to_mask(unary_union(frontage).buffer(sw_w + 8.0 if sw_inside else 8.0).intersection(P)) if frontage else np.zeros(hard.shape, bool)
        # lot lines on public right-of-way of any kind (streets and alleys)
        row_side = P.exterior.intersection(row.buffer(2.0)) if not row.is_empty else None
        row_zone = to_mask(row_side.buffer(6.0).intersection(P)) if row_side is not None and not row_side.is_empty else np.zeros(hard.shape, bool)
        # ...but only where it's paved just outside (an alley), not a field or creek
        paved_out = ndi.binary_dilation((hard | pave_m | classify_hard(mos.rgb)) & ~parcel_m,
                                        iterations=max(1, int(round(8.0 / ft_px))))
        row_zone &= paved_out
        if frontage and row_zone.any():
            # an alley runs behind the house: only the back half of the lot,
            # away from the street (not a corner lot's side street or strip)
            fl = to_mask(unary_union(frontage).buffer(2.0))
            if fl.any():
                dfront = ndi.distance_transform_edt(~fl)
                row_zone &= dfront > 0.5 * float(dfront[parcel_m].max())
        raw_lot_hard = lot_hard
        lot_hard, eave_m = _sensible_paving(lot_hard, mos.rgb, parcel_m & ~bldg_m, bldg_m, sw_band_m | front_zone | row_zone,
                                            ft_px, sqft_px)
        hard = hard & ~(raw_lot_hard & ~lot_hard)  # patches judged to be grass count as lawn
        bldg_m = bldg_m | eave_m  # roof-edge strips: neither patio nor lawn
        # street side and alley side (behind the house)
        labels, nlab = ndi.label(lot_hard, structure=np.ones((3, 3)))
        touching = np.unique(labels[(front_zone | row_zone) & lot_hard])
        touching = touching[touching > 0]
        # keep the biggest frontage-touching blobs (2 = circle drives / side-load)
        if len(touching):
            # a driveway leads to the house (garage) and is car-wide
            near_house = ndi.binary_dilation(bldg_m, iterations=max(1, int(round(30.0 / ft_px)))) if bldg_m.any() else None
            ok_ids = [t for t in touching if _drive_shaped(labels == t, near_house, ft_px)]
            touching = np.array(ok_ids, dtype=touching.dtype) if ok_ids else touching[:0]
        if len(touching):
            sizes = ndi.sum(lot_hard, labels, touching)
            order = np.argsort(sizes)[::-1]
            keep = [touching[i] for i in order[:2] if sizes[i] * sqft_px >= 60]
        else:
            keep = []
        drive_m = np.isin(labels, keep) if keep else np.zeros(hard.shape, bool)
        if drive_m.any() and frontage and building_in is not None and not building_in.is_empty:
            # A driveway runs from the street to the garage, not on past the
            # house into the back yard: whatever lies deeper than the house
            # front + 25 ft has to look like a slab on its own to stay paving.
            front_line = to_mask(unary_union(frontage).buffer(2.0)) | row_zone  # street or alley
            if front_line.any():
                depth = ndi.distance_transform_edt(~front_line) * ft_px
                limit = building_in.distance(unary_union(frontage)) + 25.0
                far = drive_m & (depth > limit)
                if far.any():
                    drive_m &= ~far
                    slab, _ = _sensible_paving(far, mos.rgb, parcel_m & ~bldg_m, np.zeros(hard.shape, bool),
                                               np.zeros(hard.shape, bool), ft_px, sqft_px)
                    lot_hard = lot_hard & ~(far & ~slab)
                    hard = hard & ~(far & ~slab)
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
            for si, (p, (nx, ny), dist_pave, _, _) in enumerate(stations):
                inside = Point(p.x - nx * 1.5, p.y - ny * 1.5)
                if sample(drive_m, inside):
                    gap = min(dist_pave, 30.0) - (0 if sw_inside or not sw_stations else sw_w)
                    # never more than the paving actually visible out to the street
                    seen = out_hard[si] if si < len(out_hard) else gap
                    apron_sqft += max(0.0, min(gap, seen)) * step
        driveway_sqft = drive_lot_sqft + apron_sqft
        driveway_method = "imagery"
        if driveway_sqft < 120 and building_in is not None and not building_in.is_empty and V is not None \
                and building_in.area >= 600:
            # Every house has a driveway. Shadowed, tree-covered or the same
            # colour as the lawn: fall back to a typical-width slab from the
            # house to the street, and say so.
            gap = building_in.distance(V)
            if 5 < gap < 120:
                dw = float(self.c.get("default_driveway_width_ft", 16))
                driveway_sqft = dw * gap
                driveway_method = "estimated"
                in_lot = building_in.distance(unary_union(frontage)) if frontage else building_in.distance(P.exterior)
                est_in_lot = dw * min(gap, in_lot if frontage else max(0.0, gap - 12.0))
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
        struct_m = bed_m = canopy_m = crown_m = fence_m = np.zeros(hard.shape, bool)
        tops = []
        if lid is not None:
            struct_m, bed_m, canopy_m, crown_m, tops, fence_m, high_m = lid
            roofish = high_m & ndi.binary_dilation(bldg_m, iterations=max(1, int(round(6.0 / ft_px))))
            if roofish.any():  # roof edges the photo called paving: neither patio nor lawn
                other_m &= ~roofish
                other_sqft = float(other_m.sum()) * sqft_px
                bldg_m = bldg_m | roofish
                yard = yard & ~roofish
            crown_m = crown_m & ~bldg_m
            struct_m &= yard
            # decks, sheds and play sets aren't green; leafy "structures" are
            # tree tops the LiDAR caught as solid (the grass under them is lawn)
            rgbf = ndi.uniform_filter(mos.rgb[..., :3].astype(np.float32), size=(3, 3, 1))
            exg = (2 * rgbf[..., 1] - rgbf[..., 0] - rgbf[..., 2]) / (rgbf.sum(-1) + 1e-6)
            leafy = exg > 0.06
            if ndvi is not None:
                leafy = leafy | (ndi.uniform_filter(ndvi, 3) > 0.30)
            lab_s, n_s = ndi.label(struct_m)
            if n_s:
                green_share = ndi.mean(leafy, lab_s, range(1, n_s + 1))
                drop = np.zeros(n_s + 1, bool)
                drop[1:] = np.asarray(green_share) > 0.5
                struct_m = struct_m & ~drop[lab_s]
            canopy_m &= yard & ~struct_m
            bed_m &= yard & ~hard & ~struct_m & ~canopy_m
        pool_m = _pool_shapes(_pools(mos.rgb) & yard & ~struct_m, sqft_px)
        lawn_m = yard & ~hard & ~struct_m & ~bed_m & ~canopy_m & ~pool_m
        lawn_m = ndi.binary_opening(lawn_m, iterations=1)
        lawn_m = _drop_small(lawn_m, 4.0 / sqft_px)
        est_cut = 0.0
        if driveway_method == "estimated":
            est_cut = est_in_lot
        # hand corrections saved by the business for this property
        from .counties import DEFAULT_COUNTY
        corr = None if internals else (corrections.get(f"{self.cid}:{pin}") or (corrections.get(pin) if self.cid == DEFAULT_COUNTY else None))
        if internals:
            internals_out = {"mos": mos, "lawn": lawn_m.copy(), "yard": yard.copy(), "paved": (hard | drive_m) & yard,
                             "source": str(img.get("source", ""))}
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
        pool_sqft = float(_drop_small(pool_m, 60.0 / sqft_px).sum()) * sqft_px
        # Fences: most stand on the lot lines, so walk the lot line and count
        # the feet with a fence within ~3 ft. "Fenced yard" = fences along most
        # of the side and back lines (the street side doesn't count).
        lot_line = parcel_m & ~ndi.binary_erosion(parcel_m, iterations=1)
        near_fence = ndi.binary_dilation(fence_m, iterations=max(1, int(round(3.0 / ft_px))))
        fence_px = lot_line & near_fence
        fence_ft = float(fence_px.sum()) * ft_px * 0.9  # pixel steps along a line run ~10% long
        street = to_mask(unary_union(frontage).buffer(15.0)) if frontage else np.zeros(hard.shape, bool)
        back_line = lot_line & ~street
        fenced_share = float((back_line & near_fence).sum()) / max(1, int(back_line.sum()))
        fenced_yard = bool(fence_ft >= 40 and fenced_share >= 0.6)
        beds_sqft = float((bed_m | canopy_m).sum()) * sqft_px
        # Trees: a tree belongs to this lot when its trunk (crown top) is on it.
        # Canopy counts every crown over the lot, including a neighbour's
        # tree hanging over the fence. Lawn under deciduous trees stays lawn
        # (it gets mowed); dense evergreens were already taken out above.
        lot_tops = [t for t in tops if parcel_m[t[0], t[1]]]
        tree_canopy_sqft = float((crown_m & parcel_m).sum()) * sqft_px
        lawn_under_trees_sqft = float((lawn_m & crown_m).sum()) * sqft_px

        quality, why = "ok", []
        open_area = max(1.0, P.area - building_sqft)
        if driveway_sqft > max(5000.0, 0.25 * P.area) or (driveway_sqft > 2000 and lot_area < 15000):
            why.append("driveway unusually large")
        if (drive_lot_sqft + other_sqft) > 0.6 * open_area:
            why.append("most of the yard reads as paving")
        liv = _num(attrs.get("bldg_sqft"))
        if liv and building_sqft > max(6000.0, 4 * liv):
            why.append("buildings much bigger than the house on record")
        if lawn_sqft < 50 and open_area > 2000 and not why:
            why.append("no lawn found")
        if why:
            quality = "review"
        result = {
            "pin": pin,
            "pid": f"{self.cid}:{pin}",
            "address": (attrs.get("address") or "").strip(),
            "city": attrs.get("city"),
            "zip": attrs.get("zip"),
            "class": attrs.get("class"),
            "lot_sqft": round(lot_area),
            "measured_area_sqft": round(P.area),
            "building_sqft": round(building_sqft),
            "lawn_sqft": round(lawn_sqft),
            "parkway_sqft": round(parkway_sqft),
            "driveway_sqft": round(driveway_sqft),
            "sidewalk_sqft": round(sidewalk_sqft),
            "other_hardscape_sqft": round(other_sqft),
            "structures_sqft": round(structures_sqft),
            "pool_sqft": round(pool_sqft),
            "has_pool": bool(pool_sqft >= 150),  # beta: not sold until validated
            "fence_ft": round(fence_ft),  # beta: not sold until validated
            "fenced_yard": fenced_yard,
            "beds_sqft": round(beds_sqft),
            **({"tree_count": len(lot_tops), "tree_canopy_sqft": round(tree_canopy_sqft),
                "lawn_under_trees_sqft": round(lawn_under_trees_sqft),
                "trees": [{"lat": round(la, 7), "lon": round(lo, 7), "height_ft": round(hh)}
                          for la, lo, hh in ((mos.px_to_lonlat(c + 0.5, r + 0.5)[1], mos.px_to_lonlat(c + 0.5, r + 0.5)[0], h_)
                                             for r, c, h_ in lot_tops)]} if MEASURE_TREES else {}),
            "frontage_ft": round(frontage_ft),
            **self._house_walls(P_wgs, env, lid is not None),
            "home_value": _num(attrs.get("home_value")),
            "year_built": _year(attrs.get("year_built")),
            "living_sqft": _num(attrs.get("bldg_sqft")),
            "assessor": {
                "lot_sqft": attrs.get("lot_sqft"),
                "living_sqft": attrs.get("bldg_sqft"),
                "building": attrs.get("bldg_desc"),
                "year_built": attrs.get("year_built"),
                "home_value": attrs.get("home_value"),
                "land_value": attrs.get("land_value"),
                "sale_price": attrs.get("sale_price"),
            },
            "sources": {
                "imagery_year": img.get("year"),
                "imagery_missing_tiles": mos.missing,
                "sidewalk_detected": bool(sw_stations),
                "driveway_method": driveway_method,
                "lidar_year": self.c["lidar"]["year"] if lid is not None else None,
                "lidar_source": self.c["lidar"].get("source") if lid is not None else None,
                "imagery_source": img.get("source"),
                "infrared": bool(ndvi is not None),
                "county": self.cid,
                "quality": quality,
                "note": "; ".join([x for x in [note] + why if x]),
                "corrected": bool(corr),
            },
        }
        if want_overlay:
            result["overlay"] = _overlay_png(mos, parcel_m, lawn_m | parkway_m, drive_m, other_m, struct_m | pool_m, bed_m | canopy_m,
                                             frontage, sw_stations, self, step, sw_w,
                                             crown_m=crown_m & (parcel_m | parkway_m), tops=lot_tops,
                                             fence_m=fence_px,
                                             photo=want_photo)
            if want_photo:
                result["photo_jpg"] = result["overlay"].pop("photo_jpg", "")
            result["parcel_geojson"] = _geojson(P_wgs)
        if internals:
            result["_internals"] = internals_out
        return result


# --------------------------------------------------------------------- helpers
def _lidar_masks(obj, veg, gnd, ft_px, sqft_px, inten=None, bldg=None):
    """Turn sampled LiDAR heights into structure / bed / evergreen masks."""
    r = lambda ft: max(1, int(round(ft / ft_px)))
    # Raised non-tree objects. The opening drops anything under ~2.5 ft wide
    # (fences, posts, mailboxes) so mowable lawn along a fence isn't lost.
    # Decks, sheds and play sets stand under ~14 ft; anything taller in a yard
    # is a tree top the scan caught as a solid return (common in USGS data).
    tall = ndi.binary_dilation((obj > 14.0) | (veg > 14.0), iterations=r(2.0))
    struct = (obj > 0.8) & (obj < 14.0) & ~tall
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


def _trees(veg, ft_px, sqft_px, sparse=False):
    """Tree crowns and individual trees from the vegetation height raster.

    Crowns: vegetation taller than 8 ft (shrubs and hedges stay beds).
    Trees: tops of the smoothed canopy height model, at least 12 ft tall and
    no closer than the crown radius of a tree that tall (~18% of its
    height, 5-11 ft), so a big tree's lumpy crown isn't counted several times
    while young trees planted in a row still are. Works on leaf-off scans too: branches
    still return multiple echoes."""
    r = lambda ft: max(1, int(round(ft / ft_px)))
    v = np.where(np.isfinite(veg), veg, -1).astype(np.float32)
    crown = v > 8.0
    crown = ndi.binary_closing(crown, iterations=r(2.0))  # gaps between bare branches
    crown = ndi.binary_opening(crown, iterations=r(1.0))
    crown = _drop_small(crown, 25.0 / sqft_px)
    if not crown.any():
        return crown, []
    chm = ndi.grey_dilation(np.where(v > 0, v, 0), size=r(2.0))  # fill sparse holes
    chm = ndi.gaussian_filter(chm, r(2.5 if sparse else 1.5))
    peak = _disk_peaks(chm, crown, r)
    lab, n = ndi.label(peak)
    tops = []
    if n:
        for (sl_r, sl_c), i in zip(ndi.find_objects(lab), range(1, n + 1)):
            rr, cc = np.nonzero(lab[sl_r, sl_c] == i)
            r0, c0 = int(rr.mean()) + sl_r.start, int(cc.mean()) + sl_c.start
            tops.append((r0, c0, float(chm[r0, c0])))
    return crown, tops

def _disk_peaks(chm, crown, r):
    """Tree tops: pixels at least as tall as everything within the crown
    radius for their height. Same answer as a full disk maximum filter, but
    only pixels that already top their 3x3 neighbourhood are checked (a disk
    filter over the whole raster was the slowest step of a home)."""
    peak = np.zeros(chm.shape, bool)
    cand = crown & (chm > 12) & (chm >= ndi.maximum_filter(chm, size=3) - 1e-3)
    if not cand.any():
        return peak
    H, W = chm.shape
    disks = {}
    for y, x in zip(*np.nonzero(cand)):
        h = chm[y, x]
        rad = r(5.0 if h <= 30 else 7.5 if h <= 45 else 10.5)
        d = disks.get(rad)
        if d is None:
            d = disks[rad] = _disk(rad)
        y0, y1, x0, x1 = max(0, y - rad), min(H, y + rad + 1), max(0, x - rad), min(W, x + rad + 1)
        win = chm[y0:y1, x0:x1]
        fp = d[y0 - (y - rad):y1 - (y - rad), x0 - (x - rad):x1 - (x - rad)]
        if h >= win[fp].max() - 1e-3:
            peak[y, x] = True
    return peak


def _disk(rad: int) -> np.ndarray:
    yy, xx = np.ogrid[-rad:rad + 1, -rad:rad + 1]
    return xx * xx + yy * yy <= rad * rad


def _fences(obj, veg, ft_px, bldg_m=None):
    """Thin raised lines 2.5-8.5 ft tall: fences and walls (not sheds, decks,
    houses, trees or shrubs)."""
    r = lambda ft: max(1, int(round(ft / ft_px)))
    raised = (obj > 2.5) & (obj < 8.5)
    solid = ndi.binary_opening(raised, iterations=r(1.6))          # anything wider than ~3 ft
    thin = raised & ~ndi.binary_dilation(solid, iterations=r(0.6))
    thin &= ~ndi.binary_dilation(veg > 2.0, iterations=r(1.0))       # hedges and tree edges
    if bldg_m is not None:
        thin &= ~ndi.binary_dilation(bldg_m, iterations=r(1.5))
    thin = ndi.binary_closing(thin, iterations=r(1.0))              # join posts / gaps
    lab, n = ndi.label(thin)
    if n:
        # keep line-like runs at least ~8 ft long
        objs = ndi.find_objects(lab)
        keep = np.zeros(n + 1, bool)
        for i, sl in enumerate(objs, 1):
            hgt = (sl[0].stop - sl[0].start) * ft_px
            wid = (sl[1].stop - sl[1].start) * ft_px
            keep[i] = max(hgt, wid) >= 8.0
        thin = keep[lab]
    return thin


def _otsu(v: np.ndarray, bins: int = 64) -> float:
    hist, edges = np.histogram(v, bins=bins)
    p = hist / max(hist.sum(), 1)
    w = np.cumsum(p)
    mu = np.cumsum(p * (edges[:-1] + edges[1:]) / 2)
    between = (mu[-1] * w - mu) ** 2 / np.maximum(w * (1 - w), 1e-9)
    return float((edges[:-1] + edges[1:])[np.argmax(between)] / 2)


def _photo_shift(rgb: np.ndarray, bldg_m: np.ndarray, max_px: int) -> tuple[int, int]:
    """(dy, dx) pixels the photo is off from the building outlines. Roofs
    aren't green and the lawn around a house is, so the right shift is the
    one where the band just outside the outlines is greenest compared with
    inside them. (0, 0) unless clearly better than no shift."""
    if bldg_m.sum() < 200:
        return (0, 0)
    sm = ndi.uniform_filter(rgb[..., :3].astype(np.float32), size=(3, 3, 1))
    exg = (2 * sm[..., 1] - sm[..., 0] - sm[..., 2]) / (sm.sum(-1) + 1e-6)
    k = max(2, max_px // 3)
    inside = ndi.binary_erosion(bldg_m, iterations=2)
    ring = ndi.binary_dilation(bldg_m, iterations=k) & ~ndi.binary_dilation(bldg_m, iterations=1)
    iy, ix = np.nonzero(inside)
    ry, rx = np.nonzero(ring)
    if len(iy) < 50 or len(ry) < 50:
        return (0, 0)
    H, W = exg.shape
    def score(dy, dx):
        return float(exg[np.clip(ry + dy, 0, H - 1), np.clip(rx + dx, 0, W - 1)].mean()
                     - exg[np.clip(iy + dy, 0, H - 1), np.clip(ix + dx, 0, W - 1)].mean())
    base = score(0, 0)
    best = (base, 0, 0)
    for dy in range(-max_px, max_px + 1):
        for dx in range(-max_px, max_px + 1):
            v = score(dy, dx)
            if v > best[0]:
                best = (v, dy, dx)
    # needs a real gain in contrast, and not just a pixel or two
    if best[0] - base < 0.015 or max(abs(best[1]), abs(best[2])) <= 1:
        return (0, 0)
    return (best[1], best[2])


def _texture(rgb: np.ndarray, size: int) -> np.ndarray:
    """Local brightness variation: grass (even brown winter grass) is speckled,
    concrete and asphalt are smooth."""
    lum = rgb[..., :3].mean(-1).astype(np.float32)
    m = ndi.uniform_filter(lum, size)
    m2 = ndi.uniform_filter(lum * lum, size)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def _sensible_paving(hard, rgb, yard_m, bldg_m, anchors, ft_px, sqft_px):
    """Common sense on what the colour classifier called paving, patch by patch.
    Paving that touches the house, the sidewalk or the street side is kept:
    driveways, front walks and patios are joined to something. A patch
    floating out in the yard has to look like a slab to stay paving: smooth
    (not as speckled as the lawn) and solid (not a ragged scatter), unless
    it's bright, flat, colourless concrete. Otherwise it's dry or patchy grass."""
    if not hard.any():
        return hard, np.zeros(hard.shape, bool)
    size = max(3, int(round(3.0 / ft_px)) | 1)
    tex = _texture(rgb, size)
    lawnish = yard_m & ~ndi.binary_dilation(hard, iterations=2)
    if lawnish.sum() < 50:
        return hard, np.zeros(hard.shape, bool)
    grass_tex = float(np.median(tex[lawnish]))
    sm = ndi.uniform_filter(rgb[..., :3].astype(np.float32), size=(5, 5, 1))
    mx_, mn_ = sm.max(-1), sm.min(-1)
    plain = (mx_ > 0.62) & ((mx_ - mn_) / (mx_ + 1e-6) < 0.10)
    grow = max(1, int(round(3.0 / ft_px)))
    anchor = ndi.binary_dilation(anchors | bldg_m, iterations=grow)
    street = ndi.binary_dilation(anchors, iterations=grow)
    hug = ndi.binary_dilation(bldg_m, iterations=max(1, int(round(5.0 / ft_px)))) if bldg_m.any() else np.zeros(hard.shape, bool)
    eave = np.zeros(hard.shape, bool)
    lab, n = ndi.label(hard, structure=np.ones((3, 3)))
    if not n:
        return hard, np.zeros(hard.shape, bool)
    keep = np.zeros(n + 1, bool)
    objs = ndi.find_objects(lab)
    for i, sl in enumerate(objs, 1):
        blob = lab[sl] == i
        area = float(blob.sum()) * sqft_px
        inner = ndi.binary_erosion(blob, iterations=1)
        t = float(np.median(tex[sl][inner if inner.sum() >= 6 else blob]))
        bright = float(plain[sl][blob].mean())
        hull_fill = blob.sum() / max(1.0, float(_hull_area(blob)))
        touches = bool((anchor[sl] & blob).any())
        if touches:
            # a thin strip lying along the house wall, not reaching the street:
            # the photo's roof edge (or drip line), not a patio
            width = 2 * float(ndi.distance_transform_edt(blob).max()) * ft_px
            on_street = bool((street[sl] & blob).any())
            if not on_street and width < 6.0 and float(hug[sl][blob].mean()) > 0.8:
                eave[sl] |= blob
                continue
            if on_street:  # driveways and front walks: always paving
                keep[i] = True
                continue
            # against the house only: a patio is still a slab, smooth or solid
            keep[i] = bright > 0.5 or t < 0.85 * grass_tex or hull_fill >= 0.45
            continue
        smooth = t < 0.85 * grass_tex
        solid = hull_fill >= 0.45
        keep[i] = bright > 0.5 or (smooth and solid and area >= 40)
    return keep[lab], eave


def _hull_area(blob: np.ndarray) -> float:
    ys, xs = np.nonzero(blob)
    if len(xs) < 3:
        return float(len(xs))
    try:
        from shapely.geometry import MultiPoint
        return max(float(MultiPoint(list(zip(xs.tolist(), ys.tolist()))).convex_hull.area), float(len(xs)))
    except Exception:
        return float(len(xs))


def _drive_shaped(blob: np.ndarray, near_house, ft_px: float) -> bool:
    """A driveway is at least car-wide (8 ft) and leads toward the house."""
    if not blob.any():
        return False
    width_px = 2 * float(ndi.distance_transform_edt(blob).max())
    if width_px * ft_px < 8.0:
        return False
    if near_house is None:
        return True
    ys, xs = np.nonzero(blob)
    long_side = max(xs.max() - xs.min(), ys.max() - ys.min()) * ft_px
    return bool((near_house & blob).any()) or long_side >= 40.0


def classify_hard(rgb: np.ndarray) -> np.ndarray:
    """Pixel classifier for paved / concrete surfaces.

    Concrete and asphalt are near-neutral grey; grass (dormant or green),
    mulch and soil always have less blue than red/green. Photos differ a lot
    by county and season (tan leaf-off Douglas vs washed-out Lincoln), so the
    cut-off on that "blue deficit" is picked per photo (Otsu's method),
    within sane limits. On strongly coloured leaf-off photos this agrees with
    the original fixed saturation rule.
    """
    sm = ndi.uniform_filter(rgb, size=(3, 3, 1))
    mx = sm.max(-1)
    mn = sm.min(-1)
    sat = (mx - mn) / (mx + 1e-6)
    if np.median(sat) > 0.25:
        # vivid leaf-off photo (Douglas 2025): original, field-checked rule
        hard = ((sat < 0.27) & (mx > 0.42)) | ((sat < 0.20) & (mx > 0.18))
    else:
        y = ((sm[..., 0] + sm[..., 1]) / 2 - sm[..., 2]) / (mx + 1e-6)
        valid = mx > 0.15
        t = _otsu(y[valid][:: max(1, valid.sum() // 200000)]) if valid.any() else 0.15
        t = float(np.clip(t, 0.06, 0.25))
        hard = (y < t) & (sat < 0.30) & (mx > 0.18)
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
    # water is bright and clearly blue-green; shade only leans blue and is dark
    m = (b > r * 1.08) & (b >= g * 0.97) & (sat > 0.15) & (mx > 0.38) & ((b > g * 1.05) | (mx > 0.48))
    return ndi.binary_opening(m, iterations=2)


def _pool_shapes(m: np.ndarray, sqft_px: float) -> np.ndarray:
    """Keep pool-sized, pool-shaped patches only: 120-1,500 sq ft and a solid
    outline (rectangle or kidney). Blue-looking tree shade is ragged and big."""
    lab, n = ndi.label(m)
    if not n:
        return m
    best, best_area = 0, 0.0
    for i, sl in enumerate(ndi.find_objects(lab), 1):
        blob = lab[sl] == i
        area = float(blob.sum()) * sqft_px
        if 120.0 <= area <= 1500.0 and blob.sum() / max(1.0, _hull_area(blob)) >= 0.7 and area > best_area:
            best, best_area = i, area
    return lab == best if best else np.zeros(m.shape, bool)  # one pool per home at most


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


def _num(v):
    try:
        f = float(v)
        return round(f) if f > 0 else None
    except (TypeError, ValueError):
        return None


def _year(v):
    n = _num(v)
    return n if n and 1700 <= n <= 2100 else None


def _geojson(g):
    from shapely.geometry import mapping
    return mapping(g)


def _overlay_png(mos, parcel_m, lawn_m, drive_m, other_m, struct_m, bed_m, frontage, sw_stations, meas, step, sw_w,
                 crown_m=None, tops=(), photo=False, fence_m=None):
    h, w = parcel_m.shape
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[lawn_m] = (34, 197, 94, 110)
    rgba[other_m] = (234, 179, 8, 150)
    rgba[struct_m] = (239, 68, 68, 160)
    rgba[bed_m] = (168, 85, 247, 150)
    rgba[drive_m] = (59, 130, 246, 170)
    # sidewalk band (drawn along detected stations)
    if sw_stations:
        # a smooth band: one quad per station, from its curb-side to its
        # house-side edge, half a station wide on either side
        from PIL import ImageDraw
        band = Image.new("L", (w, h), 0)
        dr = ImageDraw.Draw(band)

        def px(xf, yf):
            lon, lat = meas.proj.inv.transform(xf, yf)
            return mos.to_px(lon, lat)
        half = step * 0.55
        for p, (nx, ny), _, rs, re in sw_stations:
            tx, ty = -ny, nx  # along the street
            quad = [px(p.x + nx * o + tx * t, p.y + ny * o + ty * t)
                    for o, t in ((rs, -half), (re, -half), (re, half), (rs, half))]
            dr.polygon(quad, fill=255)
        sw = ndi.binary_closing(np.asarray(band) > 0, iterations=2)
        rgba[sw & ~drive_m] = (236, 72, 153, 170)
    if crown_m is not None and crown_m.any():
        # tree crowns: dark green outline over whatever is underneath
        ring = crown_m & ~ndi.binary_erosion(crown_m, iterations=2)
        rgba[ring] = (20, 83, 45, 230)
    rr, cc = np.ogrid[:h, :w]
    for r, c, _ in tops:  # crown top (about where the trunk is)
        d2 = (rr - r) ** 2 + (cc - c) ** 2
        rgba[d2 <= 49] = (250, 250, 250, 255)
        rgba[d2 <= 25] = (20, 83, 45, 255)
    edge = parcel_m ^ ndi.binary_erosion(parcel_m, iterations=2)
    rgba[edge] = (250, 250, 250, 255)
    if fence_m is not None and fence_m.any():  # fenced stretches of the lot line, drawn over it
        rgba[ndi.binary_dilation(fence_m, iterations=2)] = (249, 115, 22, 255)
    im = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    W_, S_, E_, N_ = mos.bounds_wgs
    out = {
        "png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "bounds": [[S_, W_], [N_, E_]],
    }
    if photo:
        out["photo_jpg"] = _composite_jpg(mos, im, parcel_m)
    return out


def _composite_jpg(mos, overlay_im, parcel_m, size=900) -> str:
    """The overlay on the aerial photo, cropped square around the lot."""
    try:
        rgb = np.asarray(mos.rgb)
        if rgb.dtype != np.uint8:
            rgb = np.clip(rgb * (255 if rgb.max() <= 1.5 else 1), 0, 255).astype(np.uint8)
        bg = Image.fromarray(rgb).convert("RGBA").resize(overlay_im.size)
        comp = Image.alpha_composite(bg, overlay_im).convert("RGB")
        ys, xs = np.nonzero(parcel_m)
        if not len(xs):
            return ""
        sx, sy = overlay_im.size[0] / parcel_m.shape[1], overlay_im.size[1] / parcel_m.shape[0]
        x0, x1, y0, y1 = xs.min() * sx, xs.max() * sx, ys.min() * sy, ys.max() * sy
        side = max(x1 - x0, y1 - y0) * 1.3
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        crop = comp.crop((int(max(0, cx - side / 2)), int(max(0, cy - side / 2)),
                          int(min(comp.size[0], cx + side / 2)), int(min(comp.size[1], cy + side / 2))))
        crop.thumbnail((size, size))
        b = io.BytesIO()
        crop.save(b, "JPEG", quality=80, optimize=True)
        return base64.b64encode(b.getvalue()).decode()
    except Exception:
        return ""


_measurers: dict = {}
_default_lock = threading.Lock()


def measurer_for(county_id: str | None = None) -> Measurer:
    from .counties import DEFAULT_COUNTY
    cid = county_id or DEFAULT_COUNTY
    with _default_lock:
        if cid not in _measurers:
            _measurers[cid] = Measurer(cid)
        return _measurers[cid]


def default_measurer() -> Measurer:
    return measurer_for(None)
