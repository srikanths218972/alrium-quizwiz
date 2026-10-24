"""Moves the ålrium - QuizWiz database from Docker PostgreSQL to a locally installed PostgreSQL.

    python -m qa_api.pgmove preflight            check tools, versions, connectivity, privileges
    python -m qa_api.pgmove backup               timestamped pg_dump of the source + checksum + manifest
    python -m qa_api.pgmove restore <backup-dir> restore into a NEW target database
    python -m qa_api.pgmove verify               compare source and target, then read-only API smoke checks
    python -m qa_api.pgmove migrate              preflight, backup, restore, verify (stops at the first failure)

Configuration (environment, see .env.example): SOURCE_DATABASE_URL, TARGET_ADMIN_URL,
TARGET_DATABASE, TARGET_OWNER, optional PG_BIN. Uses the PostgreSQL client tools installed on
this machine; Docker is only the source database runtime. Passwords are passed to the tools
through the environment, never on a command line, and are never printed.

It never changes .env, never stops or deletes the source container or volume, and never
overwrites an existing target database. Stop application writers before the final backup and
keep them stopped until cutover (see docs/POSTGRES-MIGRATION.md).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
BACKUPS = ROOT / "backups" / "postgres"
TABLES = {"questions": "question_id", "responses": "response_id", "schema_migrations": "version"}
NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Fail(Exception):
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class Conn:
    """A libpq connection target. str() never includes the password."""

    host: str
    port: int
    user: str
    password: str
    dbname: str

    @classmethod
    def parse(cls, url: str, label: str) -> "Conn":
        u = urlparse(url)
        if u.scheme not in ("postgresql", "postgres") or not u.path.strip("/"):
            raise Fail(f"{label} must look like postgresql://user:password@host:port/database")
        return cls(u.hostname or "127.0.0.1", u.port or 5432, unquote(u.username or ""), unquote(u.password or ""), unquote(u.path.lstrip("/")))

    def with_db(self, dbname: str) -> "Conn":
        return Conn(self.host, self.port, self.user, self.password, dbname)

    def __str__(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.dbname}"

    def url(self) -> str:
        from urllib.parse import quote

        return f"postgresql://{quote(self.user, safe='')}:{quote(self.password, safe='')}@{self.host}:{self.port}/{quote(self.dbname, safe='')}"

    def connect(self, **kw: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(host=self.host, port=self.port, user=self.user, password=self.password, dbname=self.dbname,
                               connect_timeout=5, options="-c timezone=Asia/Kolkata", autocommit=True, **kw)

    def tool_args(self) -> list[str]:
        return ["-h", self.host, "-p", str(self.port), "-U", self.user, "-d", self.dbname]

    def tool_env(self) -> dict[str, str]:
        return {**os.environ, "PGPASSWORD": self.password, "PGTZ": "Asia/Kolkata", "PGCONNECT_TIMEOUT": "5"}


@dataclass
class Config:
    source: Conn
    target_admin: Conn
    target_db: str
    target_owner: str
    pg_bin: str

    @property
    def target(self) -> Conn:
        return self.target_admin.with_db(self.target_db)


def config() -> Config:
    missing = [k for k in ("SOURCE_DATABASE_URL", "TARGET_ADMIN_URL", "TARGET_DATABASE", "TARGET_OWNER") if not os.environ.get(k)]
    if missing:
        raise Fail("set " + ", ".join(missing) + " (see .env.example and docs/POSTGRES-MIGRATION.md)")
    target_db, owner = os.environ["TARGET_DATABASE"], os.environ["TARGET_OWNER"]
    for label, value in (("TARGET_DATABASE", target_db), ("TARGET_OWNER", owner)):
        if not NAME.match(value):
            raise Fail(f"{label} must be a plain lowercase identifier")
    return Config(Conn.parse(os.environ["SOURCE_DATABASE_URL"], "SOURCE_DATABASE_URL"),
                  Conn.parse(os.environ["TARGET_ADMIN_URL"], "TARGET_ADMIN_URL"), target_db, owner, os.environ.get("PG_BIN", ""))


def tool(cfg: Config, name: str) -> str:
    path = str(Path(cfg.pg_bin) / name) if cfg.pg_bin else shutil.which(name)
    if not path or not Path(path).exists():
        raise Fail(
            f"{name} not found. Install the PostgreSQL 18 client tools on this machine, e.g. macOS: `brew install postgresql@18` "
            "(or the EDB/Postgres.app installer); Debian/Ubuntu: the PGDG postgresql-client-18 package. "
            "Then put them on PATH or set PG_BIN to their directory. This script does not install anything."
        )
    return path


def tool_major(path: str) -> int:
    out = subprocess.run([path, "--version"], capture_output=True, text=True, check=True).stdout  # noqa: S603
    m = re.search(r"(\d+)(?:\.\d+)?", out)
    if not m:
        raise Fail(f"cannot read version from {path}")
    return int(m.group(1))


def server_major(c: psycopg.Connection[Any]) -> int:
    return int(c.execute("SHOW server_version_num").fetchone()[0]) // 10000  # type: ignore[index]


def instance_id(c: psycopg.Connection[Any]) -> str:
    try:
        return str(c.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])  # type: ignore[index]
    except psycopg.Error:
        return ""




def preflight(cfg: Config) -> dict[str, Any]:
    say("== preflight")
    tools = {n: tool(cfg, n) for n in ("pg_dump", "pg_restore", "psql")}
    majors = {n: tool_major(p) for n, p in tools.items()}
    say("   client tools: " + ", ".join(f"{n} {v}" for n, v in majors.items()))
    try:
        src = cfg.source.connect()
    except psycopg.Error as err:
        raise Fail(f"cannot connect to source {cfg.source}: {type(err).__name__}") from None
    try:
        adm = cfg.target_admin.connect()
    except psycopg.Error as err:
        raise Fail(f"cannot connect to target server {cfg.target_admin}: {type(err).__name__}") from None
    with src, adm:
        s_major, t_major = server_major(src), server_major(adm)
        say(f"   source {cfg.source}: PostgreSQL {s_major}")
        say(f"   target {cfg.target_admin}: PostgreSQL {t_major}")
        if t_major < s_major:
            raise Fail(f"target PostgreSQL {t_major} is older than source {s_major}; downgrades are not supported")
        if min(majors["pg_dump"], majors["pg_restore"]) < max(s_major, t_major):
            raise Fail(f"client tools ({majors['pg_dump']}) must be at least the server major ({max(s_major, t_major)})")
        if t_major != s_major:
            say(f"   warning: major versions differ ({s_major} -> {t_major}); matching majors (18) are preferred")
        sid, tid = instance_id(src), instance_id(adm)
        same_endpoint = (cfg.source.host, cfg.source.port) == (cfg.target_admin.host, cfg.target_admin.port)
        if (sid and sid == tid) or (not sid and same_endpoint):
            raise Fail("source and target are the same PostgreSQL instance; TARGET_ADMIN_URL must point at the local server")
        if not src.execute("SELECT has_table_privilege('questions', 'SELECT') AND has_table_privilege('responses', 'SELECT') "
                           "AND has_table_privilege('schema_migrations', 'SELECT')").fetchone()[0]:  # type: ignore[index]
            raise Fail("the source user cannot read every application table")
        priv = adm.execute("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()
        if not priv or not priv[0]:
            raise Fail("the target admin user needs CREATEDB (or superuser)")
        if not adm.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [cfg.target_owner]).fetchone():
            raise Fail(f"target owner role {cfg.target_owner} does not exist. Create it first, e.g. "
                       f"CREATE ROLE {cfg.target_owner} LOGIN PASSWORD '...'; (pg_dump does not include roles)")
        if not adm.execute("SELECT pg_has_role(current_user, %s, 'MEMBER') OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)",
                           [cfg.target_owner]).fetchone()[0]:  # type: ignore[index]
            raise Fail(f"the target admin user must be a member of {cfg.target_owner} (GRANT {cfg.target_owner} TO <admin>)")
        if adm.execute("SELECT 1 FROM pg_database WHERE datname = %s", [cfg.target_db]).fetchone():
            raise Fail(f"target database {cfg.target_db} already exists; choose a new TARGET_DATABASE (nothing is overwritten)")
        say("   ok")
        return {"tools": majors, "source_major": s_major, "target_major": t_major}




def fingerprint(c: psycopg.Connection[Any]) -> dict[str, Any]:
    """Row counts, ordered content checksums and schema objects. Timestamps are compared as instants."""
    c.execute("SET timezone = 'UTC'")
    out: dict[str, Any] = {"tables": {}}
    for table, key in TABLES.items():
        row = c.execute(sql.SQL("SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY {k}), '')) FROM {t} AS t")
                        .format(t=sql.Identifier(table), k=sql.Identifier(key))).fetchone()
        assert row
        out["tables"][table] = {"rows": row[0], "md5": row[1]}
    out["migrations"] = [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
    out["sequences"] = {r[0]: {"next": r[1]} for r in c.execute(
        """SELECT sequencename, CASE WHEN last_value IS NULL THEN start_value ELSE last_value + increment_by END
           FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1""").fetchall()}
    out["identity"] = sorted(r[0] for r in c.execute(
        "SELECT table_name || '.' || column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND is_identity = 'YES' AND identity_generation = 'ALWAYS'").fetchall())
    out["constraints"] = sorted(f"{r[0]}:{r[1]}:{r[2]}" for r in c.execute(
        """SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint
           WHERE connamespace = 'public'::regnamespace AND contype <> 'n'""").fetchall())
    out["indexes"] = sorted(r[0] for r in c.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'").fetchall())
    out["triggers"] = sorted(f"{r[0]}:{r[1]}:{r[2]}" for r in c.execute(
        "SELECT tgrelid::regclass::text, tgname, tgenabled FROM pg_trigger "
        "WHERE NOT tgisinternal AND tgrelid::regclass::text IN ('questions', 'responses')").fetchall())
    out["functions"] = sorted(f"{r[0]}:{r[1]}:{r[2]}" for r in c.execute(
        "SELECT proname, prosecdef, md5(prosrc) FROM pg_proc WHERE pronamespace = 'public'::regnamespace").fetchall())
    out["samples"] = [list(r) for r in c.execute(
        """SELECT q.question_id, q.question_date::text, q.question::text, to_char(q.created_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
           FROM questions q ORDER BY q.question_date DESC, q.created_at DESC, q.question_id DESC LIMIT 5""").fetchall()]
    out["response_samples"] = [list(r) for r in c.execute(
        """SELECT response_id, question_id, user_id, user_name, question, correct_answer, response_answer, is_correct,
                  to_char(responded_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') FROM responses ORDER BY response_id DESC LIMIT 5""").fetchall()]
    c.execute("SET timezone = 'Asia/Kolkata'")
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()




def backup(cfg: Config, out_dir: Path | None = None) -> Path:
    say("== backup")
    pg_dump = tool(cfg, "pg_dump")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = out_dir or BACKUPS / f"{cfg.source.dbname}-{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    dump = folder / f"{cfg.source.dbname}.dump"
    with cfg.source.connect() as c:
        before = fingerprint(c)
        version = c.execute("SHOW server_version").fetchone()[0]  # type: ignore[index]
    with dump.open("wb") as f:
        proc = subprocess.run([pg_dump, *cfg.source.tool_args(), "--format=custom", "--no-password", "--serializable-deferrable"],  # noqa: S603
                              stdout=f, stderr=subprocess.PIPE, env=cfg.source.tool_env(), check=False)
    if proc.returncode != 0:
        raise Fail(f"pg_dump failed: {proc.stderr.decode(errors='replace').strip()[-500:]}")
    dump.chmod(0o600)
    with cfg.source.connect() as c:
        after = fingerprint(c)
    if before["tables"] != after["tables"]:
        raise Fail("the source changed while the backup ran; stop application writers and run the backup again")
    digest = sha256(dump)
    (folder / f"{dump.name}.sha256").write_text(f"{digest}  {dump.name}\n")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": {"host": cfg.source.host, "port": cfg.source.port, "database": cfg.source.dbname, "user": cfg.source.user,
                   "server_version": version},
        "pg_dump_major": tool_major(pg_dump),
        "file": dump.name,
        "bytes": dump.stat().st_size,
        "sha256": digest,
        "fingerprint": after,
        "note": "Roles and grants are cluster-wide and not included; the target owner role must exist before restore.",
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    say(f"   wrote {folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder} ({manifest['bytes']} bytes, sha256 {digest[:12]}...)")
    for t, v in after["tables"].items():
        say(f"   {t}: {v['rows']} rows")
    return folder




def restore(cfg: Config, folder: Path) -> None:
    say("== restore")
    pg_restore = tool(cfg, "pg_restore")
    manifest = json.loads((folder / "manifest.json").read_text())
    dump = folder / manifest["file"]
    if sha256(dump) != manifest["sha256"]:
        raise Fail(f"checksum mismatch for {dump.name}; the backup is damaged or was modified")
    with cfg.target_admin.connect() as adm:
        if adm.execute("SELECT 1 FROM pg_database WHERE datname = %s", [cfg.target_db]).fetchone():
            raise Fail(f"target database {cfg.target_db} already exists; nothing was changed")
        adm.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'")
                    .format(sql.Identifier(cfg.target_db), sql.Identifier(cfg.target_owner)))
        adm.execute(sql.SQL("ALTER DATABASE {} SET timezone TO 'Asia/Kolkata'").format(sql.Identifier(cfg.target_db)))
    say(f"   created database {cfg.target_db} owned by {cfg.target_owner} (timezone Asia/Kolkata)")
    with dump.open("rb") as f:
        proc = subprocess.run(  # noqa: S603
            [pg_restore, *cfg.target.tool_args(), "--no-owner", "--no-acl", f"--role={cfg.target_owner}", "--exit-on-error",
             "--single-transaction", "--no-password"],
            stdin=f, capture_output=True, env=cfg.target_admin.tool_env(), check=False)
    if proc.returncode != 0:
        raise Fail(f"pg_restore failed and was rolled back; database {cfg.target_db} exists but is EMPTY and incomplete. "
                   f"Inspect, then drop it (DROP DATABASE {cfg.target_db}) before retrying. "
                   f"Detail: {proc.stderr.decode(errors='replace').strip()[-500:]}")
    with cfg.target.connect() as c:
        c.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(cfg.target_db)))
        c.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(sql.Identifier(cfg.target_db), sql.Identifier(cfg.target_owner)))
    say(f"   restored {dump.name} into {cfg.target}")




