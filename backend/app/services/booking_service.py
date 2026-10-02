"""Booking engine.

Concurrency strategy (see README, "Concurrency strategy"):

1. ``SELECT ... FOR UPDATE`` on the event row. Concurrent bookings for the same event queue here.
2. Re-check eligibility and remaining capacity *under the lock*.
3. Insert the booking and increment ``tickets_sold`` in the same transaction, then commit.

The ``tickets_sold <= capacity`` CHECK constraint is a second, database-enforced line of defence.
Lock order is always event -> booking, which keeps cancel/book/event-cancel deadlock-free.

Inventory invariant (for every event):
    tickets_sold == SUM(quantity) of bookings whose status != CANCELLED
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.exceptions import BusinessValidationError, ConflictError, NotFoundError
from app.db.transaction import run_in_transaction
from app.models.booking import Booking, BookingStatus
from app.models.event import Event, EventStatus
from app.models.user import User, UserRole
from app.schemas.booking import BookingFilters
from app.services.event_service import lock_event
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


def _booking_not_found() -> NotFoundError:
    return NotFoundError("Booking not found", code="BOOKING_NOT_FOUND")


def create_booking(db: Session, event_id: uuid.UUID, attendee: User, quantity: int) -> Booking:
    max_quantity = get_settings().booking_max_tickets_per_booking
    if quantity > max_quantity:
        raise BusinessValidationError(
            f"A single booking may contain at most {max_quantity} tickets",
            code="QUANTITY_LIMIT_EXCEEDED",
        )
    attendee_id = attendee.id

    def operation() -> Booking:
        event = lock_event(db, event_id)
        now = utcnow()
        if event.status == EventStatus.CANCELLED:
            raise ConflictError("Event has been cancelled", code="EVENT_CANCELLED")
        if now >= event.start_at:
            raise ConflictError("Booking has closed for this event", code="EVENT_ALREADY_STARTED")
        remaining = event.capacity - event.tickets_sold
        if quantity > remaining:
            raise ConflictError(
                f"Not enough tickets available: {remaining} remaining",
                code="INSUFFICIENT_CAPACITY",
            )
        booking = Booking(
            id=uuid.uuid4(),
            user_id=attendee_id,
            event_id=event.id,
            quantity=quantity,
            unit_price=event.ticket_price,  # snapshot: later price edits do not rewrite history
            total_price=event.ticket_price * quantity,
            status=BookingStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )
        event.tickets_sold += quantity
        db.add(booking)
        db.flush()
        booking.event = event
        return booking

    booking = run_in_transaction(db, operation)
    logger.info(
        "Booking confirmed id=%s event=%s quantity=%d", booking.id, booking.event_id, quantity
    )
    return booking


def get_booking_for_viewer(db: Session, booking_id: uuid.UUID, viewer: User) -> Booking:
    """The attendee who booked, or the organizer who owns the event. Otherwise 404 (no leak)."""
    booking = db.scalar(
        select(Booking).options(joinedload(Booking.event)).where(Booking.id == booking_id)
    )
    if booking is None:
        raise _booking_not_found()
    is_owner = booking.user_id == viewer.id
    is_event_organizer = (
        viewer.role == UserRole.ORGANIZER and booking.event.organizer_id == viewer.id
    )
    if not (is_owner or is_event_organizer):
        raise _booking_not_found()
    return booking


def list_user_bookings(
    db: Session, user: User, filters: BookingFilters
) -> tuple[Sequence[Booking], int]:
    stmt = select(Booking).where(Booking.user_id == user.id)
    if filters.status:
        stmt = stmt.where(Booking.status == filters.status)
    if filters.created_from:
        stmt = stmt.where(Booking.created_at >= filters.created_from)
    if filters.created_to:
        stmt = stmt.where(Booking.created_at <= filters.created_to)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    bookings = db.scalars(
        stmt.options(joinedload(Booking.event))
        .order_by(Booking.created_at.desc(), Booking.id.desc())
        .offset((filters.page - 1) * filters.limit)
        .limit(filters.limit)
    ).all()
    return bookings, total


def cancel_booking(
    db: Session, booking_id: uuid.UUID, attendee: User, reason: str | None
) -> Booking:
    """Attendee cancellation. Allowed for CONFIRMED bookings until the cutoff before start."""
    cutoff = timedelta(hours=get_settings().booking_cancellation_cutoff_hours)
    attendee_id = attendee.id

    def operation() -> Booking:
        event_id = db.scalar(
            select(Booking.event_id).where(Booking.id == booking_id, Booking.user_id == attendee_id)
        )
        if event_id is None:
            raise _booking_not_found()
        # Lock order: event first, then booking (same order as every other writer).
        event = db.scalar(
            select(Event)
            .where(Event.id == event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        booking = db.scalar(
            select(Booking)
            .where(Booking.id == booking_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if event is None or booking is None:  # FKs are RESTRICT, so this cannot happen
            raise _booking_not_found()

        now = utcnow()
        if booking.status == BookingStatus.CANCELLED:
            raise ConflictError("Booking is already cancelled", code="BOOKING_ALREADY_CANCELLED")
        if booking.status != BookingStatus.CONFIRMED:
            raise ConflictError(
                f"Booking cannot be cancelled in status {booking.status.value}",
                code="BOOKING_NOT_CANCELLABLE",
            )
        if now >= event.start_at - cutoff:
            raise ConflictError(
                f"Bookings can only be cancelled up to {cutoff.total_seconds() / 3600:g} hours "
                "before the event starts",
                code="CANCELLATION_WINDOW_CLOSED",
            )
        booking.status = BookingStatus.CANCELLED
        booking.cancelled_at = now
        booking.updated_at = now
        booking.cancellation_reason = reason
        event.tickets_sold -= booking.quantity  # release inventory exactly once
        event.updated_at = now
        booking.event = event
        return booking

    booking = run_in_transaction(db, operation)
    logger.info("Booking cancelled id=%s event=%s", booking.id, booking.event_id)
    return booking
