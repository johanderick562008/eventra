from pydantic import BaseModel, ConfigDict, Field

from app.models.user import UserRole
from app.schemas.user import Email, Name, StrongPassword, UserRead


class RegisterRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "name": "Johan",
                "email": "johan@example.com",
                "password": "SecurePassword123!",
                "role": "ATTENDEE",
            }
        },
    )

    name: Name
    email: Email
    password: StrongPassword
    role: UserRole = Field(description="ATTENDEE or ORGANIZER. Fixed after registration.")


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"email": "johan@example.com", "password": "SecurePassword123!"}
        },
    )

    email: Email
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=20, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(description="Access token lifetime in seconds")
    user: UserRead
