"""Roof measurement from raw LiDAR points: real planes, real pitches.

1. Building points inside the roof outline get a normal from their
   neighbours; region growing joins points that share a plane (same normal,
   on the plane, close together). Steps between roof levels split planes
   because the points aren't on the same plane.
2. Each plane is fitted (z = a x + b y + c): its pitch (rounded to whole
   /12, like the measuring services) and downslope direction.
3. The outline is filled on a 0.5-ft grid; each cell takes the plane of its
   nearest points. Plan area per plane x slope factor = roof area.
4. Every boundary is classified and measured:
     plane | open air, level  -> eave      plane | open air, sloped -> rake
     plane | plane, peak      -> ridge (level) or hip (sloped)
     plane | plane, trough    -> valley
     upper | lower roof (step in height): upper edge is an eave/rake, and
       the lower roof meets the wall: flashing (level) / step flashing (sloped)
   True length = plan length x sqrt(1 + (slope along the line)^2), with the
   slope taken from the plane(s) on that line.
"""
from __future__ import annotations

import math

import numpy as np
import shapely
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
from shapely.geometry import Polygon

LINE_TYPES = ["ridge", "hip", "valley", "rake", "eave", "step_flashing", "flashing"]
GRID = 0.5  # ft


def _fit_plane(p):
    A = np.column_stack([p[:, 0], p[:, 1], np.ones(len(p))])
    coef, *_ = np.linalg.lstsq(A, p[:, 2], rcond=None)
    return coef  # a, b, c


def _refine_by_residual(pts, labels, coefs, nlab, iters=2, radius=2.5):
    """Move each point to whichever nearby plane it actually lies on
    (smallest height residual), then refit. Puts plane borders where the
    planes really meet instead of where the normals happened to flip."""
    tree = cKDTree(pts[:, :2])
    nb = tree.query_ball_point(pts[:, :2], radius)
    for _ in range(iters):
        A = np.array([coefs[k] for k in range(nlab)])  # (nlab, 3)
        new = labels.copy()
        for i, idx in enumerate(nb):
            cand = np.unique(labels[idx])
            if len(cand) < 2:
                continue
            zp = A[cand, 0] * pts[i, 0] + A[cand, 1] * pts[i, 1] + A[cand, 2]
            r = np.abs(zp - pts[i, 2])
            best = cand[int(np.argmin(r))]
            if r.min() < 0.6 and best != labels[i]:
                new[i] = best
        if (new == labels).all():
            break
        labels = new
        for k in range(nlab):
            P = pts[labels == k]
            if len(P) >= 8:
                coefs[k] = _robust_plane(P)
    return labels, coefs


def _slope_stderr(P, a, b):
    """Standard error of a plane's slope (rise per run) from its points."""
    if len(P) < 6:
        return 9.0
    r = P[:, 2] - (a * P[:, 0] + b * P[:, 1])
    r = r - np.median(r)
    sd = 1.4826 * np.median(np.abs(r))  # robust residual spread
    s = math.hypot(a, b)
    d = np.array([-a, -b]) / s if s > 1e-6 else np.array([1.0, 0.0])
    t = P[:, :2] @ d
    spread = np.sqrt(np.sum((t - t.mean()) ** 2))
    return sd / max(spread, 1e-6)


def _harmonize_pitches(pts, labels, coefs, nlab):
    """Pitch per plane (x/12), corrected the way a measurer would:
    * a plane keeps its own measured pitch when the measurement is tight
      (standard error under ~0.35/12), whatever its size;
    * planes too sparse or narrow to measure (a strip mostly hidden under an
      overhang) take the building's main pitch;
    * the two sides of one gable are built the same, so opposite-facing
      neighbours with close readings share their average."""
    p12 = {k: 12 * math.hypot(*coefs[k][:2]) for k in range(nlab)}
    down, n, se, depth = {}, {}, {}, {}
    for k in range(nlab):
        a, b, _ = coefs[k]
        s_ = math.hypot(a, b)
        down[k] = np.array([-a, -b]) / s_ if s_ > 1e-6 else np.zeros(2)
        P = pts[labels == k]
        n[k] = len(P)
        # use only points close to the plane for the error estimate
        if len(P):
            res = np.abs(P[:, 2] - (a * P[:, 0] + b * P[:, 1] + coefs[k][2]))
            P = P[res < max(0.3, np.percentile(res, 80))]
        se[k] = 12 * _slope_stderr(P, a, b)
        depth[k] = float(np.ptp(P[:, :2] @ down[k])) if len(P) > 2 and down[k].any() else 0.0
    # trust a plane's own pitch only if it's tight AND deep enough down the
    # slope (a 3-ft strip under an overhang reads steep from its gutter edge)
    trusted = [k for k in range(nlab) if p12[k] >= 1.5 and se[k] <= 0.35 and n[k] >= 25 and depth[k] >= 5.0]
    if trusted:
        w = np.array([n[k] for k in trusted], float)
        main = float(np.round(np.average([p12[k] for k in trusted], weights=w)))
        for k in range(nlab):
            if k not in trusted and p12[k] >= 1.5:
                p12[k] = main
    tree = {k: cKDTree(pts[labels == k, :2]) for k in range(nlab) if n[k]}
    for i in trusted:
        for j in trusted:
            if j <= i or down[i] @ down[j] > -0.9 or abs(p12[i] - p12[j]) > 1.5:
                continue
            d, _ = tree[i].query(pts[labels == j, :2], k=1)
            if d.min() < 2.0:
                m = (p12[i] * n[i] + p12[j] * n[j]) / (n[i] + n[j])
                p12[i] = p12[j] = m
    return p12


