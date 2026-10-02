"""Event lifecycle.

Every mutation that reads or writes ``tickets_sold`` or ``status`` locks the event row with
``SELECT ... FOR UPDATE`` first. Booking creation/cancellation take the same lock, so all
inventory changes on one event are serialised.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, literal_column, select, update
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import (
    BusinessValidationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.db.transaction import run_in_transaction
from app.models.booking import Booking, BookingStatus
from app.models.event import EVENT_SEARCH_VECTOR_SQL, Event, EventStatus
from app.models.user import User
from app.schemas.event import EventCreate, EventFilters, EventSort, EventUpdate
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

_SORTS = {
    EventSort.START_ASC: (Event.start_at.asc(),),
    EventSort.START_DESC: (Event.start_at.desc(),),
    EventSort.PRICE_ASC: (Event.ticket_price.asc(), Event.start_at.asc()),
    EventSort.PRICE_DESC: (Event.ticket_price.desc(), Event.start_at.asc()),
    EventSort.CREATED_DESC: (Event.created_at.desc(),),
    EventSort.TITLE_ASC: (Event.title.asc(),),
}


def get_event(db: Session, event_id: uuid.UUID) -> Event:
    event = db.scalar(
        select(Event)
        .options(joinedload(Event.organizer))
        .where(Event.id == event_id, Event.deleted_at.is_(None))
    )
    if event is None:
        raise NotFoundError("Event not found", code="EVENT_NOT_FOUND")
    return event


def lock_event(db: Session, event_id: uuid.UUID) -> Event:
    """Load an event with a row-level lock, refreshing any stale identity-map copy."""
    event = db.scalar(
        select(Event)
        .where(Event.id == event_id, Event.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise NotFoundError("Event not found", code="EVENT_NOT_FOUND")
    return event


def ensure_owner(event: Event, user: User) -> None:
    if event.organizer_id != user.id:
        raise ForbiddenError("You do not own this event", code="NOT_EVENT_OWNER")


def get_owned_event(db: Session, event_id: uuid.UUID, organizer: User) -> Event:
    event = get_event(db, event_id)
    ensure_owner(event, organizer)
    return event


def create_event(db: Session, organizer: User, data: EventCreate) -> Event:
    if data.start_at <= utcnow():
        raise BusinessValidationError("start_at must be in the future", code="EVENT_START_IN_PAST")
    event = Event(
        organizer_id=organizer.id,  # owner always comes from the token, never the payload
        title=data.title,
        description=data.description,
        category=data.category,
        city=data.city,
        venue=data.venue,
        address=data.address,
        image_url=str(data.image_url) if data.image_url else None,
        start_at=data.start_at,
        end_at=data.end_at,
        ticket_price=data.ticket_price,
        capacity=data.capacity,
        tickets_sold=0,
        status=EventStatus.UPCOMING,
    )
    event.organizer = organizer
    db.add(event)
    db.commit()
    logger.info("Event created id=%s organizer=%s", event.id, organizer.id)
    return event


def _apply_filters(stmt: Select[tuple[Event]], filters: EventFilters) -> Select[tuple[Event]]:
    now = utcnow()
    stmt = stmt.where(Event.deleted_at.is_(None))
    if not filters.include_cancelled:
        stmt = stmt.where(Event.status != EventStatus.CANCELLED)
    if not filters.include_past:
        stmt = stmt.where(Event.end_at > now)
    if filters.city:
        stmt = stmt.where(func.lower(Event.city) == filters.city.strip().lower())
    if filters.category:
        stmt = stmt.where(Event.category == filters.category)
    if filters.start_from:
        stmt = stmt.where(Event.start_at >= filters.start_from)
    if filters.start_to:
        stmt = stmt.where(Event.start_at <= filters.start_to)
    if filters.min_price is not None:
        stmt = stmt.where(Event.ticket_price >= filters.min_price)
    if filters.max_price is not None:
        stmt = stmt.where(Event.ticket_price <= filters.max_price)
    if filters.q:
        # websearch_to_tsquery never raises on user input; the search term is a bound parameter.
        query = func.websearch_to_tsquery(literal_column("'english'::regconfig"), filters.q)
        stmt = stmt.where(literal_column(EVENT_SEARCH_VECTOR_SQL).op("@@")(query))
    return stmt


def list_events(db: Session, filters: EventFilters) -> tuple[Sequence[Event], int]:
    stmt = _apply_filters(select(Event), filters)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    events = db.scalars(
        stmt.options(joinedload(Event.organizer))
        .order_by(*_SORTS[filters.sort], Event.id.asc())  # id tiebreaker => stable pages
        .offset((filters.page - 1) * filters.limit)
        .limit(filters.limit)
    ).all()
    return events, total


def update_event(db: Session, event_id: uuid.UUID, organizer: User, data: EventUpdate) -> Event:
    changes = data.changes()

    def operation() -> Event:
        event = lock_event(db, event_id)
        ensure_owner(event, organizer)
        now = utcnow()
        if event.status == EventStatus.CANCELLED:
            raise ConflictError("Cancelled events cannot be edited", code="EVENT_CANCELLED")
        if now >= event.start_at:
            raise ConflictError(
                "Events cannot be edited once started", code="EVENT_ALREADY_STARTED"
            )

        start_at = changes.get("start_at", event.start_at)
        end_at = changes.get("end_at", event.end_at)
        if "start_at" in changes and start_at <= now:
            raise BusinessValidationError(
                "start_at must be in the future", code="EVENT_START_IN_PAST"
            )
        if end_at <= start_at:
            raise BusinessValidationError(
                "end_at must be after start_at", code="INVALID_DATE_RANGE"
            )
        if "capacity" in changes and changes["capacity"] < event.tickets_sold:
            raise ConflictError(
                f"Capacity cannot be lower than the {event.tickets_sold} tickets already sold",
                code="CAPACITY_BELOW_TICKETS_SOLD",
            )
        for field, value in changes.items():
            setattr(event, field, value)
        event.updated_at = now
        return event

    event = run_in_transaction(db, operation)
    logger.info("Event updated id=%s fields=%s", event.id, sorted(changes))
    return get_event(db, event.id)


def cancel_event(
    db: Session, event_id: uuid.UUID, organizer: User, reason: str | None
) -> tuple[Event, int]:
    """Cancel an event and move every CONFIRMED booking to REFUND_PENDING, atomically.

    ``tickets_sold`` is left unchanged: those tickets are owed a refund, not resold.
    """

    def operation() -> tuple[Event, int]:
        event = lock_event(db, event_id)
        ensure_owner(event, organizer)
        now = utcnow()
        if event.status == EventStatus.CANCELLED:
            raise ConflictError("Event is already cancelled", code="EVENT_ALREADY_CANCELLED")
        if now >= event.end_at:
            raise ConflictError("Event has already ended", code="EVENT_ALREADY_ENDED")
        event.status = EventStatus.CANCELLED
        event.cancelled_at = now
        event.updated_at = now
        result = db.execute(
            update(Booking)
            .where(Booking.event_id == event.id, Booking.status == BookingStatus.CONFIRMED)
            .values(
                status=BookingStatus.REFUND_PENDING,
                cancelled_at=now,
                updated_at=now,
                cancellation_reason=reason or "Event cancelled by organizer",
            )
            .execution_options(synchronize_session=False)
        )
        return event, result.rowcount

    event, affected = run_in_transaction(db, operation)
    logger.info("Event cancelled id=%s refund_pending_bookings=%d", event.id, affected)
    return get_event(db, event.id), affected


def delete_event(db: Session, event_id: uuid.UUID, organizer: User) -> None:
    """Soft-delete. Allowed for cancelled events, or unstarted events with no live bookings.

    Booking rows are never removed, so attendees keep their history.
    """

    def operation() -> None:
        event = lock_event(db, event_id)
        ensure_owner(event, organizer)
        now = utcnow()
        if event.status != EventStatus.CANCELLED:
            if now >= event.start_at:
                raise ConflictError(
                    "Started events cannot be deleted", code="EVENT_ALREADY_STARTED"
                )
            if event.tickets_sold > 0:
                raise ConflictError(
                    "Event has active bookings; cancel the event instead",
                    code="EVENT_HAS_ACTIVE_BOOKINGS",
                )
        event.deleted_at = now
        event.updated_at = now

    run_in_transaction(db, operation)
    logger.info("Event soft-deleted id=%s", event_id)
