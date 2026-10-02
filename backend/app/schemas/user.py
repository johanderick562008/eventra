import re
import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)

from app.models.user import UserRole

PASSWORD_RULES = (
    "8-128 characters with at least one lowercase letter, one uppercase letter, "  # noqa: S105
    "one digit and one symbol"
)


def _check_password_strength(value: str) -> str:
    checks = (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    if not all(re.search(pattern, value) for pattern in checks):
        raise ValueError(f"Password must be {PASSWORD_RULES}")
    return value


def _normalise_email(value: str) -> str:
    return value.strip().lower()


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Email = Annotated[EmailStr, AfterValidator(_normalise_email)]
StrongPassword = Annotated[
    str, Field(min_length=8, max_length=128), AfterValidator(_check_password_strength)
]


class UserRead(BaseModel):
    """The caller's own profile."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime


class UserPublic(BaseModel):
    """What any authenticated user may see about an organizer."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    role: UserRole


class UserUpdate(BaseModel):
    """Self-service profile update. `role`, `email` and `is_active` are deliberately absent:
    unknown fields are rejected, which blocks mass-assignment and role escalation."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    current_password: str | None = Field(default=None, max_length=128)
    new_password: StrongPassword | None = None

    @model_validator(mode="after")
    def _password_change_needs_current(self) -> Self:
        if self.new_password is not None and not self.current_password:
            raise ValueError("current_password is required to set new_password")
        if self.name is None and self.new_password is None:
            raise ValueError("Provide at least one of: name, new_password")
        return self
