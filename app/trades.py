"""Trades: what each kind of home-service company buys and looks at.

Each trade has
  * a ZIP-list preset: the sheet measurements it needs (or the Premier list
    when its numbers only exist in the full report, e.g. roofs), and
  * the headline numbers it quotes from, pulled out of a full property
    report for one address (single pulls, the demo page, member reports).

Measurements marked beta (fences, pools) are shown for evaluation but are
not sold yet.
"""
from __future__ import annotations

TRADES = [
    # key, label, list preset (sheet measurement types), premier?, pitch
    ("lawn", "Lawn care & fertilizer", ["lawn", "beds", "trees"], False,
     "Grass-only square footage, parkway, beds and trees for every home."),
    ("snow", "Snow removal", ["driveway", "sidewalk"], False,
     "Driveway and public sidewalk area per home, to price routes and seasons."),
    ("concrete", "Concrete & sealcoating", ["driveway", "patios", "sidewalk"], False,
     "Driveway, patio and walkway areas: find the big driveways."),
    ("pressure", "Pressure washing", ["driveway", "patios", "sidewalk", "walls"], False,
     "Flatwork and house-wall area per home: price house washes and driveways."),
    ("windows", "Window cleaning", ["walls"], False,
     "Estimated window count and wall area per home; target high-value homes."),
    ("gutters", "Gutter cleaning & guards", [], True,
     "Gutter feet, downspouts, stories and trees over the roof (full reports)."),
    ("lights", "Holiday lighting", [], True,
     "Roofline lengths (eaves, rakes, ridges) for every home (full reports)."),
    ("roofing", "Roofing", [], True,
     "Roof area, squares, pitch and every line length (full reports)."),
    ("siding", "Siding & painting", ["walls"], False,
     "House wall area per home; full reports add sides, corners, soffit and fascia."),
    ("solar", "Solar", [], True,
     "Roof area by direction faced, pitch and tree shade (full reports)."),
    ("trees", "Tree service", ["trees"], False,
     "Tree count, canopy and lawn under trees; full reports add trees over the roof."),
    ("landscaping", "Landscaping & mulch", ["beds", "lawn", "trees"], False,
     "Landscape bed area for mulch and rock, plus lawn and trees."),
    ("irrigation", "Sprinklers & irrigation", ["lawn"], False,
     "Lawn and parkway area: size systems and winterizations."),
    ("pest", "Pest & mosquito control", ["lawn", "beds", "trees", "building"], False,
     "Yard, bed, canopy and house footprint: the areas you treat."),
    ("hvac", "HVAC & insulation", ["building"], False,
     "House size, year built and value from the assessor, plus footprint."),
    ("fencing", "Fencing (beta)", ["building"], False,
     "Lot size and home value; fence detection is in beta."),
    ("pets", "Pet waste removal (beta)", ["lawn"], False,
     "Lawn area; fenced-yard detection is in beta."),
    ("pools", "Pool service (beta)", ["lawn"], False,
     "Pool detection is in beta."),
]
TRADE_KEYS = [t[0] for t in TRADES]


def catalog() -> list[dict]:
    return [{"key": k, "label": l, "types": types, "premier": prem, "pitch": pitch}
            for k, l, types, prem, pitch in TRADES]


def label(key: str | None) -> str | None:
    return next((t[1] for t in TRADES if t[0] == key), None)


# ------------------------------------------------------------------ metrics
def _n(v, unit=""):
    return None if v in (None, "") else (round(float(v)), unit)


def _facing(d) -> dict:
    """Sloped roof area by the direction each facet faces (solar)."""
    out = {"South": 0.0, "East": 0.0, "West": 0.0, "North": 0.0, "Flat": 0.0}
    for st in d.get("structures") or []:
        for f in st.get("facet_polys") or []:
            dv = f.get("down")
            if not dv or not f.get("pitch"):
                out["Flat"] += f["area_sqft"]
                continue
            dx, dy = dv
            if abs(dy) >= abs(dx):
                out["South" if dy < 0 else "North"] += f["area_sqft"]
            else:
                out["East" if dx > 0 else "West"] += f["area_sqft"]
    return {k: round(v) for k, v in out.items()}


