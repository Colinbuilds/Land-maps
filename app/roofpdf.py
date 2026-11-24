"""PDF version of a property report (reportlab). Pages follow the sections
the company's trades unlock (see trades.filter_report)."""
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
    c.drawString(M, H - 34, company.get("name") or "Property Report")
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


def render_pdf(rep: dict, company: dict, trades: list[str] | None = None) -> bytes:
    d = rep["data"]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Property Report - {d['address']}")
    has_facets = any(st.get("facet_polys") for st in d["structures"])
    extra = []  # pages after the first three, in order
    if trades:
        extra.append("trades")
    if has_facets:
        extra += ["pitch", "area"]
    if d.get("walls"):
        extra.append("walls")
        if (d["walls"].get("elevations") or {}):
            extra.append("elev")
    if d.get("yard") and not d["yard"].get("basic"):
        extra.append("yard")
    has_roof = bool(d.get("lines"))
    first = 3 if has_roof else 1
    total_pages = first + len(extra)

    if not has_roof:
        _cover_page(c, rep, company, d, total_pages)
    else:
        # ---------------------------------------------------------------- page 1
        _header(c, rep, company, 1, total_pages)
        _title(c, "Property Measurement Report", H - 92)
        c.setFont("Helvetica", 11)
        c.setFillColor(colors.HexColor("#334155"))
        c.drawString(M, H - 110, f"{d['address']}, {(d.get('city') or '').title()} NE {d.get('zip') or ''}".strip()
                     + (f"  ({d['county_name']})" if d.get("county_name") else ""))
        c.drawString(M, H - 124, f"Parcel {str(d['pin']).split(':')[-1]} · Report {rep['id']} · {time.strftime('%B %d, %Y', time.localtime(rep['created']))}")
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
            ("Under tree canopy", f"{d.get('tree_overhang_sqft', 0):,} sq ft"),
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
        if d.get("tree_overhang_sqft"):
            c.setFillColor(colors.HexColor("#15803d"))
            c.setFont("Helvetica", 9)
            c.drawString(M, ly - 18, f"Green hatch: roof under tree canopy ({d['tree_overhang_sqft']:,} sq ft). "
                                     "Dashed lines run under trees: verify on site.")
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
        src = d.get("sources") or {}
        notes = [
            "Pitch is measured per roof plane from LiDAR and rounded to the nearest 1/12. Areas and sloped",
            "lengths (rakes, hips, valleys, step flashing) use each plane's own pitch; ridges and eaves are level.",
            f"Roof outlines: {src.get('outlines') or 'county building footprints'}.",
            f"Roof planes and lines: {src.get('lidar') or 'LiDAR'}. Photo: {src.get('imagery') or 'aerial imagery'}.",
            f"Changes made after the {src.get('lidar_year') or 'LiDAR'} scan are not reflected. Verify on site before ordering.",
        ]
        if d.get("method") != "lidar":
            notes.append("Part of this roof had no usable LiDAR; its edges are counted as eaves from the outline.")
        if d.get("tree_note"):
            import textwrap
            notes += textwrap.wrap(d["tree_note"], 112)
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#334155"))
        for n in notes:
            c.drawString(M, y, n)
            y -= 13
        c.showPage()
    for i, kind in enumerate(extra):
        pg = first + 1 + i
        if kind == "trades":
            _trades_page(c, rep, company, d, trades, pg, total_pages)
        elif kind in ("pitch", "area"):
            _facet_page(c, rep, company, d, kind, pg, total_pages)
        elif kind == "walls":
            _walls_page(c, rep, company, d["walls"], total_pages, pg)
        elif kind == "elev":
            _elev_page(c, rep, company, d["walls"], pg, total_pages)
        elif kind == "yard":
            _yard_page(c, rep, company, d["yard"], pg, total_pages)
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
    sc = min((bw - 30) / max(maxx - minx, 1), bh / max(maxy - miny, 1)) * 0.92
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
    # tree canopy over the roof: green, hatched
    for s in d["structures"]:
        for ring in s.get("tree_cover") or []:
            path = c.beginPath()
            path.moveTo(*P(ring[0]))
            for p in ring[1:]:
                path.lineTo(*P(p))
            path.close()
            c.saveState()
            c.setFillColor(colors.HexColor("#16a34a"))
            c.setFillAlpha(0.15)
            c.setStrokeColor(colors.HexColor("#15803d"))
            c.setLineWidth(0.5)
            c.drawPath(path, stroke=1, fill=1)
            c.clipPath(path, stroke=0, fill=0)
            c.setStrokeAlpha(0.6)
            for k in range(-int(bh), int(bw), 5):
                c.line(bx + k, by, bx + k + bh, by + bh)
            c.restoreState()
    for s in d["structures"]:
        for seg in s["segments"]:
            a, b = P(seg["a"]), P(seg["b"])
            c.setStrokeColor(_hex(LINE_COLORS[seg["type"]]))
            c.setLineWidth(2.2)
            c.setDash(4, 2) if seg.get("tree") else c.setDash()
            c.line(*a, *b)
    c.setDash()
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
    nx, ny = bx + bw - 8, by + bh + 2  # above the drawing, clear of the roof
    c.line(nx, ny, nx, ny + 22)
    c.drawCentredString(nx, ny + 26, "N")


