import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.booking import Booking, BookingStatus
from app.models.event import EventStatus
from app.utils.time import utcnow


class BookingCreate(BaseModel):
    """Only the quantity is accepted. Price and totals are always computed by the server."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {"quantity": 2}})

    quantity: int = Field(
        strict=True, ge=1, description="Tickets to book (server-side maximum applies)"
    )


class BookingCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class BookingEventSummary(BaseModel):
    id: uuid.UUID
    title: str
    city: str
    venue: str
    start_at: datetime
    end_at: datetime
    status: EventStatus


class BookingRead(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    quantity: int
    unit_price: Decimal
    total_price: Decimal
    status: BookingStatus
    created_at: datetime
    cancelled_at: datetime | None
    cancellation_reason: str | None
    event: BookingEventSummary

    @classmethod
    def from_model(cls, booking: Booking, now: datetime | None = None) -> "BookingRead":
        now = now or utcnow()
        event = booking.event
        return cls(
            id=booking.id,
            event_id=booking.event_id,
            quantity=booking.quantity,
            unit_price=booking.unit_price,
            total_price=booking.total_price,
            status=booking.status,
            created_at=booking.created_at,
            cancelled_at=booking.cancelled_at,
            cancellation_reason=booking.cancellation_reason,
            event=BookingEventSummary(
                id=event.id,
                title=event.title,
                city=event.city,
                venue=event.venue,
                start_at=event.start_at,
                end_at=event.end_at,
                status=event.effective_status(now),
            ),
        )


class AttendeeSummary(BaseModel):
    """Minimum attendee data an organizer needs to run the event (check-in, contact)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str


class OrganizerBookingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    quantity: int
    unit_price: Decimal
    total_price: Decimal
    status: BookingStatus
    created_at: datetime
    cancelled_at: datetime | None
    attendee: AttendeeSummary = Field(validation_alias="user")


class BookingFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BookingStatus | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    page: int = Field(default=1, ge=1, le=10_000)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from must not be after created_to")
        return self


class EventSalesSummary(BaseModel):
    """Booking totals. No payment provider is integrated, so these are *booked values*,
    not money received or refunded."""

    event_id: uuid.UUID
    title: str
    status: EventStatus
    capacity: int
    tickets_sold: int = Field(
        description="Tickets held by CONFIRMED, REFUND_PENDING and REFUNDED bookings"
    )
    remaining_capacity: int
    confirmed_bookings: int
    confirmed_tickets: int
    cancelled_bookings: int
    cancelled_tickets: int
    refund_pending_bookings: int
    refund_pending_tickets: int
    refunded_bookings: int
    gross_booking_value: Decimal = Field(
        description="Sum of total_price over all bookings ever made"
    )
    cancelled_booking_value: Decimal = Field(description="Value of attendee-cancelled bookings")
    refund_pending_value: Decimal = Field(description="Value owed back after event cancellation")
    refunded_value: Decimal
    net_booking_value: Decimal = Field(description="Value of bookings still CONFIRMED")