def metrics(key: str, d: dict) -> list[dict]:
    """The numbers a trade quotes from, for one property report ``d``."""
    w = d.get("walls") or {}
    y = d.get("yard") or {}
    L = d.get("lines") or {}
    side = w.get("by_side") or {}
    m: list[tuple] = []
    add = lambda label, v, unit="sq ft", beta=False, note=None: m.append((label, v, unit, beta, note))
    flat = sum(float(y.get(k) or 0) for k in ("driveway_sqft", "other_hardscape_sqft", "sidewalk_sqft"))
    if key == "lawn":
        add("Lawn (grass only)", y.get("lawn_sqft")); add("Parkway grass", y.get("parkway_sqft"))
        add("Lawn under trees", y.get("lawn_under_trees_sqft")); add("Landscape beds", y.get("beds_sqft"))
        add("Trees on the lot", y.get("tree_count"), "trees"); add("Lot", y.get("lot_sqft"))
    elif key == "snow":
        add("Driveway", y.get("driveway_sqft")); add("Public sidewalk", y.get("sidewalk_sqft"))
        add("Driveway + sidewalk", (y.get("driveway_sqft") or 0) + (y.get("sidewalk_sqft") or 0))
        add("Street frontage", y.get("frontage_ft"), "ft")
    elif key == "concrete":
        add("Driveway", y.get("driveway_sqft")); add("Patios & walks", y.get("other_hardscape_sqft"))
        add("Public sidewalk", y.get("sidewalk_sqft")); add("Year built", y.get("year_built"), "year")
    elif key == "pressure":
        add("Driveway + patios + sidewalk", flat); add("Driveway", y.get("driveway_sqft"))
        add("Patios & walks", y.get("other_hardscape_sqft")); add("House walls (wash)", w.get("gross_wall_sqft"))
        add("Roof (soft wash)", d.get("roof_sqft"))
    elif key == "windows":
        add("Windows & doors (estimate)", w.get("est_openings"), "openings")
        add("Stories (estimate)", w.get("stories"), "")
        add("House wall area", w.get("gross_wall_sqft")); add("House size (assessor)", y.get("living_sqft"))
        add("Home value (assessed)", y.get("home_value"), "$")
    elif key == "gutters":
        add("Gutters (eaves)", w.get("gutter_ft") or L.get("eave"), "ft"); add("Downspouts (estimate)", w.get("downspouts"), "")
        add("Stories (estimate)", w.get("stories"), ""); add("Roof under trees", d.get("tree_overhang_sqft"))
        add("Fascia", w.get("fascia_ft"), "ft")
    elif key == "lights":
        add("Eaves", L.get("eave"), "ft"); add("Rakes (gable edges)", L.get("rake"), "ft")
        add("Ridges", L.get("ridge"), "ft")
        add("Roofline total (eaves + rakes + ridges)", (L.get("eave") or 0) + (L.get("rake") or 0) + (L.get("ridge") or 0), "ft")
        add("Stories (estimate)", w.get("stories"), ""); add("Home value (assessed)", y.get("home_value"), "$")
    elif key == "roofing":
        add("Roof area", d.get("roof_sqft")); add("Squares", d.get("squares"), "sq")
        add("Predominant pitch", d.get("pitch_label"), ""); add("Facets", d.get("facets"), "")
        for k_, lab in (("ridge", "Ridges"), ("hip", "Hips"), ("valley", "Valleys"), ("rake", "Rakes"), ("eave", "Eaves")):
            add(lab, L.get(k_), "ft")
        add("Year built", y.get("year_built"), "year")
    elif key == "siding":
        add("House walls (gross)", w.get("gross_wall_sqft")); add("Net of windows/doors (estimate)", w.get("est_net_wall_sqft"))
        for s_ in ("North", "East", "South", "West"):
            add(f"{s_} walls", side.get(s_))
        add("Outside corners", w.get("outside_corners_ft"), "ft"); add("Soffit", w.get("soffit_sqft")); add("Fascia", w.get("fascia_ft"), "ft")
    elif key == "solar":
        fac = _facing(d)
        for k_ in ("South", "West", "East", "North", "Flat"):
            add(f"Roof facing {k_.lower()}" if k_ != "Flat" else "Flat roof", fac[k_])
        add("Predominant pitch", d.get("pitch_label"), ""); add("Roof under trees (shade)", d.get("tree_overhang_sqft"))
    elif key == "trees":
        add("Trees on the lot", y.get("tree_count"), "trees"); add("Tree canopy over the lot", y.get("tree_canopy_sqft"))
        add("Roof under trees", d.get("tree_overhang_sqft")); add("Lawn under trees", y.get("lawn_under_trees_sqft"))
    elif key == "landscaping":
        add("Landscape beds (mulch/rock)", y.get("beds_sqft")); add("Lawn", y.get("lawn_sqft"))
        add("Trees on the lot", y.get("tree_count"), "trees"); add("Lot", y.get("lot_sqft"))
    elif key == "irrigation":
        add("Lawn (grass only)", y.get("lawn_sqft")); add("Parkway grass", y.get("parkway_sqft"))
        add("Lawn + parkway", (y.get("lawn_sqft") or 0) + (y.get("parkway_sqft") or 0)); add("Lot", y.get("lot_sqft"))
    elif key == "pest":
        add("Lot", y.get("lot_sqft")); add("Lawn", y.get("lawn_sqft")); add("Landscape beds", y.get("beds_sqft"))
        add("Tree canopy", y.get("tree_canopy_sqft")); add("House perimeter", w.get("wall_perimeter_ft"), "ft")
    elif key == "hvac":
        add("House size (assessor)", y.get("living_sqft")); add("Year built", y.get("year_built"), "year")
        add("Stories (estimate)", w.get("stories"), ""); add("Building footprint", y.get("building_sqft"))
        add("Home value (assessed)", y.get("home_value"), "$")
    elif key == "fencing":
        add("Fence along lot lines", y.get("fence_ft"), "ft", beta=True)
        add("Fenced yard", "yes" if y.get("fenced_yard") else "no", "", beta=True)
        add("Lot", y.get("lot_sqft")); add("Home value (assessed)", y.get("home_value"), "$")
    elif key == "pets":
        add("Lawn", y.get("lawn_sqft")); add("Fenced yard", "yes" if y.get("fenced_yard") else "no", "", beta=True)
    elif key == "pools":
        add("Pool", y.get("pool_sqft"), "sq ft", beta=True)
        add("Pool on the lot", "yes" if y.get("has_pool") else "no", "", beta=True)
    out = []
    for lab, v, unit, beta, note in m:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            v = round(float(v), 2) if unit == "sq" else round(float(v))
        if unit == "year":
            v, unit = str(v), ""
        out.append({"label": lab, "value": v, "unit": unit, "beta": beta})
    return out


