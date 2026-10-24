"""All SQL lives here. Every query uses explicit column lists and bound parameters."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from .auth import Principal
from .db import to_delhi_iso

Conn = Connection[DictRow]
Row = dict[str, Any]

RESULT_FIELDS = "response_id, question_id, question, correct_answer, response_answer, is_correct, responded_at"
RESULT_COLUMNS = ", ".join(f"r.{c}" for c in RESULT_FIELDS.split(", "))

ADMIN_COLUMNS = """q.question_id, q.question_date, q.question, q.created_at,
  (SELECT count(*)::int FROM responses r WHERE r.question_id = q.question_id) AS response_count,
  NOT EXISTS (SELECT 1 FROM questions n WHERE n.question_date = q.question_date
              AND (n.created_at, n.question_id) > (q.created_at, q.question_id)) AS is_latest_for_date"""

LATEST = "ORDER BY q.created_at DESC, q.question_id DESC LIMIT 1"


@dataclass
class Paging:
    page: int = 1
    page_size: int = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def _serialize(row: Row) -> Row:
    out: Row = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = to_delhi_iso(v)
        elif isinstance(v, date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _total(conn: Conn, rows: list[Row], count_sql: str, params: list[Any]) -> int:
    """Uses the window count when the page has rows; otherwise counts directly (page past the end)."""
    if rows:
        total = int(rows[0]["total"])
        for r in rows:
            del r["total"]
        return total
    row = conn.execute(count_sql, params).fetchone()
    return int(row["n"]) if row else 0


def business_today(conn: Conn) -> str:
    row = conn.execute("SELECT qa_business_date(clock_timestamp()) AS d").fetchone()
    assert row is not None
    return str(row["d"].isoformat())


def migration_applied(conn: Conn, version: str) -> bool:
    return conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", [version]).fetchone() is not None




def get_today_question(conn: Conn, user_id: str) -> Row | None:
    """Latest row for today's Delhi date. The answer is not selected."""
    row = conn.execute(
        f"""SELECT q.question_id, q.question_date, q.created_at,
                   jsonb_build_object('question', q.question -> 'question', 'options', q.question -> 'options') AS question
            FROM questions q
            WHERE q.question_date = qa_business_date(clock_timestamp())
            {LATEST}"""
    ).fetchone()
    if row is None:
        return None
    mine = get_my_result(conn, row["question_id"], user_id)
    return {**_serialize(row), "answered": mine is not None, "my_response": mine}


def get_my_result(conn: Conn, question_id: int, user_id: str) -> Row | None:
    row = conn.execute(
        f"SELECT {RESULT_COLUMNS} FROM responses r WHERE r.question_id = %s AND r.user_id = %s", [question_id, user_id]
    ).fetchone()
    return _serialize(row) if row else None


def submit_response(conn: Conn, question_id: int, who: Principal, response_answer: str) -> Row:
    """The database trigger locks the exact question row, checks it is dated today, matches the
    text against its options, copies the question and actual answer text, scores and timestamps.
    Only the question ID, the verified identity and the selected text are sent."""
    row = conn.execute(
        f"""INSERT INTO responses (question_id, user_id, user_name, response_answer)
            VALUES (%s, %s, %s, %s) RETURNING {RESULT_FIELDS}""",
        [question_id, who.user_id, who.user_name, response_answer],
    ).fetchone()
    assert row is not None
    return _serialize(row)


def list_my_responses(conn: Conn, user_id: str, date_from: str | None, date_to: str | None, p: Paging) -> tuple[list[Row], int]:
    where = "r.user_id = %s AND (%s::date IS NULL OR q.question_date >= %s::date) AND (%s::date IS NULL OR q.question_date <= %s::date)"
    params = [user_id, date_from, date_from, date_to, date_to]
    rows = conn.execute(
        f"""SELECT {RESULT_COLUMNS}, q.question_date, count(*) OVER ()::int AS total
            FROM responses r JOIN questions q USING (question_id)
            WHERE {where}
            ORDER BY q.question_date DESC, r.response_id DESC
            LIMIT %s OFFSET %s""",
        [*params, p.page_size, p.offset],
    ).fetchall()
    total = _total(conn, rows, f"SELECT count(*)::int AS n FROM responses r JOIN questions q USING (question_id) WHERE {where}", params)
    return [_serialize(r) for r in rows], total




