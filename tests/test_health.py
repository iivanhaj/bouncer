from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import make_settings


def test_healthz_returns_ok():
    with TestClient(create_app(make_settings())) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
