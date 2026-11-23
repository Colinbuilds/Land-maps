import math

import numpy as np
from shapely.geometry import Polygon, box

from app.roofclean import fit_hidden_edges, main_axis, orthogonalize, regularize_lines


def _seg(t, a, b, factor=1.0, **kw):
    pl = float(np.hypot(b[0] - a[0], b[1] - a[1]))
    return {"type": t, "a": list(a), "b": list(b), "plan_len": pl, "len": pl * factor, **kw}


def _rot(p, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


def test_orthogonalize_squares_jittery_rotated_outline():
    rng = np.random.default_rng(3)
    base = [(0, 0), (40, 0), (40, 12), (52, 12), (52, 30), (0, 30)]
    noisy = [_rot((x + rng.normal(0, 0.25), y + rng.normal(0, 0.25)), 17) for x, y in base]
    P = Polygon(noisy)
    Q = orthogonalize(P)
    assert abs(math.degrees(main_axis(P)) - 17) < 1.5
    ring = np.asarray(Q.exterior.coords)
    d = np.diff(ring, axis=0)
    ang = np.degrees(np.arctan2(d[:, 1], d[:, 0])) % 90
    assert np.all(np.minimum(np.abs(ang - ang[0]), 90 - np.abs(ang - ang[0])) < 0.01)  # all square
    assert abs(Q.area - 40 * 30 - 12 * 18) < 25


def test_orthogonalize_drops_cut_corner_keeps_long_bay():
    cut = Polygon([(0, 0), (40, 0), (40, 26), (37, 30), (0, 30)])  # 5 ft corner clipped by a tree
    Q = orthogonalize(cut)
    assert len(Q.exterior.coords) == 5 and abs(Q.area - 1200) < 1
    bay = Polygon([(0, 0), (40, 0), (40, 20), (32, 30), (0, 30)])  # 12.8 ft bay wall stays
    assert len(orthogonalize(bay).exterior.coords) == 6


def test_broken_ridge_becomes_one_line_and_meets_hips():
    out = box(0, 0, 40, 24)
    segs = [
        _seg("ridge", (8.3, 12.2), (17.0, 11.9), za=20, zb=20),
        _seg("ridge", (19.5, 12.1), (31.6, 11.8), za=20, zb=20),
        _seg("hip", (0.4, 0.6), (7.0, 11.0), 1.2),
        _seg("hip", (0.7, 23.3), (7.2, 13.4), 1.2),
        _seg("hip", (39.5, 0.5), (33.0, 10.9), 1.2),
        _seg("hip", (39.2, 23.6), (32.9, 13.2), 1.2),
    ]
    r = regularize_lines(segs, out)
    ridges = [s for s in r if s["type"] == "ridge"]
    assert len(ridges) == 1
    a, b = np.array(ridges[0]["a"]), np.array(ridges[0]["b"])
    assert abs(a[1] - b[1]) < 1e-6  # level with the walls
    ends = sorted([a[0], b[0]])
    hip_ends = [np.array(s[e]) for s in r if s["type"] == "hip" for e in ("a", "b")]
    for x in ends:  # both ridge ends are hip junctions
        assert sum(np.hypot(*(h - np.array([x, a[1]]))) < 1e-6 for h in hip_ends) == 2
    corners = {(0, 0), (40, 0), (40, 24), (0, 24)}
    assert sum(tuple(np.round(h, 6)) in corners for h in hip_ends) == 4  # hips end on corners
    hip = next(s for s in r if s["type"] == "hip")
    assert abs(hip["len"] / hip["plan_len"] - 1.2) < 1e-6  # slope factor kept


def test_fascia_strip_and_floating_steps_removed():
    out = box(0, 0, 40, 24)
    segs = [
        _seg("ridge", (0, 12), (40, 12), za=20, zb=20),
        _seg("eave", (1.5, 22.5), (38, 22.5), step=1),        # 1.5 ft from the outline
        _seg("flashing", (1.5, 22.5), (38, 22.5), step=1),
        _seg("flashing", (15, 5), (19, 5), step=2),            # attached to nothing
    ]
    r = regularize_lines(segs, out)
    assert [s["type"] for s in r] == ["ridge"]


def test_fit_hidden_edges_uses_lidar_extent():
    P = box(0, 0, 40, 30)
    rng = np.random.default_rng(0)
    pts = np.column_stack([rng.uniform(0.5, 36.0, 3000), rng.uniform(0.5, 29.5, 3000), np.full(3000, 15.0)])
    Q, n = fit_hidden_edges(P, pts, lambda xs, ys: np.asarray(xs) > 38)  # east wall under a tree
    assert n == 1
    assert abs(Q.bounds[2] - 36.75) < 0.4 and abs(Q.bounds[0]) < 1e-6


def test_short_notch_removed_real_wing_kept():
    from app.roofclean import orthogonalize
    notch = Polygon([(0, 0), (40, 0), (40, 25), (37.5, 25), (37.5, 30), (0, 30)])  # 2.5 ft x 5 ft trim leftover
    Q = orthogonalize(notch)
    assert len(Q.exterior.coords) == 5
    wing = Polygon([(0, 0), (40, 0), (40, 20), (25, 20), (25, 30), (0, 30)])  # a real 15 x 10 ft step
    assert len(orthogonalize(wing).exterior.coords) == 7


def test_deck_with_railing_is_not_roof():
    from app.roofplanes import GRID, _decks
    xs = np.arange(0.25, 20, GRID)
    ys = np.arange(19.75, 0, -GRID)
    L = np.zeros((len(ys), len(xs)), int)
    rng = np.random.default_rng(2)
    floor = np.column_stack([rng.uniform(0, 20, 600), rng.uniform(0, 20, 600), np.full(600, 100.0)])
    t = rng.uniform(0, 20, 120)
    rail = np.column_stack([np.r_[t, np.full(60, 19.8)], np.r_[np.full(120, 0.2), rng.uniform(0, 20, 60)],
                            np.full(180, 103.3)])
    plane = {"id": 0, "a": 0.0, "b": 0.0, "c": 100.0, "flat": True}
    assert _decks(L, xs, ys, np.vstack([floor, rail]), [plane]) == [0]
    assert _decks(L, xs, ys, floor, [plane]) == []  # flat roof: no railing
