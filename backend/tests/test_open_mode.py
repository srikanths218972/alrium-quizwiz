"""AUTH_MODE=open: testing only. No sign-in or admin credential; the tester's email is in the Post response body."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from conftest import ENTRA_ENV, add_days, insert_historic_response, insert_question, today
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qa_api.app import create_app
from qa_api.config import Settings
from qa_api.db import create_pool


@pytest.fixture(scope="module")
def open_client(db_url: str) -> Iterator[TestClient]:
    app = create_app(Settings(DATABASE_URL=db_url, AUTH_MODE="open", APP_ENV="local"), pool=create_pool(db_url, 6))  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c


def answer(c: TestClient, qid: Any, email: Any, text: str, **extra: Any) -> Any:
    return c.post(f"/api/v1/questions/{qid}/responses", json={"email": email, "response_answer": text, **extra})


def test_only_explicit_and_never_in_production() -> None:
    assert Settings(DATABASE_URL="postgresql://x", AUTH_MODE="open", APP_ENV="local").mode == "open"  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="refused when APP_ENV=production"):
        Settings(DATABASE_URL="postgresql://x", AUTH_MODE="open", APP_ENV="production")  # type: ignore[arg-type]
    assert Settings(DATABASE_URL="postgresql://x", EMAIL_TOKEN_SECRET="s" * 40).mode == "email"  # type: ignore[arg-type]
    assert Settings(DATABASE_URL="postgresql://x", **{k: v for k, v in ENTRA_ENV.items() if k != "AUTH_MODE"}).mode == "entra"  # type: ignore[arg-type]


def test_admin_routes_need_no_credential(open_client: TestClient, clean: Any) -> None:
    t = today(clean)
    q = {"question": "Which planet is known as the Red Planet?", "options": {"A": "Venus", "B": "Mars", "C": "Jupiter", "D": "Saturn"}, "answer": "Mars"}
    created = open_client.post("/api/v1/admin/questions", json={"question_date": t, "question": q})
    assert created.status_code == 201
    assert open_client.get("/api/v1/admin/questions").json()["meta"]["total"] == 1
    assert open_client.get("/api/v1/admin/users").status_code == 200
    assert open_client.get("/api/v1/auth/mode").json()["data"]["mode"] == "open"
    day = open_client.get("/api/v1/questions/today").json()["data"]
    assert day["question_id"] == created.json()["data"]["question_id"] and "answer" not in day["question"] and day["answered"] is False


def test_email_in_body_maps_each_tester(open_client: TestClient, clean: Any) -> None:
    qid = insert_question(clean, today(clean))
    a = answer(open_client, qid, "  First.Last@Alrium.com ", "New Delhi")
    assert a.status_code == 201 and (a.json()["data"]["is_correct"], a.json()["data"]["response_answer"]) == (True, "New Delhi")
    assert answer(open_client, qid, "second.person@alrium.com", "Mumbai").json()["data"]["is_correct"] is False
    dup = answer(open_client, qid, "FIRST.LAST@alrium.com", "Chennai")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "ALREADY_ANSWERED"
    rows = clean.execute("SELECT question_id, user_id, user_name, response_answer, is_correct FROM responses ORDER BY response_id").fetchall()
    assert rows == [(qid, "first.last@alrium.com", "first.last@alrium.com", "New Delhi", True),
                    (qid, "second.person@alrium.com", "second.person@alrium.com", "Mumbai", False)]
    users = open_client.get("/api/v1/admin/users").json()["data"]
    assert [u["user_id"] for u in users] == ["first.last@alrium.com", "second.person@alrium.com"]


@pytest.mark.parametrize("body,field", [
    ({"response_answer": "Mumbai"}, "email"),
    ({"email": "", "response_answer": "Mumbai"}, "email"),
    ({"email": "not-an-email", "response_answer": "Mumbai"}, "email"),
    ({"email": "a@alrium.com"}, "response_answer"),
    ({"email": "a@alrium.com", "response_answer": "Mumbai", "user_name": "X"}, "user_name"),
    ({"email": "a@alrium.com", "response_answer": "Mumbai", "is_correct": True}, "is_correct"),
])
def test_bad_bodies_rejected(open_client: TestClient, clean: Any, body: dict[str, Any], field: str) -> None:
    qid = insert_question(clean, today(clean))
    res = open_client.post(f"/api/v1/questions/{qid}/responses", json=body)
    assert res.status_code == 400 and field in [f["field"] for f in res.json()["error"]["fields"]]
    assert clean.execute("SELECT count(*) FROM responses").fetchone()[0] == 0


def test_rules_still_hold(open_client: TestClient, clean: Any) -> None:
    t = today(clean)
    past = insert_question(clean, add_days(t, -1))
    assert answer(open_client, past, "a@alrium.com", "New Delhi").json()["error"]["code"] == "QUESTION_NOT_OPEN"
    assert open_client.post("/api/v1/questions/today/responses", json={"email": "a@alrium.com", "response_answer": "New Delhi"}).status_code == 400
    qid = insert_question(clean, t)
    assert answer(open_client, qid, "a@alrium.com", "B").status_code == 400
    with ThreadPoolExecutor(6) as ex:
        codes = sorted(ex.map(lambda e: answer(open_client, qid, e, "Mumbai").status_code, ["b@alrium.com", "B@alrium.com", " b@alrium.com"] * 2))
    assert codes == [201, 409, 409, 409, 409, 409]
    assert open_client.post("/api/v1/questions/1/responses", headers={"x-user-email": "z@alrium.com"},
                            json={"email": "a@alrium.com", "response_answer": "x"}).status_code == 400


def test_no_personal_routes_or_login(open_client: TestClient) -> None:
    for path in ("/api/v1/me", "/api/v1/me/responses", "/api/v1/questions/1/my-response"):
        assert open_client.get(path).json()["error"]["code"] == "NOT_AVAILABLE_IN_OPEN_MODE"
    assert open_client.post("/api/v1/auth/email-login", json={"email": "a@alrium.com"}).status_code == 404


def test_domain_restriction_applies(db_url: str, clean: Any) -> None:
    app = create_app(Settings(DATABASE_URL=db_url, AUTH_MODE="open", APP_ENV="local", EMAIL_ALLOWED_DOMAINS="alrium.com"),  # type: ignore[arg-type]
                     pool=create_pool(db_url, 1))
    qid = insert_question(clean, today(clean))
    with TestClient(app) as c:
        assert answer(c, qid, "x@gmail.com", "Mumbai").status_code == 400
        assert answer(c, qid, "x@alrium.com", "Mumbai").status_code == 201


def test_historical_rows_untouched(open_client: TestClient, clean: Any) -> None:
    t = today(clean)
    past = insert_question(clean, add_days(t, -1))
    insert_historic_response(clean, past, "open.access@alrium.com", "New Delhi", True, f"{add_days(t, -1)}T17:09:37+05:30")
    before = clean.execute("SELECT r::text FROM responses r").fetchall()
    assert answer(open_client, insert_question(clean, t), "new.person@alrium.com", "Mumbai").status_code == 201
    assert clean.execute("SELECT r::text FROM responses r WHERE user_id = 'open.access@alrium.com'").fetchall() == before
