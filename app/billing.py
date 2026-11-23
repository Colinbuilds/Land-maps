"""Memberships and credit packs through Stripe (REST, no SDK).

Membership: a monthly subscription (default $99). Members get unlimited
single property reports and half price on ZIP lists. Status is stored on
the company and kept current three ways, so a missed notification never
locks a paying member out or leaves a cancelled one in:
  * when they come back from Checkout (session confirmed with Stripe),
  * Stripe webhooks (STRIPE_WEBHOOK_SECRET; signature checked), and
  * a lazy re-check with Stripe whenever a membership looks expired.

Credit packs: one-time Checkout payments that add report credits.

Settings (owner, Business > Data sales / settings file):
  membership.price_cents   default 9900
  credits.price_cents      price of one report credit, default 2500
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import httpx

from . import accounts
from .pricing import load_settings

API = "https://api.stripe.com/v1"
DEFAULTS = {"member_price_cents": 9900, "credit_price_cents": 2500, "list_discount": 0.5}
CREDIT_PACKS = (1, 5, 10, 25)


def enabled() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def plans() -> dict:
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (load_settings().get("membership") or {}).items() if k in DEFAULTS})
    return p


def _auth():
    return (os.environ["STRIPE_SECRET_KEY"], "")


def _post(path: str, data: dict) -> dict:
    r = httpx.post(API + path, data=data, auth=_auth(), timeout=30)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message")
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(msg or f"Stripe error {r.status_code}")
    return r.json()


def _get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(API + path, params=params, auth=_auth(), timeout=30)
    r.raise_for_status()
    return r.json()


def _biz() -> str:
    return (load_settings().get("business") or {}).get("name") or "Property Data"


# --------------------------------------------------------------- membership
def member_checkout(c: dict, base_url: str) -> str:
    p = plans()
    data = {
        "mode": "subscription",
        "client_reference_id": str(c["id"]),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(int(p["member_price_cents"])),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": f"{_biz()} membership",
        "success_url": f"{base_url}/join/done?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/join?cancelled=1",
        "metadata[company_id]": str(c["id"]),
        "metadata[kind]": "membership",
        "subscription_data[metadata][company_id]": str(c["id"]),
    }
    if c.get("stripe_customer"):
        data["customer"] = c["stripe_customer"]
    else:
        data["customer_email"] = c["email"]
    return _post("/checkout/sessions", data)["url"]


def _apply_subscription(cid: int, sub: dict) -> dict | None:
    status = sub.get("status")
    active = status in ("active", "trialing", "past_due")
    end = sub.get("current_period_end")
    if end is None:  # newer API versions keep the period on the subscription item
        items = (sub.get("items") or {}).get("data") or []
        end = items[0].get("current_period_end") if items else None
    until = float(end) if end else (time.time() + 31 * 86400 if active else time.time())
    return accounts.set_membership(cid, until if active else time.time(), sub.get("customer"), sub.get("id"), active)


def confirm_session(session_id: str) -> dict | None:
    """After Checkout: activate a membership or add purchased credits."""
    s = _get(f"/checkout/sessions/{session_id}", {"expand[]": "subscription"})
    cid = int((s.get("metadata") or {}).get("company_id") or s.get("client_reference_id") or 0)
    if not cid:
        return None
    kind = (s.get("metadata") or {}).get("kind")
    if kind == "membership" and s.get("status") == "complete":
        sub = s.get("subscription")
        if isinstance(sub, str):
            sub = _get(f"/subscriptions/{sub}")
        if sub:
            return _apply_subscription(cid, sub)
    if kind == "credits" and s.get("payment_status") == "paid":
        _credit_once(cid, s)
    return accounts.get_company(cid)


def refresh(c: dict | None) -> dict | None:
    """Re-check an expired-looking membership with Stripe (renewals)."""
    if not c or not enabled() or not c.get("stripe_sub") or c.get("member"):
        return c
    try:
        return _apply_subscription(c["id"], _get(f"/subscriptions/{c['stripe_sub']}"))
    except Exception:
        return c


def portal_url(c: dict, base_url: str) -> str:
    """Stripe's page where a member updates their card or cancels."""
    if not c.get("stripe_customer"):
        raise RuntimeError("no billing account yet")
    return _post("/billing_portal/sessions", {"customer": c["stripe_customer"],
                                              "return_url": f"{base_url}/roof"})["url"]


# ------------------------------------------------------------ credit packs
def credits_checkout(c: dict, qty: int, base_url: str) -> str:
    if qty not in CREDIT_PACKS:
        raise ValueError("pick a credit pack")
    p = plans()
    data = {
        "mode": "payment",
        "client_reference_id": str(c["id"]),
        "line_items[0][quantity]": str(qty),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(int(p["credit_price_cents"])),
        "line_items[0][price_data][product_data][name]": f"{_biz()} property report credit",
        "success_url": f"{base_url}/join/done?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/join?cancelled=1",
        "metadata[company_id]": str(c["id"]),
        "metadata[kind]": "credits",
        "metadata[qty]": str(qty),
    }
    if c.get("stripe_customer"):
        data["customer"] = c["stripe_customer"]
    else:
        data["customer_email"] = c["email"]
        data["customer_creation"] = "always"
    return _post("/checkout/sessions", data)["url"]


def _credit_once(cid: int, s: dict):
    """Add a paid pack's credits exactly once (return page and webhook can both land)."""
    qty = int((s.get("metadata") or {}).get("qty") or 0)
    if qty <= 0:
        return
    reason = f"stripe {s['id']}"
    with accounts._db() as con:
        if con.execute("SELECT 1 FROM ledger WHERE reason=?", (reason,)).fetchone():
            return
    accounts.add_credits(cid, qty, reason)
    if s.get("customer"):
        with accounts._lock, accounts._db() as con:
            con.execute("UPDATE companies SET stripe_customer=COALESCE(stripe_customer, ?) WHERE id=?",
                        (s["customer"], cid))


# ----------------------------------------------------------------- webhook
def verify_webhook(payload: bytes, sig_header: str | None, tolerance: int = 600) -> bool:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret or not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    t = parts.get("t")
    sigs = [v for k, v in (p.split("=", 1) for p in sig_header.split(",") if "=" in p) if k == "v1"]
    if not t or not sigs:
        return False
    try:
        if abs(time.time() - int(t)) > tolerance:
            return False
    except ValueError:
        return False
    good = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(good, s) for s in sigs)


def handle_event(ev: dict) -> str:
    """Apply a verified Stripe event. Returns what was done (for logs)."""
    typ = ev.get("type", "")
    obj = (ev.get("data") or {}).get("object") or {}
    if typ == "checkout.session.completed":
        kind = (obj.get("metadata") or {}).get("kind")
        if kind in ("membership", "credits"):
            confirm_session(obj["id"])
            return f"{kind} confirmed"
        oid = (obj.get("metadata") or {}).get("order_id")
        if oid and obj.get("payment_status") == "paid":
            from . import datastore
            o = datastore.get_order(oid)
            if o and o["status"] == "awaiting_payment" and int(obj.get("amount_total") or 0) >= int(o["amount_cents"]):
                datastore.mark_paid(oid, note="stripe")
                return f"order {oid} paid"
        return "ignored"
    if typ.startswith("customer.subscription."):
        c = accounts.company_by_sub(obj.get("id", ""))
        if not c:
            cid = int((obj.get("metadata") or {}).get("company_id") or 0)
            c = accounts.get_company(cid) if cid else None
        if c:
            _apply_subscription(c["id"], obj)
            return f"membership updated for company {c['id']} ({obj.get('status')})"
    return "ignored"
