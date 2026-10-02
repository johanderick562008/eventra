"""Reusable authentication / authorization dependencies. Every protected route uses one."""

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import ForbiddenError, RateLimitedError, UnauthorizedError
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User, UserRole

_bearer = HTTPBearer(auto_error=False, description="Paste the `access_token` from /auth/login")

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Not authenticated", code="NOT_AUTHENTICATED")
    claims = decode_access_token(credentials.credentials)
    user = db.get(User, uuid.UUID(claims["sub"]))
    # Checked on every request, so deactivation takes effect immediately.
    if user is None or not user.is_active:
        raise UnauthorizedError("Invalid access token", code="INVALID_TOKEN")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def _require_role(role: UserRole) -> Callable[[User], User]:
    def dependency(user: CurrentUser) -> User:
        # The role is read from the database row, not trusted from the token.
        if user.role != role:
            raise ForbiddenError(
                f"This action requires the {role.value} role", code="INSUFFICIENT_ROLE"
            )
        return user

    return dependency


CurrentAttendee = Annotated[User, Depends(_require_role(UserRole.ATTENDEE))]
CurrentOrganizer = Annotated[User, Depends(_require_role(UserRole.ORGANIZER))]


booking_rate_limiter = SlidingWindowRateLimiter(
    limit=get_settings().booking_rate_limit_per_minute, window_seconds=60
)


def enforce_booking_rate_limit(user: CurrentAttendee) -> User:
    allowed, retry_after = booking_rate_limiter.hit(str(user.id))
    if not allowed:
        raise RateLimitedError(
            "Too many booking attempts, please retry later",
            code="RATE_LIMITED",
            headers={"Retry-After": str(retry_after)},
        )
    return user


RateLimitedAttendee = Annotated[User, Depends(enforce_booking_rate_limit)]