def summaries(d: dict) -> list[dict]:
    """Every trade's headline numbers for one report."""
    return [{"key": k, "label": l, "premier": prem, "metrics": metrics(k, d)} for k, l, _t, prem, _p in TRADES]


# ------------------------------------------------------------------ pricing
# Each company sets its rates once per trade; every report, sheet, Premier
# mail file and postcard then shows "your price" for that home automatically.
# price = max(minimum, base + rate x (measurement / per))
# ``sheet``: sheet columns summed for the measurement (ZIP sheets);
# ``report``: how to read it from a full report.
def _y(k):
    return lambda d: (d.get("yard") or {}).get(k)


def _w(k):
    return lambda d: (d.get("walls") or {}).get(k)


def _sum(*fs):
    def f(d):
        vals = [g(d) for g in fs]
        return None if all(v in (None, "") for v in vals) else sum(float(v or 0) for v in vals)
    return f


PRICE_RULES = {
    # trade: (measured, unit, per, default label, default rate $, default minimum $, sheet columns, report getter)
    "lawn": ("lawn + parkway", "sq ft", 1000, "per mow", 6.0, 35.0, ["lawn_sqft", "parkway_sqft"],
             _sum(_y("lawn_sqft"), _y("parkway_sqft"))),
    "snow": ("driveway + sidewalk", "sq ft", 1000, "per push", 40.0, 45.0, ["driveway_sqft", "sidewalk_sqft"],
             _sum(_y("driveway_sqft"), _y("sidewalk_sqft"))),
    "concrete": ("driveway", "sq ft", 1, "to seal", 0.25, 150.0, ["driveway_sqft"], _y("driveway_sqft")),
    "pressure": ("driveway + patios + sidewalk", "sq ft", 1, "per wash", 0.20, 125.0,
                 ["driveway_sqft", "other_hardscape_sqft", "sidewalk_sqft"],
                 _sum(_y("driveway_sqft"), _y("other_hardscape_sqft"), _y("sidewalk_sqft"))),
    "windows": ("windows", "windows", 1, "per cleaning", 8.0, 150.0, ["est_windows"], _w("est_openings")),
    "gutters": ("gutters", "ft", 1, "per cleaning", 1.25, 125.0, [], _w("gutter_ft")),
    "lights": ("roofline", "ft", 1, "installed", 3.0, 300.0, [],
               lambda d: sum(float((d.get("lines") or {}).get(k) or 0) for k in ("eave", "rake", "ridge")) or None),
    "roofing": ("roof", "squares", 1, "to replace", 450.0, 6000.0, [], lambda d: d.get("squares")),
    "siding": ("house walls", "sq ft", 1, "installed", 8.0, 5000.0, ["wall_sqft"], _w("gross_wall_sqft")),
    "solar": ("roof", "sq ft", 1, "system estimate", 0.0, 0.0, [], lambda d: d.get("roof_sqft")),
    "trees": ("trees", "trees", 1, "per trim", 150.0, 250.0, ["tree_count"], _y("tree_count")),
    "landscaping": ("beds", "sq ft", 1, "to mulch", 0.75, 150.0, ["beds_sqft"], _y("beds_sqft")),
    "irrigation": ("lawn + parkway", "sq ft", 1000, "to winterize", 5.0, 60.0, ["lawn_sqft", "parkway_sqft"],
                   _sum(_y("lawn_sqft"), _y("parkway_sqft"))),
    "pest": ("lot", "sq ft", 1000, "per treatment", 4.0, 69.0, ["lot_sqft"], _y("lot_sqft")),
    "hvac": ("house", "sq ft", 1000, "tune-up", 0.0, 99.0, ["living_sqft"], _y("living_sqft")),
}


