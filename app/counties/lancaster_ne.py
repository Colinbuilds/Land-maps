"""Lancaster County / City of Lincoln, Nebraska public GIS services."""

_LNK = "https://gis.lincoln.ne.gov/public/rest/services"

COUNTY = {
    "id": "lancaster_ne",
    "name": "Lancaster County, NE",
    "fips": "31109",
    "crs": "EPSG:26852",
    "center": [40.81, -96.69],
    "parcels": {
        "url": f"{_LNK}/Assessor/TaxParcels/MapServer/0",
        "fields": {
            "pin": "PARCELID",
            "address": "SITEADDRESS",      # "21200 SW 72ND ST, CRETE, NE, 68368"
            "city": None,
            "zip": None,
            "owner": "OWNERNME1",
            "class": "CLASSCD",            # R1/R2 residential, C1/C2 commercial, A1/A2 ag
            "lot_sqft": "GIS_AREA",
            "bldg_sqft": "RESFLRAREA",
            "bldg_desc": "RESSTRTYP",
            "year_built": "RESYRBLT",
            "home_value": "CNTASSDVAL",     # current assessed value
            "land_value": "LNDVALUE",
            "mail_address": "PSTLADDRESS", "mail_city": "PSTLCITY", "mail_state": "PSTLSTATE", "mail_zip": "PSTLZIP5",
        },
        "address_has_city_zip": True,
        "oid": "OBJECTID",
        "classes": {"R": "CLASSCD LIKE 'R%'", "C": "CLASSCD LIKE 'C%'", "I": "CLASSCD LIKE 'I%'",
                    "A": "CLASSCD LIKE 'A%'", "E": "CLASSCD LIKE 'E%'"},
    },
    # No county building layer is published: FEMA / ORNL USA Structures.
    "buildings": {"source": "FEMA/ORNL USA Structures", "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USA_Structures_View/FeatureServer/0",
                  "oid": "OBJECTID", "height_field": "HEIGHT", "height_units": "m"},
    "pavement": None,
    # No edge-of-pavement layer: street centrelines widened to a typical
    # residential street stand in for it (sidewalk / driveway apron checks).
    "streets": {"url": f"{_LNK}/Planning/Streets/MapServer/0", "half_width_ft": 15.0},
    # Statewide 2025 NAIP with near-infrared: tells grass from paving no
    # matter how a county's colour photo is exposed.
    "infrared": {"url": "https://gis.ne.gov/Image/rest/services/NAIP2025WM/ImageServer", "red": 0, "nir": 3,
                 "source": "USDA NAIP 2025 (Nebraska)"},
    "imagery": {
        "tile_url": "https://gis.lincoln.ne.gov/image/rest/services/Aerials/Aerial2026/MapServer/tile/{z}/{y}/{x}",
        "year": 2026,
        "zoom": 20,
        "max_zoom": 22,
        "source": "Lincoln-Lancaster County 2026 aerial photo",
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
