"""Door-to-door mail: a print-ready postcard design for one property.

9 x 6 in postcard with 1/8 in bleed on every side (9.25 x 6.25 in PDF),
two pages: the front (aerial photo of the home with its measurements) and
the back (message + address panel). The buyer sends the PDF to any print
shop or mail house; we don't print or mail.

Photos come from the county's own aerial imagery. Where that imagery isn't
licensed for print (county config ``imagery.print_ok`` False), the photo is
USDA NAIP, which is public domain.
"""
from __future__ import annotations

import io
import math
import re

from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas
from shapely.geometry import shape

IN = 72.0
BLEED = 0.125 * IN
TRIM_W, TRIM_H = 9 * IN, 6 * IN
PW, PH = TRIM_W + 2 * BLEED, TRIM_H + 2 * BLEED
SAFE = BLEED + 0.1875 * IN          # keep text 3/16 in inside the trim
PHOTO_FRAC = 0.56                    # share of the front the photo covers

ROOF_TRADES = {"roofing", "gutters", "lights", "solar", "siding", "windows", "hvac"}
NAIP = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer/exportImage"

DEFAULT_COPY = {
    "lawn": ("Your yard, measured.", "No visit needed: we already know your lawn size. Call for your exact price."),
    "snow": ("Your driveway, measured.", "Get on our route before the first snow. Call for your exact price."),
    "concrete": ("Your driveway, measured.", "Sealing, repairs and new concrete, priced from your exact square feet."),
    "pressure": ("Your home, measured.", "House wash, driveway and patio cleaning priced to your exact size."),
    "windows": ("Your home, measured.", "Sparkling windows inside and out. Call for your exact price."),
    "gutters": ("Your gutters, measured.", "Cleaning and guards priced to your home's exact gutter length."),
    "lights": ("Your roofline, measured.", "Professional holiday lighting, priced to your exact roofline."),
    "roofing": ("Your roof, measured.", "A free inspection and an exact quote, from your roof's real size."),
    "siding": ("Your home, measured.", "Siding and paint priced from your home's exact wall area."),
    "solar": ("Your roof, measured.", "See how much of your roof faces the sun. Call for a free solar estimate."),
    "landscaping": ("Your yard, measured.", "Mulch, rock and planting priced from your exact bed size."),
    "irrigation": ("Your lawn, measured.", "Sprinkler installs, repairs and winterizing sized to your lawn."),
    "pest": ("Your property, measured.", "Pest and mosquito control sized to your yard and home."),
    "hvac": ("Your home, measured.", "Heating and cooling sized right for your home."),
}
GENERIC = ("Your property, measured.", "Call for your exact price. No visit needed.")
PRICED_OFFER = "Your price is already figured from your home's real measurements. Call or text to get on the schedule."


# ----------------------------------------------------------- studio settings
LAYOUT_DEFAULTS = {"photo_side": "left", "show_facts": True, "show_address_tag": True}


def studio() -> dict:
    """The owner's postcard studio settings: per-trade wording and layout."""
    from .pricing import load_settings
    st = load_settings().get("postcards") or {}
    return {"templates": st.get("templates") or {}, "layout": {**LAYOUT_DEFAULTS, **(st.get("layout") or {})}}


def copy_for(trade: str | None, priced: bool) -> tuple[str, str]:
    """(headline, back message) for a trade: the studio's wording, else the defaults."""
    t = studio()["templates"].get(trade or "") or {}
    h, o = DEFAULT_COPY.get(trade or "", GENERIC)
    if priced:
        o = t.get("priced_offer") or PRICED_OFFER
    else:
        o = t.get("offer") or o
    return t.get("headline") or h, o


# ------------------------------------------------------------------- photo
def _parcel(d: dict):
    from .counties import split_pid
    from .measure import measurer_for
    m = measurer_for(split_pid(d["pin"])[0])
    return m, m.get_parcel(split_pid(d["pin"])[1])


def _parcel_geom(d: dict):
    m, f = _parcel(d)
    return m, (f["geometry"] if f else None)


