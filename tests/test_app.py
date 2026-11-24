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


def test_value_bands_and_band_pricing():
    from app import datastore
    from app.bulk import band_of, band_label, VALUE_EDGES
    assert len(VALUE_EDGES) == 8 and VALUE_EDGES[0] == 250_000 and VALUE_EDGES[-1] == 2_000_000
    assert band_of(None) == 0 and band_of(249_999) == 0 and band_of(250_000) == 1 and band_of(3_000_000) == 8
    assert band_label(0) == "Under $250k" and band_label(4) == "$1M-$1.25M" and band_label(8) == "$2M+"
    assert datastore.range_label(0, 8) == "All home values"
    assert datastore.range_label(2, 8) == "Homes valued $500k+"
    assert datastore.range_label(0, 0) == "Homes valued under $250k"
    assert datastore.range_label(1, 2) == "Homes valued $250k-$750k"
    p = dict(datastore.DEFAULT_PRICING)
    bands = [100, 100, 100, 0, 0, 0, 0, 0, 10]   # homes per $250k band
    allp = datastore.list_price(0, ["lawn"], "standard", False, p, bands=bands)
    assert allp["count"] == 310
    # 100*10*1 + 100*10*1.25 + 100*10*1.5 + 10*10*5 = 4250 cents
    assert allp["price_cents"] == 4250
    hi = datastore.list_price(0, ["lawn"], "standard", False, p, bands=bands, lo=2)
    assert hi["count"] == 110 and hi["price_cents"] == 2500   # 2000 -> $25 minimum
    rows = [{"home_value": "200000"}, {"home_value": "600000"}, {"home_value": ""}, {"home_value": "2500000"}]
    assert len(datastore.rows_in_bands(rows, 2, 8)) == 2
