import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, Response, status

from app.api.dependencies import CurrentOrganizer, DbSession, RateLimitedAttendee
from app.schemas.booking import (
    BookingCreate,
    BookingFilters,
    BookingRead,
    EventSalesSummary,
    OrganizerBookingRead,
)
from app.schemas.common import DataResponse, PaginatedResponse, PaginationMeta, error_responses
from app.schemas.event import (
    EventCancelRequest,
    EventCancelResult,
    EventCreate,
    EventFilters,
    EventRead,
    EventUpdate,
)
from app.services import analytics_service, booking_service, event_service
from app.utils.time import utcnow

router = APIRouter(prefix="/events", tags=["Events"])

_OWNER_ERRORS = error_responses(401, 403, 404, 409, 422)


def _meta(page: int, limit: int, total: int) -> PaginationMeta:
    return PaginationMeta(page=page, limit=limit, total=total, pages=-(-total // limit))


@router.get(
    "",
    response_model=PaginatedResponse[EventRead],
    responses=error_responses(422),
    summary="Browse and search public events",
)
def list_events(
    db: DbSession, filters: Annotated[EventFilters, Query()]
) -> PaginatedResponse[EventRead]:
    """Public, no authentication. By default returns events that are not cancelled and have not
    ended, ordered by start time. `q` uses PostgreSQL full-text search (`websearch_to_tsquery`,
    English stemming) over title and description. Unknown query parameters return 422."""
    events, total = event_service.list_events(db, filters)
    now = utcnow()
    return PaginatedResponse(
        data=[EventRead.from_model(e, now) for e in events],
        meta=_meta(filters.page, filters.limit, total),
    )


@router.get(
    "/{event_id}",
    response_model=DataResponse[EventRead],
    responses=error_responses(404, 422),
    summary="Event details with remaining capacity",
)
def get_event(event_id: uuid.UUID, db: DbSession) -> DataResponse[EventRead]:
    """Public. `remaining_capacity` is informational; availability is re-checked under a row lock
    when booking."""
    return DataResponse(data=EventRead.from_model(event_service.get_event(db, event_id), utcnow()))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DataResponse[EventRead],
    responses=error_responses(401, 403, 422),
    summary="Create an event (organizer)",
)
def create_event(
    payload: EventCreate, organizer: CurrentOrganizer, db: DbSession
) -> DataResponse[EventRead]:
    """The authenticated organizer becomes the owner; `organizer_id`, `status` and `tickets_sold`
    are not accepted. `start_at` must be in the future and before `end_at`."""
    event = event_service.create_event(db, organizer, payload)
    return DataResponse(data=EventRead.from_model(event, utcnow()))


@router.patch(
    "/{event_id}",
    response_model=DataResponse[EventRead],
    responses=_OWNER_ERRORS,
    summary="Update an event (owner)",
)
def update_event(
    event_id: uuid.UUID, payload: EventUpdate, organizer: CurrentOrganizer, db: DbSession
) -> DataResponse[EventRead]:
    """Partial update. Rejected once the event has started or been cancelled. Capacity cannot go
    below `tickets_sold`. Price changes do not affect existing bookings (price is snapshotted)."""
    event = event_service.update_event(db, event_id, organizer, payload)
    return DataResponse(data=EventRead.from_model(event, utcnow()))


@router.delete(
    "/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_OWNER_ERRORS,
    summary="Delete an event (owner, soft delete)",
)
def delete_event(event_id: uuid.UUID, organizer: CurrentOrganizer, db: DbSession) -> None:
    """Allowed for cancelled events, or for events that have not started and have no active
    bookings (otherwise 409 `EVENT_HAS_ACTIVE_BOOKINGS`: cancel instead). Bookings are kept."""
    event_service.delete_event(db, event_id, organizer)


@router.post(
    "/{event_id}/cancel",
    response_model=DataResponse[EventCancelResult],
    responses=_OWNER_ERRORS,
    summary="Cancel an event (owner)",
)
def cancel_event(
    event_id: uuid.UUID,
    organizer: CurrentOrganizer,
    db: DbSession,
    payload: Annotated[EventCancelRequest | None, Body()] = None,
) -> DataResponse[EventCancelResult]:
    """Atomically marks the event CANCELLED and moves every CONFIRMED booking to
    REFUND_PENDING. No money is moved: there is no payment integration. Cancelling twice
    returns 409 `EVENT_ALREADY_CANCELLED`."""
    event, affected = event_service.cancel_event(
        db, event_id, organizer, payload.reason if payload else None
    )
    return DataResponse(
        data=EventCancelResult(
            event=EventRead.from_model(event, utcnow()), affected_bookings=affected
        )
    )


@router.post(
    "/{event_id}/bookings",
    status_code=status.HTTP_201_CREATED,
    response_model=DataResponse[BookingRead],
    responses=error_responses(401, 403, 404, 409, 422, 429),
    tags=["Bookings"],
    summary="Book tickets (attendee)",
)
def create_booking(
    event_id: uuid.UUID, payload: BookingCreate, attendee: RateLimitedAttendee, db: DbSession
) -> DataResponse[BookingRead]:
    """Books `quantity` tickets. The event row is locked (`SELECT ... FOR UPDATE`) while capacity
    is checked and `tickets_sold` is incremented, so concurrent requests can never oversell.

    Errors: 409 `INSUFFICIENT_CAPACITY`, `EVENT_CANCELLED`, `EVENT_ALREADY_STARTED`;
    422 `QUANTITY_LIMIT_EXCEEDED`; 429 when the per-user attempt limit is exceeded."""
    booking = booking_service.create_booking(db, event_id, attendee, payload.quantity)
    return DataResponse(data=BookingRead.from_model(booking))


@router.get(
    "/{event_id}/bookings",
    response_model=PaginatedResponse[OrganizerBookingRead],
    responses=_OWNER_ERRORS,
    tags=["Organizer"],
    summary="Bookings for one of your events (owner)",
)
def list_event_bookings(
    event_id: uuid.UUID,
    organizer: CurrentOrganizer,
    db: DbSession,
    filters: Annotated[BookingFilters, Query()],
) -> PaginatedResponse[OrganizerBookingRead]:
    """Includes attendee id, name and email only."""
    event = event_service.get_owned_event(db, event_id, organizer)
    bookings, total = analytics_service.event_bookings(db, event, filters)
    return PaginatedResponse(
        data=[OrganizerBookingRead.model_validate(b) for b in bookings],
        meta=_meta(filters.page, filters.limit, total),
    )


@router.get(
    "/{event_id}/summary",
    response_model=DataResponse[EventSalesSummary],
    responses=_OWNER_ERRORS,
    tags=["Organizer"],
    summary="Ticket sales summary (owner)",
)
def event_summary(
    event_id: uuid.UUID, organizer: CurrentOrganizer, db: DbSession
) -> DataResponse[EventSalesSummary]:
    """Values are booking totals, not money received: `gross_booking_value` (all bookings),
    `cancelled_booking_value`, `refund_pending_value`, `refunded_value`, and
    `net_booking_value` (still-confirmed bookings)."""
    event = event_service.get_owned_event(db, event_id, organizer)
    return DataResponse(data=analytics_service.event_sales_summary(db, event))
