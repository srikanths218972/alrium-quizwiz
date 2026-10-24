"""Writes the OpenAPI document (email mode with throwaway values, so the email-login route is included)."""

import json
import os
import sys

from fastapi.testclient import TestClient

from .app import create_app
from .config import Settings


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "openapi.json"
    cfg = Settings(DATABASE_URL="postgresql://unused@127.0.0.1:1/unused", AUTH_MODE="email", APP_ENV="local",
                   API_DOCS_PUBLIC=True, EMAIL_TOKEN_SECRET=os.urandom(32).hex())
    with TestClient(create_app(cfg)) as client:
        doc = client.get("/openapi.json").json()
    with open(out, "w") as f:
        f.write(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
