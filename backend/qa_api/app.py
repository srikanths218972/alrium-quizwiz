"""FastAPI application factory. Run with: uvicorn qa_api.app:create_app --factory"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import repo
from . import schemas as s
from .auth import (
    KeyResolver,
    Principal,
    RateLimiter,
    Verifier,
    admin_key_principal,
    email_verifier,
    entra_verifier,
    issue_email_token,
    normalize_email,
    parse_login_email,
    require_admin,
    token_from_header,
)
from .config import BODY_LIMIT, BUSINESS_TIMEZONE, REQUIRED_MIGRATION, Settings
from .db import create_pool, to_delhi_iso
from .errors import AppError, FieldError, bad_request, from_db_error, not_found, unavailable
from .params import MAX_GAP_RANGE_DAYS, days_between, parse_date, parse_id, parse_paging, parse_range, query
from .questions import parse_question_payload

log = logging.getLogger("qa_api")
Pool = ConnectionPool[Connection[DictRow]]
M = TypeVar("M", bound=BaseModel)

ERRORS: dict[int | str, dict[str, Any]] = {code: {"model": s.ErrorBody} for code in (400, 401, 403, 404, 409, 503)}


def _envelope(request: Request, e: AppError) -> JSONResponse:
    body: dict[str, Any] = {"code": e.code, "message": e.message, "request_id": getattr(request.state, "request_id", "")}
    if e.fields:
        body["fields"] = [{"field": f.field, "message": f.message} for f in e.fields]
    return JSONResponse({"error": body}, status_code=e.status)


def _fields(err: ValidationError | RequestValidationError) -> list[FieldError]:
    out = []
    for item in err.errors():
        loc = [str(x) for x in item.get("loc", ()) if x not in ("body", "query", "path")]
        message = "unknown field" if item["type"] == "extra_forbidden" else "is required" if item["type"] == "missing" else item["msg"]
        out.append(FieldError(".".join(loc) or "request", message))
    return out


def create_app(settings: Settings | None = None, *, pool: Pool | None = None, entra_keys: KeyResolver | None = None) -> FastAPI:
    cfg = settings or Settings()
    db: Pool = pool or create_pool(cfg.DATABASE_URL, cfg.DB_POOL_MAX)
    verify: Verifier = entra_verifier(cfg, entra_keys) if cfg.mode == "entra" else email_verifier(cfg)
    is_open = cfg.mode == "open"
    login_limit = RateLimiter(cfg.EMAIL_LOGIN_RATE_PER_MINUTE)

    async def lifespan(_app: FastAPI) -> Any:
        db.open(wait=False)
        if is_open:
            log.warning("OPEN TEST MODE: no sign-in; every route, including admin, is open to anyone who can reach this API.")
        if cfg.mode == "email":
            log.warning("EMAIL SIGN-IN MODE: participant emails are self-declared and not verified. Admin routes %s.",
                        "require X-Admin-Key" if cfg.ADMIN_API_KEY else "are disabled (no ADMIN_API_KEY)")
        yield
        db.close()

    app = FastAPI(
        title=f"{cfg.APP_NAME} API",
        version="2.0.0",
        description=(
            "ålrium - QuizWiz REST API. Timestamps are ISO 8601 in Asia/Kolkata (+05:30). Dates are YYYY-MM-DD business dates. "
            'Question payloads are {"question", "options": {"A".."D"}, "answer"} where answer is the actual option text. '
            'Errors use {"error": {"code", "message", "request_id", "fields?"}}.'
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    if cfg.origins:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.origins, allow_methods=["GET", "POST"],
                           allow_headers=["authorization", "content-type"], max_age=600)

    @app.middleware("http")
    async def common(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request.state.request_id = str(uuid.uuid4())
        length = request.headers.get("content-length")
        if request.method in ("POST", "PUT", "PATCH"):
            if length and length.isdigit() and int(length) > BODY_LIMIT:
                response: Response = _envelope(request, AppError(413, "PAYLOAD_TOO_LARGE", f"Request bodies are limited to {BODY_LIMIT} bytes."))
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        response.headers["x-content-type-options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["cache-control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, e: AppError) -> JSONResponse:
        return _envelope(request, e)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, e: RequestValidationError) -> JSONResponse:
        return _envelope(request, bad_request("The request is invalid.", _fields(e)))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, e: StarletteHTTPException) -> JSONResponse:
        if e.status_code == 405:
            return _envelope(request, AppError(405, "METHOD_NOT_ALLOWED", "Method not allowed."))
        return _envelope(request, AppError(404, "NOT_FOUND", "Route not found."))

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(PoolTimeout)
    @app.exception_handler(Exception)
    async def unexpected(request: Request, e: Exception) -> JSONResponse:
        if isinstance(e, PoolTimeout):
            mapped: AppError | None = unavailable()
        else:
            mapped = from_db_error(e)
        if mapped is None or mapped.status >= 500:
            log.error("request failed (%s): %s", request.state.request_id, type(e).__name__)
        return _envelope(request, mapped or AppError(500, "INTERNAL_ERROR", "An unexpected error occurred."))


    def conn() -> Any:
        try:
            with db.connection() as c:
                yield c
        except PoolTimeout as err:
            raise unavailable() from err

    def no_identity_headers(request: Request) -> None:
        """Identity never comes from headers. Clients still sending them are told so, instead of silently ignored."""
        sent = sorted(h for h in request.headers if h.startswith("x-user-"))
        if sent:
            raise bad_request("Identity headers are not accepted; identity comes only from the bearer token.",
                              [FieldError(h, "not accepted") for h in sent])

    def employee(request: Request) -> Principal:
        if is_open:
            raise AppError(404, "NOT_AVAILABLE_IN_OPEN_MODE", "This server runs in open test mode without sign-in, so there is no "
                           "personal view. Post response takes your email in its body.")
        who = verify(token_from_header(request.headers.get("authorization")))
        no_identity_headers(request)
        return who

    def viewer(request: Request) -> Principal | None:
        """Participant for read routes; in open mode nobody is signed in."""
        if is_open:
            no_identity_headers(request)
            return None
        return employee(request)

    def admin(request: Request) -> Principal:
        if is_open:
            no_identity_headers(request)
            return Principal("open-mode", "Open test mode", ["admin"], "open-mode", "open")
        if cfg.mode == "email":
            key = request.headers.get("x-admin-key")
            if key is None and request.headers.get("authorization"):
                verify(token_from_header(request.headers.get("authorization")))
                raise AppError(403, "FORBIDDEN", "Administrator access requires the X-Admin-Key header.")
            who = admin_key_principal(cfg, key)
        else:
            if request.headers.get("x-admin-key") is not None:
                raise bad_request("X-Admin-Key is not used in Microsoft sign-in mode; use an administrator access token.",
                                  [FieldError("X-Admin-Key", "not accepted")])
            who = employee(request)
        no_identity_headers(request)
        return require_admin(who)

    async def read_json(request: Request) -> Any:
        if "application/json" not in request.headers.get("content-type", ""):
            raise AppError(415, "UNSUPPORTED_MEDIA_TYPE", "Use Content-Type: application/json.")
        raw = await request.body()
        if len(raw) > BODY_LIMIT:
            raise AppError(413, "PAYLOAD_TOO_LARGE", f"Request bodies are limited to {BODY_LIMIT} bytes.")
        try:
            return json.loads(raw)
        except ValueError as err:
            raise bad_request("The request body is not valid JSON.") from err

    def validated(model: type[M], data: Any) -> M:
        try:
            return model.model_validate(data)
        except ValidationError as err:
            raise bad_request("The request is invalid.", _fields(err)) from err

    def body_doc(model: type[BaseModel]) -> dict[str, Any]:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}

    bearer: list[dict[str, list[str]]] = [{"bearerAuth": []}]


    @app.get("/health/live", tags=["Health"], summary="Process liveness (no database call)", response_model=s.Health,
             response_model_exclude_none=True)
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"], summary="Database reachable and required migration applied", response_model=s.Health,
             responses={503: {"model": s.Health}})
    def ready() -> Any:
        try:
            with db.connection(timeout=3) as c:
                if repo.migration_applied(c, REQUIRED_MIGRATION):
                    return {"status": "ready", "migration": REQUIRED_MIGRATION}
                log.warning("required migration %s not applied", REQUIRED_MIGRATION)
        except Exception as err:  # noqa: BLE001 - readiness reports, never raises
            log.warning("readiness check failed: %s", type(err).__name__)
        return JSONResponse({"status": "unavailable"}, status_code=503)


    @app.get("/api/v1/me", tags=["Employee"], summary="Verified identity, roles and the current business date",
             response_model=s.Data[s.Me], responses=ERRORS, openapi_extra={"security": bearer})
    def me(who: Principal = Depends(employee), c: Any = Depends(conn)) -> Any:
        return s.as_data({
            "user_id": who.user_id, "user_name": who.user_name, "roles": who.roles, "is_admin": who.is_admin,
            "auth_mode": cfg.mode, "email_verified": who.email_verified, "timezone": BUSINESS_TIMEZONE, "business_date": repo.business_today(c),
            "server_time": to_delhi_iso(datetime.now(UTC)),
        })

    @app.get("/api/v1/questions/today", tags=["Employee"], summary="Today's question (Asia/Kolkata) and the caller's answered state",
             description="Serves the newest row for today's Delhi date (created_at DESC, question_id DESC). question holds the text "
                         "and options only, never the answer. After answering, my_response holds the saved text values and score.",
             response_model=s.Data[s.TodayQuestion], responses=ERRORS, openapi_extra={"security": bearer})
    def today(who: Principal | None = Depends(viewer), c: Any = Depends(conn)) -> Any:
        q = repo.get_today_question(c, who.user_id if who else "")
        if q is None:
            raise not_found("QUESTION_NOT_SCHEDULED", "No question is scheduled for today.")
        return s.as_data(q)

    @app.get("/api/v1/questions/{questionId}/my-response", tags=["Employee"], summary="The caller's saved result for a question",
             response_model=s.Data[s.MyResult], responses=ERRORS, openapi_extra={"security": bearer})
    def my_response(questionId: str, who: Principal = Depends(employee), c: Any = Depends(conn)) -> Any:  # noqa: N803
        mine = repo.get_my_result(c, parse_id(questionId), who.user_id)
        if mine is None:
            raise not_found("RESPONSE_NOT_FOUND", "No saved response for this question.")
        return s.as_data(mine)

    @app.post("/api/v1/questions/{questionId}/responses", tags=["Employee"], status_code=201, summary="Answer today's question (once)",
              description="Body contains only response_answer: the selected option's actual text. questionId is the numeric ID "
                          "returned by Get question of the day; it is required. Identity comes from the token; the database copies the "
                          "question text and actual answer from this exact question ID, scores and timestamps it. Incorrect answers are "
                          "still saved (201). A second answer returns 409 ALREADY_ANSWERED. Policy: a question replaced by a newer row "
                          "for today can still be answered by its own ID and is scored against that version.",
              response_model=s.Data[s.MyResult], responses=ERRORS, openapi_extra={"security": bearer, **body_doc(s.SubmitBody)})
    async def submit(questionId: str, request: Request, signed_in: Principal | None = Depends(viewer)) -> Any:  # noqa: N803
        qid = parse_id(questionId)
        raw = await read_json(request)
        if signed_in is None:
            open_body = validated(s.OpenSubmitBody, raw)
            email = parse_login_email(open_body.email, cfg.allowed_domains)
            who = Principal(email, email, ["employee"], email, "open", email_verified=False)
            body = s.SubmitBody(response_answer=open_body.response_answer)
        else:
            who = signed_in
            body = validated(s.SubmitBody, raw)
        if not body.response_answer.strip():
            raise bad_request("The request is invalid.", [FieldError("response_answer", "must not be blank")])
        return s.as_data(await _run(lambda c: repo.submit_response(c, qid, who, body.response_answer)))

    @app.api_route("/api/v1/questions//responses", methods=["POST"], include_in_schema=False)
    @app.api_route("/api/v1/questions//my-response", methods=["GET"], include_in_schema=False)
    def missing_question_id() -> Any:
        raise bad_request("The question ID is missing from the URL. Send Get question of the day or Create question first; "
                          "it fills question_id.", [FieldError("questionId", "is required")])

    @app.get("/api/v1/me/responses", tags=["Employee"], summary="The caller's answer history",
             response_model=s.Page[s.HistoryItem], responses=ERRORS, openapi_extra={"security": bearer})
    def my_history(request: Request, who: Principal = Depends(employee), c: Any = Depends(conn)) -> Any:
        q = query(request, {"page", "page_size", "from", "to"})
        paging = parse_paging(q)
        date_from, date_to = parse_range(q)
        items, total = repo.list_my_responses(c, who.user_id, date_from, date_to, paging)
        return {"data": items, "meta": {"page": paging.page, "page_size": paging.page_size, "total": total}}

    async def _run(fn: Callable[[Any], Any]) -> Any:
        from starlette.concurrency import run_in_threadpool

        def work() -> Any:
            try:
                with db.connection() as c:
                    return fn(c)
            except PoolTimeout as err:
                raise unavailable() from err

        return await run_in_threadpool(work)


    @app.get("/api/v1/admin/questions", tags=["Admin"], summary="All stored questions (every version), including answers",
             description="Ordered by question_date, then created_at and question_id. is_latest_for_date marks the row served for "
                         "that date. Optional from/to date filters; an unfiltered request lists everything. Admin only.",
             response_model=s.Page[s.AdminQuestion], responses=ERRORS, openapi_extra={"security": bearer})
    def list_questions(request: Request, _: Principal = Depends(admin), c: Any = Depends(conn)) -> Any:
        q = query(request, {"page", "page_size", "from", "to"})
        paging = parse_paging(q)
        date_from, date_to = parse_range(q)
        items, total = repo.list_questions(c, date_from, date_to, paging)
        return {"data": items, "meta": {"page": paging.page, "page_size": paging.page_size, "total": total}}

    @app.post("/api/v1/admin/questions", tags=["Admin"], status_code=201, summary="Insert a question for a date (always a new row)",
              description="Supporting operation. Inserts a new question ID even when the date already has questions; the newest "
                          "row becomes the one served for that date. There is no update or upsert. answer must equal one option's "
                          "text; option values must be distinct.",
              response_model=s.Data[s.AdminQuestion], responses=ERRORS, openapi_extra={"security": bearer, **body_doc(s.QuestionCreate)})
    async def create_question(request: Request, _: Principal = Depends(admin)) -> Any:
        raw = await read_json(request)
        body = validated(s.QuestionCreate, raw)
        question_date = parse_date(body.question_date, "question_date")
        payload, errors = parse_question_payload(raw["question"])
        if payload is None:
            raise bad_request("The question is invalid.", errors)
        return s.as_data(await _run(lambda c: repo.create_question(c, str(question_date), payload)))

    @app.get("/api/v1/admin/questions/{questionId}", tags=["Admin"], summary="One stored question with its response count",
             response_model=s.Data[s.AdminQuestion], responses=ERRORS, openapi_extra={"security": bearer})
    def get_question(questionId: str, _: Principal = Depends(admin), c: Any = Depends(conn)) -> Any:  # noqa: N803
        q = repo.get_question(c, parse_id(questionId))
        if q is None:
            raise not_found("QUESTION_NOT_FOUND", "Question not found.")
        return s.as_data(q)

    @app.api_route("/api/v1/admin/questions/{questionId}", methods=["PATCH", "PUT", "DELETE"], include_in_schema=False)
    def insert_only(questionId: str) -> Any:  # noqa: N803, ARG001
        e = AppError(405, "QUESTIONS_INSERT_ONLY", "Questions cannot be edited or deleted. Insert a new question for the date instead.")
        raise e

    @app.get("/api/v1/admin/questions/{questionId}/stats", tags=["Admin"], summary="Response counts, option distribution and accuracy",
             description="Stored answers are option text; the distribution maps them back to option labels.",
             response_model=s.Data[s.Stats], responses=ERRORS, openapi_extra={"security": bearer})
    def stats(questionId: str, _: Principal = Depends(admin), c: Any = Depends(conn)) -> Any:  # noqa: N803
        result = repo.question_stats(c, parse_id(questionId))
        if result is None:
            raise not_found("QUESTION_NOT_FOUND", "Question not found.")
        return s.as_data(result)

    @app.get("/api/v1/admin/responses", tags=["Admin"], summary="Response audit list",
             description="Filters: question_id, user_id (email), from/to (question date), is_correct.",
             response_model=s.Page[s.AuditItem], responses=ERRORS, openapi_extra={"security": bearer})
    def responses(request: Request, _: Principal = Depends(admin), c: Any = Depends(conn)) -> Any:
        q = query(request, {"page", "page_size", "from", "to", "question_id", "user_id", "is_correct"})
        paging = parse_paging(q)
        date_from, date_to = parse_range(q)
        if "is_correct" in q and q["is_correct"] not in ("true", "false"):
            raise bad_request("The request is invalid.", [FieldError("is_correct", "must be true or false")])
        user = q.get("user_id")
        if user is not None and not (1 <= len(user) <= 320):
            raise bad_request("The request is invalid.", [FieldError("user_id", "must be 1 to 320 characters")])
        f = {
            "from": date_from, "to": date_to,
            "qid": parse_id(q["question_id"], "question_id") if "question_id" in q else None,
            "uid": normalize_email(user) if user else None,
            "ok": None if "is_correct" not in q else q["is_correct"] == "true",
        }
        items, total = repo.list_responses(c, f, paging)
        return {"data": items, "meta": {"page": paging.page, "page_size": paging.page_size, "total": total}}

    @app.get("/api/v1/admin/users", tags=["Admin"], summary="All registered users",
             description="Every row of the users table, which is never deleted from: user_id, latest user_name, counts and "
                         "first/last answer times (null if none), ordered by user_id. In email mode these identities are self-declared. Admin only.",
             response_model=s.Page[s.UserItem], responses=ERRORS, openapi_extra={"security": bearer})
    def users(request: Request, _: Principal = Depends(admin), c: Any = Depends(conn)) -> Any:
        paging = parse_paging(query(request, {"page", "page_size"}))
        items, total = repo.list_users(c, paging)
        return {"data": items, "meta": {"page": paging.page, "page_size": paging.page_size, "total": total}}

    @app.get("/api/v1/admin/answers/by-date", tags=["Admin"], summary="Who answered on a date, what they chose, and who did not",
             description="date is required (question date, YYYY-MM-DD). answered maps each response to its user; not_answered lists "
                         "users registered by that date who did not answer.",
             response_model=s.Data[s.DayAnswers], responses=ERRORS, openapi_extra={"security": bearer})
    def answers_by_date(request: Request, _: Principal = Depends(admin), c: Any = Depends(conn)) -> Any:
        q = query(request, {"date"})
        if "date" not in q:
            raise bad_request("The request is invalid.", [FieldError("date", "is required")])
        day = parse_date(q["date"], "date")
        assert day
        return s.as_data(repo.day_answers(c, day))

    @app.get("/api/v1/admin/schedule/gaps", tags=["Admin"], summary="Dates in a range with no question",
             description=f"from and to are required; the range may span at most {MAX_GAP_RANGE_DAYS} days.",
             response_model=s.Data[s.Gaps], responses=ERRORS, openapi_extra={"security": bearer})
    def gaps(request: Request, _: Principal = Depends(admin), c: Any = Depends(conn)) -> Any:
        q = query(request, {"from", "to"})
        missing = [FieldError(k, "is required") for k in ("from", "to") if k not in q]
        if missing:
            raise bad_request("The request is invalid.", missing)
        date_from, date_to = parse_range(q)
        assert date_from and date_to
        if days_between(date_from, date_to) > MAX_GAP_RANGE_DAYS:
            raise bad_request("Date range too large.", [FieldError("to", f"range must be at most {MAX_GAP_RANGE_DAYS} days")])
        return s.as_data({"from": date_from, "to": date_to, "missing_dates": repo.schedule_gaps(c, date_from, date_to)})


    @app.get("/api/v1/auth/mode", tags=["Sign-in"], summary="Which sign-in the server uses (public; no secrets)",
             response_model=s.Data[s.AuthMode])
    def auth_mode() -> Any:
        return s.as_data({"mode": cfg.mode, "email_verified": cfg.mode == "entra",
                          "allowed_domains": cfg.allowed_domains if cfg.mode in ("email", "open") else []})


    if cfg.mode == "email":

        @app.post("/api/v1/auth/email-login", tags=["Sign-in"],
                  summary="Continue with an email (email mode only). The email is self-declared and NOT verified",
                  description="Returns a short-lived participant token signed by this server. Participant role only; never admin. "
                              "The email is trimmed and lowercased, nothing else. Rate limited per client address and per email "
                              f"({cfg.EMAIL_LOGIN_RATE_PER_MINUTE} per minute each); excess requests get 429. "
                              "404 USER_NOT_FOUND if the email has not signed up.",
                  response_model=s.Data[s.Token], responses={400: {"model": s.ErrorBody}, 404: {"model": s.ErrorBody},
                                                             429: {"model": s.ErrorBody}},
                  openapi_extra=body_doc(s.EmailLogin))
        async def email_login(request: Request, c: Any = Depends(conn)) -> Any:
            email = await login_email(request)
            if not repo.user_exists(c, email):
                raise AppError(404, "USER_NOT_FOUND", "No account uses this email. Sign up first.")
            return session(email)

        @app.post("/api/v1/auth/email-signup", tags=["Sign-in"], status_code=201,
                  summary="Register an email and sign in (email mode only). The email is self-declared and NOT verified",
                  description="Adds the email to users and returns the same token as email-login. 409 if the email is already "
                              "registered. Shares the email-login rate limits.",
                  response_model=s.Data[s.Token], responses={400: {"model": s.ErrorBody}, 409: {"model": s.ErrorBody},
                                                             429: {"model": s.ErrorBody}},
                  openapi_extra=body_doc(s.EmailLogin))
        async def email_signup(request: Request, c: Any = Depends(conn)) -> Any:
            email = await login_email(request)
            if not repo.create_user(c, email, email):
                raise AppError(409, "USER_EXISTS", "An account already uses this email. Sign in instead.")
            return session(email)

        async def login_email(request: Request) -> str:
            client = request.client.host if request.client else "unknown"
            if not login_limit.allow(f"ip:{client}"):
                raise AppError(429, "TOO_MANY_REQUESTS", "Too many sign-in attempts. Wait a minute and try again.")
            body = validated(s.EmailLogin, await read_json(request))
            email = parse_login_email(body.email, cfg.allowed_domains)
            if not login_limit.allow(f"email:{email}"):
                raise AppError(429, "TOO_MANY_REQUESTS", "Too many sign-in attempts. Wait a minute and try again.")
            return email

        def session(email: str) -> Any:
            token, ttl = issue_email_token(cfg, email)
            return s.as_data({"access_token": token, "token_type": "Bearer", "expires_in": ttl, "user_id": email,
                              "user_name": email, "authentication_method": "email", "email_verified": False})


    def openapi_doc() -> dict[str, Any]:
        if app.openapi_schema is None:
            from fastapi.openapi.utils import get_openapi

            doc = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            components = doc.setdefault("components", {})
            components["securitySchemes"] = {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
                                              "adminKey": {"type": "apiKey", "in": "header", "name": "X-Admin-Key"}}
            for path in doc.get("paths", {}).values():
                for op in path.values():
                    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
                    components.setdefault("schemas", {}).update(schema.pop("$defs", {}))
            app.openapi_schema = doc
        return app.openapi_schema

    if cfg.docs_public:
        @app.get("/openapi.json", include_in_schema=False)
        def openapi_public() -> Any:
            return openapi_doc()

        @app.get("/docs", include_in_schema=False)
        def docs() -> HTMLResponse:
            return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{cfg.APP_NAME} API")
    else:
        @app.get("/openapi.json", include_in_schema=False)
        def openapi_admin(_: Principal = Depends(admin)) -> Any:
            return openapi_doc()

    return app
