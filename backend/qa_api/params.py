"""Query-string and path parsing with stable VALIDATION_ERROR field names."""

import re
from datetime import date

from starlette.requests import Request

from .errors import FieldError, bad_request
from .repo import Paging

MAX_INT = 2_147_483_647
MAX_PAGE_SIZE = 100
MAX_GAP_RANGE_DAYS = 366
ID = re.compile(r"^[1-9][0-9]{0,9}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAGE = {"page": re.compile(r"^[1-9][0-9]{0,5}$"), "page_size": re.compile(r"^[1-9][0-9]{0,2}$")}


def parse_id(value: str, name: str = "questionId") -> int:
    if not ID.match(value) or int(value) > MAX_INT:
        raise bad_request("Invalid identifier.", [FieldError(name, "must be a positive integer")])
    return int(value)


def is_real_date(value: str) -> bool:
    if not DATE.match(value):
        return False
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return False
    return 2000 <= d.year <= 2999


def parse_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not is_real_date(value):
        raise bad_request("Invalid date.", [FieldError(name, "must be a real calendar date (YYYY-MM-DD)")])
    return value


def query(request: Request, allowed: set[str]) -> dict[str, str]:
    """Returns the query parameters, rejecting unknown or repeated ones."""
    params = request.query_params
    unknown = [k for k in params if k not in allowed]
    if unknown:
        raise bad_request("The request is invalid.", [FieldError(k, "unknown field") for k in unknown])
    repeated = [k for k in params if len(params.getlist(k)) > 1]
    if repeated:
        raise bad_request("The request is invalid.", [FieldError(k, "must be given once") for k in repeated])
    return dict(params)


def parse_paging(q: dict[str, str]) -> Paging:
    for k, pattern in PAGE.items():
        if k in q and not pattern.match(q[k]):
            raise bad_request("Invalid pagination.", [FieldError(k, "must be a positive integer")])
    paging = Paging(int(q.get("page", "1")), int(q.get("page_size", "20")))
    if paging.page_size > MAX_PAGE_SIZE:
        raise bad_request("Invalid pagination.", [FieldError("page_size", f"must be at most {MAX_PAGE_SIZE}")])
    return paging


def parse_range(q: dict[str, str]) -> tuple[str | None, str | None]:
    date_from = parse_date(q.get("from"), "from")
    date_to = parse_date(q.get("to"), "to")
    if date_from and date_to and date_from > date_to:
        raise bad_request("Invalid date range.", [FieldError("to", "must not be before from")])
    return date_from, date_to


def days_between(date_from: str, date_to: str) -> int:
    return (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days
