import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from conftest import (
    ADMIN,
    ADMIN_KEY,
    API_CLIENT,
    EMAIL_ENV,
    ENTRA_ENV,
    OTHER_TENANT,
    POSTMAN_CLIENT,
    TENANT,
    EntraKit,
    auth,
    email_token,
    settings,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qa_api.app import create_app
from qa_api.auth import EMAIL_AUDIENCE, EMAIL_ISSUER, RateLimiter
from qa_api.config import Settings
from qa_api.db import create_pool

SECRET = EMAIL_ENV["EMAIL_TOKEN_SECRET"]


def cfg(**env: Any) -> Settings:
    return Settings(DATABASE_URL="postgresql://unused", **env)


class TestModeSelection:
    def test_no_entra_settings_selects_email(self) -> None:
        assert cfg(EMAIL_TOKEN_SECRET=SECRET).mode == "email"

    def test_complete_entra_settings_select_entra(self) -> None:
        assert cfg(**{k: v for k, v in ENTRA_ENV.items() if k != "AUTH_MODE"}).mode == "entra"

    @pytest.mark.parametrize("partial", [
        {"ENTRA_TENANT_ID": TENANT},
        {"ENTRA_TENANT_ID": TENANT, "ENTRA_API_AUDIENCE": API_CLIENT},
        {"ENTRA_API_AUDIENCE": "api://<api-client-id>", "ENTRA_ALLOWED_CLIENT_IDS": POSTMAN_CLIENT, "ENTRA_TENANT_ID": TENANT},
        {**{k: v for k, v in ENTRA_ENV.items() if k != "AUTH_MODE"}, "ENTRA_ALLOWED_CLIENT_IDS": "replace-with-client-id"},
    ])
    def test_partial_entra_settings_fail_clearly(self, partial: dict[str, str]) -> None:
        with pytest.raises(ValidationError, match="incomplete"):
            cfg(EMAIL_TOKEN_SECRET=SECRET, **partial)

    def test_explicit_mode_is_honored(self) -> None:
        assert cfg(AUTH_MODE="email", EMAIL_TOKEN_SECRET=SECRET, **{k: v for k, v in ENTRA_ENV.items() if k != "AUTH_MODE"}).mode == "email"
        with pytest.raises(ValidationError, match="AUTH_MODE=entra but Microsoft Entra is not configured"):
            cfg(AUTH_MODE="entra", EMAIL_TOKEN_SECRET=SECRET)

    @pytest.mark.parametrize("mode", ["none", "local-test", "demo", "anonymous"])
    def test_removed_modes_refused(self, mode: str) -> None:
        with pytest.raises(ValidationError, match="AUTH_MODE must be entra, email, open or empty"):
            cfg(AUTH_MODE=mode, EMAIL_TOKEN_SECRET=SECRET)

    @pytest.mark.parametrize("env,match", [
        ({}, "EMAIL_TOKEN_SECRET"),
        ({"EMAIL_TOKEN_SECRET": "short"}, "EMAIL_TOKEN_SECRET"),
        ({"EMAIL_TOKEN_SECRET": "replace-with-64-hex-characters-xxxxxxxxxxxxxxxx"}, "EMAIL_TOKEN_SECRET"),
        ({"EMAIL_TOKEN_SECRET": SECRET, "ADMIN_API_KEY": "short"}, "ADMIN_API_KEY"),
        ({"EMAIL_TOKEN_SECRET": SECRET, "ADMIN_API_KEY": SECRET}, "must differ"),
        ({"EMAIL_TOKEN_SECRET": SECRET, "EMAIL_ALLOWED_DOMAINS": "alrium.com,not a domain"}, "EMAIL_ALLOWED_DOMAINS"),
    ])
    def test_email_mode_needs_its_secrets(self, env: dict[str, str], match: str) -> None:
        with pytest.raises(ValidationError, match=match):
            cfg(AUTH_MODE="email", **env)

    def test_docs_never_public_in_production(self) -> None:
        assert not cfg(**ENTRA_ENV, API_DOCS_PUBLIC=True).docs_public
        assert cfg(**{**ENTRA_ENV, "APP_ENV": "local"}, API_DOCS_PUBLIC=True).docs_public

    def test_database_selected_only_by_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for url in ("postgresql://qa@127.0.0.1:5433/qa", "postgresql://qa@127.0.0.1:5432/qa"):
            monkeypatch.setenv("DATABASE_URL", url)
            assert url == Settings(**EMAIL_ENV).DATABASE_URL  # type: ignore[arg-type]


def login(client: TestClient, body: Any, **kw: Any) -> Any:
    return client.post("/api/v1/auth/email-login", json=body, **kw)


def signup(client: TestClient, body: Any, **kw: Any) -> Any:
    return client.post("/api/v1/auth/email-signup", json=body, **kw)


class TestEmailLogin:
    def test_valid_email_gives_participant_only_session(self, client: TestClient) -> None:
        signup(client, {"email": "first.last+quiz@example.test"})
        res = login(client, {"email": "  First.Last+Quiz@Example.TEST "})
        assert res.status_code == 200
        d = res.json()["data"]
        assert {k: d[k] for k in ("token_type", "user_id", "user_name", "authentication_method", "email_verified")} == {
            "token_type": "Bearer", "user_id": "first.last+quiz@example.test", "user_name": "first.last+quiz@example.test",
            "authentication_method": "email", "email_verified": False}
        claims = jwt.decode(d["access_token"], SECRET, algorithms=["HS256"], audience=EMAIL_AUDIENCE)
        assert claims["roles"] == ["employee"] and claims["iss"] == EMAIL_ISSUER and claims["email_verified"] is False
        assert claims["exp"] - claims["iat"] == d["expires_in"] == 3600
        me = client.get("/api/v1/me", headers=auth(d["access_token"])).json()["data"]
        assert (me["user_id"], me["is_admin"], me["roles"], me["email_verified"]) == ("first.last+quiz@example.test", False, ["employee"], False)

    @pytest.mark.parametrize("email", ["", "   ", None, 5, ["a@b.test"], "no-at-sign", "a@b", "two@@example.test", "sp ace@example.test",
                                       "a@-bad.test", ".lead@example.test", "x" * 65 + "@example.test", "a@" + "b" * 200 + ".test"])
    def test_blank_or_invalid_rejected(self, client: TestClient, email: Any) -> None:
        res = login(client, {"email": email})
        assert res.status_code == 400 and res.json()["error"]["fields"][0]["field"] == "email"
        assert "access_token" not in res.text

    def test_identity_and_roles_in_body_rejected(self, client: TestClient) -> None:
        for extra in ({"roles": ["admin"]}, {"name": "Someone"}, {"is_admin": True}):
            assert login(client, {"email": "a@example.test", **extra}).status_code == 400

    def test_unregistered_email_must_sign_up(self, clean: Any, client: TestClient) -> None:
        res = login(client, {"email": "new.person@example.test"})
        assert res.status_code == 404 and res.json()["error"]["code"] == "USER_NOT_FOUND" and "access_token" not in res.text
        first = signup(client, {"email": " New.Person@Example.test"})
        assert first.status_code == 201 and first.json()["data"]["user_id"] == "new.person@example.test"
        again = signup(client, {"email": "new.person@example.test"})
        assert again.status_code == 409 and again.json()["error"]["code"] == "USER_EXISTS"
        assert login(client, {"email": "new.person@example.test"}).status_code == 200
        assert clean.execute("SELECT user_id FROM users").fetchall() == [("new.person@example.test",)]

    def test_domain_restriction(self, db_url: str) -> None:
        app = create_app(settings(db_url, EMAIL_ALLOWED_DOMAINS="alrium.com"), pool=create_pool(db_url, 1))
        with TestClient(app) as c:
            signup(c, {"email": "first.last@alrium.com"})
            assert login(c, {"email": "First.Last@Alrium.com"}).json()["data"]["user_id"] == "first.last@alrium.com"
            assert signup(c, {"email": "first.last@alrium.com.evil.test"}).status_code == 400
            assert signup(c, {"email": "first.last@sub.alrium.com"}).status_code == 400
            signup(c, {"email": "admin@alrium.com"})
            tok = login(c, {"email": "admin@alrium.com"}).json()["data"]["access_token"]
            assert c.get("/api/v1/admin/questions", headers=auth(tok)).status_code == 403

    def test_rate_limited(self, db_url: str) -> None:
        app = create_app(settings(db_url, EMAIL_LOGIN_RATE_PER_MINUTE="3"), pool=create_pool(db_url, 1))
        with TestClient(app) as c:
            codes = [signup(c, {"email": f"rate{i}@example.test"}).status_code for i in range(4)]
            assert codes == [201, 201, 201, 429]
        limiter = RateLimiter(2)
        assert [limiter.allow("k", t) for t in (0, 1, 2, 61, 62, 63)] == [True, True, False, True, True, False]

    def test_forged_and_expired_tokens_rejected(self, client: TestClient) -> None:
        now = int(time.time())
        base = {"iss": EMAIL_ISSUER, "aud": EMAIL_AUDIENCE, "sub": "x@example.test", "email": "x@example.test", "name": "x@example.test",
                "authentication_method": "email", "email_verified": False, "roles": ["employee"], "iat": now, "nbf": now, "exp": now + 300}
        bad = [
            jwt.encode(base, "not-the-real-secret-not-the-real-secret", algorithm="HS256"),
            jwt.encode({**base, "exp": now - 120, "iat": now - 600, "nbf": now - 600}, SECRET, algorithm="HS256"),
            jwt.encode({**base, "aud": "other"}, SECRET, algorithm="HS256"),
            jwt.encode({**base, "iss": "other"}, SECRET, algorithm="HS256"),
            jwt.encode({**base, "authentication_method": "entra"}, SECRET, algorithm="HS256"),
            jwt.encode({**base, "email": "y@example.test"}, SECRET, algorithm="HS256"),
            jwt.encode({**base, "sub": "not an email", "email": "not an email"}, SECRET, algorithm="HS256"),
            jwt.encode(base, SECRET, algorithm="HS512"),
            jwt.encode(base, None, algorithm="none"),
        ]
        for token in bad:
            assert client.get("/api/v1/me", headers=auth(token)).status_code == 401, jwt.decode(token, options={"verify_signature": False})
        good = jwt.encode(base, SECRET, algorithm="HS256")
        assert client.get("/api/v1/me", headers=auth(good)).status_code == 200

    def test_admin_role_claim_in_token_is_ignored(self, client: TestClient) -> None:
        now = int(time.time())
        forged = jwt.encode({"iss": EMAIL_ISSUER, "aud": EMAIL_AUDIENCE, "sub": "boss@example.test", "email": "boss@example.test",
                             "authentication_method": "email", "roles": ["employee", "admin"], "iat": now, "nbf": now, "exp": now + 300},
                            SECRET, algorithm="HS256")
        assert client.get("/api/v1/me", headers=auth(forged)).json()["data"]["is_admin"] is False
        assert client.get("/api/v1/admin/questions", headers=auth(forged)).status_code == 403

    @pytest.mark.parametrize("headers", [{}, {"authorization": "Basic abc"}, {"authorization": "Bearer not-a-jwt"},
                                         {"x-user-email": "first.last@example.test", "x-user-name": "First Last"}])
    def test_missing_identity_rejected(self, client: TestClient, headers: dict[str, str]) -> None:
        for path in ("/api/v1/me", "/api/v1/questions/today", "/api/v1/me/responses"):
            res = client.get(path, headers=headers)
            assert res.status_code == 401 and res.json()["error"]["code"] == "UNAUTHORIZED"


class TestAdminKey:
    def test_participant_tokens_cannot_use_admin_routes(self, client: TestClient, tokens: dict[str, str]) -> None:
        for path in ("/api/v1/admin/questions", "/api/v1/admin/responses"):
            res = client.get(path, headers=auth(tokens["employee-correct"]))
            assert res.status_code == 403 and res.json()["error"]["code"] == "FORBIDDEN"
        res = client.post("/api/v1/admin/questions", headers=auth(tokens["employee-correct"]), json={})
        assert res.status_code == 403

    def test_key_required_and_compared(self, client: TestClient, tokens: dict[str, str]) -> None:
        assert client.get("/api/v1/admin/questions").status_code == 401
        assert client.get("/api/v1/admin/questions", headers={"x-admin-key": ADMIN_KEY[:-1] + "x"}).status_code == 401
        assert client.get("/api/v1/admin/questions", headers={"x-admin-key": ""}).status_code == 401
        assert client.get("/api/v1/admin/questions", headers=auth(ADMIN)).status_code == 200
        both = client.get("/api/v1/admin/questions", headers={**auth(tokens["employee-correct"]), **auth(ADMIN)})
        assert both.status_code == 200

    def test_admin_key_is_not_a_participant_identity(self, client: TestClient) -> None:
        assert client.get("/api/v1/me", headers=auth(ADMIN)).status_code == 401
        assert client.post("/api/v1/questions/1/responses", headers=auth(ADMIN), json={"response_answer": "x"}).status_code == 401

    def test_admin_disabled_without_key(self, db_url: str) -> None:
        app = create_app(settings(db_url, ADMIN_API_KEY=""), pool=create_pool(db_url, 1))
        with TestClient(app) as c:
            res = c.get("/api/v1/admin/questions", headers={"x-admin-key": ADMIN_KEY})
            assert res.status_code == 403 and res.json()["error"]["code"] == "ADMIN_DISABLED"
            assert c.get("/api/v1/admin/questions").status_code == 403

    def test_mode_endpoint_is_public(self, client: TestClient, entra: TestClient) -> None:
        assert client.get("/api/v1/auth/mode").json()["data"] == {"mode": "email", "email_verified": False, "allowed_domains": []}
        assert entra.get("/api/v1/auth/mode").json()["data"] == {"mode": "entra", "email_verified": True, "allowed_domains": []}

    def test_public_docs_list_email_login_only_in_email_mode(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        assert "/api/v1/auth/email-login" in spec["paths"] and "/api/v1/dev/token" not in spec["paths"]
        assert "adminKey" in spec["components"]["securitySchemes"]


@pytest.fixture(scope="module")
def kit() -> EntraKit:
    return EntraKit()


@pytest.fixture(scope="module")
def entra(db_url: str, kit: EntraKit) -> Iterator[TestClient]:
    app = create_app(Settings(DATABASE_URL=db_url, **ENTRA_ENV), pool=create_pool(db_url, 2), entra_keys=kit.resolver())  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c


class TestEntra:
    def me(self, entra: TestClient, token: str) -> Any:
        return entra.get("/api/v1/me", headers=auth(token))

    def test_valid_token_normalized_identity(self, entra: TestClient, kit: EntraKit) -> None:
        d = self.me(entra, kit.sign()).json()["data"]
        assert (d["user_id"], d["user_name"], d["roles"], d["auth_mode"], d["email_verified"]) == (
            "employee.one@contoso.example", "Employee One", ["employee"], "entra", True)

    def test_client_audience_postman_and_upn(self, entra: TestClient, kit: EntraKit) -> None:
        assert self.me(entra, kit.sign({"aud": API_CLIENT})).status_code == 200
        assert self.me(entra, kit.sign({"azp": POSTMAN_CLIENT})).status_code == 200
        assert self.me(entra, kit.sign({"email": None, "upn": "UPN.User@contoso.example"})).json()["data"]["user_id"] == "upn.user@contoso.example"

    def test_admin_only_via_role(self, entra: TestClient, kit: EntraKit) -> None:
        assert self.me(entra, kit.sign({"roles": ["Quiz.Admin"]})).json()["data"]["is_admin"] is True
        assert entra.get("/api/v1/admin/questions", headers=auth(kit.sign({"email": "admin@contoso.example", "roles": ["Other"]}))).status_code == 403
        assert entra.get("/api/v1/admin/questions", headers=auth(kit.sign({"roles": ["Quiz.Admin"]}))).status_code == 200

    @pytest.mark.parametrize("label,claims,opts,status,code", [
        ("wrong tenant issuer", {"tid": OTHER_TENANT}, {"issuer": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"}, 401, "UNAUTHORIZED"),
        ("tid mismatch", {"tid": OTHER_TENANT}, {}, 401, "UNAUTHORIZED"),
        ("v1 issuer", {}, {"issuer": f"https://sts.windows.net/{TENANT}/"}, 401, "UNAUTHORIZED"),
        ("v1 version", {"ver": "1.0"}, {}, 401, "UNAUTHORIZED"),
        ("graph audience", {"aud": "00000003-0000-0000-c000-000000000000"}, {}, 401, "UNAUTHORIZED"),
        ("id token", {"aud": "22222222-0000-0000-0000-000000000002", "scp": None}, {}, 401, "UNAUTHORIZED"),
        ("app-only", {"scp": None, "roles": ["Quiz.Admin"], "idtyp": "app"}, {}, 401, "UNAUTHORIZED"),
        ("expired", {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200, "nbf": int(time.time()) - 7200}, {}, 401, "UNAUTHORIZED"),
        ("not yet valid", {"nbf": int(time.time()) + 3600}, {}, 401, "UNAUTHORIZED"),
        ("other key", {}, {"other": True}, 401, "UNAUTHORIZED"),
        ("unknown kid", {}, {"kid": "nope"}, 401, "UNAUTHORIZED"),
        ("missing scope", {"scp": "User.Read"}, {}, 403, "INSUFFICIENT_SCOPE"),
        ("client not allowed", {"azp": "33333333-0000-0000-0000-000000000000"}, {}, 403, "CLIENT_NOT_ALLOWED"),
        ("guest", {"acct": 1}, {}, 403, "GUEST_NOT_ALLOWED"),
        ("no email or upn", {"email": None}, {}, 403, "IDENTITY_CLAIMS_MISSING"),
        ("no name", {"name": None}, {}, 403, "IDENTITY_CLAIMS_MISSING"),
        ("no oid", {"oid": None}, {}, 403, "IDENTITY_CLAIMS_MISSING"),
    ])
    def test_rejects(self, entra: TestClient, kit: EntraKit, label: str, claims: Any, opts: Any, status: int, code: str) -> None:
        res = self.me(entra, kit.sign(claims, **opts))
        assert (res.status_code, res.json()["error"]["code"]) == (status, code), label

    def test_failed_microsoft_sign_in_never_falls_back_to_email(self, entra: TestClient, client: TestClient, kit: EntraKit) -> None:
        assert login(entra, {"email": "first.last@example.test"}).status_code == 404
        assert self.me(entra, email_token(client, "first.last@example.test")).status_code == 401
        for h in ({}, {"authorization": "Bearer not-a-jwt"}, auth(kit.sign({"exp": int(time.time()) - 3600, "iat": 0, "nbf": 0}))):
            assert entra.get("/api/v1/questions/today", headers=h).status_code == 401

    def test_unreachable_microsoft_keys_fail_closed(self, db_url: str) -> None:
        def down(_token: str) -> Any:
            raise ConnectionError("login.microsoftonline.com unreachable")

        app = create_app(Settings(DATABASE_URL=db_url, **ENTRA_ENV), pool=create_pool(db_url, 1), entra_keys=down)  # type: ignore[arg-type]
        with TestClient(app) as c:
            assert c.get("/api/v1/me", headers=auth(EntraKit().sign())).status_code == 401
            assert login(c, {"email": "first.last@example.test"}).status_code == 404

    def test_admin_key_not_accepted_in_entra_mode(self, entra: TestClient, kit: EntraKit) -> None:
        assert entra.get("/api/v1/admin/questions", headers={"x-admin-key": ADMIN_KEY}).status_code == 400
        both = entra.get("/api/v1/admin/questions", headers={**auth(kit.sign({"roles": ["Quiz.Admin"]})), "x-admin-key": ADMIN_KEY})
        assert both.status_code == 400

    def test_docs_private(self, entra: TestClient, kit: EntraKit) -> None:
        assert entra.get("/openapi.json").status_code == 401
        assert entra.get("/openapi.json", headers=auth(kit.sign())).status_code == 403
        ok = entra.get("/openapi.json", headers=auth(kit.sign({"roles": ["Quiz.Admin"]})))
        assert ok.status_code == 200 and "/api/v1/auth/email-login" not in ok.json()["paths"]
        assert entra.get("/docs").status_code == 404

    def test_participant_role_when_configured(self, db_url: str, kit: EntraKit) -> None:
        app = create_app(Settings(DATABASE_URL=db_url, **ENTRA_ENV, ENTRA_PARTICIPANT_ROLE="Quiz.Participant"),  # type: ignore[arg-type]
                         pool=create_pool(db_url, 1), entra_keys=kit.resolver())
        with TestClient(app) as c:
            assert c.get("/api/v1/me", headers=auth(kit.sign())).json()["error"]["code"] == "NOT_ELIGIBLE"
            assert c.get("/api/v1/me", headers=auth(kit.sign({"roles": ["Quiz.Participant"]}))).status_code == 200
