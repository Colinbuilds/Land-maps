"""CRM portal (member add-on): customers, estimates, schedule, photos, money.

Built around the property reports: a customer can be made from any report
(address, owner name, measurements) and an estimate is pre-filled from the
company's own prices for each of its trades. Everything is per company.

Tables (data/crm.db):
  customers  one per household the company works with
  estimates  line items; a public link the homeowner opens to accept
  jobs       one-off or repeating work (weekly, every 2 weeks, monthly)
  visits     a dated occurrence of a job that was done / skipped
  photos     before/after photos on a job or customer
  invoices   line items; a public link; marked paid by the company
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import threading
import time

from .pricing import DATA_DIR

DB = DATA_DIR / "crm.db"
PHOTO_DIR = DATA_DIR / "crm_photos"
PHOTO_DIR.mkdir(exist_ok=True)
_lock = threading.Lock()
REPEATS = {"none": 0, "weekly": 7, "biweekly": 14, "monthly": 30}


def _db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _lock, _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, name TEXT, phone TEXT, email TEXT,
            address TEXT, city TEXT, zip TEXT, pin TEXT, lat REAL, lon REAL, report_id TEXT,
            notes TEXT, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS cust_co ON customers(company_id);
        CREATE TABLE IF NOT EXISTS estimates (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
            report_id TEXT, items TEXT NOT NULL, total_cents INTEGER NOT NULL, note TEXT,
            status TEXT NOT NULL, token TEXT NOT NULL, created REAL NOT NULL, sent REAL, answered REAL);
        CREATE INDEX IF NOT EXISTS est_co ON estimates(company_id);
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
            estimate_id INTEGER, title TEXT NOT NULL, trade TEXT, start TEXT NOT NULL, time TEXT,
            repeat TEXT NOT NULL DEFAULT 'none', until TEXT, price_cents INTEGER NOT NULL DEFAULT 0,
            notes TEXT, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS job_co ON jobs(company_id);
        CREATE TABLE IF NOT EXISTS visits (
            job_id INTEGER NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL, ts REAL NOT NULL,
            invoice_id INTEGER, note TEXT, PRIMARY KEY (job_id, day));
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, customer_id INTEGER, job_id INTEGER,
            day TEXT, kind TEXT, caption TEXT, file TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
            job_id INTEGER, items TEXT NOT NULL, total_cents INTEGER NOT NULL, note TEXT,
            status TEXT NOT NULL, token TEXT NOT NULL, created REAL NOT NULL, due TEXT,
            paid REAL, method TEXT);
        CREATE INDEX IF NOT EXISTS inv_co ON invoices(company_id);
        """)


def _row(r):
    if r is None:
        return None
    d = dict(r)
    if "items" in d and isinstance(d["items"], str):
        d["items"] = json.loads(d["items"])
    return d


def _one(sql, args):
    with _db() as con:
        return _row(con.execute(sql, args).fetchone())


def _all(sql, args=()):
    with _db() as con:
        return [_row(r) for r in con.execute(sql, args)]


