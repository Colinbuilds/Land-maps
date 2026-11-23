"""Drafting clean-up for roof outlines and roof lines.

The plane engine finds every line from the scan on its own, so a ridge
shared by several plane pairs comes out as broken pieces, and line ends stop
a foot or two short of each other. A roof measurer draws it the way it's
built: square walls, one straight ridge, and hips, valleys and ridges that
meet at a single point. This module does the same, without moving anything
further than the scan's own uncertainty (a couple of feet).

Lengths stay true: each line keeps its own plan-to-true slope factor, so
only its plan length changes.
"""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import LineString, Point, Polygon

INTERIOR = ("ridge", "hip", "valley")
LINE_ALL = ("ridge", "hip", "valley", "rake", "eave", "step_flashing", "flashing")
WALL = ("step_flashing", "flashing")


# ------------------------------------------------------------------ outlines
def main_axis(poly) -> float:
    """Dominant wall direction (radians, 0..pi/2): length-weighted mode of
    edge angles modulo 90 degrees."""
    ring = np.asarray(poly.exterior.coords)
    d = np.diff(ring, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])
    ang = np.mod(np.arctan2(d[:, 1], d[:, 0]), math.pi / 2)
    # circular mean on the 90-degree circle (x4 maps it to a full turn)
    c = (L * np.cos(4 * ang)).sum()
    s = (L * np.sin(4 * ang)).sum()
    base = math.atan2(s, c) / 4
    # refine: mean of the edges within 10 degrees of it
    dev = (ang - base + math.pi / 4) % (math.pi / 2) - math.pi / 4
    m = np.abs(dev) < math.radians(10)
    if m.any():
        base = base + float((dev[m] * L[m]).sum() / L[m].sum())
    return base % (math.pi / 2)


def _snap_dir(u, axis, step_deg, tol_deg):
    """Rotate unit vector u to the nearest axis + k*step if within tol."""
    a = math.atan2(u[1], u[0])
    step = math.radians(step_deg)
    k = round((a - axis) / step)
    tgt = axis + k * step
    if abs(a - tgt) <= math.radians(tol_deg):
        return np.array([math.cos(tgt), math.sin(tgt)]), True
    return np.asarray(u, float), False


def _intersect(p, u, q, v):
    cross = u[0] * v[1] - u[1] * v[0]
    if abs(cross) < 1e-6:
        return None
    t = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / cross
    return p + t * u


