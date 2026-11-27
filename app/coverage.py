"""Coverage: grow the service area metro by metro, nationwide.

1. Metros: every US metropolitan statistical area with 150,000+ people
   (Census population estimates), centred on its main city.
2. For each metro, the ZIP codes (Census ZCTAs) and counties within 15 miles
   of the centre.
3. Counties are set up from nationwide sources: USGS 3DEP LiDAR (point-cloud
   index), USGS/USDA public-domain aerial photos (NAIP + high-res where
   available), NAIP near-infrared, FEMA/ORNL "USA Structures" building
   footprints and Census roads. What differs county by county is the parcel
   (property records) service, so we search ArcGIS Online for candidates,
   test each one (does it cover the county? does it have parcel id, address,
   owner, value?), rank them, and the owner approves one.
4. An approved county goes live straight away (no deploy), and its metro's
   ZIPs join the measuring queue.

State is in data/coverage.db; approved county configs in data/counties/.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import sqlite3
import threading
import time
import urllib.parse

from .gis import get_bytes, get_json
from .pricing import DATA_DIR

DB = DATA_DIR / "coverage.db"
CFG_DIR = DATA_DIR / "counties"
CFG_DIR.mkdir(exist_ok=True)
_lock = threading.Lock()
MIN_POP = 150_000
RADIUS_MI = 15.0

POP_CSV = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/metro/totals/cbsa-est2024-alldata.csv"
GEOCODER = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
ZCTA = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1"
TIGER_COUNTY = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1"
EPT_INDEX = "https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/resources.geojson"
AGOL_SEARCH = "https://www.arcgis.com/sharing/rest/search"
HUB_SEARCH = "https://hub.arcgis.com/api/v3/search"

NATIONAL = {
    "buildings": {"source": "FEMA/ORNL USA Structures",
                  "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USA_Structures_View/FeatureServer/0",
                  "oid": "OBJECTID", "height_field": "HEIGHT", "height_units": "m"},
    "streets": {"url": "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/8",
                "half_width_ft": 15.0},
    "infrared": {"url": "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer",
                 "red": 0, "nir": 3, "source": "USDA NAIP (national)"},
    # USDA NAIP (~0.6 m, public domain, fine for print), rendered on the tile
    # grid; a county's own sharper photos or Esri can be set when approving.
    "imagery": {"tile_url": "export:https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer",
                "year": None, "zoom": 19, "max_zoom": 19, "source": "USDA NAIP aerial photo (public domain)"},
}
STATE_ABBR = {"AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10", "DC": "11",
              "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
              "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30",
              "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
              "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
              "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56", "PR": "72"}
FIPS_STATE = {v: k for k, v in STATE_ABBR.items()}
STATE_NAME = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
              "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
              "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
              "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
              "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
              "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
              "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
              "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
              "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
              "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
              "PR": "Puerto Rico"}
# States whose public-record names may not be used to sell services
NO_OWNER_NAMES = {"KS"}  # K.S.A. 45-230


# ------------------------------------------------------------------ storage
def _db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _lock, _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS metros (
            cbsa TEXT PRIMARY KEY, name TEXT, pop INTEGER, lat REAL, lon REAL, status TEXT,
            zips TEXT, counties TEXT, note TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS counties (
            fips TEXT PRIMARY KEY, name TEXT, state TEXT, status TEXT, candidates TEXT, config TEXT,
            note TEXT, metros TEXT, updated REAL);
        """)
        have = {r[1] for r in con.execute("PRAGMA table_info(counties)")}
        for col in ("auto", "needs_check"):  # auto-approved counties, and ones to look at
            if col not in have:
                con.execute(f"ALTER TABLE counties ADD COLUMN {col} INTEGER")


def _set(table: str, key: str, keyval: str, **f):
    f["updated"] = time.time()
    with _lock, _db() as con:
        cols = ",".join(f)
        con.execute(f"INSERT INTO {table}({key},{cols}) VALUES(?,{','.join('?' * len(f))}) "
                    f"ON CONFLICT({key}) DO UPDATE SET {', '.join(k + '=excluded.' + k for k in f)}",
                    (keyval, *f.values()))


def _row(r):
    if not r:
        return None
    d = dict(r)
    for k in ("zips", "counties", "candidates", "config", "metros"):
        if k in d and d[k]:
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def metros() -> list[dict]:
    with _db() as con:
        return [_row(r) for r in con.execute("SELECT * FROM metros ORDER BY pop DESC")]


def metro(cbsa: str) -> dict | None:
    with _db() as con:
        return _row(con.execute("SELECT * FROM metros WHERE cbsa=?", (cbsa,)).fetchone())


def counties() -> list[dict]:
    with _db() as con:
        return [_row(r) for r in con.execute("SELECT * FROM counties ORDER BY state, name")]


def county(fips: str) -> dict | None:
    with _db() as con:
        return _row(con.execute("SELECT * FROM counties WHERE fips=?", (fips,)).fetchone())


