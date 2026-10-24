"""Read-only database queries that need no psql client. Uses DATABASE_URL.

    python -m qa_api.dbtool query <name> [var=value]   run a read-only query from db/queries
"""

import os
import sys
from pathlib import Path

import psycopg

from .sqlscript import run_script

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("error: DATABASE_URL is not set (see .env.example)")
    if len(sys.argv) < 2 or sys.argv[1] != "query":
        raise SystemExit(__doc__)
    with psycopg.connect(url, autocommit=True, options="-c timezone=Asia/Kolkata") as conn:
        if len(sys.argv) < 3:
            names = sorted(p.stem for p in (ROOT / "db" / "queries").glob("*.sql"))
            raise SystemExit("usage: dbtool query <name> [var=value ...]; queries: " + ", ".join(names))
        f = ROOT / "db" / "queries" / f"{sys.argv[2]}.sql"
        if not f.is_file():
            raise SystemExit(f"error: no query {f.name}")
        variables = {"day": "", "user": "", "from": "", "to": ""}
        variables.update(dict(a.split("=", 1) for a in sys.argv[3:]))
        conn.execute("SET default_transaction_read_only = on")
        for row in run_script(conn, f.read_text(), variables):
            print(" | ".join("" if v is None else str(v) for v in row))


if __name__ == "__main__":
    main()
