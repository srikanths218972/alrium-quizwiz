"""Test support. Runs against the DISPOSABLE PostgreSQL started by scripts/test-db (never .env)."""

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from psycopg import sql

from qa_api.app import create_app
from qa_api.config import Settings
from qa_api.db import create_pool
from qa_api.migrate import run as migrate

ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "")
if not ADMIN_URL:
    raise pytest.UsageError("Tests must run through scripts/test-db (disposable database); TEST_DATABASE_URL is not set.")

for _k in list(os.environ):
    if _k.startswith(("ENTRA_", "EMAIL_", "ADMIN_API_KEY", "LOCAL_TEST_", "AUTH_MODE", "APP_ENV", "API_DOCS_PUBLIC", "DATABASE_URL", "ALLOWED_ORIGINS")):
        del os.environ[_k]

ADMIN_KEY = "test-admin-key-0123456789abcdef-0123456789"
EMAIL_ENV = {
    "AUTH_MODE": "email",
    "APP_ENV": "local",
    "API_DOCS_PUBLIC": "true",
    "EMAIL_TOKEN_SECRET": "test-email-token-secret-0123456789abcdef",
    "ADMIN_API_KEY": ADMIN_KEY,
    "EMAIL_LOGIN_RATE_PER_MINUTE": "1000",
}
PEOPLE = {
    "employee-correct": "first.last@example.test",
    "employee-wrong": "second.person@example.test",
    "employee-unanswered": "third.person+quiz@example.test",
}
ADMIN = "admin-key"
DELHI_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:30$"
CAPITAL = {"A": "Mumbai", "B": "New Delhi", "C": "Chennai", "D": "Kolkata"}


def url_for(database: str) -> str:
    return ADMIN_URL.rsplit("/", 1)[0] + "/" + database


def fresh_database(prefix: str = "t") -> str:
    """Creates an empty database and returns its URL."""
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return url_for(name)


@pytest.fixture(scope="session")
def db_url() -> str:
    url = fresh_database("api")
    migrate(url)
    return url


@pytest.fixture
def db(db_url: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(db_url, autocommit=True, options="-c timezone=Asia/Kolkata") as c:
        yield c


def settings(url: str, **env: str) -> Settings:
    return Settings(DATABASE_URL=url, **{**EMAIL_ENV, **env})  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def client(db_url: str) -> Iterator[TestClient]:
    pool = create_pool(db_url, 10)
    with TestClient(create_app(settings(db_url), pool=pool)) as c:
        yield c


def email_token(client: TestClient, email: str) -> str:
    """Signs up the email if needed, then returns its session token."""
    res = client.post("/api/v1/auth/email-signup", json={"email": email})
    if res.status_code == 409:
        res = client.post("/api/v1/auth/email-login", json={"email": email})
    assert res.status_code in (200, 201), res.text
    return str(res.json()["data"]["access_token"])


@pytest.fixture(scope="session")
def tokens(client: TestClient) -> dict[str, str]:
    """Participant tokens from the real email-login route; "admin" stands for the X-Admin-Key credential."""
    return {"admin": ADMIN, **{p: email_token(client, e) for p, e in PEOPLE.items()}}


def auth(token: str) -> dict[str, str]:
    if token == ADMIN:
        return {"x-admin-key": ADMIN_KEY}
    return {"authorization": f"Bearer {token}"}


def reset(db: psycopg.Connection[Any]) -> None:
    """Test-only: removes all rows by disabling triggers for one transaction (superuser, disposable DB)."""
    with db.transaction():
        db.execute("SET LOCAL session_replication_role = replica")
        db.execute("DELETE FROM responses")
        db.execute("DELETE FROM questions")
        db.execute("DELETE FROM users")


@pytest.fixture
def clean(db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    reset(db)
    return db


def today(db: psycopg.Connection[Any]) -> str:
    return str(db.execute("SELECT qa_business_date(clock_timestamp())").fetchone()[0])  # type: ignore[index]


def add_days(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def payload(answer: str = "New Delhi", text: str = "What is the capital of India?", options: dict[str, str] | None = None) -> dict[str, Any]:
    return {"question": text, "options": options or CAPITAL, "answer": answer}


def insert_question(db: psycopg.Connection[Any], d: str, answer: str = "New Delhi", text: str = "What is the capital of India?",
                    created_at: str | None = None) -> int:
    """Inserts directly (bypassing API validation, as an import would)."""
    row = db.execute(
        "INSERT INTO questions (question_date, question, created_at) VALUES (%s, %s, COALESCE(%s::timestamptz, now())) RETURNING question_id",
        [d, json.dumps(payload(answer, text)), created_at],
    ).fetchone()
    assert row
    return int(row[0])


def insert_raw_question(db: psycopg.Connection[Any], d: str, raw: Any) -> int:
    row = db.execute("INSERT INTO questions (question_date, question) VALUES (%s, %s) RETURNING question_id", [d, json.dumps(raw)]).fetchone()
    assert row
    return int(row[0])


def insert_historic_response(db: psycopg.Connection[Any], qid: int, user: str, answer: str, correct: bool, at: str) -> None:
    """Test-only: a response for a past date (the trigger would reject it). Values are text."""
    with db.transaction():
        db.execute("SET LOCAL session_replication_role = replica")
        db.execute(
            """INSERT INTO responses (question_id, user_id, user_name, question, correct_answer, response_answer, is_correct, responded_at)
               SELECT %s, %s, 'Historic User', q.question ->> 'question', q.question ->> 'answer', %s, %s, %s
               FROM questions q WHERE q.question_id = %s""",
            [qid, user, answer, correct, at, qid],
        )



TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-2222-3333-4444-555555555555"
API_CLIENT = "22222222-0000-0000-0000-000000000001"
SPA_CLIENT = "22222222-0000-0000-0000-000000000002"
POSTMAN_CLIENT = "22222222-0000-0000-0000-000000000003"
AUDIENCE = f"api://{API_CLIENT}"
ENTRA_ENV = {
    "AUTH_MODE": "entra",
    "APP_ENV": "production",
    "ENTRA_TENANT_ID": TENANT,
    "ENTRA_API_AUDIENCE": f"{AUDIENCE},{API_CLIENT}",
    "ENTRA_REQUIRED_SCOPE": "Quiz.Participate",
    "ENTRA_ALLOWED_CLIENT_IDS": f"{SPA_CLIENT},{POSTMAN_CLIENT}",
    "ENTRA_ADMIN_ROLE": "Quiz.Admin",
}


class EntraKit:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def resolver(self) -> Callable[[str], Any]:
        public = self.key.public_key()

        def resolve(token: str) -> Any:
            if jwt.get_unverified_header(token).get("kid") != "test-key":
                raise ValueError("unknown kid")
            return public

        return resolve

    def sign(self, claims: dict[str, Any] | None = None, *, issuer: str | None = None, other: bool = False, kid: str = "test-key") -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "aud": AUDIENCE, "iss": issuer or f"https://login.microsoftonline.com/{TENANT}/v2.0", "iat": now, "nbf": now,
            "exp": now + 3600, "ver": "2.0", "tid": TENANT, "azp": SPA_CLIENT, "scp": "Quiz.Participate",
            "oid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "sub": "subject-1", "email": "Employee.One@Contoso.example",
            "name": "Employee One", "acct": 0, **(claims or {}),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, self.other if other else self.key, algorithm="RS256", headers={"kid": kid})