def compare(src: dict[str, Any], dst: dict[str, Any]) -> list[str]:
    problems = []
    for key in src:
        if src[key] != dst.get(key):
            problems.append(key)
    return problems


def smoke(url: str) -> None:
    """Read-only checks against the target through the application's own queries (no sign-in needed)."""
    from . import repo
    from .config import REQUIRED_MIGRATION
    from .db import create_pool

    pool = create_pool(url, 1)
    pool.open(wait=True, timeout=10)
    try:
        with pool.connection() as c:
            if not repo.migration_applied(c, REQUIRED_MIGRATION):
                raise Fail(f"migration {REQUIRED_MIGRATION} is not applied on the target")
            questions, nq = repo.list_questions(c, None, None, repo.Paging(1, 5))
            responses, nr = repo.list_responses(c, {"from": None, "to": None, "qid": None, "uid": None, "ok": None}, repo.Paging(1, 5))
            today = repo.get_today_question(c, "")
            c.rollback()
    finally:
        pool.close()
    stamps = [r["created_at"] for r in questions] + [r["responded_at"] for r in responses]
    if not all(s.endswith("+05:30") for s in stamps):
        raise Fail("timestamps are not serialized in Delhi time")
    say(f"   read-only checks: {nq} questions, {nr} responses, today {'scheduled' if today else 'not scheduled'}")


