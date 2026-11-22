"""PDF version of a roof report (reportlab)."""
from __future__ import annotations

import base64
import io
import math
import time

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

LINE_COLORS = {
    "ridge": "#dc2626", "hip": "#ea580c", "valley": "#2563eb", "rake": "#16a34a",
    "eave": "#a16207", "step_flashing": "#9333ea", "flashing": "#0891b2",
}
ORDER = ["ridge", "hip", "valley", "rake", "eave", "step_flashing", "flashing"]
W, H = letter
M = 40


def _hex(c):
    return colors.HexColor(c)


def _header(c, rep, company, page, total):
    accent = _hex(company.get("brand_color") or "#1d4ed8")
    c.setFillColor(accent)
    c.rect(0, H - 54, W, 54, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(M, H - 34, company.get("name") or "Roof Report")
    c.setFont("Helvetica", 9)
    right = " · ".join(x for x in [company.get("phone"), company.get("email")] if x)
    c.drawRightString(W - M, H - 34, right)
    c.setFillColor(colors.HexColor("#64748b"))
    c.setFont("Helvetica", 8)
    c.drawString(M, 24, f"Report {rep['id']} · {rep['data']['address']} · {time.strftime('%b %d, %Y', time.localtime(rep['created']))}")
    c.drawRightString(W - M, 24, f"Page {page} of {total}")


def _title(c, text, y):
    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 17)
    c.drawString(M, y, text)


