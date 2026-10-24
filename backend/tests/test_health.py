from typing import Any

import pytest
from conftest import ADMIN, auth, settings
from fastapi.testclient import TestClient

from qa_api.app import create_app
from qa_api.auth import issue_email_token
from qa_api.db import create_pool, to_delhi_iso

DEAD = "postgresql://nobody:secret-password@127.0.0.1:1/none"


def test_live_and_ready(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json() == {"status": "ready", "migration": "0004"}


@pytest.fixture(scope="module")
def dead() -> Any:
    with TestClient(create_app(settings(DEAD), pool=create_pool(DEAD, 1))) as c:
        yield c


def test_ready_503_live_200_when_database_down(dead: TestClient) -> None:
    assert dead.get("/health/live").status_code == 200
    ready = dead.get("/health/ready")
    assert ready.status_code == 503 and ready.json() == {"status": "unavailable"}
    assert "secret-password" not in ready.text and "127.0.0.1" not in ready.text


def test_business_routes_503_without_details(dead: TestClient) -> None:
    token = issue_email_token(settings("postgresql://x@127.0.0.1:1/x"), "first.last@example.test")[0]
    res = dead.get("/api/v1/questions/today", headers=auth(token))
    assert res.status_code == 503 and res.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "secret-password" not in res.text and "127.0.0.1" not in res.text
    sub = dead.post("/api/v1/questions/1/responses", headers=auth(token), json={"response_answer": "x"})
    assert sub.status_code == 503
    assert dead.get("/api/v1/admin/questions", headers=auth(ADMIN)).status_code == 503


def test_unknown_route_json_404(client: TestClient) -> None:
    res = client.get("/api/v1/sql")
    assert res.status_code == 404 and res.json()["error"]["code"] == "NOT_FOUND"


def test_switching_database_url_switches_database(db_url: str) -> None:
    """The same code serves whichever database DATABASE_URL names (Docker or local PostgreSQL)."""
    with TestClient(create_app(settings(db_url), pool=create_pool(db_url, 1))) as ok, \
         TestClient(create_app(settings(DEAD), pool=create_pool(DEAD, 1))) as down:
        assert ok.get("/health/ready").status_code == 200
        assert down.get("/health/ready").status_code == 503


@pytest.mark.parametrize("utc,delhi", [
    ("2026-10-23T07:50:01.999+00:00", "2026-10-23T13:20:01+05:30"),
    ("2026-10-23T18:29:59+00:00", "2026-10-23T23:59:59+05:30"),
    ("2026-10-23T18:30:00+00:00", "2026-10-24T00:00:00+05:30"),
    ("2026-12-31T20:00:00+00:00", "2027-01-01T01:30:00+05:30"),
])
def test_delhi_serialization(utc: str, delhi: str) -> None:
    from datetime import datetime

    assert to_delhi_iso(datetime.fromisoformat(utc)) == delhi
