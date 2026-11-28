"""Sell measured ZIP-code sheets: quotes, free previews, orders, cached datasets.

Measure once, sell many times: each (ZIP, property types) sheet is measured
one time and kept under data/zips/. Every later order and every free preview
for that ZIP is served from the saved sheet at no processing cost.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import secrets
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import bulk, counties
from .measure import MEASUREMENTS, measurer_for
from .pricing import DATA_DIR, load_settings

ZIPS_DIR = DATA_DIR / "zips"
ZIPS_DIR.mkdir(exist_ok=True)
DB = DATA_DIR / "datasales.db"
_lock = threading.Lock()
_building: dict = {}

PREVIEW_TOTAL = 15     # free preview: the contractor's own homes + more from the ZIP
PREVIEW_CHOSEN = 5
SAMPLES_PER_IP_PER_DAY = 3
DATASET_MAX_AGE_DAYS = 365

# Columns sold. Owner names come from the county assessor and are filled in at
# download time (bulk.zip_owners); counties where marketing use of public-record
# names isn't allowed (Kansas) leave them blank.
COLUMNS = [
    ("address", "Address"), ("city", "City"), ("zip", "ZIP"), ("county", "County"),
    ("owner", "Owner name"), ("owner_mailing", "Owner mailing address"),
    ("lot_sqft", "Lot sq ft"), ("home_value", "Assessed value $"), ("year_built", "Year built"),
    ("living_sqft", "House sq ft"), ("lawn_sqft", "Lawn sq ft (grass only)"),
    ("parkway_sqft", "Parkway grass sq ft"), ("building_sqft", "Building footprint sq ft"),
    ("driveway_sqft", "Driveway sq ft"), ("sidewalk_sqft", "Public sidewalk sq ft"),
    ("other_hardscape_sqft", "Patios & walks sq ft"), ("structures_sqft", "Decks/sheds/play sets sq ft"),
    ("beds_sqft", "Landscape beds sq ft"), ("frontage_ft", "Street frontage ft"),
    ("wall_sqft", "House wall area sq ft (siding)"), ("est_windows", "Windows & doors (estimate)"),
    ("quality", "Quality (ok / review)"), ("notes", "Notes"),
    ("lat", "Latitude"), ("lon", "Longitude"), ("pin", "Property id"),
]

# What a buyer can pick. The first type costs `rate_cents` per property,
# each extra type `extra_type_cents` more. Every sheet also has the basics.
MEASURE_TYPES = [
    ("lawn", "Lawn (grass only) + parkway", ["lawn_sqft", "parkway_sqft"]),
    ("driveway", "Driveway", ["driveway_sqft"]),
    ("sidewalk", "Public sidewalk + street frontage", ["sidewalk_sqft", "frontage_ft"]),
    ("patios", "Patios & walks", ["other_hardscape_sqft"]),
    ("structures", "Decks, sheds & play sets", ["structures_sqft"]),
    ("beds", "Landscape beds", ["beds_sqft"]),
    ("building", "Building footprint", ["building_sqft"]),
    ("walls", "House walls & windows (siding)", ["wall_sqft", "est_windows"]),
    # add-on, not a measurement: each home's assessed value as a column (+5c)
    ("value", "Assessed home value (per house)", ["home_value"]),
]
ADDONS = {"value"}  # priced on their own, never the "first measurement", never locked
TYPE_KEYS = [k for k, _, _ in MEASURE_TYPES]
BASE_HEAD = ["address", "city", "zip", "county", "owner", "owner_mailing", "lot_sqft", "year_built", "living_sqft"]
BASE_TAIL = ["quality", "notes", "lat", "lon", "pin"]

DEFAULT_PRICING = {
    "rate_cents": 10.0,          # per property, for the first measurement type
    "extra_type_cents": 5.0,     # per property, for each additional type
    "premier_cents": 25.0,       # per property, Premier list (full report PDF per home)
    "value_cents": 5.0,          # per property, add-on: each home's assessed value as a column
    "min_order_usd": 25.0,
    # price multiplier per assessed-value band (every $250k: <250k ... 1.75-2M, 2M+)
    "value_multipliers": [1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
    # Exclusive ZIP: buy every home in the ZIP and nobody else can buy those
    # measurement types there until the lock runs out. Whole ZIP only.
    "exclusive_cents": 100.0,        # per property, first measurement type
    "exclusive_extra_cents": 50.0,   # per property, each additional type
    "exclusive_months": 12,          # how long the lock lasts
}


def band_range(lo, hi) -> tuple[int, int]:
    """Clamp a buyer's value-band range (inclusive band indexes)."""
    from .bulk import VALUE_EDGES
    top = len(VALUE_EDGES)
    lo = 0 if lo is None else max(0, min(top, int(lo)))
    hi = top if hi is None else max(lo, min(top, int(hi)))
    return lo, hi


def range_label(lo: int, hi: int) -> str:
    """'All home values', 'Homes valued $500k+', 'Homes valued $250k-$750k', 'Homes valued under $250k'."""
    from .bulk import VALUE_EDGES
    top = len(VALUE_EDGES)
    lo, hi = band_range(lo, hi)

    def k(v):
        return f"${v / 1e6:g}M" if v >= 1_000_000 else f"${v // 1000:g}k"
    if lo == 0 and hi == top:
        return "All home values"
    if hi == top:
        return f"Homes valued {k(VALUE_EDGES[lo - 1])}+"
    if lo == 0:
        return f"Homes valued under {k(VALUE_EDGES[hi])}"
    return f"Homes valued {k(VALUE_EDGES[lo - 1])}-{k(VALUE_EDGES[hi])}"


def normalize_types(types) -> list[str]:
    """Valid types in catalogue order; lawn when no measurement is given
    (add-ons like the assessed value can't be bought on their own)."""
    want = {str(t).strip().lower() for t in (types or []) if str(t).strip()}
    out = [k for k in TYPE_KEYS if k in want]
    if not [k for k in out if k not in ADDONS]:
        out = ["lawn"] + out
    return out


def measures_of(types) -> list[str]:
    """The measurement types (no add-ons)."""
    return [t for t in normalize_types(types) if t not in ADDONS]


def columns_for(types) -> list[tuple[str, str]]:
    labels = dict(COLUMNS)
    keys = list(BASE_HEAD)
    for k, _, cols in MEASURE_TYPES:
        if k in normalize_types(types):
            keys += cols
    keys += BASE_TAIL
    return [(k, labels[k]) for k in keys]


def per_property_cents(types, p: dict | None = None) -> float:
    p = p or pricing()
    n = len(measures_of(types))
    addon = float(p.get("value_cents", 5.0)) if "value" in normalize_types(types) else 0.0
    return float(p["rate_cents"]) + float(p.get("extra_type_cents", 5.0)) * (n - 1) + addon


