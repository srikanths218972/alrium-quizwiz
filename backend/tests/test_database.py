"""Schema, migration and SQL-level tests (ported from the former db/tests/run.sh).

The SQL files in db/tests use a few psql features (\\gset, :var, :'var'); qa_api.sqlscript
interprets exactly those, so no psql client is needed.
"""

import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
import pytest
from conftest import ADMIN_URL, fresh_database

from qa_api.migrate import DEFAULT_DIR
from qa_api.migrate import run as migrate
from qa_api.sqlscript import run_script

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "db" / "tests"
FIX = TESTS / "fixtures"


def connect(url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(url, autocommit=True, options="-c timezone=Asia/Kolkata")


def scalar(conn: psycopg.Connection[Any], query: str, params: Any = None) -> Any:
    row = conn.execute(query, params).fetchone()
    return row[0] if row else None


def migrated(target: str | None = None) -> str:
    url = fresh_database("db")
    migrate(url, target=target)
    return url




def test_fresh_install_and_idempotence(capsys: pytest.CaptureFixture[str]) -> None:
    url = fresh_database("db")
    assert migrate(url) == 4
    assert migrate(url) == 0
    with connect(url) as c:
        assert scalar(c, "SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations") == "0001,0002,0003,0004"
        assert scalar(c, "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'") == \
            "questions,responses,schema_migrations,users"
        assert scalar(c, "SHOW timezone") == "Asia/Kolkata"


def test_failing_migration_rolls_back_and_modified_migration_detected(tmp_path: Path) -> None:
    url = migrated()
    for f in DEFAULT_DIR.glob("*.sql"):
        shutil.copy(f, tmp_path)
    (tmp_path / "0099_broken.sql").write_text("CREATE TABLE should_not_exist (x int);\nSELECT 1/0;\n")
    with pytest.raises(psycopg.Error):
        migrate(url, tmp_path)
    with connect(url) as c:
        assert scalar(c, "SELECT to_regclass('should_not_exist') IS NULL")
        assert scalar(c, "SELECT count(*) FROM schema_migrations WHERE version = '0099'") == 0
    (tmp_path / "0099_broken.sql").unlink()
    (tmp_path / "0003_jsonb_questions_text_responses.sql").write_text("-- edited\n")
    with pytest.raises(SystemExit, match="checksum mismatch"):
        migrate(url, tmp_path)




def test_sql_files() -> None:
    url = migrated()
    u = urlparse(url)
    connstr = f"dbname={u.path.lstrip('/')} user={u.username} password={u.password} host=/var/run/postgresql"
    with connect(url) as c:
        for f in sorted(TESTS.glob("0*.sql")):
            run_script(c, f.read_text(), {"connstr": connstr})
        results = c.execute("SELECT name, ok, detail FROM qa_test.results ORDER BY id").fetchall()
    failed = [f"{n} ({d})" for n, ok, d in results if not ok]
    assert len(results) > 100
    assert failed == []




def query(c: psycopg.Connection[Any], name: str, **variables: str) -> list[tuple[Any, ...]]:
    defaults = {"day": "", "user": "", "from": "", "to": ""}
    return run_script(c, (ROOT / "db" / "queries" / f"{name}.sql").read_text(), {**defaults, **variables})


def test_fresh_install_has_no_business_rows() -> None:
    url = migrated()
    with connect(url) as c:
        assert scalar(c, "SELECT (SELECT count(*) FROM questions) || '/' || (SELECT count(*) FROM responses)") == "0/0"


def test_synthetic_data_and_queries() -> None:
    url = migrated()
    seed = (FIX / "synthetic_questions.sql").read_text()
    with connect(url) as c:
        run_script(c, seed)
        first = scalar(c, "SELECT count(*) || '/' || (SELECT count(*) FROM responses) FROM questions")
        run_script(c, seed)
        assert first == "10/0"
        assert scalar(c, "SELECT count(*) || '/' || (SELECT count(*) FROM responses) FROM questions") == first
        assert scalar(c, "SELECT count(*) FROM questions WHERE question->>'answer' IN ('A','B','C','D')") == 0
        today = scalar(c, "SELECT qa_business_date(now())")
        gap = str(today.fromordinal(today.toordinal() - 2))
        missing = query(c, "missing_dates", **{"from": str(today.fromordinal(today.toordinal() - 3)), "to": str(today.fromordinal(today.toordinal() + 7))})
        assert [str(r[0]) for r in missing] == [gap]
        daily = query(c, "daily_question")
        assert len(daily) == 1 and "New Delhi" in str(daily[0]) and '"answer"' not in str(daily[0])
        assert query(c, "daily_question", day=gap) == []
        assert len(query(c, "question_stats")) == 10
        qid = scalar(c, "SELECT question_id FROM questions WHERE question_date = %s", [today])
        c.execute("INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, 'waiter@alrium.com', 'W', 'Mumbai')", [qid])
        hist = query(c, "response_history", user=" WAITER@alrium.com")
        assert len(hist) == 1
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$", hist[0][1])
        assert hist[0][5:] == ("What is the capital of India?", "New Delhi", "Mumbai", False)



SNAP_RESPONSES_0001 = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s', q.question_date, r.user_id, r.user_name,
    q.question->>'question', q.question->'options'->>(q.question->>'answer'), q.question->'options'->>r.response_answer,
    r.is_correct, r.responded_at), E'\\n' ORDER BY r.responded_at, r.user_id) FROM responses r JOIN questions q USING (question_id)"""
SNAP_RESPONSES_0002 = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s|%s|%s', r.response_id, q.question_id, q.question_date, r.user_id,
    r.user_name, q.question, CASE q.correct_answer WHEN 'A' THEN q.option_a WHEN 'B' THEN q.option_b WHEN 'C' THEN q.option_c ELSE q.option_d END,
    CASE r.response_answer WHEN 'A' THEN q.option_a WHEN 'B' THEN q.option_b WHEN 'C' THEN q.option_c ELSE q.option_d END,
    r.is_correct, r.responded_at), E'\\n' ORDER BY r.response_id) FROM responses r JOIN questions q USING (question_id)"""
