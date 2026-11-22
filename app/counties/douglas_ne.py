"""Douglas County, Nebraska (DOGIS) public GIS services."""

_DOGIS = "https://gis.dogis.org/arcgis/rest/services"
_DCGIS = "https://dcgis.org/server/rest/services"

COUNTY = {
    "id": "douglas_ne",
    "name": "Douglas County, NE",
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
    },
    # 2022 building footprints (planimetric, roof outlines).
    "buildings": {"url": f"{_DCGIS}/Hosted/2022_Building_Footprints/FeatureServer/0"},
    # 2022 LiDAR edge-of-pavement (streets / public pavement only).
    "pavement": {"url": f"{_DCGIS}/Hosted/Pavement_2022/FeatureServer/0"},
    # Street segments the city has flagged as having no sidewalk.
    "no_sidewalks": {"url": f"{_DCGIS}/Hosted/No_Sidewalks_Public_View/FeatureServer/0"},
    # Leaf-off orthoimagery, Web-Mercator tile cache (z up to 23).
    "imagery": {
        "tile_url": f"{_DCGIS}/2025_Douglas_County_NE_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}",
        "year": 2025,
        "zoom": 20,
        "max_zoom": 22,
    },
    # Typical public sidewalk width in this jurisdiction (feet).
    "sidewalk_width_ft": 4.0,
    # Used only when a driveway is hidden by shadow/trees in the imagery.
    "default_driveway_width_ft": 16.0,
}
