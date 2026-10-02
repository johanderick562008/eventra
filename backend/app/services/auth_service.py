import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import ConflictError, ForbiddenError, UnauthorizedError
from app.core.security import (
    burn_password_check,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import RegisterRequest, TokenResponse
from app.schemas.user import UserRead
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

_INVALID_CREDENTIALS = "Invalid email or password"


def register_user(db: Session, data: RegisterRequest) -> User:
    duplicate = ConflictError(
        "An account with this email already exists", code="EMAIL_ALREADY_REGISTERED"
    )
    if db.scalar(select(User.id).where(User.email == data.email)) is not None:
        raise duplicate
    user = User(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:  # concurrent registration with the same email
        db.rollback()
        raise duplicate from exc
    logger.info("User registered id=%s role=%s", user.id, user.role)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        burn_password_check(password)
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")
    if not verify_password(password, user.password_hash):
        logger.info("Failed login for user id=%s", user.id)
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")
    # Only revealed after a correct password, so it does not leak account existence.
    if not user.is_active:
        raise ForbiddenError("This account has been deactivated", code="ACCOUNT_INACTIVE")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    return user


def _new_refresh_token(user_id: uuid.UUID, family_id: uuid.UUID) -> tuple[RefreshToken, str]:
    raw = generate_refresh_token()
    token = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=hash_refresh_token(raw),
        family_id=family_id,
        expires_at=utcnow() + timedelta(days=get_settings().refresh_token_expire_days),
    )
    return token, raw


def _token_response(user: User, raw_refresh: str) -> TokenResponse:
    access_token, expires_in = create_access_token(user.id, user.role.value)
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        user=UserRead.model_validate(user),
    )


def issue_tokens(db: Session, user: User) -> TokenResponse:
    token, raw = _new_refresh_token(user.id, family_id=uuid.uuid4())
    db.add(token)
    db.commit()
    logger.info("Login succeeded for user id=%s", user.id)
    return _token_response(user, raw)


def _revoke_family(db: Session, family_id: uuid.UUID) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def rotate_refresh_token(db: Session, raw_token: str) -> TokenResponse:
    """Exchange a refresh token for a new pair.

    Presenting an already-rotated token revokes its whole family (reuse detection)."""
    stored = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(raw_token))
        .with_for_update()
    )
    if stored is None:
        raise UnauthorizedError("Invalid refresh token", code="INVALID_REFRESH_TOKEN")

    if stored.revoked_at is not None:
        _revoke_family(db, stored.family_id)
        db.commit()
        logger.warning("Refresh token reuse detected; family %s revoked", stored.family_id)
        raise UnauthorizedError("Refresh token has been revoked", code="REFRESH_TOKEN_REVOKED")

    if stored.expires_at <= utcnow():
        raise UnauthorizedError("Refresh token has expired", code="REFRESH_TOKEN_EXPIRED")

    user = db.get(User, stored.user_id)
    if user is None or not user.is_active:
        _revoke_family(db, stored.family_id)
        db.commit()
        raise UnauthorizedError("Invalid refresh token", code="INVALID_REFRESH_TOKEN")

    replacement, raw = _new_refresh_token(user.id, family_id=stored.family_id)
    db.add(replacement)
    db.flush()
    stored.revoked_at = utcnow()
    stored.replaced_by_id = replacement.id
    db.commit()
    return _token_response(user, raw)


def logout(db: Session, raw_token: str) -> None:
    """Revoke the token's whole family. Idempotent; unknown tokens are ignored."""
    stored = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token))
    )
    if stored is not None:
        _revoke_family(db, stored.family_id)
        db.commit()


def revoke_all_for_user(db: Session, user_id: uuid.UUID) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
