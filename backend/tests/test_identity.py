"""Response mapping by verified session: responses.user_id comes only from the validated token."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from conftest import ENTRA_ENV, EntraKit, add_days, auth, email_token, insert_historic_response, insert_question, today
from fastapi.testclient import TestClient

from qa_api.app import create_app
from qa_api.config import Settings
from qa_api.db import create_pool

T = "Which planet is known as the red planet?"
OPTS = ("Venus", "Mars", "Jupiter", "Saturn")


def rows(db: Any) -> list[tuple[Any, ...]]:
    return db.execute("SELECT question_id, user_id, user_name, question, correct_answer, response_answer, is_correct "
                      "FROM responses ORDER BY response_id").fetchall()


def question(db: Any, created_at: str | None = None, answer: str = "Mars", text: str = T) -> int:
    row = db.execute(
        "INSERT INTO questions (question_date, question, created_at) VALUES (%s, jsonb_build_object('question', %s::text, 'options', "
        "jsonb_build_object('A', %s::text, 'B', %s::text, 'C', %s::text, 'D', %s::text), 'answer', %s::text), COALESCE(%s::timestamptz, now())) "
        "RETURNING question_id",
        [today(db), text, *OPTS, answer, created_at],
    ).fetchone()
    return int(row[0])


def submit(client: TestClient, qid: Any, token: str, answer: str, headers: dict[str, str] | None = None, **extra: Any) -> Any:
    return client.post(f"/api/v1/questions/{qid}/responses", headers={**auth(token), **(headers or {})}, json={"response_answer": answer, **extra})


def test_two_emails_answer_the_same_question_independently(client: TestClient, clean: Any) -> None:
    qid = question(clean)
    a, b = email_token(client, "ann.lee@example.test"), email_token(client, "ben.roy+daily@example.test")
    assert submit(client, qid, a, "Mars").status_code == 201
    assert submit(client, qid, b, "Venus").status_code == 201
    assert rows(clean) == [
        (qid, "ann.lee@example.test", "ann.lee@example.test", T, "Mars", "Mars", True),
        (qid, "ben.roy+daily@example.test", "ben.roy+daily@example.test", T, "Mars", "Venus", False),
    ]
    da = client.get("/api/v1/questions/today", headers=auth(a)).json()["data"]["my_response"]
    db_ = client.get("/api/v1/questions/today", headers=auth(b)).json()["data"]["my_response"]
    assert (da["response_answer"], db_["response_answer"]) == ("Mars", "Venus")
    assert [r["response_answer"] for r in client.get("/api/v1/me/responses", headers=auth(a)).json()["data"]] == ["Mars"]
    c = email_token(client, "cara.d@example.test")
    assert client.get("/api/v1/questions/today", headers=auth(c)).json()["data"]["my_response"] is None
    assert client.get(f"/api/v1/questions/{qid}/my-response", headers=auth(c)).status_code == 404


@pytest.mark.parametrize("variant", ["Ann.Lee@Example.TEST", "  ann.lee@example.test  ", "ANN.LEE@EXAMPLE.TEST\t"])
def test_same_email_variants_are_one_participant(client: TestClient, clean: Any, variant: str) -> None:
    qid = question(clean)
    assert submit(client, qid, email_token(client, "ann.lee@example.test"), "Mars").status_code == 201
    again = email_token(client, variant)
    state = client.get("/api/v1/questions/today", headers=auth(again)).json()["data"]
    assert state["answered"] is True and state["my_response"]["response_answer"] == "Mars"
    dup = submit(client, qid, again, "Venus")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "ALREADY_ANSWERED"
    assert len(rows(clean)) == 1


def test_dots_and_plus_tags_are_distinct_addresses(client: TestClient, clean: Any) -> None:
    qid = question(clean)
    for e in ("annlee@example.test", "ann.lee@example.test", "ann.lee+x@example.test"):
        assert submit(client, qid, email_token(client, e), "Mars").status_code == 201
    assert [r[1] for r in rows(clean)] == ["annlee@example.test", "ann.lee@example.test", "ann.lee+x@example.test"]


def test_headers_and_body_cannot_override_the_session(client: TestClient, clean: Any) -> None:
    qid = question(clean)
    ann = email_token(client, "ann.lee@example.test")
    assert submit(client, qid, email_token(client, "ben.roy@example.test"), "Venus").status_code == 201
    forged = submit(client, qid, ann, "Mars", {"x-user-email": "ben.roy@example.test", "x-user-name": "Ben Roy"})
    assert forged.status_code == 400 and forged.json()["error"]["fields"] == [
        {"field": "x-user-email", "message": "not accepted"}, {"field": "x-user-name", "message": "not accepted"}]
    for extra in ({"user_id": "ben.roy@example.test"}, {"user_name": "Ben Roy"}, {"email": "other@example.test"}, {"is_correct": True}):
        assert submit(client, qid, ann, "Mars", **extra).status_code == 400
    assert submit(client, qid, ann, "Mars").status_code == 201
    assert [r[1:3] for r in rows(clean)] == [("ben.roy@example.test", "ben.roy@example.test"), ("ann.lee@example.test", "ann.lee@example.test")]


def test_concurrent_duplicates_store_one_per_email(client: TestClient, clean: Any) -> None:
    qid = question(clean)
    variants = [email_token(client, v) for v in ("ann.lee@example.test", "Ann.Lee@example.test", " ann.lee@EXAMPLE.test")]
    other = email_token(client, "ben.roy@example.test")
    jobs = [(variants[i % 3], OPTS[i % 4]) for i in range(9)] + [(other, "Mars")] * 3
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda j: submit(client, qid, j[0], j[1]).status_code, jobs))
    assert sorted(codes) == [201, 201] + [409] * 10
    assert sorted(r[1] for r in rows(clean)) == ["ann.lee@example.test", "ben.roy@example.test"]


def test_explicit_question_id_required_and_exact_version_scored(client: TestClient, clean: Any) -> None:
    t = today(clean)
    v1 = question(clean, f"{t}T08:00:00+05:30", "Mars", "Version 1")
    v2 = question(clean, f"{t}T09:00:00+05:30", "Venus", "Version 2")
    tok = email_token(client, "ann.lee@example.test")
    for ref in ("today", "latest", "", " ", "null", "undefined", "{{question_id}}"):
        res = client.post(f"/api/v1/questions/{ref}/responses", headers=auth(tok), json={"response_answer": "Mars"})
        assert res.status_code in (400, 404) and res.json()["error"]["code"] in ("VALIDATION_ERROR", "NOT_FOUND"), ref
    assert rows(clean) == []
    old = submit(client, v1, tok, "Mars").json()["data"]
    assert (old["question_id"], old["question"], old["correct_answer"], old["is_correct"]) == (v1, "Version 1", "Mars", True)
    assert client.get("/api/v1/questions/today", headers=auth(tok)).json()["data"]["question_id"] == v2


def test_historical_shared_identity_rows_unchanged(client: TestClient, clean: Any) -> None:
    """Rows recorded under the former shared identity are never reassigned to a newly entered email."""
    t = today(clean)
    past = insert_question(clean, add_days(t, -1))
    insert_historic_response(clean, past, "open.access@alrium.com", "New Delhi", True, f"{add_days(t, -1)}T17:09:37+05:30")
    before = clean.execute("SELECT r::text FROM responses r ORDER BY response_id").fetchall()
    qid = question(clean)
    tok = email_token(client, "ann.lee@example.test")
    assert submit(client, qid, tok, "Mars").status_code == 201
    assert client.get("/api/v1/me/responses", headers=auth(tok)).json()["meta"]["total"] == 1
    assert clean.execute("SELECT r::text FROM responses r WHERE user_id = 'open.access@alrium.com'").fetchall() == before
    shared = email_token(client, "open.access@alrium.com")
    assert client.get("/api/v1/me", headers=auth(shared)).json()["data"]["is_admin"] is False
    assert clean.execute("SELECT count(*) FROM responses WHERE user_id = 'open.access@alrium.com'").fetchone()[0] == 1


def test_entra_two_users_map_by_verified_claims(db_url: str, clean: Any) -> None:
    kit = EntraKit()
    app = create_app(Settings(DATABASE_URL=db_url, **ENTRA_ENV), pool=create_pool(db_url, 4), entra_keys=kit.resolver())  # type: ignore[arg-type]
    qid = question(clean)
    one = kit.sign({"email": "Emp.One@Contoso.example", "name": "Employee One", "oid": "11111111-0000-0000-0000-000000000001"})
    two = kit.sign({"email": "emp.two@contoso.example", "name": "Employee Two", "oid": "11111111-0000-0000-0000-000000000002"})
    with TestClient(app) as c:
        assert submit(c, qid, one, "Mars").status_code == 201
        assert submit(c, qid, two, "Saturn").status_code == 201
        assert submit(c, qid, one, "Venus").status_code == 409
        assert submit(c, qid, two, "Mars", {"x-user-email": "emp.one@contoso.example"}).status_code == 400
        assert c.get("/api/v1/questions/today", headers=auth(two)).json()["data"]["my_response"]["response_answer"] == "Saturn"
    assert [r[1:3] + r[5:] for r in rows(clean)] == [
        ("emp.one@contoso.example", "Employee One", "Mars", True), ("emp.two@contoso.example", "Employee Two", "Saturn", False)]
