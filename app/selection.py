"""What a contractor is buying: an area (a ZIP or a shape drawn on the map),
an optional job-size filter on one measurement, and the measurement columns.

One function, ``build``, turns a selection into the exact homes and the exact
price. The page's live quote, the order (checkout amount) and the receipt all
come from it, so they always agree.

Rules
* Area: a whole ZIP, or a drawn polygon that may cross ZIP lines. Every home
  is counted once (by property id), at its map location (inside the polygon).
* Job size: one measurement controls which homes are included ("Driveway:
  1,000 sq ft and up"). Minimum and maximum are inclusive. Homes missing that
  measurement are left out of a size filter (and counted, so it's visible).
* Columns: the filter's measurement is always the first, paid column; extra
  measurements are added at the usual extra-column rate.
* Only measured homes can be filtered or drawn. A whole ZIP that isn't
  measured yet can still be bought; it's measured after purchase.
* ZIP locks: in a drawn area, homes in a ZIP another company holds for one of
  the chosen measurements are left out (and named); a whole-ZIP purchase of
  that ZIP is refused, as before.
"""
from __future__ import annotations

import json
import math

from . import bulk, counties
from . import datastore as ds

MAX_AREA_SQMI = 40.0
MAX_VERTICES = 400
MAX_ZIPS = 12

# measurement type -> the column that measures its size
SIZE_COLUMN = {k: cols[0] for k, _, cols in ds.MEASURE_TYPES if k not in ds.ADDONS}


class SelectionError(ValueError):
    pass


# ------------------------------------------------------------------- area
def _sqmi(poly) -> float:
    lat = poly.centroid.y
    return poly.area * (69.0 * 69.172 * math.cos(math.radians(lat)))


def parse_polygon(gj) -> "object":
    """A drawn area from the page, checked: a single closed polygon, not
    self-crossing, a sensible size, inside the US."""
    from shapely.geometry import shape
    from shapely.validation import explain_validity
    if isinstance(gj, str):
        gj = json.loads(gj)
    if not isinstance(gj, dict) or gj.get("type") != "Polygon" or not gj.get("coordinates"):
        raise SelectionError("Draw an area with at least 3 points.")
    ring = gj["coordinates"][0]
    if len(ring) < 4:
        raise SelectionError("Draw an area with at least 3 points.")
    if len(ring) > MAX_VERTICES:
        raise SelectionError(f"That shape has too many points (over {MAX_VERTICES}). Draw it with fewer clicks.")
    try:
        poly = shape(gj)
    except Exception:
        raise SelectionError("That shape couldn't be read. Clear it and draw again.")
    for x, y in ring:
        if not (-180 <= x <= -60 and 15 <= y <= 72):
            raise SelectionError("Draw the area on the map inside the United States.")
    if not poly.is_valid:
        why = explain_validity(poly)
        if "Self-intersection" in why:
            raise SelectionError("The shape crosses over itself. Clear it and draw the outline without crossing lines.")
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 0:
        raise SelectionError("That shape has no area. Draw at least 3 points around the homes you want.")
    if _sqmi(poly) > MAX_AREA_SQMI:
        raise SelectionError(f"That area is about {_sqmi(poly):,.0f} square miles. Draw up to {MAX_AREA_SQMI:.0f}, "
                             f"or search whole ZIPs instead.")
    return poly


