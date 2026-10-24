import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from conftest import (
    DELHI_TS,
    add_days,
    auth,
    insert_historic_response,
    insert_question,
    insert_raw_question,
    today,
)

T = "What is the capital of India?"


def count(db: Any) -> int:
    return int(db.execute("SELECT count(*) FROM responses").fetchone()[0])


def saved(db: Any, rid: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT question_id, user_id, user_name, question, correct_answer, response_answer, is_correct FROM responses WHERE response_id = %s", [rid]
    ).fetchone()
    keys = ["question_id", "user_id", "user_name", "question", "correct_answer", "response_answer", "is_correct"]
    return dict(zip(keys, row, strict=True))


def submit(client: Any, qid: Any, token: str, body: Any) -> Any:
    return client.post(f"/api/v1/questions/{qid}/responses", headers=auth(token), json=body)


def test_me_returns_self_declared_identity_and_business_date(client: Any, tokens: dict[str, str], db: Any) -> None:
    res = client.get("/api/v1/me", headers=auth(tokens["employee-correct"]))
    assert res.status_code == 200
    d = res.json()["data"]
    assert d | {"server_time": None} == {
        "user_id": "first.last@example.test", "user_name": "first.last@example.test", "roles": ["employee"], "is_admin": False,
        "auth_mode": "email", "email_verified": False, "timezone": "Asia/Kolkata", "business_date": today(db), "server_time": None,
    }
    assert re.match(DELHI_TS, d["server_time"])