# ------------------------------------------------------------------- metros
def load_metro_list() -> int:
    """Every metro with 150k+ people (latest Census estimate). Keeps what's
    already been worked on; adds new ones."""
    raw = get_bytes(POP_CSV, ttl=30 * 86400)
    rows = list(csv.DictReader(io.StringIO(raw.decode("latin1"))))
    col = sorted(c for c in rows[0] if c.startswith("POPESTIMATE"))[-1]
    have = {m["cbsa"] for m in metros()}
    n = 0
    for r in rows:
        if r.get("LSAD") != "Metropolitan Statistical Area" or int(r[col] or 0) < MIN_POP:
            continue
        if r["CBSA"] in have:
            with _lock, _db() as con:
                con.execute("UPDATE metros SET pop=? WHERE cbsa=?", (int(r[col]), r["CBSA"]))
            continue
        _set("metros", "cbsa", r["CBSA"], name=r["NAME"], pop=int(r[col]), status="new")
        n += 1
    return n


def _main_city(name: str) -> tuple[str, str]:
    """'Kansas City, MO-KS' -> ('Kansas City', 'MO')."""
    city, _, states = name.partition(",")
    return city.split("-")[0].strip(), states.strip().split("-")[0].strip()


def _query_in_radius(url: str, lat: float, lon: float, fields: str, geom: bool = False) -> list[dict]:
    d = get_json(url + "/query", {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
                                  "spatialRel": "esriSpatialRelIntersects", "distance": RADIUS_MI,
                                  "units": "esriSRUnit_StatuteMile", "outFields": fields,
                                  "returnGeometry": "true" if geom else "false", "outSR": 4326, "f": "json"},
                 ttl=30 * 86400)
    return d.get("features") or []


def map_metro(cbsa: str) -> dict:
    """Centre (main city), and the ZIPs and counties within 15 miles."""
    m = metro(cbsa)
    city, st = _main_city(m["name"])
    g = get_json(GEOCODER, {"SingleLine": f"{city}, {st}", "category": "City", "maxLocations": 1, "f": "json"},
                 ttl=365 * 86400)
    cand = (g.get("candidates") or [None])[0]
    if not cand:
        _set("metros", "cbsa", cbsa, status="error", note=f"couldn't find {city}, {st}")
        return metro(cbsa)
    lon, lat = cand["location"]["x"], cand["location"]["y"]
    zips = sorted({f["attributes"]["ZCTA5"] for f in _query_in_radius(ZCTA, lat, lon, "ZCTA5")})
    cos = [{"fips": f["attributes"]["GEOID"], "name": f["attributes"]["NAME"],
            "state": FIPS_STATE.get(f["attributes"]["STATE"], "")}
           for f in _query_in_radius(TIGER_COUNTY, lat, lon, "GEOID,NAME,STATE")]
    for c in cos:
        old = county(c["fips"])
        ms = sorted(set((old or {}).get("metros") or []) | {cbsa})
        _set("counties", "fips", c["fips"], name=c["name"], state=c["state"],
             status=(old or {}).get("status") or "new", metros=json.dumps(ms))
    _set("metros", "cbsa", cbsa, lat=lat, lon=lon, zips=json.dumps(zips), counties=json.dumps([c["fips"] for c in cos]),
         status="mapped", note=f"{len(zips)} ZIPs, {len(cos)} counties within {RADIUS_MI:g} mi of {city}, {st}")
    return metro(cbsa)


# --------------------------------------------------------- county set-up
def _county_geom(fips: str):
    from shapely.geometry import shape
    from .gis import _esri_to_geojson_geom
    d = get_json(TIGER_COUNTY + "/query", {"where": f"GEOID='{fips}'", "outFields": "GEOID,NAME,CENTLAT,CENTLON",
                                           "outSR": 4326, "geometryPrecision": 5, "f": "json"}, ttl=365 * 86400)
    f = (d.get("features") or [None])[0]
    return shape(_esri_to_geojson_geom(f["geometry"])) if f else None


_EPT: dict = {}


def _ept_index() -> list:
    """The USGS point-cloud index, parsed once, with each project's box."""
    if "rows" not in _EPT or time.time() - _EPT.get("t", 0) > 7 * 86400:
        idx = json.loads(get_bytes(EPT_INDEX, ttl=30 * 86400))
        rows = []
        for f in idx.get("features") or []:
            xs, ys = [], []

            def walk(c):
                if c and isinstance(c[0], (int, float)):
                    xs.append(c[0]); ys.append(c[1])
                else:
                    for cc in c or []:
                        walk(cc)
            walk((f.get("geometry") or {}).get("coordinates"))
            if xs:
                rows.append(((min(xs), min(ys), max(xs), max(ys)), f))
        _EPT.update(rows=rows, t=time.time())
    return _EPT["rows"]


