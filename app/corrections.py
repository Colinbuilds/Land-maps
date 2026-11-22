"""Hand corrections to a property's lawn, saved by the business.

Stored per PIN as GeoJSON polygons (WGS84):
  exclude: areas that are not grass (a new patio, a garden the imagery missed)
  include: areas that are grass but were cut out
"""
from __future__ import annotations

import json
import threading

from .pricing import DATA_DIR

PATH = DATA_DIR / "corrections.json"
_lock = threading.Lock()


def _load() -> dict:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text())
        except Exception:
            return {}
    return {}


def get(pin: str) -> dict | None:
    with _lock:
        c = _load().get(pin)
    return c if c and (c.get("exclude") or c.get("include")) else None


def put(pin: str, exclude: list, include: list) -> dict:
    with _lock:
        d = _load()
        if exclude or include:
            d[pin] = {"exclude": exclude, "include": include}
        else:
            d.pop(pin, None)
        PATH.write_text(json.dumps(d))
    return d.get(pin) or {"exclude": [], "include": []}
