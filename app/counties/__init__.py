"""County GIS adapters and the registry that routes a location to its county.

Each county module exposes a ``COUNTY`` dict: where its public parcel,
building, imagery and LiDAR services live and how their fields map onto
what the estimator needs. Add a county by copying one of these files and
registering it below.

Property ids are ``"<county_id>:<parcel id>"``; a bare parcel id means the
default county (Douglas), so older links and saved data keep working.
"""
from __future__ import annotations

import json
import threading

from . import cass_ne, douglas_ne, lancaster_ne, sarpy_ne

COUNTIES = {c.COUNTY["id"]: c.COUNTY for c in (douglas_ne, sarpy_ne, lancaster_ne, cass_ne)}
DEFAULT_COUNTY = douglas_ne.COUNTY["id"]
REGION_NAME = "Omaha & Lincoln metro (Douglas, Sarpy, Lancaster, Cass counties)"

_TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1"
_bounds: dict = {}
_lock = threading.Lock()


def get_county(county_id: str | None = None) -> dict:
    return COUNTIES[county_id or DEFAULT_COUNTY]


def split_pid(pid: str) -> tuple[str, str]:
    """'sarpy_ne:011589990' -> ('sarpy_ne', '011589990'); bare pin -> default county."""
    pid = (pid or "").strip()
    if ":" in pid:
        cid, pin = pid.split(":", 1)
        if cid in COUNTIES:
            return cid, pin
    return DEFAULT_COUNTY, pid


def make_pid(county_id: str, pin: str) -> str:
    return f"{county_id}:{pin}"


def boundaries() -> dict:
    """County boundary polygons (WGS84 shapely), from Census TIGERweb, cached."""
    from shapely.geometry import shape
    from ..gis import CACHE_DIR, get_json, _esri_to_geojson_geom
    with _lock:
        if _bounds:
            return _bounds
        path = CACHE_DIR / "county_bounds.json"
        raw = json.loads(path.read_text()) if path.exists() else {}
        if any(c["fips"] not in raw for c in COUNTIES.values()):  # a county was added: fetch its outline
            fips = ",".join(f"'{c['fips']}'" for c in COUNTIES.values())
            d = get_json(_TIGER + "/query", {"where": f"GEOID IN ({fips})", "outFields": "GEOID",
                                             "outSR": 4326, "geometryPrecision": 5, "f": "json"}, ttl=365 * 86400)
            raw = {f["attributes"]["GEOID"]: _esri_to_geojson_geom(f["geometry"]) for f in d.get("features", [])}
            path.write_text(json.dumps(raw))
        for c in COUNTIES.values():
            if c["fips"] in raw:
                _bounds[c["id"]] = shape(raw[c["fips"]])
        return _bounds


def register(cfg: dict) -> None:
    """Add (or update) a county at runtime, e.g. one approved in the
    command center's Coverage tab. Measurers and outlines refresh."""
    with _lock:
        COUNTIES[cfg["id"]] = cfg
        _bounds.clear()
    try:
        from ..measure import _measurers
        _measurers.pop(cfg["id"], None)
    except Exception:
        pass


def county_at(lon: float, lat: float) -> str | None:
    from shapely.geometry import Point
    pt = Point(lon, lat)
    for cid, g in boundaries().items():
        if g.contains(pt):
            return cid
    # just outside a line (geocoder points on a road centre): nearest within ~300 m
    best = min(boundaries().items(), key=lambda kv: kv[1].distance(pt), default=None)
    if best and best[1].distance(pt) < 0.003:
        return best[0]
    return None


def counties_in(bbox) -> list[str]:
    from shapely.geometry import box
    b = box(*bbox)
    return [cid for cid, g in boundaries().items() if g.intersects(b)]
