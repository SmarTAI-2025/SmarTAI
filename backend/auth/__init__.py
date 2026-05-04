"""Auth utilities — JWT encode/decode + bcrypt hashing + FastAPI dependencies.

Token format: HS256 JWT containing {sub: user_id, role: ..., exp: ...}.

Two dependencies are exposed:
  - get_optional_user: returns User or None (used by legacy endpoints to gracefully
    accept anonymous calls when SMARTAI_REQUIRE_AUTH=false).
  - get_current_user: returns User or raises 401 (used by all new endpoints).

Demo tokens (prefix "demo-"): the frontend's "Demo login (no backend)" button sets
a synthetic token like "demo-teacher-alice". This module decodes them into a
synthetic User without needing a backend record. Disable in production by setting
SMARTAI_REQUIRE_AUTH=true (real JWT only).
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from backend.config import settings
from backend.models import User
from backend.state import get_user_store


# OAuth2 bearer scheme. auto_error=False so missing tokens don't 401 here;
# we let the dependency decide.
_oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ─── Password hashing ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ─── JWT encode / decode ──────────────────────────────────────────────────────

def create_token(user_id: str, role: str, expires_in_hours: Optional[int] = None) -> str:
    exp = int(time.time()) + (expires_in_hours or settings.jwt_expiry_hours) * 3600
    payload = {"sub": user_id, "role": role, "exp": exp, "iat": int(time.time()), "jti": str(uuid.uuid4())[:12]}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ─── Demo token decoding ──────────────────────────────────────────────────────

def _decode_demo_token(token: str) -> Optional[User]:
    """Decode a frontend "demo-..." token into a synthetic User.

    Token format: "demo-<role>-<username>" or "demo-<username>" (defaults to teacher).
    """
    if not token.startswith("demo-"):
        return None
    rest = token[len("demo-"):]
    parts = rest.split("-", 1)
    if len(parts) == 2 and parts[0] in ("teacher", "student", "admin"):
        role, username = parts
    else:
        role, username = "teacher", rest
    return User(
        id=f"demo_{username}",
        username=username,
        email=f"{username}@demo.local",
        role=role,  # type: ignore
        password_hash="",
    )


# ─── FastAPI dependencies ─────────────────────────────────────────────────────

def get_optional_user(
    token: Optional[str] = Depends(_oauth2),
    user_store: dict = Depends(get_user_store),
) -> Optional[User]:
    """Return a User if the token is valid; else None.

    If SMARTAI_REQUIRE_AUTH=true and token is missing/invalid, raises 401 here so
    even legacy endpoints become protected.
    """
    if not token:
        if settings.require_auth:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing token")
        return None

    # Demo token shortcut
    demo = _decode_demo_token(token)
    if demo is not None:
        return demo

    payload = decode_token(token)
    if payload is None:
        if settings.require_auth:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return None
    user_id = payload.get("sub")
    user = user_store.get(user_id) if user_id else None
    if user is None and settings.require_auth:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_current_user(user: Optional[User] = Depends(get_optional_user)) -> User:
    """Required-auth dependency. Used by all new (auth/users/courses/assignments) endpoints."""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def require_teacher(user: User = Depends(get_current_user)) -> User:
    if user.role not in ("teacher", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Teacher access required")
    return user


def require_student(user: User = Depends(get_current_user)) -> User:
    if user.role != "student":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Student access required")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
