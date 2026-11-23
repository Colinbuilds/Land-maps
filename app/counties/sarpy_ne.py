"""Sarpy County, Nebraska public GIS services."""

_SARPY = "https://geodata.sarpy.gov/arcgis/rest/services"

COUNTY = {
    "id": "sarpy_ne",
    "name": "Sarpy County, NE",
    "fips": "31153",
    "crs": "EPSG:26852",
    "center": [41.115, -96.05],
    "parcels": {
        "url": f"{_SARPY}/Cadastral/LandRecordsSearch/MapServer/5",
        "fields": {
            "pin": "PARCELID",
            "address": "SITEADDRESS",      # "1620 WILSHIRE DR  BELLEVUE NE 68005"
            "city": None,
            "zip": None,
            "owner": "OWNERNME1",
            "class": "CLASSCD",            # 1000 residential, 2000 commercial ...
            "lot_sqft": ("ACREAGE", 43560.0),
            "bldg_sqft": "RESFLRAREA",
            "bldg_desc": "RESSTRTYP",
            "year_built": "RESYRBLT",
            "mail_address": "PSTLADDRESS", "mail_city": "PSTLCITY", "mail_state": "PSTLSTATE", "mail_zip": "PSTLZIP5",
        },
        "address_has_city_zip": True,
        "oid": "OBJECTID",
        "classes": {"R": "CLASSCD='1000'", "C": "CLASSCD='2000'", "I": "CLASSCD='3000'",
                    "A": "CLASSCD IN ('4000','4500')", "E": "CLASSCD LIKE '9%'"},
    },
    "buildings": {"source": "Sarpy County building footprints", "url": f"{_SARPY}/Cadastral/LandRecordsDynamic/MapServer/42", "oid": "OBJECTID",
                  "height_field": "BLDGHEIGHT", "height_units": "ft"},
    "pavement": None,
    # No edge-of-pavement layer: street centrelines widened to a typical
    # residential street stand in for it (sidewalk / driveway apron checks).
    "streets": {"url": f"{_SARPY}/Cadastral/LandRecordsDynamic/MapServer/3", "half_width_ft": 15.0},
    # Statewide 2025 NAIP with near-infrared: tells grass from paving no
    # matter how a county's colour photo is exposed.
    "infrared": {"url": "https://gis.ne.gov/Image/rest/services/NAIP2025WM/ImageServer", "red": 0, "nir": 3,
                 "source": "USDA NAIP 2025 (Nebraska)"},
    "imagery": {
        "tile_url": "https://tiles.arcgis.com/tiles/OiG7dbwhQEWoy77N/arcgis/rest/services/Aerials2026/MapServer/tile/{z}/{y}/{x}",
        "year": 2026,
        "zoom": 20,
        "max_zoom": 22,
        "source": "Sarpy County 2026 aerial photo",
    },
    "lidar": {
        "type": "ept",
        "url": "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/USGS_LPC_NE_Eastern_UA_2016_LAS_2018/ept.json",
        "year": 2016,
        "source": "USGS 3DEP Eastern Nebraska 2016 LiDAR",
    },
    "sidewalk_width_ft": 4.0,
    "default_driveway_width_ft": 16.0,
}
