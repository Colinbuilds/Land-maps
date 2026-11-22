"""County GIS adapters.

Each county module exposes a ``COUNTY`` dict describing where its public
ArcGIS REST layers live and how their fields map onto what the estimator
needs. Add a county by copying ``douglas_ne.py`` and pointing it at that
county's services.
"""
from . import douglas_ne

COUNTIES = {douglas_ne.COUNTY["id"]: douglas_ne.COUNTY}
DEFAULT_COUNTY = douglas_ne.COUNTY["id"]


def get_county(county_id: str | None = None) -> dict:
    return COUNTIES[county_id or DEFAULT_COUNTY]
