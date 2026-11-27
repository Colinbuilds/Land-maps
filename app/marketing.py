"""Marketing hub (owner): videos, images, files and written content in one
place, each with a status (idea, draft, ready, posted), tags (trades and
channels) and an optional public share link (/m/<token>).

Files live under data/marketing/ (the Railway volume), the list in
data/marketing.db.
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import sqlite3
import threading
import time

from .pricing import DATA_DIR

DB = DATA_DIR / "marketing.db"
FILES = DATA_DIR / "marketing"
FILES.mkdir(exist_ok=True)
_lock = threading.Lock()
KINDS = ("video", "image", "file", "script", "post", "email", "ad", "idea")
STATUSES = ("idea", "draft", "ready", "posted")
MAX_BYTES = 1024 * 1024 * 1024  # 1 GB per upload
CHANNELS = ["Facebook", "Instagram", "TikTok", "YouTube", "LinkedIn", "Email", "Postcard", "Website", "Trade show"]


def _db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _lock, _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT, file TEXT, mime TEXT,
            size INTEGER, tags TEXT, status TEXT NOT NULL, token TEXT, created REAL NOT NULL, updated REAL NOT NULL)""")


def _row(r) -> dict | None:
    if not r:
        return None
    d = dict(r)
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    return d


def list_items() -> list[dict]:
    with _db() as con:
        return [_row(r) for r in con.execute("SELECT * FROM items ORDER BY updated DESC")]


def get(iid: int) -> dict | None:
    with _db() as con:
        return _row(con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())


def by_token(tok: str) -> dict | None:
    with _db() as con:
        return _row(con.execute("SELECT * FROM items WHERE token=?", (tok,)).fetchone())


def _clean(kind=None, title=None, body=None, tags=None, status=None) -> dict:
    f = {}
    if kind is not None:
        f["kind"] = kind if kind in KINDS else "idea"
    if title is not None:
        f["title"] = (str(title).strip() or "Untitled")[:160]
    if body is not None:
        f["body"] = str(body)[:20000]
    if tags is not None:
        tl = tags.split(",") if isinstance(tags, str) else tags
        f["tags"] = ",".join(dict.fromkeys(str(t).strip()[:40] for t in tl if str(t).strip()))
    if status is not None:
        f["status"] = status if status in STATUSES else "draft"
    return f


