"""Authentication: password hashing, JWT issue/verify, request dependencies."""
import hashlib
import hmac
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from .database import get_db
from .errors import AppError
from .models import User

# Access tokens presented to /auth/logout are recorded here so they can no
# longer be used.
_revoked_tokens: set[str] = set()
_used_refresh_tokens: set[str] = set()
_token_state_lock = threading.Lock()

_PBKDF2_ROUNDS = 100_000
_REQUIRED_TOKEN_CLAIMS = {"sub", "org", "role", "jti", "iat", "exp", "type"}
_VALID_TOKEN_TYPES = {"access", "refresh"}
_VALID_ROLES = {"admin", "member"}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), dk_hex)


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def create_access_token(user: User) -> str:
    iat = _now_ts()
    lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": uuid.uuid4().hex,
        "iat": iat,
        "exp": iat + int(lifetime.total_seconds()),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user: User) -> str:
    iat = _now_ts()
    lifetime = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": uuid.uuid4().hex,
        "iat": iat,
        "exp": iat + int(lifetime.total_seconds()),
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise AppError(401, "UNAUTHORIZED", "Invalid or expired token")


def _int_claim(payload: dict, claim: str) -> int:
    try:
        value = payload[claim]
        if isinstance(value, bool):
            raise ValueError
        return int(value)
    except (KeyError, TypeError, ValueError):
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")


def validate_token_payload(payload: dict, expected_type: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")

    if _REQUIRED_TOKEN_CLAIMS - payload.keys():
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")

    token_type = payload.get("type")
    if token_type not in _VALID_TOKEN_TYPES:
        raise AppError(401, "UNAUTHORIZED", "Invalid token type")
    if expected_type is not None and token_type != expected_type:
        raise AppError(401, "UNAUTHORIZED", "Wrong token type")

    _int_claim(payload, "sub")
    _int_claim(payload, "org")
    _int_claim(payload, "iat")
    _int_claim(payload, "exp")

    if payload.get("role") not in _VALID_ROLES:
        raise AppError(401, "UNAUTHORIZED", "Invalid token role")
    if not isinstance(payload.get("jti"), str) or not payload["jti"]:
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")

    return payload


def revoke_access_token(payload: dict) -> None:
    validate_token_payload(payload, "access")
    with _token_state_lock:
        _revoked_tokens.add(payload["jti"])


def mark_refresh_token_used(payload: dict) -> None:
    validate_token_payload(payload, "refresh")
    jti = payload.get("jti")
    if not jti:
        raise AppError(401, "UNAUTHORIZED", "Invalid refresh token")
    with _token_state_lock:
        if jti in _used_refresh_tokens:
            raise AppError(401, "UNAUTHORIZED", "Refresh token already used")
        _used_refresh_tokens.add(jti)


def get_token_payload(request: Request) -> dict:
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        raise AppError(401, "UNAUTHORIZED", "Missing bearer token")
    token = header[len("Bearer "):].strip()
    payload = validate_token_payload(decode_token(token), "access")
    with _token_state_lock:
        if payload.get("jti") in _revoked_tokens:
            raise AppError(401, "UNAUTHORIZED", "Token has been revoked")
    return payload


def get_current_user(
    payload: dict = Depends(get_token_payload),
    db: Session = Depends(get_db),
) -> User:
    user_id = _int_claim(payload, "sub")
    org_id = _int_claim(payload, "org")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or user.org_id != org_id or user.role != payload.get("role"):
        raise AppError(401, "UNAUTHORIZED", "Unknown user")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise AppError(403, "FORBIDDEN", "Admin privileges required")
    return user