def orthogonalize(poly, tol_deg: float = 12.0, min_edge: float = 1.0):
    """Square up a traced building outline.

    Edges within ``tol_deg`` of the main axis (or its perpendicular) are
    turned to exactly that direction about their midpoint, and 45-degree
    bay walls within 6 degrees to exactly 45. Corners are rebuilt where the
    straightened walls meet; nearly collinear neighbours become one wall.
    Returns the original if the result would change the area by > 3%."""
    if poly is None or poly.is_empty or poly.geom_type != "Polygon":
        return poly
    axis = main_axis(poly)
    ring = np.asarray(poly.exterior.coords)[:-1]
    if Polygon(ring).exterior.is_ccw is False:
        ring = ring[::-1]
    # drop tiny edges first (tracing jitter)
    pts = [ring[0]]
    for p in ring[1:]:
        if np.hypot(*(p - pts[-1])) >= min_edge:
            pts.append(p)
    if len(pts) < 3:
        return poly
    ring = np.array(pts)
    n = len(ring)
    lines = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        d = b - a
        L = float(np.hypot(*d))
        u = d / L
        v, ok = _snap_dir(u, axis, 90, tol_deg)
        if not ok and L >= 6.0:  # a real 45-degree bay wall, not a trimmed corner
            v, ok = _snap_dir(u, axis + math.pi / 4, 90, 6.0)
        lines.append(((a + b) / 2, v if ok else u, L, ok))
    # Off-angle pieces up to ~15 ft between square walls are tracing
    # artefacts (a corner cut by a tree, a crown bulge): drop them and let the
    # square walls on either side run on to their own corner.
    if any(x[3] for x in lines):
        keep = [True] * n
        i = 0
        while i < n:
            if lines[i][3]:
                i += 1
                continue
            j = i
            run = []
            while not lines[j % n][3] and len(run) < n:
                run.append(j % n)
                j += 1
            if sum(lines[k][2] for k in run) <= 15.0:
                for k in run:
                    keep[k] = False
            i = j if j > i else i + 1
        if sum(keep) >= 3:
            lines = [ln for ln, k in zip(lines, keep) if k]
    lines = [ln[:3] for ln in lines]
    # merge consecutive (nearly) collinear walls
    merged = []
    for ln in lines:
        if merged:
            p, u, L = merged[-1]
            q, v, M = ln
            off = abs((q - p) @ np.array([-u[1], u[0]]))
            if abs(u[0] * v[1] - u[1] * v[0]) < math.sin(math.radians(3)) and u @ v > 0 and off < 0.6:
                w = L + M
                merged[-1] = ((p * L + q * M) / w, u if L >= M else v, w)
                continue
        merged.append(ln)
    if len(merged) > 3:
        p, u, L = merged[0]
        q, v, M = merged[-1]
        off = abs((p - q) @ np.array([-v[1], v[0]]))
        if abs(u[0] * v[1] - u[1] * v[0]) < math.sin(math.radians(3)) and u @ v > 0 and off < 0.6:
            w = L + M
            merged[0] = ((p * L + q * M) / w, u if L >= M else v, w)
            merged.pop()
    merged = _drop_stubs(merged)
    m = len(merged)
    if m < 3:
        return poly
    out = []
    for i in range(m):
        p, u, Lp = merged[i - 1]
        q, v, _ = merged[i]
        x = _intersect(p, u, q, v)
        if x is None:  # parallel neighbours (a tiny jog was dropped): square step
            x1 = p + u * Lp / 2
            out.append(x1)
            out.append(q + v * ((x1 - q) @ v))
            continue
        out.append(x)
    new = Polygon(out)
    if not new.is_valid:
        new = new.buffer(0)
    # zero-width spikes (a wall that doubles back on itself)
    new = new.buffer(0.05, join_style=2).buffer(-0.05, join_style=2)
    if new.geom_type != "Polygon" or abs(new.area - poly.area) > 0.05 * poly.area \
            or new.hausdorff_distance(poly) > 4.0:
        return poly
    return new


def _drop_stubs(lines, stub=6.0, jog=4.0):
    """Remove a short wall stuck out (or in) from a long wall by a small jog:
    long wall - jog - stub  becomes one straight wall (trim leftovers)."""
    def par(u, v):
        return abs(u[0] * v[1] - u[1] * v[0]) < math.sin(math.radians(3))
    changed = True
    while changed and len(lines) > 4:
        changed = False
        n = len(lines)
        for i in range(n):
            for d in (1, -1):
                s_, j_, w_ = lines[i], lines[(i + d) % n], lines[(i + 2 * d) % n]
                if s_[2] <= stub and j_[2] <= jog and par(s_[1], w_[1]) and not par(s_[1], j_[1]) \
                        and w_[2] >= 3 * s_[2]:
                    # stub onto the long wall's line; then it is the same wall
                    p, u, L = w_
                    q = s_[0]
                    q2 = p + u * ((q - p) @ u)
                    new_w = ((p * L + q2 * s_[2]) / (L + s_[2]), u, L + s_[2] + j_[2])
                    keep = [k for k in range(n) if k not in (i, (i + d) % n)]
                    lines = [new_w if k == (i + 2 * d) % n else lines[k] for k in keep]
                    changed = True
                    break
            if changed:
                break
    return lines