def _walls_page(c, rep, company, w, total_pages, page=4):
    """Siding / gutter / trim measurements."""
    import textwrap
    _header(c, rep, company, page, total_pages)
    _title(c, "Walls & Exterior", H - 92)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(M, H - 108, "For siding, gutter, trim and window work. Square feet unless noted.")
    y = H - 140
    n = lambda v: f"{round(v):,}"
    sides = [(f"{s} walls", n(w["by_side"][s])) for s in ("North", "East", "South", "West")]
    _kv_table(c, [("Total wall area (gross)", f"{n(w['gross_wall_sqft'])} sq ft")] + sides + [
        ("Windows & doors (estimate)", f"~{w['est_openings']} / ~{n(w['est_opening_sqft'])} sq ft"),
        ("Net wall area (estimate)", n(w["est_net_wall_sqft"])),
    ], M, y, col_w=(170, 80))
    _kv_table(c, [
        ("Gutters (eaves)", f"{n(w['gutter_ft'])} ft"),
        ("Downspouts (estimate)", str(w["downspouts"])),
        ("Fascia (eaves + rakes)", f"{n(w['fascia_ft'])} ft"),
        (f"Soffit (at {w['overhang_ft']:g} ft overhang)", n(w["soffit_sqft"])),
        ("Outside corners", f"{n(w['outside_corners_ft'])} ft"),
        ("Inside corners", f"{n(w['inside_corners_ft'])} ft"),
    ], W / 2 + 10, y, col_w=(170, 70))
    y -= 7 * 17 + 34
    _title(c, "Siding Waste", y)
    y -= 22
    c.setFont("Helvetica-Bold", 9.5)
    c.setFillColor(colors.HexColor("#0f172a"))
    for i, h in enumerate(("Waste", "Area (sq ft)", "Squares")):
        c.drawString(M + i * 120, y, h)
    c.setFont("Helvetica", 9.5)
    for row in w["siding_squares"]:
        y -= 16
        for i, v in enumerate((f"{row['pct']}%", n(row["sqft"]), f"{row['squares']:.1f}")):
            c.drawString(M + i * 120, y, v)
    y -= 30
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#334155"))
    for line in textwrap.wrap(w["note"], 112):
        c.drawString(M, y, line)
        y -= 13
    c.showPage()


_PITCH_FILL = ["#e0f2fe", "#dcfce7", "#fef9c3", "#ffedd5", "#fce7f3", "#ede9fe", "#e2e8f0"]


