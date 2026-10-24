"""End-to-end tests of qa_api.pgmove. Run through scripts/test-pgmove (isolated source and target)."""

import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import fresh_database

from qa_api import pgmove
from qa_api.migrate import run as migrate
from qa_api.sqlscript import run_script

TARGET_ADMIN = os.environ.get("QA_PGMOVE_TARGET_ADMIN_URL", "")
pytestmark = pytest.mark.skipif(not TARGET_ADMIN, reason="run through scripts/test-pgmove")
FIX = Path(__file__).resolve().parents[2] / "db" / "tests" / "fixtures"


@pytest.fixture(scope="module")
def source() -> str:
    url = fresh_database("src")
    migrate(url, target="0002")
    with psycopg.connect(url, autocommit=True) as c:
        run_script(c, (FIX / "legacy_0002_populated.sql").read_text())
    migrate(url)
    with psycopg.connect(url, autocommit=True, options="-c timezone=Asia/Kolkata") as c:
        c.execute("""INSERT INTO questions (question_date, question) VALUES (qa_business_date(now()),
                     '{"question":"What is the capital of India?","options":{"A":"Mumbai","B":"New Delhi","C":"Chennai","D":"Kolkata"},"answer":"New Delhi"}')""")
    return url


@pytest.fixture(scope="module")
def owner() -> str:
    with psycopg.connect(TARGET_ADMIN, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = 'qa_app_owner'").fetchone():
            c.execute("CREATE ROLE qa_app_owner LOGIN PASSWORD 'owner-test-password'")
    return "qa_app_owner"


def env(monkeypatch: pytest.MonkeyPatch, source: str, owner: str, db: str) -> pgmove.Config:
    monkeypatch.setenv("SOURCE_DATABASE_URL", source)
    monkeypatch.setenv("TARGET_ADMIN_URL", TARGET_ADMIN)
    monkeypatch.setenv("TARGET_DATABASE", db)
    monkeypatch.setenv("TARGET_OWNER", owner)
    return pgmove.config()


def snapshot(url: str) -> dict[str, Any]:
    with psycopg.connect(url, autocommit=True) as c:
        return pgmove.fingerprint(c)


def test_full_migration_and_guards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, owner: str,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    cfg = env(monkeypatch, source, owner, "daily_qa_local")
    before = snapshot(source)
    pgmove.preflight(cfg)
    folder = pgmove.backup(cfg, tmp_path / "bk")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["sha256"] == pgmove.sha256(folder / manifest["file"])
    assert (folder / f"{manifest['file']}.sha256").read_text().split()[0] == manifest["sha256"]
    text = (folder / "manifest.json").read_text()
    assert "password" not in text.lower() and cfg.source.password not in text
    pgmove.restore(cfg, folder)
    pgmove.verify(cfg)
    out = capsys.readouterr().out
    assert cfg.source.password not in out and cfg.target_admin.password not in out
    assert "read-only checks: 4 questions, 4 responses" in out

    with cfg.target.connect() as t:
        assert t.execute("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'").fetchall() == [(owner,)]
        assert t.execute("SELECT question->>'answer' FROM questions WHERE question_id = 1").fetchone() == ("New Delhi",)
        assert t.execute("SELECT response_answer, correct_answer, is_correct FROM responses WHERE response_id = 2").fetchone() == \
            ("Mumbai", "New Delhi", False)
    with psycopg.connect(cfg.target.url()) as fresh:
        assert fresh.execute("SHOW timezone").fetchone() == ("Asia/Kolkata",)
    with cfg.target.connect() as t, pytest.raises(psycopg.Error):
        t.execute("UPDATE responses SET is_correct = NOT is_correct")
    assert snapshot(source) == before

    with pytest.raises(pgmove.Fail, match="already exists"):
        pgmove.restore(cfg, folder)
    with pytest.raises(pgmove.Fail, match="already exists"):
        pgmove.preflight(cfg)

    bad = env(monkeypatch, source, owner, "daily_qa_damaged")
    dump = folder / manifest["file"]
    dump.write_bytes(dump.read_bytes() + b"x")
    with pytest.raises(pgmove.Fail, match="checksum mismatch"):
        pgmove.restore(bad, folder)
    with psycopg.connect(TARGET_ADMIN, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM pg_database WHERE datname = 'daily_qa_damaged'").fetchone() == (0,)

    with cfg.target.connect() as t, t.transaction():
        t.execute("SET LOCAL session_replication_role = replica")
        t.execute("DELETE FROM responses WHERE response_id = 7")
    with pytest.raises(pgmove.Fail, match="differ in: tables"):
        pgmove.verify(cfg)


def test_preflight_rejects_same_instance_and_missing_owner(monkeypatch: pytest.MonkeyPatch, source: str, owner: str) -> None:
    same = env(monkeypatch, source, owner, "x_new_db")
    monkeypatch.setenv("TARGET_ADMIN_URL", source)
    with pytest.raises(pgmove.Fail, match="same PostgreSQL instance|same instance"):
        pgmove.preflight(pgmove.config())
    del same
    env(monkeypatch, source, "role_that_does_not_exist", "daily_qa_other")
    with pytest.raises(pgmove.Fail, match="does not exist"):
        pgmove.preflight(pgmove.config())


def test_missing_tools_give_instructions(monkeypatch: pytest.MonkeyPatch, source: str, owner: str, tmp_path: Path) -> None:
    env(monkeypatch, source, owner, "daily_qa_tools")
    monkeypatch.setenv("PG_BIN", str(tmp_path))
    with pytest.raises(pgmove.Fail, match="pg_dump not found.*does not install anything"):
        pgmove.preflight(pgmove.config())


def test_failed_restore_reports_incomplete_target(monkeypatch: pytest.MonkeyPatch, source: str, owner: str, tmp_path: Path) -> None:
    cfg = env(monkeypatch, source, owner, "daily_qa_partial")
    folder = pgmove.backup(cfg, tmp_path / "bk")
    manifest = json.loads((folder / "manifest.json").read_text())
    (folder / manifest["file"]).write_bytes(b"PGDMP not really a dump")
    manifest["sha256"] = pgmove.sha256(folder / manifest["file"])
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(pgmove.Fail, match="EMPTY and incomplete"):
        pgmove.restore(cfg, folder)