def price_catalog() -> list[dict]:
    """What each trade's price is based on, with suggested starting rates."""
    out = []
    for k, (what, unit, per, lab, rate, mn, cols, _g) in PRICE_RULES.items():
        out.append({"key": k, "label": label(k), "measured": what, "unit": unit, "per": per,
                    "basis": f"per {per:,} {unit}" if per != 1 else f"per {unit.rstrip('s') if unit.endswith('s') and unit != 'sq ft' else unit}",
                    "price_label": lab, "rate": rate, "min": mn, "sheet_ok": bool(cols)})
    return out


def clean_rates(raw: dict | None) -> dict:
    out = {}
    for k, v in (raw or {}).items():
        if k not in PRICE_RULES or not isinstance(v, dict):
            continue
        try:
            r = {"rate": max(0.0, float(v.get("rate") or 0)), "min": max(0.0, float(v.get("min") or 0)),
                 "base": max(0.0, float(v.get("base") or 0)),
                 "label": str(v.get("label") or PRICE_RULES[k][3])[:30]}
        except (TypeError, ValueError):
            continue
        if r["rate"] or r["min"] or r["base"]:
            out[k] = r
    return out


def price_for(trade: str, rates: dict | None, amount) -> dict | None:
    """{"price": dollars, "label": "per mow", "text": "$48 per mow"} or None."""
    r = (rates or {}).get(trade)
    if not r or amount in (None, ""):
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if amount <= 0 and not r.get("min"):
        return None
    per = PRICE_RULES[trade][2]
    p = max(float(r.get("min") or 0), float(r.get("base") or 0) + float(r.get("rate") or 0) * amount / per)
    p = round(p) if p >= 20 else round(p, 2)
    return {"price": p, "label": r.get("label") or PRICE_RULES[trade][3],
            "text": f"${p:,.0f} {r.get('label') or PRICE_RULES[trade][3]}" if p >= 20 else f"${p:,.2f} {r.get('label')}"}


