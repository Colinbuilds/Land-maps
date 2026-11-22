"""Business settings and ballpark pricing.

Settings live in data/settings.json so the business can edit them from the
dashboard. Each service prices off one or more measured areas.
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
_lock = threading.Lock()

DEFAULT_SETTINGS = {
    "business": {
        "name": "Your Company",
        "phone": "",
        "email": "",
        "accent": "#16a34a",
    },
    # Ballpark range shown to customers: price x (1 - spread) .. price x (1 + spread)
    "spread": 0.15,
    "services": {
        "mowing": {
            "enabled": True,
            "label": "Lawn mowing",
            "unit": "per visit",
            "measures": ["lawn_sqft", "parkway_sqft"],
            "rate": 5.50, "rate_unit": "per 1,000 sq ft",
            "minimum": 40.0,
        },
        "fertilizer": {
            "enabled": True,
            "label": "Fertilizer & weed control",
            "unit": "per application",
            "measures": ["lawn_sqft", "parkway_sqft"],
            "rate": 4.75, "rate_unit": "per 1,000 sq ft",
            "minimum": 45.0,
            "applications_per_year": 5,
        },
        "snow": {
            "enabled": True,
            "label": "Snow removal",
            "unit": "per push",
            "measures": ["driveway_sqft", "sidewalk_sqft"],
            "rate": 45.0, "rate_unit": "per 1,000 sq ft",
            "minimum": 45.0,
            "season_pushes": 12,
        },
        "concrete_driveway": {
            "enabled": True,
            "label": "Driveway replacement (concrete)",
            "unit": "installed",
            "measures": ["driveway_sqft"],
            "rate": 11.00, "rate_unit": "per sq ft",
            "minimum": 3500.0,
            "finishes": {
                "broom": {"label": "Standard broom finish", "multiplier": 1.0},
                "exposed": {"label": "Exposed aggregate", "multiplier": 1.25},
                "stamped": {"label": "Stamped & colored", "multiplier": 1.6},
                "stained": {"label": "Smooth with color stain", "multiplier": 1.3},
            },
        },
        "concrete_sidewalk": {
            "enabled": True,
            "label": "Sidewalk replacement (concrete)",
            "unit": "installed",
            "measures": ["sidewalk_sqft"],
            "rate": 10.00, "rate_unit": "per sq ft",
            "minimum": 1200.0,
        },
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    with _lock:
        if SETTINGS_PATH.exists():
            try:
                return _merge(DEFAULT_SETTINGS, json.loads(SETTINGS_PATH.read_text()))
            except Exception:
                pass
        return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(new: dict) -> dict:
    merged = _merge(DEFAULT_SETTINGS, new)
    with _lock:
        SETTINGS_PATH.write_text(json.dumps(merged, indent=2))
    return merged


def _per_unit(rate_unit: str) -> float:
    return 1000.0 if "1,000" in rate_unit or "1000" in rate_unit else 1.0


def price_service(svc: dict, m: dict, spread: float, finish: str | None = None) -> dict | None:
    sqft = sum(float(m.get(k) or 0) for k in svc["measures"])
    if sqft <= 0:
        return None
    price = sqft / _per_unit(svc.get("rate_unit", "")) * float(svc["rate"])
    if finish and svc.get("finishes", {}).get(finish):
        price *= float(svc["finishes"][finish]["multiplier"])
    price = max(price, float(svc.get("minimum") or 0))
    out = {
        "label": svc["label"],
        "unit": svc["unit"],
        "sqft": round(sqft),
        "low": _round_price(price * (1 - spread)),
        "high": _round_price(price * (1 + spread)),
    }
    if svc.get("applications_per_year"):
        n = svc["applications_per_year"]
        out["annual"] = {"low": _round_price(out["low"] * n), "high": _round_price(out["high"] * n),
                         "note": f"{n}-application program"}
    if svc.get("season_pushes"):
        n = svc["season_pushes"]
        out["annual"] = {"low": _round_price(out["low"] * n), "high": _round_price(out["high"] * n),
                         "note": f"season of ~{n} pushes"}
    if svc.get("finishes"):
        out["finishes"] = {k: v["label"] for k, v in svc["finishes"].items()}
        out["finish"] = finish or next(iter(svc["finishes"]))
    return out


def _round_price(x: float) -> int:
    if x >= 1000:
        return int(round(x / 50.0) * 50)
    if x >= 100:
        return int(round(x / 5.0) * 5)
    return int(round(x))


def estimate(measurements: dict, settings: dict | None = None, finish: str | None = None) -> dict:
    s = settings or load_settings()
    out = {}
    for key, svc in s["services"].items():
        if not svc.get("enabled"):
            continue
        p = price_service(svc, measurements, float(s.get("spread", 0.15)), finish if svc.get("finishes") else None)
        if p:
            out[key] = p
    return out
