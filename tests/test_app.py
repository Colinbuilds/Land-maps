"""The app imports and serves its pages (catches missing runtime deps)."""
from fastapi.testclient import TestClient

from app.main import app


def test_pages_and_health():
    assert TestClient(app).get("/roof").status_code == 404  # removed: reports live at /reports
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
    assert "Lawn" in row["summary"]["text"] and row["summary"]["mrr_cents"] == 11500  # CRM included
    # pausing blocks sign-in
    cl.patch(f"/api/admin/companies/{cid}/manage", json={"disabled": True})
    assert cl.get("/api/crm/home").status_code == 401


def test_report_sections_follow_trades():
    from app import trades
    d = {"pin": "x", "address": "1 Test St", "lines": {"eave": 100}, "squares": 20, "waste": [], "structures": [
        {"name": "House", "plan_sqft": 1500, "lines": {"eave": 100}, "facet_polys": [{"down": [0, -1], "pitch": 6, "area_sqft": 900}]}],
        "walls": {"gross_wall_sqft": 2000, "elevations": {"North": []}, "gutter_ft": 150},
        "yard": {"lawn_sqft": 5000, "lot_sqft": 9000, "year_built": 1990, "photo_jpg": "x"}}
    lawn = trades.filter_report(d, trades.sections_for(["lawn", "snow"]))
    assert lawn["sections"] == ["yard"] and "lines" not in lawn and "walls" not in lawn
    assert "facet_polys" not in lawn["structures"][0] and lawn["yard"]["lawn_sqft"] == 5000
    siding = trades.filter_report(d, trades.sections_for(["siding"]))
    assert siding["walls"]["gross_wall_sqft"] == 2000 and "elevations" in siding["walls"]
    assert siding["yard"]["basic"] and "lawn_sqft" not in siding["yard"] and siding["yard"]["year_built"] == 1990
    gut = trades.filter_report(d, trades.sections_for(["gutters"]))
    assert gut["lines"] and "elevations" not in gut["walls"]
    assert trades.filter_report(d, trades.sections_for([]))["sections"] == list(trades.SECTIONS)


def test_yearly_membership_is_25_percent_off():
    from app import billing
    assert billing.member_price_cents(["lawn", "snow"], yearly=True) == 11500 * 12 * 3 // 4  # $1,035
    assert billing.member_price_cents(["lawn", "snow", "gutters", "roofing"], crm=True, yearly=True) == round(13500 * 12 * 0.75)
    assert billing.monthly_value_cents({"member": True, "billing": "year", "trades": ["lawn"]}) == round(11500 * 0.75)
    assert TestClient(app).get("/api/billing/plans").json()["annual_discount"] == 0.25