def _facet_page(c, rep, company, d, mode, page, total_pages):
    _header(c, rep, company, page, total_pages)
    _title(c, "Pitch Diagram" if mode == "pitch" else "Facet Areas", H - 92)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(M, H - 108, "Each facet's measured pitch (rise per 12 in.); arrows point downslope. North is up." if mode == "pitch"
                 else "Facets lettered smallest (A) to largest, with sloped area in sq ft. North is up.")
    facets = [(st, f) for st in d["structures"] for f in (st.get("facet_polys") or [])]
    box_h = H - 240 if mode == "pitch" else H - 330
    bx, by, bw, bh = M, H - 130 - box_h, W - 2 * M, box_h
    xs = [x for _, f in facets for ring in f["poly"] for x, _ in ring]
    ys = [y for _, f in facets for ring in f["poly"] for _, y in ring]
    if not xs:
        c.showPage()
        return
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sc = min(bw / max(maxx - minx, 1), bh / max(maxy - miny, 1)) * 0.9
    ox = bx + (bw - (maxx - minx) * sc) / 2
    oy = by + (bh - (maxy - miny) * sc) / 2
    P = lambda x, y: (ox + (x - minx) * sc, oy + (y - miny) * sc)
    pitches = sorted({f["pitch"] for _, f in facets})
    for _, f in facets:
        fill = _PITCH_FILL[pitches.index(f["pitch"]) % len(_PITCH_FILL)] if mode == "pitch" else "#f1f5f9"
        for ring in f["poly"]:
            path = c.beginPath()
            path.moveTo(*P(*ring[0]))
            for x, y in ring[1:]:
                path.lineTo(*P(x, y))
            path.close()
            c.setFillColor(colors.HexColor(fill))
            c.setStrokeColor(colors.HexColor("#334155"))
            c.setLineWidth(0.8)
            c.drawPath(path, stroke=1, fill=1)
    for _, f in facets:
        cx, cy = P(*f["c"])
        c.setFillColor(colors.HexColor("#0f172a"))
        if mode == "pitch":
            c.setFont("Helvetica-Bold", 9)
            c.drawCentredString(cx, cy + 2, f"{f['pitch']:g}/12" if f["pitch"] else "Flat")
            if f.get("down"):
                L = 9
                dx, dy = f["down"][0] * L, f["down"][1] * L
                c.setStrokeColor(colors.HexColor("#0f172a"))
                c.setLineWidth(1)
                x0, y0 = cx - dx / 2, cy - 8 - dy / 2
                x1, y1 = cx + dx / 2, cy - 8 + dy / 2
                c.line(x0, y0, x1, y1)
                import math
                a = math.atan2(y1 - y0, x1 - x0)
                for s_ in (2.6, -2.6):
                    c.line(x1, y1, x1 - 3.5 * math.cos(a + s_ / 6), y1 - 3.5 * math.sin(a + s_ / 6))
        else:
            c.setFont("Helvetica-Bold", 10)
            c.drawCentredString(cx, cy + 3, f["label"])
            c.setFont("Helvetica", 8)
            c.drawCentredString(cx, cy - 7, f"{round(f['area_sqft']):,}")
    if mode == "area":
        y = by - 22
        c.setFont("Helvetica-Bold", 9.5)
        c.setFillColor(colors.HexColor("#0f172a"))
        for i, h in enumerate(("Facet", "Pitch", "Area (sq ft)")):
            c.drawString(M + i * 110, y, h)
        c.setFont("Helvetica", 9.5)
        for st, f in facets:
            y -= 14
            if y < 60:
                break
            name = f["label"] + (f"  ({st['name']})" if len(d["structures"]) > 1 else "")
            for i, v in enumerate((name, f"{f['pitch']:g}/12" if f["pitch"] else "Flat", f"{round(f['area_sqft']):,}")):
                c.drawString(M + i * 110, y, v)
    else:
        lx, ly = M, by - 18
        c.setFont("Helvetica", 9)
        for i, p_ in enumerate(pitches):
            c.setFillColor(colors.HexColor(_PITCH_FILL[i % len(_PITCH_FILL)]))
            c.setStrokeColor(colors.HexColor("#94a3b8"))
            c.rect(lx, ly - 1, 12, 9, stroke=1, fill=1)
            c.setFillColor(colors.HexColor("#0f172a"))
            lab = f"{p_:g}/12" if p_ else "Flat"
            c.drawString(lx + 16, ly, lab)
            lx += 30 + c.stringWidth(lab, "Helvetica", 9)
    c.showPage()


def _ftin(v):
    f = int(v + 1e-6)
    i = round((v - f) * 12)
    if i == 12:
        return f"{f + 1}'"
    return f"{f}' {i}\"" if i else f"{f}'"


