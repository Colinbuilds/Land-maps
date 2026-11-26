"""Safety: sign-in lockouts, the owner's audit log, nightly backups and the
set-up checklist shown in the command center.

  * Lockouts: 10 wrong passwords or admin keys from one IP address (or for
    one email) within 15 minutes locks that address out for 15 minutes.
  * Audit log: every owner action (any change made through /api/admin/...)
    and every owner sign-in, with time, IP and result (data/safety.db).
  * Backups: every database and the settings, copied consistently into
    data/backups/backup-YYYY-MM-DD-HHMM.zip once a day (and on demand); the
    newest 14 are kept. Download them from the command center and keep a
    copy off the server.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque

from .pricing import DATA_DIR

DB = DATA_DIR / "safety.db"
BACKUPS = DATA_DIR / "backups"
BACKUPS.mkdir(exist_ok=True)
KEEP_BACKUPS = 14
LOCK_AFTER = 10            # failures ...
WINDOW = 15 * 60           # ... within this many seconds
_lock = threading.Lock()
_fails: dict[str, deque] = defaultdict(deque)


def _db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _lock, _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, ip TEXT, method TEXT, path TEXT, status INTEGER, note TEXT)""")


# ---------------------------------------------------------------- lockouts
def _trim(q: deque, now: float):
    while q and now - q[0] > WINDOW:
        q.popleft()


def locked(*keys: str) -> int:
    """Seconds until these keys (IP, email...) may try again; 0 = allowed."""
    now = time.time()
    with _lock:
        for k in keys:
            q = _fails.get(k)
            if q:
                _trim(q, now)
                if len(q) >= LOCK_AFTER:
                    return int(WINDOW - (now - q[0])) + 1
    return 0


def failed(*keys: str):
    now = time.time()
    with _lock:
        for k in keys:
            if k:
                _fails[k].append(now)


def succeeded(*keys: str):
    with _lock:
        for k in keys:
            _fails.pop(k, None)


def lockouts() -> list[dict]:
    """Addresses currently locked out (for the Safety tab)."""
    now = time.time()
    out = []
    with _lock:
        for k, q in _fails.items():
            _trim(q, now)
            if len(q) >= LOCK_AFTER:
                out.append({"key": k, "fails": len(q), "free_in": int(WINDOW - (now - q[0])) + 1})
    return out


# ---------------------------------------------------------------- audit log
def log(ip: str, method: str, path: str, status: int, note: str = ""):
    try:
        with _lock, _db() as con:
            con.execute("INSERT INTO audit(ts,ip,method,path,status,note) VALUES(?,?,?,?,?,?)",
                        (time.time(), ip, method, path[:300], status, note[:300]))
    except Exception:
        pass


def audit(limit: int = 200) -> list[dict]:
    with _db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]


# ------------------------------------------------------------------ backups
def backup_now() -> dict:
    """A consistent copy of every database plus the settings, zipped."""
    name = time.strftime("backup-%Y-%m-%d-%H%M.zip")
    dest = BACKUPS / name
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(DATA_DIR.glob("*.db")):
            with tempfile.TemporaryDirectory() as td:
                copy = os.path.join(td, p.name)
                src = sqlite3.connect(p, timeout=30)
                dst = sqlite3.connect(copy)
                with dst:
                    src.backup(dst)  # safe while the site is running
                src.close()
                dst.close()
                z.write(copy, p.name)
        for p in sorted(list(DATA_DIR.glob("*.json")) + [DATA_DIR / "session.key"]):
            if p.exists():
                z.write(p, p.name)
    tmp.replace(dest)
    for old in sorted(BACKUPS.glob("backup-*.zip"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return {"name": name, "size": dest.stat().st_size, "created": dest.stat().st_mtime}


def backups() -> list[dict]:
    return [{"name": p.name, "size": p.stat().st_size, "created": p.stat().st_mtime}
            for p in sorted(BACKUPS.glob("backup-*.zip"), reverse=True)]


def backup_path(name: str):
    p = BACKUPS / name
    return p if p.parent == BACKUPS and p.name.startswith("backup-") and p.suffix == ".zip" and p.exists() else None


_started = False


def start_nightly():
    """Back up once a day in the background (and at start if the newest
    backup is over a day old)."""
    global _started
    if _started:
        return
    _started = True

    def run():
        while True:
            try:
                b = backups()
                if not b or time.time() - b[0]["created"] > 23 * 3600:
                    backup_now()
            except Exception:
                pass
            time.sleep(3600)
    threading.Thread(target=run, daemon=True, name="nightly-backup").start()


# --------------------------------------------------------------- checklist
def checklist(admin_token_set: bool) -> list[dict]:
    """What's set up and what isn't, for the Safety tab."""
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    vol = os.path.ismount("/var/data") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    b = backups()
    items = [
        ("Admin key set", admin_token_set, "Set ESTIMATOR_ADMIN_TOKEN in Railway > Variables. Without it the command center is open to anyone."),
        ("Session secret set", bool(os.environ.get("ESTIMATOR_SECRET")) or (DATA_DIR / "session.key").exists(),
         "Sign-ins are signed with a secret stored on the volume (or ESTIMATOR_SECRET)."),
        ("Data on a persistent volume", bool(vol), "Attach a Railway volume at /var/data, or every redeploy erases accounts, CRM data and orders."),
        ("Recent backup (last 26 hours)", bool(b) and time.time() - b[0]["created"] < 26 * 3600,
         "Backups run nightly; tap Back up now, then download one and keep it off the server."),
        ("Card payments live", key.startswith("sk_live_") or key.startswith("rk_live_"),
         "STRIPE_SECRET_KEY is a test key or missing. Use the live secret key to take real payments."),
        ("Stripe webhook secret set", bool(os.environ.get("STRIPE_WEBHOOK_SECRET")),
         "Add STRIPE_WEBHOOK_SECRET so renewals and cancellations update automatically."),
        ("Email sending set up", bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD")),
         "Set GMAIL_USER and GMAIL_APP_PASSWORD so order, estimate and password-reset emails go out."),
        ("Site address set", bool(os.environ.get("SITE_URL")), "Set SITE_URL (e.g. https://www.homeserviceinfo.com) so links in emails point to your domain."),
    ]
    return [{"label": l, "ok": bool(ok), "fix": fix} for l, ok, fix in items]


init()