def list_questions(conn: Conn, date_from: str | None, date_to: str | None, p: Paging) -> tuple[list[Row], int]:
    """Every stored version: by date, then oldest version first within a date."""
    where = "(%s::date IS NULL OR q.question_date >= %s::date) AND (%s::date IS NULL OR q.question_date <= %s::date)"
    params = [date_from, date_from, date_to, date_to]
    rows = conn.execute(
        f"""SELECT {ADMIN_COLUMNS}, count(*) OVER ()::int AS total
            FROM questions q WHERE {where}
            ORDER BY q.question_date, q.created_at, q.question_id
            LIMIT %s OFFSET %s""",
        [*params, p.page_size, p.offset],
    ).fetchall()
    total = _total(conn, rows, f"SELECT count(*)::int AS n FROM questions q WHERE {where}", params)
    return [_serialize(r) for r in rows], total


def get_question(conn: Conn, question_id: int) -> Row | None:
    row = conn.execute(f"SELECT {ADMIN_COLUMNS} FROM questions q WHERE q.question_id = %s", [question_id]).fetchone()
    return _serialize(row) if row else None


def create_question(conn: Conn, question_date: str, payload: dict[str, Any]) -> Row:
    """Always a new row, also when the date already has questions. There is no upsert."""
    row = conn.execute(
        "INSERT INTO questions (question_date, question) VALUES (%s, %s) RETURNING question_id", [question_date, Jsonb(payload)]
    ).fetchone()
    assert row is not None
    created = get_question(conn, row["question_id"])
    assert created is not None
    return created


def user_exists(conn: Conn, user_id: str) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE user_id = %s", [user_id]).fetchone() is not None


def create_user(conn: Conn, user_id: str, user_name: str) -> bool:
    """False if the user already exists. Users are never updated or deleted."""
    row = conn.execute("INSERT INTO users (user_id, user_name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
                       [user_id, user_name]).fetchone()
    return row is not None


def list_users(conn: Conn, p: Paging) -> tuple[list[Row], int]:
    """Every row of users, also those with no responses. user_name is the most recent one answered with."""
    rows = conn.execute(
        """SELECT u.user_id, coalesce(a.user_name, u.user_name) AS user_name,
                  coalesce(a.response_count, 0) AS response_count, coalesce(a.correct_count, 0) AS correct_count,
                  a.first_response_at, a.last_response_at, count(*) OVER ()::int AS total
           FROM users u
           LEFT JOIN (SELECT r.user_id,
                        (array_agg(r.user_name ORDER BY r.responded_at DESC, r.response_id DESC))[1] AS user_name,
                        count(*)::int AS response_count,
                        count(*) FILTER (WHERE r.is_correct)::int AS correct_count,
                        min(r.responded_at) AS first_response_at,
                        max(r.responded_at) AS last_response_at
                 FROM responses r GROUP BY r.user_id) a USING (user_id)
           ORDER BY u.user_id
           LIMIT %s OFFSET %s""",
        [p.page_size, p.offset],
    ).fetchall()
    total = _total(conn, rows, "SELECT count(*)::int AS n FROM users", [])
    return [_serialize(r) for r in rows], total