def _lidar_for(geom) -> dict | None:
    """The newest USGS 3DEP point clouds covering the county."""
    from shapely.geometry import shape
    hits = []
    gx0, gy0, gx1, gy1 = geom.bounds
    for (x0, y0, x1, y1), f in _ept_index():
        if x1 < gx0 or x0 > gx1 or y1 < gy0 or y0 > gy1:  # cheap box test first
            continue
        try:
            g = shape(f["geometry"])
        except Exception:
            continue
        if not g.intersects(geom):
            continue
        share = g.intersection(geom).area / max(geom.area, 1e-12)
        name = f["properties"].get("name", "")
        yr = max((int(y) for y in re.findall(r"(20\d\d)", name)), default=0)
        hits.append((yr, share, name, f["properties"].get("url")))
    if not hits:
        return None
    hits.sort(reverse=True)
    best, covered, urls = [], 0.0, []
    for yr, share, name, url in hits:  # newest first until the county is covered
        if url and covered < 0.98:
            urls.append(url)
            best.append((yr, name))
            covered += share
    return {"type": "ept", "urls": urls[:4], "url": urls[0], "year": best[0][0] or None,
            "source": "USGS 3DEP LiDAR: " + ", ".join(n for _, n in best[:4])}


def base_config(fips: str) -> dict:
    """Everything for a county except its parcels (nationwide sources)."""
    c = county(fips)
    g = _county_geom(fips)
    lat, lon = g.centroid.y, g.centroid.x
    st = c["state"]
    cid = re.sub(r"[^a-z0-9]+", "_", c["name"].lower().replace(" county", "").replace(" parish", "")).strip("_") + "_" + st.lower()
    return {
        "id": cid, "name": f"{c['name']}, {st}", "fips": fips,
        # local transverse Mercator in US survey feet, centred on the county
        "crs": f"+proj=tmerc +lat_0={lat:.5f} +lon_0={lon:.5f} +k=1 +x_0=0 +y_0=0 +datum=NAD83 +units=us-ft +no_defs",
        "center": [round(lat, 5), round(lon, 5)],
        "buildings": NATIONAL["buildings"], "pavement": None, "streets": NATIONAL["streets"],
        "infrared": NATIONAL["infrared"], "imagery": dict(NATIONAL["imagery"]),
        "lidar": _lidar_for(g),
        "sidewalk_width_ft": 4.0, "default_driveway_width_ft": 16.0,
        "owner_names": st not in NO_OWNER_NAMES,
        "auto": True,
    }


