"""What buyers can look at before paying: real measured rows with everything
that identifies a home removed, one flagged example, and a sample postcard
(made-up name and address on a real home's measurements)."""
from __future__ import annotations

import json
import time

from . import datastore as ds
from .pricing import DATA_DIR

# Left out of the public sample: anything that points to a specific home or person.
HIDDEN = {"address", "city", "zip", "owner", "owner_mailing", "lat", "lon", "pin", "notes"}
FAKE = {"address": "1234 Example St", "city": "Anytown", "zip": "00000", "owner": "SAMPLE JORDAN"}  # county style: last first
_CACHE = DATA_DIR / "showcase_v2.json"
_POSTCARD = DATA_DIR / "showcase_postcard_v2.json"
MAX_AGE = 24 * 3600


def _source_rows(limit_zips: int = 6) -> list[dict]:
    """Rows from the measured ZIP sheets on file (newest first)."""
    metas = []
    for p in ds.ZIPS_DIR.glob("*.json"):
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        if m.get("status") == "ready":
            metas.append((m.get("finished") or 0, p.stem))
    rows = []
    for _, key in sorted(metas, reverse=True)[:limit_zips]:
        z, _, cls = key.partition("_")
        try:
            rows += ds.read_dataset(z, (cls or "R").split(","))
        except Exception:
            continue
    return rows


def _anon(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k not in HIDDEN}
    if r.get("home_value") not in (None, ""):
        from .bulk import band_label, band_of
        out["home_value"] = band_label(band_of(r["home_value"]))  # a range, not the exact figure
    yb = str(r.get("year_built") or "")
    out["year_built"] = (yb[:3] + "0s") if yb[:4].isdigit() else ""
    return out


def sample(n: int = 12) -> dict:
    """Up to n anonymized real rows, spread from small to large lawns, plus a
    flagged ("review") example with the reason. Cached for a day."""
    if _CACHE.exists():
        try:
            c = json.loads(_CACHE.read_text())
            if time.time() - c.get("at", 0) < MAX_AGE:
                return c
        except Exception:
            pass
    rows = [r for r in _source_rows() if (ds._num_or_none(r.get("lawn_sqft")) or 0) > 0]  # homes with a lawn
    if not rows:
        return {"available": False, "rows": [], "columns": []}
    ok = [r for r in rows if (r.get("quality") or "ok") == "ok"]
    flagged = [r for r in rows if r.get("quality") == "review"]
    pick = ds._spread(ok or rows, n - (1 if flagged else 0), lambda r: ds._num_or_none(r.get("lawn_sqft")))
    ex = None
    if flagged:  # prefer a tree-cover reason, the most common one buyers ask about
        flagged.sort(key=lambda r: 0 if "tree" in (r.get("notes") or "").lower() else 1)
        ex = flagged[0]
        pick = pick + [ex]
    cols = [(k, l) for k, l in ds.COLUMNS if k not in HIDDEN]
    out = {"available": True, "at": time.time(),
           "columns": [{"key": k, "label": l} for k, l in cols],
           "rows": [_anon(r) for r in pick],
           "csv": ds.to_csv([_anon(r) for r in pick], cols),
           "flagged": ({**_anon(ex), "reason": ex.get("notes") or "Marked for a second look"} if ex else None),
           "flagged_share": round(len(flagged) / len(rows), 3), "from_homes": len(rows),
           "postcard_row": (pick[len(pick) // 2] or {}).get("pin")}
    _CACHE.write_text(json.dumps(out))
    return out


def postcard() -> dict:
    """A finished postcard: a real home's aerial photo and measurements, with
    a made-up owner, address and company, priced at the suggested lawn rate.
    Cached for a day (rendering takes a few seconds)."""
    if _POSTCARD.exists():
        try:
            c = json.loads(_POSTCARD.read_text())
            if time.time() - c.get("at", 0) < MAX_AGE:
                return c
        except Exception:
            pass
    s = sample()
    if not s.get("available"):
        return {"available": False}
    pin = s.get("postcard_row")
    row = next((r for r in _source_rows() if r.get("pin") == pin), None)
    if not row:
        return {"available": False}
    from . import postcard as pc
    from . import trades
    num = ds._num_or_none
    yard = {k: num(row.get(k)) for k, _ in ds.COLUMNS if k.endswith("_sqft") or k.endswith("_ft")}
    d = {"pin": pin, **FAKE, "yard": yard,
         "walls": {"gross_wall_sqft": num(row.get("wall_sqft")), "est_openings": num(row.get("est_windows"))}}
    comp = {"name": "Your Company Name", "phone": "(555) 010-0100", "email": "hello@example.com",
            "brand_color": "#15803d", "rates": trades.suggested_rates()}
    pdf = pc.render_postcard(d, comp, "lawn")
    import base64
    import io
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    pages = []
    for i in range(len(doc)):
        im = doc[i].render(scale=1.4).to_pil()
        b = io.BytesIO()
        im.save(b, "JPEG", quality=82)
        pages.append("data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode())
    pr = trades.report_price("lawn", comp["rates"], d)
    out = {"available": True, "at": time.time(), "pages": pages,
           "lawn_sqft": yard.get("lawn_sqft"), "price": pr}
    _POSTCARD.write_text(json.dumps(out))
    return out
