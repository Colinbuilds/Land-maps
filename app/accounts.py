"""Company accounts, credits and stored roof reports (SQLite in the data dir)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time

from .pricing import DATA_DIR

DB_PATH = DATA_DIR / "roof.db"
_lock = threading.Lock()


def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _lock, _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL, credits INTEGER NOT NULL DEFAULT 0,
            brand_color TEXT DEFAULT '#1d4ed8', phone TEXT DEFAULT '', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY, company_id INTEGER NOT NULL, pin TEXT, address TEXT,
            created REAL NOT NULL, share_token TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, delta INTEGER NOT NULL,
            reason TEXT, ts REAL NOT NULL);
        """)
        have = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
        for col, typ in (("plan", "TEXT"), ("member_until", "REAL"), ("stripe_customer", "TEXT"),
                         ("stripe_sub", "TEXT")):
            if col not in have:
                con.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ}")


# ---------------------------------------------------------------- passwords
def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split("$", 1)
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)


# ----------------------------------------------------------------- sessions
def _secret() -> bytes:
    env = os.environ.get("ESTIMATOR_SECRET")
    if env:
        return env.encode()
    p = DATA_DIR / "session.key"
    if not p.exists():
        p.write_text(secrets.token_hex(32))
    return p.read_text().strip().encode()


def make_session(company_id: int, days: int = 30) -> str:
    exp = int(time.time() + days * 86400)
    msg = f"{company_id}.{exp}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def read_session(tok: str | None) -> int | None:
    if not tok:
        return None
    try:
        cid, exp, sig = tok.split(".")
        good = hmac.new(_secret(), f"{cid}.{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return int(cid)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- companies
MEMBER_GRACE = 3 * 86400  # a renewal payment can take a day or two to land


def _public(row) -> dict:
    d = dict(row)
    d.pop("pw_hash", None)
    d["member"] = is_member(d)
    return d


def is_member(c: dict | None) -> bool:
    return bool(c and c.get("plan") == "member" and (c.get("member_until") or 0) + MEMBER_GRACE > time.time())


def set_membership(cid: int, until: float | None, customer: str | None = None, sub: str | None = None,
                   active: bool = True) -> dict | None:
    with _lock, _db() as con:
        con.execute("UPDATE companies SET plan=?, member_until=?, stripe_customer=COALESCE(?, stripe_customer), "
                    "stripe_sub=COALESCE(?, stripe_sub) WHERE id=?",
                    ("member" if active else None, until, customer, sub, cid))
    return get_company(cid)


def company_by_email(email: str) -> dict | None:
    with _db() as con:
        r = con.execute("SELECT * FROM companies WHERE email=?", (email.strip().lower(),)).fetchone()
    return _public(r) if r else None


def company_by_sub(sub: str) -> dict | None:
    with _db() as con:
        r = con.execute("SELECT * FROM companies WHERE stripe_sub=?", (sub,)).fetchone()
    return _public(r) if r else None


def create_company(name: str, email: str, password: str, credits: int = 0, phone: str = "") -> dict:
    with _lock, _db() as con:
        cur = con.execute("INSERT INTO companies(name,email,pw_hash,credits,phone,created) VALUES(?,?,?,?,?,?)",
                          (name.strip(), email.strip().lower(), hash_pw(password), 0, phone, time.time()))
        cid = cur.lastrowid
    if credits:
        add_credits(cid, credits, "initial credits")
    return get_company(cid)


def get_company(cid: int) -> dict | None:
    with _db() as con:
        r = con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    return _public(r) if r else None


def login(email: str, password: str) -> dict | None:
    with _db() as con:
        r = con.execute("SELECT * FROM companies WHERE email=?", (email.strip().lower(),)).fetchone()
    if r and check_pw(password, r["pw_hash"]):
        return _public(r)
    return None


def list_companies() -> list[dict]:
    with _db() as con:
        rows = con.execute("""SELECT c.*, (SELECT COUNT(*) FROM reports r WHERE r.company_id=c.id) AS reports
                              FROM companies c ORDER BY c.created DESC""").fetchall()
    return [_public(r) for r in rows]


def update_company(cid: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "phone", "brand_color") and v is not None}
    if fields.get("password"):
        allowed["pw_hash"] = hash_pw(fields["password"])
    if allowed:
        with _lock, _db() as con:
            con.execute(f"UPDATE companies SET {', '.join(k + '=?' for k in allowed)} WHERE id=?",
                        (*allowed.values(), cid))
    return get_company(cid)


def add_credits(cid: int, delta: int, reason: str) -> int:
    with _lock, _db() as con:
        con.execute("UPDATE companies SET credits = credits + ? WHERE id=?", (delta, cid))
        con.execute("INSERT INTO ledger(company_id,delta,reason,ts) VALUES(?,?,?,?)", (cid, delta, reason, time.time()))
        return con.execute("SELECT credits FROM companies WHERE id=?", (cid,)).fetchone()["credits"]


# ------------------------------------------------------------------ reports
def spend_and_save(cid: int, report: dict) -> str:
    """Atomically take one credit (none for members) and store the report.
    Raises if out of credits."""
    rid = secrets.token_hex(5).upper()
    member = is_member(get_company(cid))
    with _lock, _db() as con:
        left = con.execute("SELECT credits FROM companies WHERE id=?", (cid,)).fetchone()
        if not left:
            raise PermissionError("no such company")
        if member:
            con.execute("INSERT INTO ledger(company_id,delta,reason,ts) VALUES(?,?,?,?)",
                        (cid, 0, f"report {rid} (member)", time.time()))
        else:
            if left["credits"] < 1:
                raise PermissionError("no credits left")
            con.execute("UPDATE companies SET credits = credits - 1 WHERE id=?", (cid,))
            con.execute("INSERT INTO ledger(company_id,delta,reason,ts) VALUES(?,?,?,?)",
                        (cid, -1, f"report {rid}", time.time()))
        con.execute("INSERT INTO reports(id,company_id,pin,address,created,share_token,data) VALUES(?,?,?,?,?,?,?)",
                    (rid, cid, report.get("pin"), report.get("address"), time.time(),
                     secrets.token_urlsafe(16), json.dumps(report)))
    return rid


def has_credit(cid: int) -> bool:
    c = get_company(cid)
    return bool(c and (c["member"] or c["credits"] >= 1))


def get_report(rid: str) -> dict | None:
    with _db() as con:
        r = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"])
    return d


def list_reports(cid: int | None = None, limit: int = 200) -> list[dict]:
    q = "SELECT r.id,r.company_id,r.pin,r.address,r.created,r.share_token,c.name AS company FROM reports r JOIN companies c ON c.id=r.company_id"
    args: tuple = ()
    if cid is not None:
        q += " WHERE r.company_id=?"
        args = (cid,)
    q += " ORDER BY r.created DESC LIMIT ?"
    with _db() as con:
        return [dict(r) for r in con.execute(q, (*args, limit)).fetchall()]


init()
