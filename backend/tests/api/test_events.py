from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.utils.time import utcnow
from tests.conftest import UserCtx, event_payload, insert_event

EVENTS = "/api/v1/events"


def test_create_event(client: TestClient, organizer: UserCtx) -> None:
    response = client.post(EVENTS, json=event_payload(), headers=organizer.headers)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["organizer"]["id"] == organizer.id
    assert set(data["organizer"]) == {"id", "name", "role"}
    assert data["status"] == "UPCOMING"
    assert data["tickets_sold"] == 0
    assert data["remaining_capacity"] == 100
    assert data["ticket_price"] == "250.00"
    assert data["is_bookable"] is True
    assert data["start_at"].endswith("Z")  # stored and returned in UTC


def test_attendee_cannot_create_event(client: TestClient, attendee: UserCtx) -> None:
    response = client.post(EVENTS, json=event_payload(), headers=attendee.headers)
    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_ROLE"


def test_unauthenticated_cannot_create_event(client: TestClient) -> None:
    assert client.post(EVENTS, json=event_payload()).status_code == 401


def test_client_cannot_choose_owner_or_counters(client: TestClient, organizer: UserCtx) -> None:
    for extra in ({"organizer_id": organizer.id}, {"tickets_sold": 5}, {"status": "CANCELLED"}):
        response = client.post(EVENTS, json=event_payload(**extra), headers=organizer.headers)
        assert response.status_code == 422, extra


@pytest.mark.parametrize(
    "overrides",
    [
        {"ticket_price": "-1"},
        {"ticket_price": "10.999"},
        {"capacity": 0},
        {"capacity": -5},
        {"capacity": "10"},
        {"title": "ab"},
        {"category": "PARTY"},
        {"image_url": "not a url"},
        {"start_at": "2026-12-01T10:00:00"},  # naive datetime rejected
    ],
)
def test_invalid_event_payload(
    client: TestClient, organizer: UserCtx, overrides: dict[str, Any]
) -> None:
    response = client.post(EVENTS, json=event_payload(**overrides), headers=organizer.headers)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_end_before_start_rejected(client: TestClient, organizer: UserCtx) -> None:
    start = utcnow() + timedelta(days=3)
    body = event_payload(
        start_at=start.isoformat(), end_at=(start - timedelta(hours=1)).isoformat()
    )
    assert client.post(EVENTS, json=body, headers=organizer.headers).status_code == 422


def test_start_in_past_rejected(client: TestClient, organizer: UserCtx) -> None:
    start = utcnow() - timedelta(hours=1)
    body = event_payload(
        start_at=start.isoformat(), end_at=(start + timedelta(hours=3)).isoformat()
    )
    response = client.post(EVENTS, json=body, headers=organizer.headers)
    assert response.status_code == 422
    assert response.json()["code"] == "EVENT_START_IN_PAST"


def test_list_filters_and_search(client: TestClient, make_event: Callable[..., dict]) -> None:
    soon = utcnow() + timedelta(days=2)
    later = utcnow() + timedelta(days=30)
    a = make_event(
        title="Rust Workshop",
        city="Chennai",
        category="WORKSHOP",
        ticket_price="500.00",
        start_at=soon.isoformat(),
        end_at=(soon + timedelta(hours=2)).isoformat(),
    )
    b = make_event(
        title="Jazz Night",
        description="Live jazz performances",
        city="Mumbai",
        category="CONCERT",
        ticket_price="1500.00",
        start_at=later.isoformat(),
        end_at=(later + timedelta(hours=2)).isoformat(),
    )
    c = make_event(title="Free Meetup", city="chennai", category="MEETUP", ticket_price="0")

    def ids(**params: Any) -> list[str]:
        response = client.get(EVENTS, params=params)
        assert response.status_code == 200, response.text
        return [e["id"] for e in response.json()["data"]]

    assert set(ids()) == {a["id"], b["id"], c["id"]}
    assert set(ids(city="CHENNAI")) == {a["id"], c["id"]}
    assert ids(category="CONCERT") == [b["id"]]
    assert set(ids(min_price=100, max_price=1000)) == {a["id"]}
    assert ids(max_price=0) == [c["id"]]
    assert ids(start_from=(utcnow() + timedelta(days=20)).isoformat()) == [b["id"]]
    assert ids(start_to=(utcnow() + timedelta(days=3)).isoformat()) == [a["id"]]
    assert ids(q="performance") == [b["id"]]  # stemmed full-text match on description
    assert ids(q="workshop rust") == [a["id"]]
    assert ids(sort="-ticket_price")[0] == b["id"]
    assert ids(city="Chennai", category="WORKSHOP", min_price=100, max_price=1000) == [a["id"]]


