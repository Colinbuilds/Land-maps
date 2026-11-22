"""The app imports and serves its pages (catches missing runtime deps)."""
from fastapi.testclient import TestClient

from app.main import app


def test_pages_and_health():
    c = TestClient(app)
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/").status_code == 200
    assert c.get("/business").status_code == 200