def zips_in(poly) -> list[str]:
    """ZIPs (Census ZCTAs) a drawn area touches."""
    from .gis import _esri_to_geojson_geom, get_json
    ring = [list(map(float, p)) for p in poly.exterior.coords]
    d = get_json(bulk._ZCTA + "/query", {
        "geometry": json.dumps({"rings": [ring], "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPolygon", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ZCTA5", "returnGeometry": "false", "f": "json"}, ttl=30 * 86400)
    zs = sorted({f["attributes"]["ZCTA5"] for f in d.get("features") or []})
    if len(zs) > MAX_ZIPS:
        raise SelectionError(f"That area crosses {len(zs)} ZIP codes. Draw a smaller area (up to {MAX_ZIPS}).")
    return zs


# --------------------------------------------------------------- the spec
def normalize(spec: dict) -> dict:
    """Clean a selection from the page; raises SelectionError for bad input."""
    spec = dict(spec or {})
    area = spec.get("area") or {}
    classes = [c for c in (spec.get("classes") or ["R"]) if c in ("R", "C", "A", "E")] or ["R"]
    out = {"classes": classes, "product": spec.get("product") if spec.get("product") in ("standard", "premier") else "standard"}
    if area.get("polygon"):
        poly = parse_polygon(area["polygon"])
        out["area"] = {"polygon": json.loads(json.dumps(poly.__geo_interface__))}
    else:
        z = "".join(ch for ch in str(area.get("zip") or "") if ch.isdigit())
        if len(z) != 5:
            raise SelectionError("Enter a 5-digit ZIP code, or draw an area.")
        out["area"] = {"zip": z}
    f = spec.get("filter") or {}
    measure = f.get("measure")
    lo, hi = f.get("min"), f.get("max")
    lo = None if lo in (None, "") else float(lo)
    hi = None if hi in (None, "") else float(hi)
    if measure and measure not in SIZE_COLUMN:
        raise SelectionError("Pick one of the listed measurements for job size.")
    if lo is not None and lo < 0 or hi is not None and hi < 0:
        raise SelectionError("Sizes can't be negative.")
    if lo is not None and hi is not None and lo > hi:
        raise SelectionError("The minimum is bigger than the maximum.")
    if not measure:
        lo = hi = None
    out["filter"] = {"measure": measure, "min": lo, "max": hi} if measure and (lo is not None or hi is not None) else None
    types = ds.normalize_types(spec.get("types") or ([measure] if measure else ["lawn"]))
    if measure:  # the size measurement leads (it's the first, paid column)
        types = [measure] + [t for t in types if t != measure]
    out["types"] = types
    v = spec.get("value") or None  # the existing assessed-value range (band indexes)
    out["value"] = [int(v[0]), int(v[1])] if isinstance(v, (list, tuple)) and len(v) == 2 else None
    return out


def describe_filter(spec: dict) -> str:
    f = spec.get("filter")
    if not f:
        return "Every home"
    name = dict((k, l) for k, l, _ in ds.MEASURE_TYPES)[f["measure"]].split(" (")[0].split(" +")[0]
    fmt = lambda v: f"{int(v):,}" if float(v).is_integer() else f"{v:,.1f}"
    unit = "ft" if SIZE_COLUMN[f["measure"]].endswith("_ft") else "sq ft"
    if f["min"] is not None and f["max"] is not None:
        return f"{name}: {fmt(f['min'])}–{fmt(f['max'])} {unit}"
    if f["min"] is not None:
        return f"{name}: {fmt(f['min'])} {unit} and up"
    return f"{name}: up to {fmt(f['max'])} {unit}"


def describe_area(spec: dict, zips: list[str]) -> str:
    if spec["area"].get("zip"):
        return f"ZIP {spec['area']['zip']}"
    return f"Drawn area ({len(zips)} ZIP{'s' if len(zips) != 1 else ''}: {', '.join(zips)})" if zips else "Drawn area"


def size_of(row: dict, measure: str | None):
    col = SIZE_COLUMN.get(measure) if measure else None
    v = row.get(col) if col else row.get("lot_sqft")
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def area_polygon(spec: dict):
    if spec["area"].get("polygon"):
        from shapely.geometry import shape
        return shape(spec["area"]["polygon"])
    return bulk.zip_polygon(spec["area"]["zip"])


# ------------------------------------------------------------------ build
def _imagery(county_names: set[str]) -> list[dict]:
    out = []
    by_name = {c["name"]: c for c in counties.COUNTIES.values()}
    for n in sorted(county_names):
        c = by_name.get(n)
        if not c:
            continue
        img, lid = c.get("imagery") or {}, c.get("lidar") or {}
        out.append({"county": n, "imagery": img.get("source"), "imagery_year": img.get("year"),
                    "lidar_year": lid.get("year"), "lidar": lid.get("source")})
    return out


def build(spec: dict, member: bool = False, email: str | None = None, start_measuring: bool = False) -> dict:
    """The homes this selection buys and what they cost. ``spec`` must be
    normalized. Rows come back in the sale format (all export columns)."""
    from shapely.geometry import Point
    from shapely.prepared import prep
    p = ds.pricing()
    classes, types, product = spec["classes"], spec["types"], spec["product"]
    drawn = bool(spec["area"].get("polygon"))
    poly = area_polygon(spec) if drawn else None
    zips = zips_in(poly) if drawn else [spec["area"]["zip"]]
    f = spec.get("filter")
    measure = f["measure"] if f else None
    out = {"spec": spec, "zips": [], "area_label": describe_area(spec, zips), "filter_label": describe_filter(spec),
           "types": types, "type_labels": ds.type_labels(types), "notes": []}
    # a whole ZIP that isn't measured yet: sold as before, measured after purchase
    z0 = zips[0] if zips else None
    if not drawn and not f and not ds.dataset_ready(z0, classes):
        q = ds.quote(z0, classes, types, product, member, *(spec.get("value") or (None, None)), email=email)
        out.update(mode="zip_unmeasured", count=q["count"], flagged=None, missing=0, rows=None,
                   blocked=q["blocked"], ready=False, price=_price_parts(q), measured_after=True,
                   coverage=[{"zip": z0, "ready": False, "homes": q["count"]}],
                   imagery=_imagery(set(q.get("by_county") or {})))
        out["notes"].append(f"ZIP {z0} is measured after you buy (about {max(5, round(q['count'] * 0.5 / 60))} minutes). "
                            f"Size filters and drawn areas need measured homes.")
        return out
    rows, seen, coverage, missing = [], set(), [], 0
    inside = prep(poly) if poly is not None else None
    blocked = None
    for z in zips:
        ready = ds.dataset_ready(z, classes)
        cov = {"zip": z, "ready": ready, "homes": 0}
        if ready:
            meta = ds.dataset_meta(z, classes) or {}
            cov["measured"] = meta.get("finished")
        coverage.append(cov)
        if not ready:
            if start_measuring:  # never start jobs from a public page: join the background queue if it's running
                cov["measuring"] = ds.queue_zip(z)
            continue
        taken = [t for t in ds.taken_by_others(z, email) if t in types]
        if taken:
            if drawn:
                cov["excluded"] = f"exclusive to another company for {', '.join(ds.type_labels(taken))}"
                continue
            blocked = ds.sale_blocked(z, types, product, email)
        for r in ds.read_dataset(z, classes):
            pin = r.get("pin")
            if not pin or pin in seen:
                continue
            if inside is not None:
                try:
                    if not inside.contains(Point(float(r["lon"]), float(r["lat"]))):
                        continue
                except (TypeError, ValueError, KeyError):
                    continue
            seen.add(pin)
            cov["homes"] += 1
            if spec.get("value"):
                from .bulk import band_of
                lo, hi = ds.band_range(*spec["value"])
                if not (lo <= band_of(r.get("home_value")) <= hi):
                    continue
            if f:
                v = size_of(r, measure)
                if v is None:
                    missing += 1
                    continue
                if (f["min"] is not None and v < f["min"]) or (f["max"] is not None and v > f["max"]):
                    continue
            rows.append(r)
    pending = [c["zip"] for c in coverage if not c["ready"]]
    if pending:
        out["notes"].append(("Not measured yet, so not included: ZIP " + ", ".join(pending) + ". ")
                            + ("They're in line to be measured." if any(c.get("measuring") for c in coverage) else ""))
    for c in coverage:
        if c.get("excluded"):
            out["notes"].append(f"ZIP {c['zip']} is left out: {c['excluded']}.")
    if f and missing:
        out["notes"].append(f"{missing:,} home{'s' if missing != 1 else ''} in the area {'have' if missing != 1 else 'has'} no "
                            f"{SIZE_COLUMN[measure].replace('_sqft', '').replace('_', ' ')} measurement and "
                            f"{'are' if missing != 1 else 'is'} left out of the size filter.")
    from .bulk import band_of
    bands = [0] * (len(bulk.VALUE_EDGES) + 1)
    for r in rows:
        bands[band_of(r.get("home_value"))] += 1
    lp = ds.list_price(0, types, product, member, p, bands=bands, lo=0, hi=len(bands) - 1)
    lp.pop("count", None)
    out.update(mode="selection", count=len(rows), rows=rows, blocked=blocked, ready=True, coverage=coverage,
               flagged=sum(1 for r in rows if (r.get("quality") or "") == "review"), missing=missing,
               price={**lp, "value_bands": bands, "value_multipliers": p.get("value_multipliers")},
               imagery=_imagery({r.get("county") for r in rows if r.get("county")}), measured_after=False)
    return out


def _price_parts(q: dict) -> dict:
    return {k: q.get(k) for k in ("price_cents", "full_price_cents", "per_property_cents", "member_discount",
                                  "base_cents_per_home", "subtotal_cents", "minimum_cents", "minimum_applied", "discount")}


def public_view(b: dict) -> dict:
    """What the page shows: no rows, just the facts and the price."""
    return {k: b.get(k) for k in ("mode", "count", "flagged", "missing", "blocked", "ready", "coverage", "imagery",
                                  "area_label", "filter_label", "types", "type_labels", "notes", "measured_after", "price")} | {
        "columns": [{"key": k, "label": l} for k, l in ds.columns_for(b["types"])],
        "controls": (b["spec"].get("filter") or {}).get("measure"),
        "can_buy": bool(b.get("count")) and not b.get("blocked")}
