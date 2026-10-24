"""Applies db/migrations/NNNN_name.sql in order, each in one transaction together with its
schema_migrations row (same bookkeeping and SHA-256 checksums as the earlier shell runner,
so existing databases keep their history). Runs locally; no psql or container needed.

    python -m qa_api.migrate              apply all pending migrations to DATABASE_URL
    python -m qa_api.migrate --to 0002    apply pending migrations up to and including 0002
    python -m qa_api.migrate --status     list applied and pending migrations
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")


def _migrations(directory: Path) -> list[Path]:
    files = sorted(directory.glob("*.sql"))
    for f in files:
        if not NAME.match(f.name):
            raise SystemExit(f"error: invalid migration filename: {f.name}")
    return files


def run(url: str, directory: Path = DEFAULT_DIR, target: str | None = None, status: bool = False) -> int:
    applied = 0
    with psycopg.connect(url, autocommit=True, options="-c timezone=Asia/Kolkata") as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version    text        PRIMARY KEY,
            filename   text        NOT NULL,
            checksum   text        NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()}
        for f in _migrations(directory):
            version = f.name[:4]
            if target and version > target:
                break
            data = f.read_bytes()
            checksum = hashlib.sha256(data).hexdigest()
            if version in done:
                if done[version] != checksum:
                    raise SystemExit(f"error: applied migration {f.name} has been modified (checksum mismatch). Add a new migration instead.")
                print(f"{'applied' if status else 'skip   '} {f.name}{'' if status else ' (already applied)'}")
                continue
            if status:
                print(f"pending {f.name}")
                continue
            with conn.transaction():
                conn.execute(data.decode())
                conn.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s)", [version, f.name, checksum]
                )
            print(f"applied {f.name}")
            applied += 1
    if not status:
        print(f"migrations applied: {applied}")
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", dest="target")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("error: DATABASE_URL is not set (see .env.example)")
    try:
        run(url, args.dir, args.target, args.status)
    except psycopg.Error as err:
        diag = err.diag.message_primary or str(err)
        print(f"error: migration failed and was rolled back: {diag}", file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