def test_owner_test_drive():
    cl = TestClient(app)
    r = cl.post("/api/admin/test-drive", json={"trades": ["siding"], "member": True, "crm": True})
    assert r.status_code == 200
    me = cl.get("/api/roof/me").json()
    assert me["trades"] == ["siding"] and me["member"] and me["crm"]
    assert cl.get("/api/crm/home").status_code == 200
    lib = cl.get("/api/admin/library").json()
    assert not any(c["email"] == "testdrive@owner.invalid" for c in lib["companies"])
    cl2 = TestClient(app)
    r = cl2.get("/test-drive?trades=lawn,snow&member=0&crm=0&go=/reports", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/reports"
    me = cl2.get("/api/roof/me").json()
    assert me["trades"] == ["lawn", "snow"] and not me["member"] and me["credits"] >= 3


def test_admin_key_signs_in_on_customer_site(monkeypatch):
    import app.main as m
    cl = TestClient(app)
    r = cl.post("/api/roof/login", json={"email": "admin", "password": ""})
    assert r.status_code == 401 and "isn't set" in r.json()["detail"]  # no key set: off, and says so
    monkeypatch.setattr(m, "ADMIN_TOKEN", "owner-key-123")
    assert cl.post("/api/roof/login", json={"email": "admin", "password": "wrong"}).status_code == 401
    assert "isn't the admin key" in cl.post("/api/roof/login", json={"email": "admin", "password": "nope"}).json()["detail"]
    r = cl.post("/api/roof/login", json={"email": " Admin ", "password": " owner-key-123 "})  # phone spaces/caps
    assert r.status_code == 200 and r.json()["crm"] and r.json()["member"]
    assert cl.get("/api/crm/home").status_code == 200


def test_zip_totals_by_value_band(tmp_path, monkeypatch):
    from app import datastore as ds
    monkeypatch.setattr(ds, "ZIPS_DIR", tmp_path)
    rows = [{"address": "1 A St", "pin": "1", "home_value": 200000, "lawn_sqft": 5000, "driveway_sqft": 600, "lot_sqft": 9000},
            {"address": "2 A St", "pin": "2", "home_value": 300000, "lawn_sqft": 7000, "driveway_sqft": 800, "lot_sqft": 11000},
            {"address": "3 A St", "pin": "3", "home_value": 320000, "lawn_sqft": 6000, "driveway_sqft": 700, "lot_sqft": 10000}]
    (tmp_path / "99999_R.csv").write_text(ds.to_csv(rows, [(k, k) for k, _ in ds.COLUMNS]).replace(
        ",".join(k for k, _ in ds.COLUMNS), ",".join(k for k, _ in ds.COLUMNS), 1))
    (tmp_path / "99999_R.json").write_text('{"status": "ready"}')
    t = ds.totals_for_key("99999_R")
    assert t["all"]["homes"] == 3 and t["all"]["lawn_sqft"] == 18000
    b = {r["band"]: r for r in t["bands"]}
    assert b[0]["homes"] == 1 and b[1]["homes"] == 2 and b[1]["lawn_sqft"] == 13000
    assert ds.totals_all()["all"]["driveway_sqft"] == 2100


def test_signin_page_and_admin_check(monkeypatch):
    import app.main as m
    cl = TestClient(app)
    assert cl.get("/signin").status_code == 200
    assert cl.get("/api/admin/check").json()["open"] is True  # no key set
    monkeypatch.setattr(m, "ADMIN_TOKEN", "owner-key-123")
    assert cl.get("/api/admin/check", headers={"x-admin-token": "nope"}).status_code == 401
    assert cl.get("/api/admin/check", headers={"x-admin-token": "owner-key-123"}).json() == {"ok": True, "open": False}


def test_owner_names_first_last():
    from app.postcard import person_name as p
    assert p("ANDERSON MARK A") == "Mark Anderson"                       # Douglas
    assert p("RIGGS/MACK E & SUSANNE K") == "Mack & Susanne Riggs"       # Sarpy
    assert p("LAPE JR/LEROY W") == "Leroy Lape Jr"
    assert p("GRAY, GENE J & TERI L") == "Gene & Teri Gray"              # Lancaster / Cass
    assert p("WORTMANN, JOAN & DOSTAL, JORDAN T") == "Joan Wortmann & Jordan Dostal"
    assert p("PALMER, RALPH V & PATRICIA E  TTEES") == "Ralph & Patricia Palmer"
    assert p("O'BRIEN SEAN") == "Sean O'Brien"
    assert p("GRACE BAPTIST CHURCH") == "Grace Baptist Church"           # not a person: kept
    assert p("11207 LLC") == "11207 LLC"
    assert p("") == ""


def test_new_prices_and_crm_included():
    import time as _t
    from app import accounts, billing
    assert billing.plans()["credit_price_cents"] == 500 and billing.member_price_cents(["lawn"]) == 11500
    c = accounts.create_company("Crm Inc Test", f"ci{_t.time()}@x.test", "password123")
    assert not c["crm"]
    c = accounts.set_membership(c["id"], _t.time() + 86400, active=True)
    assert c["crm"] == 1  # every member has the CRM


def test_marketing_hub_upload_and_share(tmp_path, monkeypatch):
    from app import marketing as mk
    monkeypatch.setattr(mk, "FILES", tmp_path)
    cl = TestClient(app)
    d = cl.get("/api/admin/marketing").json()
    assert any("Launch plan" in i["title"] for i in d["items"])  # starter kit
    it = cl.post("/api/admin/marketing", json={"kind": "video", "title": "Lawn demo", "tags": ["Lawn", "TikTok"]}).json()
    r = cl.post(f"/api/admin/marketing/{it['id']}/file", files={"file": ("demo.mp4", b"\x00\x00\x00\x18ftypmp42" + b"0" * 2000, "video/mp4")})
    assert r.status_code == 200 and r.json()["kind"] == "video" and r.json()["size"] > 2000
    assert cl.get(f"/api/public/marketing/nope").status_code == 404
    tok = cl.post(f"/api/admin/marketing/{it['id']}/share?on=true").json()["token"]
    assert cl.get(f"/api/public/marketing/{tok}").json()["title"] == "Lawn demo"
    assert cl.get(f"/m/{tok}/file").content.startswith(b"\x00\x00\x00\x18ftyp")
    cl.post(f"/api/admin/marketing/{it['id']}/share?on=false")
    assert cl.get(f"/m/{tok}/file").status_code == 404
    cl.delete(f"/api/admin/marketing/{it['id']}")


def test_safety_lockouts_headers_backups_and_reset(monkeypatch, tmp_path):
    import app.main as m
    from app import accounts, safety
    cl = TestClient(app)
    r = cl.get("/terms")
    assert r.status_code == 200 and r.headers["x-frame-options"] == "SAMEORIGIN" and r.headers["x-content-type-options"] == "nosniff"
    for p in ("/privacy", "/refunds", "/reset", "/command/safety"):
        assert cl.get(p).status_code == 200
    # login lockout after 10 wrong passwords for one email
    em = f"lock{__import__('time').time()}@x.test"
    accounts.create_company("Lock Co", em, "password123")
    for _ in range(10):
        assert cl.post("/api/roof/login", json={"email": em, "password": "nope"}).status_code == 401
    r = cl.post("/api/roof/login", json={"email": em, "password": "password123"})
    assert r.status_code == 429 and "Too many" in r.json()["detail"]
    safety.succeeded("email:" + em, "login:testclient")
    # admin key lockout
    monkeypatch.setattr(m, "ADMIN_TOKEN", "k-123")
    for _ in range(10):
        cl.get("/api/admin/check", headers={"x-admin-token": "bad"})
    assert cl.get("/api/admin/check", headers={"x-admin-token": "k-123"}).status_code == 429
    safety.succeeded("admin:testclient")
    s = cl.get("/api/admin/safety", headers={"x-admin-token": "k-123"}).json()
    assert any(c["label"] == "Admin key set" and c["ok"] for c in s["checklist"])
    assert any(a["status"] == 401 for a in s["audit"])
    # backup
    monkeypatch.setattr(safety, "BACKUPS", tmp_path)
    b = cl.post("/api/admin/safety/backup", headers={"x-admin-token": "k-123"}).json()
    import zipfile
    names = zipfile.ZipFile(tmp_path / b["name"]).namelist()
    assert "roof.db" in names
    # password reset: one-time
    c = accounts.company_by_email(em)
    tok = accounts.make_reset(c["id"])
    assert cl.post("/api/roof/reset", json={"token": tok, "password": "newpass123"}).status_code == 200
    assert cl.post("/api/roof/reset", json={"token": tok, "password": "again1234"}).status_code == 400
    assert accounts.login(em, "newpass123")
    assert cl.post("/api/roof/forgot", json={"email": "nobody@x.test"}).json()["ok"]


def test_buy_several_zips_in_one_order(monkeypatch):
    from app import datastore as ds
    fake = {"count": 100, "blocked": None}
    monkeypatch.setattr(ds, "quote", lambda z, *a, **k: {**fake, "zip": z, "price_cents": 2500 if z != "68001" else 4000,
                                                         "value_lo": 0, "value_hi": 8, "new_types": ["lawn"]})
    g = ds.create_group("buyer@x.test", ["68022", "68001", "68022"], ["R"], ["lawn"])
    assert len(g["orders"]) == 2 and {o["zip"] for o in g["orders"]} == {"68022", "68001"}  # duplicates dropped
    assert all(o["grp"] == g["grp"] for o in g["orders"])
    assert not ds.group_paid(g["grp"], 1000)  # short payment: nothing marked
    monkeypatch.setattr(ds, "ensure_dataset", lambda *a, **k: "building")
    assert ds.group_paid(g["grp"], 6500)
    assert all(o["status"] == "processing" for o in ds.group_orders(g["grp"]))
    cl = TestClient(app)
    r = cl.get(f"/api/data/orders/{g['grp']}?t={g['token']}").json()
    assert len(r["orders"]) == 2 and r["amount_cents"] == 6500
    assert cl.get(f"/api/data/orders/{g['grp']}?t=wrong").status_code == 404
    fake["blocked"] = "ZIP is exclusive"
    import pytest
    with pytest.raises(PermissionError):
        ds.create_group("buyer@x.test", ["68022"], ["R"], ["lawn"])


def test_assessed_value_addon_and_totals(tmp_path, monkeypatch):
    from app import datastore as ds
    p = ds.pricing()
    assert ds.per_property_cents(["lawn"], p) == 10 and ds.per_property_cents(["lawn", "value"], p) == 15
    assert ds.per_property_cents(["value"], p) == 15  # can't be bought alone: comes with lawn
    assert ds.per_property_cents(["lawn", "driveway", "value"], p) == 20
    assert "home_value" not in [k for k, _ in ds.columns_for(["lawn"])]
    assert "home_value" in [k for k, _ in ds.columns_for(["lawn", "value"])]
    assert ds.measures_of(["value", "driveway"]) == ["driveway"]
    monkeypatch.setattr(ds, "ZIPS_DIR", tmp_path)
    rows = [{"address": "1 A St", "home_value": 200000, "lawn_sqft": 5000}, {"address": "2 A St", "home_value": 300000, "lawn_sqft": 7000}]
    (tmp_path / "99997_R.csv").write_text(ds.to_csv(rows, [(k, k) for k, _ in ds.COLUMNS]))
    t = ds.totals_for_key("99997_R")
    assert t["all"]["home_value"] == 500000 and any(c["key"] == "home_value" for c in t["columns"])


def test_coverage_fields_approve_and_reload(tmp_path, monkeypatch):
    from app import coverage as cv, counties
    g = cv.guess_fields([{"name": n} for n in ["FID", "ParcelId", "FullAddres", "City", "Zip", "TotalValue", "LandValue",
                                                "LotSqFt", "Class", "DataOwner", "OWN_NAME", "YrBuilt"]])
    assert g["pin"] == "ParcelId" and g["address"] == "FullAddres" and g["home_value"] == "TotalValue"
    assert g["owner"] == "OWN_NAME" and g["year_built"] == "YrBuilt" and g["lot_sqft"] == "LotSqFt"
    monkeypatch.setattr(cv, "CFG_DIR", tmp_path)
    monkeypatch.setattr(cv, "base_config", lambda fips: {"id": "test_xx", "name": "Test County, XX", "fips": "99999",
                                                         "crs": "EPSG:3857", "imagery": {"tile_url": "export:x"}})
    monkeypatch.setattr(cv, "auto_pull", lambda: [])
    cv._set("counties", "fips", "99999", name="Test County", state="XX", status="found")
    cfg = cv.approve("99999", "https://example.test/FeatureServer/0", {"pin": "PIN", "address": "ADDR", "lot_acres": "ACRES"},
                     "Class='R'", "esri")
    assert cfg["parcels"]["fields"]["lot_sqft"] == ("ACRES", 43560.0) and cfg["parcels"]["classes"]["R"] == "Class='R'"
    assert cfg["parcels"]["row_parcels"] and "World_Imagery" in cfg["imagery"]["tile_url"] and cfg["imagery"]["print_ok"] is False
    assert "test_xx" in counties.COUNTIES and cv.county("99999")["status"] == "live"
    counties.COUNTIES.pop("test_xx")
    assert cv.load_saved_configs() == 1 and counties.COUNTIES["test_xx"]["parcels"]["fields"]["lot_sqft"] == ("ACRES", 43560.0)
    counties.COUNTIES.pop("test_xx")


def test_zip_heat_counts_paid_purchases(monkeypatch):
    from app import datastore as ds
    monkeypatch.setattr(ds, "service_zips", lambda: ["99990", "99991"])
    monkeypatch.setattr(ds, "zip_shapes", lambda zs: {z: {"type": "Point", "coordinates": [0, 0]} for z in zs})
    monkeypatch.setattr(ds, "zip_points", lambda zs: ds._ZIP_POINTS.write_text(__import__("json").dumps({z: [41.0, -96.0] for z in zs})))
    monkeypatch.setitem(ds._heat, "at", 0)
    monkeypatch.setattr(ds, "list_orders", lambda n: [
        {"zip": "99990", "status": "ready", "amount_cents": 2500, "created": 1, "paid": 2},
        {"zip": "99990", "status": "processing", "amount_cents": 3000, "created": 1, "paid": 3},
        {"zip": "99991", "status": "awaiting_payment", "amount_cents": 2500, "created": 1}])
    h = ds.zip_heat()
    p = {r["zip"]: r for r in h["zips"]}
    assert p["99990"]["paid"] == 2 and p["99990"]["cents"] == 5500 and p["99991"]["paid"] == 0 and p["99991"]["pending"] == 1
    assert h["totals"]["bought"] == 1 and p["99990"]["lat"] == 41.0
    assert ds.zip_outlines(["99990"])["features"][0]["properties"]["zip"] == "99990"


def test_service_zips_asks_county_by_county(monkeypatch):
    from shapely.geometry import box
    from app import datastore as ds
    from app import gis
    monkeypatch.setattr(ds.counties, "boundaries", lambda: {"a": box(0, 0, 1, 1), "b": box(30, 30, 31, 31)})
    ds._svc_cache.clear()
    asked = []
    def fake(url, params, **k):
        asked.append(params["geometry"])
        x = float(params["geometry"].split(",")[0])
        ring = [[x, x], [x, x + .5], [x + .5, x + .5], [x + .5, x], [x, x]]  # clockwise = outer ring
        return {"features": [{"attributes": {"ZCTA5": f"{int(x):05d}"}, "geometry": {"rings": [ring]}}]}
    monkeypatch.setattr(gis, "get_json", fake)
    assert sorted(ds.service_zips()) == ["00000", "00030"] and len(asked) == 2
    ds._svc_cache.clear()


def test_auto_approve_rules(monkeypatch):
    from app import coverage as cv
    vals = {"TX": [{"value": "A1", "n": 900}, {"value": "F1", "n": 50}, {"value": "C1", "n": 10}],
            "IA": [{"value": "R", "n": 900}, {"value": "A", "n": 300}, {"value": "C", "n": 40}],
            "XX": [{"value": "Residential - Single Family", "n": 900}, {"value": "Vacant Residential", "n": 30},
                   {"value": "Commercial", "n": 40}],
            "ZZ": [{"value": "511", "n": 900}, {"value": "300", "n": 40}]}
    st = {"v": "TX"}
    monkeypatch.setattr(cv, "class_values", lambda url, f: vals[st["v"]])
    assert cv.guess_residential("u", "Class", "TX") == "Class IN ('A1')"
    st["v"] = "IA"
    assert cv.guess_residential("u", "Class", "IA") == "Class IN ('R')"  # "A" is farmland in Iowa
    st["v"] = "XX"
    assert cv.guess_residential("u", "Use", "XX") == "Use IN ('Residential - Single Family')"
    st["v"] = "ZZ"
    assert cv.guess_residential("u", "Code", "ZZ") is None  # unreadable codes: no filter, flagged
    calls = []
    monkeypatch.setattr(cv, "approve", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(cv, "guess_residential", lambda *a: None)
    monkeypatch.setattr(cv, "_county_geom", lambda f: None)
    monkeypatch.setattr(cv, "_type_fields", lambda u: [])
    broken = set()
    monkeypatch.setattr(cv, "live_query_ok", lambda u, g: u not in broken)
    good = {"url": "u1", "coverage": 4, "points": 5, "fields": {"pin": "P", "address": "A"}, "oid": "OBJECTID",
            "samples": [{"address": "1 Main"}, {"address": "2 Main"}], "item": "Parcels"}
    weak = {**good, "url": "u0", "coverage": 1}
    blank = {**good, "url": "u2", "samples": [{"address": ""}, {"address": ""}]}
    cv._set("counties", "fips", "99996", name="Auto Test", state="XX", status="found",
            candidates=__import__("json").dumps([weak, blank, good]))
    r = cv.auto_approve("99996")
    assert r["url"] == "u1" and calls[0][1] == "u1" and "homes-only filter (every parcel is included)" in r["check"]
    assert cv.county("99996")["needs_check"] == 1
    broken.add("u1")  # a layer whose real map searches fail is never approved
    cv._set("counties", "fips", "99995", name="Broken", state="XX", status="found",
            candidates=__import__("json").dumps([good]))
    assert cv.auto_approve("99995") is None and "fail" in cv.county("99995")["note"]


def test_queue_goes_omaha_first(monkeypatch):
    from app import coverage as cv
    from app import datastore as ds
    pts = {"68102": [41.26, -95.93], "50309": [41.59, -93.62], "80202": [39.75, -105.0], "66603": [39.05, -95.68],
           "64105": [39.10, -94.59]}
    monkeypatch.setattr(ds, "zip_points", lambda zs: {z: pts[z] for z in zs if z in pts})
    monkeypatch.setattr(cv, "metros", lambda: [
        {"cbsa": "19740", "zips": ["80202"], "lat": 39.7, "lon": -105.0},
        {"cbsa": "28140", "zips": ["64105"], "lat": 39.1, "lon": -94.6},
        {"cbsa": "19780", "zips": ["50309"], "lat": 41.6, "lon": -93.6},
        {"cbsa": "45820", "zips": ["66603"], "lat": 39.0, "lon": -95.7}])
    # Omaha, then the priority metros in order (Des Moines, KC, Denver), then the rest nearest first
    assert ds.queue_order(["66603", "80202", "64105", "50309", "68102", "99999"]) == ["68102", "50309", "64105", "80202", "66603", "99999"]


def test_cache_kept_while_disk_has_room(monkeypatch, tmp_path):
    from collections import namedtuple
    import shutil
    from app import gis
    monkeypatch.setattr(gis, "CACHE_DIR", tmp_path)
    (tmp_path / "bin").mkdir()
    for i in range(5):
        (tmp_path / "bin" / f"t{i}").write_bytes(b"x" * 1000)
    DU = namedtuple("DU", "total used free")
    monkeypatch.delenv("CACHE_MAX_GB", raising=False)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: DU(1e12, 2e11, 8e11))  # 1 TB, plenty free
    assert gis.prune_cache()["bytes"] == 5000  # nothing deleted
    monkeypatch.setattr(shutil, "disk_usage", lambda p: DU(1e12, 1e12 - 5e10 - 3000, 5e10 - 3000))  # under the 5% reserve
    assert gis.prune_cache()["bytes"] < 5000  # oldest trimmed


def test_home_is_the_buy_page(client=None):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200 and 'id="zipbox"' in r.text and 'id="member-value"' in r.text
    r = c.get("/data?zip=68022", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/?zip=68022#zipbox"


def test_learning_from_fixes(monkeypatch, tmp_path):
    import numpy as np
    from app import learning as L
    monkeypatch.setattr(L, "SAMPLES_DIR", tmp_path)
    monkeypatch.setattr(L, "MIN_FIXES", 1)
    L._models.clear()
    # a photo: left half green grass, right half grey paving
    rgb = np.zeros((40, 40, 3), np.float32)
    rgb[:, :20] = [0.35, 0.55, 0.25]
    rgb[:, 20:] = [0.6, 0.6, 0.6]
    F = L.features(rgb)
    L.add_pixels("test_cty", "src", F[:, 2:18].reshape(-1, 6), F[:, 22:38].reshape(-1, 6), "lawn_exclude", "x:1")
    wrong = np.zeros((40, 40), bool)
    wrong[:, :20] = True  # the colour rules called the grass paving
    area = np.ones((40, 40), bool)
    fixed = L.adjust_hard("test_cty", "src", rgb, wrong, area)
    assert fixed[:, 3:17].sum() == 0 and fixed[:, 23:37].all()  # learned: left is grass, right is paving
    assert L.adjust_hard("other_cty", "src", rgb, wrong, area) is wrong  # no fixes there: untouched
    # roof edits: a steady 3 ft east nudge becomes the county's default
    with L._db() as con:
        con.execute("DELETE FROM roof_edits WHERE county='roof_cty'")
        con.execute("DELETE FROM fixes WHERE county='test_cty'")
    a = {"type": "Polygon", "coordinates": [[[-96, 41], [-96, 41.0001], [-95.9999, 41.0001], [-95.9999, 41], [-96, 41]]]}
    e = {"type": "Polygon", "coordinates": [[[x + 3 / (364567 * np.cos(np.radians(41))), y] for x, y in a["coordinates"][0]]]}
    for i in range(5):
        L.record_roof_edit("roof_cty", f"p{i}", a, e)
    dx, dy = L.roof_shift_ft("roof_cty")
    assert 2.5 < dx < 3.5 and abs(dy) < 0.5
    # once outlines start pre-shifted, a roofer who leaves them alone keeps the shift
    L.record_roof_edit("roof_cty", "p6", e, e)
    assert 2.5 < L.roof_shift_ft("roof_cty")[0] < 3.5
