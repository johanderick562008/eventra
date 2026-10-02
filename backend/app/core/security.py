"""Password hashing (Argon2id) and token primitives (JWT access tokens, opaque refresh tokens)."""

import hashlib
import secrets
import uuid
from datetime import timedelta
from functools import lru_cache
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.utils.time import utcnow

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification so unknown emails are not detectable by timing."""
    verify_password(password, _dummy_hash())


def create_access_token(user_id: uuid.UUID, role: str) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``. Claims are kept minimal: no email or name."""
    settings = get_settings()
    now = utcnow()
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],  # pinned: rejects `none` and algorithm confusion
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "iss", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Access token has expired", code="TOKEN_EXPIRED") from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid access token", code="INVALID_TOKEN") from exc

    if claims.get("type") != "access":
        raise UnauthorizedError("Invalid access token", code="INVALID_TOKEN")
    try:
        uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        raise UnauthorizedError("Invalid access token", code="INVALID_TOKEN") from exc
    return claims


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """Refresh tokens are 384-bit random values, so a fast unsalted hash is sufficient."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