class TestToday:
    def test_404_when_nothing_scheduled(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        insert_question(clean, add_days(today(clean), 1))
        res = client.get("/api/v1/questions/today", headers=auth(tokens["employee-unanswered"]))
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "QUESTION_NOT_SCHEDULED"
        assert "capital" not in res.text

    def test_nested_question_without_answer(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        qid = insert_question(clean, today(clean))
        res = client.get("/api/v1/questions/today", headers=auth(tokens["employee-unanswered"]))
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store"
        d = res.json()["data"]
        assert re.match(DELHI_TS, d.pop("created_at"))
        assert d == {
            "question_id": qid, "question_date": today(clean),
            "question": {"question": T, "options": {"A": "Mumbai", "B": "New Delhi", "C": "Chennai", "D": "Kolkata"}},
            "answered": False, "my_response": None,
        }
        assert not re.search(r'"answer"|correct_answer|is_correct', res.text)

    def test_newest_row_for_today_is_served(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        t = today(clean)
        insert_question(clean, t, "Mumbai", "Version 1", f"{t}T08:00:00+05:30")
        v2 = insert_question(clean, t, "Chennai", "Version 2", f"{t}T09:00:00+05:30")
        insert_question(clean, add_days(t, 1), "Mumbai", "Tomorrow", f"{t}T10:00:00+05:30")
        d = client.get("/api/v1/questions/today", headers=auth(tokens["employee-unanswered"])).json()["data"]
        assert (d["question_id"], d["question"]["question"]) == (v2, "Version 2")

    def test_equal_created_at_falls_back_to_higher_id(self, client: Any, tokens: dict[str, str], clean: Any) -> None:
        t = today(clean)
        a = insert_question(clean, t, "Mumbai", "Tie 1", f"{t}T09:00:00+05:30")
        b = insert_question(clean, t, "Mumbai", "Tie 2", f"{t}T09:00:00+05:30")
        assert b > a
        assert client.get("/api/v1/questions/today", headers=auth(tokens["employee-unanswered"])).json()["data"]["question_id"] == b


class TestSubmit:
    @pytest.fixture
    def qid(self, clean: Any) -> int:
        return insert_question(clean, today(clean))

    def test_correct_answer_as_text(self, client: Any, tokens: dict[str, str], db: Any, qid: int) -> None:
        res = submit(client, qid, tokens["employee-correct"], {"response_answer": "New Delhi"})
        assert res.status_code == 201
        d = res.json()["data"]
        assert isinstance(d["response_id"], int) and re.match(DELHI_TS, d["responded_at"])
        assert {k: d[k] for k in ("question_id", "question", "correct_answer", "response_answer", "is_correct")} == {
            "question_id": qid, "question": T, "correct_answer": "New Delhi", "response_answer": "New Delhi", "is_correct": True,
        }
        assert set(d) == {"response_id", "question_id", "question", "correct_answer", "response_answer", "is_correct", "responded_at"}
        assert saved(db, d["response_id"]) == {
            "question_id": qid, "user_id": "first.last@example.test", "user_name": "first.last@example.test", "question": T,
            "correct_answer": "New Delhi", "response_answer": "New Delhi", "is_correct": True,
        }

    def test_incorrect_answer_is_201_with_texts(self, client: Any, tokens: dict[str, str], db: Any, qid: int) -> None:
        res = submit(client, qid, tokens["employee-wrong"], {"response_answer": "Mumbai"})
        assert res.status_code == 201
        d = res.json()["data"]
        assert (d["response_answer"], d["correct_answer"], d["is_correct"]) == ("Mumbai", "New Delhi", False)
        row = saved(db, d["response_id"])
        assert (row["user_id"], row["user_name"], row["response_answer"], row["is_correct"]) == ("second.person@example.test", "second.person@example.test", "Mumbai", False)

    def test_spaces_trimmed_to_canonical_text(self, client: Any, tokens: dict[str, str], qid: int) -> None:
        res = submit(client, qid, tokens["employee-unanswered"], {"response_answer": "  New Delhi "})
        assert res.status_code == 201 and res.json()["data"]["response_answer"] == "New Delhi"

    def test_duplicate_is_409_and_row_unchanged(self, client: Any, tokens: dict[str, str], db: Any, qid: int) -> None:
        first = submit(client, qid, tokens["employee-correct"], {"response_answer": "New Delhi"}).json()["data"]
        before = saved(db, first["response_id"])
        res = submit(client, qid, tokens["employee-correct"], {"response_answer": "Chennai"})
        assert res.status_code == 409
        e = res.json()["error"]
        assert (e["code"], e["message"]) == ("ALREADY_ANSWERED", "You have already answered this question.") and e["request_id"]
        assert count(db) == 1 and saved(db, first["response_id"]) == before

    def test_concurrent_submissions_store_one(self, client: Any, tokens: dict[str, str], db: Any, qid: int) -> None:
        answers = ["Mumbai", "New Delhi", "Chennai", "Kolkata", "New Delhi"]
        with ThreadPoolExecutor(5) as ex:
            codes = sorted(ex.map(lambda a: submit(client, qid, tokens["employee-unanswered"], {"response_answer": a}).status_code, answers))
        assert codes == [201, 409, 409, 409, 409]
        assert count(db) == 1

    def test_saved_result_restored(self, client: Any, tokens: dict[str, str], qid: int) -> None:
        submit(client, qid, tokens["employee-wrong"], {"response_answer": "Kolkata"})
        d = client.get("/api/v1/questions/today", headers=auth(tokens["employee-wrong"])).json()["data"]
        assert d["answered"] is True and "answer" not in d["question"]
        assert {k: d["my_response"][k] for k in ("question", "response_answer", "correct_answer", "is_correct")} == {
            "question": T, "response_answer": "Kolkata", "correct_answer": "New Delhi", "is_correct": False,
        }
        mine = client.get(f"/api/v1/questions/{qid}/my-response", headers=auth(tokens["employee-wrong"]))
        assert mine.status_code == 200 and re.match(DELHI_TS, mine.json()["data"]["responded_at"])

    @pytest.mark.parametrize("field,body", [
        ("user_id", {"response_answer": "New Delhi", "user_id": "someone@alrium.com"}),
        ("user_name", {"response_answer": "New Delhi", "user_name": "Someone"}),
        ("question", {"response_answer": "Mumbai", "question": "Forged"}),
        ("correct_answer", {"response_answer": "Mumbai", "correct_answer": "Mumbai"}),
        ("is_correct", {"response_answer": "Mumbai", "is_correct": True}),
        ("responded_at", {"response_answer": "New Delhi", "responded_at": "2000-01-01T00:00:00+05:30"}),
        ("response_id", {"response_answer": "New Delhi", "response_id": 1}),
        ("question_id", {"response_answer": "New Delhi", "question_id": 1}),
    ])
    def test_forged_fields_rejected(self, client: Any, tokens: dict[str, str], db: Any, qid: int, field: str, body: Any) -> None:
        res = submit(client, qid, tokens["employee-unanswered"], body)
        assert res.status_code == 400 and res.json()["error"]["code"] == "VALIDATION_ERROR"
        assert {"field": field, "message": "unknown field"} in res.json()["error"]["fields"]
        assert count(db) == 0

    @pytest.mark.parametrize("answer", ["B", "new delhi", "Delhi", "Bangalore"])
    def test_values_that_are_not_options_rejected(self, client: Any, tokens: dict[str, str], db: Any, qid: int, answer: str) -> None:
        res = submit(client, qid, tokens["employee-unanswered"], {"response_answer": answer})
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
        assert res.json()["error"]["fields"][0]["field"] == "response_answer"
        assert "New Delhi" not in res.text and count(db) == 0

    @pytest.mark.parametrize("answer", ["", "   ", 1, None, ["New Delhi"], {"text": "New Delhi"}, "x" * 201])
    def test_malformed_answers_rejected(self, client: Any, tokens: dict[str, str], db: Any, qid: int, answer: Any) -> None:
        assert submit(client, qid, tokens["employee-unanswered"], {"response_answer": answer}).status_code == 400
        assert count(db) == 0

    def test_missing_field_bad_json_wrong_type_and_oversize(self, client: Any, tokens: dict[str, str], qid: int) -> None:
        h = auth(tokens["employee-unanswered"])
        assert submit(client, qid, tokens["employee-unanswered"], {}).status_code == 400
        bad = client.post(f"/api/v1/questions/{qid}/responses", headers={**h, "content-type": "application/json"}, content='{"response_answer":')
        assert bad.status_code == 400 and not re.search(r"Traceback|File \"", bad.text)
        assert client.post(f"/api/v1/questions/{qid}/responses", headers={**h, "content-type": "text/plain"}, content="x").status_code == 415
        assert submit(client, qid, tokens["employee-unanswered"], {"response_answer": "New Delhi", "pad": "x" * 20000}).status_code == 413

    @pytest.mark.parametrize("offset", [-1, 1, -30])
    def test_not_today_is_403(self, client: Any, tokens: dict[str, str], db: Any, qid: int, offset: int) -> None:
        other = insert_question(db, add_days(today(db), offset), text=f"Offset {offset}")
        res = submit(client, other, tokens["employee-unanswered"], {"response_answer": "New Delhi"})
        assert res.status_code == 403 and res.json()["error"]["code"] == "QUESTION_NOT_OPEN"
        assert count(db) == 0

    def test_unknown_and_malformed_ids(self, client: Any, tokens: dict[str, str], qid: int) -> None:
        res = submit(client, 2147480000, tokens["employee-unanswered"], {"response_answer": "New Delhi"})
        assert res.status_code == 404 and res.json()["error"]["code"] == "QUESTION_NOT_FOUND"
        for bad in ["0", "abc", "1.5", "99999999999", "-3"]:
            assert submit(client, bad, tokens["employee-unanswered"], {"response_answer": "New Delhi"}).status_code == 400

    def test_malformed_stored_payload_fails_closed(self, client: Any, tokens: dict[str, str], db: Any, qid: int) -> None:
        t = today(db)
        cases = [
            {"question": "Dup?", "options": {"A": "x", "B": "x", "C": "y", "D": "z"}, "answer": "x"},
            {"question": "No answer?", "options": {"A": "a", "B": "b", "C": "c", "D": "d"}},
            {"question": "Label?", "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "answer": "A"},
        ]
        for raw in cases:
            res = submit(client, insert_raw_question(db, t, raw), tokens["employee-unanswered"], {"response_answer": "a"})
            assert res.status_code == 409 and res.json()["error"]["code"] == "QUESTION_INVALID"
        assert count(db) == 0

    def test_my_response_404_before_answering(self, client: Any, tokens: dict[str, str], qid: int) -> None:
        res = client.get(f"/api/v1/questions/{qid}/my-response", headers=auth(tokens["employee-unanswered"]))
        assert res.status_code == 404 and res.json()["error"]["code"] == "RESPONSE_NOT_FOUND"
        assert not re.search(r"correct|New Delhi", res.text)


def test_replacement_policy_and_history(client: Any, tokens: dict[str, str], clean: Any) -> None:
    db = clean
    t = today(db)
    v1 = insert_question(db, t, "New Delhi", "Version 1", f"{t}T08:00:00+05:30")
    assert client.get("/api/v1/questions/today", headers=auth(tokens["employee-wrong"])).json()["data"]["question_id"] == v1
    before = submit(client, v1, tokens["employee-correct"], {"response_answer": "New Delhi"}).json()["data"]

    v2 = insert_question(db, t, "Chennai", "Version 2", f"{t}T09:00:00+05:30")
    assert client.get("/api/v1/questions/today", headers=auth(tokens["employee-wrong"])).json()["data"]["question_id"] == v2

    late = submit(client, v1, tokens["employee-wrong"], {"response_answer": "New Delhi"})
    assert late.status_code == 201
    assert {k: late.json()["data"][k] for k in ("question_id", "question", "correct_answer", "is_correct")} == {
        "question_id": v1, "question": "Version 1", "correct_answer": "New Delhi", "is_correct": True}
    after = client.get(f"/api/v1/questions/{v1}/my-response", headers=auth(tokens["employee-correct"])).json()["data"]
    assert after == before

    again = submit(client, v2, tokens["employee-correct"], {"response_answer": "Chennai"})
    assert again.status_code == 201
    assert (again.json()["data"]["question_id"], again.json()["data"]["correct_answer"], again.json()["data"]["is_correct"]) == (v2, "Chennai", True)
    d = client.get("/api/v1/questions/today", headers=auth(tokens["employee-correct"])).json()["data"]
    assert d["question_id"] == v2 and d["answered"] and d["my_response"]["question_id"] == v2


def test_history_paginates_with_text_values(client: Any, tokens: dict[str, str], clean: Any) -> None:
    db = clean
    t = today(db)
    for i in range(1, 6):
        qid = insert_question(db, add_days(t, -i), "Mumbai", f"Past {i}")
        insert_historic_response(db, qid, "first.last@example.test", "Mumbai" if i % 2 else "Chennai", i % 2 == 1, f"{add_days(t, -i)}T09:00:00+05:30")
        insert_historic_response(db, qid, "someone.else@alrium.com", "Mumbai", True, f"{add_days(t, -i)}T10:00:00+05:30")
    h = auth(tokens["employee-correct"])
    page1 = client.get("/api/v1/me/responses?page_size=2", headers=h).json()
    assert page1["meta"] == {"page": 1, "page_size": 2, "total": 5}
    assert [r["question_date"] for r in page1["data"]] == [add_days(t, -1), add_days(t, -2)]
    assert page1["data"][0]["responded_at"] == f"{add_days(t, -1)}T09:00:00+05:30"
    assert {k: page1["data"][0][k] for k in ("response_answer", "correct_answer", "is_correct", "question")} == {
        "response_answer": "Mumbai", "correct_answer": "Mumbai", "is_correct": True, "question": "Past 1"}
    assert len(client.get("/api/v1/me/responses?page_size=2&page=3", headers=h).json()["data"]) == 1
    assert client.get("/api/v1/me/responses?page_size=2&page=9", headers=h).json() == {"data": [], "meta": {"page": 9, "page_size": 2, "total": 5}}
    ranged = client.get(f"/api/v1/me/responses?from={add_days(t, -4)}&to={add_days(t, -3)}", headers=h).json()
    assert ranged["meta"]["total"] == 2 and all("user_id" not in r for r in ranged["data"])
    assert client.get("/api/v1/me/responses", headers=auth(tokens["employee-unanswered"])).json()["meta"]["total"] == 0


@pytest.mark.parametrize("q", ["page_size=101", "page_size=0", "page=0", "page=-1", "from=2026-02-30", "from=yesterday",
                               "from=2026-10-10&to=2026-10-01", "unknown=1", "page=1&page=2"])
def test_history_rejects_invalid_query(client: Any, tokens: dict[str, str], q: str) -> None:
    res = client.get(f"/api/v1/me/responses?{q}", headers=auth(tokens["employee-correct"]))
    assert res.status_code == 400 and res.json()["error"]["code"] == "VALIDATION_ERROR"
