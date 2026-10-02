import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Enum, String, Uuid, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.event import Event
    from app.models.refresh_token import RefreshToken


class UserRole(StrEnum):
    ATTENDEE = "ATTENDEE"
    ORGANIZER = "ORGANIZER"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=False, create_constraint=True, length=20)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    events: Mapped[list["Event"]] = relationship(back_populates="organizer", lazy="raise")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="user", lazy="raise")
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user", lazy="raise")
