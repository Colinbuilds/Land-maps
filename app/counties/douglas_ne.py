"""Douglas County, Nebraska (DOGIS) public GIS services."""

_DOGIS = "https://gis.dogis.org/arcgis/rest/services"
_DCGIS = "https://dcgis.org/server/rest/services"

COUNTY = {
    "id": "douglas_ne",
    "name": "Douglas County, NE",
    "fips": "31055",
    # Local projected CRS used for all area/length maths (NAD83 / Nebraska, US feet).
    "crs": "EPSG:26852",
    "center": [41.2565, -96.0],
    # Address search + autocomplete (county geocoder).
    "geocoder": f"{_DCGIS}/Geocoders/PointAddress_and_Street_Roles/GeocodeServer",
    # Tax parcels with assessor attributes.
    "parcels": {
        "url": f"{_DOGIS}/Accela_Dynamic/MapServer/7",
        "fields": {
            "pin": "PIN",
            "address": "PROPERTY_A",
            "city": "PROP_CITY",
            "zip": "PROP_ZIP",
            "owner": "OWNER_NAME",
            "class": "CLASS",          # R=residential, C=commercial, ...
            "lot_sqft": "SQ_FEET",
            "bldg_sqft": "BLDG_SF",
            "bldg_desc": "BLDG_DESC",
            "year_built": "BLDG_YRBLT",
        },
        "max_records": 2000,
        "oid": "OBJECTID",
        # property type -> where clause (bulk runs)
        "classes": {"R": "CLASS='R'", "C": "CLASS='C'", "I": "CLASS='I'", "A": "CLASS='A'", "E": "CLASS='E'"},
    },
    # 2022 building footprints (planimetric, roof outlines).
    "buildings": {"source": "Douglas County 2022 building footprints", "url": f"{_DCGIS}/Hosted/2022_Building_Footprints/FeatureServer/0", "oid": "objectid",
                  "height_field": "eaveheight", "height_units": "ft"},
    # 2022 LiDAR edge-of-pavement (streets / public pavement only).
    "pavement": {"url": f"{_DCGIS}/Hosted/Pavement_2022/FeatureServer/0"},
    # Street segments the city has flagged as having no sidewalk.
    "no_sidewalks": {"url": f"{_DCGIS}/Hosted/No_Sidewalks_Public_View/FeatureServer/0"},
    # Leaf-off orthoimagery, Web-Mercator tile cache (z up to 23).
    # Statewide 2025 NAIP with near-infrared: tells grass from paving no
    # matter how a county's colour photo is exposed.
    "infrared": {"url": "https://gis.ne.gov/Image/rest/services/NAIP2025WM/ImageServer", "red": 0, "nir": 3,
                 "source": "USDA NAIP 2025 (Nebraska)"},
    "imagery": {
        "tile_url": f"{_DCGIS}/2025_Douglas_County_NE_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}",
        "year": 2025,
        "source": "Douglas County 2025 orthoimagery",
        "zoom": 20,
        "max_zoom": 22,
    },
    # 2022 QL1 classified LiDAR point cloud (public S3), used to find decks,
    # sheds, play sets, trampolines and shrub beds so they're kept out of the lawn.
    "lidar": {
        "type": "tiles",
        "year": 2022,
        "source": "Douglas County 2022 QL1 LiDAR",
        "index_url": f"{_DCGIS}/Hosted/Douglas_County_NE_LiDAR_Tiles_view/FeatureServer/4",
        "las_field": "las_tile",
        "las_base_url": "https://dcgis-lidar.s3.amazonaws.com/2022/Point_Cloud/Classified/",
        # The servers shift NAD83(2011)->WGS84 when serving lat/lon; measure it
        # against one known feature so everything lines up with the LiDAR.
        "calibration": {
            "url": f"{_DCGIS}/Hosted/2022_Building_Footprints/FeatureServer/0",
            "where": "objectid=1",
            "fallback_ft": (2.34, -2.55),
        },
        "crs_wkt": ('PROJCS["Transverse_Mercator",GEOGCS["GCS_NAD_1983_2011",DATUM["D_NAD83_NATIONAL_SPATIAL_REFERENCE_SYSTEM_2011",SPHEROID["GRS_1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["Degree",0.017453292519943295]],PROJECTION["Transverse_Mercator"],PARAMETER["scale_factor",1.0000482],PARAMETER["central_meridian",-96.05],PARAMETER["latitude_of_origin",41.1833333299992],PARAMETER["false_easting",131233.5958005249],PARAMETER["false_northing",82020.99737532808],UNIT["Foot (International)",0.3048]]'),
    },
    # Typical public sidewalk width in this jurisdiction (feet).
    "sidewalk_width_ft": 4.0,
    # Used only when a driveway is hidden by shadow/trees in the imagery.
    "default_driveway_width_ft": 16.0,
}