def _kv_table(c, rows, x, y, col_w=(150, 90), lh=17, bold_first=False):
    c.setFont("Helvetica", 10)
    for i, (k, v) in enumerate(rows):
        if i % 2 == 0:
            c.setFillColor(colors.HexColor("#f1f5f9"))
            c.rect(x - 4, y - 5, sum(col_w) + 8, lh, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont("Helvetica-Bold" if (bold_first and i == 0) else "Helvetica", 10)
        c.drawString(x, y, str(k))
        c.drawRightString(x + sum(col_w), y, str(v))
        y -= lh
    return y


def _ft(v):
    return f"{v:,.0f} ft"


def render_pdf(rep: dict, company: dict) -> bytes:
    d = rep["data"]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Roof Report - {d['address']}")
    total_pages = 3

    # ---------------------------------------------------------------- page 1
    _header(c, rep, company, 1, total_pages)
    _title(c, "Roof Measurement Report", H - 92)
    c.setFont("Helvetica", 11)
    c.setFillColor(colors.HexColor("#334155"))
    c.drawString(M, H - 110, f"{d['address']}, {(d.get('city') or '').title()} NE {d.get('zip') or ''}".strip())
    c.drawString(M, H - 124, f"Parcel {d['pin']} · Report {rep['id']} · {time.strftime('%B %d, %Y', time.localtime(rep['created']))}")
    img_top = H - 140
    if d.get("aerial_jpg"):
        img = ImageReader(io.BytesIO(base64.b64decode(d["aerial_jpg"])))
        iw, ih = img.getSize()
        bw, bh = W - 2 * M, 300
        s = min(bw / iw, bh / ih)
        c.drawImage(img, M + (bw - iw * s) / 2, img_top - ih * s, iw * s, ih * s)
        img_bottom = img_top - ih * s
    else:
        img_bottom = img_top
    y = img_bottom - 26
    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(M, y, "Summary")
    c.drawString(W / 2 + 10, y, "Lengths")
    y -= 20
    L = d["lines"]
    _kv_table(c, [
        ("Total roof area", f"{d['roof_sqft']:,} sq ft"),
        ("Squares (no waste)", f"{d['squares']:.2f}"),
        ("Predominant pitch", d["pitch_label"]),
        ("Roof facets", d["facets"] or "—"),
        ("Structures", len(d["structures"])),
        ("Suggested waste", f"{d['suggested_waste']}%"),
        ("Squares w/ suggested waste", next(f"{w['squares']:.2f}" for w in d["waste"] if w["suggested"])),
    ], M, y, col_w=(160, 80))
    _kv_table(c, [
        ("Ridges", _ft(L["ridge"])), ("Hips", _ft(L["hip"])), ("Valleys", _ft(L["valley"])),
        ("Rakes", _ft(L["rake"])), ("Eaves", _ft(L["eave"])), ("Step flashing", _ft(L["step_flashing"])),
        ("Flashing", _ft(L.get("flashing", 0))),
        ("Drip edge (eaves + rakes)", _ft(d["derived"]["drip_edge"])),
        ("Ridge cap (ridges + hips)", _ft(d["derived"]["ridge_cap"])),
    ], W / 2 + 10, y, col_w=(170, 70))
    c.showPage()

    # ---------------------------------------------------------------- page 2
    _header(c, rep, company, 2, total_pages)
    _title(c, "Length Diagram", H - 92)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(M, H - 108, "True lengths in feet, using each roof plane's own pitch. North is up.")
    box = (M, 110, W - 2 * M, H - 240)
    _draw_diagram(c, d, box)
    lx, ly = M, 84
    for k in ORDER:
        c.setStrokeColor(_hex(LINE_COLORS[k]))
        c.setLineWidth(3)
        c.line(lx, ly + 3, lx + 18, ly + 3)
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont("Helvetica", 9)
        lab = f"{d['line_labels'][k]} {L.get(k, 0):,.0f}'"
        c.drawString(lx + 22, ly, lab)
        lx += 30 + c.stringWidth(lab, "Helvetica", 9) + 8
    c.showPage()

    # ---------------------------------------------------------------- page 3
    _header(c, rep, company, 3, total_pages)
    _title(c, "Waste Calculation", H - 92)
    y = H - 122
    cols = [M, M + 120, M + 260, M + 400]
    c.setFont("Helvetica-Bold", 10)
    for x, t in zip(cols, ["Waste %", "Area (sq ft)", "Squares", ""]):
        c.drawString(x, y, t)
    y -= 6
    c.setStrokeColor(colors.HexColor("#cbd5e1"))
    c.line(M, y, W - M, y)
    y -= 16
    for w in d["waste"]:
        if w["suggested"]:
            c.setFillColor(colors.HexColor("#dcfce7"))
            c.rect(M - 4, y - 5, W - 2 * M + 8, 17, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont("Helvetica-Bold" if w["suggested"] else "Helvetica", 10)
        c.drawString(cols[0], y, f"{w['pct']}%")
        c.drawString(cols[1], y, f"{w['area']:,}")
        c.drawString(cols[2], y, f"{w['squares']:.2f}")
        if w["suggested"]:
            c.drawString(cols[3], y, "Suggested")
        y -= 17
    c.setFont("Helvetica", 8.5)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(M, y - 4, "Squares are rounded up to the nearest 1/3 square (one bundle).")
    y -= 36
    _title(c, "Areas per Pitch", y)
    y -= 22
    c.setFont("Helvetica-Bold", 10)
    for x, t in zip(cols, ["Pitch", "Area (sq ft)", "% of roof"]):
        c.drawString(x, y, t)
    y -= 16
    c.setFont("Helvetica", 10)
    for row in d.get("pitch_table", []):
        c.drawString(cols[0], y, row["pitch"])
        c.drawString(cols[1], y, f"{row['area']:,}")
        c.drawString(cols[2], y, f"{row['pct']}%")
        y -= 15
    y -= 22
    _title(c, "Structures", y)
    y -= 26
    c.setFont("Helvetica-Bold", 9.5)
    hdr = ["Structure", "Roof sq ft", "Facets", "Ridges", "Hips", "Valleys", "Rakes", "Eaves"]
    xs = [M, M + 130, M + 200, M + 250, M + 305, M + 360, M + 420, M + 475]
    for x, t in zip(xs, hdr):
        c.drawString(x, y, t)
    y -= 16
    c.setFont("Helvetica", 9.5)
    for s in d["structures"]:
        vals = [s["name"], f"{s['roof_sqft']:,}", s["facets"] or "—"] + [f"{s['lines'][k]:,.0f}" for k in ("ridge", "hip", "valley", "rake", "eave")]
        for x, t in zip(xs, vals):
            c.drawString(x, y, str(t))
        y -= 15
    y -= 22
    _title(c, "Notes", y)
    y -= 18
    notes = [
        "Pitch is measured per roof plane from LiDAR and rounded to the nearest 1/12. Areas and sloped",
        "lengths (rakes, hips, valleys, step flashing) use each plane's own pitch; ridges and eaves are level.",
        "Roof outlines: Douglas County 2022 building footprints. Roof planes and lines: Douglas County",
        "2022 QL1 LiDAR. Photo: Douglas County 2025 orthoimagery.",
        "Additions or re-roofs after April 2022 are not reflected. Verify on site before ordering.",
    ]
    if d.get("method") != "lidar":
        notes.append("Part of this roof had no usable LiDAR; its edges are counted as eaves from the outline.")
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#334155"))
    for n in notes:
        c.drawString(M, y, n)
        y -= 13
    c.showPage()
    c.save()
    return buf.getvalue()


def _draw_diagram(c, d, box):
    bx, by, bw, bh = box
    xs, ys = [], []
    for s in d["structures"]:
        for ring in s["outline"]:
            xs += [p[0] for p in ring]
            ys += [p[1] for p in ring]
    if not xs:
        return
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sc = min(bw / max(maxx - minx, 1), bh / max(maxy - miny, 1)) * 0.92
    ox = bx + (bw - (maxx - minx) * sc) / 2
    oy = by + (bh - (maxy - miny) * sc) / 2
    P = lambda p: (ox + (p[0] - minx) * sc, oy + (p[1] - miny) * sc)
    for s in d["structures"]:
        for ring in s["outline"]:
            path = c.beginPath()
            x0, y0 = P(ring[0])
            path.moveTo(x0, y0)
            for p in ring[1:]:
                path.lineTo(*P(p))
            path.close()
            c.setFillColor(colors.HexColor("#f8fafc"))
            c.setStrokeColor(colors.HexColor("#cbd5e1"))
            c.setLineWidth(0.6)
            c.drawPath(path, stroke=1, fill=1)
    for s in d["structures"]:
        for seg in s["segments"]:
            a, b = P(seg["a"]), P(seg["b"])
            c.setStrokeColor(_hex(LINE_COLORS[seg["type"]]))
            c.setLineWidth(2.2)
            c.line(*a, *b)
    # length labels on longer lines
    c.setFont("Helvetica-Bold", 7.5)
    for s in d["structures"]:
        for seg in s["segments"]:
            if seg["len"] < 6:
                continue
            a, b = P(seg["a"]), P(seg["b"])
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            txt = f"{seg['len']:.0f}"
            tw = c.stringWidth(txt, "Helvetica-Bold", 7.5)
            c.setFillColor(colors.white)
            c.rect(mx - tw / 2 - 1.5, my - 3, tw + 3, 9, stroke=0, fill=1)
            c.setFillColor(_hex(LINE_COLORS[seg["type"]]))
            c.drawCentredString(mx, my - 1, txt)
    # north arrow
    c.setFillColor(colors.HexColor("#0f172a"))
    nx, ny = bx + bw - 20, by + bh - 30
    c.line(nx, ny, nx, ny + 22)
    c.drawCentredString(nx, ny + 26, "N")
