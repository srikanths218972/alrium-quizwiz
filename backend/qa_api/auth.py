"""Authentication. Participant identity only ever comes from a verified bearer token.

entra: Microsoft Entra ID v2.0 delegated access tokens (RS256, tenant keys).
email: tokens this API signs itself (HS256, EMAIL_TOKEN_SECRET) after POST /api/v1/auth/email-login.
       The email inside is self-declared, not verified, and the token never carries admin rights.
The two verifiers use different algorithms, issuers and audiences, and only the configured mode's
verifier exists in a running app, so an email token is never accepted in entra mode.
"""

import hmac
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import jwt
from jwt import PyJWKClient

from .config import Settings
from .errors import AppError, FieldError, bad_request, forbidden, unauthorized

Role = Literal["employee", "admin"]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
BEARER = re.compile(r"^Bearer ([A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]*)$")


@dataclass(frozen=True)
class Principal:
    user_id: str
    user_name: str
    roles: list[Role] = field(default_factory=list)
    subject: str = ""
    source: Literal["entra", "email", "admin-key", "open"] = "entra"
    email_verified: bool = True

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


Verifier = Callable[[str], Principal]


def normalize_email(value: str) -> str:
    return value.strip().lower()


def _is_email(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) <= 320 and bool(EMAIL.match(value.strip()))


def token_from_header(header: str | None) -> str:
    match = BEARER.match(header or "")
    if not match:
        raise unauthorized()
    return match.group(1)



KeyResolver = Callable[[str], Any]


def entra_verifier(cfg: Settings, keys: KeyResolver | None = None) -> Verifier:
    """Validates Entra ID v2.0 delegated ACCESS tokens issued for this API (single tenant).

    Claims: tid = ENTRA_TENANT_ID; aud in ENTRA_API_AUDIENCE; scp contains ENTRA_REQUIRED_SCOPE;
    azp in ENTRA_ALLOWED_CLIENT_IDS; oid, email (or upn) and name for identity; roles for admin;
    acct = 1 (guest) rejected when ENTRA_REJECT_GUESTS. `keys` maps a token to its signing key
    and is injectable for tests only; the running app always uses the tenant's published JWKS.
    """
    tenant = cfg.ENTRA_TENANT_ID.lower()
    issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
    if keys is None:
        jwks = PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", cache_keys=True, lifespan=3600)

        def keys(token: str) -> Any:
            return jwks.get_signing_key_from_jwt(token).key

    resolve = keys

    def verify(token: str) -> Principal:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                resolve(token),
                algorithms=["RS256"],
                audience=cfg.audiences,
                issuer=issuer,
                leeway=60,
                options={"require": ["exp", "nbf", "iat", "tid"]},
            )
        except Exception as err:
            raise unauthorized("The access token is invalid or expired.") from err

        if claims.get("ver") != "2.0":
            raise unauthorized("A v2.0 access token is required.")
        if str(claims.get("tid", "")).lower() != tenant:
            raise unauthorized("The token was issued by another tenant.")
        scopes = claims["scp"].split(" ") if isinstance(claims.get("scp"), str) else []
        if not scopes or claims.get("idtyp") == "app":
            raise unauthorized("A delegated user access token is required.")
        if cfg.ENTRA_REQUIRED_SCOPE not in scopes:
            raise forbidden("INSUFFICIENT_SCOPE", f"The token does not grant the {cfg.ENTRA_REQUIRED_SCOPE} scope.")
        if str(claims.get("azp", "")).lower() not in cfg.allowed_client_ids:
            raise forbidden("CLIENT_NOT_ALLOWED", "This client application is not allowed to call the API.")
        if cfg.ENTRA_REJECT_GUESTS and str(claims.get("acct")) == "1":
            raise forbidden("GUEST_NOT_ALLOWED", "Guest accounts cannot use this application.")

        app_roles = [r for r in claims.get("roles", []) if isinstance(r, str)] if isinstance(claims.get("roles"), list) else []
        is_admin = cfg.ENTRA_ADMIN_ROLE in app_roles
        if cfg.ENTRA_PARTICIPANT_ROLE and not is_admin and cfg.ENTRA_PARTICIPANT_ROLE not in app_roles:
            raise forbidden("NOT_ELIGIBLE", "Your account is not assigned to this application.")

        email = claims.get("email") if _is_email(claims.get("email")) else claims.get("upn") if _is_email(claims.get("upn")) else None
        name = claims["name"].strip()[:200].strip() if isinstance(claims.get("name"), str) else ""
        oid = claims.get("oid") if isinstance(claims.get("oid"), str) else ""
        if not email or not name or not oid:
            raise forbidden("IDENTITY_CLAIMS_MISSING", "The token does not include the required oid, email and name claims.")
        return Principal(
            user_id=normalize_email(email),
            user_name=name,
            roles=["employee", "admin"] if is_admin else ["employee"],
            subject=oid,
            source="entra",
        )

    return verify



