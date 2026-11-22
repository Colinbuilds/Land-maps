"""Plane engine on synthetic LiDAR-like point clouds with known answers."""
import math

import numpy as np
from shapely.geometry import box

from app.roofplanes import analyze_roof_points

rng = np.random.default_rng(1)


def _cloud(fn, x0, y0, w, h, density=1.2, noise=0.08):
    n = int(w * h * density)
    x = rng.uniform(x0, x0 + w, n)
    y = rng.uniform(y0, y0 + h, n)
    return np.column_stack([x, y, fn(x, y) + rng.normal(0, noise, n)])


def test_gable_4_12():
    p = 4 / 12
    pts = _cloud(lambda x, y: 1000 + 10 + p * (15 - np.abs(y - 2015)), 1000, 2000, 40, 30)
    r = analyze_roof_points(box(1000, 2000, 1040, 2030), pts)
    k = math.sqrt(1 + p * p)
    assert r["predominant_pitch"] == 4
    assert abs(r["roof_area_sqft"] - 1200 * k) / (1200 * k) < 0.02
    L = r["lines"]
    assert abs(L["ridge"] - 40) / 40 < 0.05
    assert abs(L["eave"] - 80) / 80 < 0.05
    assert abs(L["rake"] - 60 * k) / (60 * k) < 0.05


def test_two_levels_give_eave_and_flashing():
    """A 5/12 lower shed roof against the wall of a taller 4/12 gable."""
    up = lambda x, y: 1020 + (4 / 12) * (15 - np.abs(y - 2015))
    low = lambda x, y: 1008 + (5 / 12) * (y - 1990)  # slopes down to the south, top edge at y=2000
    pts = np.vstack([_cloud(up, 1000, 2000, 40, 30), _cloud(low, 1000, 1990, 40, 9.5)])
    r = analyze_roof_points(box(1000, 1990, 1040, 2030), pts)
    L = r["lines"]
    assert sorted(r["pitch_areas"]) == [4, 5]
    # upper roof's south eave sits over the lower roof; the lower roof's top
    # edge meets the wall: ~40 ft of each
    assert abs(L["flashing"] - 40) < 4
    assert abs(L["eave"] - (40 + 40 + 40)) < 8  # upper north + upper south + lower south