def _insert(table: str, **f) -> int:
    with _lock, _db() as con:
        cur = con.execute(f"INSERT INTO {table}({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        return cur.lastrowid


def _update(table: str, co: int, rid: int, **f):
    f = {k: v for k, v in f.items()}
    if not f:
        return
    with _lock, _db() as con:
        con.execute(f"UPDATE {table} SET {', '.join(k + '=?' for k in f)} WHERE id=? AND company_id=?",
                    (*f.values(), rid, co))


def _items(items) -> tuple[list[dict], int]:
    """Clean line items: [{desc, qty, price_cents}] and the total."""
    out = []
    for it in items or []:
        desc = str(it.get("desc") or "").strip()[:160]
        try:
            qty = max(0.0, float(it.get("qty") or 1))
            price = int(round(float(it.get("price_cents") if it.get("price_cents") is not None
                                    else float(it.get("price") or 0) * 100)))
        except (TypeError, ValueError):
            continue
        if desc or price:
            out.append({"desc": desc, "qty": qty, "price_cents": max(0, price)})
    return out, int(round(sum(i["qty"] * i["price_cents"] for i in out)))


# ---------------------------------------------------------------- customers
CUST_FIELDS = ("name", "phone", "email", "address", "city", "zip", "notes")


def add_customer(co: int, **f) -> dict:
    data = {k: (str(f.get(k) or "").strip()[:300] or None) for k in CUST_FIELDS}
    for k in ("pin", "report_id"):
        data[k] = f.get(k)
    for k in ("lat", "lon"):
        data[k] = f.get(k)
    cid = _insert("customers", company_id=co, created=time.time(), **data)
    return get_customer(co, cid)


def update_customer(co: int, cid: int, **f):
    _update("customers", co, cid, **{k: (str(f[k]).strip()[:300] if f[k] is not None else None)
                                     for k in CUST_FIELDS if k in f})
    return get_customer(co, cid)


def get_customer(co: int, cid: int) -> dict | None:
    return _one("SELECT * FROM customers WHERE id=? AND company_id=?", (cid, co))


def list_customers(co: int, q: str | None = None) -> list[dict]:
    sql = """SELECT c.*,
               (SELECT COUNT(*) FROM jobs j WHERE j.customer_id=c.id AND j.active=1) AS jobs,
               (SELECT COALESCE(SUM(total_cents),0) FROM invoices i WHERE i.customer_id=c.id AND i.status='paid') AS paid_cents,
               (SELECT COALESCE(SUM(total_cents),0) FROM invoices i WHERE i.customer_id=c.id AND i.status='unpaid') AS owed_cents
             FROM customers c WHERE c.company_id=?"""
    args: list = [co]
    if q:
        sql += " AND (c.name LIKE ? OR c.address LIKE ? OR c.phone LIKE ? OR c.email LIKE ?)"
        args += [f"%{q}%"] * 4
    return _all(sql + " ORDER BY c.created DESC LIMIT 500", args)


def customer_from_report(co: int, rep: dict) -> dict:
    """The customer for a report's address (made once, reused after)."""
    d = rep["data"]
    hit = _one("SELECT * FROM customers WHERE company_id=? AND pin=?", (co, d.get("pin")))
    if hit:
        return hit
    from .postcard import nice_name, owner_of
    name = owner_of(d) or ""
    return add_customer(co, name=nice_name(name), address=d.get("address"),
                        city=(d.get("city") or "").title(), zip=d.get("zip"), pin=d.get("pin"),
                        report_id=rep["id"])


# ---------------------------------------------------------------- estimates
def estimate_items_from_report(company: dict, d: dict, trades: list[str] | None = None) -> list[dict]:
    """One line per trade the company prices, from this home's measurements."""
    from . import trades as T
    out = []
    for t in trades or company.get("trades") or []:
        if t not in T.PRICE_RULES:
            continue
        p = T.report_price(t, company.get("rates"), d)
        what, unit, per = T.PRICE_RULES[t][0], T.PRICE_RULES[t][1], T.PRICE_RULES[t][2]
        amt = T.PRICE_RULES[t][7](d)
        size = f"{float(amt):,.0f} {unit}" if amt not in (None, "") else ""
        out.append({"desc": f"{T.label(t)} ({p['label'] if p else 'price'}){f' - {what}: {size}' if size else ''}",
                    "qty": 1, "price_cents": int(round(p["price"] * 100)) if p else 0, "trade": t})
    return out


def create_estimate(co: int, customer_id: int, items, note: str = "", report_id: str | None = None) -> dict:
    its, total = _items(items)
    eid = _insert("estimates", company_id=co, customer_id=customer_id, report_id=report_id,
                  items=json.dumps(its), total_cents=total, note=(note or "")[:1000], status="draft",
                  token=secrets.token_urlsafe(12), created=time.time())
    return get_estimate(co, eid)


def update_estimate(co: int, eid: int, items=None, note=None, status=None) -> dict | None:
    f = {}
    if items is not None:
        its, total = _items(items)
        f.update(items=json.dumps(its), total_cents=total)
    if note is not None:
        f["note"] = note[:1000]
    if status in ("draft", "sent", "accepted", "declined"):
        f["status"] = status
        if status == "sent":
            f["sent"] = time.time()
    _update("estimates", co, eid, **f)
    return get_estimate(co, eid)


def get_estimate(co: int, eid: int) -> dict | None:
    return _one("SELECT * FROM estimates WHERE id=? AND company_id=?", (eid, co))


def estimate_by_token(tok: str) -> dict | None:
    return _one("SELECT * FROM estimates WHERE token=?", (tok,))


def answer_estimate(tok: str, accept: bool) -> dict | None:
    e = estimate_by_token(tok)
    if not e or e["status"] in ("accepted", "declined"):
        return e
    with _lock, _db() as con:
        con.execute("UPDATE estimates SET status=?, answered=? WHERE id=?",
                    ("accepted" if accept else "declined", time.time(), e["id"]))
    return estimate_by_token(tok)


def list_estimates(co: int, status: str | None = None) -> list[dict]:
    sql = ("SELECT e.*, c.name AS customer, c.address FROM estimates e JOIN customers c ON c.id=e.customer_id "
           "WHERE e.company_id=?")
    args: list = [co]
    if status:
        sql += " AND e.status=?"
        args.append(status)
    return _all(sql + " ORDER BY e.created DESC LIMIT 500", args)


# --------------------------------------------------------------------- jobs
def create_job(co: int, customer_id: int, title: str, start: str, repeat: str = "none", price_cents: int = 0,
               time_: str | None = None, until: str | None = None, trade: str | None = None,
               estimate_id: int | None = None, notes: str = "") -> dict:
    dt.date.fromisoformat(start)
    if until:
        dt.date.fromisoformat(until)
    jid = _insert("jobs", company_id=co, customer_id=customer_id, estimate_id=estimate_id,
                  title=title.strip()[:120] or "Job", trade=trade, start=start, time=(time_ or None),
                  repeat=repeat if repeat in REPEATS else "none", until=until or None,
                  price_cents=max(0, int(price_cents or 0)), notes=(notes or "")[:1000], active=1,
                  created=time.time())
    return get_job(co, jid)


def update_job(co: int, jid: int, **f) -> dict | None:
    ok = {k: v for k, v in f.items() if k in ("title", "start", "time", "repeat", "until", "price_cents",
                                              "notes", "active", "trade")}
    if "repeat" in ok and ok["repeat"] not in REPEATS:
        ok.pop("repeat")
    _update("jobs", co, jid, **ok)
    return get_job(co, jid)


def get_job(co: int, jid: int) -> dict | None:
    return _one("SELECT * FROM jobs WHERE id=? AND company_id=?", (jid, co))


def _occurs(job: dict, day: dt.date) -> bool:
    start = dt.date.fromisoformat(job["start"])
    if day < start or (job.get("until") and day > dt.date.fromisoformat(job["until"])):
        return False
    step = REPEATS.get(job["repeat"], 0)
    if not step:
        return day == start
    if job["repeat"] == "monthly":
        return day.day == start.day
    return (day - start).days % step == 0


def schedule(co: int, frm: str, days: int = 7) -> list[dict]:
    """Every job visit from ``frm`` for ``days`` days, with customer, status
    and price, ordered by day, then time, then a simple route order
    (north-to-south sweep, west to east) for untimed stops."""
    d0 = dt.date.fromisoformat(frm)
    jobs = _all("""SELECT j.*, c.name AS customer, c.address, c.city, c.phone, c.lat, c.lon
                   FROM jobs j JOIN customers c ON c.id=j.customer_id WHERE j.company_id=? AND j.active=1""", (co,))
    vis = {(v["job_id"], v["day"]): v for v in _all(
        "SELECT v.* FROM visits v JOIN jobs j ON j.id=v.job_id WHERE j.company_id=? AND v.day>=? AND v.day<?",
        (co, d0.isoformat(), (d0 + dt.timedelta(days=days)).isoformat()))}
    out = []
    for i in range(days):
        day = d0 + dt.timedelta(days=i)
        for j in jobs:
            if _occurs(j, day):
                v = vis.get((j["id"], day.isoformat())) or {}
                out.append({"day": day.isoformat(), "job_id": j["id"], "title": j["title"], "trade": j["trade"],
                            "time": j["time"], "repeat": j["repeat"], "price_cents": j["price_cents"],
                            "customer_id": j["customer_id"], "customer": j["customer"], "address": j["address"],
                            "city": j["city"], "phone": j["phone"], "notes": j["notes"],
                            "status": v.get("status") or "scheduled", "invoice_id": v.get("invoice_id"),
                            "lat": j["lat"], "lon": j["lon"]})
    out.sort(key=lambda s: (s["day"], s["time"] or "99", -(s["lat"] or 0), s["lon"] or 0))
    return out


def mark_visit(co: int, jid: int, day: str, status: str, note: str = "", invoice: bool = False) -> dict:
    """Done / skipped / scheduled for one visit; "done" can bill it at once."""
    j = get_job(co, jid)
    if not j:
        raise LookupError("job not found")
    dt.date.fromisoformat(day)
    inv_id = None
    if status == "done" and invoice and j["price_cents"]:
        inv = create_invoice(co, j["customer_id"], [{"desc": f"{j['title']} ({day})", "qty": 1,
                                                    "price_cents": j["price_cents"]}], job_id=jid)
        inv_id = inv["id"]
    with _lock, _db() as con:
        if status == "scheduled":
            con.execute("DELETE FROM visits WHERE job_id=? AND day=?", (jid, day))
        else:
            con.execute("INSERT OR REPLACE INTO visits(job_id,day,status,ts,invoice_id,note) VALUES(?,?,?,?,?,?)",
                        (jid, day, status, time.time(), inv_id, (note or "")[:500]))
    return {"job_id": jid, "day": day, "status": status, "invoice_id": inv_id}


# ------------------------------------------------------------------- photos
def add_photo(co: int, data: bytes, ext: str, customer_id: int | None = None, job_id: int | None = None,
              day: str | None = None, kind: str = "", caption: str = "") -> dict:
    """Store a job photo (re-encoded as JPEG, at most 2000 px)."""
    import io
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((2000, 2000))
    name = f"{co}_{secrets.token_hex(8)}.jpg"
    im.save(PHOTO_DIR / name, "JPEG", quality=85)
    pid = _insert("photos", company_id=co, customer_id=customer_id, job_id=job_id, day=day,
                  kind=kind if kind in ("before", "after") else "", caption=(caption or "")[:200], file=name,
                  created=time.time())
    return _one("SELECT * FROM photos WHERE id=?", (pid,))


def list_photos(co: int, customer_id: int | None = None, job_id: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM photos WHERE company_id=?", [co]
    if customer_id:
        sql += " AND customer_id=?"
        args.append(customer_id)
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)
    return _all(sql + " ORDER BY created DESC LIMIT 300", args)


def photo_file(co: int, pid: int):
    p = _one("SELECT * FROM photos WHERE id=? AND company_id=?", (pid, co))
    return PHOTO_DIR / p["file"] if p else None


def delete_photo(co: int, pid: int):
    f = photo_file(co, pid)
    with _lock, _db() as con:
        con.execute("DELETE FROM photos WHERE id=? AND company_id=?", (pid, co))
    if f and f.exists():
        f.unlink()


# ----------------------------------------------------------------- invoices
def create_invoice(co: int, customer_id: int, items, note: str = "", job_id: int | None = None,
                   due_days: int = 14) -> dict:
    its, total = _items(items)
    iid = _insert("invoices", company_id=co, customer_id=customer_id, job_id=job_id, items=json.dumps(its),
                  total_cents=total, note=(note or "")[:1000], status="unpaid", token=secrets.token_urlsafe(12),
                  created=time.time(), due=(dt.date.today() + dt.timedelta(days=due_days)).isoformat())
    return get_invoice(co, iid)


def get_invoice(co: int, iid: int) -> dict | None:
    return _one("SELECT * FROM invoices WHERE id=? AND company_id=?", (iid, co))


def invoice_by_token(tok: str) -> dict | None:
    return _one("SELECT * FROM invoices WHERE token=?", (tok,))


def set_invoice(co: int, iid: int, status: str, method: str | None = None) -> dict | None:
    if status not in ("paid", "unpaid", "void"):
        raise ValueError("bad status")
    _update("invoices", co, iid, status=status, paid=time.time() if status == "paid" else None,
            method=(method or None) if status == "paid" else None)
    return get_invoice(co, iid)


def list_invoices(co: int, status: str | None = None) -> list[dict]:
    sql = ("SELECT i.*, c.name AS customer, c.address FROM invoices i JOIN customers c ON c.id=i.customer_id "
           "WHERE i.company_id=?")
    args: list = [co]
    if status:
        sql += " AND i.status=?"
        args.append(status)
    return _all(sql + " ORDER BY i.created DESC LIMIT 500", args)


# ------------------------------------------------------------------- money
def money(co: int) -> dict:
    """The numbers on the Money tab: this month, last month, owed, by trade,
    the pipeline of open estimates and what's booked for the next 30 days."""
    today = dt.date.today()
    m0 = today.replace(day=1)
    lm0 = (m0 - dt.timedelta(days=1)).replace(day=1)
    ts = lambda d: time.mktime(d.timetuple())
    with _db() as con:
        q = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0
        paid_m = q("SELECT SUM(total_cents) FROM invoices WHERE company_id=? AND status='paid' AND paid>=?", co, ts(m0))
        paid_lm = q("SELECT SUM(total_cents) FROM invoices WHERE company_id=? AND status='paid' AND paid>=? AND paid<?",
                    co, ts(lm0), ts(m0))
        billed_m = q("SELECT SUM(total_cents) FROM invoices WHERE company_id=? AND status<>'void' AND created>=?", co, ts(m0))
        owed = q("SELECT SUM(total_cents) FROM invoices WHERE company_id=? AND status='unpaid'", co)
        overdue = q("SELECT SUM(total_cents) FROM invoices WHERE company_id=? AND status='unpaid' AND due<?",
                    co, today.isoformat())
        pipeline = q("SELECT SUM(total_cents) FROM estimates WHERE company_id=? AND status IN ('draft','sent')", co)
        won = q("SELECT COUNT(*) FROM estimates WHERE company_id=? AND status='accepted'", co)
        answered = q("SELECT COUNT(*) FROM estimates WHERE company_id=? AND status IN ('accepted','declined')", co)
        by_trade = {r[0] or "other": r[1] for r in con.execute(
            """SELECT j.trade, SUM(i.total_cents) FROM invoices i LEFT JOIN jobs j ON j.id=i.job_id
               WHERE i.company_id=? AND i.status='paid' AND i.paid>=? GROUP BY j.trade""", (co, ts(m0)))}
        months = [{"month": r[0], "cents": r[1]} for r in con.execute(
            """SELECT strftime('%Y-%m', paid, 'unixepoch', 'localtime') m, SUM(total_cents) FROM invoices
               WHERE company_id=? AND status='paid' GROUP BY m ORDER BY m DESC LIMIT 12""", (co,))]
    booked = sum(s["price_cents"] for s in schedule(co, today.isoformat(), 30) if s["status"] == "scheduled")
    return {"paid_this_month": paid_m, "paid_last_month": paid_lm, "billed_this_month": billed_m, "owed": owed,
            "overdue": overdue, "pipeline": pipeline, "booked_next_30": booked,
            "win_rate": round(100 * won / answered) if answered else None, "by_trade": by_trade, "months": months}


def stats(co: int) -> dict:
    """Short usage numbers for the owner's company library."""
    with _db() as con:
        q = lambda sql: con.execute(sql, (co,)).fetchone()[0] or 0
        return {"customers": q("SELECT COUNT(*) FROM customers WHERE company_id=?"),
                "jobs": q("SELECT COUNT(*) FROM jobs WHERE company_id=? AND active=1"),
                "estimates": q("SELECT COUNT(*) FROM estimates WHERE company_id=?"),
                "paid_cents": q("SELECT SUM(total_cents) FROM invoices WHERE company_id=? AND status='paid'"),
                "owed_cents": q("SELECT SUM(total_cents) FROM invoices WHERE company_id=? AND status='unpaid'")}


init()
