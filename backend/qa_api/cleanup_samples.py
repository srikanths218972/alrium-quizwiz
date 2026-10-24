"""Removes positively identified sample rows from the database named by DATABASE_URL.

    python -m qa_api.cleanup_samples            read-only report (default)
    python -m qa_api.cleanup_samples --apply    back up, then delete the identified rows

Only the exact rows listed in SAMPLES are candidates. Each is matched on every identifying value
(ID, date, text, identity, answer, timestamp); a mismatch aborts without changes. A sample
question is kept if any response that is not a known sample references it. Questions and
responses are otherwise immutable: only the two protection triggers are disabled, inside the
transaction, and re-enabled and checked before commit (foreign keys stay enforced). Identity
sequences are not reset. Rerunning after a successful apply finds nothing and changes nothing.
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from .pgmove import sha256

BACKUPS = Path(__file__).resolve().parents[2] / "backups"

SAMPLES: dict[str, list[dict[str, Any]]] = {
    "questions": [
        {"question_id": 1, "question_date": "2026-10-23", "text": "Demo: which city is the capital of India? (today)",
         "created_at": "2026-10-23T09:06:13.677159+00:00"},
        {"question_id": 4, "question_date": "2026-10-24", "text": "Demo: which city is the capital of India? (future)",
         "created_at": "2026-10-23T09:06:14.104557+00:00"},
        {"question_id": 5, "question_date": "2026-10-22", "text": "Demo: which city is the capital of India? (past)",
         "created_at": "2026-10-23T09:06:14.156825+00:00"},
    ],
    "responses": [
        {"response_id": 1, "question_id": 1, "user_id": "first.last@alrium.com", "user_name": "First Last",
         "response_answer": "New Delhi", "responded_at": "2026-10-23T09:06:13.756900+00:00"},
        {"response_id": 2, "question_id": 1, "user_id": "rahul.sharma@alrium.com", "user_name": "Rahul Sharma",
         "response_answer": "Mumbai", "responded_at": "2026-10-23T09:06:13.772368+00:00"},
    ],
}
TRIGGERS = (("questions", "questions_block_update_delete"), ("responses", "responses_block_update_delete"))


class Abort(Exception):
    pass


def _iso(v: datetime) -> str:
    return v.astimezone(UTC).isoformat()


def plan(c: psycopg.Connection[Any]) -> dict[str, Any]:
    """Classifies rows. Raises Abort if a listed row exists but differs from its recorded identity."""
    out: dict[str, Any] = {"delete_responses": [], "delete_questions": [], "kept_questions": [], "absent": [], "retained": {}}
    for s in SAMPLES["responses"]:
        row = c.execute("SELECT question_id, user_id, user_name, response_answer, responded_at FROM responses WHERE response_id = %s",
                        [s["response_id"]]).fetchone()
        if row is None:
            out["absent"].append(f"response {s['response_id']}")
            continue
        actual = {"question_id": row[0], "user_id": row[1], "user_name": row[2], "response_answer": row[3], "responded_at": _iso(row[4])}
        expected = {k: s[k] for k in actual}
        if actual != expected:
            raise Abort(f"response {s['response_id']} does not match its recorded sample identity; nothing was changed")
        out["delete_responses"].append(s["response_id"])
    sample_responses = {s["response_id"] for s in SAMPLES["responses"]}
    for s in SAMPLES["questions"]:
        row = c.execute("SELECT question_date::text, question ->> 'question', created_at FROM questions WHERE question_id = %s",
                        [s["question_id"]]).fetchone()
        if row is None:
            out["absent"].append(f"question {s['question_id']}")
            continue
        if (row[0], row[1], _iso(row[2])) != (s["question_date"], s["text"], s["created_at"]):
            raise Abort(f"question {s['question_id']} does not match its recorded sample identity; nothing was changed")
        others = [r[0] for r in c.execute("SELECT response_id FROM responses WHERE question_id = %s ORDER BY 1",
                                          [s["question_id"]]).fetchall() if r[0] not in sample_responses]
        if others:
            out["kept_questions"].append({"question_id": s["question_id"], "referenced_by_responses": others})
        else:
            out["delete_questions"].append(s["question_id"])
    out["retained"] = {
        "questions": c.execute("SELECT count(*) FROM questions WHERE NOT (question_id = ANY(%s))", [out["delete_questions"]]).fetchone()[0],  # type: ignore[index]
        "responses": c.execute("SELECT count(*) FROM responses WHERE NOT (response_id = ANY(%s))", [out["delete_responses"]]).fetchone()[0],  # type: ignore[index]
    }
    return out


def backup(c: psycopg.Connection[Any]) -> Path:
    """Protected JSON copy of every application row and the sequence positions (no secrets)."""
    data: dict[str, Any] = {"created_at": datetime.now(UTC).isoformat(timespec="seconds"), "database": c.info.dbname}
    for table, key in (("questions", "question_id"), ("responses", "response_id"), ("schema_migrations", "version")):
        cur = c.execute(f"SELECT * FROM {table} ORDER BY {key}")  # noqa: S608 - fixed table names
        names = [d.name for d in cur.description or []]
        data[table] = [{n: (v.isoformat() if isinstance(v, datetime) else str(v) if not isinstance(v, int | bool | dict | type(None)) else v)
                        for n, v in zip(names, r, strict=True)} for r in cur.fetchall()]
    data["sequences"] = dict(c.execute("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'").fetchall())
    BACKUPS.mkdir(exist_ok=True)
    path = BACKUPS / f"before-sample-cleanup-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    path.chmod(0o600)
    (path.parent / (path.name + ".sha256")).write_text(f"{sha256(path)}  {path.name}\n")
    return path


def apply(c: psycopg.Connection[Any]) -> dict[str, Any]:
    with c.transaction():
        c.execute("LOCK TABLE questions, responses IN ACCESS EXCLUSIVE MODE")
        p = plan(c)
        if not p["delete_responses"] and not p["delete_questions"]:
            return p
        for table, trigger in TRIGGERS:
            c.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        n_r = c.execute("DELETE FROM responses WHERE response_id = ANY(%s)", [p["delete_responses"]]).rowcount
        n_q = c.execute("DELETE FROM questions WHERE question_id = ANY(%s)", [p["delete_questions"]]).rowcount
        if (n_r, n_q) != (len(p["delete_responses"]), len(p["delete_questions"])):
            raise Abort("unexpected delete count; rolled back")
        for table, trigger in TRIGGERS:
            c.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
        disabled = c.execute("""SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'D'
                                AND tgrelid IN ('questions'::regclass, 'responses'::regclass)""").fetchone()[0]  # type: ignore[index]
        orphans = c.execute("SELECT count(*) FROM responses r WHERE NOT EXISTS "
                            "(SELECT 1 FROM questions q WHERE q.question_id = r.question_id)").fetchone()[0]  # type: ignore[index]
        counts = (c.execute("SELECT count(*) FROM questions").fetchone()[0], c.execute("SELECT count(*) FROM responses").fetchone()[0])  # type: ignore[index]
        if disabled or orphans or counts != (p["retained"]["questions"], p["retained"]["responses"]):
            raise Abort("post-delete verification failed; rolled back")
    return p


def report(p: dict[str, Any], applied: bool) -> None:
    verb = "deleted" if applied else "would delete"
    print(f"sample responses {verb}: {p['delete_responses'] or 'none'}")
    print(f"sample questions {verb}: {p['delete_questions'] or 'none'}")
    for k in p["kept_questions"]:
        print(f"kept sample question {k['question_id']}: referenced by non-sample responses {k['referenced_by_responses']} (needs a decision)")
    if p["absent"]:
        print(f"already absent: {', '.join(p['absent'])}")
    print(f"retained rows: {p['retained']['questions']} questions, {p['retained']['responses']} responses")
    print("reason: rows inserted by the former Postman demo collection on 2026-10-23 14:36 (see SAMPLES in qa_api/cleanup_samples.py)")


def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("error: DATABASE_URL is not set")
    try:
        with psycopg.connect(url, autocommit=True, options="-c timezone=Asia/Kolkata") as c:
            if "--apply" not in sys.argv:
                c.execute("SET default_transaction_read_only = on")
                report(plan(c), applied=False)
                print("read-only report; run with --apply to back up and delete")
                return
            p = plan(c)
            if not p["delete_responses"] and not p["delete_questions"]:
                report(p, applied=True)
                print("nothing to delete")
                return
            path = backup(c)
            print(f"backup: {path.relative_to(BACKUPS.parent)}")
            report(apply(c), applied=True)
    except Abort as err:
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
