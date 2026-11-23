import numpy as np
from shapely.geometry import Polygon, box

from app.measure import _trees
from app.lidar import EptLidar
from app.trees import TreeGrid, tree_grid


def _cone(H, W, cy, cx, height, radius, ft_px):
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.hypot(yy - cy, xx - cx) * ft_px
    return np.where(d < radius, height * (1 - 0.5 * d / radius), -1.0)


def test_trees_counts_separate_crowns_and_skips_hedges():
    ft_px = 0.5
    H = W = 200
    veg = np.full((H, W), -1.0)
    veg = np.maximum(veg, _cone(H, W, 60, 60, 40, 14, ft_px))     # big tree
    veg = np.maximum(veg, _cone(H, W, 60, 140, 25, 9, ft_px))     # smaller tree
    veg[150:160, 20:180] = 5.0                                    # 5 ft hedge
    crown, tops = _trees(veg, ft_px, ft_px * ft_px)
    assert len(tops) == 2
    assert not crown[150:160, 20:180].any()
    hts = sorted(t[2] for t in tops)
    assert 20 < hts[0] < 26 and 34 < hts[1] < 41


def test_lumpy_crown_is_one_tree():
    ft_px = 0.5
    H = W = 160
    veg = _cone(H, W, 80, 80, 45, 18, ft_px)
    rng = np.random.default_rng(1)
    veg = np.where(veg > 0, veg + rng.normal(0, 1.5, veg.shape), veg)
    _, tops = _trees(veg, ft_px, ft_px * ft_px)
    assert len(tops) == 1


def test_tree_grid_polygon_and_lookup():
    m = np.zeros((20, 20), bool)
    m[5:15, 5:15] = True
    g = TreeGrid(m, 0.0, 20.0)
    assert g.at([10.0], [10.0])[0] and not g.at([1.0], [1.0])[0]
    poly = g.polygon(clip=box(0, 0, 10, 20))
    assert 40 < poly.area < 55


class _FakeCountyLidar:
    """County rasters: roof (obj) 15 ft, a tree 30 ft over one corner."""
    def sample(self, env, xs, ys):
        obj = np.where((xs >= 0) & (xs <= 40) & (ys >= 0) & (ys <= 30), 15.0, -1.0)
        veg = np.where((xs > 30) & (ys > 20), 30.0, -1.0)
        veg = np.where((xs < 0) & (xs > -3), 12.0, veg)  # shrub against the wall, lower than the roof
        return {"obj": obj, "veg": veg, "gnd": np.zeros(xs.shape)}


def test_tree_grid_county_finds_overhang_not_shrubs():
    roof = box(0, 0, 40, 30)
    tg = tree_grid(_FakeCountyLidar(), None, roof.buffer(2))
    cover = tg.polygon(clip=roof)
    assert cover is not None
    assert 60 < cover.area < 130          # the 10 x 10 corner
    assert not tg.at([-1.5], [15.0])[0]   # shrub below roof height


def test_branch_points_flagged_roof_points_kept():
    rng = np.random.default_rng(0)
    # planar roof: 400 single-return points on a 6/12 plane, 10-20 ft up
    rx, ry = rng.uniform(0, 30, 400), rng.uniform(0, 20, 400)
    rz = 110 + 0.5 * ry
    # tree crown over one side: scattered single + multi-return points
    tx, ty = rng.uniform(32, 42, 150), rng.uniform(0, 20, 150)
    tz = rng.uniform(115, 135, 150)
    n = len(rx) + len(tx)
    r = {"x": np.concatenate([rx, tx]), "y": np.concatenate([ry, ty]), "z": np.concatenate([rz, tz]),
         "cls": np.ones(n, int), "nret": np.concatenate([np.ones(len(rx), int), np.where(np.arange(150) % 2, 2, 1)])}
    h = r["z"] - 100
    cand = r["nret"] <= 1
    flag = EptLidar._tree_single(r, cand, h)
    roof_flagged = flag[:len(rx)].mean()
    tree_single = cand[len(rx):]
    tree_flagged = flag[len(rx):][tree_single].mean()
    assert roof_flagged < 0.05
    assert tree_flagged > 0.7


def test_trace_outline_ignores_crown_edge_over_roof():
    from app.photoedge import trace_outline

    class S:  # roof edges right on the outline, plus a crown edge 3.5 ft out east
        def edge_strength(self, xs, ys, nx, ny):
            d = np.minimum.reduce([np.abs(xs), np.abs(xs - 43.5), np.abs(ys), np.abs(ys - 30)])
            return np.exp(-d ** 2)

    poly = Polygon([(0, 0), (40, 0), (40, 30), (0, 30)])
    free, _ = trace_outline(S(), poly)
    treed, st = trace_outline(S(), poly, is_tree=lambda xs, ys: np.asarray(xs) > 35)
    assert free.bounds[2] > 42.5          # without tree info it follows the crown
    assert abs(treed.bounds[2] - 40) < 0.6
    assert st["tree_edges"] == 1
