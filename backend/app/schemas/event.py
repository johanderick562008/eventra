import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.event import Event, EventCategory, EventStatus
from app.schemas.user import UserPublic

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
City = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]
Venue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]
Address = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Price = Annotated[Decimal, Field(ge=0, le=Decimal("1000000"), max_digits=10, decimal_places=2)]
Capacity = Annotated[int, Field(strict=True, ge=1, le=100_000)]

_EVENT_EXAMPLE: dict[str, Any] = {
    "title": "Python Workshop",
    "description": "Hands-on FastAPI and PostgreSQL workshop.",
    "category": "WORKSHOP",
    "city": "Chennai",
    "venue": "IIT Madras Research Park",
    "address": "Kanagam Road, Taramani",
    "image_url": "https://example.com/workshop.png",
    "start_at": "2026-11-15T10:00:00+05:30",
    "end_at": "2026-11-15T17:00:00+05:30",
    "ticket_price": "499.00",
    "capacity": 100,
}


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": _EVENT_EXAMPLE})

    title: Title
    description: Description = ""
    category: EventCategory
    city: City
    venue: Venue
    address: Address | None = None
    image_url: HttpUrl | None = None
    start_at: AwareDatetime = Field(description="Must be in the future; include a UTC offset")
    end_at: AwareDatetime
    ticket_price: Price
    capacity: Capacity

    @field_validator("start_at", "end_at")
    @classmethod
    def _normalise_to_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


_NULLABLE_FIELDS = frozenset({"address", "image_url"})


class EventUpdate(BaseModel):
    """Partial update. `status`, `tickets_sold` and `organizer_id` cannot be set here."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"capacity": 150, "ticket_price": "549.00"}}
    )

    title: Title | None = None
    description: Description | None = None
    category: EventCategory | None = None
    city: City | None = None
    venue: Venue | None = None
    address: Address | None = None
    image_url: HttpUrl | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    ticket_price: Price | None = None
    capacity: Capacity | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def _normalise_to_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        explicit_nulls = [
            name
            for name in self.model_fields_set
            if getattr(self, name) is None and name not in _NULLABLE_FIELDS
        ]
        if explicit_nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(sorted(explicit_nulls))}")
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(exclude_unset=True)
        if data.get("image_url") is not None:
            data["image_url"] = str(data["image_url"])
        return data


class EventRead(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    category: EventCategory
    city: str
    venue: str
    address: str | None
    image_url: str | None
    start_at: datetime
    end_at: datetime
    ticket_price: Decimal = Field(description='Serialised as a decimal string, e.g. "499.00"')
    capacity: int
    tickets_sold: int
    remaining_capacity: int
    status: EventStatus = Field(description="Derived from the clock; CANCELLED is stored")
    is_bookable: bool
    organizer: UserPublic
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, event: Event, now: datetime) -> "EventRead":
        return cls(
            id=event.id,
            title=event.title,
            description=event.description,
            category=event.category,
            city=event.city,
            venue=event.venue,
            address=event.address,
            image_url=event.image_url,
            start_at=event.start_at,
            end_at=event.end_at,
            ticket_price=event.ticket_price,
            capacity=event.capacity,
            tickets_sold=event.tickets_sold,
            remaining_capacity=event.remaining_capacity,
            status=event.effective_status(now),
            is_bookable=event.is_bookable(now),
            organizer=UserPublic.model_validate(event.organizer),
            created_at=event.created_at,
            updated_at=event.updated_at,
        )


class EventCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class EventCancelResult(BaseModel):
    event: EventRead
    affected_bookings: int = Field(description="Bookings moved from CONFIRMED to REFUND_PENDING")


class EventSort(StrEnum):
    START_ASC = "start_at"
    START_DESC = "-start_at"
    PRICE_ASC = "ticket_price"
    PRICE_DESC = "-ticket_price"
    CREATED_DESC = "-created_at"
    TITLE_ASC = "title"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class EventFilters(BaseModel):
    """Query parameters for `GET /events`. Naive datetimes are interpreted as UTC."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Full-text search on title and description",
    )
    city: str | None = Field(
        default=None, min_length=1, max_length=100, description="Case-insensitive exact match"
    )
    category: EventCategory | None = None
    start_from: datetime | None = Field(default=None, description="Events starting at or after")
    start_to: datetime | None = Field(default=None, description="Events starting at or before")
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, ge=0)
    include_past: bool = Field(default=False, description="Include events that have ended")
    include_cancelled: bool = Field(default=False, description="Include cancelled events")
    sort: EventSort = EventSort.START_ASC
    page: int = Field(default=1, ge=1, le=10_000)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("start_from", "start_to")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price must not exceed max_price")
        if self.start_from and self.start_to and self.start_from > self.start_to:
            raise ValueError("start_from must not be after start_to")
        return self


class OrganizerEventRead(BaseModel):
    id: uuid.UUID
    title: str
    status: EventStatus
    start_at: datetime
    end_at: datetime
    ticket_price: Decimal
    capacity: int
    tickets_sold: int
    remaining_capacity: int
    confirmed_bookings: int
    total_bookings: int
