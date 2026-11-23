import numpy as np
from shapely.geometry import box

from app.walls import measure_walls


class FakeLidar:
    """Roof heights above ground for made-up buildings (LAS feet)."""
    def __init__(self, fn):
        self.fn = fn

    def sample(self, env, xs, ys):
        return {"obj": self.fn(np.asarray(xs, float), np.asarray(ys, float))}


def gable(xs, ys):
    # 40 x 30 ft roof (edge to edge), ridge along x at y=15: 10 ft eaves, 15 ft ridge
    inside = (xs >= 0) & (xs <= 40) & (ys >= 0) & (ys <= 30)
    return np.where(inside, 10 + 5 * (1 - np.abs(ys - 15) / 15), -1.0)


def test_gable_house_wall_area():
    w = measure_walls(FakeLidar(gable), None, box(0, 0, 40, 30), overhang=1.0)
    # walls 38 x 28: two 10-ft eave walls + two gable ends (rectangle + triangle)
    expect = 2 * 38 * 10 + 2 * (28 * 10 + 0.5 * 28 * 5 * 28 / 30)
    assert abs(w["gross_wall_sqft"] - expect) / expect < 0.05
    assert w["by_side"]["North"] == w["by_side"]["South"]
    assert w["by_side"]["East"] == w["by_side"]["West"]
    assert w["by_side"]["East"] > 0 and w["outside_corners_ft"] > 30


def two_story(xs, ys):
    # flat-ish test: west half 20 ft tall, east half 10 ft
    inside = (xs >= 0) & (xs <= 40) & (ys >= 0) & (ys <= 30)
    return np.where(inside, np.where(xs < 20, 20.0, 10.0), -1.0)


def test_upper_wall_above_lower_roof_counted():
    w = measure_walls(FakeLidar(two_story), None, box(0, 0, 40, 30), overhang=1.0)
    outer = 2 * (19 * 20 + 19 * 10) + 28 * 20 + 28 * 10  # N+S split, W tall, E short
    upper = 26 * 10  # the step wall (inside the 1-ft margin), facing east
    assert abs(w["gross_wall_sqft"] - (outer + upper)) / (outer + upper) < 0.06
    assert any(f["kind"] == "upper wall" and f["side"] == "East" for f in w["facets"])


def test_deck_edge_is_not_wall():
    def deck(xs, ys):
        house = (xs >= 0) & (xs <= 30) & (ys >= 0) & (ys <= 30)
        dk = (xs > 30) & (xs <= 42) & (ys >= 5) & (ys <= 25)
        return np.where(house, 10.0, np.where(dk, 4.0, -1.0))
    outline = box(0, 0, 30, 30).union(box(30, 5, 42, 25))
    w = measure_walls(FakeLidar(deck), None, outline, overhang=1.0)
    # the house wall behind the deck counts (full height); the deck's own edges don't
    assert any(f["kind"] == "upper wall" and f["side"] == "East" and f["height_ft"] >= 9 for f in w["facets"])
    assert w["gross_wall_sqft"] < 4 * 30 * 10 + 50