# ------------------------------------------------------------------ storage
def _db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _lock, _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, email TEXT NOT NULL, zip TEXT NOT NULL, classes TEXT NOT NULL,
            count INTEGER, amount_cents INTEGER, status TEXT NOT NULL, token TEXT NOT NULL,
            stripe_session TEXT, created REAL NOT NULL, paid REAL, note TEXT);
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL, ip TEXT, zip TEXT, ts REAL NOT NULL);
        """)
        have = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
        if "types" not in have:  # orders before per-type pricing: lawn sheet
            con.execute("ALTER TABLE orders ADD COLUMN types TEXT")
        if "site" not in have:   # the site address the buyer used (for email links)
            con.execute("ALTER TABLE orders ADD COLUMN site TEXT")
        if "product" not in have:  # standard sheet or premier (report PDF per home)
            con.execute("ALTER TABLE orders ADD COLUMN product TEXT")
        for col in ("value_lo", "value_hi"):  # home-value band range bought
            if col not in have:
                con.execute(f"ALTER TABLE orders ADD COLUMN {col} INTEGER")
        if "new_types" not in have:  # exclusive orders: the types this order adds (charged)
            con.execute("ALTER TABLE orders ADD COLUMN new_types TEXT")
        for col in ("grp", "grp_token"):  # several ZIPs bought together, paid in one checkout
            if col not in have:
                con.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        con.execute("""CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY, order_id TEXT, zip TEXT, email TEXT, kind TEXT, ts REAL NOT NULL)""")
        # Exclusive ZIP locks: one holder per (ZIP, measurement type)
        con.execute("""CREATE TABLE IF NOT EXISTS zip_locks (
            zip TEXT NOT NULL, type TEXT NOT NULL, email TEXT NOT NULL, order_id TEXT NOT NULL,
            since REAL NOT NULL, until REAL NOT NULL, PRIMARY KEY (zip, type))""")


def pricing() -> dict:
    p = dict(DEFAULT_PRICING)
    saved = load_settings().get("data_sales") or {}
    p.update({k: v for k, v in saved.items() if k in DEFAULT_PRICING})
    return p


def price_cents(count: int, types=None, p: dict | None = None) -> int:
    p = p or pricing()
    cents = count * per_property_cents(types, p)
    return int(max(math.ceil(cents - 1e-9), round(float(p["min_order_usd"]) * 100)))


def _key(zip_code: str, classes: list[str]) -> str:
    z = "".join(ch for ch in zip_code if ch.isdigit())[:5]
    return f"{z}_{''.join(sorted(set(classes))) or 'ALL'}"


def dataset_path(zip_code: str, classes: list[str]):
    return ZIPS_DIR / f"{_key(zip_code, classes)}.csv"


def dataset_meta(zip_code: str, classes: list[str]) -> dict | None:
    p = ZIPS_DIR / f"{_key(zip_code, classes)}.json"
    return json.loads(p.read_text()) if p.exists() else None


def dataset_ready(zip_code: str, classes: list[str]) -> bool:
    m = dataset_meta(zip_code, classes)
    # a sheet missing a county (its server was down) is redone after 3 days
    max_age = 3 if m and m.get("skipped") else DATASET_MAX_AGE_DAYS
    return bool(m and m.get("status") == "ready" and dataset_path(zip_code, classes).exists()
                and time.time() - m.get("finished", 0) < max_age * 86400)


# ------------------------------------------------------------------- quotes
PREMIER_CENTS_DEFAULT = 25.0


def list_price(count: int, types=None, product: str = "standard", member: bool = False, p: dict | None = None,
               bands: list[int] | None = None, lo: int = 0, hi: int | None = None) -> dict:
    """Price of a ZIP list. Standard: per-measurement pricing. Premier: a full
    property report PDF for every home. With value bands, each home is
    priced at its band's multiplier and only the chosen bands are counted.
    Members pay half."""
    p = p or pricing()
    per = float(p.get("premier_cents", PREMIER_CENTS_DEFAULT)) if product == "premier" else per_property_cents(types, p)
    # the assessed-value add-on is a flat amount per home (not scaled by home value)
    addon = float(p.get("value_cents", 5.0)) if product != "premier" and "value" in normalize_types(types) else 0.0
    base = per - addon
    if bands is not None:
        lo, hi = band_range(lo, hi)
        mult = list(p.get("value_multipliers") or DEFAULT_PRICING["value_multipliers"])
        mult += [mult[-1]] * (len(bands) - len(mult))
        count = sum(bands[lo:hi + 1])
        raw = sum(bands[i] * base * float(mult[i]) for i in range(lo, hi + 1)) + count * addon
    else:
        raw = count * per
    cents = int(max(math.ceil(raw - 1e-9), round(float(p["min_order_usd"]) * 100))) if count else 0
    full = cents
    if member:
        from .billing import plans
        cents = int(round(cents * (1 - float(plans()["list_discount"]))))
    return {"per_property_cents": per * (0.5 if member else 1), "price_cents": cents, "full_price_cents": full,
            "member_discount": member, "count": count}


def quote(zip_code: str, classes: list[str], types=None, product: str = "standard", member: bool = False,
          lo: int | None = None, hi: int | None = None, email: str | None = None) -> dict:
    pv = bulk.preview(zip_code, classes)
    meta = dataset_meta(zip_code, classes)
    p = pricing()
    types = normalize_types(types)
    vb = bulk.value_bands(zip_code, classes) if pv["count"] else {"bands": [], "labels": [], "total": 0}
    if product == "exclusive":
        lo, hi = band_range(None, None)  # whole ZIP only
        n = vb.get("total", pv["count"]) if vb["bands"] else pv["count"]
        lp = exclusive_price(zip_code, n, types, email, p)
    else:
        lo, hi = band_range(lo, hi)
        lp = list_price(0, types, product, member, p, bands=vb["bands"] or [0], lo=lo, hi=hi)
        n = lp.pop("count")
    lk = locks(zip_code)
    mine = {t for t, l in lk.items() if email and l["email"] == email.lower()}
    taken = [t for t in lk if t not in mine]
    blocked = sale_blocked(zip_code, types, product, email)
    return {"zip": zip_code, "classes": classes, "count": n, "zip_total": vb.get("total", pv["count"]),
            "locks": {t: {"until": l["until"], "yours": t in mine} for t, l in lk.items()},
            "taken_types": taken, "blocked": blocked,
            "by_county": pv["by_county"], "value_bands": vb, "value_lo": lo, "value_hi": hi,
            "value_label": range_label(lo, hi),
            "types": types, "product": product, **lp, "pricing": p,
            "ready": dataset_ready(zip_code, classes),
            "measured": meta.get("finished") if meta and meta.get("status") == "ready" else None}


# --------------------------------------------------------- exclusive ZIPs
def locks(zip_code: str) -> dict:
    """Active locks in a ZIP: {type: {email, until, order_id}}."""
    with _db() as con:
        rows = con.execute("SELECT * FROM zip_locks WHERE zip=? AND until>?", (zip_code, time.time())).fetchall()
    return {r["type"]: {"email": r["email"], "until": r["until"], "order_id": r["order_id"]} for r in rows}


def all_locks() -> list[dict]:
    with _db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM zip_locks ORDER BY zip, type")]


def held_types(zip_code: str, email: str | None) -> list[str]:
    if not email:
        return []
    return [t for t, l in locks(zip_code).items() if l["email"] == email.lower()]


def taken_by_others(zip_code: str, email: str | None = None) -> list[str]:
    e = (email or "").lower()
    return [t for t, l in locks(zip_code).items() if l["email"] != e]


def sale_blocked(zip_code: str, types, product: str = "standard", email: str | None = None) -> str | None:
    """Why this sale can't happen (another company holds the ZIP), else None."""
    taken = taken_by_others(zip_code, email)
    if not taken:
        return None
    lab = dict((k, l) for k, l, _ in MEASURE_TYPES)
    names = ", ".join(lab.get(t, t) for t in TYPE_KEYS if t in taken)
    if product == "premier":
        return (f"ZIP {zip_code} is exclusive to another company for: {names}. "
                f"Premier lists include every measurement, so they can't be sold in this ZIP right now.")
    hit = [t for t in normalize_types(types) if t in taken]
    if hit:
        return (f"ZIP {zip_code} is exclusive to another company for: "
                f"{', '.join(lab[t] for t in hit)}. Untick those to buy the rest.")
    return None


def exclusive_price(zip_code: str, count: int, types, email: str | None = None, p: dict | None = None) -> dict:
    """$1/home for the first type, 50c/home for each extra type. Types the
    buyer already holds in this ZIP aren't charged again; once they hold
    any type, each added type is the extra-type rate."""
    p = p or pricing()
    types = normalize_types(types)
    held = set(held_types(zip_code, email))
    new = [t for t in types if t not in held and t not in ADDONS]
    first, extra = float(p.get("exclusive_cents", 100.0)), float(p.get("exclusive_extra_cents", 50.0))
    per = (extra * len(new)) if held else ((first + extra * (len(new) - 1)) if new else 0.0)
    if new and "value" in types:  # the assessed-value column rides along at its own rate
        per += float(p.get("value_cents", 5.0))
    raw = count * per
    cents = int(max(math.ceil(raw - 1e-9), round(float(p["min_order_usd"]) * 100))) if (count and new) else 0
    return {"per_property_cents": per, "price_cents": cents, "full_price_cents": cents, "member_discount": False,
            "new_types": new, "already_held": [t for t in types if t in held],
            "exclusive_months": int(p.get("exclusive_months", 12))}