_KEEP_UP = {"LLC", "LP", "LLP", "INC", "II", "III", "IV", "TR", "TRS", "CO", "USA", "HOA", "PC", "PLLC"}


def nice_name(raw: str | None) -> str:
    """'SMITH JOHN A & JANE B' -> 'Smith John A & Jane B' (assessor names are
    all caps); company suffixes stay upper case."""
    words = " ".join(str(raw or "").split()).split(" ")
    out = []
    for w in words:
        core = w.strip(",.")
        if core.upper() in _KEEP_UP or not re.search(r"[A-Za-z]", core):
            out.append(w)
        else:  # O'BRIEN -> O'Brien, SMITH-JONES -> Smith-Jones
            out.append(re.sub(r"[A-Za-z]+", lambda mm: mm.group(0)[:1].upper() + mm.group(0)[1:].lower(), w))
    return " ".join(out).strip()


_BUSINESS = {"LLC", "L.L.C.", "INC", "INC.", "LP", "LLP", "LTD", "CO", "CO.", "CORP", "CORPORATION", "COMPANY", "CHURCH",
             "CITY", "COUNTY", "STATE", "TRUST", "BANK", "ASSOCIATION", "ASSN", "PROPERTIES", "PROPERTY", "INVESTMENTS",
             "INVESTMENT", "INVESTORS", "HOLDINGS", "HOMES", "BUILDERS", "BUILDING", "PARTNERS", "PARTNERSHIP",
             "DEVELOPMENT", "MANAGEMENT", "RENTALS", "REALTY", "SCHOOL", "DISTRICT", "UNIVERSITY", "HOA", "FOUNDATION",
             "MINISTRIES", "REVOCABLE", "LIVING", "FAMILY", "USA", "AUTHORITY", "GROUP", "VENTURES", "ENTERPRISES",
             "CAPITAL", "FUND", "COMMUNITY", "HOUSING", "CONDOMINIUM", "CONDO", "OWNERS", "LAND", "FARMS", "FARM",
             "RIVER", "OF", "THE", "HOLDCO", "SERVICES", "TRS", "LH"}
_ROLES = r"\b(CO-?TTEE'?S?|TTEE'?S?|TRUSTEES?|ET ?AL|ET ?UX|ET ?VIR|JTRS?|JTWROS|LIFE ESTATE|L/E|H/W|C/O.*)$"
_SUFFIX = {"JR", "SR", "II", "III", "IV"}


def person_name(raw: str | None) -> str:
    """An assessor owner name as you'd write it on mail, first name first.
    'ANDERSON MARK A' -> 'Mark Anderson'; 'RIGGS/MACK E & SUSANNE K' ->
    'Mack & Susanne Riggs'; 'GRAY, GENE J & TERI L' -> 'Gene & Teri Gray';
    'WORTMANN, JOAN & DOSTAL, JORDAN T' -> 'Joan Wortmann & Jordan Dostal'.
    Businesses and trusts keep their own name ('Jp & Pc LLC')."""
    s = " ".join(str(raw or "").upper().split())
    for _ in range(2):
        s = re.sub(_ROLES, "", s).strip(" ,&")
    if not s:
        return ""
    words = set(re.findall(r"[A-Z.']+", s))
    if words & _BUSINESS or re.search(r"\d", s):
        return nice_name(s)
    parts = [p.strip() for p in re.split(r"\s*&\s*|\s+AND\s+", s) if p.strip()]
    people, last = [], None
    for i, part in enumerate(parts):
        if "/" in part:
            l, g = part.split("/", 1)
        elif "," in part:
            l, g = part.split(",", 1)
        elif i == 0:
            l, _, g = part.partition(" ")
        else:  # "SUSANNE K" after "RIGGS/MACK E": same last name
            l, g = last or "", part
        lt = l.split()
        suffix = " ".join(w for w in lt if w in _SUFFIX)
        l = " ".join(w for w in lt if w not in _SUFFIX)
        gt = [w for w in g.replace(",", " ").split() if w not in _SUFFIX]
        suffix = suffix or " ".join(w for w in g.split() if w in _SUFFIX)
        first = next((w for w in gt if len(w.strip(".")) > 1), gt[0] if gt else "")
        last = l or last
        people.append((first, last or "", suffix))
    people = [p for p in people if p[0] or p[1]][:2]
    if not people:
        return nice_name(s)
    cap = lambda w: nice_name(w)
    if len(people) == 2 and people[0][1] == people[1][1]:
        (f1, l, s1), (f2, _, _) = people
        return f"{cap(f1)} & {cap(f2)} {cap(l)}".strip() + (f" {cap(s1)}" if s1 else "")
    return " & ".join(" ".join(x for x in (cap(f), cap(l), cap(sf)) if x) for f, l, sf in people)


