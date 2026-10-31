"""
Central configuration. Secrets come from the environment (a local `.env` file that is
git-ignored, or the hosting platform's variables) and are never written in source.

    DATABASE_URL   postgresql://user:password@host:port/dbname
    JWT_SECRET     signing key for access tokens (32+ random characters)
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in "
            f"(or set it in the environment).")
    return value


def database_url() -> str:
    return require("DATABASE_URL")


def jwt_secret() -> str:
    secret = require("JWT_SECRET")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 characters")
    return secret


JWT_ALGORITHM = "HS256"
JWT_TTL_MINUTES = int(os.getenv("JWT_TTL_MINUTES", "60"))