# --------------------------------------------------------- parcel discovery
FIELD_PATTERNS = {
    "pin": [r"^PARCEL_?ID$", r"^PROP_?ID$", r"^GEO_?ID$", r"^ACCOUNT_?(NO|NUM|NUMBER)?$", r"^PARCELID$", r"^PIN$", r"^APN$", r"^PARCEL_?(NO|NUM|NUMBER)$", r"^GPIN$", r"^PID$",
            r"^PARCEL$", r"^PARID$", r"^PARCELNUMB$", r"^MAPNO$", r"^KEYPIN$", r"^QUICKREFID$"],
    "address": [r"^SITUS_?CONCAT(_?SHORT)?$", r"^SITUS_?FULL(_?ADDR(ESS)?)?$", r"^SITE_?ADDR(ESS)?$", r"^SITUS(_?ADDR(ESS)?)?$", r"^PROP(ERTY)?_?ADDR(ESS)?$", r"^PHY(SICAL)?_?ADDR(ESS)?$",
                r"^SITEADDRESS$", r"^FULL_?ADDR(ESS)?$", r"^ADDRESS$", r"^LOC(ATION)?_?ADDR(ESS)?$", r"^STREET_?ADDR(ESS)?$",
                r"^SITUSADDR$", r"^ADDR$", r"^SITE_?ADD$", r"^PROPADDR$", r"^SITUS_ADDRESS$"],
    "city": [r"^SITE_?CITY$", r"^SITUS_?CITY$", r"^PROP(ERTY)?_?CITY$", r"^CITY$", r"^MUNICIPALITY$", r"^PHY_?CITY$"],
    "zip": [r"^SITE_?ZIP(5|CODE)?$", r"^SITUS_?ZIP(5|CODE)?$", r"^PROP(ERTY)?_?ZIP$", r"^ZIP(_?CODE)?5?$", r"^PHY_?ZIP$"],
    "owner": [r"^OWNER_?NAME1?$", r"^OWNERNME1$", r"^OWNER1?$", r"^OWN_?NAME$", r"^OWNER_?1$", r"^OWNERNAME$",
              r"^TAXPAYER(_?NAME)?$", r"^DEED_?HOLDER$", r"^OWN1$", r"^OWNER_FULL$"],
    "home_value": [r"^TOT(AL)?_?(ASSESSED|APPR(AISED)?|MKT|MARKET)?_?VAL(UE)?$", r"^CNTASSDVAL$", r"^ASSESSED_?VAL(UE)?$",
                   r"^APPRAISED_?VAL(UE)?$", r"^MARKET_?VAL(UE)?$", r"^TOTVAL$", r"^TOTAL_?VALUE$", r"^JUST_?VAL(UE)?$",
                   r"^FAIR_?MARKET_?VAL(UE)?$", r"^TOTAPPR$", r"^PropertyValue$", r"^TOTALVAL$", r"^APPR_?TOT(AL)?$"],
    "land_value": [r"^LAND_?VAL(UE)?$", r"^LNDVALUE$", r"^LANDVAL$", r"^LAND_?APPR$"],
    "year_built": [r"^YEAR_?BUILT$", r"^YR_?BUILT$", r"^YRBLT$", r"^RESYRBLT$", r"^YEARBLT$", r"^BUILT$", r"^YEARBUILT$"],
    "bldg_sqft": [r"^(BLDG|BUILDING|LIVING|HEATED|FIN(ISHED)?)_?(SQ_?FT|AREA|SF)$", r"^RESFLRAREA$", r"^SQ_?FT$",
                  r"^LIVING_?AREA$", r"^TOT_?SQFT$", r"^GLA$", r"^IMPRV_?MAIN_?AREA$"],
    "lot_acres": [r"^(GIS_?|CALC_?|DEED_?)?ACRE(S|AGE)?$", r"^ACRES$", r"^ACREAGE$", r"^LAND_?SIZE_?ACRES$"],
    "lot_sqft": [r"^LOT_?(SIZE|SQ_?FT|AREA)$", r"^LAND_?SQ_?FT$", r"^SQFT_?LOT$", r"^LAND_?SIZE_?SQ_?FT$"],
    "class": [r"^(PROP(ERTY)?_?)?CLASS(_?CODE|CD)?$", r"^CLASSCD$", r"^LAND_?USE(_?CODE)?$", r"^LUC$", r"^USE_?CODE$",
              r"^PROP_?TYPE$", r"^PROPERTY_?TYPE$", r"^ZONING$", r"^DOR_?UC$", r"^USECD$"],
    "mail_address": [r"^MAIL(ING)?_?ADDR(ESS)?1?$", r"^PSTLADDRESS$", r"^OWN(ER)?_?ADDR(ESS)?1?$"],
    "mail_city": [r"^MAIL(ING)?_?CITY$", r"^PSTLCITY$", r"^OWN(ER)?_?CITY$"],
    "mail_state": [r"^MAIL(ING)?_?STATE$", r"^PSTLSTATE$", r"^OWN(ER)?_?STATE?$"],
    "mail_zip": [r"^MAIL(ING)?_?ZIP(5|CODE)?$", r"^PSTLZIP5?$", r"^OWN(ER)?_?ZIP$"],
}


# Looser second pass (field names cut to 10 characters by shapefiles, odd
# naming): (must contain, must not contain, preferred words)
FALLBACKS = {
    "address": (r"ADDR|ADRS|SITUS", r"MAIL|OWN|PSTL|TAXP|BILL|CITY|ZIP|STATE|NUM|NBR|NO$|UNIT|APT|SUITE|PREFIX|SUFFIX|DIR|TYPE",
                r"CONCAT|FULL|SITE|SITUS|PROP|PHY|LOC"),
    "owner": (r"OWN|TAXPAYER|GRANTEE", r"ADDR|ADR|CITY|ZIP|STATE|ST$|TYPE|CODE|PCT|SHIP|DATE|DATA|SOURCE", r"NAME|NM|1"),
    "home_value": (r"VAL|APPR|ASSESS|MKT", r"LAND|LND|BLDG|IMP|PER|ACRE|YEAR|DATE|PRIOR|PREV|_1$|EXEMPT", r"TOT|ASSESS|MARKET|APPR|FULL"),
    "year_built": (r"YR|YEAR", r"SALE|DATE|ASSESS|TAX", r"BLT|BUILT|BUILD"),
    "pin": (r"PARC|PIN|APN|PID", r"OLD|PREV|ALT|ADDR|PARENT|MAP|SPLIT|GROUP", r"ID|NO|NUM|PIN"),
    "city": (r"CITY|MUNI|TOWN", r"MAIL|OWN|PSTL|BILL", r"SITE|SITUS|PROP|PHY"),
    "zip": (r"ZIP", r"MAIL|OWN|PSTL|BILL|4$", r"SITE|SITUS|PROP|PHY"),
}


