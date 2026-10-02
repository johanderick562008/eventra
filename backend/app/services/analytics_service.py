"""Organizer dashboard queries: per-event sales summaries and booking lists."""

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.booking import Booking, BookingStatus
from app.models.event import Event
from app.models.user import User
from app.schemas.booking import BookingFilters, EventSalesSummary
from app.schemas.common import Pagination
from app.schemas.event import OrganizerEventRead
from app.utils.time import utcnow

_ZERO = Decimal("0.00")


def event_sales_summary(db: Session, event: Event) -> EventSalesSummary:
    rows = db.execute(
        select(
            Booking.status,
            func.count(Booking.id),
            func.coalesce(func.sum(Booking.quantity), 0),
            func.coalesce(func.sum(Booking.total_price), 0),
        )
        .where(Booking.event_id == event.id)
        .group_by(Booking.status)
    ).all()
    stats: dict[BookingStatus, tuple[int, int, Decimal]] = {
        status: (count, int(tickets), Decimal(value)) for status, count, tickets, value in rows
    }

    def get(status: BookingStatus) -> tuple[int, int, Decimal]:
        return stats.get(status, (0, 0, _ZERO))

    confirmed, cancelled = get(BookingStatus.CONFIRMED), get(BookingStatus.CANCELLED)
    refund_pending, refunded = get(BookingStatus.REFUND_PENDING), get(BookingStatus.REFUNDED)
    gross = sum((value for _, _, value in stats.values()), _ZERO)

    return EventSalesSummary(
        event_id=event.id,
        title=event.title,
        status=event.effective_status(utcnow()),
        capacity=event.capacity,
        tickets_sold=event.tickets_sold,
        remaining_capacity=event.remaining_capacity,
        confirmed_bookings=confirmed[0],
        confirmed_tickets=confirmed[1],
        cancelled_bookings=cancelled[0],
        cancelled_tickets=cancelled[1],
        refund_pending_bookings=refund_pending[0],
        refund_pending_tickets=refund_pending[1],
        refunded_bookings=refunded[0],
        gross_booking_value=gross,
        cancelled_booking_value=cancelled[2],
        refund_pending_value=refund_pending[2],
        refunded_value=refunded[2],
        net_booking_value=confirmed[2],
    )


def organizer_events(
    db: Session, organizer: User, pagination: Pagination
) -> tuple[list[OrganizerEventRead], int]:
    base = select(Event).where(Event.organizer_id == organizer.id, Event.deleted_at.is_(None))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    events = db.scalars(
        base.order_by(Event.start_at.desc(), Event.id)
        .offset(pagination.offset)
        .limit(pagination.limit)
    ).all()

    counts: dict[object, dict[BookingStatus, int]] = defaultdict(dict)
    if events:
        for event_id, status, count in db.execute(
            select(Booking.event_id, Booking.status, func.count(Booking.id))
            .where(Booking.event_id.in_([e.id for e in events]))
            .group_by(Booking.event_id, Booking.status)
        ):
            counts[event_id][status] = count

    now = utcnow()
    items = [
        OrganizerEventRead(
            id=e.id,
            title=e.title,
            status=e.effective_status(now),
            start_at=e.start_at,
            end_at=e.end_at,
            ticket_price=e.ticket_price,
            capacity=e.capacity,
            tickets_sold=e.tickets_sold,
            remaining_capacity=e.remaining_capacity,
            confirmed_bookings=counts[e.id].get(BookingStatus.CONFIRMED, 0),
            total_bookings=sum(counts[e.id].values()),
        )
        for e in events
    ]
    return items, total


def event_bookings(
    db: Session, event: Event, filters: BookingFilters
) -> tuple[Sequence[Booking], int]:
    stmt = select(Booking).where(Booking.event_id == event.id)
    if filters.status:
        stmt = stmt.where(Booking.status == filters.status)
    if filters.created_from:
        stmt = stmt.where(Booking.created_at >= filters.created_from)
    if filters.created_to:
        stmt = stmt.where(Booking.created_at <= filters.created_to)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    bookings = db.scalars(
        stmt.options(joinedload(Booking.user))
        .order_by(Booking.created_at.desc(), Booking.id.desc())
        .offset((filters.page - 1) * filters.limit)
        .limit(filters.limit)
    ).all()
    return bookings, total