def verify(cfg: Config) -> None:
    say("== verify")
    with cfg.source.connect() as s, cfg.target.connect() as t:
        a, b = fingerprint(s), fingerprint(t)
        tz = t.execute("SELECT setting FROM pg_settings WHERE name = 'TimeZone'").fetchone()
        db_tz = t.execute("""SELECT coalesce((SELECT split_part(cfg, '=', 2) FROM pg_db_role_setting r, unnest(r.setconfig) cfg
                                 WHERE r.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
                                 AND cfg ILIKE 'timezone=%'), '')""").fetchone()
    problems = compare(a, b)
    for t_name, v in a["tables"].items():
        say(f"   {t_name}: source {v['rows']} rows, target {b['tables'].get(t_name, {}).get('rows')} rows, "
            f"content {'match' if v == b['tables'].get(t_name) else 'DIFFERENT'}")
    say(f"   next IDs: {', '.join(f'{k} {v['next']}' for k, v in b['sequences'].items())} (read without consuming values)")
    say(f"   target session timezone {tz[0] if tz else '?'}, database default {db_tz[0] if db_tz else '?'}")
    if db_tz and db_tz[0] != "Asia/Kolkata":
        problems.append("database timezone")
    if problems:
        raise Fail("source and target differ in: " + ", ".join(problems) + ". Do not cut over; the source is unchanged.")
    say("   schema, migrations, constraints, indexes, triggers, functions, sequences and data match")
    smoke(cfg.target.url())
    say("   ok")


CUTOVER = """
Next steps (nothing has been switched):
  1. Keep application writers stopped (make backend / make dev not running).
  2. Point the application at the local database by editing DATABASE_URL in .env, e.g.
       DATABASE_URL=postgresql://{owner}:<password>@{host}:{port}/{db}
  3. Start the backend and check /health/ready, then resume use.
Rollback before any new local writes: restore the previous DATABASE_URL (Docker, still untouched).
After new writes reach the local database, switching back requires reconciling those writes first.
The Docker container and volume were not stopped or modified.
"""


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        cfg = config()
        if cmd == "preflight":
            preflight(cfg)
        elif cmd == "backup":
            backup(cfg)
        elif cmd == "restore":
            if len(sys.argv) < 3:
                raise Fail("usage: pgmove restore <backup-dir>")
            restore(cfg, Path(sys.argv[2]))
        elif cmd == "verify":
            verify(cfg)
        elif cmd == "migrate":
            preflight(cfg)
            folder = backup(cfg)
            restore(cfg, folder)
            verify(cfg)
            say(CUTOVER.format(owner=cfg.target_owner, host=cfg.target_admin.host, port=cfg.target_admin.port, db=cfg.target_db))
        else:
            raise SystemExit(__doc__)
    except Fail as err:
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