def guess_fields(fields: list[dict]) -> dict:
    names = [f["name"] for f in fields if f.get("type") not in ("esriFieldTypeGeometry", "esriFieldTypeOID")]
    out = {}
    for key, pats in FIELD_PATTERNS.items():
        for p in pats:  # first pattern wins: they're in order of preference
            hit = next((n for n in names if re.match(p, n, re.I)), None)
            if hit:
                out[key] = hit
                break
    for key, (must, never, prefer) in FALLBACKS.items():
        if key in out:
            continue
        cands = [n for n in names if re.search(must, n, re.I) and not re.search(never, n, re.I) and n not in out.values()]
        if cands:
            cands.sort(key=lambda n: (0 if re.search(prefer, n, re.I) else 1, len(n)))
            out[key] = cands[0]
    return out


def _layer_candidates(service_url: str) -> list[tuple[str, dict]]:
    """Polygon layers of a Feature/Map service that look like parcels."""
    meta = get_json(service_url, {"f": "json"}, ttl=7 * 86400)
    layers = meta.get("layers") or []
    if not layers and meta.get("geometryType"):  # the URL is already a layer
        return [(service_url, meta)]
    out = []
    for lyr in layers:
        if lyr.get("subLayerIds"):
            continue
        nm = (lyr.get("name") or "").lower()
        if re.search(r"parcel|tax|cadastr|property|ownership|lot", nm) and not re.search(r"label|anno|dimension|line|point|buffer", nm):
            url = f"{service_url}/{lyr['id']}"
            out.append((url, get_json(url, {"f": "json"}, ttl=7 * 86400)))
    return out


def _sample_points(geom, n: int = 5) -> list[tuple[float, float]]:
    """Points spread over the county (centre + 4 around it)."""
    c = geom.representative_point()
    minx, miny, maxx, maxy = geom.bounds
    pts = [(c.x, c.y)]
    for fx, fy in ((0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7)):
        x, y = minx + (maxx - minx) * fx, miny + (maxy - miny) * fy
        from shapely.geometry import Point
        if geom.contains(Point(x, y)):
            pts.append((x, y))
    return pts[:n]