def owner_of(d: dict) -> str:
    """The owner's name for the address panel (from the report, else the county)."""
    if d.get("owner") is not None:
        return person_name(d.get("owner"))
    try:
        from .bulk import owner_info
        m, f = _parcel(d)
        return person_name(owner_info(m, f)["owner"]) if f else ""
    except Exception:
        return ""


def _naip(bbox, px_w: int, px_h: int) -> Image.Image | None:
    from .gis import get_bytes
    url = (f"{NAIP}?bbox={bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}&bboxSR=4326&imageSR=3857"
           f"&size={px_w},{px_h}&format=jpg&f=image")
    b = get_bytes(url)
    try:
        return Image.open(io.BytesIO(b)).convert("RGB") if b else None
    except Exception:
        return None


def property_photo(d: dict, trade: str | None = None, aspect: float = 0.82, lines: bool = True,
                   min_px: int = 1600, roof_lines: bool = False) -> bytes:
    """A JPEG of the property from above, framed on the lot at ``aspect``
    (width / height), with the lot line drawn on (and the measured roof
    outline when ``roof_lines``). Upscaled to at least ``min_px`` on its long side
    so print shops get ~300 dpi."""
    m, lot = _parcel_geom(d)
    if lot is None:
        raise LookupError("parcel not found")
    roofs = []
    if roof_lines:  # off on mail: a homeowner-facing piece shows only the lot line
        from shapely.geometry import Polygon
        from .roofreport import _to_wgs, lean_corrected
        for st in d.get("structures") or []:
            try:
                o = st.get("outline")
                if o:  # stored in LiDAR (state plane) feet; drawn where the roof shows in the photo
                    roofs.append(lean_corrected(m, _to_wgs(m, Polygon(o[0], o[1:]))))
                elif st.get("geojson"):
                    roofs.append(shape(st["geojson"]))
            except Exception:
                pass
    focus = lot
    minx, miny, maxx, maxy = focus.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    kx = math.cos(math.radians(cy))          # degrees of lon are shorter
    w_m, h_m = (maxx - minx) * kx, (maxy - miny)
    w_m, h_m = w_m * 1.25, h_m * 1.25        # margin around the lot
    if w_m / h_m < aspect:
        w_m = h_m * aspect
    else:
        h_m = w_m / aspect
    img_cfg = m.c.get("imagery") or {}
    print_ok = img_cfg.get("print_ok", True)
    if not print_ok:
        w_m, h_m = max(w_m, 0.0022 * aspect * 1.6), max(h_m, 0.0022 * 1.6)  # NAIP is ~2 ft/px: show the block
    bbox = (cx - w_m / 2 / kx, cy - h_m / 2, cx + w_m / 2 / kx, cy + h_m / 2)

    if print_ok:
        from .imagery import Mosaic
        z = min(21, int(img_cfg.get("max_zoom") or 21))
        mos = Mosaic(img_cfg["tile_url"], bbox, z)
        x0, y0 = mos.to_px(bbox[0], bbox[3])
        x1, y1 = mos.to_px(bbox[2], bbox[1])
        im = mos.image.crop((int(x0), int(y0), int(x1), int(y1)))
        to_px = lambda lon, lat: (mos.to_px(lon, lat)[0] - int(x0), mos.to_px(lon, lat)[1] - int(y0))
    else:
        pw = 1000
        im = _naip(bbox, pw, int(pw / aspect))
        if im is None:
            raise RuntimeError("no aerial photo available")
        sx, sy = im.size[0] / (bbox[2] - bbox[0]), im.size[1] / (bbox[3] - bbox[1])
        to_px = lambda lon, lat: ((lon - bbox[0]) * sx, (bbox[3] - lat) * sy)

    # upscale first so the lines are drawn crisp at print resolution
    s = max(1.0, min_px / max(im.size))
    if s > 1:
        im = im.resize((round(im.size[0] * s), round(im.size[1] * s)), Image.LANCZOS)
    if lines:
        dr = ImageDraw.Draw(im)
        lw = max(3, round(im.size[0] / 260))
        P = lambda pts: [(x * s, y * s) for x, y in (to_px(*c[:2]) for c in pts)]
        for g in getattr(lot, "geoms", [lot]):
            pts = P(g.exterior.coords)
            dr.line(pts, fill=(15, 23, 42), width=lw + 4, joint="curve")
            dr.line(pts, fill=(255, 255, 255), width=lw, joint="curve")
        if roofs:
            for r in roofs:
                for g in getattr(r, "geoms", [r]):
                    dr.line(P(g.exterior.coords), fill=(0, 229, 255), width=lw, joint="curve")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, dpi=(300, 300))
    return buf.getvalue()