def report_price(trade: str, rates: dict | None, d: dict) -> dict | None:
    return price_for(trade, rates, PRICE_RULES[trade][7](d)) if trade in PRICE_RULES else None


def sheet_price(trade: str, rates: dict | None, row: dict) -> dict | None:
    """Price from a ZIP-sheet row (None when the sheet lacks the columns)."""
    if trade not in PRICE_RULES or not PRICE_RULES[trade][6]:
        return None
    vals = [row.get(c) for c in PRICE_RULES[trade][6]]
    if all(v in (None, "") for v in vals):
        return None
    return price_for(trade, rates, sum(float(v or 0) for v in vals))


# ------------------------------------------------------------ report sections
# A report only carries what the company's trades need.
#   roof        roof area, pitch, lines, waste, facet diagrams
#   walls       wall areas, siding, windows, gutters, soffit and fascia
#   elevations  the four side views
#   yard        lot & yard: lawn, driveway, sidewalk, beds, trees (shaded photo)
# County facts (lot size, house size, year built, value) come with every report.
SECTIONS = ("roof", "walls", "elevations", "yard")
TRADE_SECTIONS = {
    "lawn": {"yard"}, "snow": {"yard"}, "concrete": {"yard"}, "pressure": {"yard", "walls"},
    "windows": {"walls", "elevations"}, "gutters": {"roof", "walls"}, "lights": {"roof"},
    "roofing": {"roof"}, "siding": {"walls", "elevations"}, "solar": {"roof"}, "trees": {"yard"},
    "landscaping": {"yard"}, "irrigation": {"yard"}, "pest": {"yard", "walls"}, "hvac": {"walls"},
    "fencing": {"yard"}, "pets": {"yard"}, "pools": {"yard"},
}
SECTION_LABELS = {"roof": "Roof", "walls": "Walls & siding", "elevations": "Elevations", "yard": "Lot & yard"}
_BASIC_YARD = ("lot_sqft", "home_value", "year_built", "living_sqft", "building_sqft")
_ROOF_KEYS = ("squares", "pitch", "pitch_label", "pitch_table", "pitch_measured", "facets", "lines", "derived",
              "waste", "suggested_waste", "line_labels", "tree_note")


def sections_for(trade_keys) -> set[str]:
    """Sections a company's trades unlock. No trades (older accounts) = all."""
    ks = [t for t in (trade_keys or []) if t in TRADE_SECTIONS]
    if not ks:
        return set(SECTIONS)
    out = set()
    for t in ks:
        out |= TRADE_SECTIONS[t]
    return out


def filter_report(d: dict, sections: set[str]) -> dict:
    """The report with only the unlocked sections (the full one is kept on file)."""
    if set(SECTIONS) <= set(sections):
        return dict(d, sections=list(SECTIONS))
    out = dict(d)
    if "roof" not in sections:
        for k in _ROOF_KEYS:
            out.pop(k, None)
        out["structures"] = [{"name": s.get("name"), "plan_sqft": s.get("plan_sqft")} for s in d.get("structures") or []]
    if "walls" not in sections:
        out.pop("walls", None)
    elif "elevations" not in sections and out.get("walls"):
        out["walls"] = {k: v for k, v in out["walls"].items() if k != "elevations"}
    if "yard" not in sections and out.get("yard"):
        out["yard"] = {**{k: out["yard"].get(k) for k in _BASIC_YARD}, "basic": True}
    out["sections"] = [s for s in SECTIONS if s in sections]
    return out


def suggested_rates() -> dict:
    """Every trade at its suggested starting price (demo and sample postcards)."""
    return clean_rates({r["key"]: {"rate": r["rate"], "min": r["min"], "label": r["price_label"]} for r in price_catalog()})
