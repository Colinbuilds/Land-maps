"""Sell measured ZIP-code sheets: quotes, free samples, orders, cached datasets.

Measure once, sell many times: each (ZIP, property types) sheet is measured
one time and kept under data/zips/. Every later order and every free sample
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

SAMPLE_SIZE = 25
SAMPLES_PER_IP_PER_DAY = 3
DATASET_MAX_AGE_DAYS = 365

# Columns sold. No owner names or mailing addresses (several states restrict
# their use for solicitation).
COLUMNS = [
    ("address", "Address"), ("city", "City"), ("zip", "ZIP"), ("county", "County"),
    ("lot_sqft", "Lot sq ft"), ("lawn_sqft", "Lawn sq ft (grass only)"),
    ("parkway_sqft", "Parkway grass sq ft"), ("building_sqft", "Building footprint sq ft"),
    ("driveway_sqft", "Driveway sq ft"), ("sidewalk_sqft", "Public sidewalk sq ft"),
    ("other_hardscape_sqft", "Patios & walks sq ft"), ("structures_sqft", "Decks/sheds/play sets sq ft"),
    ("beds_sqft", "Landscape beds sq ft"), ("frontage_ft", "Street frontage ft"),
    ("tree_count", "Trees on lot"), ("tree_canopy_sqft", "Tree canopy sq ft"),
    ("lawn_under_trees_sqft", "Lawn under trees sq ft"),
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
    ("trees", "Trees (count, canopy, lawn under trees)", ["tree_count", "tree_canopy_sqft", "lawn_under_trees_sqft"]),
    ("building", "Building footprint", ["building_sqft"]),
    ("walls", "House walls & windows (siding)", ["wall_sqft", "est_windows"]),
]
TYPE_KEYS = [k for k, _, _ in MEASURE_TYPES]
BASE_HEAD = ["address", "city", "zip", "county", "lot_sqft"]
BASE_TAIL = ["quality", "notes", "lat", "lon", "pin"]

DEFAULT_PRICING = {
    "rate_cents": 10.0,          # per property, for the first measurement type
    "extra_type_cents": 5.0,     # per property, for each additional type
    "premier_cents": 25.0,       # per property, Premier list (full report PDF per home)
    "min_order_usd": 25.0,
}


def normalize_types(types) -> list[str]:
    """Valid types in catalogue order; lawn when none are given."""
    want = {str(t).strip().lower() for t in (types or []) if str(t).strip()}
    out = [k for k in TYPE_KEYS if k in want]
    return out or ["lawn"]


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
    n = len(normalize_types(types))
    return float(p["rate_cents"]) + float(p.get("extra_type_cents", 5.0)) * (n - 1)


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
    return bool(m and m.get("status") == "ready" and dataset_path(zip_code, classes).exists()
                and time.time() - m.get("finished", 0) < DATASET_MAX_AGE_DAYS * 86400)


# ------------------------------------------------------------------- quotes
PREMIER_CENTS_DEFAULT = 25.0


def list_price(count: int, types=None, product: str = "standard", member: bool = False, p: dict | None = None) -> dict:
    """Price of a ZIP list. Standard: per-measurement pricing. Premier: a full
    property report PDF for every home. Members pay half."""
    p = p or pricing()
    if product == "premier":
        per = float(p.get("premier_cents", PREMIER_CENTS_DEFAULT))
        cents = int(max(math.ceil(count * per - 1e-9), round(float(p["min_order_usd"]) * 100)))
    else:
        per = per_property_cents(types, p)
        cents = price_cents(count, types, p)
    full = cents
    if member:
        from .billing import plans
        cents = int(round(cents * (1 - float(plans()["list_discount"]))))
    return {"per_property_cents": per * (0.5 if member else 1), "price_cents": cents, "full_price_cents": full,
            "member_discount": member}


def quote(zip_code: str, classes: list[str], types=None, product: str = "standard", member: bool = False) -> dict:
    pv = bulk.preview(zip_code, classes)
    n = pv["count"]
    meta = dataset_meta(zip_code, classes)
    p = pricing()
    types = normalize_types(types)
    lp = list_price(n, types, product, member, p) if n else {"per_property_cents": 0, "price_cents": 0,
                                                              "full_price_cents": 0, "member_discount": member}
    return {"zip": zip_code, "classes": classes, "count": n, "by_county": pv["by_county"],
            "types": types, "product": product, **lp, "pricing": p,
            "ready": dataset_ready(zip_code, classes),
            "measured": meta.get("finished") if meta and meta.get("status") == "ready" else None}


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


def read_dataset(zip_code: str, classes: list[str]) -> list[dict]:
    with open(dataset_path(zip_code, classes), newline="") as fh:
        return [r for r in csv.DictReader(fh) if r.get("lawn_sqft") not in (None, "") and (r.get("address") or "").strip()]


# ------------------------------------------------------------------ samples
def sample_allowed(email: str, ip: str | None) -> str | None:
    """None if allowed, else the reason."""
    with _db() as con:
        if con.execute("SELECT 1 FROM samples WHERE lower(email)=lower(?)", (email,)).fetchone():
            return "This email has already used its free sample. Buy the full sheet, or contact us."
        if ip:
            n = con.execute("SELECT COUNT(*) FROM samples WHERE ip=? AND ts>?", (ip, time.time() - 86400)).fetchone()[0]
            if n >= SAMPLES_PER_IP_PER_DAY:
                return "Too many free samples from this connection today. Try again tomorrow."
    return None


def take_sample(email: str, ip: str | None, zip_code: str, classes: list[str]) -> list[dict]:
    """25 properties spread across the ZIP. From the saved sheet when there is
    one, otherwise measured on the spot (about 20-60 s)."""
    if dataset_ready(zip_code, classes):
        rows = read_dataset(zip_code, classes)
        step = max(1, len(rows) // SAMPLE_SIZE)
        picked = rows[::step][:SAMPLE_SIZE]
    else:
        items = bulk._zip_parcels(zip_code, classes)
        # only sellable rows: properties with a street address
        def has_addr(it):
            m_ = measurer_for(it[0])
            return bool((m_.parcel_attrs(it[1]["attributes"]).get("address") or "").strip())
        items = [it for it in items if has_addr(it)]
        if not items:
            raise LookupError("No matching properties in that ZIP code in our service area.")
        # spread the sample over the ZIP: spatial sort, then even stride
        items.sort(key=lambda it: (round(it[1]["geometry"].centroid.y / 0.004), it[1]["geometry"].centroid.x))
        step = max(1, len(items) // SAMPLE_SIZE)
        chosen = items[::step][:SAMPLE_SIZE]

        def one(it):
            cid, f = it
            m = measurer_for(cid)
            try:
                return _row(m.measure(f), m.c["name"], f["geometry"])
            except Exception:
                return None
        with ThreadPoolExecutor(max_workers=8) as ex:
            picked = [r for r in ex.map(one, chosen) if r]
    with _lock, _db() as con:
        con.execute("INSERT INTO samples(email,ip,zip,ts) VALUES(?,?,?,?)", (email.lower(), ip, zip_code, time.time()))
    from . import mailer
    mailer.notify_owner(f"Free sample: {email} took ZIP {zip_code}",
                        f"{email} downloaded the free 25-property sample for ZIP {zip_code}.\n"
                        f"A good time to follow up.", reply_to=email)
    return picked


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
                        if r.get("lawn_sqft") not in (None, "") and (r.get("address") or "").strip()]
            tmp = dataset_path(zip_code, classes).with_suffix(".tmp")
            with open(tmp, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=[k for k, _ in COLUMNS], extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(_bulk_row_to_sale(r))
            shutil.move(tmp, dataset_path(zip_code, classes))
        meta_p.write_text(json.dumps({"status": "ready" if ok else "failed", "job": job["id"],
                                      "count": job.get("done"), "errors": job.get("errors"),
                                      "finished": time.time()}))
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


def dataset_progress(zip_code: str, classes: list[str]) -> dict:
    meta = dataset_meta(zip_code, classes) or {}
    if meta.get("status") == "building" and meta.get("job"):
        j = bulk.get_job(meta["job"]) or {}
        return {"status": "building", "done": j.get("done", 0), "total": j.get("total", 0),
                "stage": j.get("status")}
    return {"status": meta.get("status", "none")}


# ------------------------------------------------------------------- orders
def create_order(email: str, zip_code: str, classes: list[str], types=None, site: str = "",
                 product: str = "standard", member: bool = False) -> dict:
    types = normalize_types(types)
    product = "premier" if product == "premier" else "standard"
    q = quote(zip_code, classes, types, product, member)
    if not q["count"]:
        raise LookupError("No matching properties in that ZIP code in our service area.")
    oid = secrets.token_hex(4).upper()
    tok = secrets.token_urlsafe(16)
    with _lock, _db() as con:
        con.execute("INSERT INTO orders(id,email,zip,classes,count,amount_cents,status,token,created,types,site,product) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, email.lower(), zip_code, ",".join(classes), q["count"], q["price_cents"],
                     "awaiting_payment", tok, time.time(), ",".join(types), site, product))
    o = get_order(oid)
    _email_new_order(o)
    return o


def order_types(o: dict) -> list[str]:
    return normalize_types((o.get("types") or "lawn").split(","))


def type_labels(types) -> list[str]:
    lab = {k: l for k, l, _ in MEASURE_TYPES}
    return [lab[t] for t in normalize_types(types)]


def order_link(o: dict) -> str:
    from . import mailer
    return f"{mailer.site_url(o.get('site') or '')}/data/order/{o['id']}?t={o['token']}"


def _email_new_order(o: dict):
    from . import mailer
    usd = f"${o['amount_cents'] / 100:,.2f}"
    what = ("Premier list: full property report PDF for every home" if (o.get("product") or "") == "premier"
            else ", ".join(type_labels(order_types(o))))
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
        _set(oid, status="processing", paid=time.time(), note=note)
        from . import mailer
        mailer.notify_owner(f"Paid: order {oid}, ZIP {o['zip']} (${o['amount_cents'] / 100:,.2f})",
                            f"{o['email']} paid for ZIP {o['zip']} ({note or 'payment'}).\n{order_link(o)}")
        classes = o["classes"].split(",") if o["classes"] else []

        def ready():
            _set(oid, status="ready")
            _email_ready(oid)
        if (o.get("product") or "standard") == "premier":
            from . import premier
            state = premier.ensure(o["zip"], classes, on_ready=ready)
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
    v["types"] = order_types(o)
    v["type_labels"] = (["Full property report PDF for every home (roof, walls & elevations, lot & yard)"]
                        if v["product"] == "premier" else type_labels(v["types"]))
    if o["status"] == "processing":
        if v["product"] == "premier":
            from . import premier
            is_ready, prog = premier.ready(o["zip"], classes), premier.progress(o["zip"], classes)
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
            "Premier list: full property report PDF for every home" if (o.get("product") or "") == "premier"
            else ", ".join(type_labels(order_types(o)))),
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


init()
