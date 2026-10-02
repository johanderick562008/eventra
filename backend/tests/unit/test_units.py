"""Pure unit tests: no HTTP. (Importing `app` still requires the test settings from conftest.)"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import INSECURE_DEV_SECRET, Settings
from app.core.exceptions import UnauthorizedError
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models import Event, EventStatus
from app.schemas.auth import RegisterRequest
from app.schemas.booking import BookingCreate
from app.schemas.event import EventFilters, EventUpdate


def test_password_hashing() -> None:
    hashed = hash_password("SecurePassword123!")
    assert hashed.startswith("$argon2id$")
    assert hashed != hash_password("SecurePassword123!")  # salted
    assert verify_password("SecurePassword123!", hashed)
    assert not verify_password("securepassword123!", hashed)
    assert not verify_password("anything", "not-a-hash")


def test_access_token_roundtrip_and_tamper() -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, "ATTENDEE")
    claims = decode_access_token(token)
    assert claims["sub"] == str(user_id) and claims["role"] == "ATTENDEE"
    assert expires_in > 0
    with pytest.raises(UnauthorizedError):
        decode_access_token(token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1])


def test_refresh_tokens_are_random_and_hashed() -> None:
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b and len(a) >= 60
    assert len(hash_refresh_token(a)) == 64 and hash_refresh_token(a) != a


def test_register_schema_normalises_email() -> None:
    req = RegisterRequest(
        name=" Ann ", email="Ann@Example.COM", password="Abcdef1!", role="ORGANIZER"
    )
    assert req.email == "ann@example.com" and req.name == "Ann"


@pytest.mark.parametrize(
    "password", ["Abc1!", "abcdefg1!", "ABCDEFG1!", "Abcdefgh!", "Abcdefg12", "A1!" + "a" * 130]
)
def test_weak_passwords(password: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(name="x", email="x@example.com", password=password, role="ATTENDEE")


def test_booking_quantity_is_strict() -> None:
    assert BookingCreate(quantity=2).quantity == 2
    for bad in (0, "2", 2.0, True):
        with pytest.raises(ValidationError):
            BookingCreate.model_validate({"quantity": bad})


def test_event_update_rejects_null_required_fields() -> None:
    with pytest.raises(ValidationError):
        EventUpdate.model_validate({"title": None})
    assert EventUpdate.model_validate({"address": None}).changes() == {"address": None}


def test_event_filters_treat_naive_dates_as_utc() -> None:
    filters = EventFilters.model_validate({"start_from": "2026-10-10"})
    assert filters.start_from == datetime(2026, 10, 10, tzinfo=UTC)


def test_effective_status() -> None:
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    event = Event(
        start_at=now + timedelta(hours=1),
        end_at=now + timedelta(hours=3),
        status=EventStatus.UPCOMING,
        capacity=1,
        tickets_sold=0,
        ticket_price=Decimal(0),
    )
    assert event.effective_status(now) == EventStatus.UPCOMING and event.is_bookable(now)
    assert event.effective_status(now + timedelta(hours=2)) == EventStatus.ONGOING
    assert not event.is_bookable(now + timedelta(hours=1))
    assert event.effective_status(now + timedelta(hours=3)) == EventStatus.COMPLETED
    event.tickets_sold = 1
    assert not event.is_bookable(now)
    event.status = EventStatus.CANCELLED
    assert event.effective_status(now) == EventStatus.CANCELLED


def test_rate_limiter_window() -> None:
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
    assert limiter.hit("u")[0] and limiter.hit("u")[0]
    allowed, retry_after = limiter.hit("u")
    assert not allowed and 0 < retry_after <= 61
    assert limiter.hit("other")[0]
    assert SlidingWindowRateLimiter(limit=0).hit("u") == (True, 0)


def test_settings_validation() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret_key=INSECURE_DEV_SECRET)
    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret_key="short")
    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret_key="x" * 40, frontend_origins="*")
    s = Settings(
        database_url="postgres://u:p@h:5432/d", frontend_origins="https://a.com/, https://b.com"
    )
    assert s.database_url == "postgresql+psycopg://u:p@h:5432/d"
    assert s.frontend_origins == ["https://a.com", "https://b.com"]
