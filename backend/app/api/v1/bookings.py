import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query

from app.api.dependencies import CurrentAttendee, CurrentUser, DbSession
from app.schemas.booking import BookingCancelRequest, BookingFilters, BookingRead
from app.schemas.common import DataResponse, PaginatedResponse, PaginationMeta, error_responses
from app.services import booking_service
from app.utils.time import utcnow

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.get(
    "/me",
    response_model=PaginatedResponse[BookingRead],
    responses=error_responses(401, 403, 422),
    summary="My booking history (attendee)",
)
def my_bookings(
    attendee: CurrentAttendee, db: DbSession, filters: Annotated[BookingFilters, Query()]
) -> PaginatedResponse[BookingRead]:
    """Newest first. Filter by `status` and `created_from` / `created_to`."""
    bookings, total = booking_service.list_user_bookings(db, attendee, filters)
    now = utcnow()
    return PaginatedResponse(
        data=[BookingRead.from_model(b, now) for b in bookings],
        meta=PaginationMeta(
            page=filters.page, limit=filters.limit, total=total, pages=-(-total // filters.limit)
        ),
    )


@router.get(
    "/{booking_id}",
    response_model=DataResponse[BookingRead],
    responses=error_responses(401, 404),
    summary="Booking details",
)
def get_booking(
    booking_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> DataResponse[BookingRead]:
    """Visible to the attendee who made it and to the organizer of the event. Anyone else gets
    404, so booking ids cannot be probed."""
    return DataResponse(
        data=BookingRead.from_model(booking_service.get_booking_for_viewer(db, booking_id, user))
    )


@router.patch(
    "/{booking_id}/cancel",
    response_model=DataResponse[BookingRead],
    responses=error_responses(401, 403, 404, 409, 422),
    summary="Cancel my booking (attendee)",
)
def cancel_booking(
    booking_id: uuid.UUID,
    attendee: CurrentAttendee,
    db: DbSession,
    payload: Annotated[BookingCancelRequest | None, Body()] = None,
) -> DataResponse[BookingRead]:
    """Allowed only for CONFIRMED bookings, until `BOOKING_CANCELLATION_CUTOFF_HOURS` (default
    24) before the event starts. Tickets return to inventory. Errors: 409
    `BOOKING_ALREADY_CANCELLED`, `BOOKING_NOT_CANCELLABLE`, `CANCELLATION_WINDOW_CLOSED`."""
    booking = booking_service.cancel_booking(
        db, booking_id, attendee, payload.reason if payload else None
    )
    return DataResponse(data=BookingRead.from_model(booking))
