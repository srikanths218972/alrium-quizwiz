"""Connection pool. Every session uses the Asia/Kolkata timezone."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from .config import BUSINESS_TIMEZONE

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def create_pool(url: str, max_size: int = 10) -> ConnectionPool[Connection[DictRow]]:
    return ConnectionPool(
        url,
        min_size=0,
        max_size=max_size,
        timeout=3,
        open=False,
        kwargs={
            "row_factory": dict_row,
            "options": f"-c timezone={BUSINESS_TIMEZONE} -c statement_timeout=10000",
            "connect_timeout": 3,
            "autocommit": False,
        },
        connection_class=Connection[DictRow],
    )


@contextmanager
def transaction(pool: ConnectionPool[Connection[DictRow]]) -> Iterator[Connection[DictRow]]:
    """One connection, committed on success and rolled back on error."""
    with pool.connection() as conn:
        yield conn


def to_delhi_iso(value: datetime) -> str:
    """ISO 8601 in Delhi time with an explicit offset, e.g. 2026-10-23T13:20:01+05:30."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30")