# --------------------------------------------------------------------- lines
def _seg_vec(s):
    a, b = np.asarray(s["a"], float), np.asarray(s["b"], float)
    d = b - a
    L = float(np.hypot(*d))
    return a, b, (d / L if L > 0 else np.array([1.0, 0.0])), L


def _factor(s):
    pl = s.get("plan_len") or float(np.hypot(*(np.subtract(s["b"], s["a"]))))
    return (s["len"] / pl) if pl > 1e-6 else 1.0


def _set(s, a, b, factor=None):
    f = _factor(s) if factor is None else factor
    s["a"] = [float(a[0]), float(a[1])]
    s["b"] = [float(b[0]), float(b[1])]
    pl = float(np.hypot(b[0] - a[0], b[1] - a[1]))
    s["plan_len"] = pl
    s["len"] = pl * f


def _zmean(s):
    za, zb = s.get("za"), s.get("zb")
    return None if za is None or zb is None else (za + zb) / 2


def _merge_collinear(segs, kinds, ang_deg=8.0, off_tol=1.5, gaps=None):
    """Join pieces of one line: same type, same direction, on the same line,
    with a gap no longer than the type allows (and, for level lines, at the
    same height)."""
    gaps = gaps or {}
    changed = True
    while changed:
        changed = False
        for i in range(len(segs)):
            si = segs[i]
            if si is None or si["type"] not in kinds:
                continue
            ai, bi, ui, Li = _seg_vec(si)
            for j in range(i + 1, len(segs)):
                sj = segs[j]
                if sj is None or sj["type"] != si["type"]:
                    continue
                aj, bj, uj, Lj = _seg_vec(sj)
                if abs(ui[0] * uj[1] - ui[1] * uj[0]) > math.sin(math.radians(ang_deg)):
                    continue
                ni = np.array([-ui[1], ui[0]])
                nj = np.array([-uj[1], uj[0]])
                if max(abs((aj - ai) @ ni), abs((bj - ai) @ ni), abs((ai - aj) @ nj), abs((bi - aj) @ nj)) > off_tol:
                    continue
                t = sorted([(aj - ai) @ ui, (bj - ai) @ ui])
                gap = max(t[0] - Li, -t[1])
                if gap > gaps.get(si["type"], 3.0):
                    continue
                zi, zj = _zmean(si), _zmean(sj)
                if si["type"] in ("ridge", "flashing") and zi is not None and zj is not None and abs(zi - zj) > 1.5:
                    continue
                # merged line: length-weighted direction and position
                uj2 = uj if uj @ ui > 0 else -uj
                u = ui * Li + uj2 * Lj
                u /= np.hypot(*u)
                c = ((ai + bi) / 2 * Li + (aj + bj) / 2 * Lj) / (Li + Lj)
                ts = [(p - c) @ u for p in (ai, bi, aj, bj)]
                f = (_factor(si) * Li + _factor(sj) * Lj) / (Li + Lj)
                for k in ("za", "zb"):
                    if si.get(k) is not None and sj.get(k) is not None:
                        si[k] = (si[k] * Li + sj[k] * Lj) / (Li + Lj)
                _set(si, c + min(ts) * u, c + max(ts) * u, f)
                segs[j] = None
                ai, bi, ui, Li = _seg_vec(si)
                changed = True
    return [s for s in segs if s is not None]


def _ls_point(lines):
    """Point closest (least squares) to a set of lines (p, u[, weight])."""
    A = np.zeros((2, 2))
    rhs = np.zeros(2)
    for ln in lines:
        p, u = ln[0], ln[1]
        w = ln[2] if len(ln) > 2 else 1.0
        n = np.array([-u[1], u[0]])
        N = w * np.outer(n, n)
        A += N
        rhs += N @ p
    if abs(np.linalg.det(A)) < 1e-3:
        return None
    return np.linalg.solve(A, rhs)