# --------------------------------------------------------------- postcard
def _hero(trade: str | None, d: dict) -> tuple[str, str, list[tuple[str, str]]]:
    """Big number, its caption, and up to three more facts."""
    from .trades import metrics
    ms = [m for m in metrics(trade, d) if not m.get("beta")] if trade else []
    if not ms:
        y = d.get("yard") or {}
        ms = [{"label": "Lot", "value": y.get("lot_sqft"), "unit": "sq ft"}] if y.get("lot_sqft") else []
    from .roofpdf import _metric_text
    nums = [m for m in ms if isinstance(m["value"], (int, float)) and m["value"]]
    if not nums:
        return "", "", []
    h = nums[0]
    big = _metric_text(h)
    # skip zeros and anything that just repeats the big number
    more = [(m["label"], _metric_text(m)) for m in ms
            if m is not h and m.get("unit") not in ("$", "year") and m["value"] not in (0, "0", None, "")
            and _metric_text(m) != big][:3]
    return big, h["label"], more


def _wrap(c, text, x, y, width, font, size, lead, color):
    c.setFont(font, size)
    c.setFillColor(color)
    for line in simpleSplit(text, font, size, width):
        c.drawString(x, y, line)
        y -= lead
    return y


def render_postcard(d: dict, company: dict, trade: str | None = None, headline: str | None = None,
                    offer: str | None = None, photo_jpg: bytes | None = None, cv=None,
                    layout: dict | None = None, price_override: dict | None = None) -> bytes | None:
    """Front + back for one property. Pass ``cv`` to add the pages to an
    existing canvas (a whole mailing in one print file)."""
    own = cv is None
    buf = io.BytesIO()
    c = cv or canvas.Canvas(buf, pagesize=(PW, PH))
    if own:
        c.setTitle(f"Postcard - {d.get('address', '')}")
    accent = colors.HexColor(company.get("brand_color") or "#1d4ed8")
    dark = colors.HexColor("#0f172a")
    from .trades import report_price
    pr = price_override or (report_price(trade, company.get("rates"), d) if trade else None)
    h_def, o_def = copy_for(trade, bool(pr))  # a priced card: no "call for your price"
    headline, offer = (headline or h_def), (offer or o_def)
    lay = layout or studio()["layout"]
    name = company.get("name") or ""
    phone = company.get("phone") or ""
    big, cap, more = _hero(trade, d)
    addr = (d.get("address") or "").strip()
    city = f"{(d.get('city') or '').title()}, NE {d.get('zip') or ''}".strip(", ")

    # ---------------------------------------------------------------- front
    pw = PW * PHOTO_FRAC
    right = lay.get("photo_side") == "right"
    px0 = PW - pw if right else 0          # where the photo sits
    qx0 = 0 if right else pw               # where the colour panel sits
    if photo_jpg is None:
        try:
            photo_jpg = property_photo(d, trade, aspect=pw / PH)
        except Exception:
            photo_jpg = None
    if photo_jpg:
        img = ImageReader(io.BytesIO(photo_jpg))
        iw, ih = img.getSize()
        s = max(pw / iw, PH / ih)                    # cover the panel
        c.saveState()
        p = c.beginPath(); p.rect(px0, 0, pw, PH); c.clipPath(p, stroke=0, fill=0)
        c.drawImage(img, px0 + (pw - iw * s) / 2, (PH - ih * s) / 2, iw * s, ih * s)
        c.restoreState()
    else:
        c.setFillColor(colors.HexColor("#e2e8f0")); c.rect(px0, 0, pw, PH, stroke=0, fill=1)
    # address tag on the photo
    if addr and lay.get("show_address_tag", True):
        c.setFont("Helvetica-Bold", 11)
        tw = c.stringWidth(addr, "Helvetica-Bold", 11)
        c.setFillColor(colors.Color(0.06, 0.09, 0.16, alpha=0.82))
        tx = (PW - SAFE - tw - 20) if right else SAFE
        c.roundRect(tx, PH - SAFE - 26, tw + 20, 24, 6, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.drawString(tx + 10, PH - SAFE - 18, addr)
    # colour panel
    c.setFillColor(accent)
    c.rect(qx0, 0, PW - pw, PH, stroke=0, fill=1)
    x = qx0 + (SAFE if right else 0.28 * IN)
    width = PW - pw - 0.28 * IN - SAFE
    y = PH - SAFE - 16
    y = _wrap(c, headline, x, y, width, "Helvetica-Bold", 21, 24, colors.white) - 14
    if big:
        num, _, unit = big.partition(" ")
        size = 46 if len(num) <= 6 else 38
        base = y - size * 0.78
        c.setFont("Helvetica-Bold", size); c.setFillColor(colors.white)
        c.drawString(x, base, num)
        if unit:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(x + c.stringWidth(num, "Helvetica-Bold", size) + 6, base, unit)
        y = _wrap(c, cap, x, base - 20, width, "Helvetica", 12.5, 15, colors.white) - 10
    for k, v in (more if lay.get("show_facts", True) else []):
        c.setFont("Helvetica", 10.5); c.setFillColor(colors.Color(1, 1, 1, alpha=0.88))
        c.drawString(x, y, k)
        c.setFont("Helvetica-Bold", 10.5); c.setFillColor(colors.white)
        c.drawRightString(x + width, y, v)
        y -= 15
    # their price for this home, from the company's rates
    yb = SAFE + 4
    if pr:
        bh = 52
        top = SAFE + 66 + bh
        c.setFillColor(colors.white)
        c.roundRect(x, top - bh, width, bh, 8, stroke=0, fill=1)
        c.setFillColor(accent); c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 10, top - 15, "YOUR PRICE")
        num = f"${pr['price']:,.0f}" if pr["price"] >= 20 else f"${pr['price']:,.2f}"
        c.setFont("Helvetica-Bold", 24); c.setFillColor(dark)
        c.drawString(x + 10, top - 42, num)
        c.setFont("Helvetica", 11)
        c.drawString(x + 16 + c.stringWidth(num, "Helvetica-Bold", 24), top - 41, pr["label"][:24])
    # company at the bottom of the panel
    if phone:
        c.setFont("Helvetica-Bold", 20); c.setFillColor(colors.white)
        c.drawString(x, yb, phone); yb += 24
    if name:
        _wrap(c, name, x, yb, width, "Helvetica-Bold", 13, 15, colors.white)
    c.showPage()

    # ----------------------------------------------------------------- back
    c.setFillColor(colors.white); c.rect(0, 0, PW, PH, stroke=0, fill=1)
    c.setFillColor(accent); c.rect(0, PH - BLEED - 0.12 * IN, PW, 0.12 * IN + BLEED, stroke=0, fill=1)
    mid = PW / 2
    # left: the message
    x, width = SAFE + 4, mid - SAFE - 0.3 * IN
    y = PH - SAFE - 30
    y = _wrap(c, offer, x, y, width, "Helvetica-Bold", 16, 20, dark) - 10
    if big:
        facts = [(cap, big)] + more
        for k, v in facts[:4]:
            c.setFillColor(colors.HexColor("#f1f5f9")); c.roundRect(x, y - 7, width, 22, 5, stroke=0, fill=1)
            c.setFont("Helvetica", 10); c.setFillColor(dark); c.drawString(x + 8, y, k[:48])
            c.setFont("Helvetica-Bold", 10); c.drawRightString(x + width - 8, y, v)
            y -= 27
    if pr:
        c.setFillColor(accent); c.roundRect(x, y - 9, width, 26, 5, stroke=0, fill=1)
        c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 11.5)
        c.drawString(x + 8, y, "Your price")
        c.drawRightString(x + width - 8, y, pr["text"])
        y -= 32
    y -= 6
    if name:
        y = _wrap(c, name, x, y, width, "Helvetica-Bold", 14, 17, accent)
    if phone:
        c.setFont("Helvetica-Bold", 18); c.setFillColor(dark); c.drawString(x, y - 6, phone); y -= 30
    em = company.get("email")
    if em:
        c.setFont("Helvetica", 10); c.setFillColor(dark); c.drawString(x, y, em)
    c.setFont("Helvetica", 7); c.setFillColor(colors.HexColor("#64748b"))
    c.drawString(x, SAFE + 2, "Measured from aerial photos and LiDAR. Estimates; final price confirmed on site.")
    # right: postage and address. Bottom 5/8 in stays clear for the USPS barcode.
    c.setStrokeColor(colors.HexColor("#94a3b8")); c.setDash(3, 3)
    sx, sy = PW - SAFE - 1.0 * IN, PH - SAFE - 0.95 * IN
    c.rect(sx, sy, 1.0 * IN, 0.8 * IN, stroke=1, fill=0)
    c.setDash()
    c.setFont("Helvetica", 7); c.setFillColor(colors.HexColor("#64748b"))
    for i, t in enumerate(("POSTAGE", "or permit", "indicia")):
        c.drawCentredString(sx + 0.5 * IN, sy + 0.5 * IN - i * 9, t)
    ax, ay = mid + 0.55 * IN, PH * 0.47
    c.setFillColor(dark); c.setFont("Helvetica", 11 if len(owner_of(d)) <= 30 else 9.5)
    who = owner_of(d)
    who = f"{who} or Current Resident" if who and len(who) <= 30 else (who or "Current Resident")
    for t in (who, addr, city):
        if t:
            c.drawString(ax, ay, t); ay -= 15
    c.setStrokeColor(colors.HexColor("#e2e8f0")); c.line(mid, SAFE + 0.625 * IN, mid, PH - SAFE - 0.3 * IN)
    c.showPage()
    if own:
        c.save()
        return buf.getvalue()
    return None


