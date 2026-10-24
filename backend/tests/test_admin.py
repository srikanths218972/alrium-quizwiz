import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from conftest import CAPITAL, DELHI_TS, add_days, auth, insert_historic_response, insert_question, payload, today


def qcount(db: Any) -> int:
    return int(db.execute("SELECT count(*) FROM questions").fetchone()[0])


def body(d: str, q: Any = None) -> dict[str, Any]:
    return {"question_date": d, "question": payload() if q is None else q}


@pytest.mark.parametrize("method,url", [
    ("GET", "/api/v1/admin/questions"), ("POST", "/api/v1/admin/questions"), ("GET", "/api/v1/admin/questions/1"),
    ("GET", "/api/v1/admin/responses"), ("GET", "/api/v1/admin/questions/1/stats"), ("GET", "/api/v1/admin/users"),
    ("GET", "/api/v1/admin/schedule/gaps?from=2026-10-01&to=2026-10-02"),
])
def test_admin_routes_403_for_employees_401_anonymous(client: Any, tokens: dict[str, str], clean: Any, method: str, url: str) -> None:
    kw = {"json": body(today(clean))} if method == "POST" else {}
    res = client.request(method, url, headers=auth(tokens["employee-unanswered"]), **kw)
    assert res.status_code == 403 and res.json()["error"]["code"] == "FORBIDDEN" and "New Delhi" not in res.text
    anon = client.request(method, url, **({"json": {"bad": True}} if method == "POST" else {}))
    assert anon.status_code == 401 and not re.search("question_date|VALIDATION", anon.text)
    assert qcount(clean) == 0


