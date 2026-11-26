"""Membership, credit packs and member pricing, with Stripe simulated."""
import hashlib
import hmac
import secrets
import time

from fastapi.testclient import TestClient

from app import accounts, billing, datastore
from app.main import app


def _join(client):
    email = f"co{secrets.token_hex(4)}@example.com"
    r = client.post("/api/join", json={"name": "Test Lawn Co", "email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()


def test_join_then_duplicate_email_refused():
    c = TestClient(app)
    co = _join(c)
    assert co["member"] is False and co["credits"] == 0
    assert c.get("/api/roof/me").json()["id"] == co["id"]  # logged in by the sign-up
    r = TestClient(app).post("/api/join", json={"name": "Other", "email": co["email"], "password": "password123"})
    assert r.status_code == 409


def test_membership_checkout_confirm_and_unlimited_reports(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    c = TestClient(app)
    co = _join(c)
    posted = {}

    def fake_post(path, data):
        posted[path] = data
        return {"url": "https://checkout.stripe.test/s"}
    monkeypatch.setattr(billing, "_post", fake_post)
    r = c.post("/api/billing/member")
    assert r.json()["checkout_url"].startswith("https://checkout")
    d = posted["/checkout/sessions"]
    assert d["mode"] == "subscription" and d["line_items[0][price_data][unit_amount]"] == "11500"
    assert d["line_items[0][price_data][recurring][interval]"] == "month"

    end = time.time() + 30 * 86400
    sub_id = f"sub_{secrets.token_hex(4)}"  # unique: the test database outlives one run
    sub = {"id": sub_id, "status": "active", "customer": "cus_1", "current_period_end": end}
    sess = {"id": "cs_1", "status": "complete", "metadata": {"company_id": str(co["id"]), "kind": "membership"},
            "subscription": sub}
    monkeypatch.setattr(billing, "_get", lambda path, params=None: sess)
    me = c.get("/api/billing/confirm", params={"session_id": "cs_1"}).json()
    assert me["member"] is True and me["stripe_sub"] == sub_id

    # members don't spend credits on reports
    rid = accounts.spend_and_save(co["id"], {"pin": "x", "address": "1 Test St"})
    assert rid and accounts.get_company(co["id"])["credits"] == 0

    # a cancelled subscription ends it
    billing.handle_event({"type": "customer.subscription.deleted",
                          "data": {"object": {**sub, "status": "canceled"}}})
    assert accounts.get_company(co["id"])["member"] is False


def test_member_pays_half_for_lists():
    full = datastore.list_price(1000, ["lawn", "driveway"], "standard", member=False)
    half = datastore.list_price(1000, ["lawn", "driveway"], "standard", member=True)
    assert full["price_cents"] == 15000 and half["price_cents"] == 7500
    prem = datastore.list_price(1000, None, "premier", member=False)
    assert prem["price_cents"] == 25000
    assert datastore.list_price(1000, None, "premier", member=True)["price_cents"] == 12500


def test_credit_pack_added_once(monkeypatch):
    co = accounts.create_company("Pack Co", f"p{secrets.token_hex(4)}@example.com", "password123")
    s = {"id": f"cs_{secrets.token_hex(3)}", "payment_status": "paid", "customer": "cus_9",
         "metadata": {"company_id": str(co["id"]), "kind": "credits", "qty": "5"}}
    billing._credit_once(co["id"], s)
    billing._credit_once(co["id"], s)  # return page + webhook both land
    assert accounts.get_company(co["id"])["credits"] == 5


def test_webhook_signature(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    body = b'{"type":"ping"}'
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_test", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    assert billing.verify_webhook(body, f"t={t},v1={sig}")
    assert not billing.verify_webhook(body, f"t={t},v1={'0' * 64}")
    assert not billing.verify_webhook(body + b" ", f"t={t},v1={sig}")
    old = str(int(time.time()) - 3600)
    sig_old = hmac.new(b"whsec_test", f"{old}.".encode() + body, hashlib.sha256).hexdigest()
    assert not billing.verify_webhook(body, f"t={old},v1={sig_old}")
    r = TestClient(app).post("/api/stripe/webhook", content=body, headers={"stripe-signature": "t=1,v1=bad"})
    assert r.status_code == 400
