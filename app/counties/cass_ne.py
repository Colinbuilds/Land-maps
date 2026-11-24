"""Cass County, Nebraska public GIS services."""

COUNTY = {
    "id": "cass_ne",
    "name": "Cass County, NE",
    "fips": "31025",
    "crs": "EPSG:26852",
    "center": [40.91, -96.14],
    "parcels": {
        "url": "https://services3.arcgis.com/oIhWXTph9P1clNT3/arcgis/rest/services/Tax_Parcels_(Cass_County)/FeatureServer/0",
        "fields": {
            "pin": "PID",
            "address": "FullSitusAddress",  # "16920 6TH AVE PLATTSMOUTH NE 68048"
            "city": "SitusCity",
            "zip": "SitusZipcode",
            "owner": "OwnerName1",
            "class": "ClassCodeUse",        # 01 residential, 05 agricultural ...
            "lot_sqft": ("LegalAcres", 43560.0),
            "bldg_sqft": "FloorArea",
            "bldg_desc": "Style1",
            "year_built": "YearBuilt",
            "home_value": "PropertyValue",
            "land_value": "LandValue",
            "sale_price": "SaleAmount",
            "sale_date": "SaleDate",
            "mail_address": "OwnerAddress1", "mail_city": "OwnerCity", "mail_state": "OwnerState", "mail_zip": "OwnerZip",
        },
        "address_has_city_zip": True,
        "oid": "OBJECTID",
        "classes": {"R": "ClassCodeUse='01'", "C": "ClassCodeUse IN ('02','03')", "I": "ClassCodeUse='04'",
                    "A": "ClassCodeUse='05'", "E": "ClassCodeUse IN ('06','10','11')"},
    },
    "buildings": {"source": "FEMA/ORNL USA Structures", "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USA_Structures_View/FeatureServer/0",
                  "oid": "OBJECTID", "height_field": "HEIGHT", "height_units": "m"},
    "pavement": None,
    # No edge-of-pavement layer: street centrelines widened to a typical
    # residential street stand in for it (sidewalk / driveway apron checks).
    "streets": {"url": "https://services3.arcgis.com/oIhWXTph9P1clNT3/arcgis/rest/services/Street_Centerlines/FeatureServer/71", "half_width_ft": 15.0},
    # No county imagery service is published: Esri World Imagery.
    # Statewide 2025 NAIP with near-infrared: tells grass from paving no
    # matter how a county's colour photo is exposed.
    "infrared": {"url": "https://gis.ne.gov/Image/rest/services/NAIP2025WM/ImageServer", "red": 0, "nir": 3,
                 "source": "USDA NAIP 2025 (Nebraska)"},
    "imagery": {
        "tile_url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "year": None,
        "zoom": 19,
        "max_zoom": 19,
        "source": "Esri World Imagery",
    },
    "lidar": {
        "type": "ept",
        "urls": [
            "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/NE_SouthernNE_B4B_2018/ept.json",
            "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/NE_SouthernNE_B5_2018/ept.json",
        ],
        "year": 2018,
        "source": "USGS 3DEP Southern Nebraska 2018 LiDAR",
    },
    "sidewalk_width_ft": 4.0,
    "default_driveway_width_ft": 16.0,
}