def test_layer(url: str, meta: dict, geom) -> dict | None:
    """Score a candidate parcel layer: coverage across the county, fields, samples."""
    if (meta.get("geometryType") or "") != "esriGeometryPolygon":
        return None
    fm = guess_fields(meta.get("fields") or [])
    if "pin" not in fm or "address" not in fm:
        return None
    hits, samples = 0, []
    for x, y in _sample_points(geom):
        d = 0.004
        q = get_json(url + "/query", {"geometry": f"{x - d},{y - d},{x + d},{y + d}", "geometryType": "esriGeometryEnvelope",
                                      "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
                                      "returnGeometry": "false", "resultRecordCount": 5, "f": "json"}, ttl=86400)
        feats = q.get("features") or []
        if feats:
            hits += 1
            samples += [f["attributes"] for f in feats[:2]]
    if not hits:
        return None
    filled = lambda k: sum(1 for s in samples if str(s.get(fm.get(k, ""), "") or "").strip())
    score = (hits * 20 + sum(8 for k in ("owner", "home_value", "year_built", "bldg_sqft", "class", "city", "zip") if k in fm)
             + (10 if filled("address") >= max(1, len(samples) // 2) else 0) + (5 if filled("owner") else 0))
    return {"url": url, "name": meta.get("name"), "fields": fm, "coverage": hits, "points": len(_sample_points(geom)),
            "score": score, "oid": next((f["name"] for f in meta.get("fields") or [] if f.get("type") == "esriFieldTypeOID"), "OBJECTID"),
            "samples": [{k: s.get(v) for k, v in fm.items() if k in ("pin", "address", "owner", "home_value", "class", "year_built")}
                        for s in samples[:4]]}


def discover(fips: str, max_items: int = 25) -> list[dict]:
    """Search for the county's parcel service and rank what works."""
    c = county(fips)
    geom = _county_geom(fips)
    st_name = STATE_NAME.get(c["state"], c["state"])
    queries = [f'parcels "{c["name"]}" {st_name}', f'{c["name"]} {c["state"]} parcels', f"{st_name} statewide parcels"]
    seen, found = set(), []
    minx, miny, maxx, maxy = geom.bounds
    items = []
    for qtext in queries:  # ArcGIS Online, limited to the county's area
        d = get_json(AGOL_SEARCH, {"q": qtext + ' (type:"Feature Service" OR type:"Map Service")', "f": "json",
                                   "num": max_items, "bbox": f"{minx},{miny},{maxx},{maxy}"}, ttl=7 * 86400)
        items += d.get("results") or []
    for qtext in (f'parcels {c["name"]} {st_name}', f'{c["name"]} {st_name} tax parcels', f"{st_name} parcels"):
        try:  # ArcGIS Hub (open-data sites) finds more county layers
            d = get_json(HUB_SEARCH, {"q": qtext}, ttl=7 * 86400)
            for r in d.get("data") or []:
                a = r.get("attributes") or {}
                if a.get("url") and a.get("type") in ("Feature Layer", "Feature Service", "Map Service", "Table"):
                    items.append({"url": a["url"], "title": a.get("name"), "owner": a.get("owner") or a.get("source")})
        except Exception:
            pass
    for it in items:
            u = (it.get("url") or "").rstrip("/")
            if not u or u in seen:
                continue
            seen.add(u)
            try:
                for lurl, meta in _layer_candidates(u):
                    r = test_layer(lurl, meta, geom)
                    if r:
                        r["item"] = it.get("title")
                        r["owner_org"] = it.get("owner")
                        gov = re.search(r"county|gov|gis|state|assessor|co_|city", (it.get("owner") or "") + " " + u, re.I)
                        r["score"] += 10 if gov else 0
                        found.append(r)
            except Exception:
                continue
    found.sort(key=lambda r: -r["score"])
    _set("counties", "fips", fips, candidates=json.dumps(found[:8]),
         status="found" if found else "not_found",
         note=f"{len(found)} candidate parcel layer(s)" if found else "No public parcel layer found automatically; paste one in.")
    return found[:8]


def class_values(url: str, field: str) -> list[dict]:
    """The most common values of a land-use / class field (to pick residential)."""
    d = get_json(url + "/query", {"where": "1=1", "groupByFieldsForStatistics": field,
                                  "outStatistics": json.dumps([{"statisticType": "count", "onStatisticField": field,
                                                                "outStatisticFieldName": "n"}]),
                                  "orderByFields": "n DESC", "f": "json"}, ttl=86400)
    return [{"value": f["attributes"].get(field), "n": f["attributes"].get("n")} for f in (d.get("features") or [])[:40]]


def approve(fips: str, url: str, fields: dict, residential_where: str | None = None, imagery_url: str | None = None,
            oid: str = "OBJECTID", samples: list | None = None) -> dict:
    """Make the county live with this parcel layer (no deploy needed)."""
    cfg = base_config(fips)
    fm = {k: v for k, v in fields.items() if v}
    if fm.get("lot_acres") and not fm.get("lot_sqft"):
        fm["lot_sqft"] = (fm.pop("lot_acres"), 43560.0)
    fm.pop("lot_acres", None)
    for k in ("city", "zip", "owner", "class", "home_value", "year_built", "bldg_sqft", "land_value"):
        fm.setdefault(k, None)
    cfg["parcels"] = {"url": url, "fields": fm, "oid": oid,
                      "address_has_city_zip": not fm.get("city"),
                      "row_parcels": True,  # skip street right-of-way strips mapped as parcels
                      # addresses that carry ", CITY, ST ZIP" while city and ZIP have their own fields
                      "address_trim_comma": bool(fm.get("city")) and any("," in str(sm.get("address") or "") for sm in samples or []),
                      "classes": {"R": residential_where or "1=1"}}
    if imagery_url == "esri":  # sharp, nationwide; commercial use needs an Esri account
        cfg["imagery"] = {**cfg["imagery"], "tile_url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                          "source": "Esri World Imagery", "zoom": 19, "max_zoom": 19, "print_ok": False}
    elif imagery_url:
        cfg["imagery"] = {**cfg["imagery"], "tile_url": imagery_url, "source": "County aerial photo", "zoom": 20, "max_zoom": 21,
                          "print_ok": False}
    (CFG_DIR / f"{cfg['id']}.json").write_text(json.dumps(cfg, indent=1, default=list))
    from . import counties as _co
    _co.register(cfg)
    _set("counties", "fips", fips, status="live", config=json.dumps(cfg, default=list), note="Live", auto=0, needs_check=0)
    try:
        auto_pull()  # a metro may be complete now
    except Exception:
        pass
    return cfg


# ------------------------------------------------------------ auto-approve
_RES_TEXT = re.compile(r"RESID|SINGLE|SFR|S\.F\.|DUPLEX|TOWN ?HO|CONDO|DWELL|MOBILE HOME|MANUFACTURED|PLANNED UNIT|"
                       r"\bSTORY\b|RANCH|SPLIT LEVEL|BI ?-?LEVEL|TRI ?-?LEVEL|BUNGALOW|COTTAGE", re.I)
_NOT_RES = re.compile(r"VACANT|COMMERC|NON[- ]?RES|EXEMPT|BPP|OFFICE|INDUSTR|RETAIL|WAREHOUSE|APARTMENTS? ABOVE|"
                      r"BANK|STORE|SHOP|CHURCH|SCHOOL|HOTEL|MOTEL|RESTAURANT|MEDICAL|CLINIC|HOSPITAL|AUTO|SERVICE|"
                      r"GARAGE, ?(SERVICE|REPAIR|PARKING)|DAY ?CARE|NURSING|ASSISTED|FRATERNAL|THEATER|BAR\b|TAVERN", re.I)
_RES_CODES = {"R", "RES", "RS", "R1", "R-1", "R2", "R-2", "SF", "SFR", "RESIDENTIAL",
              "A1", "A2", "A3", "A",  # Texas appraisal: A = single family (A1 house, A2 mobile, A3 condo)
              "0100", "0101", "0102", "0103", "0104", "0105", "0106", "01",  # Florida DOR: 01xx single family
              "1112", "1212", "1215"}  # Colorado residential abstract codes
_NOT_A = {"IA", "NE", "KS", "MO", "SD", "ND", "MN", "WI", "IL", "IN", "OH"}  # states where "A" means agricultural


def _res_values(vals: list[dict], state: str) -> tuple[list, int]:
    """Which of a field's values mean a home, and how many parcels they cover."""
    keep, n = [], 0
    for v in vals:
        raw = v.get("value")
        if raw in (None, ""):
            continue
        t = str(raw).strip().upper()
        tokens = [x.strip() for x in re.split(r"[,;/]", t) if x.strip()]
        is_res = (_RES_TEXT.search(t) and not _NOT_RES.search(t)) or \
            (t in _RES_CODES and not (t == "A" and state in _NOT_A)) or \
            (len(tokens) > 1 and all(x in _RES_CODES for x in tokens))
        if is_res:
            keep.append(raw)
            n += int(v.get("n") or 1)
    return keep, n


def guess_residential(url: str, field: str | None, state: str, other_fields: list[str] | None = None) -> str | None:
    """A homes-only filter from a layer's own values, when they say so
    plainly: the class field first, then any other land-use / property-type
    field, picking the one that marks the most parcels as homes. None when
    nothing is readable (the county goes live with every parcel, flagged)."""
    best = (0, None, [])
    for f in [x for x in [field] + list(other_fields or []) if x][:7]:
        try:
            keep, n = _res_values(class_values(url, f), state)
        except Exception:
            continue
        if keep and n > best[0]:
            best = (n, f, keep)
    if not best[1]:
        return None
    quote = lambda x: str(x) if isinstance(x, (int, float)) else "'" + str(x).replace("'", "''") + "'"
    return f"{best[1]} IN ({','.join(quote(k) for k in best[2])})"


def _type_fields(url: str) -> list[str]:
    """Text fields that may say what a parcel is (land use, class, style)."""
    meta = get_json(url, {"f": "json"}, ttl=7 * 86400)
    out = []
    for f in meta.get("fields") or []:
        n = f.get("name", "")
        if f.get("type") == "esriFieldTypeString" and re.search(r"USE|CLASS|TYPE|LUC|DESC|DSC|ZON|DOR|CAT|STYLE|STRUCT", n, re.I) \
                and not re.search(r"USER|LEGAL|EDIT|CREAT|ADDR|OWNER|NAME", n, re.I):
            out.append(n)
    return out


def auto_settings() -> dict:
    from .pricing import load_settings
    return {"auto_approve": True, **(load_settings().get("coverage") or {})}


def live_query_ok(url: str, geom) -> bool:
    """Run the same kind of map search the measuring uses (fresh, not cached).
    Some layers answer the sample test but refuse real searches."""
    from . import gis
    x, y = _sample_points(geom)[0]
    d = 0.002
    try:
        gis.query_features(url, envelope=(x - d, y - d, x + d, y + d), out_fields="*", limit=5)
        return True
    except Exception:
        return False


def auto_approve(fips: str) -> dict | None:
    """Approve the county's best parcel layer when it clearly works: parcels
    at most test spots and real addresses in the samples. Sets the homes-only
    filter when the class values are readable; flags the county for a check."""
    c = county(fips)
    cands = c.get("candidates") if c and isinstance(c.get("candidates"), list) else []
    for k in cands:
        samples = k.get("samples") or []
        with_addr = sum(1 for sm in samples if str(sm.get("address") or "").strip())
        if not k.get("points") or k["coverage"] / k["points"] < 0.6 or with_addr < max(1, len(samples) // 2):
            continue
        if not live_query_ok(k["url"], _county_geom(fips)):
            _set("counties", "fips", fips, note=f"Auto-approve skipped {k.get('name') or 'a layer'}: its map searches fail right now")
            continue
        try:
            others = _type_fields(k["url"])
        except Exception:
            others = []
        where = guess_residential(k["url"], (k.get("fields") or {}).get("class"), c["state"], others)
        approve(fips, k["url"], k["fields"], where, None, k.get("oid") or "OBJECTID", samples)
        check = [] if where else ["homes-only filter (every parcel is included)"]
        if not (k.get("fields") or {}).get("home_value"):
            check.append("assessed value field")
        if not (k.get("fields") or {}).get("owner"):
            check.append("owner field")
        note = "Auto-approved: " + (k.get("item") or k.get("name") or "layer") + (
            " · check " + ", ".join(check) if check else "")
        _set("counties", "fips", fips, note=note, auto=1, needs_check=1 if check else 0)
        return {"fips": fips, "url": k["url"], "where": where, "check": check}
    return None


def approve_all_found() -> int:
    n = 0
    for c in counties():
        if c["status"] == "found":
            try:
                if auto_approve(c["fips"]):
                    n += 1
            except Exception as e:
                _set("counties", "fips", c["fips"], note=f"Auto-approve failed: {str(e)[:150]}")
    return n


def metro_ready_zips(cbsa: str) -> list[str]:
    """ZIPs of a metro whose counties are all live."""
    m = metro(cbsa)
    if not m or not m.get("zips"):
        return []
    live = {c["fips"] for c in counties() if c["status"] == "live"}
    from . import counties as _co
    live |= {c["fips"] for c in _co.COUNTIES.values()}
    return list(m["zips"]) if set(m.get("counties") or []) <= live else []


def summary() -> dict:
    ms, cs = metros(), counties()
    from . import counties as _co
    builtin = {c["fips"] for c in _co.COUNTIES.values() if not c.get("auto")}
    for c in cs:
        if c["fips"] in builtin:
            c["status"] = "live"
    by = {}
    for c in cs:
        by[c["status"]] = by.get(c["status"], 0) + 1
    return {"metros": len(ms), "mapped": sum(1 for m in ms if m["status"] in ("mapped", "live")),
            "counties": len(cs), "by_status": by,
            "zips": sum(len(m.get("zips") or []) for m in ms if isinstance(m.get("zips"), list))}


# ------------------------------------------------------ background worker
_worker = {"thread": None, "stop": False, "now": None}


def run_all(limit: int | None = None):
    """Map every metro and search every county, a little at a time, in the
    background (skips what's already done). Safe to start again any time."""
    if _worker["thread"] and _worker["thread"].is_alive():
        return status()
    _worker["stop"] = False

    def go():
        try:
            if not metros():
                load_metro_list()
            todo = [m for m in metros() if m["status"] in ("new", "error")][:limit]
            for m in todo:
                if _worker["stop"]:
                    return
                _worker["now"] = f"Mapping {m['name']}"
                try:
                    map_metro(m["cbsa"])
                except Exception as e:
                    _set("metros", "cbsa", m["cbsa"], status="error", note=str(e)[:200])
            from . import counties as _co
            builtin = {c["fips"] for c in _co.COUNTIES.values()}
            for c in counties():
                if _worker["stop"]:
                    return
                if c["status"] != "new" or c["fips"] in builtin:
                    continue
                _worker["now"] = f"Searching {c['name']}, {c['state']}"
                try:
                    discover(c["fips"])
                    if auto_settings().get("auto_approve"):
                        auto_approve(c["fips"])
                except Exception as e:
                    _set("counties", "fips", c["fips"], status="error", note=str(e)[:200])
        finally:
            _worker["now"] = None
    _worker["thread"] = threading.Thread(target=go, daemon=True, name="coverage")
    _worker["thread"].start()
    return status()


def stop():
    _worker["stop"] = True


def status() -> dict:
    return {"running": bool(_worker["thread"] and _worker["thread"].is_alive()), "now": _worker["now"]}


def load_saved_configs() -> int:
    """At start-up: register every county approved earlier."""
    from . import counties as _co
    n = 0
    for p in sorted(CFG_DIR.glob("*.json")):
        try:
            cfg = json.loads(p.read_text())
            fm = cfg["parcels"]["fields"]
            for k, v in list(fm.items()):  # JSON turns (field, multiplier) into a list
                if isinstance(v, list):
                    fm[k] = tuple(v)
            _co.register(cfg)
            n += 1
        except Exception:
            continue
    return n


init()


# ------------------------------------------------- continuous (daily) loop
def auto_pull() -> list[str]:
    """Queue every metro whose 15-mile counties are all live (once)."""
    from . import datastore
    pulled = []
    for m in metros():
        if m["status"] == "live":
            continue
        zs = metro_ready_zips(m["cbsa"])
        if zs:
            datastore.prebuild_all(["R"], zips=zs)
            _set("metros", "cbsa", m["cbsa"], status="live", note=f"Measuring {len(zs)} ZIPs")
            pulled.append(m["name"])
    return pulled


_loop = {"started": False}


def start_continuous():
    """Every day: refresh the metro list (new metros cross 150k), map new
    metros, search new counties, and queue metros that became ready."""
    if _loop["started"]:
        return
    _loop["started"] = True

    def go():
        time.sleep(60)  # let the site finish starting
        while True:
            try:
                if not (_worker["thread"] and _worker["thread"].is_alive()):
                    try:
                        load_metro_list()
                    except Exception:
                        pass
                    run_all()
                    while _worker["thread"] and _worker["thread"].is_alive():
                        time.sleep(30)
                    if auto_settings().get("auto_approve"):
                        approve_all_found()
                    auto_pull()
            except Exception:
                pass
            time.sleep(24 * 3600)
    threading.Thread(target=go, daemon=True, name="coverage-daily").start()
