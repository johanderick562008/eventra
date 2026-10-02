import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.security import hash_password, verify_password
from app.models.booking import Booking, BookingStatus
from app.models.event import Event, EventStatus
from app.models.user import User, UserRole
from app.schemas.common import Pagination
from app.schemas.user import UserUpdate
from app.services.auth_service import revoke_all_for_user
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


def list_organizers(db: Session, pagination: Pagination) -> tuple[Sequence[User], int]:
    """Public organizer directory. Attendee accounts are never listed."""
    base = select(User).where(User.role == UserRole.ORGANIZER, User.is_active.is_(True))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    users = db.scalars(
        base.order_by(User.name, User.id).offset(pagination.offset).limit(pagination.limit)
    ).all()
    return users, total


def get_visible_user(db: Session, user_id: uuid.UUID, viewer: User) -> User:
    """Users see themselves; anyone authenticated may see an active organizer's public profile."""
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found", code="USER_NOT_FOUND")
    if user.id == viewer.id or (user.role == UserRole.ORGANIZER and user.is_active):
        return user
    raise NotFoundError("User not found", code="USER_NOT_FOUND")


def ensure_self(user_id: uuid.UUID, viewer: User) -> None:
    """No administrator role exists, so account management is strictly self-service."""
    if user_id != viewer.id:
        raise ForbiddenError("You can only manage your own account", code="NOT_ACCOUNT_OWNER")


def update_profile(db: Session, user: User, data: UserUpdate) -> User:
    if data.new_password is not None:
        if not verify_password(data.current_password or "", user.password_hash):
            raise ForbiddenError("Current password is incorrect", code="INVALID_CURRENT_PASSWORD")
        user.password_hash = hash_password(data.new_password)
        revoke_all_for_user(db, user.id)  # force re-login on other devices
    if data.name is not None:
        user.name = data.name
    db.commit()
    return user


def deactivate_account(db: Session, user: User) -> None:
    """Soft-delete: the row stays so bookings, events and sales history remain intact."""
    now = utcnow()
    if user.role == UserRole.ORGANIZER:
        has_live_events = db.scalar(
            select(
                exists().where(
                    Event.organizer_id == user.id,
                    Event.deleted_at.is_(None),
                    Event.status != EventStatus.CANCELLED,
                    Event.end_at > now,
                )
            )
        )
        if has_live_events:
            raise ConflictError(
                "Cancel or delete your upcoming events before deactivating your account",
                code="ACCOUNT_HAS_ACTIVE_EVENTS",
            )
    else:
        has_upcoming_bookings = db.scalar(
            select(
                exists().where(
                    Booking.user_id == user.id,
                    Booking.status == BookingStatus.CONFIRMED,
                    Booking.event_id == Event.id,
                    Event.start_at > now,
                    Event.status != EventStatus.CANCELLED,
                )
            )
        )
        if has_upcoming_bookings:
            raise ConflictError(
                "Cancel your upcoming bookings before deactivating your account",
                code="ACCOUNT_HAS_ACTIVE_BOOKINGS",
            )
    user.is_active = False
    revoke_all_for_user(db, user.id)
    db.commit()
    logger.info("User deactivated id=%s", user.id)
