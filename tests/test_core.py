"""Offline unit tests (no network)."""
import numpy as np
from shapely.geometry import Polygon

from app.gis import _esri_to_geojson_geom
from app.measure import _longest_run, classify_hard, _extend_to_building
from app.pricing import DEFAULT_SETTINGS, estimate, price_service


def test_longest_run():
    assert _longest_run([0, 1, 1, 0, 1, 1, 1, 0]) == (4, 6)
    assert _longest_run([0, 0]) is None


def test_esri_rings_with_hole():
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]  # clockwise = outer in Esri
    hole = [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]]        # counter-clockwise = hole
    g = _esri_to_geojson_geom({"rings": [outer, hole]})
    poly = Polygon(g["coordinates"][0], g["coordinates"][1:])
    assert abs(poly.area - 96) < 1e-9


def test_classifier_separates_concrete_from_lawn():
    img = np.zeros((60, 60, 3), np.float32)
    img[:, :30] = np.array([186, 180, 153]) / 255  # sunlit concrete
    img[:, 30:] = np.array([131, 118, 84]) / 255   # dormant lawn
    hard = classify_hard(img)
    assert hard[10:50, 5:25].mean() > 0.95  # ignore image-border effects
    assert hard[:, 35:55].mean() < 0.05


def test_extend_driveway_through_shadow_but_not_lawn():
    H = W = 40
    drive = np.zeros((H, W), bool); drive[0:5, 10:20] = True   # slab at the street
    bldg = np.zeros((H, W), bool); bldg[30:40, :] = True        # house to the south
    allowed = np.ones((H, W), bool)
    lawn = np.zeros((H, W), bool)
    ext = _extend_to_building(drive, bldg, allowed, lawn, np.array([0.0, 1.0]), 40)
    assert ext[10:29, 12:18].all()            # filled down to the garage
    assert not ext[:, :8].any()               # but not sideways
    lawn[15, :] = True                        # a strip of sunlit grass blocks it
    ext2 = _extend_to_building(drive, bldg, allowed, lawn, np.array([0.0, 1.0]), 40)
    assert not ext2.any()


def test_pricing_minimum_and_range():
    svc = DEFAULT_SETTINGS["services"]["mowing"]
    p = price_service(svc, {"lawn_sqft": 1000}, 0.1)
    assert p["low"] == round(40 * 0.9) and p["high"] == round(40 * 1.1)
    p = price_service(svc, {"lawn_sqft": 20000}, 0.0)
    assert p["low"] == p["high"] == 110


def test_estimate_skips_missing_areas_and_applies_finish():
    m = {"lawn_sqft": 8000, "driveway_sqft": 600, "sidewalk_sqft": 0}
    e = estimate(m, DEFAULT_SETTINGS)
    assert "concrete_sidewalk" not in e
    e2 = estimate(m, DEFAULT_SETTINGS, finish="stamped")
    assert e2["concrete_driveway"]["low"] > e["concrete_driveway"]["low"]


def _lidar_grid(n=120):
    return (np.zeros((n, n), np.float32), np.full((n, n), -1.0, np.float32), np.full((n, n), 5, np.int16))


def test_lidar_trampoline_ring_is_filled_and_fence_dropped():
    from app.measure import _lidar_masks
    obj, veg, gnd = _lidar_grid()
    yy, xx = np.mgrid[0:120, 0:120]
    rr = np.hypot(yy - 40, xx - 40)
    obj[(rr > 14) & (rr < 17)] = 3.0       # trampoline frame, no returns from the mat
    obj[100:101, 5:115] = 5.0              # 1-px-wide fence line
    struct, bed, canopy = _lidar_masks(obj, veg, gnd, ft_px=0.5, sqft_px=0.25)
    assert struct[40, 40]                  # mat counted as structure
    assert not struct[100, 60]             # fence doesn't eat the lawn


def test_lidar_shrubs_become_beds_and_evergreens_excluded():
    from app.measure import _lidar_masks
    obj, veg, gnd = _lidar_grid()
    veg[20:30, 20:30] = 3.0                # shrub
    veg[20:30, 36:46] = 3.0                # neighbour shrub 3 ft away -> one bed
    veg[70:100, 70:100] = 25.0             # spruce, no ground returns beneath
    gnd[70:100, 70:100] = 0
    struct, bed, canopy = _lidar_masks(obj, veg, gnd, ft_px=0.5, sqft_px=0.25)
    assert bed[25, 33]                     # gap between shrubs is bed, not lawn
    assert canopy[85, 85]
    assert not bed[60, 60] and not canopy[60, 60]


def test_pool_detector():
    from app.measure import _pools
    img = np.zeros((40, 40, 3), np.float32)
    img[:] = np.array([131, 118, 84]) / 255
    img[10:30, 10:30] = np.array([60, 140, 190]) / 255
    m = _pools(img)
    assert m[20, 20] and not m[2, 2]