def mailing_canvas(buf):
    """A canvas for a whole mailing: front/back pairs, one property after another."""
    c = canvas.Canvas(buf, pagesize=(PW, PH))
    c.setTitle("Postcard mailing - print file")
    return c


# ------------------------------------------------------------- mail files
MAIL_DIR = None
BATCH = 250   # postcards per print PDF


def _mail_dir():
    global MAIL_DIR
    if MAIL_DIR is None:
        from .pricing import DATA_DIR
        MAIL_DIR = DATA_DIR / "mailings"
        MAIL_DIR.mkdir(exist_ok=True)
    return MAIL_DIR


MERGE_COLS = ["owner", "address", "city", "zip", "your_price", "price_label", "photo", "headline", "offer",
              "big_number", "big_label",
              "fact1_label", "fact1", "fact2_label", "fact2", "fact3_label", "fact3"]


def build_mailing(src_zip, out_path, company: dict, trade: str | None, headline: str | None = None,
                  offer: str | None = None, progress=None) -> dict:
    """A mail file for a Premier folder, branded for the buyer:
       print/postcards_001.pdf ...   print-ready 9x6 in cards (front+back per home),
                                     {BATCH} homes per file
       photos/*.jpg                  the aerial photos (for your own design)
       mail_merge.csv                one row per home, for Canva / InDesign / a mail house
    Built from what the folder already holds: no re-measuring."""
    import csv as _csv
    import json as _json
    import zipfile as _zf
    h_def, o_def = copy_for(trade, False)
    custom_offer = offer
    headline, offer = headline or h_def, offer or o_def
    with _zf.ZipFile(src_zip) as src:
        stems = sorted(n[len("mail/data/"):-5] for n in src.namelist() if n.startswith("mail/data/") and n.endswith(".json"))
        photos = {n[len("mail/photos/"):-4] for n in src.namelist() if n.startswith("mail/photos/")}
        total = len(stems)
        tmp = out_path.with_suffix(".building")
        rows = []
        with _zf.ZipFile(tmp, "w", compression=_zf.ZIP_STORED) as out:
            for b0 in range(0, total, BATCH):
                buf = io.BytesIO()
                cv = mailing_canvas(buf)
                for i, stem in enumerate(stems[b0:b0 + BATCH], b0 + 1):
                    d = _json.loads(src.read(f"mail/data/{stem}.json"))
                    jpg = src.read(f"mail/photos/{stem}.jpg") if stem in photos else None
                    if jpg:
                        render_postcard(d, company, trade, headline, custom_offer, photo_jpg=jpg, cv=cv)
                        out.writestr(f"photos/{stem}.jpg", jpg)
                    big, cap, more = _hero(trade, d)
                    from .trades import report_price
                    pr = report_price(trade, company.get("rates"), d) if trade else None
                    r = {"owner": person_name(d.get("owner")), "your_price": pr["price"] if pr else "",
                         "price_label": pr["label"] if pr else "",
                         "offer": custom_offer or copy_for(trade, bool(pr))[1],
                         "address": d.get("address"), "city": (d.get("city") or "").title(), "zip": d.get("zip"),
                         "photo": f"photos/{stem}.jpg" if jpg else "", "headline": headline,
                         "big_number": big, "big_label": cap}
                    for j, (k, v) in enumerate(more, 1):
                        r[f"fact{j}_label"], r[f"fact{j}"] = k, v
                    rows.append(r)
                    if progress and (i % 10 == 0 or i == total):
                        progress(i, total)
                cv.save()
                out.writestr(f"print/postcards_{b0 // BATCH + 1:03d}.pdf", buf.getvalue())
            cbuf = io.StringIO()
            w = _csv.DictWriter(cbuf, fieldnames=MERGE_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            out.writestr("mail_merge.csv", cbuf.getvalue())
            out.writestr("READ ME.txt", MAIL_README.format(n=total, batch=BATCH))
        tmp.replace(out_path)
    return {"homes": total, "size": out_path.stat().st_size}


MAIL_README = """Postcard mail file ({n} homes)

print/       Print-ready postcards, 9 x 6 in with 1/8 in bleed on every side
             (the PDF pages are 9.25 x 6.25 in). Each home is two pages: front, then back.
             {batch} homes per file. Send these to any print shop or mail house.
             The back has a "Current Resident" address panel and a box for your postage
             stamp or bulk-mail permit (indicia). Keep the bottom 5/8 in of the address
             side clear: that's where USPS prints its barcode.

photos/      The aerial photo of each home, for making your own design.

mail_merge.csv  One row per home (address, photo file, the numbers). Load it into
             Canva Bulk Create, InDesign Data Merge or your mail house's system to
             build your own design with each home's photo and measurements.

Measurements are estimates from aerial photos and LiDAR.
"""
