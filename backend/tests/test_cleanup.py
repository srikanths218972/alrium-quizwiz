"""qa_api.cleanup_samples against an isolated database holding exact copies of the known sample rows."""

import json
from typing import Any

import psycopg
import pytest
from conftest import fresh_database

from qa_api import cleanup_samples as cs
from qa_api.migrate import run as migrate

PAYLOAD = '{"question": "%s", "options": {"A": "Mumbai", "B": "New Delhi", "C": "Chennai", "D": "Kolkata"}, "answer": "New Delhi"}'


def database(extra_response_on_q1: bool = False) -> str:
    url = fresh_database("clean")
    migrate(url)
    with psycopg.connect(url, autocommit=True) as c, c.transaction():
        c.execute("SET LOCAL session_replication_role = replica")
        for q in cs.SAMPLES["questions"]:
            c.execute("INSERT INTO questions (question_id, question_date, question, created_at) OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s)",
                      [q["question_id"], q["question_date"], PAYLOAD % q["text"], q["created_at"]])
        for r in cs.SAMPLES["responses"]:
            c.execute("""INSERT INTO responses (response_id, question_id, user_id, user_name, question, correct_answer, response_answer,
                         is_correct, responded_at) OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, 'q', 'New Delhi', %s, %s, %s)""",
                      [r["response_id"], r["question_id"], r["user_id"], r["user_name"], r["response_answer"],
                       r["response_answer"] == "New Delhi", r["responded_at"]])
        c.execute("INSERT INTO questions (question_id, question_date, question, created_at) OVERRIDING SYSTEM VALUE "
                  "VALUES (20, '2026-10-25', %s, '2026-10-24T10:00:00+05:30')", [PAYLOAD % "Real question"])
        c.execute("""INSERT INTO responses (response_id, question_id, user_id, user_name, question, correct_answer, response_answer,
                     is_correct, responded_at) OVERRIDING SYSTEM VALUE
                     VALUES (30, 20, 'first.last@alrium.com', 'First Last', 'Real question', 'New Delhi', 'Mumbai', false, '2026-10-25T09:00:00+05:30')""")
        if extra_response_on_q1:
            c.execute("""INSERT INTO responses (response_id, question_id, user_id, user_name, question, correct_answer, response_answer,
                         is_correct, responded_at) OVERRIDING SYSTEM VALUE
                         VALUES (31, 1, 'ananya.iyer@alrium.com', 'Ananya Iyer', 'q', 'New Delhi', 'Chennai', false, '2026-10-23T11:00:00+05:30')""")
        c.execute("SELECT setval('questions_question_id_seq', 40), setval('responses_response_id_seq', 40)")
    return url


def rows(url: str) -> tuple[list[Any], list[Any], Any]:
    with psycopg.connect(url) as c:
        return (c.execute("SELECT question_id, question_date, question, created_at FROM questions ORDER BY 1").fetchall(),
                c.execute("SELECT * FROM responses ORDER BY 1").fetchall(),
                c.execute("SELECT last_value FROM questions_question_id_seq").fetchone())


def test_report_is_read_only_and_apply_removes_only_samples() -> None:
    url = database()
    before = rows(url)
    with psycopg.connect(url, autocommit=True) as c:
        p = cs.plan(c)
    assert (p["delete_responses"], p["delete_questions"], p["kept_questions"]) == ([1, 2], [1, 4, 5], [])
    assert rows(url) == before
    with psycopg.connect(url, autocommit=True) as c:
        cs.apply(c)
        assert c.execute("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled <> 'O' "
                         "AND tgrelid IN ('questions'::regclass, 'responses'::regclass)").fetchone() == (0,)
        with pytest.raises(psycopg.Error):
            c.execute("DELETE FROM questions WHERE question_id = 20")
    q, r, seq = rows(url)
    assert [x[0] for x in q] == [20] and q[0] == before[0][-1]
    assert r == [before[1][-1]]
    assert seq == before[2]
    with psycopg.connect(url, autocommit=True) as c:
        again = cs.apply(c)
    assert (again["delete_responses"], again["delete_questions"]) == ([], [])
    assert rows(url) == (q, r, seq)


def test_question_with_a_non_sample_response_is_kept() -> None:
    url = database(extra_response_on_q1=True)
    with psycopg.connect(url, autocommit=True) as c:
        p = cs.apply(c)
    assert p["kept_questions"] == [{"question_id": 1, "referenced_by_responses": [31]}]
    q, r, _ = rows(url)
    assert [x[0] for x in q] == [1, 20] and [x[0] for x in r] == [30, 31]


def test_unexpected_values_abort_without_changes() -> None:
    url = database()
    with psycopg.connect(url, autocommit=True) as c, c.transaction():
        c.execute("SET LOCAL session_replication_role = replica")
        c.execute("UPDATE responses SET user_name = 'Someone Else' WHERE response_id = 2")
    before = rows(url)
    with psycopg.connect(url, autocommit=True) as c, pytest.raises(cs.Abort, match="response 2"):
        cs.apply(c)
    assert rows(url) == before


def test_backup_is_protected_and_complete(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "BACKUPS", tmp_path)
    url = database()
    with psycopg.connect(url, autocommit=True) as c:
        path = cs.backup(c)
    data = json.loads(path.read_text())
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert [q["question_id"] for q in data["questions"]] == [1, 4, 5, 20] and len(data["responses"]) == 3
    assert (tmp_path / (path.name + ".sha256")).exists()
