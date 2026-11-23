"""Email through Gmail (SMTP with an app password).

Settings (environment variables, e.g. in Railway):
  GMAIL_USER          the Gmail / Google Workspace address that sends
  GMAIL_APP_PASSWORD  a 16-character app password for that account
                      (Google Account > Security > 2-Step Verification > App passwords)
  NOTIFY_EMAIL        where owner notifications go (default: GMAIL_USER)
  SITE_URL            public address of the site, e.g. https://www.example.com
                      (default: the address the visitor used)

Without GMAIL_USER / GMAIL_APP_PASSWORD nothing is sent and the site works
as before. Mail goes out on a background thread so a slow or failing mail
server never holds up a page.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("mailer")


def enabled() -> bool:
    return bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"))


def owner_address() -> str | None:
    return os.environ.get("NOTIFY_EMAIL") or os.environ.get("GMAIL_USER")


def site_url(fallback: str = "") -> str:
    return (os.environ.get("SITE_URL") or fallback or "").rstrip("/")


def _send_now(to: str, subject: str, text: str, reply_to: str | None = None) -> bool:
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw and to):
        return False
    try:
        from .pricing import load_settings
        name = (load_settings().get("business") or {}).get("name") or "Property Data"
    except Exception:
        name = "Property Data"
    msg = EmailMessage()
    msg["From"] = formataddr((name, user))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(user, pw.replace(" ", ""))
            s.send_message(msg)
        return True
    except Exception as e:  # never break a request because of email
        log.warning("email to %s failed: %s", to, e)
        return False


def send(to: str | None, subject: str, text: str, reply_to: str | None = None) -> None:
    """Send in the background (no-op when email isn't set up)."""
    if not enabled() or not to:
        return
    threading.Thread(target=_send_now, args=(to, subject, text, reply_to), daemon=True).start()


def notify_owner(subject: str, text: str, reply_to: str | None = None) -> None:
    send(owner_address(), subject, text, reply_to=reply_to)
