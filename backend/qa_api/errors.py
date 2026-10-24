"""Stable error envelope {"error": {code, message, request_id, fields?}} and database error mapping."""

from dataclasses import dataclass, field

import psycopg


@dataclass
class FieldError:
    field: str
    message: str


@dataclass
class AppError(Exception):
    status: int
    code: str
    message: str
    fields: list[FieldError] = field(default_factory=list)


def unauthorized(message: str = "A valid bearer token is required.") -> AppError:
    return AppError(401, "UNAUTHORIZED", message)


def forbidden(code: str, message: str) -> AppError:
    return AppError(403, code, message)


def not_found(code: str, message: str) -> AppError:
    return AppError(404, code, message)


def bad_request(message: str, fields: list[FieldError] | None = None) -> AppError:
    return AppError(400, "VALIDATION_ERROR", message, fields or [])


def unavailable() -> AppError:
    return AppError(503, "SERVICE_UNAVAILABLE", "The database is unavailable. Try again shortly.")


def from_db_error(err: Exception) -> AppError | None:
    """Translates database errors into domain errors. Returns None for unknown errors."""
    if isinstance(err, psycopg.OperationalError) and not isinstance(err, psycopg.errors.QueryCanceled):
        return unavailable()
    if not isinstance(err, psycopg.Error):
        return None
    state = err.sqlstate or ""
    if state == "23505":
        if err.diag.constraint_name == "responses_question_id_user_id_key":
            return AppError(409, "ALREADY_ANSWERED", "You have already answered this question.")
        return AppError(409, "CONFLICT", "The request conflicts with existing data.")
    if state == "23001":
        return AppError(409, "HISTORY_IMMUTABLE", "Questions and responses cannot be changed or deleted.")
    if state == "23503":
        return not_found("QUESTION_NOT_FOUND", "Question not found.")
    if state == "QA001":
        return forbidden("QUESTION_NOT_OPEN", "Only today's question can be answered.")
    if state == "QA002":
        return AppError(409, "QUESTION_INVALID", "This question cannot be answered because its stored content is invalid.")
    if state == "QA003":
        return bad_request(
            "The answer is not one of the options.",
            [FieldError("response_answer", "must equal one of the question's option values")],
        )
    if state in ("23514", "23502"):
        return bad_request("One or more fields are invalid.")
    if state in ("22007", "22008"):
        return bad_request("Invalid date.", [FieldError("question_date", "must be a real calendar date")])
    if state in ("40001", "40P01"):
        return AppError(503, "RETRY", "The request conflicted with another change. Try again.")
    if state.startswith("08") or state in ("57P01", "57P03"):
        return unavailable()
    return None
