"""Shared fixtures. Tests run against a real PostgreSQL database (default: the Docker Compose
`eventra_test` database on localhost:5433), migrated from scratch with Alembic once per session."""

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://eventra:eventra@localhost:5433/eventra_test"
)
# Must be set before any `app` import so the cached settings and engine use them.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "JWT_SECRET_KEY": "test-secret-key-0123456789abcdef0123456789abcdef",
        "BOOKING_RATE_LIMIT_PER_MINUTE": "0",
        "EVENT_STATUS_SWEEP_INTERVAL_SECONDS": "0",
        "LOG_LEVEL": "WARNING",
    }
)

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import booking_rate_limiter
from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.main import app
from app.models import Event, EventCategory, EventStatus, User, UserRole
from app.utils.time import utcnow

BACKEND_DIR = Path(__file__).resolve().parents[1]
PASSWORD = "SecurePassword123!"


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    if not (url.database or "").endswith("_test"):
        raise RuntimeError("Refusing to wipe a database whose name does not end in '_test'")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def clean_database(migrated_database: None) -> Iterator[None]:
    with SessionLocal() as db:
        db.execute(text("TRUNCATE bookings, refresh_tokens, events, users CASCADE"))
        db.commit()
    booking_rate_limiter.reset()
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


@dataclass
class UserCtx:
    id: str
    email: str
    role: str
    token: str
    refresh_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def make_user(client: TestClient) -> Callable[..., UserCtx]:
    def factory(
        role: str = "ATTENDEE", email: str | None = None, name: str = "Test User"
    ) -> UserCtx:
        email = email or f"{role.lower()}-{uuid.uuid4().hex[:10]}@example.com"
        response = client.post(
            "/api/v1/auth/register",
            json={"name": name, "email": email, "password": PASSWORD, "role": role},
        )
        assert response.status_code == 201, response.text
        login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        return UserCtx(
            id=data["user"]["id"],
            email=email.lower(),
            role=role,
            token=data["access_token"],
            refresh_token=data["refresh_token"],
        )

    return factory


@pytest.fixture
def organizer(make_user: Callable[..., UserCtx]) -> UserCtx:
    return make_user("ORGANIZER")


@pytest.fixture
def attendee(make_user: Callable[..., UserCtx]) -> UserCtx:
    return make_user("ATTENDEE")


def event_payload(**overrides: Any) -> dict[str, Any]:
    start = utcnow() + timedelta(days=7)
    payload: dict[str, Any] = {
        "title": "Python Workshop",
        "description": "Hands-on FastAPI and PostgreSQL workshop",
        "category": "WORKSHOP",
        "city": "Chennai",
        "venue": "Main Hall",
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=3)).isoformat(),
        "ticket_price": "250.00",
        "capacity": 100,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def make_event(client: TestClient, organizer: UserCtx) -> Callable[..., dict[str, Any]]:
    def factory(owner: UserCtx | None = None, **overrides: Any) -> dict[str, Any]:
        response = client.post(
            "/api/v1/events", json=event_payload(**overrides), headers=(owner or organizer).headers
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    return factory


def insert_event(db: Session, organizer_id: str, **overrides: Any) -> Event:
    """Insert directly, bypassing API validation (e.g. for events that have already started)."""
    start = overrides.pop("start_at", utcnow() + timedelta(days=7))
    values: dict[str, Any] = {
        "organizer_id": uuid.UUID(organizer_id),
        "title": "Direct Event",
        "description": "",
        "category": EventCategory.MEETUP,
        "city": "Chennai",
        "venue": "Hall",
        "start_at": start,
        "end_at": overrides.pop("end_at", start + timedelta(hours=2)),
        "ticket_price": Decimal("100.00"),
        "capacity": 10,
        "tickets_sold": 0,
        "status": EventStatus.UPCOMING,
    }
    values.update(overrides)
    event = Event(**values)
    db.add(event)
    db.commit()
    return event


def insert_attendees(db: Session, count: int) -> list[UserCtx]:
    """Bulk-create attendees with one shared hash; much faster than registering via the API."""
    password_hash = hash_password(PASSWORD)
    users = [
        User(
            name=f"Attendee {i}",
            email=f"bulk-{i}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=password_hash,
            role=UserRole.ATTENDEE,
        )
        for i in range(count)
    ]
    db.add_all(users)
    db.commit()
    return [
        UserCtx(
            id=str(u.id),
            email=u.email,
            role="ATTENDEE",
            token=create_access_token(u.id, u.role.value)[0],
            refresh_token="",
        )
        for u in users
    ]
