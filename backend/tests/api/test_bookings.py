import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import booking_rate_limiter
from app.models import Event
from app.utils.time import utcnow
from tests.conftest import UserCtx, insert_event


def book(client: TestClient, user: UserCtx, event_id: str, quantity: Any = 1):
    return client.post(
        f"/api/v1/events/{event_id}/bookings", json={"quantity": quantity}, headers=user.headers
    )


def sold(client: TestClient, event_id: str) -> int:
    return client.get(f"/api/v1/events/{event_id}").json()["data"]["tickets_sold"]


def test_single_ticket_booking(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event(ticket_price="250.00")
    response = book(client, attendee, event["id"])
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "CONFIRMED"
    assert data["quantity"] == 1
    assert data["unit_price"] == "250.00" and data["total_price"] == "250.00"
    assert data["event"]["id"] == event["id"]
    assert sold(client, event["id"]) == 1


def test_multi_ticket_booking_and_price_snapshot(
    client: TestClient, organizer: UserCtx, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event(ticket_price="199.99")
    booking = book(client, attendee, event["id"], 3).json()["data"]
    assert booking["total_price"] == "599.97"
    assert sold(client, event["id"]) == 3

    client.patch(
        f"/api/v1/events/{event['id']}", json={"ticket_price": "999.00"}, headers=organizer.headers
    )
    again = client.get(f"/api/v1/bookings/{booking['id']}", headers=attendee.headers).json()["data"]
    assert again["unit_price"] == "199.99" and again["total_price"] == "599.97"


def test_client_cannot_supply_price(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    response = client.post(
        f"/api/v1/events/{event['id']}/bookings",
        json={"quantity": 1, "total_price": "0.01"},
        headers=attendee.headers,
    )
    assert response.status_code == 422


def test_insufficient_capacity(
    client: TestClient, make_user: Callable[..., UserCtx], make_event: Callable[..., dict]
) -> None:
    event = make_event(capacity=3)
    alice, bob = make_user(), make_user()
    assert book(client, alice, event["id"], 2).status_code == 201
    response = book(client, bob, event["id"], 2)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Not enough tickets available: 1 remaining",
        "code": "INSUFFICIENT_CAPACITY",
    }
    assert book(client, bob, event["id"], 1).status_code == 201
    assert book(client, bob, event["id"], 1).status_code == 409
    assert sold(client, event["id"]) == 3


@pytest.mark.parametrize("quantity", [0, -1, "2", 1.5, True, None])
def test_invalid_quantity(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict], quantity: Any
) -> None:
    event = make_event()
    assert book(client, attendee, event["id"], quantity).status_code == 422
    assert sold(client, event["id"]) == 0


def test_quantity_limit(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    response = book(client, attendee, event["id"], 11)
    assert response.status_code == 422
    assert response.json()["code"] == "QUANTITY_LIMIT_EXCEEDED"
    assert book(client, attendee, event["id"], 10).status_code == 201


def test_booking_nonexistent_event(client: TestClient, attendee: UserCtx) -> None:
    response = book(client, attendee, "00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_booking_started_event(
    client: TestClient, organizer: UserCtx, attendee: UserCtx, db: Session
) -> None:
    # Stored status is still UPCOMING (sweep has not run): the clock check must still reject it.
    started = insert_event(db, organizer.id, start_at=utcnow() - timedelta(seconds=1))
    response = book(client, attendee, str(started.id))
    assert response.status_code == 409
    assert response.json()["code"] == "EVENT_ALREADY_STARTED"


def test_booking_cancelled_event(
    client: TestClient, organizer: UserCtx, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    client.post(f"/api/v1/events/{event['id']}/cancel", headers=organizer.headers)
    response = book(client, attendee, event["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "EVENT_CANCELLED"


def test_only_attendees_can_book(
    client: TestClient, organizer: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    assert book(client, organizer, event["id"]).status_code == 403
    assert (
        client.post(f"/api/v1/events/{event['id']}/bookings", json={"quantity": 1}).status_code
        == 401
    )


def test_booking_history_is_private(
    client: TestClient, make_user: Callable[..., UserCtx], make_event: Callable[..., dict]
) -> None:
    event = make_event()
    alice, bob = make_user(), make_user()
    a1 = book(client, alice, event["id"]).json()["data"]
    a2 = book(client, alice, event["id"], 2).json()["data"]
    b1 = book(client, bob, event["id"]).json()["data"]

    mine = client.get("/api/v1/bookings/me", headers=alice.headers).json()
    assert [b["id"] for b in mine["data"]] == [a2["id"], a1["id"]]  # newest first
    assert mine["meta"]["total"] == 2

    assert client.get(f"/api/v1/bookings/{b1['id']}", headers=alice.headers).status_code == 404
    assert (
        client.patch(f"/api/v1/bookings/{b1['id']}/cancel", headers=alice.headers).status_code
        == 404
    )


def test_booking_history_filters(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    keep = book(client, attendee, event["id"]).json()["data"]
    drop = book(client, attendee, event["id"]).json()["data"]
    client.patch(f"/api/v1/bookings/{drop['id']}/cancel", headers=attendee.headers)
    confirmed = client.get(
        "/api/v1/bookings/me", params={"status": "CONFIRMED"}, headers=attendee.headers
    ).json()
    assert [b["id"] for b in confirmed["data"]] == [keep["id"]]
    future = (utcnow() + timedelta(days=1)).isoformat()
    empty = client.get(
        "/api/v1/bookings/me", params={"created_from": future}, headers=attendee.headers
    ).json()
    assert empty["data"] == [] and empty["meta"]["total"] == 0


def test_organizer_of_event_can_view_booking(
    client: TestClient,
    organizer: UserCtx,
    attendee: UserCtx,
    make_user: Callable[..., UserCtx],
    make_event: Callable[..., dict],
) -> None:
    event = make_event()
    booking = book(client, attendee, event["id"]).json()["data"]
    assert (
        client.get(f"/api/v1/bookings/{booking['id']}", headers=organizer.headers).status_code
        == 200
    )
    other_org = make_user("ORGANIZER")
    assert (
        client.get(f"/api/v1/bookings/{booking['id']}", headers=other_org.headers).status_code
        == 404
    )


def test_cancel_booking_releases_inventory(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event(capacity=5)
    booking = book(client, attendee, event["id"], 5).json()["data"]
    assert sold(client, event["id"]) == 5

    response = client.patch(
        f"/api/v1/bookings/{booking['id']}/cancel",
        json={"reason": "Plans changed"},
        headers=attendee.headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "CANCELLED"
    assert data["cancelled_at"] is not None
    assert data["cancellation_reason"] == "Plans changed"
    assert sold(client, event["id"]) == 0

    again = client.patch(f"/api/v1/bookings/{booking['id']}/cancel", headers=attendee.headers)
    assert again.status_code == 409
    assert again.json()["code"] == "BOOKING_ALREADY_CANCELLED"
    assert sold(client, event["id"]) == 0  # inventory released exactly once


def test_cancellation_window(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    soon = utcnow() + timedelta(hours=2)  # inside the default 24h cutoff
    event = make_event(start_at=soon.isoformat(), end_at=(soon + timedelta(hours=1)).isoformat())
    booking = book(client, attendee, event["id"]).json()["data"]
    response = client.patch(f"/api/v1/bookings/{booking['id']}/cancel", headers=attendee.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "CANCELLATION_WINDOW_CLOSED"
    assert sold(client, event["id"]) == 1


def test_refund_pending_booking_cannot_be_cancelled(
    client: TestClient, organizer: UserCtx, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    booking = book(client, attendee, event["id"]).json()["data"]
    client.post(f"/api/v1/events/{event['id']}/cancel", headers=organizer.headers)
    response = client.patch(f"/api/v1/bookings/{booking['id']}/cancel", headers=attendee.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "BOOKING_NOT_CANCELLABLE"


def test_booking_rate_limit(
    client: TestClient,
    attendee: UserCtx,
    make_user: Callable[..., UserCtx],
    make_event: Callable[..., dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(booking_rate_limiter, "limit", 3)
    event = make_event()
    statuses = [book(client, attendee, event["id"]).status_code for _ in range(4)]
    assert statuses == [201, 201, 201, 429]
    limited = book(client, attendee, event["id"])
    assert limited.json()["code"] == "RATE_LIMITED"
    assert int(limited.headers["retry-after"]) > 0
    # Limits are per user.
    assert book(client, make_user(), event["id"]).status_code == 201


def test_tickets_sold_matches_db(
    client: TestClient, attendee: UserCtx, make_event: Callable[..., dict], db: Session
) -> None:
    event = make_event(capacity=7)
    book(client, attendee, event["id"], 4)
    stored = db.get(Event, uuid.UUID(event["id"]))
    assert stored is not None and stored.tickets_sold == 4