def create(kind: str, title: str, body: str = "", tags=None, status: str = "draft") -> dict:
    f = _clean(kind, title, body, tags or [], status)
    now = time.time()
    with _lock, _db() as con:
        cur = con.execute("INSERT INTO items(kind,title,body,tags,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                          (f["kind"], f["title"], f.get("body", ""), f.get("tags", ""), f["status"], now, now))
        iid = cur.lastrowid
    return get(iid)  # read after the insert is committed


def update(iid: int, **fields) -> dict | None:
    f = _clean(**{k: v for k, v in fields.items() if k in ("kind", "title", "body", "tags", "status")})
    if f:
        f["updated"] = time.time()
        with _lock, _db() as con:
            con.execute(f"UPDATE items SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), iid))
    return get(iid)


async def attach(iid: int, upload) -> dict:
    """Store an uploaded file for an item, streamed to disk (large videos)."""
    it = get(iid)
    if not it:
        raise LookupError
    name = upload.filename or "file"
    ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "bin")[:8]
    fn = f"{iid}_{secrets.token_hex(6)}.{''.join(ch for ch in ext if ch.isalnum()) or 'bin'}"
    path, size = FILES / fn, 0
    with open(path, "wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                out.close()
                path.unlink(missing_ok=True)
                raise ValueError("Files up to 1 GB.")
            out.write(chunk)
    mime = upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if it.get("file"):
        (FILES / it["file"]).unlink(missing_ok=True)
    kind = it["kind"]
    if kind in ("idea", "file", "script", "post", "email", "ad"):
        kind = "video" if mime.startswith("video/") else "image" if mime.startswith("image/") else kind
    with _lock, _db() as con:
        con.execute("UPDATE items SET file=?, mime=?, size=?, kind=?, updated=? WHERE id=?",
                    (fn, mime, size, kind, time.time(), iid))
    return get(iid)


def file_path(it: dict):
    return FILES / it["file"] if it and it.get("file") else None


def set_share(iid: int, on: bool) -> dict | None:
    with _lock, _db() as con:
        con.execute("UPDATE items SET token=? WHERE id=?", (secrets.token_urlsafe(10) if on else None, iid))
    return get(iid)


def delete(iid: int):
    it = get(iid)
    if it and it.get("file"):
        (FILES / it["file"]).unlink(missing_ok=True)
    with _lock, _db() as con:
        con.execute("DELETE FROM items WHERE id=?", (iid,))


# ------------------------------------------------------------ starter kit
STARTER = [
    ("idea", "Launch plan: first 30 days", "ready", ["Plan"], """WEEK 1: FOUNDATION
- Finish the launch checklist (terms, privacy, refund policy, live Stripe, admin key, DNS).
- Record the 3 core videos below (lawn, roofing, the CRM). Phone + screen recording is fine.
- Build 5 'proof' reports for well-known Omaha neighborhoods (Elkhorn, Papillion, Gretna, Bennington, Lincoln SW).

WEEK 2: FOUNDING MEMBERS (target 20 companies)
- Founding offer: $115/mo locked for life, 25 free ZIP-list properties, 1 free exclusive ZIP review call.
- Direct outreach: 10 lawn, 5 snow, 5 roofing/gutter companies per day (Google Maps + Facebook groups).
- Offer a free report on their own house or a customer's house, live on the call.

WEEK 3: CONTENT ENGINE
- 1 short video a day (TikTok / Reels / Shorts) from the scripts here.
- 2 posts a week in Omaha/Lincoln contractor Facebook groups (value first, link second).
- Email every company that took a free sample.

WEEK 4: SCALE WHAT WORKS
- Turn the best video into a $10-20/day Facebook + Instagram ad aimed at business owners within 50 miles of Omaha.
- Ask founding members for one-line testimonials + permission to show their postcard.
- Pitch Augusta Lawn Care and other franchise owners: one demo per territory."""),
    ("script", "Video: 'Price a lawn without driving there' (lawn care, 45 sec)", "ready", ["Lawn", "TikTok", "Instagram", "YouTube"], """HOOK (0-3s, on screen: a truck on the highway)
"Still driving to every house to measure a lawn?"

PROBLEM (3-10s)
"Every quote costs you 30 minutes of drive time and gas, and half of them never book."

DEMO (10-35s, screen recording)
"Type the address. 30 seconds later: 6,796 square feet of grass, the parkway, the beds. And your price, already figured: $45 a mow. Tap once and it's an estimate you text to the homeowner."

CTA (35-45s)
"Every home in your ZIP, measured and priced, before you knock. Home Service Info. Link in bio."

SHOTS: phone in truck cab, screen recording of /reports, the shaded yard photo, the estimate page on a phone."""),
    ("script", "Video: 'Roof measurements without a ladder' (roofing & gutters, 40 sec)", "ready", ["Roofing", "Gutters", "TikTok", "YouTube"], """HOOK: "Stop paying $30+ for every roof report."
DEMO: address -> pitch diagram, facet areas, ridges, valleys, eaves, gutters, elevations. "Unlimited reports for one flat monthly price."
PROOF: side by side with a paid aerial roof report: "within a few percent."
CTA: "Measure every roof in a ZIP. Mail them a postcard with their price on it. homeserviceinfo.com" """),
    ("script", "Video: 'Your whole route in one app' (CRM, 60 sec)", "ready", ["CRM", "Instagram", "Facebook", "YouTube"], """HOOK: "This is my entire lawn business on one screen."
WALKTHROUGH: Start new report -> homeowner added by name -> Make estimate -> text it -> customer taps Accept -> Schedule it weekly -> Done + invoice -> Money tab.
LINE: "No spreadsheets. No visits to quote. The CRM comes with the membership."
CTA: "$115 a month. 2 trades included. homeserviceinfo.com" """),
    ("post", "Facebook group post: free measurement offer", "ready", ["Facebook", "Lawn", "Snow"], """Omaha lawn & snow guys: I built a tool that measures any property from aerial photos + LiDAR in about 30 seconds (lawn sq ft, driveway, sidewalk, beds, trees).

I'll run 5 free reports for anyone in here. Comment an address you're quoting this week (or DM it) and I'll send you the PDF.

If it saves you a drive, cool. If it's off, tell me. I'm testing accuracy before launch."""),
    ("post", "Social post: ZIP code numbers (Elkhorn 68022)", "ready", ["Facebook", "Instagram", "LinkedIn", "Lawn"], """68022 (Elkhorn) has about 166 million square feet of lawn across 15,768 homes.

At $6 per 1,000 sq ft, that's about $1 million of mowing every single week of the season.

We measured every yard. Owner name, lawn size and YOUR price per home, ready to mail. Lock the ZIP so no other lawn company can buy it.

homeserviceinfo.com"""),
    ("email", "Cold email to a lawn / snow company owner", "ready", ["Email", "Lawn", "Snow"], """Subject: your next 500 customers, already measured

Hi {first name},

Quick one: we measured every home in {ZIP}: lawn square feet, driveway, sidewalk, beds, plus the owner's name.

Put in your price per 1,000 sq ft once, and every home gets a price. We'll even make the postcards (their house from above + your price), you just print and mail.

Want 25 homes from {ZIP} free so you can see it? Reply "yes" and I'll send the sheet.

{your name}
Home Service Info · homeserviceinfo.com"""),
    ("email", "Follow-up after a free sample", "ready", ["Email"], """Subject: how did the {ZIP} sample look?

Hi {first name}, did the 25 homes line up with what you see on site?

Two things members use most:
1. Start new report: any address, 30 seconds, priced, and the homeowner lands in your CRM.
2. Exclusive ZIP: lock {ZIP} so nobody else gets this data.

Founding members lock in $115/month for life. Want me to set yours up?"""),
    ("ad", "Facebook/Instagram ad: 'Quote without driving'", "draft", ["Facebook", "Instagram", "Lawn", "Roofing"], """PRIMARY TEXT: Quote any home in 30 seconds without driving there. Lawn, driveway, roof, walls, all measured from aerial photos and LiDAR, priced with YOUR rates.
HEADLINE: Every home measured before you knock
DESCRIPTION: Unlimited reports + CRM, $115/mo
AUDIENCE: Business owners, 25-60, within 50 miles of Omaha / Lincoln; interests: lawn care business, landscaping, roofing contractor, Jobber, Housecall Pro.
CREATIVE: the 45-second lawn video, or the before/after postcard."""),
    ("idea", "Video ideas list (shorts)", "idea", ["TikTok", "Instagram", "YouTube"], """- "I measured every yard in Elkhorn" (map zooming, numbers popping up)
- "Guess this lawn's square footage" (then reveal)
- "How much is mowing this ZIP worth?" ($1M/week)
- Postcard reveal: homeowner's house from above + the price
- "Roof report vs ours" side by side
- Day in the life: Start new report -> estimate accepted -> mowed -> paid
- Owner testimonial: "I quoted 40 houses from my couch"
- Snow: "Every driveway in Papillion, measured" before the first storm"""),
]


def seed_if_empty() -> int:
    with _db() as con:
        if con.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
            return 0
    for kind, title, status, tags, body in STARTER:
        create(kind, title, body, tags, status)
    return len(STARTER)


init()
