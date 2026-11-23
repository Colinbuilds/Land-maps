"""The app imports and serves its pages (catches missing runtime deps)."""
from fastapi.testclient import TestClient

from app.main import app


def test_pages_and_health():
    c = TestClient(app)
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/").status_code == 200
    assert c.get("/business").status_code == 200


def test_data_pricing_per_measurement_type():
    from app import datastore
    p = {"rate_cents": 10.0, "extra_type_cents": 5.0, "min_order_usd": 25.0}
    assert datastore.per_property_cents(["lawn"], p) == 10
    assert datastore.per_property_cents(["lawn", "driveway", "sidewalk"], p) == 20
    assert datastore.per_property_cents([], p) == 10          # nothing picked = lawn
    assert datastore.per_property_cents(["bogus"], p) == 10   # unknown types ignored
    assert datastore.price_cents(1000, ["lawn", "driveway"], p) == 15000
    assert datastore.price_cents(10, ["lawn"], p) == 2500     # $25 minimum
    cols = [k for k, _ in datastore.columns_for(["driveway"])]
    assert "driveway_sqft" in cols and "lawn_sqft" not in cols and cols[0] == "address" and "quality" in cols
    cols = [k for k, _ in datastore.columns_for(["lawn", "walls"])]
    assert "parkway_sqft" in cols and "wall_sqft" in cols and "est_windows" in cols


def test_mailer_is_silent_without_settings(monkeypatch):
    from app import mailer
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert not mailer.enabled()
    mailer.send("someone@example.com", "x", "y")  # no error, nothing sent