EMAIL_ISSUER = "urn:daily-qa:email-login"
EMAIL_AUDIENCE = "urn:daily-qa:participant"
EMAIL_MAX = 200
EMAIL_SYNTAX = re.compile(
    r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def parse_login_email(value: Any, allowed_domains: list[str]) -> str:
    """Trims and lowercases; nothing else is rewritten (dots and +tags are kept)."""
    if not isinstance(value, str) or not value.strip():
        raise bad_request("Enter your email address.", [FieldError("email", "must not be blank")])
    email = normalize_email(value)
    if len(email) > EMAIL_MAX or len(email.split("@")[0]) > 64 or not EMAIL_SYNTAX.match(email):
        raise bad_request("Enter a valid email address.", [FieldError("email", f"must be a valid email address of at most {EMAIL_MAX} characters")])
    if allowed_domains and email.rsplit("@", 1)[1] not in allowed_domains:
        raise bad_request("This email domain is not accepted.", [FieldError("email", f"must end with @{' or @'.join(allowed_domains)}")])
    return email


def issue_email_token(cfg: Settings, email: str, now: int | None = None) -> tuple[str, int]:
    """Participant role only. The display name is the email itself: no name is invented."""
    now = int(time.time()) if now is None else now
    ttl = cfg.EMAIL_TOKEN_TTL_MINUTES * 60
    claims = {
        "iss": EMAIL_ISSUER, "aud": EMAIL_AUDIENCE, "sub": email, "email": email, "name": email,
        "roles": ["employee"], "authentication_method": "email", "email_verified": False,
        "iat": now, "nbf": now, "exp": now + ttl,
    }
    return jwt.encode(claims, cfg.EMAIL_TOKEN_SECRET, algorithm="HS256"), ttl


def email_verifier(cfg: Settings) -> Verifier:
    def verify(token: str) -> Principal:
        try:
            claims = jwt.decode(
                token, cfg.EMAIL_TOKEN_SECRET, algorithms=["HS256"], audience=EMAIL_AUDIENCE, issuer=EMAIL_ISSUER,
                options={"require": ["exp", "nbf", "iat", "sub"]},
            )
        except Exception as err:
            raise unauthorized("The session is invalid or expired. Continue with your email again.") from err
        if claims.get("authentication_method") != "email" or claims.get("email") != claims.get("sub"):
            raise unauthorized("The session is invalid or expired. Continue with your email again.")
        try:
            email = parse_login_email(claims["sub"], cfg.allowed_domains)
        except AppError as err:
            raise unauthorized("The session is invalid or expired. Continue with your email again.") from err
        return Principal(email, email, ["employee"], email, "email", email_verified=False)

    return verify


class RateLimiter:
    """Sliding one-minute window per key, in process memory (single API process)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            if len(self.hits) > 10000:
                for k in [k for k, v in self.hits.items() if not v or v[-1] <= now - 60]:
                    del self.hits[k]
            return True




def admin_key_principal(cfg: Settings, presented: str | None) -> Principal:
    """Email mode only: the separately configured X-Admin-Key. Not a participant identity."""
    if not cfg.ADMIN_API_KEY:
        raise forbidden("ADMIN_DISABLED", "Administration is not enabled on this server.")
    if not presented or not hmac.compare_digest(presented.encode(), cfg.ADMIN_API_KEY.encode()):
        raise unauthorized("A valid X-Admin-Key header is required.")
    return Principal("admin-key", "Administrator key", ["admin"], "admin-key", "admin-key")


def require_admin(who: Principal) -> Principal:
    if not who.is_admin:
        raise AppError(403, "FORBIDDEN", "Administrator role required.")
    return who