def test_pagination_is_stable_and_bounded(
    client: TestClient, make_event: Callable[..., dict]
) -> None:
    start = utcnow() + timedelta(days=5)
    created = {
        make_event(start_at=start.isoformat(), end_at=(start + timedelta(hours=1)).isoformat())[
            "id"
        ]
        for _ in range(5)
    }
    seen: list[str] = []
    for page in (1, 2, 3):
        body = client.get(EVENTS, params={"page": page, "limit": 2}).json()
        assert body["meta"] == {"page": page, "limit": 2, "total": 5, "pages": 3}
        seen += [e["id"] for e in body["data"]]
    assert len(seen) == 5 and set(seen) == created  # same start time: id tiebreak, no dupes

    assert client.get(EVENTS, params={"limit": 101}).status_code == 422
    assert client.get(EVENTS, params={"page": 0}).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"min_price": 10, "max_price": 5},
        {"min_price": -1},
        {"category": "NOPE"},
        {"unknown": "x"},
        {"sort": "password"},
    ],
)
def test_invalid_filters(client: TestClient, params: dict[str, Any]) -> None:
    response = client.get(EVENTS, params=params)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_listing_hides_cancelled_past_and_deleted(
    client: TestClient, organizer: UserCtx, make_event: Callable[..., dict], db: Session
) -> None:
    visible = make_event()
    cancelled = make_event()
    client.post(f"{EVENTS}/{cancelled['id']}/cancel", headers=organizer.headers)
    deleted = make_event()
    client.delete(f"{EVENTS}/{deleted['id']}", headers=organizer.headers)
    past = insert_event(db, organizer.id, start_at=utcnow() - timedelta(days=2))

    ids = [e["id"] for e in client.get(EVENTS).json()["data"]]
    assert ids == [visible["id"]]
    with_all = client.get(EVENTS, params={"include_past": True, "include_cancelled": True}).json()
    assert {e["id"] for e in with_all["data"]} == {visible["id"], cancelled["id"], str(past.id)}
    assert client.get(f"{EVENTS}/{deleted['id']}").status_code == 404


def test_event_details(client: TestClient, make_event: Callable[..., dict]) -> None:
    event = make_event()
    response = client.get(f"{EVENTS}/{event['id']}")
    assert response.status_code == 200
    assert response.json()["data"]["remaining_capacity"] == 100
    assert "email" not in response.json()["data"]["organizer"]


