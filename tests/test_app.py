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


def test_trade_catalog_and_metrics():
    from app import trades, datastore
    keys = trades.TRADE_KEYS
    assert {"lawn", "pressure", "gutters", "lights", "solar", "roofing", "siding", "windows"} <= set(keys)
    for t in trades.catalog():  # list presets only use sellable measurement types
        assert set(t["types"]) <= set(datastore.TYPE_KEYS), t["key"]
    d = {"roof_sqft": 2000, "squares": 20.0, "pitch_label": "6/12", "facets": 4, "tree_overhang_sqft": 120,
         "lines": {"eave": 120, "rake": 80, "ridge": 40, "hip": 0, "valley": 0},
         "walls": {"gross_wall_sqft": 2500, "est_openings": 15, "stories": 2, "gutter_ft": 120, "downspouts": 4,
                   "by_side": {"North": 600, "East": 650, "South": 600, "West": 650}},
         "yard": {"lawn_sqft": 5000, "driveway_sqft": 600, "other_hardscape_sqft": 100, "sidewalk_sqft": 300,
                  "year_built": 1999, "home_value": 450000, "fence_ft": 80, "fenced_yard": True},
         "structures": [{"facet_polys": [{"pitch": 6, "area_sqft": 1000, "down": [0, -1]},
                                          {"pitch": 6, "area_sqft": 1000, "down": [0, 1]}]}]}
    m = {x["label"]: x for x in trades.metrics("lights", d)}
    assert m["Roofline total (eaves + rakes + ridges)"]["value"] == 240
    m = {x["label"]: x for x in trades.metrics("pressure", d)}
    assert m["Driveway + patios + sidewalk"]["value"] == 1000
    m = {x["label"]: x for x in trades.metrics("solar", d)}
    assert m["Roof facing south"]["value"] == 1000 and m["Roof facing north"]["value"] == 1000
    m = {x["label"]: x for x in trades.metrics("roofing", d)}
    assert m["Year built"]["value"] == "1999"                    # not "1,999"
    assert all(x["beta"] for x in trades.metrics("fencing", d) if "ence" in x["label"])


def test_exclusive_zip_locks_and_pricing(monkeypatch):
    import secrets
    from app import datastore as ds
    z = "9" + secrets.token_hex(2)[:4].translate(str.maketrans("abcdef", "123456"))  # a ZIP no one uses
    a, b = f"a{z}@x.test", f"b{z}@x.test"
    # $1/home first type, 50c each extra; whole ZIP
    p = ds.exclusive_price(z, 1000, ["lawn", "driveway"], a)
    assert p["price_cents"] == 1000 * 150 and p["new_types"] == ["lawn", "driveway"]
    # buyer A locks lawn
    o = {"id": "T" + z, "zip": z, "email": a, "types": "lawn", "new_types": "lawn"}
    assert ds._grant_locks(o) == []
    # B can't buy lawn here (standard, exclusive or Premier) but can buy driveway
    assert "exclusive" in ds.sale_blocked(z, ["lawn", "driveway"], "standard", b)
    assert ds.sale_blocked(z, ["driveway"], "standard", b) is None
    assert ds.sale_blocked(z, ["driveway"], "premier", b)
    assert ds.sale_blocked(z, ["lawn"], "exclusive", b)
    # A can still buy their own ZIP, and adds driveway at 50c/home
    assert ds.sale_blocked(z, ["lawn"], "premier", a) is None
    p = ds.exclusive_price(z, 1000, ["lawn", "driveway"], a)
    assert p["new_types"] == ["driveway"] and p["price_cents"] == 1000 * 50
    # B's later exclusive on lawn is refused at payment time
    assert ds._grant_locks({"id": "U" + z, "zip": z, "email": b, "types": "lawn", "new_types": "lawn"}) == ["lawn"]
    # free samples hide the locked columns from others
    rows, taken = ds.strip_locked([{"lawn_sqft": 5000, "driveway_sqft": 600}], z, b)
    assert taken == ["lawn"] and rows[0]["lawn_sqft"] == "" and rows[0]["driveway_sqft"] == 600
    ds.release_lock(z, "lawn")
    assert ds.sale_blocked(z, ["lawn"], "standard", b) is None


def test_crm_flow_and_command_center():
    import datetime as dt
    import secrets
    from app import accounts
    cl = TestClient(app)
    em = f"crm{secrets.token_hex(4)}@x.test"
    r = cl.post("/api/join", json={"name": "Green Test Co", "email": em, "password": "password123", "trades": ["lawn", "snow"]})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    # CRM needs membership
    assert cl.put("/api/roof/crm", json={"on": True}).status_code == 402
    accounts.set_membership(cid, __import__("time").time() + 86400 * 30, active=True)
    assert cl.put("/api/roof/crm", json={"on": True}).json()["crm"] == 1
    cu = cl.post("/api/crm/customers", json={"name": "Pat Sample", "address": "1 Test St", "phone": "4025550100"}).json()
    e = cl.post("/api/crm/estimates", json={"customer_id": cu["id"], "items": [{"desc": "Mow", "price": 45}, {"desc": "Edge", "price": 10}]}).json()
    assert e["total_cents"] == 5500 and e["status"] == "draft"
    s = cl.post(f"/api/crm/estimates/{e['id']}/send").json()
    tok = s["link"].rsplit("/", 1)[1]
    pub = cl.get(f"/api/public/estimate/{tok}").json()
    assert pub["total_cents"] == 5500 and "company_id" not in pub
    assert cl.post(f"/api/public/estimate/{tok}", json={"accept": True}).json()["status"] == "accepted"
    today = dt.date.today().isoformat()
    jobs = cl.post(f"/api/crm/estimates/{e['id']}/schedule", json={"start": today, "repeat": "weekly"}).json()
    assert len(jobs) == 2
    wk = cl.get(f"/api/crm/schedule?start={today}&days=14").json()
    assert len(wk) == 4  # two jobs, weekly, two weeks
    v = cl.post("/api/crm/visits", json={"job_id": jobs[0]["id"], "day": today, "status": "done", "invoice": True}).json()
    assert v["invoice_id"]
    inv = cl.get("/api/crm/invoices").json()[0]
    assert inv["total_cents"] == 4500 and inv["status"] == "unpaid"
    assert cl.put(f"/api/crm/invoices/{inv['id']}", json={"status": "paid", "method": "card"}).json()["status"] == "paid"
    m = cl.get("/api/crm/money").json()
    assert m["paid_this_month"] == 4500 and m["win_rate"] == 100
    # another company can't see these
    cl2 = TestClient(app)
    cl2.post("/api/join", json={"name": "Other Co", "email": "o" + em, "password": "password123"})
    assert cl2.get(f"/api/crm/customers/{cu['id']}").status_code in (402, 404)
    # owner library has the company with a summary
    lib = cl.get("/api/admin/library").json()
    row = next(c for c in lib["companies"] if c["id"] == cid)
    assert "Lawn" in row["summary"]["text"] and row["summary"]["mrr_cents"] == 9900 + 2500
    # pausing blocks sign-in
    cl.patch(f"/api/admin/companies/{cid}/manage", json={"disabled": True})
    assert cl.get("/api/crm/home").status_code == 401
