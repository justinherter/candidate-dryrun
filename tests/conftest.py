import pytest
from fastapi.testclient import TestClient

import app.main
import app.service
from app.lender import LenderClient
from app.main import api
from app.store import ApplicationStore


@pytest.fixture(autouse=True)
def backoff_delays(monkeypatch) -> list[float]:
    """Make retry backoff instant, and record the delays it asked for.

    Keeps the suite fast while still letting a test assert that we really do back
    off, and by how much.
    """
    delays: list[float] = []

    def submit_without_waiting(app_id, lender, store):
        return app.service.submit_application(app_id, lender, store, sleep=delays.append)

    monkeypatch.setattr(app.main, "submit_application", submit_without_waiting)
    return delays


@pytest.fixture
def lender() -> LenderClient:
    return LenderClient()


@pytest.fixture
def client(lender):
    api.state.store = ApplicationStore()
    api.state.lender = lender
    return TestClient(api)


@pytest.fixture
def new_application(client):
    def _create(**overrides) -> dict:
        body = {"business_name": "Blue Door Bakery", "requested_amount": 50_000, "lender": "acme-capital"}
        body.update(overrides)
        resp = client.post("/applications", json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _create