def _robust_plane(p, iters=4):
    """Least-squares plane, re-fitted on the points within 0.25 ft of it
    (drops gutters, vents, points from a neighbouring plane)."""
    a, b, c = _fit_plane(p)
    for _ in range(iters):
        r = p[:, 2] - (a * p[:, 0] + b * p[:, 1] + c)
        tol = max(0.25, 2.5 * np.median(np.abs(r)))
        q = p[np.abs(r) < tol]
        if len(q) < 8:
            break
        a, b, c = _fit_plane(q)
    return a, b, c


def _normals(pts, tree, k=10):
    _, idx = tree.query(pts, k=min(k, len(pts)))
    nb = pts[idx]
    c = nb - nb.mean(1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", c, c)
    w, v = np.linalg.eigh(cov)
    n = v[:, :, 0]
    n[n[:, 2] < 0] *= -1
    curv = w[:, 0] / np.maximum(w.sum(1), 1e-9)
    return n, curv


def segment_planes(pts, ang_deg=12.0, dist_tol=0.4, radius=1.8, min_pts=12):
    """Region-grow points into planes. Returns labels (-1 = none)."""
    n = len(pts)
    # neighbours in 3-D, so a lower roof a few feet to the side of (and well
    # below) an upper roof never shares a plane or a normal with it
    tree = cKDTree(pts)
    nrm, curv = _normals(pts, tree)
    labels = -np.ones(n, int)
    cos_t = math.cos(math.radians(ang_deg))
    order = np.argsort(curv)
    nbrs = tree.query_ball_point(pts, radius)
    lab = 0
    for seed in order:
        if labels[seed] >= 0:
            continue
        region = [seed]
        labels[seed] = lab
        plane_n = nrm[seed]
        plane_d = -plane_n @ pts[seed]
        front = [seed]
        while front:
            nxt = []
            for i in front:
                for j in nbrs[i]:
                    if labels[j] >= 0:
                        continue
                    if abs(nrm[j] @ plane_n) < cos_t:
                        continue
                    if abs(plane_n @ pts[j] + plane_d) > dist_tol:
                        continue
                    labels[j] = lab
                    region.append(j)
                    nxt.append(j)
            front = nxt
            if len(region) >= 8 and len(region) % 8 == 0:
                # refit the plane as the region grows
                P = pts[region]
                a, b, c = _fit_plane(P)
                v = np.array([-a, -b, 1.0])
                plane_n = v / np.linalg.norm(v)
                plane_d = -plane_n @ P.mean(0)
        if len(region) < min_pts:
            labels[np.array(region)] = -2  # too small: reassign later
        else:
            lab += 1
    return labels, lab


def _merge_planes(pts, labels, nlab, ang_deg=6.0, off_tol=0.5):
    """Join planes that are really the same plane (split by noise)."""
    coefs = {k: _fit_plane(pts[labels == k]) for k in range(nlab)}
    parent = list(range(nlab))

    def find(a):
        while parent[a] != a:
            a = parent[a]
        return a
    tree = cKDTree(pts[:, :2])
    for i in range(nlab):
        for j in range(i + 1, nlab):
            a1, b1, _ = coefs[i]
            a2, b2, _ = coefs[j]
            n1 = np.array([-a1, -b1, 1]) / math.hypot(math.hypot(a1, b1), 1)
            n2 = np.array([-a2, -b2, 1]) / math.hypot(math.hypot(a2, b2), 1)
            if n1 @ n2 < math.cos(math.radians(ang_deg)):
                continue
            pj = pts[labels == j]
            off = np.median(np.abs(pj[:, 2] - (a1 * pj[:, 0] + b1 * pj[:, 1] + coefs[i][2])))
            if off > off_tol:
                continue
            # must touch
            d, _ = tree.query(pj[:, :2], k=1, distance_upper_bound=2.5)
            pi = pts[labels == i]
            ti = cKDTree(pi[:, :2])
            dd, _ = ti.query(pj[:, :2], k=1)
            if dd.min() > 2.5:
                continue
            parent[find(j)] = find(i)
    roots = sorted({find(k) for k in range(nlab)})
    remap = {r: i for i, r in enumerate(roots)}
    new = labels.copy()
    for k in range(nlab):
        new[labels == k] = remap[find(k)]
    return new, len(roots)


def _lines_ransac(pts, tol=0.6, gap=2.0, min_len=1.5, max_lines=8):
    """Split boundary points into straight segments (no double counting)."""
    if len(pts) >= 3:
        c = pts.mean(0)
        _, sv, vt = np.linalg.svd(pts - c, full_matrices=False)
        resid = np.abs((pts - c) @ vt[1])
        if np.percentile(resid, 90) < 0.9:  # one straight line: fit it whole
            return _merge_collinear(_split_gaps(pts, c, vt[0], gap, min_len))
    return _merge_collinear(_ransac(pts, tol, gap, min_len, max_lines))


def _split_gaps(P, c, u, gap, min_len):
    t = np.sort((P - c) @ u)
    cuts = np.where(np.diff(t) > gap)[0]
    out = []
    for s_, e in zip(np.r_[0, cuts + 1], np.r_[cuts, len(t) - 1]):
        a, b = t[s_] - GRID / 2, t[e] + GRID / 2
        if b - a >= min_len:
            out.append((c + u * a, c + u * b))
    return out


def _merge_collinear(segs, ang_deg=12, off_tol=1.2):
    """Union pieces that lie along the same line, so overlaps count once."""
    segs = [(np.asarray(a, float), np.asarray(b, float)) for a, b in segs]
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a1, b1 = segs[i]
                a2, b2 = segs[j]
                u1 = (b1 - a1) / max(np.hypot(*(b1 - a1)), 1e-9)
                u2 = (b2 - a2) / max(np.hypot(*(b2 - a2)), 1e-9)
                if abs(u1 @ u2) < math.cos(math.radians(ang_deg)):
                    continue
                n1 = np.array([-u1[1], u1[0]])
                if abs((a2 - a1) @ n1) > off_tol or abs((b2 - a1) @ n1) > off_tol:
                    continue
                t = [0.0, float((b1 - a1) @ u1), float((a2 - a1) @ u1), float((b2 - a1) @ u1)]
                lo1, hi1 = sorted(t[:2])
                lo2, hi2 = sorted(t[2:])
                if lo2 > hi1 + 1.0 or lo1 > hi2 + 1.0:  # separate pieces
                    continue
                lo, hi = min(lo1, lo2), max(hi1, hi2)
                segs[i] = (a1 + u1 * lo, a1 + u1 * hi)
                segs.pop(j)
                changed = True
                break
            if changed:
                break
    return segs


def _ransac(pts, tol=0.6, gap=2.0, min_len=1.5, max_lines=8):
    out = []
    rem = pts.copy()
    rng = np.random.default_rng(0)
    while len(rem) >= 4 and len(out) < max_lines:
        best = None
        for _ in range(60):
            i, j = rng.choice(len(rem), 2, replace=False)
            d = rem[j] - rem[i]
            L = np.hypot(*d)
            if L < 1.0:
                continue
            u = d / L
            nrm = np.array([-u[1], u[0]])
            inl = np.abs((rem - rem[i]) @ nrm) < tol
            if best is None or inl.sum() > best[0].sum():
                best = (inl, u, rem[i])
        if best is None or best[0].sum() < 4:
            break
        inl, u, p0 = best
        P = rem[inl]
        c = P.mean(0)
        _, _, vt = np.linalg.svd(P - c)
        u = vt[0]
        t = np.sort((P - c) @ u)
        # split at gaps
        cuts = np.where(np.diff(t) > gap)[0]
        starts = np.r_[0, cuts + 1]
        ends = np.r_[cuts, len(t) - 1]
        for s, e in zip(starts, ends):
            a, b = t[s] - GRID / 2, t[e] + GRID / 2
            if b - a >= min_len:
                out.append((c + u * a, c + u * b))
        rem = rem[~inl]
    return out


def analyze_roof_points(outline: Polygon, pts: np.ndarray, round_pitch: bool = True, _pass: int = 0) -> dict:
    """``pts``: building LiDAR points (LAS feet) near the roof."""
    res = {"method": "lidar_planes", "segments": [], "planes": []}
    plan_area = outline.area
    if len(pts) < 40:
        return {**res, "method": "insufficient_points"}
    inside = shapely.contains_xy(outline.buffer(1.0), pts[:, 0], pts[:, 1])
    pts = pts[inside]
    if len(pts) < 40:
        return {**res, "method": "insufficient_points"}
    # drop stray points (chimney tops, antennas) far above their neighbours
    tree = cKDTree(pts[:, :2])
    _, idx = tree.query(pts[:, :2], k=min(8, len(pts)))
    med = np.median(pts[idx, 2], axis=1)
    pts = pts[np.abs(pts[:, 2] - med) < 2.5]

    # adapt to point density: Douglas 2022 QL1 has a point every ~0.7 ft,
    # USGS 2016-18 data can be one every 3 ft
    spacing = math.sqrt(max(outline.area, 1.0) / max(len(pts), 1))
    sparse = spacing > 1.2
    labels, nlab = segment_planes(pts, ang_deg=16.0 if sparse else 12.0,
                                  dist_tol=0.6 if sparse else 0.4,
                                  radius=max(1.8, 2.4 * spacing),
                                  min_pts=8 if sparse else 12)
    if nlab == 0:
        return {**res, "method": "insufficient_points"}
    labels, nlab = _merge_planes(pts, labels, nlab)
    good = labels >= 0
    # orphan points join the nearest plane
    if (~good).any() and good.any():
        t = cKDTree(pts[good, :2])
        _, k = t.query(pts[~good, :2])
        labels[~good] = labels[good][k]

    # robust plane fits; anything steeper than 14/12 is a wall, tree or
    # chimney side, not roof: its points join the nearest real plane
    edge_d = shapely.distance(outline.exterior, shapely.points(pts[:, 0], pts[:, 1]))
    keep = []
    coefs = {}
    for k in range(nlab):
        P = pts[(labels == k)]
        inner = P[edge_d[labels == k] > 1.5]
        # skip the gutter band only when the plane is wide enough to spare it
        a, b, c = _robust_plane(inner if len(inner) >= max(10, 0.6 * len(P)) else P)
        coefs[k] = (a, b, c)
        if math.hypot(a, b) * 12 <= 14 and len(P) >= 10:
            keep.append(k)
    if not keep:
        return {**res, "method": "insufficient_points"}
    kept = np.isin(labels, keep)
    if (~kept).any():
        t2 = cKDTree(pts[kept, :2])
        _, kk = t2.query(pts[~kept, :2])
        labels[~kept] = labels[kept][kk]
    remap = {k: i for i, k in enumerate(keep)}
    labels = np.array([remap[l] for l in labels])
    coefs = {remap[k]: coefs[k] for k in keep}
    nlab = len(keep)

    labels, coefs = _refine_by_residual(pts, labels, coefs, nlab)
    raw = _harmonize_pitches(pts, labels, coefs, nlab)
    planes = []
    for k in range(nlab):
        P = pts[labels == k]
        a, b, c = coefs[k]
        slope = math.hypot(a, b)
        p12 = raw[k]
        if round_pitch:
            p12 = float(round(p12)) if p12 >= 1.5 else 0.0
        s = p12 / 12
        # downslope unit vector in plan (direction z decreases)
        dirv = np.array([-a, -b]) / slope if slope > 1e-6 else np.array([0.0, 0.0])
        planes.append({"id": k, "a": a, "b": b, "c": c, "pitch": p12, "slope": s, "down": dirv,
                       "n": int(len(P)), "flat": p12 == 0})

    # ---- fill the outline on a 0.5 ft grid with nearest-point planes
    minx, miny, maxx, maxy = outline.bounds
    xs = np.arange(minx + GRID / 2, maxx, GRID)
    ys = np.arange(maxy - GRID / 2, miny, -GRID)
    X, Y = np.meshgrid(xs, ys)
    inside = shapely.contains_xy(outline, X, Y)
    t = cKDTree(pts[:, :2])
    # nearest few points vote (robust at plane borders)
    _, kk = t.query(np.column_stack([X[inside], Y[inside]]), k=min(5, len(pts)))
    votes = labels[kk]
    lab_in = np.array([np.bincount(v, minlength=nlab).argmax() for v in votes])
    L = -np.ones(X.shape, int)
    L[inside] = lab_in
    # smooth jaggies
    for _ in range(1):
        cnt = np.stack([ndi.uniform_filter((L == k).astype(np.float32), 3) for k in range(nlab)])
        best = cnt.argmax(0)
        L[inside] = best[inside]
    # slivers (< 10 sq ft) between two planes are noise: give them to neighbours
    for _ in range(3):
        sizes = np.bincount(L[L >= 0], minlength=nlab) * GRID * GRID
        tiny = [k for k in range(nlab) if 0 < sizes[k] < 10]
        if not tiny:
            break
        for k in tiny:
            m = L == k
            ring = ndi.binary_dilation(m, iterations=1) & ~m & (L >= 0)
            if ring.any():
                L[m] = np.bincount(L[ring]).argmax()
    drop = []
    L = _absorb_edge_slivers(L, xs, ys, outline, coefs, nlab, pts=pts, labels=labels, drop=drop)
    drop += _decks(L, xs, ys, pts, planes)
    if drop and _pass == 0:
        # small pieces well below the roof (stoops, canopies, AC units, wall
        # tops) aren't part of the roof system: cut them out and re-measure
        from shapely.geometry import box as _box
        from shapely.ops import unary_union as _uu
        cells = []
        for k in drop:
            rr, cc = np.nonzero(L == k)
            cells += [_box(xs[c] - GRID / 2, ys[r] - GRID / 2, xs[c] + GRID / 2, ys[r] + GRID / 2) for r, c in zip(rr, cc)]
        cut = _uu(cells).buffer(0.3)
        trimmed = outline.difference(cut).buffer(-0.2).buffer(0.2)
        if trimmed.geom_type == "MultiPolygon":
            trimmed = max(trimmed.geoms, key=lambda g: g.area)
        if trimmed.geom_type == "Polygon" and trimmed.area > 0.7 * outline.area:
            from .roofclean import orthogonalize
            trimmed = orthogonalize(trimmed.simplify(0.3))
            res2 = analyze_roof_points(trimmed, pts, round_pitch, _pass=1)
            if res2.get("method") == "lidar_planes":
                res2["trimmed_sqft"] = round(outline.area - trimmed.area)
                return res2
    cell_area = plan_area / max(1, inside.sum())

    for p in planes:
        cells = int((L == p["id"]).sum())
        p["plan_sqft"] = cells * cell_area
        p["area_sqft"] = p["plan_sqft"] * math.sqrt(1 + p["slope"] ** 2)
    planes = [p for p in planes if p["plan_sqft"] > 0]
    by_id = {p["id"]: p for p in planes}

    def z_at(p, x, y):
        return p["a"] * x + p["b"] * y + p["c"]

    def along(p, u):
        """Slope of plane p along plan direction u (rise per run)."""
        return (-p["down"] @ u) * p["slope"] if not p["flat"] else 0.0

    segs = []

    def add(kind, a_xy, b_xy, plane_list, **tags):
        d = np.asarray(b_xy) - np.asarray(a_xy)
        Lp = float(np.hypot(*d))
        if Lp < 1.0:
            return
        u = d / Lp
        s = np.mean([abs(along(by_id[k], u)) for k in plane_list]) if plane_list else 0.0
        za = float(np.mean([z_at(by_id[k], *a_xy) for k in plane_list])) if plane_list else None
        zb = float(np.mean([z_at(by_id[k], *b_xy) for k in plane_list])) if plane_list else None
        segs.append({"type": kind, "a": [float(a_xy[0]), float(a_xy[1])], "b": [float(b_xy[0]), float(b_xy[1])],
                     "plan_len": Lp, "len": Lp * math.sqrt(1 + s * s), "za": za, "zb": zb, **tags})

    def level_or_sloped(p, u):
        if p["flat"]:
            return "level"
        return "level" if abs(p["down"] @ u) > 0.5 * 1.0 and abs(p["down"] @ np.array([-u[1], u[0]])) < 0.5 else (
            "level" if abs(p["down"] @ np.array([-u[1], u[0]])) > 0.7 else "sloped")

    # ---- interior boundaries between planes
    pairs = {}
    H, W = L.shape
    for dr, dc in ((0, 1), (1, 0)):
        a = L[: H - dr, : W - dc]
        b = L[dr:, dc:]
        m = (a >= 0) & (b >= 0) & (a != b)
        rr, cc = np.nonzero(m)
        for r, c in zip(rr, cc):
            i, j = int(a[r, c]), int(b[r, c])
            key = (min(i, j), max(i, j))
            x = xs[c] + dc * GRID / 2
            y = ys[r] - dr * GRID / 2
            pairs.setdefault(key, []).append((x, y))
    for (i, j), bp in pairs.items():
        if i not in by_id or j not in by_id:
            continue
        pi, pj = by_id[i], by_id[j]
        for a_xy, b_xy in _lines_ransac(np.array(bp)):
            mid = (a_xy + b_xy) / 2
            u = (b_xy - a_xy) / max(1e-9, np.hypot(*(b_xy - a_xy)))
            nrm = np.array([-u[1], u[0]])
            zi, zj = z_at(pi, *mid), z_at(pj, *mid)
            if abs(zi - zj) > 1.2:
                up, lo = (pi, pj) if zi > zj else (pj, pi)
                # upper roof: its free edge over the lower roof
                pair = len(segs)
                add("eave" if _is_level(up, u) else "rake", a_xy, b_xy, [up["id"]], step=pair)
                # lower roof meets the wall below it
                add("flashing" if _is_level(lo, u) else "step_flashing", a_xy, b_xy, [lo["id"]], step=pair)
                continue
            if pi["flat"] and pj["flat"]:
                continue
            # which side of the line is each plane on, and does it go down away from the line?
            # unit vector straight across the line toward each plane
            si = nrm * np.sign(_side_centroid(L, xs, ys, i, mid, nrm) @ nrm or 1.0)
            sj = nrm * np.sign(_side_centroid(L, xs, ys, j, mid, nrm) @ nrm or 1.0)
            if si @ sj > 0:  # both on the same side: not a real shared edge
                continue
            # plain Python bools: numpy's False is not `False` for `is` tests
            away_i = bool((si @ pi["down"]) > 0.1) if not pi["flat"] else None
            away_j = bool((sj @ pj["down"]) > 0.1) if not pj["flat"] else None
            if away_i is not False and away_j is not False and (away_i or away_j):
                add("ridge" if _is_level(pi if not pi["flat"] else pj, u) else "hip", a_xy, b_xy, [i, j])
            elif away_i is not True and away_j is not True and (away_i is False or away_j is False):
                add("valley", a_xy, b_xy, [i, j])

    # ---- outline edges: eave or rake of the plane inside
    ring = np.asarray(outline.exterior.coords)
    ccw = outline.exterior.is_ccw
    for (x0, y0), (x1, y1) in zip(ring[:-1], ring[1:]):
        d = np.array([x1 - x0, y1 - y0])
        Le = float(np.hypot(*d))
        if Le < 0.5:
            continue
        u = d / Le
        inward = np.array([-u[1], u[0]]) if ccw else np.array([u[1], -u[0]])
        n_st = max(1, int(Le / GRID))
        kinds = []
        for s in range(n_st):
            tpos = (s + 0.5) * Le / n_st
            k = -1
            # the first real roof plane in from the edge decides it (scrap
            # planes of wall / fascia hits along the edge don't)
            for depth in (1.25, 2.5, 4.0, 6.0):
                q = np.array([x0, y0]) + u * tpos + inward * depth
                c = int((q[0] - xs[0]) / GRID + 0.5)
                r = int((ys[0] - q[1]) / GRID + 0.5)
                kk = int(L[r, c]) if 0 <= r < H and 0 <= c < W else -1
                if kk >= 0 and by_id.get(kk) is not None and by_id[kk]["plan_sqft"] >= 30:
                    k = kk
                    break
            kinds.append(int(k))
        # fill unknowns, then fold runs shorter than 2.5 ft into neighbours
        kinds = _fill_runs([k if k >= 0 else None for k in kinds], int(2.5 / GRID))
        step = Le / n_st
        runs = []
        s = 0
        while s < len(kinds):
            e = s
            while e + 1 < len(kinds) and kinds[e + 1] == kinds[s]:
                e += 1
            k = kinds[s]
            p = by_id.get(k)
            runs.append([s, e, k, "eave" if (p is None or _is_level(p, u)) else "rake"])
            s = e + 1
        # one straight wall is one kind of edge (all eave or all rake) unless
        # the split is real: a minority under 30% is plane-edge noise
        tot = sum(r[1] - r[0] + 1 for r in runs)
        for kind_ in ("eave", "rake"):
            share = sum(r[1] - r[0] + 1 for r in runs if r[3] == kind_) / max(1, tot)
            if share >= 0.7:
                for r in runs:
                    r[3] = kind_
        for s, e, k, kind in runs:
            p = by_id.get(k)
            a_xy = np.array([x0, y0]) + u * (s * step)
            b_xy = np.array([x0, y0]) + u * ((e + 1) * step)
            add(kind, a_xy, b_xy, [k] if p is not None else [], edge=True)

    _extend_interior_ends(segs, outline, by_id)
    from .roofclean import regularize_lines
    segs = regularize_lines(segs, outline)
    lines = {k: 0.0 for k in LINE_TYPES}
    for sgm in segs:
        lines[sgm["type"]] += sgm["len"]
    area = sum(p["area_sqft"] for p in planes)
    pitch_areas = {}
    for p in planes:
        pitch_areas[int(p["pitch"])] = pitch_areas.get(int(p["pitch"]), 0) + p["area_sqft"]
    predominant = max(pitch_areas, key=pitch_areas.get) if pitch_areas else None
    res.update({
        "outline": outline,
        "plan_area_sqft": plan_area,
        "roof_area_sqft": area,
        "lines": lines,
        "segments": segs,
        "planes": [{k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in p.items()} for p in planes],
        "facet_count": sum(1 for p in planes if p["plan_sqft"] >= 15),
        "pitch_areas": pitch_areas,
        "predominant_pitch": predominant,
        "label_grid": L, "grid_x": xs, "grid_y": ys,
    })
    return res


def _absorb_edge_slivers(L, xs, ys, outline, coefs, nlab, band=4.0, max_area=120.0, pts=None, labels=None,
                         drop=None, drop_area=60.0, drop_dz=4.0):
    """Thin planes hugging the roof edge at the same height as their
    neighbour are overhang / wall-top hits, not separate roofs: merge them.
    A real lower roof (several feet below its neighbour) is kept."""
    X, Y = np.meshgrid(xs, ys)
    dist = shapely.distance(outline.exterior, shapely.points(X.ravel(), Y.ravel())).reshape(X.shape)
    for _ in range(3):
        changed = False
        sizes = np.bincount(L[L >= 0], minlength=nlab) * GRID * GRID
        for k in range(nlab):
            m = L == k
            if not m.any() or sizes[k] > max_area:
                continue
            if np.mean(dist[m] < band) < 0.9:
                continue  # reaches well inside: a real facet
            ring = ndi.binary_dilation(m, iterations=1) & ~m & (L >= 0)
            if not ring.any():
                continue
            nb = int(np.bincount(L[ring]).argmax())
            # height step across the shared border, from the actual points
            # (a 2-ft strip can't be extrapolated as a plane reliably)
            if pts is not None and (labels == k).any() and (labels == nb).any():
                Pk, Pn = pts[labels == k], pts[labels == nb]
                d, j = cKDTree(Pn[:, :2]).query(Pk[:, :2], distance_upper_bound=3.0)
                ok = np.isfinite(d)
                dz = float(np.median(np.abs(Pk[ok, 2] - Pn[j[ok], 2]))) if ok.any() else 9.0
            else:
                rr, cc = np.nonzero(ring & (L == nb))
                a1, b1, c1 = coefs[k]
                a2, b2, c2 = coefs[nb]
                xx, yy = xs[cc], ys[rr]
                dz = np.median(np.abs((a1 * xx + b1 * yy + c1) - (a2 * xx + b2 * yy + c2)))
            if dz >= 3.0 and pts is not None and _plane_covers(m, xs, ys, pts, coefs[nb]):
                dz = 0.0  # the neighbour's roof runs right across it: its overhang
            if dz < 3.0:  # overhang / wall-top hits read up to ~3 ft off
                L[m] = nb
                changed = True
            elif drop is not None and sizes[k] < drop_area and dz > drop_dz and k not in drop:
                # Only cut it if it sits against the upper roof's gable end
                # (a rake side): bays, stoops, wall tops. A low strip along the
                # upper roof's eave is a real lower roof (porch/skirt): keep.
                rr, cc = np.nonzero(ring & (L == nb))
                if len(rr) >= 3:
                    P2 = np.column_stack([xs[cc], ys[rr]])
                    _, _, vt = np.linalg.svd(P2 - P2.mean(0), full_matrices=False)
                    a2, b2, _ = coefs[nb]
                    s2 = math.hypot(a2, b2)
                    if s2 > 1e-6 and abs(vt[0] @ (np.array([-a2, -b2]) / s2)) > 0.7:
                        drop.append(k)
        if not changed:
            break
    return L


def _plane_covers(m, xs, ys, pts, coef, tol=0.7, need=0.5):
    """Do points lying on plane ``coef`` cover most of the cells in mask m?"""
    rr, cc = np.nonzero(m)
    if not len(rr):
        return False
    x_lo, x_hi = xs[cc].min() - 1, xs[cc].max() + 1
    y_lo, y_hi = ys[rr].min() - 1, ys[rr].max() + 1
    P = pts[(pts[:, 0] > x_lo) & (pts[:, 0] < x_hi) & (pts[:, 1] > y_lo) & (pts[:, 1] < y_hi)]
    if not len(P):
        return False
    a, b, c = coef
    on = P[np.abs(P[:, 2] - (a * P[:, 0] + b * P[:, 1] + c)) < tol]
    if not len(on):
        return False
    H, W = m.shape
    ci = np.floor((on[:, 0] - (xs[0] - GRID / 2)) / GRID).astype(int)
    ri = np.floor(((ys[0] + GRID / 2) - on[:, 1]) / GRID).astype(int)
    ok = (ci >= 0) & (ci < W) & (ri >= 0) & (ri < H)
    hit = np.zeros(m.shape, bool)
    hit[ri[ok], ci[ok]] = True
    hit = ndi.binary_dilation(hit, iterations=2)  # ~1 ft around each point
    return float(hit[m].mean()) >= need


def _decks(L, xs, ys, pts, planes, min_sqft=40.0):
    """Flat planes that are decks, not roofs.

    County footprints often include raised decks, which scan as a flat
    surface at about eave height. A deck has a railing: a line of points
    2.5-4.5 ft above the floor around its edge. A flat roof never does."""
    out = []
    H, W = L.shape
    c = np.floor((pts[:, 0] - (xs[0] - GRID / 2)) / GRID).astype(int)
    r = np.floor(((ys[0] + GRID / 2) - pts[:, 1]) / GRID).astype(int)
    ok = (c >= 0) & (c < W) & (r >= 0) & (r < H)
    lab = np.full(len(pts), -1)
    lab[ok] = L[r[ok], c[ok]]
    for p in planes:
        if not p["flat"]:
            continue
        k = p["id"]
        if (L == k).sum() * GRID * GRID < min_sqft:
            continue
        P = pts[lab == k]
        if len(P) < 30:
            continue
        dz = P[:, 2] - (p["a"] * P[:, 0] + p["b"] * P[:, 1] + p["c"])
        floor = int((np.abs(dz) < 0.6).sum())
        rail = int(((dz > 2.3) & (dz < 4.8)).sum())
        if floor and rail >= max(10, 0.10 * floor):
            out.append(k)
    return out


def _extend_interior_ends(segs, outline, by_id, reach=3.0):
    """Ridges, hips and valleys stop a little short in the cell map (edge
    cells, overhangs hiding the end). Run each end on, up to ``reach`` feet,
    to the roof edge or the nearest other line, like a measurer would."""
    from shapely.geometry import LineString, Point
    edge = outline.exterior
    others = [LineString([s_["a"], s_["b"]]) for s_ in segs]
    for idx, sgm in enumerate(segs):
        if sgm["type"] not in ("ridge", "hip", "valley"):
            continue
        a, b = np.array(sgm["a"]), np.array(sgm["b"])
        Lp = float(np.hypot(*(b - a)))
        if Lp < 1.0:
            continue
        u = (b - a) / Lp
        for end, sign in (("a", -1.0), ("b", 1.0)):
            p0 = np.array(sgm[end])
            ray = LineString([p0, p0 + sign * u * reach])
            best = None
            for tgt in [edge] + [o for j, o in enumerate(others) if j != idx]:
                hit = ray.intersection(tgt)
                if hit.is_empty:
                    continue
                pts_ = [hit] if hit.geom_type == "Point" else [g for g in getattr(hit, "geoms", []) if g.geom_type == "Point"]
                for h in pts_:
                    d = Point(p0).distance(h)
                    if d > 0.05 and (best is None or d < best[0]):
                        best = (d, (h.x, h.y))
            if best:
                sgm[end] = [float(best[1][0]), float(best[1][1])]
        a, b = np.array(sgm["a"]), np.array(sgm["b"])
        newL = float(np.hypot(*(b - a)))
        if newL > 0:
            sgm["len"] *= newL / Lp
            sgm["plan_len"] = newL


def _is_level(p, u) -> bool:
    """Is a line with plan direction u level on plane p (eave/ridge-like)?"""
    if p["flat"]:
        return True
    return abs(p["down"] @ u) < 0.5  # line runs across the slope


def _side_centroid(L, xs, ys, k, mid, nrm):
    """Vector from the line midpoint to plane k's nearby cells (within 4 ft)."""
    rr, cc = np.nonzero(L == k)
    P = np.column_stack([xs[cc], ys[rr]])
    d = P - mid
    near = np.hypot(d[:, 0], d[:, 1]) < 4.0
    if not near.any():
        return np.zeros(2)
    return d[near].mean(0)


def _fill_runs(kinds, min_len):
    n = len(kinds)
    last = None
    for i in range(n):
        if kinds[i] is None:
            kinds[i] = last
        else:
            last = kinds[i]
    nxt = None
    for i in range(n - 1, -1, -1):
        if kinds[i] is None:
            kinds[i] = nxt
        else:
            nxt = kinds[i]
    kinds = [k if k is not None else -1 for k in kinds]
    while True:
        runs, s = [], 0
        while s < n:
            e = s
            while e + 1 < n and kinds[e + 1] == kinds[s]:
                e += 1
            runs.append([s, e, kinds[s]])
            s = e + 1
        short = [i for i, r in enumerate(runs) if r[1] - r[0] + 1 < min_len]
        if len(runs) <= 1 or not short:
            return kinds
        i = min(short, key=lambda i: runs[i][1] - runs[i][0])
        nb = [runs[x] for x in (i - 1, i + 1) if 0 <= x < len(runs)]
        tgt = max(nb, key=lambda r: r[1] - r[0])[2]
        for q in range(runs[i][0], runs[i][1] + 1):
            kinds[q] = tgt
