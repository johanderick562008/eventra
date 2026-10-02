"""Schema/migration alignment, database constraints, reconciliation and the status sweep."""

import random
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.session import engine
from app.models import Booking, BookingStatus, Event, EventStatus, User, UserRole
from app.tasks.event_status import sweep_event_statuses
from app.utils.time import utcnow
from tests.conftest import UserCtx, insert_attendees, insert_event


def test_migrations_match_models() -> None:
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == []


def test_db_rejects_overselling(db: Session, organizer: UserCtx) -> None:
    event = insert_event(db, organizer.id, capacity=2)
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("UPDATE events SET tickets_sold = 3 WHERE id = :id"), {"id": event.id})
        db.commit()
    db.rollback()
    assert "ck_events_tickets_sold_within_capacity" in str(exc.value)


def test_db_rejects_inconsistent_booking_total(
    db: Session, organizer: UserCtx, attendee: UserCtx
) -> None:
    event = insert_event(db, organizer.id)
    db.add(
        Booking(
            user_id=uuid.UUID(attendee.id),
            event_id=event.id,
            quantity=2,
            unit_price=Decimal("10.00"),
            total_price=Decimal("5.00"),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_db_rejects_uppercase_email_and_hard_delete_of_history(
    db: Session, organizer: UserCtx, attendee: UserCtx, client: TestClient
) -> None:
    db.add(User(name="x", email="Upper@Example.com", password_hash="x", role=UserRole.ATTENDEE))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    event = insert_event(db, organizer.id)
    client.post(
        f"/api/v1/events/{event.id}/bookings", json={"quantity": 1}, headers=attendee.headers
    )
    with pytest.raises(IntegrityError):  # ON DELETE RESTRICT protects booking history
        db.execute(text("DELETE FROM events WHERE id = :id"), {"id": event.id})
        db.commit()
    db.rollback()


def _assert_inventory_consistent(db: Session) -> None:
    db.expire_all()
    for event in db.scalars(select(Event)):
        held = db.scalar(
            select(func.coalesce(func.sum(Booking.quantity), 0)).where(
                Booking.event_id == event.id, Booking.status != BookingStatus.CANCELLED
            )
        )
        assert event.tickets_sold == held, event.id
        assert 0 <= event.tickets_sold <= event.capacity


def test_reconciliation_after_random_operations(
    client: TestClient, db: Session, organizer: UserCtx
) -> None:
    rng = random.Random(1234)  # noqa: S311
    events = [insert_event(db, organizer.id, capacity=rng.randint(3, 8)) for _ in range(3)]
    users = insert_attendees(db, 5)
    bookings: list[tuple[str, dict]] = []
    for _ in range(60):
        user = rng.choice(users)
        if bookings and rng.random() < 0.35:
            booking_id, headers = rng.choice(bookings)
            client.patch(f"/api/v1/bookings/{booking_id}/cancel", headers=headers)
        else:
            event = rng.choice(events)
            response = client.post(
                f"/api/v1/events/{event.id}/bookings",
                json={"quantity": rng.randint(1, 3)},
                headers=user.headers,
            )
            assert response.status_code in (201, 409)
            if response.status_code == 201:
                bookings.append((response.json()["data"]["id"], user.headers))
    client.post(f"/api/v1/events/{events[0].id}/cancel", headers=organizer.headers)
    _assert_inventory_consistent(db)


def test_status_sweep(db: Session, organizer: UserCtx) -> None:
    now = utcnow()
    ended = insert_event(
        db, organizer.id, start_at=now - timedelta(days=1), end_at=now - timedelta(hours=1)
    )
    running = insert_event(
        db, organizer.id, start_at=now - timedelta(minutes=10), end_at=now + timedelta(hours=1)
    )
    future = insert_event(db, organizer.id)
    cancelled = insert_event(
        db,
        organizer.id,
        start_at=now - timedelta(days=1),
        end_at=now - timedelta(hours=1),
        status=EventStatus.CANCELLED,
    )
    assert sweep_event_statuses() == (1, 1)
    db.expire_all()
    assert db.get(Event, ended.id).status == EventStatus.COMPLETED  # type: ignore[union-attr]
    assert db.get(Event, running.id).status == EventStatus.ONGOING  # type: ignore[union-attr]
    assert db.get(Event, future.id).status == EventStatus.UPCOMING  # type: ignore[union-attr]
    assert db.get(Event, cancelled.id).status == EventStatus.CANCELLED  # type: ignore[union-attr]
    assert sweep_event_statuses() == (0, 0)  # idempotent
