"""Settings from environment variables (the Makefile loads the repository .env).

Participant sign-in has two modes:
  entra  Microsoft Entra ID delegated access tokens (verified identity, admin from an app role).
  email  The participant types an email and receives a short-lived server-signed token.
         The email is self-declared: ownership is NOT verified. Never grants admin.

AUTH_MODE selects the mode. When it is unset, a complete Entra configuration selects entra, no
Entra configuration at all selects email, and a partial Entra configuration is a startup error.
The mode is fixed at startup; nothing switches to email because Microsoft sign-in fails.

  open   Testing only, chosen only by AUTH_MODE=open (never automatically) and refused when
         APP_ENV=production. No sign-in and no admin credential: anyone who can reach the API can
         use every route, including admin. Post response takes the tester's email in its body
         (self-declared, not verified).
"""

import re
import sys
from functools import cached_property
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BUSINESS_TIMEZONE = "Asia/Kolkata"
REQUIRED_MIGRATION = "0004"
BODY_LIMIT = 16 * 1024
LIMITS = {"question": 500, "option": 200}

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DOMAIN = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
SETUP_DOC = "docs/ENTRA-SETUP.md and docs/AUTH-MODES.md"

ENTRA_KEYS = ("ENTRA_TENANT_ID", "ENTRA_API_AUDIENCE", "ENTRA_ALLOWED_CLIENT_IDS")


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _placeholder(value: str) -> bool:
    return "<" in value or "replace-with" in value or "00000000-0000-0000-0000-000000000000" in value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    APP_NAME: str = "ålrium - QuizWiz"
    APP_ENV: Literal["local", "production"] = "production"
    AUTH_MODE: str = ""
    DATABASE_URL: str = Field(min_length=1)
    DB_POOL_MAX: int = 10
    ALLOWED_ORIGINS: str = ""
    API_DOCS_PUBLIC: bool = False

    EMAIL_TOKEN_SECRET: str = ""
    EMAIL_TOKEN_TTL_MINUTES: int = Field(default=60, ge=5, le=720)
    EMAIL_ALLOWED_DOMAINS: str = ""
    EMAIL_LOGIN_RATE_PER_MINUTE: int = Field(default=10, ge=1, le=1000)
    ADMIN_API_KEY: str = ""

    ENTRA_TENANT_ID: str = ""
    ENTRA_API_AUDIENCE: str = ""
    ENTRA_REQUIRED_SCOPE: str = "Quiz.Participate"
    ENTRA_ALLOWED_CLIENT_IDS: str = ""
    ENTRA_ADMIN_ROLE: str = "Quiz.Admin"
    ENTRA_PARTICIPANT_ROLE: str = ""
    ENTRA_REJECT_GUESTS: bool = True

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        mode = self.AUTH_MODE.strip().lower()
        if mode not in ("", "entra", "email", "open"):
            raise ValueError(f"AUTH_MODE must be entra, email, open or empty, not {self.AUTH_MODE!r}. "
                             "The former none and local-test modes were removed.")
        if mode == "open":
            if self.APP_ENV == "production":
                raise ValueError("AUTH_MODE=open (no sign-in, admin open to everyone) is refused when APP_ENV=production")
            bad = [d for d in self.allowed_domains if not DOMAIN.match(d)]
            if bad:
                raise ValueError(f"EMAIL_ALLOWED_DOMAINS has invalid domains: {', '.join(bad)}")
            object.__setattr__(self, "AUTH_MODE", mode)
            return self
        entra_problems = self.entra_problems()
        given = [k for k in ENTRA_KEYS if getattr(self, k).strip()]
        if mode == "":
            if not given:
                mode = "email"
            elif entra_problems:
                raise ValueError("Microsoft Entra configuration is incomplete, so no sign-in mode can be chosen. Fix: "
                                 + "; ".join(entra_problems) + f". Or set AUTH_MODE=email explicitly. See {SETUP_DOC}.")
            else:
                mode = "entra"
        if mode == "entra" and entra_problems:
            raise ValueError("AUTH_MODE=entra but Microsoft Entra is not configured: " + "; ".join(entra_problems) + f". See {SETUP_DOC}.")
        if mode == "email":
            problems = []
            if len(self.EMAIL_TOKEN_SECRET) < 32 or _placeholder(self.EMAIL_TOKEN_SECRET):
                problems.append("EMAIL_TOKEN_SECRET must be at least 32 random characters (openssl rand -hex 32)")
            if self.ADMIN_API_KEY and (len(self.ADMIN_API_KEY) < 32 or _placeholder(self.ADMIN_API_KEY)):
                problems.append("ADMIN_API_KEY must be empty (admin routes off) or at least 32 random characters")
            if self.ADMIN_API_KEY and self.ADMIN_API_KEY == self.EMAIL_TOKEN_SECRET:
                problems.append("ADMIN_API_KEY and EMAIL_TOKEN_SECRET must differ")
            bad = [d for d in self.allowed_domains if not DOMAIN.match(d)]
            if bad:
                problems.append(f"EMAIL_ALLOWED_DOMAINS has invalid domains: {', '.join(bad)}")
            if problems:
                raise ValueError("Email sign-in mode is not configured: " + "; ".join(problems) + f". See {SETUP_DOC}.")
        object.__setattr__(self, "AUTH_MODE", mode)
        return self

    def entra_problems(self) -> list[str]:
        p = []
        if not UUID.match(self.ENTRA_TENANT_ID) or _placeholder(self.ENTRA_TENANT_ID):
            p.append("ENTRA_TENANT_ID must be the directory (tenant) ID")
        if not self.audiences or any(_placeholder(a) for a in self.audiences):
            p.append("ENTRA_API_AUDIENCE must list the API client ID and/or api://<api-client-id>")
        if not self.allowed_client_ids or not all(UUID.match(c) and not _placeholder(c) for c in self.allowed_client_ids):
            p.append("ENTRA_ALLOWED_CLIENT_IDS must list the SPA and Postman client IDs")
        if not self.ENTRA_REQUIRED_SCOPE.strip():
            p.append("ENTRA_REQUIRED_SCOPE must be set")
        if not self.ENTRA_ADMIN_ROLE.strip():
            p.append("ENTRA_ADMIN_ROLE must be set")
        return p

    @property
    def mode(self) -> Literal["entra", "email", "open"]:
        return "entra" if self.AUTH_MODE == "entra" else "open" if self.AUTH_MODE == "open" else "email"

    @cached_property
    def audiences(self) -> list[str]:
        return _list(self.ENTRA_API_AUDIENCE)

    @cached_property
    def allowed_client_ids(self) -> list[str]:
        return [c.lower() for c in _list(self.ENTRA_ALLOWED_CLIENT_IDS)]

    @cached_property
    def allowed_domains(self) -> list[str]:
        return [d.lower() for d in _list(self.EMAIL_ALLOWED_DOMAINS)]

    @cached_property
    def origins(self) -> list[str]:
        return _list(self.ALLOWED_ORIGINS)

    @property
    def docs_public(self) -> bool:
        return self.API_DOCS_PUBLIC and self.APP_ENV != "production"


def main() -> None:
    """Startup check: prints the selected mode or the setup problems (never secret values)."""
    try:
        cfg = Settings()
    except Exception as err:  # noqa: BLE001
        print(f"configuration error:\n{err}", file=sys.stderr)
        sys.exit(1)
    extra = ""
    if cfg.mode == "open":
        extra = (" (TESTING ONLY: no sign-in, admin routes open to anyone who can reach the API; "
                 "testers put their email in the Post response body)")
    if cfg.mode == "email":
        extra = (f" (self-declared emails, not verified; domains: {', '.join(cfg.allowed_domains) or 'any'}; "
                 f"admin key: {'set' if cfg.ADMIN_API_KEY else 'not set, admin routes refused'})")
    print(f"sign-in mode: {cfg.mode}{extra}")


if __name__ == "__main__":
    main()
