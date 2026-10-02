import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.user import User


class EventStatus(StrEnum):
    UPCOMING = "UPCOMING"
    ONGOING = "ONGOING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class EventCategory(StrEnum):
    CONFERENCE = "CONFERENCE"
    WORKSHOP = "WORKSHOP"
    MEETUP = "MEETUP"
    CONCERT = "CONCERT"
    SPORTS = "SPORTS"
    THEATRE = "THEATRE"
    FESTIVAL = "FESTIVAL"
    EXHIBITION = "EXHIBITION"
    OTHER = "OTHER"


# Must match the GIN index expression exactly so PostgreSQL can use the index for search.
EVENT_SEARCH_VECTOR_SQL = (
    "to_tsvector('english'::regconfig, (title::text || ' '::text) || description)"
)


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint("tickets_sold >= 0", name="tickets_sold_non_negative"),
        # Database-level backstop against overselling, independent of application logic.
        CheckConstraint("tickets_sold <= capacity", name="tickets_sold_within_capacity"),
        CheckConstraint("ticket_price >= 0", name="ticket_price_non_negative"),
        CheckConstraint("end_at > start_at", name="end_after_start"),
        Index("ix_events_status_start_at", "status", "start_at"),
        Index("ix_events_city_lower", func.lower(text("city"))),
        Index("ix_events_search", text(EVENT_SEARCH_VECTOR_SQL), postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organizer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    category: Mapped[EventCategory] = mapped_column(
        Enum(
            EventCategory,
            name="event_category",
            native_enum=False,
            create_constraint=True,
            length=20,
        ),
        index=True,
    )
    city: Mapped[str] = mapped_column(String(100))
    venue: Mapped[str] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ticket_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), index=True)
    capacity: Mapped[int] = mapped_column(Integer)
    tickets_sold: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[EventStatus] = mapped_column(
        Enum(
            EventStatus, name="event_status", native_enum=False, create_constraint=True, length=20
        ),
        default=EventStatus.UPCOMING,
        server_default=EventStatus.UPCOMING.value,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organizer: Mapped["User"] = relationship(back_populates="events", lazy="raise")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="event", lazy="raise")

    @property
    def remaining_capacity(self) -> int:
        return self.capacity - self.tickets_sold

    def effective_status(self, now: datetime) -> EventStatus:
        """Status derived from the clock, so it is correct even if the background sweep lags."""
        if self.status == EventStatus.CANCELLED:
            return EventStatus.CANCELLED
        if now >= self.end_at:
            return EventStatus.COMPLETED
        if now >= self.start_at:
            return EventStatus.ONGOING
        return EventStatus.UPCOMING

    def is_bookable(self, now: datetime) -> bool:
        return (
            self.deleted_at is None
            and self.status != EventStatus.CANCELLED
            and now < self.start_at
            and self.remaining_capacity > 0
        )