class TestCreate:
    def test_creates_with_full_payload(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        t = today(clean)
        res = client.post("/api/v1/admin/questions", headers=auth(tokens["admin"]), json=body(t))
        assert res.status_code == 201
        q = res.json()["data"]
        assert re.match(DELHI_TS, q["created_at"]) and isinstance(q["question_id"], int)
        assert {k: q[k] for k in ("question_date", "question", "response_count", "is_latest_for_date")} == {
            "question_date": t, "question": {"question": "What is the capital of India?", "options": CAPITAL, "answer": "New Delhi"},
            "response_count": 0, "is_latest_for_date": True}
        assert client.get(f"/api/v1/admin/questions/{q['question_id']}", headers=auth(tokens["admin"])).json()["data"] == q
        stored = clean.execute("SELECT question, pg_typeof(question)::text FROM questions WHERE question_id = %s", [q["question_id"]]).fetchone()
        assert stored == (q["question"], "jsonb")

    def test_same_date_inserts_new_row_and_newest_is_served(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        t, h = today(clean), auth(tokens["admin"])
        first = client.post("/api/v1/admin/questions", headers=h, json=body(t)).json()["data"]
        second = client.post("/api/v1/admin/questions", headers=h, json=body(t, payload("Chennai", "Corrected question")))
        assert second.status_code == 201 and second.json()["data"]["question_id"] > first["question_id"]
        rows = client.get(f"/api/v1/admin/questions?from={t}&to={t}", headers=h).json()["data"]
        assert [(r["question_id"], r["is_latest_for_date"]) for r in rows] == [(first["question_id"], False), (second.json()["data"]["question_id"], True)]
        served = client.get("/api/v1/questions/today", headers=auth(tokens["employee-unanswered"])).json()["data"]
        assert served["question_id"] == second.json()["data"]["question_id"]
        assert client.get(f"/api/v1/admin/questions/{first['question_id']}", headers=h).json()["data"]["question"] == first["question"]

    def test_trims_values(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        q = {"question": "  Padded?  ", "options": {"A": " a ", "B": "b", "C": "c ", "D": " d"}, "answer": " a "}
        res = client.post("/api/v1/admin/questions", headers=auth(tokens["admin"]), json=body(today(clean), q))
        assert res.json()["data"]["question"] == {"question": "Padded?", "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "answer": "a"}

    def test_single_letter_answer_allowed_when_it_is_an_option_value(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        letters = {"question": "Which letter comes second?", "options": {"A": "A", "B": "B", "C": "C", "D": "D"}, "answer": "B"}
        res = client.post("/api/v1/admin/questions", headers=auth(tokens["admin"]), json=body(today(clean), letters))
        assert res.status_code == 201 and res.json()["data"]["question"]["answer"] == "B"
        sub = client.post(f"/api/v1/questions/{res.json()['data']['question_id']}/responses",
                          headers=auth(tokens["employee-unanswered"]), json={"response_answer": "B"})
        d = sub.json()["data"]
        assert (d["correct_answer"], d["response_answer"], d["is_correct"]) == ("B", "B", True)

    good = payload()

    @pytest.mark.parametrize("label,q,field", [
        ("blank question", {**good, "question": "   "}, "question.question"),
        ("question too long", {**good, "question": "q" * 501}, "question.question"),
        ("number as question", {**good, "question": 42}, "question.question"),
        ("missing options", {"question": "q", "answer": "Mumbai"}, "question.options"),
        ("options as array", {**good, "options": ["a", "b", "c", "d"]}, "question.options"),
        ("missing option D", {**good, "options": {"A": "a", "B": "b", "C": "c"}}, "question.options.D"),
        ("extra option E", {**good, "options": {**CAPITAL, "E": "e"}}, "question.options.E"),
        ("empty option", {**good, "options": {**CAPITAL, "C": ""}}, "question.options.C"),
        ("blank option", {**good, "options": {**CAPITAL, "C": "  "}}, "question.options.C"),
        ("option too long", {**good, "options": {**CAPITAL, "A": "a" * 201}}, "question.options.A"),
        ("numeric option", {**good, "options": {**CAPITAL, "A": 1}}, "question.options.A"),
        ("duplicate options", {**good, "options": {**CAPITAL, "D": "Mumbai"}}, "question.options"),
        ("duplicate after trim", {**good, "options": {**CAPITAL, "D": " Mumbai "}}, "question.options"),
        ("answer as option label B", {**good, "answer": "B"}, "question.answer"),
        ("answer not an option", {**good, "answer": "Bangalore"}, "question.answer"),
        ("answer wrong case", {**good, "answer": "new delhi"}, "question.answer"),
        ("blank answer", {**good, "answer": "  "}, "question.answer"),
        ("numeric answer", {**good, "answer": 2}, "question.answer"),
        ("missing answer", {"question": "q", "options": CAPITAL}, "question.answer"),
        ("unknown payload field", {**good, "hint": "x"}, "question.hint"),
        ("payload as string", "{}", "question"),
    ])
    def test_rejects_invalid_payload(self, client: Any, tokens: dict[str, str], clean: Any, label: str, q: Any, field: str) -> None:
        res = client.post("/api/v1/admin/questions", headers=auth(tokens["admin"]), json=body(today(clean), q))
        assert res.status_code == 400, label
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
        assert field in [f["field"] for f in res.json()["error"]["fields"]], res.json()
        assert not re.search("constraint|SQL|Traceback", res.text, re.I)
        assert qcount(clean) == 0

    @pytest.mark.parametrize("label,b,field", [
        ("impossible date", {"question_date": "2026-02-30", "question": good}, "question_date"),
        ("date format", {"question_date": "23-10-2026", "question": good}, "question_date"),
        ("unknown field", {"question_date": "2026-10-23", "question": good, "question_id": 5}, "question_id"),
        ("client created_at", {"question_date": "2026-10-23", "question": good, "created_at": "2026-01-01"}, "created_at"),
        ("old flat format", {"question_date": "2026-10-23", "question": "q", "option_a": "a"}, "option_a"),
        ("missing question", {"question_date": "2026-10-23"}, "question"),
    ])
    def test_rejects_invalid_body(self, client: Any, tokens: dict[str, str], clean: Any, label: str, b: Any, field: str) -> None:
        res = client.post("/api/v1/admin/questions", headers=auth(tokens["admin"]), json=b)
        assert res.status_code == 400, label
        assert field in [f["field"] for f in res.json()["error"]["fields"]]
        assert qcount(clean) == 0


class TestInsertOnly:
    @pytest.mark.parametrize("method", ["PATCH", "PUT", "DELETE"])
    def test_edit_and_delete_are_405(self, client: Any, tokens: dict[str, str], clean: Any, method: str) -> None:
        qid = insert_question(clean, add_days(today(clean), 3))
        h = auth(tokens["admin"])
        before = client.get(f"/api/v1/admin/questions/{qid}", headers=h).json()["data"]
        res = client.request(method, f"/api/v1/admin/questions/{qid}", headers=h, **({} if method == "DELETE" else {"json": {"question": payload("Chennai")}}))
        assert res.status_code == 405 and res.json()["error"]["code"] == "QUESTIONS_INSERT_ONLY"
        assert client.get(f"/api/v1/admin/questions/{qid}", headers=h).json()["data"] == before

    def test_employee_and_anonymous_also_405(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        qid = insert_question(clean, today(clean))
        assert client.delete(f"/api/v1/admin/questions/{qid}", headers=auth(tokens["employee-wrong"])).status_code == 405
        assert client.patch(f"/api/v1/admin/questions/{qid}", json={}).status_code == 405
        assert qcount(clean) == 1

    def test_database_refuses_update_and_delete(self, clean: Any) -> None:
        import psycopg

        qid = insert_question(clean, add_days(today(clean), 3))
        for stmt in ("UPDATE questions SET question_date = question_date + 1 WHERE question_id = %s", "DELETE FROM questions WHERE question_id = %s"):
            with pytest.raises(psycopg.Error) as err:
                clean.execute(stmt, [qid])
            assert err.value.sqlstate == "23001"

    def test_openapi_lists_no_edit_operations(self, client: Any) -> None:
        doc = client.get("/openapi.json").json()
        methods = {m for p in doc["paths"].values() for m in p}
        assert not methods & {"patch", "put", "delete"}
        assert doc["components"]["schemas"]["QuestionPayloadIn"]["properties"]["answer"]["type"] == "string"


class TestListing:
    def test_lists_all_versions_unfiltered_and_filtered(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        t, h = today(clean), auth(tokens["admin"])
        for i in range(25):
            insert_question(clean, add_days(t, i), "Mumbai", f"Q{i}")
        insert_question(clean, add_days(t, 5), "New Delhi", "Q5 v2")
        first = client.get("/api/v1/admin/questions", headers=h).json()
        assert first["meta"] == {"page": 1, "page_size": 20, "total": 26}
        assert first["data"][0]["question_date"] == t and first["data"][0]["question"]["answer"] == "Mumbai"
        assert len(client.get("/api/v1/admin/questions?page=2", headers=h).json()["data"]) == 6
        ranged = client.get(f"/api/v1/admin/questions?from={add_days(t, 5)}&to={add_days(t, 7)}", headers=h).json()["data"]
        assert [r["question"]["question"] for r in ranged] == ["Q5", "Q5 v2", "Q6", "Q7"]
        assert [r["is_latest_for_date"] for r in ranged] == [False, True, True, True]
        assert len(client.get("/api/v1/admin/questions?page_size=100", headers=h).json()["data"]) == 26
        assert client.get("/api/v1/admin/questions?page_size=101", headers=h).status_code == 400
        assert client.get(f"/api/v1/admin/questions?from={add_days(t, 2)}&to={t}", headers=h).status_code == 400

    def test_audit_and_stats_use_text(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        t, h = today(clean), auth(tokens["admin"])
        qid = insert_question(clean, add_days(t, -1), "Chennai")
        other = insert_question(clean, add_days(t, -2), "Mumbai")
        day = add_days(t, -1)
        for i, (u, a, ok) in enumerate([("a", "Chennai", True), ("b", "Chennai", True), ("c", "Mumbai", False), ("d", "Kolkata", False)]):
            insert_historic_response(clean, qid, f"{u}@alrium.com", a, ok, f"{day}T09:0{i}:00+05:30")
        insert_historic_response(clean, other, "a@alrium.com", "Mumbai", True, f"{add_days(t, -2)}T09:00:00+05:30")
        all_ = client.get("/api/v1/admin/responses", headers=h).json()
        assert all_["meta"]["total"] == 5
        top = all_["data"][0]
        assert {k: top[k] for k in ("user_id", "responded_at", "question_date", "question", "correct_answer", "response_answer", "is_correct")} == {
            "user_id": "d@alrium.com", "responded_at": f"{day}T09:03:00+05:30", "question_date": day,
            "question": "What is the capital of India?", "correct_answer": "Chennai", "response_answer": "Kolkata", "is_correct": False}
        by_q = client.get(f"/api/v1/admin/responses?question_id={qid}&is_correct=false", headers=h).json()["data"]
        assert [r["user_id"] for r in by_q] == ["d@alrium.com", "c@alrium.com"]
        assert client.get("/api/v1/admin/responses?user_id=%20A@Alrium.com%20", headers=h).json()["meta"]["total"] == 2
        assert client.get(f"/api/v1/admin/responses?from={add_days(t, -2)}&to={add_days(t, -2)}", headers=h).json()["meta"]["total"] == 1
        assert client.get("/api/v1/admin/responses?is_correct=yes", headers=h).status_code == 400
        stats = client.get(f"/api/v1/admin/questions/{qid}/stats", headers=h).json()["data"]
        assert stats == {"question_id": qid, "question_date": day, "correct_answer": "Chennai", "total": 4, "correct": 2, "incorrect": 2,
                         "distribution": {"A": 1, "B": 0, "C": 2, "D": 1}, "unmatched": 0, "accuracy": 0.5}

    def test_stats_zero_safe(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        h = auth(tokens["admin"])
        qid = insert_question(clean, add_days(today(clean), 1))
        s = client.get(f"/api/v1/admin/questions/{qid}/stats", headers=h).json()["data"]
        assert (s["total"], s["distribution"], s["unmatched"], s["accuracy"]) == (0, {"A": 0, "B": 0, "C": 0, "D": 0}, 0, None)
        assert client.get("/api/v1/admin/questions/2147480000/stats", headers=h).status_code == 404

    def test_gaps(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        t, h = today(clean), auth(tokens["admin"])
        insert_question(clean, t)
        insert_question(clean, t, "Chennai", "Second version")
        insert_question(clean, add_days(t, 2))
        res = client.get(f"/api/v1/admin/schedule/gaps?from={t}&to={add_days(t, 3)}", headers=h).json()
        assert res["data"] == {"from": t, "to": add_days(t, 3), "missing_dates": [add_days(t, 1), add_days(t, 3)]}
        assert client.get("/api/v1/admin/schedule/gaps", headers=h).status_code == 400
        assert client.get(f"/api/v1/admin/schedule/gaps?from={t}", headers=h).status_code == 400
        assert client.get(f"/api/v1/admin/schedule/gaps?from={t}&to={add_days(t, 367)}", headers=h).status_code == 400
        assert client.get(f"/api/v1/admin/schedule/gaps?from={t}&to={add_days(t, 366)}", headers=h).status_code == 200


def test_concurrent_replacement_never_changes_scoring_of_named_id(client: Any, tokens: dict[str, str], db: Any) -> None:
    from conftest import reset

    for _ in range(5):
        reset(db)
        t = today(db)
        qid = insert_question(db, t)
        with ThreadPoolExecutor(2) as ex:
            created = ex.submit(lambda t=t: client.post("/api/v1/admin/questions", headers=auth(tokens["admin"]), json=body(t, payload("Chennai", "Replacement"))))
            sub = ex.submit(lambda qid=qid: client.post(f"/api/v1/questions/{qid}/responses", headers=auth(tokens["employee-unanswered"]), json={"response_answer": "New Delhi"}))
        assert created.result().status_code == 201
        d = sub.result().json()["data"]
        assert (d["question_id"], d["correct_answer"], d["is_correct"]) == (qid, "New Delhi", True)


def test_list_users_from_responses(client: Any, tokens: dict[str, str], clean: Any) -> None:
    from conftest import email_token

    h = auth(tokens["admin"])
    assert client.get("/api/v1/admin/users", headers=h).json() == {"data": [], "meta": {"page": 1, "page_size": 20, "total": 0}}
    t = today(clean)
    past = insert_question(clean, add_days(t, -1))
    insert_historic_response(clean, past, "b.user@example.test", "New Delhi", True, f"{add_days(t, -1)}T09:00:00+05:30")
    qid = insert_question(clean, t)
    for email, answer in (("b.user@example.test", "Mumbai"), ("A.User@Example.test", "New Delhi")):
        tok = email_token(client, email)
        assert client.post(f"/api/v1/questions/{qid}/responses", headers=auth(tok), json={"response_answer": answer}).status_code == 201
    res = client.get("/api/v1/admin/users", headers=h).json()
    assert res["meta"]["total"] == 2
    a, b = res["data"]
    assert (a["user_id"], a["user_name"], a["response_count"], a["correct_count"]) == ("a.user@example.test", "a.user@example.test", 1, 1)
    assert (b["user_id"], b["response_count"], b["correct_count"]) == ("b.user@example.test", 2, 1)
    assert b["first_response_at"] == f"{add_days(t, -1)}T09:00:00+05:30" and re.match(DELHI_TS, b["last_response_at"])
    assert b["user_name"] == "b.user@example.test"
    page2 = client.get("/api/v1/admin/users?page=2&page_size=1", headers=h).json()
    assert [u["user_id"] for u in page2["data"]] == ["b.user@example.test"]
    assert client.get("/api/v1/admin/users?page_size=101", headers=h).status_code == 400
    assert client.get("/api/v1/admin/users?unknown=1", headers=h).status_code == 400
    assert client.get("/api/v1/admin/users", headers=auth(tokens["employee-correct"])).status_code == 403


def test_answers_by_date_maps_users_and_lists_who_did_not_answer(client: Any, tokens: dict[str, str], clean: Any) -> None:
    t = today(clean)
    qid = insert_question(clean, t)
    with clean.transaction():
        for email in ("first.last@example.test", "second.person@example.test", "third.person+quiz@example.test"):
            clean.execute("INSERT INTO users (user_id, user_name) VALUES (%s, %s)", [email, email])
    for who, answer in (("employee-correct", "New Delhi"), ("employee-wrong", "Mumbai")):
        assert client.post(f"/api/v1/questions/{qid}/responses", headers=auth(tokens[who]), json={"response_answer": answer}).status_code == 201
    res = client.get(f"/api/v1/admin/answers/by-date?date={t}", headers=auth(tokens["admin"]))
    assert res.status_code == 200
    d = res.json()["data"]
    assert (d["question_date"], d["question_ids"]) == (t, [qid])
    assert [(a["user_id"], a["response_answer"], a["correct_answer"], a["is_correct"]) for a in d["answered"]] == [
        ("first.last@example.test", "New Delhi", "New Delhi", True), ("second.person@example.test", "Mumbai", "New Delhi", False)]
    assert d["not_answered"] == [{"user_id": "third.person+quiz@example.test", "user_name": "third.person+quiz@example.test"}]
    past = client.get(f"/api/v1/admin/answers/by-date?date={add_days(t, -1)}", headers=auth(tokens["admin"])).json()["data"]
    assert past == {"question_date": add_days(t, -1), "question_ids": [], "answered": [], "not_answered": []}
    assert client.get("/api/v1/admin/answers/by-date", headers=auth(tokens["admin"])).status_code == 400
    assert client.get("/api/v1/admin/answers/by-date?date=2026-02-30", headers=auth(tokens["admin"])).status_code == 400
    assert client.get(f"/api/v1/admin/answers/by-date?date={t}", headers=auth(tokens["employee-unanswered"])).status_code == 403
