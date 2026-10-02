"""Genuine concurrency tests.

A real Uvicorn server runs in a background thread; requests are fired from separate threads,
each with its own HTTP connection, released simultaneously by a barrier. Each request is served
by its own worker thread with its own database session/connection, so they genuinely compete
for the same PostgreSQL row. Nothing is mocked and nothing is sequential.
"""

import socket
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.models import Booking, BookingStatus, Event
from tests.conftest import UserCtx, insert_attendees, insert_event

pytestmark = pytest.mark.concurrency


@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def fire_concurrently(
    requests: list[Callable[[httpx.Client], httpx.Response]], base_url: str
) -> list[httpx.Response]:
    barrier = threading.Barrier(len(requests))

    def run(send: Callable[[httpx.Client], httpx.Response]) -> httpx.Response:
        with httpx.Client(base_url=base_url, timeout=30) as http:
            http.get("/health")  # open the connection before the race starts
            barrier.wait()
            return send(http)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(run, requests))


def book_request(
    event_id: uuid.UUID, user: UserCtx, quantity: int = 1
) -> Callable[[httpx.Client], httpx.Response]:
    return lambda http: http.post(
        f"/api/v1/events/{event_id}/bookings", json={"quantity": quantity}, headers=user.headers
    )


def db_state(db: Session, event_id: uuid.UUID) -> tuple[int, int, int]:
    """(tickets_sold, confirmed booking rows, confirmed ticket sum) read fresh from PostgreSQL."""
    db.expire_all()
    event = db.get(Event, event_id)
    assert event is not None
    rows, tickets = db.execute(
        select(func.count(Booking.id), func.coalesce(func.sum(Booking.quantity), 0)).where(
            Booking.event_id == event_id, Booking.status == BookingStatus.CONFIRMED
        )
    ).one()
    return event.tickets_sold, rows, tickets


def test_last_ticket_race_two_attendees(live_server: str, db: Session, organizer: UserCtx) -> None:
    event = insert_event(db, organizer.id, capacity=1)
    alice, bob = insert_attendees(db, 2)

    responses = fire_concurrently(
        [book_request(event.id, alice), book_request(event.id, bob)], live_server
    )

    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["code"] == "INSUFFICIENT_CAPACITY"
    assert db_state(db, event.id) == (1, 1, 1)


def test_last_ticket_race_99_of_100(live_server: str, db: Session, organizer: UserCtx) -> None:
    """The brief's example: capacity 100, 99 sold, two concurrent requests for the last ticket."""
    event = insert_event(db, organizer.id, capacity=100)
    filler, alice, bob = insert_attendees(db, 3)
    with httpx.Client(base_url=live_server) as http:  # 9 x 10 + 9 = 99 sold via the real API
        for quantity in [10] * 9 + [9]:
            assert book_request(event.id, filler, quantity)(http).status_code == 201
    assert db_state(db, event.id) == (99, 10, 99)

    responses = fire_concurrently(
        [book_request(event.id, alice), book_request(event.id, bob)], live_server
    )

    assert sorted(r.status_code for r in responses) == [201, 409]
    assert db_state(db, event.id) == (100, 11, 100)


@pytest.mark.parametrize("attempt", range(3))
def test_many_parallel_requests_never_oversell(
    live_server: str, db: Session, organizer: UserCtx, attempt: int
) -> None:
    capacity, contenders = 10, 30
    event = insert_event(db, organizer.id, capacity=capacity)
    users = insert_attendees(db, contenders)

    responses = fire_concurrently([book_request(event.id, u) for u in users], live_server)

    codes = Counter(r.status_code for r in responses)
    assert codes == {201: capacity, 409: contenders - capacity}, codes
    assert all(
        r.json()["code"] == "INSUFFICIENT_CAPACITY" for r in responses if r.status_code == 409
    )
    assert db_state(db, event.id) == (capacity, capacity, capacity)


def test_mixed_quantities_never_oversell(live_server: str, db: Session, organizer: UserCtx) -> None:
    event = insert_event(db, organizer.id, capacity=17)
    users = insert_attendees(db, 20)
    quantities = [(i % 4) + 1 for i in range(20)]  # 1..4 tickets each, 50 requested in total

    responses = fire_concurrently(
        [book_request(event.id, u, q) for u, q in zip(users, quantities, strict=True)], live_server
    )

    granted = sum(q for r, q in zip(responses, quantities, strict=True) if r.status_code == 201)
    assert {r.status_code for r in responses} <= {201, 409}
    sold, _, confirmed_tickets = db_state(db, event.id)
    assert sold == confirmed_tickets == granted <= 17
    # The service only refuses when the request truly does not fit, so at most 3 seats can be idle.
    assert granted >= 17 - 3


def test_concurrent_double_cancel_releases_once(
    live_server: str, db: Session, organizer: UserCtx
) -> None:
    event = insert_event(db, organizer.id, capacity=5)
    user = insert_attendees(db, 1)[0]
    with httpx.Client(base_url=live_server) as http:
        booking_id = book_request(event.id, user, 3)(http).json()["data"]["id"]

    cancel = lambda http: http.patch(f"/api/v1/bookings/{booking_id}/cancel", headers=user.headers)  # noqa: E731
    responses = fire_concurrently([cancel] * 6, live_server)

    assert sorted(r.status_code for r in responses) == [200] + [409] * 5
    assert db_state(db, event.id) == (0, 0, 0)


def test_event_cancellation_racing_bookings(
    live_server: str, db: Session, organizer: UserCtx
) -> None:
    event = insert_event(db, organizer.id, capacity=50)
    users = insert_attendees(db, 15)
    cancel = lambda http: http.post(f"/api/v1/events/{event.id}/cancel", headers=organizer.headers)  # noqa: E731

    responses = fire_concurrently(
        [book_request(event.id, u) for u in users] + [cancel], live_server
    )

    assert responses[-1].status_code == 200
    booked = [r for r in responses[:-1] if r.status_code == 201]
    rejected = [r for r in responses[:-1] if r.status_code != 201]
    assert all(r.json()["code"] == "EVENT_CANCELLED" for r in rejected)
    # Bookings that won the race before the cancellation are now REFUND_PENDING; none CONFIRMED.
    db.expire_all()
    statuses = Counter(db.scalars(select(Booking.status).where(Booking.event_id == event.id)))
    assert statuses == ({BookingStatus.REFUND_PENDING: len(booked)} if booked else {})
    assert responses[-1].json()["data"]["affected_bookings"] == len(booked)
