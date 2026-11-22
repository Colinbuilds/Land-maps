"""Roof engine on synthetic 8/12 roofs whose true measurements are known."""
import math

import numpy as np
from shapely.geometry import box

from app.roof import analyze_roof, pitch_factors

P = 8 / 12


def _sampler(height_fn):
    return lambda xs, ys: height_fn(xs, ys).astype(np.float32)


def test_gable_roof_40x30():
    # ridge runs east-west through y=15; eaves at y=0 and y=30
    outline = box(1000, 2000, 1040, 2030)
    h = lambda xs, ys: np.where((xs >= 1000) & (xs <= 1040) & (ys >= 2000) & (ys <= 2030),
                                10 + P * (15 - np.abs(ys - 2015)), 0.0)
    r = analyze_roof(outline, _sampler(h), 8)
    L = r["lines"]
    assert abs(r["roof_area_sqft"] - 1200 * math.sqrt(1 + P * P)) < 1
    assert abs(L["ridge"] - 40) < 6
    assert abs(L["eave"] - 80) < 8
    assert abs(L["rake"] - 4 * 15 * math.sqrt(1 + P * P)) < 10
    assert L["hip"] < 5 and L["valley"] < 5


def test_hip_roof_40x30():
    outline = box(1000, 2000, 1040, 2030)

    def h(xs, ys):
        d = np.minimum.reduce([xs - 1000, 1040 - xs, ys - 2000, 2030 - ys])  # distance to nearest eave
        return np.where(d >= 0, 10 + P * d, 0.0)
    r = analyze_roof(outline, _sampler(h), 8)
    L = r["lines"]
    hip_true = 4 * 15 * math.sqrt(2) * pitch_factors(8)["hip"]
    assert abs(L["eave"] - 140) < 10
    assert L["rake"] < 8
    assert abs(L["hip"] - hip_true) < 0.2 * hip_true
    assert abs(L["ridge"] - 10) < 6


def test_waste_suggestion_scales_with_cut_up_roofs():
    from app.roofreport import suggested_waste
    assert suggested_waste({"hip": 0, "valley": 0}, 2000) == 6
    assert suggested_waste({"hip": 150, "valley": 100}, 2000) == 12