def _grant_locks(o: dict) -> list[str]:
    """Lock the order's new types for the buyer. Returns types someone else
    took in the meantime (then nothing is locked)."""
    z, email = o["zip"], o["email"].lower()
    new = measures_of((o.get("new_types") or o.get("types") or "").split(","))
    months = int(pricing().get("exclusive_months", 12))
    now = time.time()
    with _lock, _db() as con:
        clash = [r["type"] for r in con.execute(
            f"SELECT type FROM zip_locks WHERE zip=? AND until>? AND email<>? AND type IN ({','.join('?' * len(new))})",
            (z, now, email, *new))]
        if clash:
            return clash
        for t in new:
            con.execute("INSERT OR REPLACE INTO zip_locks(zip,type,email,order_id,since,until) VALUES(?,?,?,?,?,?)",
                        (z, t, email, o["id"], now, now + months * 30.44 * 86400))
    return []


def release_lock(zip_code: str, type_key: str):
    with _lock, _db() as con:
        con.execute("DELETE FROM zip_locks WHERE zip=? AND type=?", (zip_code, type_key))


def strip_locked(rows: list[dict], zip_code: str, email: str | None = None) -> tuple[list[dict], list[str]]:
    """Blank out columns of types another company holds (free previews)."""
    taken = taken_by_others(zip_code, email)
    cols = {c for k, _, cs in MEASURE_TYPES if k in taken for c in cs}
    if not cols:
        return rows, []
    return [{k: ("" if k in cols else v) for k, v in r.items()} for r in rows], taken


# ------------------------------------------------------------- row helpers
def _row(r: dict, county_name: str, parcel_geom=None) -> dict:
    row = {k: r.get(k) for k, _ in COLUMNS}
    row["pin"] = r.get("pid") or r.get("pin")
    row["county"] = county_name
    row["quality"] = (r.get("sources") or {}).get("quality")
    row["notes"] = (r.get("sources") or {}).get("note")
    if parcel_geom is not None:
        c = parcel_geom.representative_point()
        row["lat"], row["lon"] = round(c.y, 6), round(c.x, 6)
    return row


def _bulk_row_to_sale(row: dict) -> dict:
    out = {k: row.get(k, "") for k, _ in COLUMNS}
    return out


def to_csv(rows: list[dict], cols: list[tuple[str, str]] | None = None) -> str:
    cols = cols or COLUMNS
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in cols])
    for r in rows:
        w.writerow([r.get(k, "") if r.get(k) is not None else "" for k, _ in cols])
    return buf.getvalue()


def add_owners(rows: list[dict], zip_code: str, classes: list[str]) -> list[dict]:
    """Fill owner name and mailing address into sheet rows (by property id)."""
    try:
        own = bulk.zip_owners(zip_code, classes)
    except Exception:
        return rows
    for r in rows:
        o = own.get(str(r.get("pin") or ""))
        if o:
            r.update(o)
    return rows


def add_prices(rows: list[dict], cols: list[tuple[str, str]], rates: dict | None):
    """Add a "your price" column per trade the buyer has rates for (when the
    sheet has the measurements that trade is priced from)."""
    from . import trades
    keys = [k for k in (rates or {}) if trades.PRICE_RULES.get(k, (0,) * 7)[6]
            and all(c in {k_ for k_, _ in cols} for c in trades.PRICE_RULES[k][6])]
    for k in keys:
        col = f"price_{k}"
        cols = cols + [(col, f"Your price: {trades.label(k)} ({rates[k].get('label') or trades.PRICE_RULES[k][3]})")]
        for r in rows:
            p = trades.sheet_price(k, rates, r)
            r[col] = p["price"] if p else ""
    return rows, cols


def read_dataset(zip_code: str, classes: list[str]) -> list[dict]:
    with open(dataset_path(zip_code, classes), newline="") as fh:
        return [r for r in csv.DictReader(fh) if r.get("lawn_sqft") not in (None, "") and (r.get("address") or "").strip()]


# ------------------------------------------------------------------ samples
def sample_allowed(email: str, ip: str | None) -> str | None:
    """None if allowed, else the reason."""
    with _db() as con:
        if con.execute("SELECT 1 FROM samples WHERE lower(email)=lower(?)", (email,)).fetchone():
            return "This email has already used its free preview. Buy the full sheet, or contact us."
        if ip:
            n = con.execute("SELECT COUNT(*) FROM samples WHERE ip=? AND ts>?", (ip, time.time() - 86400)).fetchone()[0]
            if n >= SAMPLES_PER_IP_PER_DAY:
                return "Too many free previews from this connection today. Try again tomorrow."
    return None


class PreviewProblem(Exception):
    """An address the contractor picked can't be used. ``problems`` is a list of
    {index, address, problem, message} so the page can ask for a replacement."""

    def __init__(self, problems: list[dict]):
        super().__init__("; ".join(p["message"] for p in problems))
        self.problems = problems


class PreviewShort(Exception):
    """Too few measured homes in the ZIP for a 15-home preview."""

    def __init__(self, zip_code: str, available: int):
        self.available = available
        super().__init__(f"ZIP {zip_code} has {available} measured home{'' if available == 1 else 's'} we can show right now, "
                         f"and a free preview needs {PREVIEW_TOTAL}. Try a neighbouring ZIP, or buy the sheet to see every home.")


def _norm_addr(a: str) -> str:
    return " ".join(str(a or "").upper().replace(",", " ").replace(".", " ").split())


def _measured_row(cid: str, f: dict) -> dict | None:
    """A sale-format row for one parcel, measured now; None if it can't be measured
    or there's no house on it."""
    m = measurer_for(cid)
    try:
        row = _row(m.measure(f), m.c["name"], f["geometry"])
    except Exception:
        return None
    return row if _has_house(row) and row.get("lawn_sqft") is not None else None


def check_preview_address(zip_code: str, classes: list[str], text: str, resolve, zp=None) -> dict:
    """One address the contractor services: found, inside the ZIP, and measurable.
    ``resolve(text) -> (county id, full parcel feature, matched address)`` or
    raises LookupError. Returns {pid, address, row} or raises PreviewProblem."""
    def bad(problem, message):
        raise PreviewProblem([{"index": None, "address": text, "problem": problem, "message": message}])
    if not str(text or "").strip():
        bad("empty", "Enter an address.")
    try:
        cid, f, label = resolve(text)
    except LookupError as e:
        bad("not_found", f"We couldn't find \"{text}\". {e}".strip())
    if f is None or f.get("geometry") is None:
        bad("not_found", f"We couldn't find a property lot at \"{text}\".")
    zp = zp if zp is not None else bulk.zip_polygon(zip_code)
    if not zp.contains(f["geometry"].representative_point()):
        bad("outside_zip", f"{label or text} isn't in ZIP {zip_code}. Pick an address inside this ZIP.")
    m = measurer_for(cid)
    pid = counties.make_pid(cid, m.parcel_attrs(f["attributes"])["pin"])
    row = None
    if dataset_ready(zip_code, classes):  # measured already: use the sheet
        row = next((r for r in read_dataset(zip_code, classes) if r.get("pin") == pid), None)
    if row is None:
        row = _measured_row(cid, f)
    if row is None:
        bad("no_measurements", f"We don't have measurements for {label or text} (no house found on the lot, "
                               f"or the aerial data doesn't cover it yet). Try another address.")
    return {"pid": pid, "address": row.get("address") or label or text, "row": row}


def _size(row_or_feat) -> float:
    if isinstance(row_or_feat, dict) and "attributes" not in row_or_feat:
        try:
            return float(row_or_feat.get("lot_sqft") or 0)
        except ValueError:
            return 0.0
    return float(row_or_feat[1]["geometry"].area)