def regularize_lines(segs: list, outline, axis: float | None = None, snap_r: float = 3.0) -> list:
    """Clean roof lines in place (see module doc). Returns the new list."""
    if not segs:
        return segs
    if axis is None:
        axis = main_axis(outline)
    segs = [dict(s) for s in segs]
    # Step lines (an upper roof's eave/rake over a lower roof, and the
    # flashing under it) come in pairs. A "lower roof" thinner than ~2.5 ft
    # running along the outline is the gutter / fascia band, not a roof: drop
    # both lines of such a pair, and pieces too short to be a real step.
    edge = outline.exterior
    bad = set()
    for s in segs:
        if s.get("step") is None:
            continue
        a, b, u, L = _seg_vec(s)
        if L < 2.5 or max(edge.distance(Point(a)), edge.distance(Point(b)),
                           edge.distance(Point((a + b) / 2))) < 3.5:
            bad.add(s["step"])
    segs = [s for s in segs if s.get("step") not in bad or s.get("step") is None]
    # 1. directions: level lines along the walls, hips/valleys at 45 when close
    def inner(s):  # anything not drawn on the outline itself
        return not s.get("edge")
    for s in segs:
        if not inner(s):
            continue
        a, b, u, L = _seg_vec(s)
        if L < 1.0:
            continue
        if s["type"] not in ("hip", "valley"):
            v, ok = _snap_dir(u, axis, 90, 10.0)
        else:
            v, ok = _snap_dir(u, axis + math.pi / 4, 90, 5.0)
        if ok:
            c = (a + b) / 2
            t = sorted([(a - c) @ v, (b - c) @ v])
            _set(s, c + t[0] * v, c + t[1] * v)
            s["_square"] = True
        elif s.get("step") is not None and s["type"] not in INTERIOR and L < 10.0:
            s["_diag_step"] = True
    diag = {s["step"] for s in segs if s.pop("_diag_step", False)}
    segs = [s for s in segs if s.get("step") is None or s["step"] not in diag]
    # 2. one line per ridge / hip / valley
    outer = [s for s in segs if not inner(s)]
    segs = _merge_collinear([s for s in segs if inner(s)], tuple(LINE_ALL),
                            gaps={"ridge": 7.0, "hip": 3.0, "valley": 3.0, "step_flashing": 3.0,
                                  "flashing": 3.0, "eave": 3.0, "rake": 3.0})
    # a ridge is one straight line: touching ridge pieces at one height that
    # zig-zag (sparse scans) are the same ridge
    segs = _merge_collinear(segs, ("ridge",), ang_deg=30.0, off_tol=4.5, gaps={"ridge": 1.5})
    for s in segs:
        if s["type"] == "ridge" and not s.get("_square"):
            a, b, u, L = _seg_vec(s)
            v, ok = _snap_dir(u, axis, 90, 12.0)
            if ok:
                c = (a + b) / 2
                t = sorted([(a - c) @ v, (b - c) @ v])
                _set(s, c + t[0] * v, c + t[1] * v)
                s["_square"] = True
    segs = segs + outer
    # 3. junctions: ends that nearly meet go to one point
    idx = [i for i, s in enumerate(segs) if s["type"] in INTERIOR]
    ends = [(i, e) for i in idx for e in ("a", "b")]
    P = np.array([segs[i][e] for i, e in ends]) if ends else np.zeros((0, 2))
    parent = list(range(len(ends)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for x in range(len(ends)):
        for y in range(x + 1, len(ends)):
            if ends[x][0] != ends[y][0] and np.hypot(*(P[x] - P[y])) <= snap_r:
                parent[find(x)] = find(y)
    groups: dict = {}
    for x in range(len(ends)):
        groups.setdefault(find(x), []).append(x)
    done = set()
    ring = np.asarray(outline.exterior.coords)[:-1]
    for g in groups.values():
        segs_in = {ends[x][0] for x in g}
        if len(segs_in) < 2:
            continue
        lines = []
        for i in segs_in:
            a, b, u, _ = _seg_vec(segs[i])
            # squared-up ridges are the most certain lines: junctions sit on them
            lines.append((a, u, 6.0 if segs[i].get("_square") and segs[i]["type"] == "ridge" else 1.0))
        q = _ls_point(lines)
        cen = P[g].mean(0)
        if q is None or np.hypot(*(q - cen)) > snap_r:
            q = cen
        sq = [i for i in segs_in if segs[i].get("_square") and segs[i]["type"] == "ridge"]
        if sq:  # keep a squared-up ridge exactly straight: the junction is on it
            ra, _, ru, _ = _seg_vec(segs[sq[0]])
            q = ra + ru * ((q - ra) @ ru)
        # a junction on the roof edge sits on an outline corner
        dv = np.hypot(*(ring - q).T)
        if dv.min() <= 1.5:
            q = ring[int(dv.argmin())]
        for x in g:
            i, e = ends[x]
            s = segs[i]
            a, b = np.asarray(s["a"], float), np.asarray(s["b"], float)
            if e == "a":
                _set(s, q, b)
            else:
                _set(s, a, q)
            done.add(x)
    # 4. lone ends: T into another line, or finish on the outline
    edge = outline.exterior
    for x, (i, e) in enumerate(ends):
        if x in done:
            continue
        s = segs[i]
        a, b, u, L = _seg_vec(s)
        if L < 0.5:
            continue
        p = a if e == "a" else b
        out_dir = -u if e == "a" else u
        best = None
        for j in idx:
            if j == i:
                continue
            c, d, v, M = _seg_vec(segs[j])
            h = _intersect(p, out_dir, c, v)
            if h is None:
                continue
            t_on = (h - c) @ v
            if -0.5 <= t_on <= M + 0.5:
                dist = (h - p) @ out_dir  # + extend, - trim
                if -snap_r <= dist <= snap_r and (best is None or abs(dist) < abs(best[0])):
                    best = (dist, h)
        if best is None and s["type"] in ("hip", "valley"):
            dv = np.hypot(*(ring - p).T)
            if dv.min() <= snap_r:
                best = (0.0, ring[int(dv.argmin())])
            else:
                # hips and valleys run to a roof corner: follow the line out
                ray = LineString([p, p + out_dir * 6.0])
                hit = ray.intersection(edge)
                hs = [hit] if hit.geom_type == "Point" else [h for h in getattr(hit, "geoms", []) if h.geom_type == "Point"]
                if hs:
                    h = min(hs, key=lambda h: Point(p).distance(h))
                    hv = np.hypot(*(ring - np.array([h.x, h.y])).T)
                    if hv.min() <= 2.5:
                        best = (0.0, ring[int(hv.argmin())])
        if best is None:
            # a ridge with nothing at its end runs out to the gable wall
            reach = 6.0 if s["type"] == "ridge" else snap_r
            ray = LineString([p - out_dir * snap_r, p + out_dir * reach])
            hit = ray.intersection(edge)
            pts = [hit] if hit.geom_type == "Point" else [h for h in getattr(hit, "geoms", []) if h.geom_type == "Point"]
            if pts:
                h = min(pts, key=lambda h: Point(p).distance(h))
                best = (0.0, np.array([h.x, h.y]))
        if best is not None:
            q = best[1]
            if e == "a":
                _set(s, q, b)
            else:
                _set(s, a, q)
    # 5. step lines end on the nearest roof corner or line end
    corners = [np.asarray(s[e], float) for s in segs if s["type"] in INTERIOR for e in ("a", "b")] + list(ring)
    if corners:
        C = np.array(corners)
        for s in segs:
            if s["type"] in INTERIOR or not inner(s):
                continue
            a, b, u, L = _seg_vec(s)
            new = []
            for p in (a, b):
                dv = np.hypot(*(C - p).T)
                k = int(dv.argmin())
                # slide the end along the line to the corner (keeps it straight)
                new.append(a + u * ((C[k] - a) @ u) if dv[k] <= 1.5 else p)
            if np.hypot(*(new[1] - new[0])) > 0.5:
                _set(s, new[0], new[1])
    for s in segs:
        s.pop("_square", None)
    segs = [s for s in segs if s.get("plan_len", 1) >= (2.0 if inner(s) else 0.3)]
    # 6. a short step line attached to nothing at either end is a plane-split
    # artefact on one roof face, not a wall: drop it (and its pair)
    def attached(p, me):
        if edge.distance(Point(p)) <= 2.0:
            return True
        for o in segs:
            if o is me or o.get("step") is not None and o.get("step") == me.get("step"):
                continue
            if LineString([o["a"], o["b"]]).distance(Point(p)) <= 2.0:
                return True
        return False
    drop = set()
    stray = set()
    for i, s in enumerate(segs):
        if s["type"] in ("hip", "valley") and s.get("plan_len", 0) < 4.0 \
                and min(np.hypot(*(ring - np.asarray(s[e], float)).T).min() for e in ("a", "b")) > 0.5:
            stray.add(i)  # a hip/valley stub hanging off scrap planes
            continue
        if s.get("step") is None or s.get("plan_len", 0) >= 10.0:
            continue
        if not attached(s["a"], s) and not attached(s["b"], s):
            drop.add(s["step"])
    return [s for i, s in enumerate(segs)
            if i not in stray and (s.get("step") is None or s["step"] not in drop)]


def fit_hidden_edges(poly, pts: np.ndarray, is_hidden, margin: float = 0.75, max_move: float = 4.0,
                     inward_only: bool = False):
    """Place walls the photo can't see (under trees) from the LiDAR.

    For each side mostly under canopy, the roof edge is the outermost roof
    points along that side plus ``margin`` (half the point spacing plus the
    drip edge). Sides the photo traced are left where they are."""
    if poly is None or poly.geom_type != "Polygon" or pts is None or len(pts) < 30:
        return poly, 0
    ring = np.asarray(poly.exterior.coords)[:-1]
    if Polygon(ring).exterior.is_ccw is False:
        ring = ring[::-1]
    n = len(ring)
    P = np.asarray(pts)[:, :2]
    lines, moved = [], 0
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        d = b - a
        L = float(np.hypot(*d))
        u = d / L if L else np.array([1.0, 0.0])
        out = np.array([u[1], -u[0]])  # outward for a CCW ring
        shift = 0.0
        if L >= (1.5 if inward_only else 4.0):
            ts = np.linspace(0.1, 0.9, max(5, int(L)))
            S = a + np.outer(ts * L, u)
            band = np.vstack([S + o * out for o in (-1.5, 0.0, 1.5)])
            if float(np.mean(is_hidden(band[:, 0], band[:, 1]))) >= 0.4:
                rel = P - a
                along = rel @ u
                perp = rel @ out
                lo, hi = (0.15 * L, 0.85 * L) if L >= 4.0 else (0.0, L)
                m = (along > lo) & (along < hi) & (perp > -6.0) & (perp < max_move + 1.0)
                if m.sum() >= max(4 if L < 4.0 else 8, int(L)):
                    edge_d = float(np.percentile(perp[m], 97)) + margin
                    if inward_only:
                        edge_d = min(0.0, edge_d)
                    if abs(edge_d) <= max_move and edge_d != 0.0:
                        shift = edge_d
                        moved += 1
        lines.append((a + out * shift, u))
    new = []
    for i in range(n):
        (p, u), (q, v) = lines[i - 1], lines[i]
        x = _intersect(p, u, q, v)
        new.append(x if x is not None else (p + q) / 2)
    cand = Polygon(new)
    if not moved or not cand.is_valid or abs(cand.area - poly.area) > 0.12 * poly.area:
        return poly, 0
    return cand, moved