def test_event_details_not_found(client: TestClient) -> None:
    response = client.get(f"{EVENTS}/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["code"] == "EVENT_NOT_FOUND"
    assert client.get(f"{EVENTS}/not-a-uuid").status_code == 422


def test_owner_updates_event(
    client: TestClient, organizer: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    response = client.patch(
        f"{EVENTS}/{event['id']}",
        json={"title": "Advanced Workshop", "capacity": 150},
        headers=organizer.headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"] == "Advanced Workshop" and data["capacity"] == 150
    assert data["updated_at"] > event["updated_at"]


def test_update_validation(
    client: TestClient, organizer: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    url = f"{EVENTS}/{event['id']}"
    assert client.patch(url, json={}, headers=organizer.headers).status_code == 422
    assert client.patch(url, json={"title": None}, headers=organizer.headers).status_code == 422
    assert (
        client.patch(url, json={"status": "COMPLETED"}, headers=organizer.headers).status_code
        == 422
    )
    # end_at alone moved before the stored start_at
    early_end = (utcnow() + timedelta(days=1)).isoformat()
    response = client.patch(url, json={"end_at": early_end}, headers=organizer.headers)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_DATE_RANGE"
    # nullable fields may be cleared
    assert client.patch(url, json={"address": None}, headers=organizer.headers).status_code == 200


def test_other_organizer_cannot_modify_event(
    client: TestClient, make_user: Callable[..., UserCtx], make_event: Callable[..., dict]
) -> None:
    event = make_event()
    intruder = make_user("ORGANIZER")
    url = f"{EVENTS}/{event['id']}"
    assert (
        client.patch(url, json={"title": "Hijacked"}, headers=intruder.headers).status_code == 403
    )
    assert client.delete(url, headers=intruder.headers).status_code == 403
    assert client.post(f"{url}/cancel", headers=intruder.headers).status_code == 403
    assert client.get(url).json()["data"]["title"] == event["title"]


def test_cannot_edit_started_event(client: TestClient, organizer: UserCtx, db: Session) -> None:
    started = insert_event(db, organizer.id, start_at=utcnow() - timedelta(minutes=5))
    response = client.patch(
        f"{EVENTS}/{started.id}", json={"title": "Too late"}, headers=organizer.headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "EVENT_ALREADY_STARTED"


def test_capacity_cannot_drop_below_tickets_sold(
    client: TestClient, organizer: UserCtx, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event(capacity=10)
    client.post(f"{EVENTS}/{event['id']}/bookings", json={"quantity": 4}, headers=attendee.headers)
    url = f"{EVENTS}/{event['id']}"
    response = client.patch(url, json={"capacity": 3}, headers=organizer.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "CAPACITY_BELOW_TICKETS_SOLD"
    assert client.patch(url, json={"capacity": 4}, headers=organizer.headers).status_code == 200


def test_delete_event_rules(
    client: TestClient,
    organizer: UserCtx,
    attendee: UserCtx,
    make_event: Callable[..., dict],
    db: Session,
) -> None:
    empty = make_event()
    deleted = client.delete(f"{EVENTS}/{empty['id']}", headers=organizer.headers)
    assert deleted.status_code == 204
    assert deleted.content == b"" and "content-type" not in deleted.headers
    assert client.delete(f"{EVENTS}/{empty['id']}", headers=organizer.headers).status_code == 404

    booked = make_event()
    client.post(f"{EVENTS}/{booked['id']}/bookings", json={"quantity": 1}, headers=attendee.headers)
    response = client.delete(f"{EVENTS}/{booked['id']}", headers=organizer.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "EVENT_HAS_ACTIVE_BOOKINGS"

    # After cancelling, deletion is allowed and the attendee keeps their booking history.
    client.post(f"{EVENTS}/{booked['id']}/cancel", headers=organizer.headers)
    assert client.delete(f"{EVENTS}/{booked['id']}", headers=organizer.headers).status_code == 204
    history = client.get("/api/v1/bookings/me", headers=attendee.headers).json()["data"]
    assert len(history) == 1 and history[0]["status"] == "REFUND_PENDING"

    started = insert_event(db, organizer.id, start_at=utcnow() - timedelta(minutes=1))
    response = client.delete(f"{EVENTS}/{started.id}", headers=organizer.headers)
    assert response.status_code == 409


def test_cancel_event_moves_bookings_to_refund_pending(
    client: TestClient,
    organizer: UserCtx,
    make_user: Callable[..., UserCtx],
    make_event: Callable[..., dict],
) -> None:
    event = make_event(capacity=10, ticket_price="100.00")
    alice, bob = make_user(), make_user()
    b1 = client.post(
        f"{EVENTS}/{event['id']}/bookings", json={"quantity": 2}, headers=alice.headers
    ).json()["data"]
    b2 = client.post(
        f"{EVENTS}/{event['id']}/bookings", json={"quantity": 3}, headers=bob.headers
    ).json()["data"]
    client.patch(
        f"/api/v1/bookings/{b2['id']}/cancel", headers=bob.headers
    )  # already cancelled: untouched

    response = client.post(
        f"{EVENTS}/{event['id']}/cancel",
        json={"reason": "Venue unavailable"},
        headers=organizer.headers,
    )
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["affected_bookings"] == 1
    assert body["event"]["status"] == "CANCELLED" and body["event"]["is_bookable"] is False

    first = client.get(f"/api/v1/bookings/{b1['id']}", headers=alice.headers).json()["data"]
    assert first["status"] == "REFUND_PENDING"
    assert first["cancellation_reason"] == "Venue unavailable"
    second = client.get(f"/api/v1/bookings/{b2['id']}", headers=bob.headers).json()["data"]
    assert second["status"] == "CANCELLED"

    again = client.post(f"{EVENTS}/{event['id']}/cancel", headers=organizer.headers)
    assert again.status_code == 409
    assert again.json()["code"] == "EVENT_ALREADY_CANCELLED"

    booking = client.post(
        f"{EVENTS}/{event['id']}/bookings", json={"quantity": 1}, headers=alice.headers
    )
    assert booking.status_code == 409
    assert booking.json()["code"] == "EVENT_CANCELLED"
    edit = client.patch(
        f"{EVENTS}/{event['id']}", json={"title": "Back on"}, headers=organizer.headers
    )
    assert edit.status_code == 409


def test_cannot_cancel_ended_event(client: TestClient, organizer: UserCtx, db: Session) -> None:
    ended = insert_event(db, organizer.id, start_at=utcnow() - timedelta(days=2))
    response = client.post(f"{EVENTS}/{ended.id}/cancel", headers=organizer.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "EVENT_ALREADY_ENDED"