def _by_size_buckets(pool: list, k: int) -> list[list]:
    """The pool sorted by size and cut into k equal buckets (small .. large),
    each ordered middle-out, so one pick per bucket gives a mix of sizes."""
    pool = sorted(pool, key=_size)
    n = len(pool)
    out = []
    for i in range(k):
        b = pool[i * n // k:(i + 1) * n // k]
        mid = len(b) // 2
        out.append(sorted(b, key=lambda x, b=b: abs(b.index(x) - mid)))
    return out


def take_preview(email: str, ip: str | None, zip_code: str, classes: list[str], addresses: list[str], resolve) -> dict:
    """The free preview: the 5 addresses the contractor already services (each
    checked: found, in the ZIP, measurable, all different) plus 10 more measured
    homes from the ZIP, spread across small to large lots, never repeating any
    of the 5. Nothing is recorded unless all 15 are ready."""
    if len(addresses) != PREVIEW_CHOSEN:
        raise PreviewProblem([{"index": None, "address": "", "problem": "count",
                               "message": f"Choose {PREVIEW_CHOSEN} addresses you already service."}])
    zp = bulk.zip_polygon(zip_code)
    problems, chosen, seen_text, seen_pid = [], [], {}, {}
    for i, text in enumerate(addresses):
        key = _norm_addr(text)
        if key and key in seen_text:
            problems.append({"index": i, "address": text, "problem": "duplicate",
                             "message": f"Address {i + 1} is the same as address {seen_text[key] + 1}. Pick {PREVIEW_CHOSEN} different homes."})
            continue
        seen_text[key] = i
        try:
            c = check_preview_address(zip_code, classes, text, resolve, zp)
        except PreviewProblem as e:
            problems += [{**p, "index": i} for p in e.problems]
            continue
        if c["pid"] in seen_pid:  # two spellings of one lot
            problems.append({"index": i, "address": text, "problem": "duplicate",
                             "message": f"Address {i + 1} is the same property as address {seen_pid[c['pid']] + 1}. Pick {PREVIEW_CHOSEN} different homes."})
            continue
        seen_pid[c["pid"]] = i
        chosen.append(c)
    if problems:
        raise PreviewProblem(problems)
    mine = {c["pid"] for c in chosen}
    mine_addr = {_norm_addr(c["address"]) for c in chosen}
    yours = [{**c["row"], "group": "yours"} for c in chosen]
    need = PREVIEW_TOTAL - PREVIEW_CHOSEN
    more: list[dict] = []
    if dataset_ready(zip_code, classes):
        pool = [r for r in read_dataset(zip_code, classes)
                if r.get("pin") not in mine and _norm_addr(r.get("address")) not in mine_addr and _has_house(r)]
        if len(pool) < need:
            raise PreviewShort(zip_code, len(pool) + PREVIEW_CHOSEN)
        more = [b[0] for b in _by_size_buckets(pool, need) if b]
    else:
        items = bulk._zip_parcels(zip_code, classes)

        def ok(it):
            m_ = measurer_for(it[0])
            a = m_.parcel_attrs(it[1]["attributes"])
            return (counties.make_pid(it[0], a["pin"]) not in mine and (a.get("address") or "").strip()
                    and _norm_addr(a.get("address")) not in mine_addr)
        items = [it for it in items if ok(it)]
        if len(items) < need:
            raise PreviewShort(zip_code, len(items) + PREVIEW_CHOSEN)
        buckets = _by_size_buckets(items, need)
        got: dict[int, dict] = {}
        tried = [0] * need
        with ThreadPoolExecutor(max_workers=8) as ex:
            for _round in range(4):  # a lot that can't be measured is swapped for its neighbour in size
                todo = [i for i in range(need) if i not in got and tried[i] < len(buckets[i])]
                if not todo:
                    break
                futs = {i: ex.submit(_measured_row, *buckets[i][tried[i]]) for i in todo}
                for i, fu in futs.items():
                    tried[i] += 1
                    r = fu.result()
                    if r and r.get("pin") not in mine:
                        got[i] = r
        more = [got[i] for i in sorted(got)]
        if len(more) < need:  # fill from any bucket that still has untried homes
            rest = [it for i in range(need) for it in buckets[i][tried[i]:]]
            for it in rest:
                if len(more) >= need:
                    break
                r = _measured_row(*it)
                if r and r.get("pin") not in mine and r.get("pin") not in {x.get("pin") for x in more}:
                    more.append(r)
        if len(more) < need:
            raise PreviewShort(zip_code, len(more) + PREVIEW_CHOSEN)
    more = [{**r, "group": "more"} for r in more[:need]]
    rows = yours + more
    assert len({r.get("pin") for r in rows}) == PREVIEW_TOTAL, "preview rows must be distinct"
    with _lock, _db() as con:
        con.execute("INSERT INTO samples(email,ip,zip,ts) VALUES(?,?,?,?)", (email.lower(), ip, zip_code, time.time()))
    from . import mailer
    mailer.notify_owner(f"Free preview: {email} took ZIP {zip_code}",
                        f"{email} previewed 15 homes in ZIP {zip_code}: their 5 addresses plus 10 more.\n"
                        f"Their addresses: " + "; ".join(r.get("address") or "" for r in yours) + "\n"
                        f"A good time to follow up.", reply_to=email)
    return {"yours": yours, "more": more, "rows": rows}


# ----------------------------------------------------------- full datasets
def ensure_dataset(zip_code: str, classes: list[str], on_ready=None) -> str:
    """Start measuring a ZIP sheet unless it's ready or already running.
    Returns 'ready' or 'building'."""
    key = _key(zip_code, classes)
    if dataset_ready(zip_code, classes):
        if on_ready:
            on_ready()
        return "ready"
    with _lock:
        if key in _building:
            if on_ready:
                _building[key]["callbacks"].append(on_ready)
            return "building"
        _building[key] = {"callbacks": [on_ready] if on_ready else []}
    meta_p = ZIPS_DIR / f"{key}.json"

    def done(job):
        ok = job.get("status") == "done"
        if ok:
            # keep only the sale columns, drop rows that failed to measure
            src = bulk.csv_path(job["id"])
            with open(src, newline="") as fh:
                rows = [r for r in csv.DictReader(fh)
                        if r.get("lawn_sqft") not in (None, "") and (r.get("address") or "").strip()
                        and _has_house(r)]
            tmp = dataset_path(zip_code, classes).with_suffix(".tmp")
            with open(tmp, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=[k for k, _ in COLUMNS], extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(_bulk_row_to_sale(r))
            shutil.move(tmp, dataset_path(zip_code, classes))
        meta_p.write_text(json.dumps({"status": "ready" if ok else "failed", "job": job["id"],
                                      "count": job.get("done"), "errors": job.get("errors"),
                                      "skipped": job.get("skipped") or [], "finished": time.time()}))
        with _lock:
            cbs = _building.pop(key, {}).get("callbacks", [])
        if not ok:
            # orders stay "processing"; the owner hears about it and can re-run
            from . import mailer
            mailer.notify_owner(f"Measuring ZIP {zip_code} failed",
                                f"The sheet for ZIP {zip_code} ({key}) failed to build: {job.get('error') or job.get('status')}.\n"
                                f"{len(cbs)} paid order(s) are waiting on it. Re-run it from Business > Data sales.")
            return
        for cb in cbs:
            try:
                cb()
            except Exception:
                pass

    job = bulk.start_job(zip_code, classes, list(MEASUREMENTS), None, False, workers=8, on_done=done)
    meta_p.write_text(json.dumps({"status": "building", "job": job["id"], "started": time.time()}))
    return "building"


def _has_house(r: dict) -> bool:
    """Sell homes, not empty lots: a building on the photo or living area on the record."""
    def num(k):
        try:
            return float(r.get(k) or 0)
        except ValueError:
            return 0.0
    return num("building_sqft") >= 200 or num("living_sqft") > 0


def dataset_progress(zip_code: str, classes: list[str]) -> dict:
    meta = dataset_meta(zip_code, classes) or {}
    if meta.get("status") == "building" and meta.get("job"):
        j = bulk.get_job(meta["job"]) or {}
        return {"status": "building", "done": j.get("done", 0), "total": j.get("total", 0),
                "stage": j.get("status")}
    return {"status": meta.get("status", "none")}


# ------------------------------------------------------------------- orders
def create_order(email: str, zip_code: str, classes: list[str], types=None, site: str = "",
                 product: str = "standard", member: bool = False, lo: int | None = None, hi: int | None = None,
                 account_email: str | None = None, notify: bool = True) -> dict:
    """``account_email`` is the signed-in company's email: lock-holder rights
    (buying in their own exclusive ZIP) come from it, never from a typed email."""
    types = normalize_types(types)
    product = product if product in ("premier", "exclusive") else "standard"
    holder = (account_email or "").lower() or None
    if product == "exclusive":
        if not holder:
            raise PermissionError("Sign in to buy an exclusive ZIP.")
        email = holder  # the lock belongs to the account
    q = quote(zip_code, classes, types, product, member, lo, hi, email=holder)
    if not q["count"]:
        raise LookupError("No matching properties in that ZIP code in our service area.")
    if q["blocked"]:
        raise PermissionError(q["blocked"])
    new_types = None
    if product == "exclusive":
        if not q["new_types"]:
            raise PermissionError("You already hold every one of those measurements in this ZIP.")
        new_types = ",".join(q["new_types"])
        types = normalize_types(types + held_types(zip_code, holder))  # deliver everything they hold
    oid = secrets.token_hex(4).upper()
    tok = secrets.token_urlsafe(16)
    with _lock, _db() as con:
        con.execute("INSERT INTO orders(id,email,zip,classes,count,amount_cents,status,token,created,types,site,product,"
                    "value_lo,value_hi,new_types) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, email.lower(), zip_code, ",".join(classes), q["count"], q["price_cents"],
                     "awaiting_payment", tok, time.time(), ",".join(types), site, product, q["value_lo"], q["value_hi"],
                     new_types))
    o = get_order(oid)
    if notify:
        _email_new_order(o)
    return o


def order_bands(o: dict) -> tuple[int, int]:
    return band_range(o.get("value_lo"), o.get("value_hi"))


def rows_in_bands(rows: list[dict], lo: int, hi: int) -> list[dict]:
    """Rows whose assessed value falls in the bought band range."""
    lo, hi = band_range(lo, hi)
    from .bulk import VALUE_EDGES, band_of
    if lo == 0 and hi == len(VALUE_EDGES):
        return rows
    return [r for r in rows if lo <= band_of(r.get("home_value")) <= hi]


def order_types(o: dict) -> list[str]:
    return normalize_types((o.get("types") or "lawn").split(","))


def order_what(o: dict) -> str:
    prod = o.get("product") or "standard"
    if prod == "premier":
        return "Premier list: full property report PDF for every home"
    what = ", ".join(type_labels(order_types(o)))
    if prod == "exclusive":
        months = int(pricing().get("exclusive_months", 12))
        what = f"Exclusive ZIP ({months} months, no one else can buy these here): {what}"
    return what


def type_labels(types) -> list[str]:
    lab = {k: l for k, l, _ in MEASURE_TYPES}
    return [lab[t] for t in normalize_types(types)]


def order_link(o: dict) -> str:
    from . import mailer
    return f"{mailer.site_url(o.get('site') or '')}/data/order/{o['id']}?t={o['token']}"


def _email_new_order(o: dict):
    from . import mailer
    usd = f"${o['amount_cents'] / 100:,.2f}"
    what = order_what(o)
    what += f"\nHomes: {range_label(*order_bands(o))}"
    mailer.notify_owner(f"New data order {o['id']}: ZIP {o['zip']}, {usd}",
                        f"Order {o['id']}\nBuyer: {o['email']}\nZIP {o['zip']}: {o['count']:,} properties\n"
                        f"Measurements: {what}\nAmount: {usd}\nStatus: waiting for payment\n\n{order_link(o)}",
                        reply_to=o["email"])
    mailer.send(o["email"], f"Your order {o['id']}: ZIP {o['zip']} property sheet",
                f"Thanks for your order.\n\nZIP {o['zip']}: {o['count']:,} properties\nMeasurements: {what}\n"
                f"Total: {usd}\n\nYour order page (keep this link, it's how you get your download):\n{order_link(o)}\n")


def _email_ready(oid: str):
    from . import mailer
    o = get_order(oid)
    if not o:
        return
    mailer.send(o["email"], f"Your ZIP {o['zip']} property sheet is ready",
                f"Your sheet for ZIP {o['zip']} ({o['count']:,} properties) is ready to download:\n\n"
                f"{order_link(o)}\n\nOne row per property. Measurements are estimates from aerial photos "
                f"and LiDAR; verify on site before final billing.\n")
    mailer.notify_owner(f"Order {o['id']} delivered: ZIP {o['zip']}", f"Sheet ready for {o['email']}.\n{order_link(o)}")


def get_order(oid: str) -> dict | None:
    with _db() as con:
        r = con.execute("SELECT * FROM orders WHERE id=?", (oid.upper(),)).fetchone()
    return dict(r) if r else None


def list_orders(limit: int = 300) -> list[dict]:
    with _db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM orders ORDER BY created DESC LIMIT ?", (limit,))]


def _set(oid: str, **fields):
    with _lock, _db() as con:
        con.execute(f"UPDATE orders SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), oid))


def mark_paid(oid: str, note: str = "") -> dict:
    o = get_order(oid)
    if not o:
        raise LookupError("order not found")
    if o["status"] in ("awaiting_payment",):
        from . import mailer
        if (o.get("product") or "") == "exclusive":
            clash = _grant_locks(o)
            if clash:
                # someone else's exclusive was paid first: refund this one
                _set(oid, status="conflict", paid=time.time(), note=f"{note}; taken: {','.join(clash)}")
                mailer.notify_owner(f"REFUND NEEDED: order {oid}, ZIP {o['zip']}",
                                    f"{o['email']} paid ${o['amount_cents'] / 100:,.2f} for an exclusive on ZIP {o['zip']}, "
                                    f"but another company locked {', '.join(type_labels(clash))} first. Refund it in Stripe.")
                mailer.send(o["email"], f"Order {oid}: ZIP {o['zip']} was just taken",
                            f"Another company locked {', '.join(type_labels(clash))} in ZIP {o['zip']} moments before "
                            f"your payment went through. You'll get a full refund; we'll be in touch.")
                return get_order(oid)
        _set(oid, status="processing", paid=time.time(), note=note)
        mailer.notify_owner(f"Paid: order {oid}, ZIP {o['zip']} (${o['amount_cents'] / 100:,.2f})",
                            f"{o['email']} paid for ZIP {o['zip']} ({note or 'payment'}).\n{order_link(o)}")
        classes = o["classes"].split(",") if o["classes"] else []

        def ready():
            _set(oid, status="ready")
            _email_ready(oid)
        if (o.get("product") or "standard") == "premier":
            from . import premier
            state = premier.ensure(o["zip"], classes, on_ready=ready, bands=order_bands(o))
        else:
            state = ensure_dataset(o["zip"], classes, on_ready=ready)
        if state == "ready":
            # ensure_dataset already ran the callback for a ready sheet
            pass
    return get_order(oid)


def order_view(o: dict) -> dict:
    classes = o["classes"].split(",") if o["classes"] else []
    v = {k: o[k] for k in ("id", "zip", "classes", "count", "amount_cents", "status", "created", "paid")}
    v["email"] = o["email"]
    v["product"] = o.get("product") or "standard"
    v["value_label"] = range_label(*order_bands(o))
    v["types"] = order_types(o)
    v["type_labels"] = (["Full property report PDF for every home (roof, walls & elevations, lot & yard)"]
                        if v["product"] == "premier" else type_labels(v["types"]))
    if v["product"] == "exclusive":
        lk = locks(o["zip"])
        v["exclusive_until"] = max([l["until"] for t, l in lk.items() if l["email"] == o["email"].lower()] or [0]) or None
    if o["status"] == "processing":
        if v["product"] == "premier":
            from . import premier
            bands = order_bands(o)
            is_ready, prog = premier.ready(o["zip"], classes, bands), premier.progress(o["zip"], classes, bands)
        else:
            is_ready, prog = dataset_ready(o["zip"], classes), dataset_progress(o["zip"], classes)
        if is_ready:
            _set(o["id"], status="ready")
            v["status"] = "ready"
        else:
            v["progress"] = prog
    return v


# ------------------------------------------------------------------ Stripe
def stripe_enabled() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def stripe_checkout(o: dict, base_url: str) -> str:
    """Create a Stripe Checkout session and return its URL."""
    key = os.environ["STRIPE_SECRET_KEY"]
    biz = load_settings()["business"].get("name") or "Property data"
    data = {
        "mode": "payment",
        "customer_email": o["email"],
        "client_reference_id": o["id"],
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(o["amount_cents"]),
        "line_items[0][price_data][product_data][name]": f"Property measurements: ZIP {o['zip']} ({o['count']:,} properties)",
        "line_items[0][price_data][product_data][description]": f"{biz}: " + (
            order_what(o)),
        "success_url": f"{base_url}/data/order/{o['id']}?t={o['token']}&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/data?zip={o['zip']}",
        "metadata[order_id]": o["id"],
    }
    r = httpx.post("https://api.stripe.com/v1/checkout/sessions", data=data, auth=(key, ""), timeout=30)
    r.raise_for_status()
    sess = r.json()
    _set(o["id"], stripe_session=sess["id"])
    return sess["url"]


def stripe_confirm(o: dict, session_id: str) -> bool:
    """Check with Stripe that this order's session is paid; if so, mark it."""
    if not stripe_enabled() or not session_id or session_id != o.get("stripe_session"):
        return False
    r = httpx.get(f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
                  auth=(os.environ["STRIPE_SECRET_KEY"], ""), timeout=30)
    if r.status_code != 200:
        return False
    s = r.json()
    if s.get("payment_status") == "paid" and s.get("client_reference_id") == o["id"] \
            and int(s.get("amount_total") or 0) >= int(o["amount_cents"]):
        mark_paid(o["id"], note="stripe")
        return True
    return False


# ------------------------------------------------- measure every ZIP ahead
PREBUILD_STATE = DATA_DIR / "prebuild_all.json"
_pre = {"thread": None, "stop": False}


_svc_cache: dict = {}


def service_zips() -> list[str]:
    """ZIPs mostly (>= 20%) inside the counties we cover, smallest first.
    Asked county by county: one box around counties in several states is too
    big for the Census server."""
    from shapely.geometry import shape
    from .gis import _esri_to_geojson_geom, get_json
    bounds = counties.boundaries()
    key = tuple(sorted(bounds))
    if _svc_cache.get("key") == key:
        return list(_svc_cache["zips"])
    inside: dict = {}
    for cid, cg in bounds.items():
        try:
            d = get_json(bulk._ZCTA + "/query", {"where": "1=1", "geometry": ",".join(f"{v:.4f}" for v in cg.bounds),
                                                  "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                                                  "spatialRel": "esriSpatialRelIntersects", "outFields": "ZCTA5",
                                                  "outSR": 4326, "geometryPrecision": 5, "f": "json"}, ttl=30 * 86400)
        except Exception:
            continue  # one slow county shouldn't hide the rest
        for f in d.get("features") or []:
            gj = _esri_to_geojson_geom(f["geometry"])
            if not gj:
                continue
            g = shape(gj)
            if not g.area or not g.intersects(cg):
                continue
            z = f["attributes"]["ZCTA5"]
            area, got = inside.get(z, (g.area, 0.0))
            inside[z] = (area, got + g.intersection(cg).area)
    out = sorted((area, z) for z, (area, got) in inside.items() if got / area >= 0.2)
    zips = [z for _, z in out]
    _svc_cache.update(key=key, zips=zips)
    return list(zips)


def queue_order(zips: list[str], fetch: bool = True) -> list[str]:
    """ZIPs nearest home first (Omaha outward). ZIPs whose location isn't
    known yet go last, in the order given."""
    from . import coverage
    from .coverage import miles_from_home
    first = {}
    for m in coverage.metros():  # ZIPs of the priority metros go first, in their order
        if m["cbsa"] in coverage.PRIORITY and isinstance(m.get("zips"), list):
            for z in m["zips"]:
                first.setdefault(z, coverage.PRIORITY.index(m["cbsa"]))
    try:
        pts = zip_points(list(zips)) if fetch else (json.loads(_ZIP_POINTS.read_text()) if _ZIP_POINTS.exists() else {})
    except Exception:
        pts = json.loads(_ZIP_POINTS.read_text()) if _ZIP_POINTS.exists() else {}
    idx = {z: i for i, z in enumerate(zips)}
    def key(z):
        d = miles_from_home(*pts[z]) if z in pts else 1e9
        grp = first.get(z, 0 if d <= 35 else 99)  # home's own metro always leads
        return (grp, d, idx[z])
    return sorted(zips, key=key)


def prebuild_status() -> dict:
    st = json.loads(PREBUILD_STATE.read_text()) if PREBUILD_STATE.exists() else {"status": "idle"}
    st["running"] = bool(_pre["thread"] and _pre["thread"].is_alive())
    if st.get("current"):
        st["current_progress"] = dataset_progress(st["current"], st.get("classes") or ["R"])
    return st


def _save_pre(st: dict):
    PREBUILD_STATE.write_text(json.dumps(st))


def prebuild_all(classes: list[str] | None = None, start: bool = True, zips: list[str] | None = None) -> dict:
    """Measure every ZIP in the service area (or the ``zips`` given, e.g. a
    new metro), one at a time, so lists are ready to download the moment
    someone buys. Skips ZIPs already measured in the last year; resumes
    after a restart. ZIPs added while a run is going join its queue."""
    if not start:
        _pre["stop"] = True
        if not (_pre["thread"] and _pre["thread"].is_alive()) and PREBUILD_STATE.exists():
            st = json.loads(PREBUILD_STATE.read_text())
            if st.get("status") == "running":
                st.update(status="stopped", current=None); _save_pre(st)
        return prebuild_status()
    if _pre["thread"] and _pre["thread"].is_alive():
        st = _pre.get("st")
        if zips and st is not None:  # join the running queue
            for z in zips:
                if z not in st["zips"]:
                    st["zips"].append(z)
            _save_pre(st)
        return prebuild_status()
    classes = classes or ["R"]
    zips = list(dict.fromkeys((zips or []) + service_zips())) if zips else service_zips()
    st = {"status": "running", "classes": classes, "zips": zips, "done": [], "failed": [],
          "started": time.time(), "current": None}
    _pre["st"] = st
    if PREBUILD_STATE.exists():  # keep what an earlier run finished
        old = json.loads(PREBUILD_STATE.read_text())
        st["done"] = [z for z in old.get("done", []) if z in zips]
    _save_pre(st)
    _pre["stop"] = False

    def run():
        while True:  # the list can grow while running; always take the nearest ZIP left
            if _pre["stop"]:
                st.update(status="stopped", current=None); _save_pre(st)
                return
            left = queue_order([z for z in st["zips"] if z not in st["done"] and z not in st["failed"]])
            if not left:
                break
            z = left[0]
            st["current"] = z; _save_pre(st)
            ev = threading.Event()
            try:
                state = ensure_dataset(z, classes, on_ready=ev.set)
                if state != "ready":
                    while not ev.wait(30):
                        m = dataset_meta(z, classes) or {}
                        if m.get("status") == "failed" or _pre["stop"]:
                            break
                ok = dataset_ready(z, classes)
            except Exception:
                ok = False
            (st["done"] if ok else st["failed"]).append(z)
            _save_pre(st)
        st.update(status="finished", current=None, finished=time.time()); _save_pre(st)

    _pre["thread"] = threading.Thread(target=run, daemon=True, name="prebuild-all")
    _pre["thread"].start()
    return prebuild_status()


def resume_prebuild():
    """Called at startup: carry on a run a deploy/restart interrupted."""
    try:
        if PREBUILD_STATE.exists() and json.loads(PREBUILD_STATE.read_text()).get("status") == "running":
            st = json.loads(PREBUILD_STATE.read_text())
            prebuild_all(st.get("classes"), zips=st.get("zips"))
    except Exception:
        pass


init()


# ------------------------------------------------------- totals (owner)
TOTAL_KEYS = [("home_value", "Assessed value $"), ("lot_sqft", "Lot"), ("lawn_sqft", "Lawn"), ("parkway_sqft", "Parkway"), ("driveway_sqft", "Driveway"),
              ("sidewalk_sqft", "Sidewalk"), ("other_hardscape_sqft", "Patios & walks"), ("beds_sqft", "Beds"),
              ("building_sqft", "Building footprint"), ("wall_sqft", "House walls"),
              ("living_sqft", "House size (assessor)")]
_totals_cache: dict = {}


def totals_for_key(key: str) -> dict | None:
    """Square feet measured in one stored ZIP sheet, in total and per
    assessed-value band ($250k steps). Cached until the sheet changes."""
    from .bulk import VALUE_EDGES, band_label, band_of
    p = ZIPS_DIR / f"{key}.csv"
    if not p.exists():
        return None
    mt = p.stat().st_mtime
    hit = _totals_cache.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    bands = {i: {"band": i, "label": band_label(i), "homes": 0, **{k: 0.0 for k, _ in TOTAL_KEYS}}
             for i in range(len(VALUE_EDGES) + 1)}
    with open(p, newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("lawn_sqft") in (None, "") or not (r.get("address") or "").strip():
                continue
            b = bands[band_of(r.get("home_value"))]
            b["homes"] += 1
            for k, _ in TOTAL_KEYS:
                try:
                    b[k] += float(r.get(k) or 0)
                except ValueError:
                    pass
    rows = [b for b in bands.values() if b["homes"]]
    allrow = {"band": None, "label": "All homes", "homes": sum(b["homes"] for b in rows),
              **{k: sum(b[k] for b in rows) for k, _ in TOTAL_KEYS}}
    for b in rows + [allrow]:
        for k, _ in TOTAL_KEYS:
            b[k] = round(b[k])
    out = {"key": key, "zip": key.split("_")[0], "bands": rows, "all": allrow,
           "columns": [{"key": k, "label": l} for k, l in TOTAL_KEYS]}
    _totals_cache[key] = (mt, out)
    return out


def totals_all() -> dict:
    """Every stored ZIP's totals, plus a grand total across them."""
    zips = []
    for m in sorted(ZIPS_DIR.glob("*.json")):
        try:
            if json.loads(m.read_text()).get("status") != "ready":
                continue
        except ValueError:
            continue
        t = totals_for_key(m.stem)
        if t:
            zips.append({"key": t["key"], "zip": t["zip"], **{k: v for k, v in t["all"].items() if k not in ("band", "label")}})
    grand = {"homes": sum(z["homes"] for z in zips), **{k: sum(z[k] for z in zips) for k, _ in TOTAL_KEYS}}
    return {"zips": zips, "all": grand, "columns": [{"key": k, "label": l} for k, l in TOTAL_KEYS]}


# ------------------------------------------------------ several ZIPs at once
MAX_ZIPS_PER_ORDER = 25


def create_group(email: str, zips: list[str], classes: list[str], types=None, site: str = "",
                 product: str = "standard", member: bool = False, lo: int | None = None, hi: int | None = None,
                 account_email: str | None = None) -> dict:
    """Buy several ZIPs together: one order per ZIP (each with its own
    download page and, for exclusives, its own lock), paid in one checkout.
    Every ZIP is checked before any order is made."""
    zips = list(dict.fromkeys(z for z in zips if z))
    if not zips:
        raise LookupError("Add at least one ZIP.")
    if len(zips) > MAX_ZIPS_PER_ORDER:
        raise PermissionError(f"Up to {MAX_ZIPS_PER_ORDER} ZIPs per order.")
    holder = (account_email or "").lower() or None
    for z in zips:  # nothing is created if any ZIP can't be sold
        q = quote(z, classes, normalize_types(types), product, member, lo, hi, email=holder)
        if not q["count"]:
            raise LookupError(f"ZIP {z}: no matching properties in our service area.")
        if q["blocked"]:
            raise PermissionError(f"ZIP {z}: {q['blocked']}")
    grp, gtok = secrets.token_hex(4).upper(), secrets.token_urlsafe(16)
    orders = [create_order(email, z, classes, types, site, product, member, lo, hi, account_email=account_email,
                           notify=False) for z in zips]
    with _lock, _db() as con:
        con.executemany("UPDATE orders SET grp=?, grp_token=? WHERE id=?", [(grp, gtok, o["id"]) for o in orders])
    orders = [get_order(o["id"]) for o in orders]
    _email_new_group(grp, gtok, orders, site)
    return {"grp": grp, "token": gtok, "orders": orders}


def _email_new_group(grp: str, gtok: str, orders: list[dict], site: str = ""):
    from . import mailer
    total = sum(o["amount_cents"] for o in orders)
    homes = sum(o["count"] or 0 for o in orders)
    lines = "\n".join(f"  ZIP {o['zip']}: {o['count']:,} properties, ${o['amount_cents'] / 100:,.2f}" for o in orders)
    link = f"{mailer.site_url(site)}/data/orders/{grp}?t={gtok}"
    what = order_what(orders[0]) + f"\nHomes: {range_label(*order_bands(orders[0]))}"
    mailer.notify_owner(f"New data order {grp}: {len(orders)} ZIPs, ${total / 100:,.2f}",
                        f"Buyer: {orders[0]['email']}\n{lines}\nMeasurements: {what}\nTotal: ${total / 100:,.2f}\n\n{link}",
                        reply_to=orders[0]["email"])
    mailer.send(orders[0]["email"], f"Your order {grp}: {len(orders)} ZIP codes",
                f"Thanks for your order.\n\n{lines}\n\n{homes:,} properties in all. Measurements: {what}\n"
                f"Total: ${total / 100:,.2f}\n\nYour order page (keep this link, it's how you get your downloads):\n{link}\n")


def group_orders(grp: str) -> list[dict]:
    with _db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM orders WHERE grp=? ORDER BY zip", (grp.upper(),))]


def stripe_checkout_group(grp: str, base_url: str) -> str:
    """One Stripe Checkout for every unpaid ZIP in the group (a line per ZIP)."""
    orders = [o for o in group_orders(grp) if o["status"] == "awaiting_payment"]
    if not orders:
        raise LookupError("nothing to pay")
    key = os.environ["STRIPE_SECRET_KEY"]
    tok = orders[0]["grp_token"]
    data = {"mode": "payment", "customer_email": orders[0]["email"], "client_reference_id": f"G-{grp}",
            "success_url": f"{base_url}/data/orders/{grp}?t={tok}&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base_url}/data", "metadata[group_id]": grp}
    for i, o in enumerate(orders):
        data.update({f"line_items[{i}][quantity]": "1", f"line_items[{i}][price_data][currency]": "usd",
                     f"line_items[{i}][price_data][unit_amount]": str(o["amount_cents"]),
                     f"line_items[{i}][price_data][product_data][name]": f"ZIP {o['zip']}: {o['count']:,} properties",
                     f"line_items[{i}][price_data][product_data][description]": order_what(o)})
    r = httpx.post("https://api.stripe.com/v1/checkout/sessions", data=data, auth=(key, ""), timeout=30)
    r.raise_for_status()
    sess = r.json()
    with _lock, _db() as con:
        con.execute("UPDATE orders SET stripe_session=? WHERE grp=?", (sess["id"], grp))
    return sess["url"]


def group_paid(grp: str, amount_total: int, note: str = "stripe") -> bool:
    """Mark every ZIP in the group paid once Stripe confirms the full amount."""
    orders = group_orders(grp)
    due = sum(o["amount_cents"] for o in orders if o["status"] == "awaiting_payment")
    if not orders or int(amount_total) < due:
        return False
    for o in orders:
        if o["status"] == "awaiting_payment":
            mark_paid(o["id"], note=note)
    return True


def stripe_confirm_group(grp: str, session_id: str) -> bool:
    orders = group_orders(grp)
    if not stripe_enabled() or not orders or not session_id or session_id != orders[0].get("stripe_session"):
        return False
    r = httpx.get(f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
                  auth=(os.environ["STRIPE_SECRET_KEY"], ""), timeout=30)
    if r.status_code != 200:
        return False
    s_ = r.json()
    if s_.get("payment_status") == "paid" and (s_.get("metadata") or {}).get("group_id") == grp:
        return group_paid(grp, int(s_.get("amount_total") or 0))
    return False


# ------------------------------------------------------ owner ZIP heat map
_ZIP_SHAPES = DATA_DIR / "zip_shapes.json"
_shape_lock = threading.Lock()


def zip_shapes(zips: list[str]) -> dict:
    """{zip: simplified GeoJSON geometry}, fetched from Census in batches and
    kept on disk (ZIP outlines rarely change)."""
    from .gis import _esri_to_geojson_geom, get_json
    with _shape_lock:
        have = json.loads(_ZIP_SHAPES.read_text()) if _ZIP_SHAPES.exists() else {}
        need = [z for z in zips if z not in have]
        for i in range(0, len(need), 40):
            chunk = need[i:i + 40]
            d = get_json(bulk._ZCTA + "/query", {"where": "ZCTA5 IN (" + ",".join(f"'{z}'" for z in chunk) + ")",
                                                  "outFields": "ZCTA5", "outSR": 4326, "geometryPrecision": 4,
                                                  "maxAllowableOffset": 0.0008, "f": "json"}, ttl=365 * 86400)
            for f in d.get("features") or []:
                g = _esri_to_geojson_geom(f["geometry"])
                if g:
                    have[f["attributes"]["ZCTA5"]] = g
        if need:
            _ZIP_SHAPES.write_text(json.dumps(have))
    return {z: have[z] for z in zips if z in have}


_ZIP_POINTS = DATA_DIR / "zip_points.json"
_heat: dict = {"zips": None, "at": 0, "thread": None, "error": None}


def zip_points(zips: list[str]) -> dict:
    """{zip: [lat, lon]} from Census (no outlines, so it's quick), kept on disk."""
    from .gis import get_json
    have = json.loads(_ZIP_POINTS.read_text()) if _ZIP_POINTS.exists() else {}
    need = [z for z in zips if z not in have]
    for i in range(0, len(need), 150):
        chunk = need[i:i + 150]
        d = get_json(bulk._ZCTA + "/query", {"where": "ZCTA5 IN (" + ",".join(f"'{z}'" for z in chunk) + ")",
                                              "outFields": "ZCTA5,INTPTLAT,INTPTLON", "returnGeometry": "false",
                                              "f": "json"}, ttl=365 * 86400)
        for f in d.get("features") or []:
            a = f["attributes"]
            have[a["ZCTA5"]] = [round(float(a["INTPTLAT"]), 5), round(float(a["INTPTLON"]), 5)]
        _ZIP_POINTS.write_text(json.dumps(have))  # save as we go: a later chunk can fail
    return {z: have[z] for z in zips if z in have}


def _covered_zips() -> set:
    from . import coverage
    zips: set = set()
    try:
        zips |= set(service_zips())
    except Exception as e:
        _heat["error"] = str(e)[:200]
    for m in coverage.metros():
        if m["status"] == "live" and isinstance(m.get("zips"), list):
            zips |= set(m["zips"])
    return zips


def _refresh_heat() -> None:
    try:
        zips = _covered_zips()
        _heat["zips"] = sorted(zips)
        zip_points(sorted(zips | {o["zip"] for o in list_orders(100000)}))
        _heat["at"] = time.time()
    except Exception as e:
        _heat["error"] = str(e)[:200]
        _heat["at"] = time.time() - 240  # try again in about a minute


def zip_heat() -> dict:
    """Every ZIP we cover, as a dot, with how often it's been bought, revenue,
    whether it's measured and whether it's locked. The ZIP list and locations
    refresh in the background so the map always answers at once."""
    t = _heat["thread"]
    if (time.time() - _heat["at"] > 300) and not (t and t.is_alive()):
        _heat["thread"] = threading.Thread(target=_refresh_heat, daemon=True)
        _heat["thread"].start()
        _heat["thread"].join(8)  # small setups finish right away
    stats: dict = {}
    for o in list_orders(100000):
        z = o["zip"]
        s = stats.setdefault(z, {"orders": 0, "paid": 0, "cents": 0, "pending": 0, "last": 0})
        s["orders"] += 1
        if o["status"] in ("processing", "ready"):
            s["paid"] += 1
            s["cents"] += o["amount_cents"] or 0
            s["last"] = max(s["last"], o.get("paid") or o["created"])
        elif o["status"] == "awaiting_payment":
            s["pending"] += 1
    measured = set()
    for p in ZIPS_DIR.glob("*.json"):
        try:
            if (json.loads(p.read_text()) or {}).get("status") == "ready":
                measured.add(p.stem.split("_")[0])
        except Exception:
            pass
    with _db() as con:
        locked = {r[0] for r in con.execute("SELECT DISTINCT zip FROM zip_locks WHERE until>?", (time.time(),))}
    zips = set(_heat["zips"] or []) | set(stats) | measured
    pts = json.loads(_ZIP_POINTS.read_text()) if _ZIP_POINTS.exists() else {}
    blank = {"orders": 0, "paid": 0, "cents": 0, "pending": 0, "last": 0}
    rows = [{"zip": z, "lat": pts[z][0], "lon": pts[z][1], **stats.get(z, blank),
             "measured": z in measured, "locked": z in locked} for z in sorted(zips) if z in pts]
    building = bool(_heat["thread"] and _heat["thread"].is_alive())
    return {"zips": rows, "building": building, "waiting": len(zips) - len(rows),
            "error": None if building or rows else _heat["error"],
            "totals": {"zips": len(zips), "bought": sum(1 for r in rows if r["paid"]),
                       "measured": sum(1 for r in rows if r["measured"]), "cents": sum(r["cents"] for r in rows)}}


def zip_outlines(zips: list[str]) -> dict:
    """GeoJSON outlines for the ZIPs on screen (at most 400 at a time)."""
    shapes = zip_shapes(sorted(set(zips))[:400])
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": g, "properties": {"zip": z}} for z, g in shapes.items()]}


# ------------------------------------------------------- live status (owner)
def log_download(o: dict, kind: str) -> None:
    try:
        with _db() as con:
            con.execute("INSERT INTO downloads (order_id, zip, email, kind, ts) VALUES (?,?,?,?,?)",
                        (o["id"], o["zip"], o["email"], kind, time.time()))
    except Exception:
        pass  # never block a customer's download over a log line


def recent_downloads(limit: int = 12) -> dict:
    day = time.time() - 86400
    with _db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM downloads ORDER BY ts DESC LIMIT ?", (limit,))]
        today = con.execute("SELECT COUNT(*) FROM downloads WHERE ts>?", (day,)).fetchone()[0]
        week = con.execute("SELECT COUNT(*) FROM downloads WHERE ts>?", (time.time() - 7 * 86400,)).fetchone()[0]
    return {"recent": rows, "today": today, "week": week}


def measuring_now() -> list[dict]:
    """ZIP sheets being measured right now, with speed and time left."""
    out = []
    now = time.time()
    for j in bulk.list_running():
        rate = None
        if j.get("measure_started") and j.get("done"):
            rate = j["done"] / max(1.0, now - j["measure_started"]) * 60  # homes a minute
        left = (j["total"] - j["done"]) / rate * 60 if rate and j.get("total") else None
        out.append({"zip": j["zip"], "stage": j["status"], "done": j["done"], "total": j["total"], "errors": j["errors"],
                    "started": j["started"], "per_min": round(rate, 1) if rate else None,
                    "eta_s": round(left) if left is not None else None})
    return out


_sheets_cache: dict = {"at": 0, "v": None}


def recent_sheets(limit: int = 10) -> dict:
    """Measured ZIP sheets (cached 20 s: the Live screen asks every 5 s)."""
    if _sheets_cache["v"] is not None and time.time() - _sheets_cache["at"] < 20:
        return _sheets_cache["v"]
    v = _recent_sheets(limit)
    _sheets_cache.update(at=time.time(), v=v)
    return v


def _recent_sheets(limit: int = 10) -> dict:
    ready, failed = [], []
    for p in ZIPS_DIR.glob("*.json"):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        row = {"zip": p.stem.split("_")[0], "count": m.get("count"), "errors": m.get("errors"), "finished": m.get("finished"),
               "skipped": m.get("skipped") or []}
        if m.get("status") == "ready":
            ready.append(row)
        elif m.get("status") == "failed":
            failed.append(row)
    ready.sort(key=lambda r: r["finished"] or 0, reverse=True)
    failed.sort(key=lambda r: r["finished"] or 0, reverse=True)
    return {"ready": ready[:limit], "ready_total": len(ready), "homes": sum(r["count"] or 0 for r in ready),
            "failed": failed[:limit], "failed_total": len(failed),
            "today": sum(1 for r in ready if (r["finished"] or 0) > time.time() - 86400)}