def _elev_page(c, rep, company, w, page, total_pages):
    _header(c, rep, company, page, total_pages)
    _title(c, "Elevations", H - 92)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(M, H - 108, "Walls as seen from each side. Lengths along the bottom; eave and peak heights on the walls. Same scale throughout.")
    E = w["elevations"]
    sides = [s_ for s_ in ("North", "East", "South", "West") if s_ in E]
    maxW = max(p_[0] for s_ in sides for wl in E[s_] for p_ in wl["pts"])
    maxH = max(p_[1] for s_ in sides for wl in E[s_] for p_ in wl["pts"])
    rowh = (H - 170) / max(1, len(sides))
    sc = min((W - 2 * M - 10) / max(maxW, 1), (rowh - 46) / max(maxH, 1))
    for i, side in enumerate(sides):
        top = H - 130 - i * rowh
        base = top - rowh + 30
        c.setFont("Helvetica-Bold", 10.5)
        c.setFillColor(colors.HexColor("#0f172a"))
        c.drawString(M, top - 4, side)
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#475569"))
        c.drawString(M + 46, top - 4, f"{round(w['by_side'][side]):,} sq ft of wall")
        c.setStrokeColor(colors.HexColor("#64748b"))
        c.setLineWidth(0.8)
        c.line(M, base, M + maxW * sc + 10, base)
        X = lambda x: M + 5 + x * sc
        for wl in E[side]:
            pts = wl["pts"]
            path = c.beginPath()
            path.moveTo(X(pts[0][0]), base)
            for x, h in pts:
                path.lineTo(X(x), base + h * sc)
            path.lineTo(X(pts[-1][0]), base)
            path.close()
            c.setFillColor(colors.HexColor("#f1f5f9"))
            c.setStrokeColor(colors.HexColor("#1e293b"))
            c.setLineWidth(0.9)
            c.drawPath(path, stroke=1, fill=1)
        walls_ = E[side]
        for wi, wl in enumerate(walls_):
            pts = wl["pts"]
            a0, a1 = pts[0][0], pts[-1][0]
            if any(min(a1, o["pts"][-1][0]) - max(a0, o["pts"][0][0]) > 0.5 * (a1 - a0) for o in walls_[wi + 1:]):
                continue  # mostly behind a nearer wall
            x0, x1 = X(pts[0][0]), X(pts[-1][0])
            span = pts[-1][0] - pts[0][0]
            if span < 5 or x1 - x0 < 18:  # too narrow to label legibly
                continue
            hl, hr = pts[0][1], pts[-1][1]
            peak = max(pts, key=lambda q: q[1])
            c.setFillColor(colors.HexColor("#b91c1c"))
            c.setFont("Helvetica-Bold", 8.5)
            c.drawCentredString((x0 + x1) / 2, base + min(hl, hr) * sc / 2 - 3, wl.get("label", ""))
            c.setFillColor(colors.HexColor("#334155"))
            c.setFont("Helvetica", 7.5)
            c.drawCentredString((x0 + x1) / 2, base - 10, _ftin(wl.get("length") or (pts[-1][0] - pts[0][0])))
            if span >= 8:
                c.drawString(x0 + 2, base + hl * sc - 9, _ftin(hl))
                if abs(hr - hl) > 0.6:
                    c.drawRightString(x1 - 2, base + hr * sc - 9, _ftin(hr))
            if peak[1] - max(hl, hr) > 1.0:
                c.drawCentredString(X(peak[0]), base + peak[1] * sc + 3, _ftin(peak[1]))
    c.showPage()


def _yard_page(c, rep, company, y, page, total_pages):
    _header(c, rep, company, page, total_pages)
    _title(c, "Lot & Yard", H - 92)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(M, H - 108, "For lawn, snow, concrete and landscape work. Square feet.")
    top = H - 124
    if y.get("photo_jpg"):
        img = ImageReader(io.BytesIO(base64.b64decode(y["photo_jpg"])))
        iw, ih = img.getSize()
        side = 300
        s_ = min(side / iw, side / ih)
        c.drawImage(img, M, top - ih * s_, iw * s_, ih * s_)
    n = lambda v: "-" if v is None else f"{round(v):,}"
    rows = [("#22c55e", "Lawn (grass only)", n(y.get("lawn_sqft"))), ("#86efac", "Parkway grass", n(y.get("parkway_sqft"))),
            ("#3b82f6", "Driveway", n(y.get("driveway_sqft"))), ("#ec4899", "Public sidewalk", n(y.get("sidewalk_sqft"))),
            ("#eab308", "Patios & walks", n(y.get("other_hardscape_sqft"))), ("#ef4444", "Decks, sheds, play sets", n(y.get("structures_sqft"))),
            ("#a855f7", "Landscape beds", n(y.get("beds_sqft"))), ("#64748b", "Buildings", n(y.get("building_sqft"))),
            (None, "Trees on the lot", str(y.get("tree_count") if y.get("tree_count") is not None else "-")),
            (None, "Street frontage", n(y.get("frontage_ft")) + " ft"), (None, "Lot", n(y.get("lot_sqft")))]
    x0, yy = M + 320, top - 12
    for col, k, v in rows:
        if col:
            c.setFillColor(colors.HexColor(col))
            c.rect(x0, yy - 1, 9, 9, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont("Helvetica-Bold" if k == "Lot" else "Helvetica", 9.5)
        c.drawString(x0 + 14, yy, k)
        c.drawRightString(W - M, yy, v)
        yy -= 17
    c.showPage()


def _metric_text(m: dict) -> str:
    v, u = m["value"], m.get("unit") or ""
    if u == "$":
        return f"${float(v):,.0f}"
    if isinstance(v, (int, float)):
        return f"{v:,.2f} {u}".strip() if u == "sq" else f"{v:,.0f} {u}".strip()
    return f"{v} {u}".strip()


def _trades_page(c, rep, company, d, trade_keys, page, total_pages):
    """The numbers each of the company's trades quotes from, one block per trade."""
    from .trades import label, metrics
    _header(c, rep, company, page, total_pages)
    _title(c, "For your trades", H - 92)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#475569"))
    c.drawString(M, H - 108, "The measurements each of your services is priced from. Full details on the other pages.")
    col_w = (W - 2 * M - 24) / 2
    cols = [M, M + col_w + 24]
    ys = [H - 136, H - 136]
    for key in trade_keys:
        ms = [m for m in metrics(key, d) if not m.get("beta")]
        if not ms:
            continue
        from .trades import report_price
        pr = report_price(key, company.get("rates"), d)
        need = 26 + 16 * len(ms) + (18 if pr else 0)
        i = 0 if ys[0] >= ys[1] else 1
        if ys[i] - need < 50:
            i = 1 - i
            if ys[i] - need < 50:
                c.showPage()
                _header(c, rep, company, page, total_pages)
                ys = [H - 80, H - 80]
                i = 0
        x, y = cols[i], ys[i]
        c.setFillColor(_hex(company.get("brand_color") or "#1d4ed8"))
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x, y, label(key) or key)
        y -= 18
        if pr:
            c.setFillColor(colors.HexColor("#0f172a"))
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x, y, f"Your price: {pr['text']}")
            y -= 18
        y = _kv_table(c, [(m["label"], _metric_text(m)) for m in ms], x + 4, y, col_w=(col_w - 90, 82), lh=16)
        ys[i] = y - 16
    c.showPage()