def list_responses(conn: Conn, f: dict[str, Any], p: Paging) -> tuple[list[Row], int]:
    where = """(%(from)s::date IS NULL OR q.question_date >= %(from)s::date) AND (%(to)s::date IS NULL OR q.question_date <= %(to)s::date)
      AND (%(qid)s::int IS NULL OR r.question_id = %(qid)s::int) AND (%(uid)s::text IS NULL OR r.user_id = %(uid)s::text)
      AND (%(ok)s::boolean IS NULL OR r.is_correct = %(ok)s::boolean)"""
    rows = conn.execute(
        f"""SELECT r.response_id, r.question_id, q.question_date, r.user_id, r.user_name, r.question, r.correct_answer,
                   r.response_answer, r.is_correct, r.responded_at, count(*) OVER ()::int AS total
            FROM responses r JOIN questions q USING (question_id)
            WHERE {where}
            ORDER BY r.responded_at DESC, r.response_id DESC
            LIMIT %(limit)s OFFSET %(offset)s""",
        {**f, "limit": p.page_size, "offset": p.offset},
    ).fetchall()
    if rows:
        total = int(rows[0]["total"])
        for r in rows:
            del r["total"]
    else:
        n = conn.execute(f"SELECT count(*)::int AS n FROM responses r JOIN questions q USING (question_id) WHERE {where}", f).fetchone()
        total = int(n["n"]) if n else 0
    return [_serialize(r) for r in rows], total


def question_stats(conn: Conn, question_id: int) -> Row | None:
    """Stored answers are text; the distribution maps each one back to its option label."""
    s = conn.execute(
        """SELECT q.question_id, q.question_date, q.question ->> 'answer' AS correct_answer,
                  count(r.response_id)::int AS total,
                  count(r.response_id) FILTER (WHERE r.is_correct)::int AS correct,
                  count(r.response_id) FILTER (WHERE NOT r.is_correct)::int AS incorrect,
                  count(r.response_id) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'A')::int AS a,
                  count(r.response_id) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'B')::int AS b,
                  count(r.response_id) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'C')::int AS c,
                  count(r.response_id) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'D')::int AS d
           FROM questions q LEFT JOIN responses r USING (question_id)
           WHERE q.question_id = %s
           GROUP BY q.question_id""",
        [question_id],
    ).fetchone()
    if s is None:
        return None
    total = s["total"]
    return {
        "question_id": s["question_id"],
        "question_date": s["question_date"].isoformat(),
        "correct_answer": s["correct_answer"],
        "total": total,
        "correct": s["correct"],
        "incorrect": s["incorrect"],
        "distribution": {"A": s["a"], "B": s["b"], "C": s["c"], "D": s["d"]},
        "unmatched": total - s["a"] - s["b"] - s["c"] - s["d"],
        "accuracy": round(s["correct"] / total, 4) if total else None,
    }


def day_answers(conn: Conn, day: str) -> Row:
    """For one question date: each response with its user, and the registered users with no response that day.
    Users who signed up after that date are not counted as missing it."""
    questions = [r["question_id"] for r in conn.execute(
        "SELECT question_id FROM questions WHERE question_date = %s ORDER BY question_id", [day]).fetchall()]
    answered = conn.execute(
        """SELECT r.user_id, r.user_name, r.question_id, r.question, r.response_answer, r.correct_answer, r.is_correct, r.responded_at
           FROM responses r JOIN questions q USING (question_id)
           WHERE q.question_date = %s
           ORDER BY r.user_id, r.responded_at, r.response_id""",
        [day],
    ).fetchall()
    missing = conn.execute(
        """SELECT u.user_id, u.user_name FROM users u
           WHERE qa_business_date(u.created_at) <= %s::date
             AND NOT EXISTS (SELECT 1 FROM responses r JOIN questions q USING (question_id)
                             WHERE r.user_id = u.user_id AND q.question_date = %s::date)
           ORDER BY u.user_id""",
        [day, day],
    ).fetchall()
    return {"question_date": day, "question_ids": questions, "answered": [_serialize(r) for r in answered],
            "not_answered": [dict(r) for r in missing]}


def schedule_gaps(conn: Conn, date_from: str, date_to: str) -> list[str]:
    rows = conn.execute(
        """SELECT d::date AS d
           FROM generate_series(%s::date, %s::date, interval '1 day') AS d
           WHERE NOT EXISTS (SELECT 1 FROM questions q WHERE q.question_date = d::date)
           ORDER BY 1""",
        [date_from, date_to],
    ).fetchall()
    return [r["d"].isoformat() for r in rows]
