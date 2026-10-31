"""
Authentication and role-based authorisation.

    login     email + password  ->  bcrypt check  ->  signed JWT access token
    session   the token is verified on every request; the user row is re-read from the
              database so a disabled account or a changed role takes effect immediately
    RBAC      one `users` table, a `role_id` column, and a permission map per role

Passwords are stored only as bcrypt hashes. The JWT signing key comes from the environment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import settings
from models import Role, User

ROLES = ("ADMIN", "MANAGER", "AUDITOR")

# What each role may do. Menus, pages and API routes are all derived from this one map.
PERMISSIONS: dict[str, frozenset[str]] = {
    "ADMIN": frozenset({"view", "registry:write", "lots:write", "status:change",
                        "users:manage", "tamper:run"}),
    "MANAGER": frozenset({"view", "registry:write", "lots:write", "status:change"}),
    "AUDITOR": frozenset({"view"}),
}

# Dashboard pages and the permission each one needs (role-based menu and page access).
PAGES: dict[str, str] = {
    "Control tower": "view",
    "Recall simulator": "view",
    "Provenance": "view",
    "Ledger": "view",
    "Registry": "view",          # read-only for AUDITOR; write forms need registry:write
    "Users": "users:manage",
    "Benchmark": "view",
    "The gap": "view",
}


class AuthError(Exception):
    """Authentication failed (bad credentials, bad or expired token, disabled account)."""


class Forbidden(Exception):
    """The caller is authenticated but their role does not allow the action."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, as seen by the service layer."""
    user_id: int
    email: str
    full_name: str
    role: str

    def can(self, permission: str) -> bool:
        return permission in PERMISSIONS.get(self.role, frozenset())

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise Forbidden(f"role {self.role} may not perform '{permission}'")


def visible_pages(principal: Principal) -> list[str]:
    return [name for name, perm in PAGES.items() if principal.can(perm)]


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(rounds=12))


def validate_password_strength(password: str) -> None:
    problems = []
    if len(password) < 8:
        problems.append("at least 8 characters")
    if not re.search(r"[A-Z]", password):
        problems.append("an uppercase letter")
    if not re.search(r"[a-z]", password):
        problems.append("a lowercase letter")
    if not re.search(r"\d", password):
        problems.append("a digit")
    if problems:
        raise ValueError("Password needs " + ", ".join(problems) + ".")
    if len(password.encode()) > 72:
        raise ValueError("Password is longer than bcrypt's 72-byte limit.")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def create_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user.user_id), "email": user.email, "role": user.role.role_name,
              "iat": now, "exp": now + timedelta(minutes=settings.JWT_TTL_MINUTES)}
    return jwt.encode(claims, settings.jwt_secret(), algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret(), algorithms=[settings.JWT_ALGORITHM],
                          options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Session expired. Please sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid token.") from exc


# ---------------------------------------------------------------------------
# Login and current user (ORM)
# ---------------------------------------------------------------------------
def authenticate(session: Session, email: str, password: str) -> User:
    """Check credentials. The same message is returned for 'no such user' and 'wrong
    password', and a dummy bcrypt check keeps the response time alike."""
    user = session.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user is None:
        bcrypt.checkpw(password.encode(), _DUMMY_HASH)      # equalise timing
        raise AuthError("Incorrect email or password.")
    if not verify_password(password, user.password_hash):
        raise AuthError("Incorrect email or password.")
    if not user.is_active:
        raise AuthError("This account is disabled. Contact an administrator.")
    user.last_login_at = datetime.now(timezone.utc)
    session.flush()
    return user


def principal_from_token(session: Session, token: str) -> Principal:
    """Verify the token, then re-read the user so role and active flag are current."""
    claims = decode_token(token)
    user = session.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise AuthError("Account no longer valid.")
    return Principal(user.user_id, user.email, user.full_name, user.role.role_name)


def create_user(session: Session, *, email: str, full_name: str, password: str, role_name: str,
                party_id: int | None = None) -> User:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address.")
    if not full_name.strip():
        raise ValueError("Name is required.")
    validate_password_strength(password)
    role = session.scalar(select(Role).where(Role.role_name == role_name))
    if role is None:
        raise ValueError(f"Unknown role {role_name!r}.")
    if session.scalar(select(User.user_id).where(User.email == email)):
        raise ValueError("A user with this email already exists.")
    user = User(email=email, full_name=full_name.strip(), password_hash=hash_password(password),
                role_id=role.role_id, party_id=party_id)
    session.add(user)
    session.flush()
    session.refresh(user)
    return user