def _cover_page(c, rep, company, d, total_pages):
    """First page of a report without the roof section: the property, its
    photo and county facts, and which sections are included."""
    from .trades import SECTION_LABELS
    _header(c, rep, company, 1, total_pages)
    _title(c, "Property Report", H - 92)
    c.setFont("Helvetica", 11)
    c.setFillColor(colors.HexColor("#334155"))
    c.drawString(M, H - 110, f"{d['address']}, {(d.get('city') or '').title()} NE {d.get('zip') or ''}".strip()
                 + (f"  ({d['county_name']})" if d.get("county_name") else ""))
    c.drawString(M, H - 124, f"Parcel {str(d['pin']).split(':')[-1]} · Report {rep['id']} · {time.strftime('%B %d, %Y', time.localtime(rep['created']))}")
    top = H - 140
    y = top
    photo = (d.get("yard") or {}).get("photo_jpg") or d.get("aerial_jpg")
    if photo:
        img = ImageReader(io.BytesIO(base64.b64decode(photo)))
        iw, ih = img.getSize()
        s_ = min((W - 2 * M) / iw, 330 / ih)
        c.drawImage(img, M + (W - 2 * M - iw * s_) / 2, top - ih * s_, iw * s_, ih * s_)
        y = top - ih * s_ - 26
    yd = d.get("yard") or {}
    n = lambda v, u="": "-" if v in (None, "") else f"{float(v):,.0f}{u}"
    facts = [("Lot", n(yd.get("lot_sqft"), " sq ft")), ("House (assessor)", n(yd.get("living_sqft"), " sq ft")),
             ("Year built", str(yd.get("year_built") or "-")),
             ("Assessed value", "-" if not yd.get("home_value") else f"${float(yd['home_value']):,.0f}"),
             ("Stories (estimate)", str((d.get("walls") or {}).get("stories") or "-"))]
    c.setFillColor(colors.HexColor("#0f172a")); c.setFont("Helvetica-Bold", 12)
    c.drawString(M, y, "Property")
    c.drawString(W / 2 + 10, y, "In this report")
    _kv_table(c, facts, M, y - 20, col_w=(150, 90))
    yy = y - 20
    c.setFont("Helvetica", 10)
    for sct in d.get("sections") or []:
        c.drawString(W / 2 + 10, yy, f"- {SECTION_LABELS.get(sct, sct)}")
        yy -= 16
    c.setFont("Helvetica", 8.5); c.setFillColor(colors.HexColor("#64748b"))
    c.drawString(M, 46, "Measured from aerial photos and LiDAR. Estimates; verify on site before final pricing.")
    c.showPage()