SNAP_RESPONSES_NEW = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s', q.question_date, r.user_id, r.user_name,
    r.question, r.correct_answer, r.response_answer, r.is_correct, r.responded_at), E'\\n' ORDER BY r.responded_at, r.user_id)
    FROM responses r JOIN questions q USING (question_id)"""
SNAP_RESPONSES_NEW_IDS = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s|%s|%s', r.response_id, q.question_id, q.question_date, r.user_id,
    r.user_name, r.question, r.correct_answer, r.response_answer, r.is_correct, r.responded_at), E'\\n' ORDER BY r.response_id)
    FROM responses r JOIN questions q USING (question_id)"""
SNAP_QUESTIONS_0001 = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s', question_date, question->>'question',
    question->'options'->>'A', question->'options'->>'B', question->'options'->>'C', question->'options'->>'D',
    question->'options'->>(question->>'answer'), created_at), E'\\n' ORDER BY question_date) FROM questions"""
SNAP_QUESTIONS_JSON = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s', question_date, question->>'question',
    question->'options'->>'A', question->'options'->>'B', question->'options'->>'C', question->'options'->>'D',
    question->>'answer', created_at), E'\\n' ORDER BY question_date) FROM questions"""
SNAP_QUESTIONS_0002 = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s|%s', question_id, question_date, question, option_a, option_b,
    option_c, option_d, CASE correct_answer WHEN 'A' THEN option_a WHEN 'B' THEN option_b WHEN 'C' THEN option_c ELSE option_d END,
    created_at), E'\\n' ORDER BY question_id) FROM questions"""
SNAP_QUESTIONS_0003 = """SELECT string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s|%s', question_id, question_date, question->>'question',
    question->'options'->>'A', question->'options'->>'B', question->'options'->>'C', question->'options'->>'D',
    question->>'answer', created_at), E'\\n' ORDER BY question_id) FROM questions"""
QTYPE = "SELECT data_type FROM information_schema.columns WHERE table_name = 'questions' AND column_name = 'question'"


def load(c: psycopg.Connection[Any], *fixtures: str) -> None:
    for f in fixtures:
        run_script(c, (FIX / f).read_text())


def test_upgrade_from_populated_0001() -> None:
    url = migrated("0001")
    with connect(url) as c:
        load(c, "legacy_0001_populated.sql")
        before_r, before_q = scalar(c, SNAP_RESPONSES_0001), scalar(c, SNAP_QUESTIONS_0001)
        assert (scalar(c, QTYPE), scalar(c, "SELECT count(*) FROM responses")) == ("jsonb", 4)
    assert migrate(url) == 3
    with connect(url) as c:
        assert scalar(c, SNAP_RESPONSES_NEW) == before_r
        assert scalar(c, SNAP_QUESTIONS_JSON) == before_q
        assert scalar(c, "SELECT string_agg(question_id::text, ',' ORDER BY question_date) FROM questions") == "1,2,3"
        assert scalar(c, "SELECT string_agg(q.question_id || ':' || r.user_id, ',' ORDER BY r.responded_at) FROM responses r JOIN questions q USING (question_id)") == \
            "1:asha@example.com,1:ravi@example.com,2:meera@example.com,2:asha@example.com"
        qid = scalar(c, """INSERT INTO questions (question_date, question) VALUES (qa_business_date(now()),
                         '{"question":"After upgrade","options":{"A":"a","B":"b","C":"c","D":"d"},"answer":"b"}') RETURNING question_id""")
        rid = c.execute("INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, 'new@alrium.com', 'New', 'b') "
                        "RETURNING response_id, is_correct", [qid]).fetchone()
        assert (qid, *rid) == (4, 5, True)
        with pytest.raises(psycopg.Error):
            c.execute("UPDATE responses SET is_correct = NOT is_correct")
        assert scalar(c, "SELECT count(*) FROM pg_proc WHERE proname = 'qa_is_valid_question'") == 0
        assert scalar(c, "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND data_type = 'uuid'") == 0


def test_inconsistent_0001_refused_and_rolled_back() -> None:
    url = migrated("0001")
    with connect(url) as c:
        load(c, "legacy_0001_populated.sql", "legacy_0001_inconsistent.sql")
        before = scalar(c, SNAP_RESPONSES_0001)
    with pytest.raises(psycopg.Error, match="score that does not match"):
        migrate(url)
    with connect(url) as c:
        assert (scalar(c, "SELECT string_agg(version, ',') FROM schema_migrations"), scalar(c, QTYPE), scalar(c, SNAP_RESPONSES_0001)) == \
            ("0001", "jsonb", before)


def test_upgrade_from_populated_0002() -> None:
    url = migrated("0002")
    with connect(url) as c:
        load(c, "legacy_0002_populated.sql")
        q_before, r_before = scalar(c, SNAP_QUESTIONS_0002), scalar(c, SNAP_RESPONSES_0002)
        seq_before = scalar(c, "SELECT (SELECT last_value FROM questions_question_id_seq) || '/' || (SELECT last_value FROM responses_response_id_seq)")
    assert migrate(url) == 2
    with connect(url) as c:
        assert scalar(c, SNAP_QUESTIONS_0003) == q_before
        assert scalar(c, SNAP_RESPONSES_NEW_IDS) == r_before
        assert scalar(c, "SELECT question->>'answer' FROM questions WHERE question_id = 1") == "New Delhi"
        assert scalar(c, "SELECT count(*) FROM questions WHERE question->>'answer' IN ('A','B','C','D')") == 0
        assert scalar(c, "SELECT string_agg(DISTINCT user_name || '<' || user_id || '>', ',') FROM responses") == \
            "First Last<first.last@alrium.com>,Rahul Sharma<rahul.sharma@alrium.com>"
        assert c.execute("SELECT question, correct_answer, response_answer, is_correct FROM responses WHERE response_id = 2").fetchone() == \
            ("Demo: which city is the capital of India? (today)", "New Delhi", "Mumbai", False)
        assert scalar(c, "SELECT (SELECT last_value FROM questions_question_id_seq) || '/' || (SELECT last_value FROM responses_response_id_seq)") == seq_before
        qid = scalar(c, """INSERT INTO questions (question_date, question) VALUES (qa_business_date(now()),
                         '{"question":"After 0003","options":{"A":"a","B":"b","C":"c","D":"d"},"answer":"c"}') RETURNING question_id""")
        rid = c.execute("INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, 'ananya.iyer@alrium.com', 'Ananya Iyer', 'c') "
                        "RETURNING response_id, is_correct", [qid]).fetchone()
        assert (qid, *rid) == (6, 10, True)
        assert scalar(c, "SELECT count(*) FROM pg_constraint WHERE conrelid = 'questions'::regclass AND contype IN ('u','c')") == 0
        assert scalar(c, "SELECT count(*) FROM pg_indexes WHERE indexname = 'questions_date_latest_idx'") == 1
        assert scalar(c, "SELECT count(*) FROM pg_constraint WHERE conrelid = 'responses'::regclass AND contype = 'c' "
                         "AND pg_get_constraintdef(oid) LIKE '%response_answer%'") == 0
        for stmt in ("UPDATE questions SET question_date = question_date WHERE question_id = 5", "UPDATE responses SET is_correct = NOT is_correct"):
            with pytest.raises(psycopg.Error):
                c.execute(stmt)
        stats = {r[0]: r for r in query(c, "question_stats")}
        assert stats[1][2:] == (2, 1, 1, 1, 1, 0, 0)
        hist = query(c, "response_history", user="rahul.sharma@alrium.com")
        assert {(h[4], h[6], h[7], h[8]) for h in hist} >= {("Rahul Sharma", "New Delhi", "Mumbai", False)}


@pytest.mark.parametrize("fixture,message", [
    ("legacy_0002_inconsistent.sql", "score that does not match"),
    ("legacy_0002_duplicate_options.sql", "duplicate option text"),
])
def test_bad_0002_data_refused_and_rolled_back(fixture: str, message: str) -> None:
    url = migrated("0002")
    with connect(url) as c:
        load(c, "legacy_0002_populated.sql", fixture)
        snap = (scalar(c, SNAP_QUESTIONS_0002), scalar(c, "SELECT count(*) FROM responses"))
    with pytest.raises(psycopg.Error, match=message):
        migrate(url)
    with connect(url) as c:
        assert (scalar(c, "SELECT max(version) FROM schema_migrations"), scalar(c, SNAP_QUESTIONS_0002), scalar(c, "SELECT count(*) FROM responses")) == \
            ("0002", *snap)


def test_admin_url_is_the_disposable_test_server() -> None:
    assert "qa_test" in ADMIN_URL
